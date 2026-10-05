"""The shared engine routes language/source pairs to the pinned adapters."""

import pytest

from isnad_core.engine import VerificationEngine
from isnad_core.models import MatchStatus, VerificationInput
from isnad_core.quran import QuranArabicVerifier


@pytest.fixture(scope="module")
def engine() -> VerificationEngine:
    instance = VerificationEngine()
    yield instance
    instance.close()


def test_engine_advertises_both_quran_editions_and_versions(engine: VerificationEngine) -> None:
    capabilities = {(item.source_type, item.language): item for item in engine.capabilities}
    assert set(capabilities) == {
        ("quran", "ar"),
        ("quran", "en"),
        ("hadith", "ar"),
        ("hadith", "en"),
    }
    assert capabilities[("quran", "ar")].source_metadata.version == "1.1"
    assert capabilities[("quran", "en")].source_metadata.version == "1.1.2"
    assert capabilities[("hadith", "en")].mode == "official_remote_api"
    assert "not a comprehensive" in capabilities[("hadith", "en")].source_metadata.coverage_note


def test_engine_routes_english_and_arabic_to_distinct_evidence(engine: VerificationEngine) -> None:
    arabic = engine.verify(VerificationInput("quran", "ar", "بسم الله الرحمن الرحيم", "1:1"))
    english = engine.verify(
        VerificationInput(
            "quran",
            "en",
            "In the name of Allāh,[2] the Entirely Merciful, the Especially Merciful.[3]",
            "1:1",
        )
    )

    assert arabic.status is MatchStatus.NORMALIZED_MATCH
    assert arabic.source_metadata is not None
    assert arabic.source_metadata.source_id == "tanzil-quran-uthmani"
    assert english.status is MatchStatus.EXACT_MATCH
    assert english.source_metadata is not None
    assert english.source_metadata.source_id == "quranenc-english-saheeh"
    assert english.evidence[0].footnotes


def test_engine_keeps_unconfigured_sources_explicitly_unsupported(
    engine: VerificationEngine,
) -> None:
    result = engine.verify(VerificationInput("tafsir", "en", "unlocated text", None))

    assert result.status is MatchStatus.UNSUPPORTED_SOURCE_OR_LANGUAGE
    assert result.source_metadata is None
    assert "not a finding" in result.explanation


def test_engine_rejects_duplicate_source_language_registration() -> None:
    adapter = QuranArabicVerifier()
    with pytest.raises(ValueError, match="Duplicate verifier adapter"):
        VerificationEngine((adapter, adapter))
