"""The normalization profile must aid matching without replacing source text."""

from isnad_core.normalization import normalize_arabic, normalize_english, normalize_with_spans


def test_arabic_profile_removes_tashkeel_and_maps_alef_wasla() -> None:
    assert normalize_arabic("بِسْمِ ٱللَّهِ") == "بسم الله"


def test_arabic_profile_preserves_hamza_and_distinct_base_letters() -> None:
    assert normalize_arabic("أمن") != normalize_arabic("امن")
    assert normalize_arabic("رحمة") != normalize_arabic("رحمه")
    assert normalize_arabic("على") != normalize_arabic("علي")


def test_compatibility_ligature_and_punctuation_are_searchable() -> None:
    assert normalize_arabic("ﷲ، الرَّحْمَٰنُ") == "الله الرحمن"
    assert normalize_english("“The LORD—Most Merciful!”") == "the lord most merciful"


def test_english_profile_ignores_numeric_footnote_markers_but_maps_raw_offsets() -> None:
    source = "Allāh,[2] the Entirely Merciful"
    normalized = normalize_with_spans(
        source,
        language="en",
        strip_quranenc_footnote_markers=True,
    )

    assert normalized.value == "allāh the entirely merciful"
    assert "2" in normalize_english(source)
    raw_span = normalized.raw_span(0, len(normalized.value))
    assert raw_span == (0, len(source))
    assert source[raw_span[0] : raw_span[1]] == source


def test_arabic_key_maps_to_original_span_without_changing_input() -> None:
    source = "ٱللَّهُ"
    normalized = normalize_with_spans(source, language="ar")
    assert normalized.value == "الله"
    assert normalized.raw_span(0, len(normalized.value)) == (0, len(source))
    assert source == "ٱللَّهُ"
