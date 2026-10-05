"""Fail-closed streaming gate for explicit citation blocks in generated text."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum

from isnad_core.engine import VerificationEngine
from isnad_core.errors import SourceUnavailable
from isnad_core.hadith import InvalidHadithReference
from isnad_core.models import VerificationInput, VerificationResult
from isnad_core.quran import InvalidQuranReference
from isnad_core.quran.verifier import MAX_QUOTE_CHARACTERS

OPEN_MARKER = "[[ISNAD-CITATION"
CLOSE_MARKER = "[[/ISNAD-CITATION]]"
MAX_HEADER_CHARACTERS = 256
MAX_STREAM_CHARACTERS = 1_000_000
MAX_STREAM_CHUNK_CHARACTERS = 16_384
MAX_WEBSOCKET_MESSAGE_BYTES = 96 * 1024

_QURAN_REFERENCE = re.compile(r"[0-9٠-٩۰-۹]+:[0-9٠-٩۰-۹]+(?:[-–][0-9٠-٩۰-۹]+)?")
_HADITH_REFERENCE = re.compile(r"(?i)hadeethenc:[0-9]{1,10}")


class StreamEventType(StrEnum):
    """Stable event kinds emitted by the citation stream gate."""

    TEXT = "text"
    CITATION_CHECKING = "citation_checking"
    CITATION_RESULT = "citation_result"
    CITATION_REJECTED = "citation_rejected"
    CITATION_ERROR = "citation_error"


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One safe prose fragment or one citation-verification lifecycle event."""

    type: StreamEventType
    text: str | None = None
    citation_id: int | None = None
    placeholder: str | None = None
    result: VerificationResult | None = None
    code: str | None = None


class StreamingCitationGate:
    """Incrementally hold marked quote/reference blocks until the shared core checks them.

    A block is written as::

        [[ISNAD-CITATION source=quran language=ar reference=2:255]]quote[[/ISNAD-CITATION]]

    ``reference`` is optional when the quote alone is to be searched. Only text outside
    a block is released before verification. Malformed, nested, oversized, and incomplete
    blocks are rejected without including their untrusted quote text in an event. Inside a
    quote, backslash-escape a literal marker; paired backslashes preserve literal backslashes.
    """

    def __init__(
        self,
        engine: VerificationEngine,
        *,
        max_quote_characters: int = MAX_QUOTE_CHARACTERS,
    ) -> None:
        if max_quote_characters < 1:
            raise ValueError("max_quote_characters must be positive.")
        self._engine = engine
        self._max_quote_characters = max_quote_characters
        self._mode = "outside"
        self._pending = ""
        self._header = ""
        self._body_parts: list[str] = []
        self._body_length = 0
        self._citation_id = 0
        self._current_citation_id: int | None = None
        self._source: str | None = None
        self._language: str | None = None
        self._reference: str | None = None
        self._rejected = False
        self._discard_depth = 0
        self._finished = False

    def feed(self, chunk: str) -> Iterator[StreamEvent]:
        """Accept one model-output fragment and yield safe events incrementally.

        This is a generator intentionally: callers can send the checking placeholder as
        soon as it is yielded, before requesting the next event and triggering lookup.
        """

        if self._finished:
            raise RuntimeError("The stream gate is already finished.")
        if not isinstance(chunk, str):
            raise TypeError("Streaming chunks must be strings.")
        if len(chunk) > MAX_STREAM_CHUNK_CHARACTERS:
            raise ValueError("A streaming input chunk exceeds the 16,384-character limit.")
        self._pending += chunk
        yield from self._drain()

    def finish(self) -> Iterator[StreamEvent]:
        """Finish the stream, rejecting any citation block that never closed."""

        if self._finished:
            raise RuntimeError("The stream gate is already finished.")
        self._finished = True
        if self._mode == "outside" and self._pending:
            partial = _partial_marker_suffix(self._pending, (OPEN_MARKER,))
            if partial is None:
                text = self._pending
                self._pending = ""
                yield StreamEvent(StreamEventType.TEXT, text=text)
                return

            escape_start, marker_start = partial
            slash_count = marker_start - escape_start
            prefix = self._pending[:escape_start] + "\\" * (slash_count // 2)
            marker_prefix = self._pending[marker_start:]
            self._pending = ""
            if slash_count % 2:
                literal_text = prefix + marker_prefix
                if literal_text:
                    yield StreamEvent(StreamEventType.TEXT, text=literal_text)
                return

            if prefix:
                yield StreamEvent(StreamEventType.TEXT, text=prefix)
            self._citation_id += 1
            citation_id = self._citation_id
            yield StreamEvent(
                StreamEventType.CITATION_CHECKING,
                citation_id=citation_id,
                placeholder="checking_citation",
            )
            yield StreamEvent(
                StreamEventType.CITATION_REJECTED,
                citation_id=citation_id,
                code="incomplete_citation_marker",
            )
            return

        if self._mode in {"header", "body"}:
            citation_id = self._current_citation_id
            self._pending = ""
            self._reset_after_citation()
            yield StreamEvent(
                StreamEventType.CITATION_REJECTED,
                citation_id=citation_id,
                code="incomplete_citation",
            )
        self._pending = ""
        self._body_parts.clear()

    def _drain(self) -> Iterator[StreamEvent]:
        while True:
            if self._mode == "outside":
                marker = _first_marker(self._pending, (OPEN_MARKER,))
                if marker is not None:
                    escape_start, marker_at, marker_text, slash_count = marker
                    prefix = self._pending[:escape_start] + "\\" * (slash_count // 2)
                    self._pending = self._pending[marker_at + len(marker_text) :]
                    if slash_count % 2:
                        literal_text = prefix + marker_text
                        if literal_text:
                            yield StreamEvent(StreamEventType.TEXT, text=literal_text)
                        continue

                    self._begin_citation()
                    if prefix:
                        yield StreamEvent(StreamEventType.TEXT, text=prefix)
                    yield StreamEvent(
                        StreamEventType.CITATION_CHECKING,
                        citation_id=self._current_citation_id,
                        placeholder="checking_citation",
                    )
                    continue

                partial = _partial_marker_suffix(self._pending, (OPEN_MARKER,))
                safe_length = partial[0] if partial is not None else len(self._pending)
                if safe_length:
                    safe_text = self._pending[:safe_length]
                    self._pending = self._pending[safe_length:]
                    yield StreamEvent(StreamEventType.TEXT, text=safe_text)
                return

            if self._mode == "header":
                header_end = self._pending.find("]]")
                if header_end < 0:
                    self._header += self._pending
                    self._pending = ""
                    if len(self._header) > MAX_HEADER_CHARACTERS:
                        yield from self._reject_and_discard("malformed_citation_header")
                    return

                self._header += self._pending[:header_end]
                self._pending = self._pending[header_end + 2 :]
                parsed_header = _parse_header(self._header)
                if parsed_header is None:
                    yield from self._reject_and_discard("malformed_citation_header")
                    continue
                self._source, self._language, self._reference = parsed_header
                self._mode = "body"
                self._header = ""
                continue

            if self._mode == "body":
                marker = _first_marker(self._pending, (OPEN_MARKER, CLOSE_MARKER))
                if marker is not None:
                    escape_start, marker_at, marker_text, slash_count = marker
                    prefix = self._pending[:escape_start] + "\\" * (slash_count // 2)
                    if slash_count % 2:
                        if not self._append_body(prefix + marker_text):
                            yield self._too_large_event()
                            continue
                        self._pending = self._pending[marker_at + len(marker_text) :]
                        continue

                    if not self._append_body(prefix):
                        yield self._too_large_event()
                        continue
                    self._pending = self._pending[marker_at + len(marker_text) :]
                    if marker_text == OPEN_MARKER:
                        self._discard_depth = 1
                        yield from self._reject_and_discard("nested_citation")
                    else:
                        yield from self._verify_current_citation()
                    continue

                partial = _partial_marker_suffix(self._pending, (OPEN_MARKER, CLOSE_MARKER))
                safe_length = partial[0] if partial is not None else len(self._pending)
                if safe_length and not self._append_body(self._pending[:safe_length]):
                    yield self._too_large_event()
                    continue
                self._pending = self._pending[safe_length:]
                return

            if self._mode == "discard":
                yield from self._drain_discarded_block()
                return

            raise RuntimeError(f"Unknown stream-gate mode: {self._mode}")

    def _begin_citation(self) -> None:
        self._citation_id += 1
        self._current_citation_id = self._citation_id
        self._mode = "header"
        self._header = ""
        self._body_parts.clear()
        self._body_length = 0
        self._source = None
        self._language = None
        self._reference = None
        self._rejected = False
        self._discard_depth = 0

    def _append_body(self, text: str) -> bool:
        if self._body_length + len(text) > self._max_quote_characters:
            self._body_parts.clear()
            self._body_length = 0
            self._rejected = True
            self._mode = "discard"
            return False
        self._body_parts.append(text)
        self._body_length += len(text)
        return True

    def _too_large_event(self) -> StreamEvent:
        return StreamEvent(
            StreamEventType.CITATION_REJECTED,
            citation_id=self._current_citation_id,
            code="citation_too_large",
        )

    def _reject_and_discard(self, code: str) -> Iterator[StreamEvent]:
        should_emit = not self._rejected
        self._rejected = True
        self._body_parts.clear()
        self._body_length = 0
        self._header = ""
        self._mode = "discard"
        if should_emit:
            yield StreamEvent(
                StreamEventType.CITATION_REJECTED,
                citation_id=self._current_citation_id,
                code=code,
            )

    def _drain_discarded_block(self) -> Iterator[StreamEvent]:
        while True:
            marker = _first_marker(self._pending, (OPEN_MARKER, CLOSE_MARKER))
            if marker is not None:
                _, marker_at, marker_text, slash_count = marker
                self._pending = self._pending[marker_at + len(marker_text) :]
                if slash_count % 2:
                    continue
                if marker_text == OPEN_MARKER:
                    self._discard_depth += 1
                    continue
                if self._discard_depth:
                    self._discard_depth -= 1
                    continue
                self._reset_after_citation()
                yield from self._drain()
                return

            partial = _partial_marker_suffix(self._pending, (OPEN_MARKER, CLOSE_MARKER))
            safe_length = partial[0] if partial is not None else len(self._pending)
            self._pending = self._pending[safe_length:]
            return

    def _verify_current_citation(self) -> Iterator[StreamEvent]:
        citation_id = self._current_citation_id
        quote = "".join(self._body_parts)
        source = self._source
        language = self._language
        reference = self._reference
        if not quote.strip() or source is None or language is None:
            event = StreamEvent(
                StreamEventType.CITATION_REJECTED,
                citation_id=citation_id,
                code="empty_or_invalid_citation",
            )
        else:
            try:
                result = self._engine.verify(
                    VerificationInput(
                        source=source,
                        language=language,
                        quote=quote,
                        reference=reference,
                    )
                )
            except SourceUnavailable:
                event = StreamEvent(
                    StreamEventType.CITATION_ERROR,
                    citation_id=citation_id,
                    code="source_unavailable",
                )
            except (InvalidHadithReference, InvalidQuranReference, ValueError):
                event = StreamEvent(
                    StreamEventType.CITATION_ERROR,
                    citation_id=citation_id,
                    code="invalid_citation",
                )
            else:
                event = StreamEvent(
                    StreamEventType.CITATION_RESULT,
                    citation_id=citation_id,
                    result=result,
                )

        self._reset_after_citation()
        yield event

    def _reset_after_citation(self) -> None:
        self._mode = "outside"
        self._header = ""
        self._body_parts.clear()
        self._body_length = 0
        self._current_citation_id = None
        self._source = None
        self._language = None
        self._reference = None
        self._rejected = False
        self._discard_depth = 0


def _parse_header(header: str) -> tuple[str, str, str | None] | None:
    attributes: dict[str, str] = {}
    for token in header.split():
        key, separator, value = token.partition("=")
        if not separator or key not in {"source", "language", "reference"} or not value:
            return None
        if key in attributes:
            return None
        attributes[key] = value

    source = attributes.get("source", "").casefold()
    language = attributes.get("language", "").casefold()
    reference = attributes.get("reference")
    if source not in {"quran", "hadith"} or language not in {"ar", "en"}:
        return None
    if reference is not None:
        pattern = _QURAN_REFERENCE if source == "quran" else _HADITH_REFERENCE
        if not pattern.fullmatch(reference):
            return None
    return source, language, reference


def _first_marker(value: str, markers: tuple[str, ...]) -> tuple[int, int, str, int] | None:
    candidates: list[tuple[int, int, str, int]] = []
    for marker in markers:
        marker_at = value.find(marker)
        if marker_at < 0:
            continue
        escape_start = marker_at
        while escape_start > 0 and value[escape_start - 1] == "\\":
            escape_start -= 1
        candidates.append((escape_start, marker_at, marker, marker_at - escape_start))
    return min(candidates, key=lambda candidate: candidate[1]) if candidates else None


def _partial_marker_suffix(value: str, markers: tuple[str, ...]) -> tuple[int, int] | None:
    prefix_length = _longest_marker_prefix_suffix(value, markers)
    if prefix_length == 0:
        return None
    marker_start = len(value) - prefix_length
    escape_start = marker_start
    while escape_start > 0 and value[escape_start - 1] == "\\":
        escape_start -= 1
    return escape_start, marker_start


def _longest_marker_prefix_suffix(value: str, markers: tuple[str, ...]) -> int:
    """Return how many trailing chars could begin one of the delimiters."""

    maximum = min(len(value), max(map(len, markers)) - 1)
    for length in range(maximum, 0, -1):
        suffix = value[-length:]
        if any(marker.startswith(suffix) for marker in markers):
            return length
    return 0
