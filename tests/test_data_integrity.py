"""The packaged corpus must remain the exact, licensed upstream artifact."""

import hashlib
import json
import xml.etree.ElementTree as ET
from importlib.resources import files
from pathlib import Path

import pytest

import isnad_core.quran.corpus as corpus_module
from isnad_core.quran import QuranCorpus, QuranCorpusError


def test_pinned_tanzil_artifact_matches_manifest_and_expected_counts() -> None:
    package_root = files("isnad_core")
    manifest = json.loads(
        package_root.joinpath("data", "quran", "manifest.json").read_text(encoding="utf-8")
    )
    raw_xml = package_root.joinpath("data", "quran", "tanzil-uthmani-v1.1.xml").read_bytes()

    assert manifest["sha256"] == corpus_module._PINNED_TANZIL_SHA256
    assert hashlib.sha256(raw_xml).hexdigest() == corpus_module._PINNED_TANZIL_SHA256
    assert b"Tanzil Quran Text (Uthmani, Version 1.1)" in raw_xml[:2_000]
    assert b"Creative Commons Attribution 3.0" in raw_xml[:2_000]
    assert b"CHANGING IT IS NOT ALLOWED" in raw_xml[:2_000]
    xml_root = ET.fromstring(raw_xml)
    assert len(xml_root.findall("sura")) == 114
    assert len(xml_root.findall(".//aya")) == 6_236


def test_notice_records_all_upstream_attributions_and_reuse_constraints() -> None:
    notice = (Path(__file__).parents[1] / "NOTICE.md").read_text(encoding="utf-8")

    for required_text in (
        "Tanzil Project",
        "Creative Commons Attribution 3.0",
        "Noor International Center",
        "1.1.2",
        "do not modify, add to, or delete content",
        "HadeethEnc.com",
        "not comprehensive",
        "does not relicense third-party source text",
    ):
        assert required_text.casefold() in notice.casefold()


def test_loader_keeps_source_ayah_text_and_provenance() -> None:
    corpus = QuranCorpus.load_default()
    first_ayah = corpus.verses_by_reference["1:1"]

    assert first_ayah.text == "بِسْمِ ٱللَّهِ ٱلرَّحْمَـٰنِ ٱلرَّحِيمِ"
    assert corpus.source.source_id == "tanzil-quran-uthmani"
    assert corpus.source.version == "1.1"
    assert corpus.source.content_sha256 is not None
    assert len(corpus.verses) == 6_236


def test_loader_rejects_a_manifest_checksum_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    package_root = files("isnad_core")
    manifest = json.loads(
        package_root.joinpath("data", "quran", "manifest.json").read_text(encoding="utf-8")
    )
    manifest["sha256"] = "0" * 64
    raw_xml = package_root.joinpath("data", "quran", "tanzil-uthmani-v1.1.xml").read_bytes()

    class FakeResource:
        def __init__(self, content: bytes) -> None:
            self._content = content

        def read_text(self, *, encoding: str) -> str:
            return self._content.decode(encoding)

        def read_bytes(self) -> bytes:
            return self._content

    class FakePackageRoot:
        def joinpath(self, *parts: str) -> FakeResource:
            if parts[-1] == "manifest.json":
                content = json.dumps(manifest).encode("utf-8")
            else:
                content = raw_xml
            return FakeResource(content)

    monkeypatch.setattr(corpus_module, "files", lambda _package: FakePackageRoot())
    with pytest.raises(QuranCorpusError, match="build-time pin"):
        QuranCorpus.load_default()


def test_loader_rejects_modified_corpus_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    package_root = files("isnad_core")
    manifest = package_root.joinpath("data", "quran", "manifest.json").read_text(encoding="utf-8")
    raw_xml = package_root.joinpath("data", "quran", "tanzil-uthmani-v1.1.xml").read_bytes()
    modified_xml = raw_xml.replace(b"Tanzil", b"tanzil", 1)

    class FakeResource:
        def __init__(self, content: bytes) -> None:
            self._content = content

        def read_text(self, *, encoding: str) -> str:
            return self._content.decode(encoding)

        def read_bytes(self) -> bytes:
            return self._content

    class FakePackageRoot:
        def joinpath(self, *parts: str) -> FakeResource:
            content = manifest.encode() if parts[-1] == "manifest.json" else modified_xml
            return FakeResource(content)

    monkeypatch.setattr(corpus_module, "files", lambda _package: FakePackageRoot())
    with pytest.raises(QuranCorpusError, match="build-time pin"):
        QuranCorpus.load_default()


def test_reference_parser_supports_arabic_digits_and_ranges() -> None:
    from isnad_core.quran import QuranReference

    assert QuranReference.parse("٢:٢٥٥–٢٥٧").display == "2:255-257"


def test_reference_parser_rejects_zero_and_reversed_ranges() -> None:
    from isnad_core.quran import InvalidQuranReference, QuranReference

    with pytest.raises(InvalidQuranReference):
        QuranReference.parse("0:1")
    with pytest.raises(InvalidQuranReference):
        QuranReference.parse("2:0")
    with pytest.raises(InvalidQuranReference):
        QuranReference.parse("2:257-255")


def test_direct_resolution_rejects_invalid_dataclass_values() -> None:
    from isnad_core.quran import InvalidQuranReference, QuranReference

    corpus = QuranCorpus.load_default()
    for reference in (
        QuranReference(0, 1, 1),
        QuranReference(2, 0, 1),
        QuranReference(2, 2, 1),
    ):
        with pytest.raises(InvalidQuranReference):
            corpus.resolve(reference)
