"""Verify Qur'an quotations against pinned source editions without rewriting them."""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from dataclasses import dataclass

from isnad_core.models import (
    Evidence,
    MatchStatus,
    SourceMetadata,
    VerificationInput,
    VerificationResult,
    WordingDifference,
)
from isnad_core.normalization import NormalizedText, normalize_with_spans
from isnad_core.quran.corpus import (
    InvalidQuranReference,
    QuranCorpus,
    QuranReference,
    QuranVerse,
)

MAX_QUOTE_CHARACTERS = 4_000
MAX_REFERENCE_AYAHS = 20
MAX_MATCH_CANDIDATES = 10
MAX_WORDING_DIFFERENCES = 50


@dataclass(frozen=True, slots=True)
class _TextIndex:
    """Normalized text mapped back to source ayahs and source-string positions."""

    text: str
    references: tuple[str | None, ...]
    source_spans: tuple[tuple[int, int] | None, ...]


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One contiguous source occurrence of a normalized quote."""

    references: tuple[str, ...]
    source_texts: tuple[str, ...]
    matched_fragments: tuple[str, ...]
    is_full_match: bool
    is_raw_exact: bool

    @property
    def matched_fragment(self) -> str:
        """Join the source-backed portions for display beside per-ayah evidence."""

        return " ".join(self.matched_fragments)

    @property
    def canonical_text(self) -> str:
        """Return the unaltered canonical text of all matched ayahs."""

        return " ".join(self.source_texts)


@dataclass(frozen=True, slots=True)
class _CandidateSearch:
    """A bounded set of matches and whether more matches were omitted."""

    candidates: tuple[_Candidate, ...]
    truncated: bool


class QuranTextVerifier:
    """Check text against a pinned Qur'an edition without making religious rulings.

    Matching is deterministic. A successful result establishes correspondence to
    the configured source edition only; it does not establish interpretation,
    religious authority, or correctness of surrounding claims.
    """

    def __init__(
        self,
        corpus: QuranCorpus,
        *,
        language: str,
        edition_label: str,
        strip_quranenc_footnote_markers: bool = False,
    ) -> None:
        if language not in {"ar", "en"}:
            raise ValueError("Qur'an text verifier language must be Arabic or English.")
        self._corpus = corpus
        self._language = language
        self._edition_label = edition_label
        self._strip_quranenc_footnote_markers = strip_quranenc_footnote_markers
        self._indexes = {
            surah: self._build_index(verses)
            for surah, verses in self._corpus.verses_by_surah.items()
        }

    @property
    def corpus(self) -> QuranCorpus:
        """Expose the immutable corpus for source wiring and diagnostics."""

        return self._corpus

    @property
    def source_type(self) -> str:
        """Return the public source type understood by the verification engine."""

        return "quran"

    @property
    def language(self) -> str:
        """Return the comparison language configured for this edition."""

        return self._language

    @property
    def source_metadata(self) -> SourceMetadata:
        """Return the exact source provenance represented by this verifier."""

        return self._corpus.source

    @property
    def normalization_profile(self) -> str:
        """Return the stable comparison profile identifier."""

        if self._language == "ar":
            return "arabic_compare_v1"
        if self._strip_quranenc_footnote_markers:
            return "english_quranenc_compare_v1"
        return "english_compare_v1"

    def _normalize(self, text: str) -> NormalizedText:
        return normalize_with_spans(
            text,
            language=self._language,
            strip_quranenc_footnote_markers=self._strip_quranenc_footnote_markers,
        )

    @property
    def mode(self) -> str:
        """Return the implementation mode advertised to API clients."""

        return "deterministic_local_corpus"

    @property
    def reference_format(self) -> str:
        """Describe canonical Surah:Ayah references accepted by this verifier."""

        return "surah:ayah[-end]"

    def verify(self, citation: VerificationInput) -> VerificationResult:
        """Return an auditable match result for one Qur'an citation."""

        source_type = citation.source.strip().casefold()
        language = citation.language.strip().casefold()
        if source_type != "quran" or language != self._language:
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
                    f"This verifier checks {self._language} Qur'an quotations against "
                    f"the pinned {self._edition_label} edition only."
                ),
            )

        quote = citation.quote.strip() if citation.quote is not None else ""
        if len(quote) > MAX_QUOTE_CHARACTERS:
            raise ValueError(f"Quote exceeds the {MAX_QUOTE_CHARACTERS}-character limit.")

        reference = QuranReference.parse(citation.reference) if citation.reference else None
        referenced_verses: tuple[QuranVerse, ...] = ()
        if reference is not None:
            if reference.end_ayah - reference.start_ayah + 1 > MAX_REFERENCE_AYAHS:
                raise InvalidQuranReference(
                    f"A reference range may contain at most {MAX_REFERENCE_AYAHS} ayahs."
                )
            referenced_verses = self._corpus.resolve(reference)

        if not quote:
            if reference is None:
                raise ValueError("Provide a quote, a reference, or both.")
            return self._result_for_reference_without_quote(citation, reference, referenced_verses)

        normalized_quote = self._normalize(quote).value
        if not normalized_quote:
            raise ValueError("The quote contains no searchable wording.")

        if reference is not None:
            cited_index = self._build_index(referenced_verses)
            cited_search = self._find_candidates(cited_index, normalized_quote, raw_quote=quote)
            if len(cited_search.candidates) == 1 and not cited_search.truncated:
                candidate = cited_search.candidates[0]
                candidate_covers_cited_range = candidate.references == tuple(
                    verse.reference for verse in referenced_verses
                )
                if candidate.is_full_match and candidate_covers_cited_range:
                    status = (
                        MatchStatus.EXACT_MATCH
                        if candidate.is_raw_exact
                        else MatchStatus.NORMALIZED_MATCH
                    )
                    return self._result_for_candidate(
                        citation,
                        status,
                        candidate,
                        reference.display,
                        self._matched_text_differences(quote, candidate.matched_fragment)
                        if status is not MatchStatus.EXACT_MATCH
                        else (),
                        f"The quote matches the cited {self._edition_label} text. "
                        "This is a textual comparison only.",
                    )
                return self._result_for_candidate(
                    citation,
                    MatchStatus.PARTIAL_MATCH,
                    candidate,
                    reference.display,
                    self._matched_text_differences(quote, candidate.matched_fragment),
                    f"The quote is a contiguous fragment of the cited {self._edition_label} text. "
                    "The full source ayah text is included as evidence.",
                )
            if cited_search.candidates or cited_search.truncated:
                return self._result_for_candidates(
                    citation,
                    MatchStatus.AMBIGUOUS_MULTIPLE_MATCHES,
                    cited_search,
                    reference.display,
                    "The wording occurs more than once within the cited range; the exact "
                    "occurrence is ambiguous.",
                )

        global_search = self._find_candidates_across_corpus(normalized_quote, raw_quote=quote)
        if global_search.candidates or global_search.truncated:
            if len(global_search.candidates) == 1 and not global_search.truncated:
                candidate = global_search.candidates[0]
                if reference is not None:
                    return self._result_for_candidate(
                        citation,
                        MatchStatus.QUOTE_FOUND_WRONG_REFERENCE,
                        candidate,
                        reference.display,
                        self._matched_text_differences(quote, candidate.matched_fragment),
                        "The quote was located in the pinned corpus, but not at the cited "
                        "reference.",
                    )
                if candidate.is_full_match:
                    status = (
                        MatchStatus.EXACT_MATCH
                        if candidate.is_raw_exact
                        else MatchStatus.NORMALIZED_MATCH
                    )
                    explanation = (
                        f"The quote matches a complete ayah or range in the pinned "
                        f"{self._edition_label} corpus. This is a textual comparison only."
                    )
                else:
                    status = MatchStatus.PARTIAL_MATCH
                    explanation = (
                        f"The quote is a contiguous fragment found in the pinned "
                        f"{self._edition_label} corpus. The full source ayah text "
                        "is included as evidence."
                    )
                return self._result_for_candidate(
                    citation,
                    status,
                    candidate,
                    None,
                    self._matched_text_differences(quote, candidate.matched_fragment)
                    if status is not MatchStatus.EXACT_MATCH
                    else (),
                    explanation,
                )
            return self._result_for_candidates(
                citation,
                MatchStatus.AMBIGUOUS_MULTIPLE_MATCHES,
                global_search,
                reference.display if reference is not None else None,
                "The wording has multiple locations in the checked corpus; a unique "
                "reference cannot be established from this quote alone.",
            )

        if reference is not None:
            cited_text = " ".join(verse.text for verse in referenced_verses)
            evidence = self._evidence_for_verses(referenced_verses, matched_fragments=None)
            return VerificationResult(
                status=MatchStatus.MISMATCH_AT_CITED_REFERENCE,
                source_type="quran",
                language=self._language,
                source_metadata=self._corpus.source,
                submitted_quote=citation.quote,
                cited_reference=reference.display,
                matched_references=tuple(verse.reference for verse in referenced_verses),
                evidence=evidence,
                wording_differences=self._matched_text_differences(quote, cited_text),
                explanation=(
                    "The reference exists in the pinned corpus, but the quote does not "
                    "match that text and was not located elsewhere in the checked corpus."
                ),
            )

        return VerificationResult(
            status=MatchStatus.NOT_FOUND_IN_CHECKED_CORPUS,
            source_type="quran",
            language=self._language,
            source_metadata=self._corpus.source,
            submitted_quote=citation.quote,
            cited_reference=None,
            matched_references=(),
            evidence=(),
            wording_differences=(),
            explanation=(
                f"The quote was not located in the checked {self._edition_label} corpus. "
                "This result is limited to that source and does not establish that the "
                "wording is fabricated or absent from every source."
            ),
        )

    def _result_for_reference_without_quote(
        self,
        citation: VerificationInput,
        reference: QuranReference,
        verses: tuple[QuranVerse, ...],
    ) -> VerificationResult:
        return VerificationResult(
            status=MatchStatus.REFERENCE_FOUND_WITHOUT_QUOTE,
            source_type="quran",
            language=self._language,
            source_metadata=self._corpus.source,
            submitted_quote=None,
            cited_reference=reference.display,
            matched_references=tuple(verse.reference for verse in verses),
            evidence=self._evidence_for_verses(verses, matched_fragments=None),
            wording_differences=(),
            explanation=(
                f"The reference exists in the pinned {self._edition_label} corpus, but no quote "
                "was supplied for comparison."
            ),
        )

    def _result_for_candidate(
        self,
        citation: VerificationInput,
        status: MatchStatus,
        candidate: _Candidate,
        cited_reference: str | None,
        differences: tuple[WordingDifference, ...],
        explanation: str,
    ) -> VerificationResult:
        return VerificationResult(
            status=status,
            source_type="quran",
            language=self._language,
            source_metadata=self._corpus.source,
            submitted_quote=citation.quote,
            cited_reference=cited_reference,
            matched_references=candidate.references,
            evidence=self._evidence_for_candidate(candidate),
            wording_differences=differences,
            explanation=explanation,
            candidate_count=1,
        )

    def _result_for_candidates(
        self,
        citation: VerificationInput,
        status: MatchStatus,
        search: _CandidateSearch,
        cited_reference: str | None,
        explanation: str,
    ) -> VerificationResult:
        candidates = search.candidates
        references = tuple(
            dict.fromkeys(
                reference for candidate in candidates for reference in candidate.references
            )
        )
        evidence = tuple(
            evidence
            for candidate in candidates
            for evidence in self._evidence_for_candidate(candidate)
        )
        return VerificationResult(
            status=status,
            source_type="quran",
            language=self._language,
            source_metadata=self._corpus.source,
            submitted_quote=citation.quote,
            cited_reference=cited_reference,
            matched_references=references,
            evidence=evidence,
            wording_differences=(),
            explanation=explanation,
            candidate_count=None if search.truncated else len(candidates),
            evidence_truncated=search.truncated,
        )

    def _evidence_for_candidate(self, candidate: _Candidate) -> tuple[Evidence, ...]:
        evidence: list[Evidence] = []
        for reference, source_text, fragment in zip(
            candidate.references,
            candidate.source_texts,
            candidate.matched_fragments,
            strict=True,
        ):
            evidence.append(
                Evidence(
                    reference=reference,
                    source_text=source_text,
                    matched_fragment=fragment,
                    source_id=self._corpus.source.source_id,
                    source_version=self._corpus.source.version,
                    source_url=self._corpus.source.url,
                    footnotes=self._corpus.verses_by_reference[reference].footnotes,
                )
            )
        return tuple(evidence)

    def _evidence_for_verses(
        self,
        verses: Sequence[QuranVerse],
        matched_fragments: Sequence[str] | None,
    ) -> tuple[Evidence, ...]:
        fragments = matched_fragments or [None] * len(verses)
        return tuple(
            Evidence(
                reference=verse.reference,
                source_text=verse.text,
                matched_fragment=fragment,
                source_id=self._corpus.source.source_id,
                source_version=self._corpus.source.version,
                source_url=self._corpus.source.url,
                footnotes=verse.footnotes,
            )
            for verse, fragment in zip(verses, fragments, strict=True)
        )

    def _build_index(self, verses: Sequence[QuranVerse]) -> _TextIndex:
        text_parts: list[str] = []
        references: list[str | None] = []
        source_spans: list[tuple[int, int] | None] = []
        for verse in verses:
            if text_parts:
                text_parts.append(" ")
                references.append(None)
                source_spans.append(None)
            normalized: NormalizedText = self._normalize(verse.text)
            text_parts.append(normalized.value)
            references.extend([verse.reference] * len(normalized.value))
            source_spans.extend(normalized.source_spans)
        text = "".join(text_parts)
        if len(text) != len(references) or len(text) != len(source_spans):
            raise RuntimeError("Internal Qur'an index mapping is inconsistent.")
        return _TextIndex(text, tuple(references), tuple(source_spans))

    def _find_candidates_across_corpus(
        self,
        query: str,
        *,
        raw_quote: str,
    ) -> _CandidateSearch:
        found: list[_Candidate] = []
        for surah in range(1, 115):
            index = self._indexes[surah]
            search = self._find_candidates(
                index,
                query,
                raw_quote=raw_quote,
                remaining=MAX_MATCH_CANDIDATES - len(found),
            )
            found.extend(search.candidates)
            if search.truncated:
                return _CandidateSearch(tuple(found), True)
        return _CandidateSearch(tuple(found), False)

    def _find_candidates(
        self,
        index: _TextIndex,
        query: str,
        *,
        raw_quote: str,
        remaining: int = MAX_MATCH_CANDIDATES,
    ) -> _CandidateSearch:
        if not query:
            return _CandidateSearch((), False)
        candidates: list[_Candidate] = []
        start_at = 0
        while True:
            found_at = index.text.find(query, start_at)
            if found_at < 0:
                return _CandidateSearch(tuple(candidates), False)
            end_at = found_at + len(query)
            start_at = found_at + 1
            if not self._has_word_boundaries(index.text, found_at, end_at):
                continue
            candidate = self._candidate_for_span(index, found_at, end_at, query, raw_quote)
            if candidate is None:
                continue
            if len(candidates) >= remaining:
                return _CandidateSearch(tuple(candidates), True)
            candidates.append(candidate)

    @staticmethod
    def _has_word_boundaries(text: str, start: int, end: int) -> bool:
        before_is_word = start > 0 and text[start - 1].isalnum()
        after_is_word = end < len(text) and text[end].isalnum()
        return not before_is_word and not after_is_word

    def _candidate_for_span(
        self,
        index: _TextIndex,
        start: int,
        end: int,
        query: str,
        raw_quote: str,
    ) -> _Candidate | None:
        reference_spans: dict[str, tuple[int, int]] = {}
        for position in range(start, end):
            reference = index.references[position]
            source_span = index.source_spans[position]
            if reference is None or source_span is None or index.text[position].isspace():
                continue
            previous = reference_spans.get(reference)
            if previous is None:
                reference_spans[reference] = source_span
            else:
                reference_spans[reference] = (previous[0], max(previous[1], source_span[1]))

        if not reference_spans:
            return None
        matched_references = tuple(reference_spans)
        source_verses = tuple(
            self._corpus.verses_by_reference[reference] for reference in matched_references
        )
        source_texts = tuple(verse.text for verse in source_verses)
        matched_fragments = tuple(
            verse.text[source_span[0] : source_span[1]]
            for verse, source_span in zip(source_verses, reference_spans.values(), strict=True)
        )
        canonical_text = " ".join(source_texts)
        is_full_match = query == self._normalize(canonical_text).value
        is_raw_exact = is_full_match and raw_quote == canonical_text
        return _Candidate(
            references=matched_references,
            source_texts=source_texts,
            matched_fragments=matched_fragments,
            is_full_match=is_full_match,
            is_raw_exact=is_raw_exact,
        )

    @staticmethod
    def _matched_text_differences(
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
            if len(differences) >= MAX_WORDING_DIFFERENCES:
                break
        return tuple(differences)


class QuranArabicVerifier(QuranTextVerifier):
    """Verify Arabic Qur'an text against the pinned Tanzil Uthmani edition."""

    def __init__(self, corpus: QuranCorpus | None = None) -> None:
        super().__init__(
            corpus if corpus is not None else QuranCorpus.load_default(),
            language="ar",
            edition_label="Tanzil Uthmani",
        )


class QuranEnglishVerifier(QuranTextVerifier):
    """Verify English Qur'an translations against the pinned QuranEnc edition."""

    def __init__(self, corpus: QuranCorpus | None = None) -> None:
        if corpus is None:
            from isnad_core.quran.quranenc import load_quranenc_english

            corpus = load_quranenc_english()
        super().__init__(
            corpus,
            language="en",
            edition_label="QuranEnc English Saheeh translation",
            strip_quranenc_footnote_markers=True,
        )
