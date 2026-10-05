"""QuranEnc payloads remain verbatim, version-pinned, and fully locatable."""

import hashlib
import json
from importlib.resources import files

import pytest

import isnad_core.quran.quranenc as quranenc_module
from isnad_core.quran import QuranCorpusError, load_quranenc_english


def test_quranenc_payload_set_matches_pinned_hashes_and_metadata() -> None:
    package_root = files("isnad_core").joinpath("data", "quranenc", "english_saheeh")
    manifest_raw = package_root.joinpath("manifest.json").read_bytes()
    manifest = json.loads(manifest_raw)
    assert hashlib.sha256(manifest_raw).hexdigest() == quranenc_module._PINNED_MANIFEST_SHA256
    assert manifest["aggregate_sha256"] == quranenc_module._PINNED_AGGREGATE_SHA256
    assert manifest["version"] == "1.1.2"
    assert manifest["publisher"] == "Noor International Center"
    assert manifest["translation_key"] == "english_saheeh"
    assert len(manifest["files"]) == 114

    payloads = {name: package_root.joinpath(name).read_bytes() for name in manifest["files"]}
    assert all(
        hashlib.sha256(payloads[name]).hexdigest() == manifest["files"][name]
        for name in manifest["files"]
    )
    assert quranenc_module._aggregate_sha256(payloads) == quranenc_module._PINNED_AGGREGATE_SHA256


def test_quranenc_loader_preserves_translation_and_footnote_text() -> None:
    corpus = load_quranenc_english()
    first_verse = corpus.verses_by_reference["1:1"]

    assert len(corpus.verses) == 6_236
    assert len(corpus.verses_by_surah) == 114
    assert first_verse.text == (
        "In the name of Allāh,[2] the Entirely Merciful, the Especially Merciful.[3]"
    )
    assert first_verse.footnotes is not None
    assert first_verse.footnotes.startswith("[2] Allāh is a proper name")
    assert corpus.source.source_id == "quranenc-english-saheeh"
    assert corpus.source.version == "1.1.2"
    assert corpus.source.content_sha256 == quranenc_module._PINNED_AGGREGATE_SHA256


def test_loader_rejects_a_modified_quranenc_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    package_root = files("isnad_core").joinpath("data", "quranenc", "english_saheeh")
    manifest = package_root.joinpath("manifest.json").read_bytes()
    original_payload = package_root.joinpath("surah_001.json").read_bytes()
    modified_payload = original_payload.replace(b"All", b"all", 1)

    class FakeResource:
        def __init__(self, content: bytes) -> None:
            self._content = content

        def read_bytes(self) -> bytes:
            return self._content

    class FakePackageRoot:
        def joinpath(self, *parts: str):
            name = parts[-1]
            if name == "manifest.json":
                return FakeResource(manifest)
            if len(parts) > 1:
                return self
            if name == "surah_001.json":
                return FakeResource(modified_payload)
            return FakeResource(package_root.joinpath(name).read_bytes())

    monkeypatch.setattr(quranenc_module, "files", lambda _package: FakePackageRoot())
    with pytest.raises(QuranCorpusError, match="checksum"):
        load_quranenc_english()
