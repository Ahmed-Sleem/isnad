"""The MCP adapter calls the shared engine and preserves the REST response schema."""

import pytest

from isnad_core.engine import VerificationEngine
from isnad_core.errors import SourceUnavailable
from isnad_core.integrations.mcp_server import create_mcp_server
from isnad_core.quran import QuranCorpus


def _mcp_client_class():
    pytest.importorskip("mcp")
    from mcp import Client

    return Client


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_mcp_tools_expose_shared_capabilities_and_verification_contract() -> None:
    client_class = _mcp_client_class()
    engine = VerificationEngine()
    server = create_mcp_server(engine)
    source_text = QuranCorpus.load_default().verses_by_reference["1:1"].text
    try:
        async with client_class(server, raise_exceptions=True) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "verify_citation",
                "isnad_capabilities",
            }

            capabilities = await client.call_tool("isnad_capabilities", {})
            assert capabilities.is_error is not True
            capability_data = capabilities.structured_content
            assert capability_data["status_semantics"] == (
                "textual_match_only_not_authenticity_or_ruling"
            )
            assert capability_data["streaming"]["path"] == "/v1/stream"

            result = await client.call_tool(
                "verify_citation",
                {
                    "source_type": "quran",
                    "language": "ar",
                    "quote": source_text,
                    "reference": "1:1",
                },
            )
            assert result.is_error is not True
            data = result.structured_content
            assert data["status"] == "exact_match"
            assert data["evidence"][0]["source_text"] == source_text
            assert data["evidence"][0]["grade_text"] is None
    finally:
        engine.close()


@pytest.mark.anyio
async def test_mcp_invalid_reference_is_a_safe_tool_error() -> None:
    client_class = _mcp_client_class()
    engine = VerificationEngine()
    server = create_mcp_server(engine)
    canary = "UNVERIFIED_QUOTE_NOT_FOR_TOOL_ERRORS"
    try:
        async with client_class(server, raise_exceptions=True) as client:
            result = await client.call_tool(
                "verify_citation",
                {
                    "source_type": "quran",
                    "language": "ar",
                    "quote": canary,
                    "reference": "999:999",
                },
            )
    finally:
        engine.close()

    assert result.is_error is True
    error_text = " ".join(content.text for content in result.content if content.type == "text")
    assert canary not in error_text
    assert "invalid" in error_text.casefold()


@pytest.mark.anyio
async def test_mcp_upstream_outage_is_a_safe_error_not_not_found() -> None:
    client_class = _mcp_client_class()

    class OutageEngine:
        def verify(self, _citation):
            raise SourceUnavailable("synthetic outage")

    server = create_mcp_server(OutageEngine())
    canary = "UNVERIFIED_OUTAGE_QUOTE"
    async with client_class(server, raise_exceptions=True) as client:
        result = await client.call_tool(
            "verify_citation",
            {"source_type": "hadith", "language": "en", "quote": canary},
        )

    assert result.is_error is True
    error_text = " ".join(content.text for content in result.content if content.type == "text")
    assert "temporarily unavailable" in error_text
    assert "not a not-found result" in error_text
    assert canary not in error_text
