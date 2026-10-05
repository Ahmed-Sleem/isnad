"""MCP tools exposing the same versioned Isnad verification contract as REST."""

from __future__ import annotations

from pydantic import ValidationError

from isnad_core.api.schemas import CapabilitiesResponse, VerifyRequest, VerifyResponse
from isnad_core.api.serializers import capabilities_response, verification_response
from isnad_core.engine import VerificationEngine
from isnad_core.errors import SourceUnavailable
from isnad_core.hadith import InvalidHadithReference
from isnad_core.models import VerificationInput
from isnad_core.quran import InvalidQuranReference


def create_mcp_server(engine: VerificationEngine | None = None):
    """Create an stdio-capable MCP server using an injected or default engine."""

    try:
        from mcp.server import MCPServer
        from mcp.server.mcpserver.exceptions import ToolError
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "The MCP adapter requires the optional dependency: pip install 'isnad-core[mcp]'."
        ) from exc

    shared_engine = engine if engine is not None else VerificationEngine()
    server = MCPServer("isnad-core")

    @server.tool()
    def verify_citation(
        source_type: str,
        language: str,
        quote: str | None = None,
        reference: str | None = None,
    ) -> VerifyResponse:
        """Check a Qur'an or HadeethEnc citation against the supported source edition.

        The result is textual correspondence only. Hadith match status is separate from
        HadeethEnc's explicitly sourced grade and attribution fields; no ruling is issued.
        """

        try:
            request = VerifyRequest.model_validate(
                {
                    "source_type": source_type,
                    "language": language,
                    "quote": quote,
                    "reference": reference,
                }
            )
        except ValidationError:
            raise ToolError(
                "Invalid citation input. Provide a quote, a reference, or both, and use a "
                "supported source and language."
            ) from None

        try:
            result = shared_engine.verify(
                VerificationInput(
                    source=request.source_type,
                    language=request.language,
                    quote=request.quote,
                    reference=request.reference,
                )
            )
        except SourceUnavailable:
            raise ToolError(
                "A checked source is temporarily unavailable. Retry later; this is not a "
                "not-found result."
            ) from None
        except (InvalidHadithReference, InvalidQuranReference, ValueError):
            raise ToolError("The citation or source-local reference is invalid.") from None
        return verification_response(result)

    @server.tool()
    def isnad_capabilities() -> CapabilitiesResponse:
        """List supported sources, languages, statuses, limits, and stream protocol."""

        return capabilities_response(shared_engine)

    return server


def main() -> None:
    """Run the MCP server over stdio, keeping diagnostic output off the protocol pipe."""

    engine = VerificationEngine()
    try:
        create_mcp_server(engine).run(transport="stdio")
    finally:
        engine.close()


if __name__ == "__main__":  # pragma: no cover - exercised by an MCP host
    main()
