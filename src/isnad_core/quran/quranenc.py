"""Load the pinned QuranEnc English translation and its unmodified API payloads."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from importlib.resources import files
from typing import Any

from isnad_core.models import SourceMetadata
from isnad_core.quran.corpus import QuranCorpus, QuranCorpusError, QuranVerse

_PINNED_MANIFEST_SHA256 = "ee82a10b91254d5dd0e1ad91e765d1be6f65b6c16bad6bdd7b896e8232e1d19a"
_PINNED_AGGREGATE_SHA256 = "e454966d9e8ab5dadff8665d934fc785ea4ec00b13e587134e8a402255f8aa78"
_TRANSLATION_KEY = "english_saheeh"
_TRANSLATION_VERSION = "1.1.2"
_EXPECTED_SURAHS = 114
_EXPECTED_AYAHS = 6_236
_MAX_SURAH_PAYLOAD_BYTES = 1_000_000


def _aggregate_sha256(payloads: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in sorted(payloads):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payloads[name])
        digest.update(b"\0")
    return digest.hexdigest()


def _read_manifest() -> dict[str, Any]:
    manifest_path = files("isnad_core").joinpath(
        "data", "quranenc", _TRANSLATION_KEY, "manifest.json"
    )
    try:
        raw_manifest = manifest_path.read_bytes()
    except OSError as exc:
        raise QuranCorpusError(
            "The pinned QuranEnc translation manifest could not be read."
        ) from exc
    if hashlib.sha256(raw_manifest).hexdigest() != _PINNED_MANIFEST_SHA256:
        raise QuranCorpusError(
            "The QuranEnc translation manifest does not match its build-time pin."
        )
    try:
        manifest = json.loads(raw_manifest)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QuranCorpusError("The pinned QuranEnc translation manifest is invalid JSON.") from exc
    if not isinstance(manifest, dict):
        raise QuranCorpusError("The pinned QuranEnc translation manifest must be an object.")
    if (
        manifest.get("translation_key") != _TRANSLATION_KEY
        or manifest.get("version") != _TRANSLATION_VERSION
        or manifest.get("surah_count") != _EXPECTED_SURAHS
        or manifest.get("ayah_count") != _EXPECTED_AYAHS
    ):
        raise QuranCorpusError("The QuranEnc edition metadata does not match the build-time pin.")
    return manifest


def load_quranenc_english() -> QuranCorpus:
    """Return a checksum-verified English QuranEnc edition as a reference corpus."""

    manifest = _read_manifest()
    files_manifest = manifest.get("files")
    if not isinstance(files_manifest, dict):
        raise QuranCorpusError("The QuranEnc manifest has no per-surah checksums.")
    expected_names = {f"surah_{surah:03d}.json" for surah in range(1, _EXPECTED_SURAHS + 1)}
    if set(files_manifest) != expected_names:
        raise QuranCorpusError("The QuranEnc manifest does not pin all 114 surah payloads.")

    package_root = files("isnad_core").joinpath("data", "quranenc", _TRANSLATION_KEY)
    raw_payloads: dict[str, bytes] = {}
    for filename in sorted(expected_names):
        expected_hash = files_manifest[filename]
        if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise QuranCorpusError(f"The QuranEnc manifest has an invalid checksum for {filename}.")
        try:
            raw = package_root.joinpath(filename).read_bytes()
        except OSError as exc:
            raise QuranCorpusError(
                f"The pinned QuranEnc payload {filename} could not be read."
            ) from exc
        if len(raw) > _MAX_SURAH_PAYLOAD_BYTES:
            raise QuranCorpusError(
                f"The pinned QuranEnc payload {filename} exceeds its size limit."
            )
        if hashlib.sha256(raw).hexdigest() != expected_hash:
            raise QuranCorpusError(f"The QuranEnc payload checksum does not match for {filename}.")
        raw_payloads[filename] = raw

    aggregate_hash = _aggregate_sha256(raw_payloads)
    if aggregate_hash != _PINNED_AGGREGATE_SHA256 or aggregate_hash != manifest.get(
        "aggregate_sha256"
    ):
        raise QuranCorpusError("The QuranEnc aggregate checksum does not match its build-time pin.")

    arabic_corpus = QuranCorpus.load_default()
    verses: list[QuranVerse] = []
    expected_id = 1
    for surah in range(1, _EXPECTED_SURAHS + 1):
        filename = f"surah_{surah:03d}.json"
        try:
            payload = json.loads(raw_payloads[filename])
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise QuranCorpusError(f"The QuranEnc payload {filename} is invalid JSON.") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("result"), list):
            raise QuranCorpusError(
                f"The QuranEnc payload {filename} has an invalid response shape."
            )
        rows = payload["result"]
        arabic_surah = arabic_corpus.verses_by_surah[surah]
        if len(rows) != len(arabic_surah):
            raise QuranCorpusError(f"The QuranEnc payload {filename} has an invalid ayah count.")
        for ayah, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                raise QuranCorpusError(f"The QuranEnc payload {filename} has a malformed ayah.")
            try:
                row_id = int(row["id"])
                row_surah = int(row["sura"])
                row_ayah = int(row["aya"])
                translation = row["translation"]
                footnotes = row["footnotes"]
            except (KeyError, TypeError, ValueError) as exc:
                raise QuranCorpusError(
                    f"The QuranEnc payload {filename} has a malformed ayah."
                ) from exc
            if (row_id, row_surah, row_ayah) != (expected_id, surah, ayah):
                raise QuranCorpusError(
                    f"The QuranEnc payload {filename} has non-contiguous locators."
                )
            if not isinstance(translation, str) or not translation:
                raise QuranCorpusError(
                    f"The QuranEnc payload {filename} has empty translation text."
                )
            if not isinstance(footnotes, str):
                raise QuranCorpusError(f"The QuranEnc payload {filename} has malformed footnotes.")
            arabic_verse = arabic_surah[ayah - 1]
            verses.append(
                QuranVerse(
                    surah=surah,
                    ayah=ayah,
                    surah_name=arabic_verse.surah_name,
                    text=translation,
                    footnotes=footnotes,
                )
            )
            expected_id += 1

    source = SourceMetadata(
        source_id=str(manifest["source_id"]),
        name=str(manifest["title"]),
        url=str(manifest["source_url"]),
        version=str(manifest["version"]),
        license="QuranEnc reuse terms: preserve the content, source, publisher, and version.",
        content_sha256=aggregate_hash,
    )
    return QuranCorpus.from_verses(verses, source)
