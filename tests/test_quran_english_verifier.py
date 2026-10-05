"""English Qur'an verification uses the pinned QuranEnc translation verbatim."""

import pytest

from isnad_core.models import MatchStatus, VerificationInput
from isnad_core.quran import QuranEnglishVerifier


@pytest.fixture(scope="module")
def verifier() -> QuranEnglishVerifier:
    return QuranEnglishVerifier()


def test_exact_translation_match_includes_provenance_and_footnotes(
    verifier: QuranEnglishVerifier,
) -> None:
    verse = verifier.corpus.verses_by_reference["1:1"]
    result = verifier.verify(VerificationInput("quran", "en", verse.text, "1:1"))

    assert result.status is MatchStatus.EXACT_MATCH
    assert result.evidence[0].source_text == verse.text
    assert result.evidence[0].footnotes == verse.footnotes
    assert result.source_metadata is not None
    assert result.source_metadata.source_id == "quranenc-english-saheeh"
    assert result.source_metadata.version == "1.1.2"


def test_english_case_and_punctuation_normalization_is_not_raw_exact(
    verifier: QuranEnglishVerifier,
) -> None:
    result = verifier.verify(
        VerificationInput(
            "quran",
            "en",
            "in the name of allāh[2] the entirely merciful the especially merciful[3]",
            "1:1",
        )
    )

    assert result.status is MatchStatus.NORMALIZED_MATCH
    assert result.wording_differences
    assert result.evidence[0].source_text == verifier.corpus.verses_by_reference["1:1"].text


def test_full_translation_without_editorial_footnote_markers_is_normalized_match(
    verifier: QuranEnglishVerifier,
) -> None:
    quote = "In the name of Allāh, the Entirely Merciful, the Especially Merciful."
    result = verifier.verify(VerificationInput("quran", "en", quote, "1:1"))

    assert result.status is MatchStatus.NORMALIZED_MATCH
    assert result.evidence[0].source_text == verifier.corpus.verses_by_reference["1:1"].text
    assert result.wording_differences


def test_partial_english_phrase_keeps_full_edition_text_as_evidence(
    verifier: QuranEnglishVerifier,
) -> None:
    result = verifier.verify(VerificationInput("quran", "en", "You we ask for help", "1:5"))

    assert result.status is MatchStatus.PARTIAL_MATCH
    assert result.matched_references == ("1:5",)
    assert result.evidence[0].source_text == verifier.corpus.verses_by_reference["1:5"].text


def test_english_quote_at_wrong_reference_is_not_reassigned(
    verifier: QuranEnglishVerifier,
) -> None:
    quote = verifier.corpus.verses_by_reference["1:5"].text
    result = verifier.verify(VerificationInput("quran", "en", quote, "1:1"))

    assert result.status is MatchStatus.QUOTE_FOUND_WRONG_REFERENCE
    assert result.cited_reference == "1:1"
    assert result.matched_references == ("1:5",)


def test_repeated_english_phrase_is_reported_ambiguous(verifier: QuranEnglishVerifier) -> None:
    result = verifier.verify(VerificationInput("quran", "en", "the Entirely Merciful", None))

    assert result.status is MatchStatus.AMBIGUOUS_MULTIPLE_MATCHES
    assert "1:1" in result.matched_references
    assert "1:3" in result.matched_references


def test_english_not_found_does_not_claim_global_absence(verifier: QuranEnglishVerifier) -> None:
    result = verifier.verify(
        VerificationInput("quran", "en", "an entirely synthetic statement", None)
    )

    assert result.status is MatchStatus.NOT_FOUND_IN_CHECKED_CORPUS
    assert result.evidence == ()
    assert "does not establish" in result.explanation


def test_english_verifier_rejects_other_languages_as_unsupported(
    verifier: QuranEnglishVerifier,
) -> None:
    result = verifier.verify(VerificationInput("quran", "ar", "بسم الله", "1:1"))

    assert result.status is MatchStatus.UNSUPPORTED_SOURCE_OR_LANGUAGE
    assert result.source_metadata is None
