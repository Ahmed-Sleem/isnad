"""Shared API-shaped serializers for REST, MCP, and streaming transports."""

from __future__ import annotations

from isnad_core.api.schemas import (
    CapabilitiesResponse,
    EvidenceResponse,
    LimitsResponse,
    ReadyResponse,
    ReadySourceResponse,
    SourceCapabilityResponse,
    SourceMetadataResponse,
    StreamingContractResponse,
    VerifyResponse,
    WordingDifferenceResponse,
)
from isnad_core.engine import VerificationEngine
from isnad_core.models import MatchStatus, VerificationResult
from isnad_core.quran.verifier import (
    MAX_MATCH_CANDIDATES,
    MAX_QUOTE_CHARACTERS,
    MAX_REFERENCE_AYAHS,
)
from isnad_core.streaming import (
    CLOSE_MARKER,
    MAX_STREAM_CHARACTERS,
    MAX_STREAM_CHUNK_CHARACTERS,
    MAX_WEBSOCKET_MESSAGE_BYTES,
    OPEN_MARKER,
    StreamEventType,
)

SERVICE_NAME = "isnad-core"


def verification_response(result: VerificationResult) -> VerifyResponse:
    """Map the shared result to the stable versioned public response schema."""

    source_metadata = (
        SourceMetadataResponse(
            source_id=result.source_metadata.source_id,
            name=result.source_metadata.name,
            url=result.source_metadata.url,
            version=result.source_metadata.version,
            license=result.source_metadata.license,
            content_sha256=result.source_metadata.content_sha256,
            coverage_note=result.source_metadata.coverage_note,
        )
        if result.source_metadata is not None
        else None
    )
    return VerifyResponse(
        status=result.status,
        source_type=result.source_type,
        language=result.language,
        source_metadata=source_metadata,
        submitted_quote=result.submitted_quote,
        cited_reference=result.cited_reference,
        matched_references=list(result.matched_references),
        evidence=[
            EvidenceResponse(
                reference=item.reference,
                source_text=item.source_text,
                matched_fragment=item.matched_fragment,
                source_id=item.source_id,
                source_version=item.source_version,
                source_url=item.source_url,
                footnotes=item.footnotes,
                record_title=item.record_title,
                attribution_text=item.attribution_text,
                grade_text=item.grade_text,
                grade_source=item.grade_source,
                graded_by=item.graded_by,
                bibliographic_reference=item.bibliographic_reference,
            )
            for item in result.evidence
        ],
        wording_differences=[
            WordingDifferenceResponse(
                kind=item.kind,
                submitted_text=item.submitted_text,
                source_text=item.source_text,
            )
            for item in result.wording_differences
        ],
        explanation=result.explanation,
        candidate_count=result.candidate_count,
        evidence_truncated=result.evidence_truncated,
    )


def capabilities_response(engine: VerificationEngine) -> CapabilitiesResponse:
    """Build the one shared capabilities contract for every integration surface."""

    return CapabilitiesResponse(
        api_version="v1",
        service=SERVICE_NAME,
        status_semantics="textual_match_only_not_authenticity_or_ruling",
        statuses=list(MatchStatus),
        sources=[
            SourceCapabilityResponse(
                source_type=capability.source_type,
                language=capability.language,
                source_id=capability.source_metadata.source_id,
                name=capability.source_metadata.name,
                source_version=capability.source_metadata.version,
                normalization_profile=capability.normalization_profile,
                mode=capability.mode,
                reference_format=capability.reference_format,
                coverage_note=capability.source_metadata.coverage_note,
            )
            for capability in engine.capabilities
        ],
        limits=LimitsResponse(
            max_quote_characters=MAX_QUOTE_CHARACTERS,
            max_reference_ayahs=MAX_REFERENCE_AYAHS,
            max_evidence_candidates=MAX_MATCH_CANDIDATES,
            max_stream_characters=MAX_STREAM_CHARACTERS,
            max_stream_chunk_characters=MAX_STREAM_CHUNK_CHARACTERS,
            max_websocket_message_bytes=MAX_WEBSOCKET_MESSAGE_BYTES,
        ),
        streaming=StreamingContractResponse(
            transport="websocket",
            path="/v1/stream",
            input_message='{"type":"chunk","text":"..."} or {"type":"finish"}',
            block_open_marker=OPEN_MARKER,
            block_close_marker=CLOSE_MARKER,
            placeholder_code="checking_citation",
            events=[event_type.value for event_type in StreamEventType]
            + ["stream_complete", "stream_error"],
            policy=(
                "Prose outside marked citation blocks streams immediately; quote and reference "
                "stay hidden until verification completes. Literal marker text inside a quote "
                "must be backslash-escaped. Malformed or incomplete blocks are rejected without "
                "quote text."
            ),
        ),
        system_prompt_path="/v1/system-prompt",
    )


def readiness_response(engine: VerificationEngine, *, version: str) -> ReadyResponse:
    """Build active source readiness rows, including remote/local adapter modes."""

    return ReadyResponse(
        status="ready",
        service=SERVICE_NAME,
        version=version,
        sources=[
            ReadySourceResponse(
                source_type=capability.source_type,
                language=capability.language,
                source_id=capability.source_metadata.source_id,
                source_version=capability.source_metadata.version,
                source_sha256=capability.source_metadata.content_sha256,
                mode=capability.mode,
            )
            for capability in engine.capabilities
        ],
    )
