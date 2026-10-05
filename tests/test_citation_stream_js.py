"""The browser-side citation streamer, exercised under node.

The parser decides what may be shown while a model is still typing, so it is
tested directly rather than only through the browser smoke test. node is an
optional tool: without it these tests skip and the rest of the suite still runs.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST_FILE = ROOT / "frontend" / "citation_stream.test.cjs"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_citation_streamer_behaviour() -> None:
    result = subprocess.run(
        [NODE, str(TEST_FILE)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "all assertions passed" in result.stdout


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_streamer_markers_match_the_server_contract() -> None:
    """One protocol, two implementations: the browser and the gate must agree."""

    script = (
        "const s = require('./frontend/citation_stream.js');"
        "process.stdout.write(JSON.stringify({open: s.OPEN_MARKER, close: s.CLOSE_MARKER}));"
    )
    result = subprocess.run(
        [NODE, "-e", script],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    import json

    from isnad_core.streaming import CLOSE_MARKER, OPEN_MARKER

    markers = json.loads(result.stdout)
    assert markers["open"] == OPEN_MARKER
    assert markers["close"] == CLOSE_MARKER
