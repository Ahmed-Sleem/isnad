"""Versioned Pydantic contracts for the REST API."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from isnad_core.models import MatchStatus


class VerifyRequest(BaseModel):
    """One citation candidate to compare against a supported source."""

    model_config = ConfigDict(extra="forbid")

    source_type: str = Field(min_length=1, max_length=32, examples=["quran"])
    language: str = Field(min_length=2, max_length=8, examples=["ar"])
    quote: str | None = Field(default=None, max_length=4_000)
    reference: str | None = Field(default=None, max_length=64, examples=["2:255"])

    @field_validator("source_type", "language")
    @classmethod
    def normalize_label(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not normalized:
            raise ValueError("This field must not be blank.")
        return normalized

    @model_validator(mode="after")
    def require_quote_or_reference(self) -> VerifyRequest:
        has_quote = self.quote is not None and bool(self.quote.strip())
        has_reference = self.reference is not None and bool(self.reference.strip())
        if not has_quote and not has_reference:
            raise ValueError("Provide a quote, a reference, or both.")
        return self


class SourceMetadataResponse(BaseModel):
    """Provenance for the exact source dataset used to generate evidence."""

    source_id: str
    name: str
    url: str
    version: str
    license: str
    content_sha256: str | None
    coverage_note: str | None


class EvidenceResponse(BaseModel):
    """Verbatim source text and locator supporting a result."""

    reference: str
    source_text: str
    matched_fragment: str | None
    source_id: str
    source_version: str
    source_url: str
    footnotes: str | None
    record_title: str | None
    attribution_text: str | None
    grade_text: str | None
    grade_source: str | None
    graded_by: str | None
    bibliographic_reference: str | None


class WordingDifferenceResponse(BaseModel):
    """A source-backed word-level difference; values are never generated corrections."""

    kind: str
    submitted_text: str
    source_text: str


class VerifyResponse(BaseModel):
    """Stable response envelope for textual citation verification."""

    status: MatchStatus
    source_type: str
    language: str
    source_metadata: SourceMetadataResponse | None
    submitted_quote: str | None
    cited_reference: str | None
    matched_references: list[str]
    evidence: list[EvidenceResponse]
    wording_differences: list[WordingDifferenceResponse]
    explanation: str
    candidate_count: int | None
    evidence_truncated: bool


class ErrorDetailResponse(BaseModel):
    """Sanitized validation location and machine-readable error category."""

    location: list[str | int]
    code: str


class ErrorBodyResponse(BaseModel):
    """Safe API error; never echoes the submitted quote or credentials."""

    code: str
    message: str
    details: list[ErrorDetailResponse] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    """Common structured error envelope."""

    request_id: str
    error: ErrorBodyResponse


class HealthResponse(BaseModel):
    """Health state for deployment probes."""

    status: str
    service: str
    version: str


class ReadySourceResponse(BaseModel):
    """A pinned source edition loaded and available in the process."""

    source_type: str
    language: str
    source_id: str
    source_version: str
    source_sha256: str | None
    mode: str


class ReadyResponse(HealthResponse):
    """Readiness details identifying all active pinned source editions."""

    sources: list[ReadySourceResponse]


class SourceCapabilityResponse(BaseModel):
    """A source/language pair currently supported by a verifier adapter."""

    source_type: str
    language: str
    source_id: str
    name: str
    source_version: str
    normalization_profile: str
    mode: str
    reference_format: str
    coverage_note: str | None


class LimitsResponse(BaseModel):
    """Public input and evidence bounds for GUI validation."""

    max_quote_characters: int
    max_reference_ayahs: int
    max_evidence_candidates: int
    max_stream_characters: int
    max_stream_chunk_characters: int
    max_websocket_message_bytes: int


class StreamingContractResponse(BaseModel):
    """Discoverable WebSocket message and event contract for citation streaming."""

    transport: str
    path: str
    input_message: str
    block_open_marker: str
    block_close_marker: str
    placeholder_code: str
    events: list[str]
    policy: str


class SystemPromptResponse(BaseModel):
    """Canonical citation protocol prompt for a model-facing client."""

    version: str
    prompt: str
    block_open_marker: str
    block_close_marker: str


class CapabilitiesResponse(BaseModel):
    """Discoverable contract details for clients such as the user's GUI."""

    api_version: str
    service: str
    status_semantics: str
    statuses: list[MatchStatus]
    sources: list[SourceCapabilityResponse]
    limits: LimitsResponse
    streaming: StreamingContractResponse
    system_prompt_path: str
