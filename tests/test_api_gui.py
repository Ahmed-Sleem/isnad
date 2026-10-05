"""The served interface must stay self-contained, honest in its copy, and in sync with the API."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from fastapi.testclient import TestClient

from isnad_core.api.app import create_app

ROOT = Path(__file__).resolve().parents[1]
API_DIRECTORY = ROOT / "src" / "isnad_core" / "api"
SERVED_TEMPLATE = API_DIRECTORY / "templates" / "gui.html"
STANDALONE_BUILD = API_DIRECTORY / "static" / "isnad-gui.html"


def _client() -> TestClient:
    return TestClient(create_app())


def test_interface_is_served_with_a_restrictive_policy() -> None:
    with _client() as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    policy = response.headers["content-security-policy"]
    assert "default-src 'none'" in policy
    assert "frame-ancestors" not in policy
    assert "http://" not in policy and "https://" not in policy


def test_gui_alias_serves_the_same_interface() -> None:
    with _client() as client:
        root = client.get("/")
        alias = client.get("/gui")

    assert alias.status_code == 200
    assert alias.text == root.text


def test_interface_loads_no_external_resources() -> None:
    html = SERVED_TEMPLATE.read_text(encoding="utf-8")

    assert "@import" not in html
    assert "fonts.googleapis.com" not in html
    assert not re.search(r'src="https?://', html)
    assert not re.search(r"<script[^>]+src=", html)
    # The only permitted <link> is the favicon, and only as an inline data: URI:
    # a page that may be opened from disk must fetch nothing from the network.
    links = re.findall(r"<link[^>]*>", html)
    assert links, "the interface should carry its own favicon"
    for tag in links:
        assert 'rel="icon"' in tag, tag
        assert 'href="data:' in tag, tag
    assert not re.search(r'href="https?://', html)
    # The only permitted external references are links the user can choose to
    # follow, which are rendered from API-supplied source URLs at runtime.


def test_interface_never_presents_a_grade_or_authenticity_claim() -> None:
    html = SERVED_TEMPLATE.read_text(encoding="utf-8")

    assert "never infers one" in html
    assert "not a hadith authenticity grade or religious ruling" in html
    assert "A failed check is not a not-found result" in html
    # An unsupported source must not read as an absence in the corpus.
    assert "Source or language not supported" in html
    assert "fabricated" in html


def test_interface_declares_the_api_contract_it_uses() -> None:
    html = SERVED_TEMPLATE.read_text(encoding="utf-8")

    for contract_path in ("/v1/capabilities", "/health/ready", "/v1/verify"):
        assert contract_path in html
    assert "ISNAD_CORS_ORIGINS" in html
    for status in (
        "exact_match",
        "normalized_match",
        "partial_match",
        "mismatch_at_cited_reference",
        "quote_found_wrong_reference",
        "reference_found_without_quote",
        "not_found_in_checked_corpus",
        "ambiguous_multiple_matches",
        "unsupported_source_or_language",
    ):
        assert status in html


def test_standalone_download_is_offered_as_an_attachment() -> None:
    with _client() as client:
        response = client.get("/gui/standalone.html")

    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment")
    assert "isnad-gui.html" in response.headers["content-disposition"]
    assert response.text == STANDALONE_BUILD.read_text(encoding="utf-8")


def test_committed_builds_match_their_sources() -> None:
    """frontend/ is the source of truth; the served and downloadable files are generated."""

    spec = importlib.util.spec_from_file_location("isnad_gui_build", ROOT / "frontend" / "build.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.render(standalone=False) == SERVED_TEMPLATE.read_text(encoding="utf-8")
    assert module.render(standalone=True) == STANDALONE_BUILD.read_text(encoding="utf-8")


def test_both_builds_share_one_body_and_script() -> None:
    served = SERVED_TEMPLATE.read_text(encoding="utf-8")
    standalone = STANDALONE_BUILD.read_text(encoding="utf-8")

    served_body = served.split("</head>", 1)[1]
    standalone_body = standalone.split("</head>", 1)[1]
    assert served_body == standalone_body
    assert '<meta name="isnad-build" content="standalone">' in standalone
    assert '<meta http-equiv="Content-Security-Policy"' in served
