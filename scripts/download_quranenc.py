"""Fetch and validate the pinned QuranEnc English translation without editing API payloads."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import httpx

from isnad_core.quran import QuranCorpus

API_ROOT = "https://quranenc.com/api/v1"
TRANSLATION_KEY = "english_saheeh"
EXPECTED_VERSION = "1.1.2"
CONCURRENCY = 4
MAX_RESPONSE_BYTES = 1_000_000
USER_AGENT = "isnad-core-source-ingest/0.1 (https://github.com/)"
TARGET = Path(__file__).resolve().parents[1] / "src/isnad_core/data/quranenc/english_saheeh"


def _json_object(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"QuranEnc returned invalid JSON for {label}.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"QuranEnc returned an unexpected response for {label}.")
    return payload


def _validate_sura_payload(raw: bytes, *, surah: int, expected_ayahs: int) -> None:
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError(f"QuranEnc response for surah {surah} exceeds the size limit.")
    payload = _json_object(raw, label=f"surah {surah}")
    rows = payload.get("result")
    if not isinstance(rows, list) or len(rows) != expected_ayahs:
        raise RuntimeError(f"QuranEnc returned an unexpected ayah count for surah {surah}.")
    for expected_ayah, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise RuntimeError(f"QuranEnc returned a malformed ayah in surah {surah}.")
        try:
            returned_surah = int(row["sura"])
            returned_ayah = int(row["aya"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"QuranEnc returned a malformed reference in surah {surah}."
            ) from exc
        if (returned_surah, returned_ayah) != (surah, expected_ayah):
            raise RuntimeError(f"QuranEnc references are not contiguous in surah {surah}.")
        if not isinstance(row.get("translation"), str) or not isinstance(row.get("footnotes"), str):
            raise RuntimeError(f"QuranEnc omitted translation text in surah {surah}.")


def _aggregate_sha256(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[name])
        digest.update(b"\0")
    return digest.hexdigest()


async def _fetch_all() -> tuple[dict[str, Any], dict[str, bytes]]:
    timeout = httpx.Timeout(20.0, connect=10.0)
    limits = httpx.Limits(max_connections=CONCURRENCY, max_keepalive_connections=CONCURRENCY)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async with httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
        follow_redirects=True,
        headers=headers,
    ) as client:
        list_response = await client.get(f"{API_ROOT}/translations/list/en?localization=en")
        list_response.raise_for_status()
        list_payload = _json_object(list_response.content, label="translation list")
        translations = list_payload.get("translations")
        if not isinstance(translations, list):
            raise RuntimeError("QuranEnc translation list has an unexpected shape.")
        edition = next(
            (
                item
                for item in translations
                if isinstance(item, dict) and item.get("key") == TRANSLATION_KEY
            ),
            None,
        )
        if not isinstance(edition, dict):
            raise RuntimeError(f"QuranEnc no longer lists {TRANSLATION_KEY}.")
        if edition.get("version") != EXPECTED_VERSION:
            raise RuntimeError(
                f"Expected QuranEnc {TRANSLATION_KEY} {EXPECTED_VERSION}; "
                f"source currently reports {edition.get('version')!r}. Review and repin first."
            )

        async def fetch_sura(surah: int) -> tuple[str, bytes]:
            async with semaphore:
                response = await client.get(
                    f"{API_ROOT}/translation/sura/{TRANSLATION_KEY}/{surah}"
                )
                response.raise_for_status()
                raw = response.content
                filename = f"surah_{surah:03d}.json"
                return filename, raw

        raw_files = dict(await asyncio.gather(*(fetch_sura(surah) for surah in range(1, 115))))

    arabic_corpus = QuranCorpus.load_default()
    expected_counts = {
        surah: len(verses) for surah, verses in arabic_corpus.verses_by_surah.items()
    }
    for surah, expected_ayahs in expected_counts.items():
        _validate_sura_payload(
            raw_files[f"surah_{surah:03d}.json"],
            surah=surah,
            expected_ayahs=expected_ayahs,
        )
    if len(raw_files) != 114:
        raise RuntimeError("QuranEnc download did not return all 114 surahs.")
    return edition, raw_files


def _write_verified_snapshot(edition: dict[str, Any], raw_files: dict[str, bytes]) -> None:
    aggregate_hash = _aggregate_sha256(raw_files)
    manifest = {
        "source_id": "quranenc-english-saheeh",
        "source": "QuranEnc.com",
        "source_url": "https://quranenc.com/en/browse/english_saheeh",
        "api_url": f"{API_ROOT}/translation/sura/{TRANSLATION_KEY}/{{surah}}",
        "translation_key": TRANSLATION_KEY,
        "language": "en",
        "direction": edition.get("direction"),
        "version": edition["version"],
        "last_update_unix": edition.get("last_update"),
        "title": edition.get("title"),
        "description": edition.get("description"),
        "publisher": "Noor International Center",
        "transcript_notice": (
            "Keep the source/publisher and translation version with redistributed text."
        ),
        "reuse_terms": [
            "Do not modify, add to, or delete translation content.",
            "Clearly identify the publisher and QuranEnc.com as the source.",
            "Include the translation version and transcript/source information.",
            "Notify QuranEnc.com about translation notes and keep the translation updated.",
            "Do not include inappropriate advertisements when displaying the translation.",
        ],
        "surah_count": 114,
        "ayah_count": 6236,
        "files": {
            name: hashlib.sha256(content).hexdigest() for name, content in sorted(raw_files.items())
        },
        "aggregate_sha256": aggregate_hash,
    }

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".english-saheeh-", dir=TARGET.parent) as temp_name:
        temp_dir = Path(temp_name)
        for name, content in raw_files.items():
            (temp_dir / name).write_bytes(content)
        (temp_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        backup = TARGET.with_name(f"{TARGET.name}.previous")
        if backup.exists():
            shutil.rmtree(backup)
        if TARGET.exists():
            TARGET.rename(backup)
        try:
            staged_dir = TARGET.with_name(f"{TARGET.name}.staged")
            if staged_dir.exists():
                shutil.rmtree(staged_dir)
            shutil.copytree(temp_dir, staged_dir)
            staged_dir.rename(TARGET)
        except Exception:
            if backup.exists() and not TARGET.exists():
                backup.rename(TARGET)
            raise
        if backup.exists():
            shutil.rmtree(backup)

    print(
        f"Saved QuranEnc {TRANSLATION_KEY} {EXPECTED_VERSION}: "
        f"{len(raw_files)} surah payloads, SHA-256 {aggregate_hash}."
    )


def main() -> None:
    """Fetch the version-pinned source after validating the full ayah structure."""

    edition, raw_files = asyncio.run(_fetch_all())
    _write_verified_snapshot(edition, raw_files)


if __name__ == "__main__":
    main()
