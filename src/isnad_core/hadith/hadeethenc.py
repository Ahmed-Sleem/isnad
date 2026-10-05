"""HadeethEnc API adapter with source-bounded claims and separate grading fields."""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from isnad_core.errors import SourceUnavailable
from isnad_core.models import (
    Evidence,
    MatchStatus,
    SourceMetadata,
    VerificationInput,
    VerificationResult,
    WordingDifference,
)
from isnad_core.normalization import NormalizedText, normalize_with_spans
from isnad_core.quran.verifier import MAX_MATCH_CANDIDATES, MAX_QUOTE_CHARACTERS

_HADEETHENC_API_ROOT = "https://hadeethenc.com/api/v1/"
_HADEETHENC_WEB_ROOT = "https://hadeethenc.com"
_REFERENCE_PATTERN = re.compile(r"(?i)^hadeethenc:(?P<record_id>[0-9]{1,10})$")
_MAX_CACHED_ENTRIES = 256
_CACHE_TTL_SECONDS = 180.0
_MAX_RESPONSE_BYTES = 2_000_000
_USER_AGENT = "Isnad-Core/0.1 (source-grounded citation verification)"


class InvalidHadithReference(ValueError):
    """Raised when a supplied HadeethEnc record locator is malformed."""


@dataclass(frozen=True, slots=True)
class _Occurrence:
    """One match span within one source record."""

    record: Mapping[str, Any]
    reference: str
    matched_fragment: str
    full_match: bool
    raw_exact: bool


class HadeethEncClient:
    """Small bounded client for HadeethEnc's documented v1 JSON API."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._owns_client = client is None
        self._client = (
            client
            if client is not None
            else httpx.Client(
                base_url=_HADEETHENC_API_ROOT,
                timeout=httpx.Timeout(8.0, connect=2.5),
                follow_redirects=True,
                headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
            )
        )
        self._search_cache: OrderedDict[tuple[str, str], tuple[float, tuple[str, ...], bool]] = (
            OrderedDict()
        )
        self._record_cache: OrderedDict[tuple[str, str], tuple[float, dict[str, Any]]] = (
            OrderedDict()
        )
        self._cache_lock = threading.RLock()

    def search(self, phrase: str, *, language: str) -> tuple[tuple[str, ...], bool]:
        """Search the source and return bounded record IDs plus truncation state."""

        self._validate_language(language)
        phrase_key = hashlib.sha256(phrase.encode("utf-8")).hexdigest()
        cache_key = (language, phrase_key)
        cached = self._get_cached_search(cache_key)
        if cached is not None:
            ids, truncated = cached
            return ids, truncated

        payload = self._get_json(
            "hadeeths/search/",
            params={"phrase": phrase, "language": language},
            allow_empty=False,
        )
        if not isinstance(payload, list):
            raise SourceUnavailable("HadeethEnc search returned an unexpected response shape.")
        record_ids: list[str] = []
        for item in payload:
            if not isinstance(item, dict):
                raise SourceUnavailable("HadeethEnc search returned a malformed record.")
            record_id = self._validated_record_id(item.get("id"))
            if record_id not in record_ids:
                record_ids.append(record_id)
        truncated = len(record_ids) > MAX_MATCH_CANDIDATES
        bounded_ids = tuple(record_ids[:MAX_MATCH_CANDIDATES])
        self._store_search(cache_key, bounded_ids, truncated)
        return bounded_ids, truncated

    def multiple(self, record_ids: tuple[str, ...], *, language: str) -> tuple[dict[str, Any], ...]:
        """Fetch multiple full source records, validating every returned ID."""

        self._validate_language(language)
        if not record_ids:
            return ()
        cache_keys = tuple((language, record_id) for record_id in record_ids)
        cached_records: dict[str, dict[str, Any]] = {}
        missing: list[str] = []
        with self._cache_lock:
            for record_id, cache_key in zip(record_ids, cache_keys, strict=True):
                cached = self._record_cache.get(cache_key)
                if cached is not None and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
                    self._record_cache.move_to_end(cache_key)
                    cached_records[record_id] = dict(cached[1])
                else:
                    self._record_cache.pop(cache_key, None)
                    missing.append(record_id)

        if missing:
            payload = self._get_json(
                "hadeeths/multiple/",
                params={"ids": ",".join(missing), "language": language},
                allow_empty=False,
            )
            if not isinstance(payload, list):
                raise SourceUnavailable("HadeethEnc record lookup returned an unexpected response.")
            received: dict[str, dict[str, Any]] = {}
            for item in payload:
                record = self._validate_record(item)
                received[record["id"]] = record
            if set(received) != set(missing):
                raise SourceUnavailable("HadeethEnc returned an incomplete record response.")
            for record_id, record in received.items():
                self._store_record((language, record_id), record)
                cached_records[record_id] = record

        return tuple(cached_records[record_id] for record_id in record_ids)

    def one(self, record_id: str, *, language: str) -> dict[str, Any] | None:
        """Fetch a single HadeethEnc record; an empty response means not located."""

        self._validate_language(language)
        record_id = self._validated_record_id(record_id)
        cache_key = (language, record_id)
        with self._cache_lock:
            cached = self._record_cache.get(cache_key)
            if cached is not None and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
                self._record_cache.move_to_end(cache_key)
                return dict(cached[1])
            self._record_cache.pop(cache_key, None)

        payload = self._get_json(
            "hadeeths/one/",
            params={"id": record_id, "language": language},
            allow_empty=True,
        )
        if payload is None or payload == {}:
            return None
        record = self._validate_record(payload)
        if record["id"] != record_id:
            raise SourceUnavailable("HadeethEnc returned a different record than requested.")
        self._store_record(cache_key, record)
        return record

    @staticmethod
    def _validate_language(language: str) -> None:
        if language not in {"ar", "en"}:
            raise ValueError("HadeethEnc verification supports Arabic and English only.")

    @staticmethod
    def _validated_record_id(value: object) -> str:
        record_id = str(value) if value is not None else ""
        if not re.fullmatch(r"[0-9]{1,10}", record_id) or int(record_id) < 1:
            raise SourceUnavailable("HadeethEnc returned an invalid record identifier.")
        return str(int(record_id))

    @classmethod
    def _validate_record(cls, payload: object) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise SourceUnavailable("HadeethEnc returned a malformed record.")
        record_id = cls._validated_record_id(payload.get("id"))
        text = payload.get("hadeeth")
        title = payload.get("title")
        if not isinstance(text, str) or not text or not isinstance(title, str):
            raise SourceUnavailable("HadeethEnc returned a record without source text.")
        if len(text.encode("utf-8")) > _MAX_RESPONSE_BYTES:
            raise SourceUnavailable("HadeethEnc returned an oversized hadith record.")
        record = dict(payload)
        record["id"] = record_id
        record["hadeeth"] = text
        record["title"] = title
        return record

    def _get_json(
        self,
        path: str,
        *,
        params: dict[str, str],
        allow_empty: bool,
    ) -> Any:
        try:
            response = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise SourceUnavailable("HadeethEnc could not be reached.") from exc
        if response.status_code == 404 and allow_empty:
            return None
        if response.status_code < 200 or response.status_code >= 300:
            raise SourceUnavailable("HadeethEnc returned an unsuccessful response.")
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise SourceUnavailable("HadeethEnc returned an oversized response.")
        if not response.content:
            if allow_empty:
                return None
            raise SourceUnavailable("HadeethEnc returned an empty response.")
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise SourceUnavailable("HadeethEnc returned invalid JSON.") from exc

    def _get_cached_search(self, key: tuple[str, str]) -> tuple[tuple[str, ...], bool] | None:
        with self._cache_lock:
            cached = self._search_cache.get(key)
            if cached is None:
                return None
            created_at, ids, truncated = cached
            if time.monotonic() - created_at >= _CACHE_TTL_SECONDS:
                del self._search_cache[key]
                return None
            self._search_cache.move_to_end(key)
            return ids, truncated

    def _store_search(
        self,
        key: tuple[str, str],
        ids: tuple[str, ...],
        truncated: bool,
    ) -> None:
        with self._cache_lock:
            self._search_cache[key] = (time.monotonic(), ids, truncated)
            self._search_cache.move_to_end(key)
            while len(self._search_cache) > _MAX_CACHED_ENTRIES:
                self._search_cache.popitem(last=False)

    def _store_record(self, key: tuple[str, str], record: dict[str, Any]) -> None:
        with self._cache_lock:
            self._record_cache[key] = (time.monotonic(), dict(record))
            self._record_cache.move_to_end(key)
            while len(self._record_cache) > _MAX_CACHED_ENTRIES:
                self._record_cache.popitem(last=False)

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""

        if self._owns_client:
            self._client.close()


class HadeethEncVerifier:
    """Compare Arabic or English hadith quotations against HadeethEnc's API."""

    def __init__(self, language: str, client: HadeethEncClient | None = None) -> None:
        if language not in {"ar", "en"}:
            raise ValueError("HadeethEnc verifier language must be Arabic or English.")
        self._language = language
        self._client = client if client is not None else HadeethEncClient()
        self._source = SourceMetadata(
            source_id="hadeethenc-api-v1",
            name="HadeethEnc.com",
            url=f"{_HADEETHENC_WEB_ROOT}/{language}/home",
            version="API v1",
            license=(
                "HadeethEnc reuse terms: do not modify content; identify the publisher and source."
            ),
            coverage_note=(
                "Curated selection, not a comprehensive hadith corpus. A missing result "
                "means only that the checked HadeethEnc source did not locate it."
            ),
        )

    @property
    def source_type(self) -> str:
        return "hadith"

    @property
    def language(self) -> str:
        return self._language

    @property
    def source_metadata(self) -> SourceMetadata:
        return self._source

    @property
    def normalization_profile(self) -> str:
        return "arabic_compare_v1" if self._language == "ar" else "english_compare_v1"

    @property
    def mode(self) -> str:
        return "official_remote_api"

    @property
    def reference_format(self) -> str:
        return "hadeethenc:<record_id>"

    def verify(self, citation: VerificationInput) -> VerificationResult:
        """Compare one submitted quotation and optional HadeethEnc record locator."""

        source_type = citation.source.strip().casefold()
        language = citation.language.strip().casefold()
        if source_type != "hadith" or language != self._language:
            return VerificationResult(
                status=MatchStatus.UNSUPPORTED_SOURCE_OR_LANGUAGE,
                source_type=source_type,
                language=language,
                source_metadata=None,
                submitted_quote=citation.quote,
                cited_reference=citation.reference,
                matched_references=(),
                evidence=(),
                wording_differences=(),
                explanation=(
                    f"This adapter checks {self._language} hadith text in the curated "
                    "HadeethEnc.com selection only."
                ),
            )

        quote = citation.quote.strip() if citation.quote is not None else ""
        if len(quote) > MAX_QUOTE_CHARACTERS:
            raise ValueError(f"Quote exceeds the {MAX_QUOTE_CHARACTERS}-character limit.")
        record_id = self._parse_reference(citation.reference) if citation.reference else None
        if not quote:
            if record_id is None:
                raise ValueError("Provide a quote, a HadeethEnc reference, or both.")
            record = self._client.one(record_id, language=self._language)
            if record is None:
                return self._not_found_result(citation, record_id=record_id)
            reference = f"hadeethenc:{record_id}"
            return VerificationResult(
                status=MatchStatus.REFERENCE_FOUND_WITHOUT_QUOTE,
                source_type="hadith",
                language=self._language,
                source_metadata=self._source,
                submitted_quote=citation.quote,
                cited_reference=reference,
                matched_references=(reference,),
                evidence=(self._evidence(record, matched_fragment=None),),
                wording_differences=(),
                explanation=(
                    "The HadeethEnc record was located. Its supplied grade and attribution "
                    "are shown separately and are not a ruling by this verifier."
                ),
            )

        if record_id is not None:
            cited_record = self._client.one(record_id, language=self._language)
            if cited_record is not None:
                cited_occurrences = self._locate(cited_record, quote)
                if cited_occurrences:
                    if len(cited_occurrences) == 1:
                        occurrence = cited_occurrences[0]
                        status = self._status_for_occurrence(occurrence)
                        differences = (
                            self._wording_differences(quote, occurrence.matched_fragment)
                            if status is not MatchStatus.EXACT_MATCH
                            else ()
                        )
                        return self._result_for_occurrences(
                            citation,
                            status,
                            (occurrence,),
                            cited_reference=f"hadeethenc:{record_id}",
                            differences=differences,
                            explanation=(
                                "The quote matches text in the cited HadeethEnc record. "
                                "This textual comparison does not establish authenticity "
                                "or a religious ruling."
                            ),
                        )
                    return self._result_for_occurrences(
                        citation,
                        MatchStatus.AMBIGUOUS_MULTIPLE_MATCHES,
                        tuple(cited_occurrences),
                        cited_reference=f"hadeethenc:{record_id}",
                        differences=(),
                        explanation=(
                            "The quote occurs more than once in the cited HadeethEnc record."
                        ),
                    )

            global_occurrences, search_truncated = self._search_occurrences(quote)
            if len(global_occurrences) == 1 and not search_truncated:
                return self._result_for_occurrences(
                    citation,
                    MatchStatus.QUOTE_FOUND_WRONG_REFERENCE,
                    global_occurrences,
                    cited_reference=f"hadeethenc:{record_id}",
                    differences=self._wording_differences(
                        quote, global_occurrences[0].matched_fragment
                    ),
                    explanation=(
                        "The quote was located in a different HadeethEnc record than the "
                        "cited record ID. This is a locator mismatch, not a hadith grade."
                    ),
                )
            if len(global_occurrences) > 1 or search_truncated and global_occurrences:
                return self._result_for_occurrences(
                    citation,
                    MatchStatus.AMBIGUOUS_MULTIPLE_MATCHES,
                    global_occurrences,
                    cited_reference=f"hadeethenc:{record_id}",
                    differences=(),
                    explanation=(
                        "The wording has multiple or truncated candidate locations in the "
                        "checked HadeethEnc source; a unique record cannot be established."
                    ),
                    truncated=search_truncated,
                )
            if cited_record is None:
                return self._not_found_result(
                    citation,
                    record_id=record_id,
                    search_truncated=search_truncated,
                )
            return self._result_for_occurrences(
                citation,
                MatchStatus.MISMATCH_AT_CITED_REFERENCE,
                (),
                cited_reference=f"hadeethenc:{record_id}",
                differences=self._wording_differences(quote, cited_record["hadeeth"]),
                explanation=(
                    "The HadeethEnc record exists, but the quote does not match its text. "
                    "No conclusion about the report's authenticity is made."
                ),
                cited_record=cited_record,
            )

        occurrences, search_truncated = self._search_occurrences(quote)
        if not occurrences:
            return self._not_found_result(citation, search_truncated=search_truncated)
        if len(occurrences) > 1 or search_truncated:
            return self._result_for_occurrences(
                citation,
                MatchStatus.AMBIGUOUS_MULTIPLE_MATCHES,
                occurrences,
                cited_reference=None,
                differences=(),
                explanation=(
                    "The wording has multiple or truncated candidate locations in the "
                    "checked HadeethEnc source; a unique record cannot be established."
                ),
                truncated=search_truncated,
            )
        occurrence = occurrences[0]
        status = self._status_for_occurrence(occurrence)
        return self._result_for_occurrences(
            citation,
            status,
            occurrences,
            cited_reference=None,
            differences=(
                self._wording_differences(quote, occurrence.matched_fragment)
                if status is not MatchStatus.EXACT_MATCH
                else ()
            ),
            explanation=(
                f"The quote was located in the checked HadeethEnc.com {self._language} "
                "selection. This textual comparison does not establish authenticity "
                "or a religious ruling."
            ),
        )

    @staticmethod
    def _parse_reference(reference: str) -> str:
        candidate = reference.strip()
        match = _REFERENCE_PATTERN.fullmatch(candidate)
        if match is None:
            raise InvalidHadithReference(
                "Use a HadeethEnc record reference such as hadeethenc:4560."
            )
        numeric_id = int(match.group("record_id"))
        if numeric_id < 1:
            raise InvalidHadithReference("A HadeethEnc record ID must be positive.")
        return str(numeric_id)

    def _search_occurrences(self, quote: str) -> tuple[tuple[_Occurrence, ...], bool]:
        record_ids, truncated = self._client.search(quote, language=self._language)
        if not record_ids:
            return (), truncated
        records = self._client.multiple(record_ids, language=self._language)
        occurrences = tuple(
            occurrence for record in records for occurrence in self._locate(record, quote)
        )
        return occurrences, truncated

    def _locate(self, record: Mapping[str, Any], quote: str) -> tuple[_Occurrence, ...]:
        source_text = record["hadeeth"]
        source_normalized = self._normalize(source_text)
        normalized_quote = self._normalize(quote).value
        if not normalized_quote:
            raise ValueError("The quote contains no searchable wording.")

        occurrences: list[_Occurrence] = []
        start = 0
        while True:
            found_at = source_normalized.value.find(normalized_quote, start)
            if found_at < 0:
                break
            end_at = found_at + len(normalized_quote)
            start = found_at + 1
            if not self._has_word_boundaries(source_normalized.value, found_at, end_at):
                continue
            source_span = source_normalized.raw_span(found_at, end_at)
            if source_span is None:
                continue
            matched_fragment = source_text[source_span[0] : source_span[1]]
            canonical = source_normalized.value
            is_full_match = normalized_quote == canonical
            occurrences.append(
                _Occurrence(
                    record=record,
                    reference=f"hadeethenc:{record['id']}",
                    matched_fragment=matched_fragment,
                    full_match=is_full_match,
                    raw_exact=is_full_match and quote.strip() == source_text,
                )
            )
        return tuple(occurrences)

    def _normalize(self, text: str) -> NormalizedText:
        return normalize_with_spans(text, language=self._language)

    @staticmethod
    def _has_word_boundaries(text: str, start: int, end: int) -> bool:
        before_is_word = start > 0 and text[start - 1].isalnum()
        after_is_word = end < len(text) and text[end].isalnum()
        return not before_is_word and not after_is_word

    @staticmethod
    def _status_for_occurrence(occurrence: _Occurrence) -> MatchStatus:
        if not occurrence.full_match:
            return MatchStatus.PARTIAL_MATCH
        if occurrence.raw_exact:
            return MatchStatus.EXACT_MATCH
        return MatchStatus.NORMALIZED_MATCH

    def _evidence(self, record: Mapping[str, Any], matched_fragment: str | None) -> Evidence:
        record_id = str(record["id"])
        grade_text = record.get("grade")
        if not isinstance(grade_text, str) or not grade_text.strip():
            grade_text = None
        attribution = record.get("attribution")
        if not isinstance(attribution, str) or not attribution.strip():
            attribution = None
        bibliographic_reference = record.get("reference")
        if not isinstance(bibliographic_reference, str) or not bibliographic_reference.strip():
            bibliographic_reference = None
        return Evidence(
            reference=f"hadeethenc:{record_id}",
            source_text=str(record["hadeeth"]),
            matched_fragment=matched_fragment,
            source_id=self._source.source_id,
            source_version=self._source.version,
            source_url=f"{_HADEETHENC_WEB_ROOT}/{self._language}/browse/hadith/{record_id}",
            record_title=str(record["title"]),
            attribution_text=attribution,
            grade_text=grade_text,
            grade_source=(f"HadeethEnc.com record {record_id}" if grade_text is not None else None),
            graded_by=None,
            bibliographic_reference=bibliographic_reference,
        )

    def _result_for_occurrences(
        self,
        citation: VerificationInput,
        status: MatchStatus,
        occurrences: tuple[_Occurrence, ...],
        *,
        cited_reference: str | None,
        differences: tuple[WordingDifference, ...],
        explanation: str,
        truncated: bool = False,
        cited_record: Mapping[str, Any] | None = None,
    ) -> VerificationResult:
        evidence_occurrences = occurrences
        if cited_record is not None:
            evidence = (self._evidence(cited_record, None),)
            references = (f"hadeethenc:{cited_record['id']}",)
        else:
            evidence = tuple(
                self._evidence(occurrence.record, occurrence.matched_fragment)
                for occurrence in evidence_occurrences
            )
            references = tuple(dict.fromkeys(item.reference for item in evidence))
        return VerificationResult(
            status=status,
            source_type="hadith",
            language=self._language,
            source_metadata=self._source,
            submitted_quote=citation.quote,
            cited_reference=cited_reference,
            matched_references=references,
            evidence=evidence,
            wording_differences=differences,
            explanation=explanation,
            candidate_count=None if truncated else len(evidence_occurrences),
            evidence_truncated=truncated,
        )

    def _not_found_result(
        self,
        citation: VerificationInput,
        *,
        record_id: str | None = None,
        search_truncated: bool = False,
    ) -> VerificationResult:
        cited_reference = f"hadeethenc:{record_id}" if record_id is not None else None
        if record_id is not None:
            explanation = (
                "The cited HadeethEnc record ID was not located. This source is a curated "
                "selection; no conclusion about the report's authenticity is made."
            )
            if search_truncated:
                explanation += (
                    " The separate quote search was truncated, so its other candidate "
                    "locations are incomplete."
                )
        elif search_truncated:
            explanation = (
                "The wording was not located among the candidate records returned by a "
                "truncated HadeethEnc search. The checked selection is curated and "
                "non-comprehensive; this does not establish that the report is fabricated "
                "or mawḍūʿ."
            )
        else:
            explanation = (
                "The wording was not located in the checked HadeethEnc selection. "
                "HadeethEnc is curated and non-comprehensive; this does not establish "
                "that the report is fabricated or mawḍūʿ."
            )
        return VerificationResult(
            status=MatchStatus.NOT_FOUND_IN_CHECKED_CORPUS,
            source_type="hadith",
            language=self._language,
            source_metadata=self._source,
            submitted_quote=citation.quote,
            cited_reference=cited_reference,
            matched_references=(),
            evidence=(),
            wording_differences=(),
            explanation=explanation,
            candidate_count=None if search_truncated else 0,
            evidence_truncated=search_truncated,
        )

    @staticmethod
    def _wording_differences(
        submitted_text: str,
        source_text: str,
    ) -> tuple[WordingDifference, ...]:
        submitted_words = submitted_text.split()
        source_words = source_text.split()
        matcher = difflib.SequenceMatcher(a=submitted_words, b=source_words, autojunk=False)
        differences: list[WordingDifference] = []
        for operation, start_a, end_a, start_b, end_b in matcher.get_opcodes():
            if operation == "equal":
                continue
            submitted = " ".join(submitted_words[start_a:end_a])
            source = " ".join(source_words[start_b:end_b])
            if operation == "insert":
                kind = "missing_from_submission"
            elif operation == "delete":
                kind = "extra_in_submission"
            else:
                kind = "wording_changed"
            differences.append(WordingDifference(kind, submitted, source))
            if len(differences) >= 50:
                break
        return tuple(differences)
