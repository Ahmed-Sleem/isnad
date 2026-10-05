"""Versioned comparison keys for Arabic and English without altering source text."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import TypeAlias

SourceSpan: TypeAlias = tuple[int, int]

_ARABIC_DIACRITIC_CATEGORIES = {"Mn", "Me"}
_QURANIC_ANNOTATION_RANGE = range(0x06D6, 0x06EE)
_ARABIC_DIGIT_RANGES = (range(0x0660, 0x066A), range(0x06F0, 0x06FA))
_ENGLISH_FOOTNOTE_MARKER = re.compile(r"\[[0-9]{1,4}\]")
_ALEF_WASLA = "\u0671"
_TATWEEL = "\u0640"


@dataclass(frozen=True, slots=True)
class NormalizedText:
    """A comparison key with offsets back to the unchanged input string."""

    value: str
    source_spans: tuple[SourceSpan, ...]

    def raw_span(self, start: int, end: int) -> SourceSpan | None:
        """Map a non-empty normalized range to a covering input-string range."""

        if start < 0 or end > len(self.value) or start >= end:
            return None
        spans = [
            span
            for char, span in zip(self.value[start:end], self.source_spans[start:end], strict=True)
            if not char.isspace()
        ]
        if not spans:
            return None
        return spans[0][0], spans[-1][1]


def _is_arabic_digit(char: str) -> bool:
    codepoint = ord(char)
    return any(codepoint in digit_range for digit_range in _ARABIC_DIGIT_RANGES)


def _is_quranic_annotation(char: str) -> bool:
    return ord(char) in _QURANIC_ANNOTATION_RANGE


def _cluster_end(text: str, start: int) -> int:
    """Include combining marks and Qur'anic annotations with the preceding base."""

    end = start + 1
    while end < len(text):
        char = text[end]
        if unicodedata.category(char) in _ARABIC_DIACRITIC_CATEGORIES or _is_quranic_annotation(
            char
        ):
            end += 1
        else:
            break
    return end


def _comparison_chars(cluster: str, *, arabic_profile: bool) -> list[str]:
    """Normalize one source cluster into comparison characters, never display text."""

    expanded = unicodedata.normalize("NFKC", cluster)
    output: list[str] = []
    for char in expanded:
        category = unicodedata.category(char)
        if category in _ARABIC_DIACRITIC_CATEGORIES or category == "Cf":
            continue
        if arabic_profile and (_is_quranic_annotation(char) or char == _TATWEEL):
            continue
        if arabic_profile and _is_arabic_digit(char):
            output.append(" ")
            continue
        if arabic_profile and char == _ALEF_WASLA:
            output.append("ا")
            continue
        if char.isspace() or category.startswith("P"):
            output.append(" ")
            continue
        output.extend(char.casefold())
    return output


def normalize_with_spans(
    text: str,
    *,
    language: str,
    strip_quranenc_footnote_markers: bool = False,
) -> NormalizedText:
    """Create a versioned normalization key and preserve offsets into `text`.

    Arabic comparison removes diacritics, Qur'anic annotation marks, tatweel,
    bidi controls and Arabic verse digits; it maps alef-wasla to ordinary alef.
    It intentionally preserves hamza-bearing letters, ta marbuta and alef maqsura.
    English comparison applies NFKC, case-folding and punctuation/space collapse.
    For the QuranEnc English profile only, bracketed numeric footnote markers may
    be ignored as editorial annotations. This never changes the returned source text.
    """

    if language not in {"ar", "en"}:
        raise ValueError(f"Unsupported normalization language: {language}")
    if strip_quranenc_footnote_markers and language != "en":
        raise ValueError("QuranEnc footnote markers can be stripped only in English text.")

    arabic_profile = language == "ar"
    normalized: list[str] = []
    spans: list[SourceSpan] = []
    cursor = 0

    while cursor < len(text):
        if strip_quranenc_footnote_markers and text[cursor] == "[":
            footnote_marker = _ENGLISH_FOOTNOTE_MARKER.match(text, cursor)
            if footnote_marker is not None:
                cursor = footnote_marker.end()
                continue

        cluster_start = cursor
        cluster_end = _cluster_end(text, cursor)
        cluster = text[cluster_start:cluster_end]
        output_chars = _comparison_chars(cluster, arabic_profile=arabic_profile)

        for char in output_chars:
            span = (cluster_start, cluster_end)
            if char == " ":
                if normalized and normalized[-1] == " ":
                    prior_start, _ = spans[-1]
                    spans[-1] = (prior_start, cluster_end)
                else:
                    normalized.append(char)
                    spans.append(span)
            else:
                normalized.append(char)
                spans.append(span)
        cursor = cluster_end

    start = 0
    while start < len(normalized) and normalized[start] == " ":
        start += 1
    end = len(normalized)
    while end > start and normalized[end - 1] == " ":
        end -= 1

    return NormalizedText("".join(normalized[start:end]), tuple(spans[start:end]))


def normalize_arabic(text: str) -> str:
    """Return the `arabic_compare_v1` key for matching; do not display this key."""

    return normalize_with_spans(text, language="ar").value


def normalize_english(text: str) -> str:
    """Return the `english_compare_v1` key for matching; do not display this key."""

    return normalize_with_spans(text, language="en").value
