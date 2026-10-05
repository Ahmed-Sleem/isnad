"""REST contract, evidence preservation, validation, and browser-origin behavior."""

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from isnad_core import __version__
from isnad_core.api.app import app, create_app
from isnad_core.engine import VerificationEngine
from isnad_core.hadith import HadeethEncClient, HadeethEncVerifier
from isnad_core.models import MatchStatus
from isnad_core.quran import QuranCorpus


def test_health_and_readiness_identify_the_active_pinned_corpus() -> None:
    with TestClient(app) as client:
        live = client.get("/health/live")
        ready = client.get("/health/ready")

    assert live.status_code == 200
    assert live.json() == {"status": "live", "service": "isnad-core", "version": __version__}
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    ready_sources = ready.json()["sources"]
    assert {(item["source_id"], item["language"]) for item in ready_sources} == {
        ("tanzil-quran-uthmani", "ar"),
        ("quranenc-english-saheeh", "en"),
        ("hadeethenc-api-v1", "ar"),
        ("hadeethenc-api-v1", "en"),
    }
    assert all(
        item["source_sha256"] is None or len(item["source_sha256"]) == 64 for item in ready_sources
    )
    assert any(item["mode"] == "official_remote_api" for item in ready_sources)
    assert ready.headers["x-request-id"]
    assert ready.headers["cache-control"] == "no-store"


def test_capabilities_expose_supported_statuses_source_and_limits() -> None:
    with TestClient(app) as client:
        response = client.get("/v1/capabilities")

    payload = response.json()
    assert response.status_code == 200
    assert payload["api_version"] == "v1"
    assert payload["status_semantics"] == "textual_match_only_not_authenticity_or_ruling"
    source_keys = {(source["source_id"], source["language"]) for source in payload["sources"]}
    assert source_keys == {
        ("tanzil-quran-uthmani", "ar"),
        ("quranenc-english-saheeh", "en"),
        ("hadeethenc-api-v1", "ar"),
        ("hadeethenc-api-v1", "en"),
    }
    assert all(
        source["coverage_note"]
        for source in payload["sources"]
        if source["source_type"] == "hadith"
    )
    assert {
        source["reference_format"]
        for source in payload["sources"]
        if source["source_type"] == "hadith"
    } == {"hadeethenc:<record_id>"}
    assert payload["limits"]["max_quote_characters"] == 4_000
    assert payload["limits"]["max_stream_characters"] == 1_000_000
    assert payload["limits"]["max_stream_chunk_characters"] == 16_384
    assert payload["limits"]["max_websocket_message_bytes"] == 96 * 1024
    assert payload["streaming"]["transport"] == "websocket"
    assert payload["streaming"]["path"] == "/v1/stream"
    assert payload["streaming"]["block_open_marker"] == "[[ISNAD-CITATION"
    assert payload["streaming"]["block_close_marker"] == "[[/ISNAD-CITATION]]"
    assert "citation_result" in payload["streaming"]["events"]
    assert {status.value for status in MatchStatus} == set(payload["statuses"])


def test_exact_verification_returns_verbatim_evidence_and_raw_submission() -> None:
    source_text = QuranCorpus.load_default().verses_by_reference["1:1"].text
    submitted = f"  {source_text}\n"
    with TestClient(app) as client:
        response = client.post(
            "/v1/verify",
            json={
                "source_type": "QURAN",
                "language": "AR",
                "quote": submitted,
                "reference": "١:١",
            },
        )

    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "exact_match"
    assert payload["submitted_quote"] == submitted
    assert payload["cited_reference"] == "1:1"
    assert payload["evidence"][0]["source_text"] == source_text
    assert payload["source_metadata"]["content_sha256"]


def test_normalized_difference_is_source_backed_and_not_a_generated_rewrite() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/v1/verify",
            json={
                "source_type": "quran",
                "language": "ar",
                "quote": "بسم الله الرحمن الرحيم",
                "reference": "1:1",
            },
        )

    payload = response.json()
    assert payload["status"] == "normalized_match"
    assert payload["wording_differences"]
    assert payload["evidence"][0]["source_text"] == "بِسْمِ ٱللَّهِ ٱلرَّحْمَـٰنِ ٱلرَّحِيمِ"


def test_english_verification_returns_pinned_translation_and_footnotes() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/v1/verify",
            json={
                "source_type": "quran",
                "language": "en",
                "quote": (
                    "In the name of Allāh,[2] the Entirely Merciful, the Especially Merciful.[3]"
                ),
                "reference": "1:1",
            },
        )

    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "exact_match"
    assert payload["source_metadata"]["source_id"] == "quranenc-english-saheeh"
    assert payload["source_metadata"]["version"] == "1.1.2"
    assert payload["evidence"][0]["footnotes"].startswith("[2] Allāh is a proper name")


def test_unsupported_source_is_a_result_not_a_false_not_found() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/v1/verify",
            json={"source_type": "tafsir", "language": "en", "quote": "example report"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "unsupported_source_or_language"
    assert response.json()["source_metadata"] is None
    assert response.json()["evidence"] == []


def test_hadith_route_exposes_match_status_and_source_grade_separately() -> None:
    source_record = {
        "id": "4560",
        "title": "A sample source record",
        "hadeeth": "Narrated by Anas: A unique proverb is helpful, and kindness matters.",
        "grade": "Authentic",
        "attribution": "Agreed upon",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/hadeeths/search/"):
            return httpx.Response(200, json=[{"id": "4560"}])
        if request.url.path.endswith("/hadeeths/multiple/"):
            return httpx.Response(200, json=[source_record])
        raise AssertionError(f"Unexpected endpoint {request.url.path}")

    raw_client = httpx.Client(
        base_url="https://hadeethenc.com/api/v1/",
        transport=httpx.MockTransport(handler),
    )
    engine = VerificationEngine((HadeethEncVerifier("en", HadeethEncClient(raw_client)),))
    api = create_app(engine, cors_origins=[])
    try:
        with TestClient(api) as client:
            response = client.post(
                "/v1/verify",
                json={"source_type": "hadith", "language": "en", "quote": "kindness matters"},
            )
    finally:
        raw_client.close()

    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "partial_match"
    assert payload["source_metadata"]["coverage_note"]
    assert payload["evidence"][0]["grade_text"] == "Authentic"
    assert payload["evidence"][0]["grade_source"] == "HadeethEnc.com record 4560"
    assert payload["evidence"][0]["graded_by"] is None
    assert payload["evidence"][0]["attribution_text"] == "Agreed upon"


def test_remote_source_outage_is_http_503_not_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic outage", request=request)

    raw_client = httpx.Client(
        base_url="https://hadeethenc.com/api/v1/",
        transport=httpx.MockTransport(handler),
    )
    engine = VerificationEngine((HadeethEncVerifier("en", HadeethEncClient(raw_client)),))
    api = create_app(engine, cors_origins=[])
    try:
        with TestClient(api) as client:
            response = client.post(
                "/v1/verify",
                json={"source_type": "hadith", "language": "en", "quote": "kindness matters"},
            )
    finally:
        raw_client.close()

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "source_unavailable"
    assert response.headers["retry-after"] == "10"


def test_invalid_reference_returns_structured_safe_error() -> None:
    canary = "PRIVATE_QUOTE_SHOULD_NOT_BE_ECHOED"
    with TestClient(app) as client:
        response = client.post(
            "/v1/verify",
            json={
                "source_type": "quran",
                "language": "ar",
                "quote": canary,
                "reference": "115:1",
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_reference"
    assert canary not in response.text
    assert response.json()["request_id"] == response.headers["x-request-id"]


def test_pydantic_validation_errors_do_not_echo_oversized_quote() -> None:
    canary = "UNTRUSTED_INPUT_SENTINEL"
    quote = "ا" * 4_001 + canary
    with TestClient(app) as client:
        response = client.post(
            "/v1/verify",
            json={"source_type": "quran", "language": "ar", "quote": quote},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert canary not in response.text
    assert response.json()["error"]["details"]


def test_empty_request_is_rejected_by_contract() -> None:
    with TestClient(app) as client:
        response = client.post("/v1/verify", json={"source_type": "quran", "language": "ar"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_request_body_limit_is_enforced_before_json_validation() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/v1/verify",
            json={
                "source_type": "quran",
                "language": "ar",
                "quote": "ا",
                "padding": "x" * (64 * 1024),
            },
            headers={"Origin": "http://localhost:5173"},
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_cors_allows_local_gui_origin_without_credentials() -> None:
    with TestClient(app) as client:
        response = client.options(
            "/v1/verify",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "access-control-allow-credentials" not in response.headers


def test_cors_does_not_allow_arbitrary_web_origins() -> None:
    with TestClient(app) as client:
        response = client.options(
            "/v1/verify",
            headers={
                "Origin": "https://untrusted.example",
                "Access-Control-Request-Method": "POST",
            },
        )

    assert "access-control-allow-origin" not in response.headers


def test_stream_websocket_releases_prose_and_returns_verified_citation() -> None:
    source_text = QuranCorpus.load_default().verses_by_reference["1:1"].text
    citation = (
        f"[[ISNAD-CITATION source=quran language=ar reference=1:1]]{source_text}[[/ISNAD-CITATION]]"
    )

    with (
        TestClient(app) as client,
        client.websocket_connect(
            "/v1/stream", headers={"origin": "http://localhost:5173"}
        ) as websocket,
    ):
        websocket.send_json({"type": "chunk", "text": "Plain prose first. "})
        prose = websocket.receive_json()
        assert prose == {"type": "text", "text": "Plain prose first. "}

        websocket.send_json({"type": "chunk", "text": citation})
        checking = websocket.receive_json()
        assert checking == {
            "type": "citation_checking",
            "citation_id": 1,
            "placeholder": "checking_citation",
        }
        result = websocket.receive_json()
        assert result["type"] == "citation_result"
        assert result["result"]["status"] == "exact_match"
        assert result["result"]["evidence"][0]["source_text"] == source_text

        websocket.send_json({"type": "chunk", "text": " Plain prose after. "})
        assert websocket.receive_json() == {
            "type": "text",
            "text": " Plain prose after. ",
        }
        websocket.send_json({"type": "finish"})
        assert websocket.receive_json() == {"type": "stream_complete"}


def test_stream_websocket_rejects_unapproved_browser_origins() -> None:
    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect) as disconnect,
        client.websocket_connect("/v1/stream", headers={"origin": "https://untrusted.example"}),
    ):
        pass

    assert disconnect.value.code == 1008


def test_stream_websocket_malformed_citation_never_echoes_its_quote() -> None:
    secret_quote = "unverified private quote"
    malformed = f"[[ISNAD-CITATION source=invalid language=en]]{secret_quote}[[/ISNAD-CITATION]]"
    with TestClient(app) as client, client.websocket_connect("/v1/stream") as websocket:
        websocket.send_json({"type": "chunk", "text": malformed})
        assert websocket.receive_json()["type"] == "citation_checking"
        rejected = websocket.receive_json()
        assert rejected == {
            "type": "citation_rejected",
            "citation_id": 1,
            "code": "malformed_citation_header",
        }
        websocket.send_json({"type": "finish"})
        assert websocket.receive_json() == {"type": "stream_complete"}
