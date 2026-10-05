"""End-to-end contract check: scripted provider -> stream gate -> core verifier.

This is the test that would have caught a mismatch between what the interface
believes and what the checker believes. It runs the same route the browser
takes, in order:

1. ``GET /v1/system-prompt`` — the protocol text the interface injects as the
   system message.
2. The scripted provider in ``examples/fake_openai_provider.py`` answers with
   that system message in place, streaming an answer whose markers are split
   across chunks the way a real provider splits them.
3. The deltas are fed to :class:`StreamingCitationGate`, the server-side gate
   built on the same markers as the browser streamer.
4. The gate's results are compared to what ``/v1/verify`` returns for the same
   quotes, so the chat card and the REST answer cannot disagree.

The provider is a test double with a fixed script; no network access, no API
key, and no tokens are spent.
"""

from __future__ import annotations

import importlib.util
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from isnad_core.api.app import app
from isnad_core.models import MatchStatus
from isnad_core.streaming import StreamEventType, StreamingCitationGate

_PROVIDER_PATH = Path(__file__).resolve().parents[1] / "examples" / "fake_openai_provider.py"


def _load_provider():
    spec = importlib.util.spec_from_file_location("isnad_fake_provider", _PROVIDER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def provider():
    """Run the scripted provider on an ephemeral port and capture its requests."""

    module = _load_provider()
    captured: list[dict] = []

    class CapturingHandler(module.Handler):
        def _report(self, payload):  # noqa: ANN001 - test double
            captured.append(payload)

        def log_message(self, format, *args):  # noqa: A002 - stdlib signature
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), CapturingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}/v1"
    try:
        yield {"base": base, "module": module, "captured": captured}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _stream_completion(base: str, messages: list[dict]) -> list[str]:
    """POST a streaming completion and return the content deltas, in order."""

    body = json.dumps(
        {"model": "fake-citation-model", "messages": messages, "stream": True}
    ).encode()
    request = urllib.request.Request(  # noqa: S310 - fixed http://127.0.0.1 URL
        f"{base}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer test-key"},
        method="POST",
    )
    deltas: list[str] = []
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        for raw in response:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if payload == "[DONE]":
                break
            chunk = json.loads(payload)
            piece = chunk["choices"][0]["delta"].get("content")
            if piece:
                deltas.append(piece)
    return deltas


def test_scripted_answer_is_verified_by_the_shared_core(provider) -> None:
    with TestClient(app) as client:
        prompt_response = client.get("/v1/system-prompt")
        assert prompt_response.status_code == 200
        protocol = prompt_response.json()
        system_message = protocol["prompt"]

        deltas = _stream_completion(
            provider["base"],
            [
                {"role": "system", "content": system_message},
                {"role": "user", "content": "Give me a short answer with quotations."},
            ],
        )

        # The interface streams the answer before anything is checked.
        assert len(deltas) > 1, "the provider must stream in more than one chunk"
        assert "".join(deltas).count(protocol["block_open_marker"]) == 2

        gate = StreamingCitationGate(app.state.engine)
        events = [event for chunk in deltas for event in gate.feed(chunk)]
        events.extend(gate.finish())

        kinds = [event.type for event in events]
        assert kinds.count(StreamEventType.CITATION_CHECKING) == 2
        assert kinds.count(StreamEventType.TEXT) >= 3

        prose = "".join(event.text for event in events if event.type is StreamEventType.TEXT)
        assert "Here is a short answer with two quotations." in prose
        assert "Both of those were marked" in prose

        # No held text may escape as prose, in any chunking.
        for marker in (protocol["block_open_marker"], protocol["block_close_marker"]):
            assert marker not in prose
        assert "Eternal Refuge and the Everlasting Guardian" not in prose

        results = [
            event.result for event in events if event.type is StreamEventType.CITATION_RESULT
        ]
        assert len(results) == 2
        assert all(result is not None for result in results)

        # A citation the pinned corpus contains.
        assert results[0].status in {MatchStatus.EXACT_MATCH, MatchStatus.NORMALIZED_MATCH}
        assert results[0].source_metadata is not None
        assert results[0].source_metadata.source_id == "quranenc-english-saheeh"
        assert results[0].matched_references

        # A quotation the corpus does not contain: it is reported, not silently shown.
        assert results[1].status not in {MatchStatus.EXACT_MATCH, MatchStatus.NORMALIZED_MATCH}
        assert results[1].explanation

        # The gate's verdict and the REST verdict are the same computation.
        for result in results:
            verified = client.post(
                "/v1/verify",
                json={
                    "source_type": result.source_type,
                    "language": result.language,
                    "quote": result.submitted_quote,
                    "reference": result.cited_reference,
                },
            )
            assert verified.status_code == 200
            assert verified.json()["status"] == result.status.value


def test_provider_received_the_injected_protocol(provider) -> None:
    """The system message that reaches the model is the published protocol verbatim."""

    with TestClient(app) as client:
        protocol = client.get("/v1/system-prompt").json()

    captured = provider["captured"]
    assert captured, "the provider was not called"
    request = captured[-1]
    system_messages = [m for m in request["messages"] if m.get("role") == "system"]
    assert len(system_messages) == 1
    assert system_messages[0]["content"] == protocol["prompt"]
    assert request["stream"] is True

    # Everything the interface needs to check the answer is in that one message.
    for fragment in (
        protocol["block_open_marker"],
        protocol["block_close_marker"],
        "reference=",
        "hadeethenc:",
    ):
        assert fragment in system_messages[0]["content"]
