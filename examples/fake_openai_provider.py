"""A local, scripted OpenAI-compatible streaming endpoint for testing this UI.

It exists so the chat path — system prompt injection, token streaming, citation
markers split across chunks — can be exercised end to end without a provider
account, and without spending tokens on every test run.

    python examples/fake_openai_provider.py --port 8123

Then in the interface: Settings → Model → provider "Other OpenAI-compatible
endpoint", base URL ``http://127.0.0.1:8123/v1``, any model name.

This is a test double, not a model: it replies with a fixed script. It is not
part of the deployed package and it is not a source of any verified text — the
quotations in it are checked by Isnad Core exactly like a real model's output.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MODEL_NAME = "fake-citation-model"

# Two quotations: one the pinned corpus contains, and one it does not, so a
# single run shows a verified card and a mismatch card in the same answer.
SCRIPT = (
    "Here is a short answer with two quotations.\n\n"
    "The first is from Sūrat al-Ikhlāṣ:\n\n"
    "[[ISNAD-CITATION source=quran language=en reference=112:1]]"
    'Say, "He is Allāh, [who is] One'  # exactly the pinned QuranEnc wording
    "[[/ISNAD-CITATION]]\n\n"
    "And here is a sentence I am not sure about:\n\n"
    "[[ISNAD-CITATION source=quran language=en reference=112:2]]"
    "Allah is the Eternal Refuge and the Everlasting Guardian of all things, "
    "who neither sleeps nor tires."
    "[[/ISNAD-CITATION]]\n\n"
    "Both of those were marked so they can be checked before you rely on them."
)

# Delivery chunk size. Small on purpose: markers and quotes arrive split across
# chunks, which is what a real provider does.
CHUNK_SIZE = 17


class Handler(BaseHTTPRequestHandler):
    """Serve the two endpoints the interface uses, with permissive CORS for local testing."""

    server_version = "IsnadFakeProvider/0.1"

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib naming
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path.rstrip("/").endswith("/v1/models"):
            body = json.dumps({"object": "list", "data": [{"id": MODEL_NAME, "object": "model"}]})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._cors()
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            return
        self.send_response(404)
        self._cors()
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self.send_response(404)
            self._cors()
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self.send_response(400)
            self._cors()
            self.end_headers()
            return

        self._report(payload)

        if not payload.get("stream"):
            self._send_json({"error": {"message": "this test double only streams"}}, status=400)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self._cors()
        self.end_headers()

        try:
            for index in range(0, len(SCRIPT), CHUNK_SIZE):
                piece = SCRIPT[index : index + CHUNK_SIZE]
                frame = {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion.chunk",
                    "model": MODEL_NAME,
                    "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
                }
                self.wfile.write(f"data: {json.dumps(frame)}\n\n".encode())
                self.wfile.flush()
                time.sleep(0.02)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # The user stopped the answer, or navigated away.
            pass

    def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _report(self, payload: dict[str, Any]) -> None:
        """Print what arrived, so a test can assert the protocol was injected."""

        messages = payload.get("messages") or []
        system = [m for m in messages if m.get("role") == "system"]
        summary = {
            "model": payload.get("model"),
            "messages": len(messages),
            "system_messages": len(system),
            "system_prompt_characters": len(system[0].get("content", "")) if system else 0,
            "stream": payload.get("stream"),
        }
        print("request: " + json.dumps(summary), file=sys.stdout, flush=True)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        """Silence the default per-request logging; _report prints what matters."""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(
        f"scripted provider on http://{args.host}:{args.port}/v1 "
        f"(model {MODEL_NAME}, streaming only)",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
