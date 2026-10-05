"""Arabic Qur'an verification must return source evidence and explicit match states."""

import pytest

from isnad_core.models import MatchStatus, VerificationInput
from isnad_core.quran import InvalidQuranReference, QuranArabicVerifier, QuranCorpus


@pytest.fixture(scope="module")
def verifier() -> QuranArabicVerifier:
    return QuranArabicVerifier(QuranCorpus.load_default())


def test_exact_match_returns_verbatim_source_evidence(verifier: QuranArabicVerifier) -> None:
    verse = verifier.corpus.verses_by_reference["1:1"]
    result = verifier.verify(VerificationInput("quran", "ar", verse.text, "1:1"))

    assert result.status is MatchStatus.EXACT_MATCH
    assert result.matched_references == ("1:1",)
    assert result.evidence[0].source_text == verse.text
    assert result.evidence[0].matched_fragment == verse.text
    assert result.source_metadata is not None
    assert result.source_metadata.version == "1.1"


def test_submitted_quote_is_preserved_verbatim(verifier: QuranArabicVerifier) -> None:
    source_quote = verifier.corpus.verses_by_reference["1:1"].text
    submitted_quote = f"  {source_quote}\n"
    result = verifier.verify(VerificationInput("quran", "ar", submitted_quote, "1:1"))

    assert result.submitted_quote == submitted_quote
    assert result.evidence[0].source_text == source_quote


def test_full_ayah_without_diacritics_is_normalized_match(
    verifier: QuranArabicVerifier,
) -> None:
    result = verifier.verify(VerificationInput("quran", "ar", "بسم الله الرحمن الرحيم", "1:1"))

    assert result.status is MatchStatus.NORMALIZED_MATCH
    assert result.evidence[0].source_text == verifier.corpus.verses_by_reference["1:1"].text
    assert result.wording_differences


def test_partial_phrase_returns_canonical_diacritized_text(verifier: QuranArabicVerifier) -> None:
    result = verifier.verify(VerificationInput("quran", "ar", "بسم الله", "1:1"))

    assert result.status is MatchStatus.PARTIAL_MATCH
    assert result.evidence[0].source_text == verifier.corpus.verses_by_reference["1:1"].text
    assert result.evidence[0].matched_fragment == "بِسْمِ ٱللَّهِ"


def test_unique_quote_at_another_reference_is_not_silently_reassigned(
    verifier: QuranArabicVerifier,
) -> None:
    quote = verifier.corpus.verses_by_reference["112:1"].text
    result = verifier.verify(VerificationInput("quran", "ar", quote, "1:1"))

    assert result.status is MatchStatus.QUOTE_FOUND_WRONG_REFERENCE
    assert result.cited_reference == "1:1"
    assert result.matched_references == ("112:1",)
    assert result.evidence[0].source_text == quote


def test_mismatch_at_valid_reference_includes_comparison_evidence(
    verifier: QuranArabicVerifier,
) -> None:
    result = verifier.verify(VerificationInput("quran", "ar", "الله الكريم الرحيم", "1:1"))

    assert result.status is MatchStatus.MISMATCH_AT_CITED_REFERENCE
    assert result.evidence[0].reference == "1:1"
    assert result.wording_differences


def test_reference_without_quote_is_distinct(verifier: QuranArabicVerifier) -> None:
    result = verifier.verify(VerificationInput("quran", "ar", None, "1:1"))

    assert result.status is MatchStatus.REFERENCE_FOUND_WITHOUT_QUOTE
    assert result.evidence[0].source_text == verifier.corpus.verses_by_reference["1:1"].text


def test_unlocated_text_is_limited_to_checked_source(verifier: QuranArabicVerifier) -> None:
    result = verifier.verify(
        VerificationInput("quran", "ar", "زوزوووو ابلبلبل غير موجود إطلاقا", None)
    )

    assert result.status is MatchStatus.NOT_FOUND_IN_CHECKED_CORPUS
    assert result.evidence == ()
    assert "does not establish" in result.explanation


def test_repeated_phrase_without_reference_is_ambiguous(verifier: QuranArabicVerifier) -> None:
    result = verifier.verify(VerificationInput("quran", "ar", "الرحمن الرحيم", None))

    assert result.status is MatchStatus.AMBIGUOUS_MULTIPLE_MATCHES
    assert "1:1" in result.matched_references
    assert "1:3" in result.matched_references
    assert result.candidate_count is None or result.candidate_count > 1


def test_common_word_matches_are_bounded_and_marked_truncated(
    verifier: QuranArabicVerifier,
) -> None:
    result = verifier.verify(VerificationInput("quran", "ar", "الله", None))

    assert result.status is MatchStatus.AMBIGUOUS_MULTIPLE_MATCHES
    assert result.evidence_truncated is True
    assert result.candidate_count is None
    assert len(result.evidence) <= 10


def test_repeated_phrase_inside_cited_range_is_ambiguous(verifier: QuranArabicVerifier) -> None:
    result = verifier.verify(VerificationInput("quran", "ar", "الرحمن الرحيم", "1:1-3"))

    assert result.status is MatchStatus.AMBIGUOUS_MULTIPLE_MATCHES
    assert result.cited_reference == "1:1-3"


def test_range_quote_subset_is_partial_with_ayah_evidence(verifier: QuranArabicVerifier) -> None:
    second_ayah = verifier.corpus.verses_by_reference["2:2"].text
    result = verifier.verify(VerificationInput("quran", "ar", second_ayah, "2:1-3"))

    assert result.status is MatchStatus.PARTIAL_MATCH
    assert result.matched_references == ("2:2",)
    assert result.evidence[0].source_text == second_ayah


def test_unsupported_source_or_language_is_not_reported_as_no_match(
    verifier: QuranArabicVerifier,
) -> None:
    result = verifier.verify(VerificationInput("hadith", "en", "sample", None))

    assert result.status is MatchStatus.UNSUPPORTED_SOURCE_OR_LANGUAGE
    assert result.source_metadata is None
    assert result.evidence == ()


def test_bad_reference_is_a_boundary_error(verifier: QuranArabicVerifier) -> None:
    with pytest.raises(InvalidQuranReference):
        verifier.verify(VerificationInput("quran", "ar", "بسم الله", "115:1"))


def test_oversized_reference_range_is_rejected(verifier: QuranArabicVerifier) -> None:
    with pytest.raises(InvalidQuranReference, match="at most"):
        verifier.verify(VerificationInput("quran", "ar", "بسم الله", "2:1-21"))


def test_oversized_quote_is_rejected(verifier: QuranArabicVerifier) -> None:
    with pytest.raises(ValueError, match="character limit"):
        verifier.verify(VerificationInput("quran", "ar", "ا" * 4_001, None))


def test_missing_quote_and_reference_is_rejected(verifier: QuranArabicVerifier) -> None:
    with pytest.raises(ValueError, match="Provide a quote"):
        verifier.verify(VerificationInput("quran", "ar", None, None))
