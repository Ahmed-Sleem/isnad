#!/usr/bin/env bash
# Run the repository's central correctness, packaging-environment, and secret checks.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON="${PYTHON:-python}"
RUFF="${RUFF:-ruff}"

"$PYTHON" -m compileall -q src tests scripts frontend
"$RUFF" check src tests scripts frontend
"$RUFF" format --check src tests scripts frontend
"$PYTHON" -m pytest -q
"$PYTHON" -m pip check
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT
"$PYTHON" -m pip wheel . --no-deps --no-build-isolation --wheel-dir "$BUILD_DIR" >/dev/null
"$PYTHON" - "$BUILD_DIR" <<'PY'
import hashlib
import json
from pathlib import Path
import sys
import zipfile

wheel_dir = Path(sys.argv[1])
wheels = list(wheel_dir.glob("isnad_core-*.whl"))
assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
with zipfile.ZipFile(wheels[0]) as wheel:
    names = set(wheel.namelist())
    required = {
        "isnad_core/data/quran/manifest.json",
        "isnad_core/data/quran/tanzil-uthmani-v1.1.xml",
        "isnad_core/data/quranenc/english_saheeh/manifest.json",
        "isnad_core/data/quranenc/english_saheeh/surah_114.json",
        "isnad_core/api/templates/gui.html",
        "isnad_core/api/static/isnad-gui.html",
    }
    missing = required - names
    assert not missing, f"wheel is missing package data: {sorted(missing)}"
    assert not any(name.startswith(("tests/", "scripts/")) for name in names)
    manifest = json.loads(wheel.read("isnad_core/data/quran/manifest.json"))
    corpus = wheel.read("isnad_core/data/quran/tanzil-uthmani-v1.1.xml")
    assert hashlib.sha256(corpus).hexdigest() == manifest["sha256"]
    quranenc_root = "isnad_core/data/quranenc/english_saheeh/"
    translation_manifest = json.loads(wheel.read(quranenc_root + "manifest.json"))
    translation_payloads = {
        name: wheel.read(quranenc_root + name) for name in translation_manifest["files"]
    }
    for name, payload in translation_payloads.items():
        assert hashlib.sha256(payload).hexdigest() == translation_manifest["files"][name]
    digest = hashlib.sha256()
    for name in sorted(translation_payloads):
        digest.update(name.encode("utf-8"))
        digest.update(bytes([0]))
        digest.update(translation_payloads[name])
        digest.update(bytes([0]))
    assert digest.hexdigest() == translation_manifest["aggregate_sha256"]
print("Wheel verification passed: pinned corpus data is packaged without repository tooling.")
PY
"$PYTHON" scripts/secret_scan.py
bash -n scripts/verify.sh
