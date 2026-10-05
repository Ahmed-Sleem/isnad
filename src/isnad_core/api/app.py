"""FastAPI application exposing the source-grounded verification core."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.middleware.cors import CORSMiddleware

from isnad_core import __version__
from isnad_core.api.middleware import RequestBodyLimitMiddleware
from isnad_core.api.schemas import (
    CapabilitiesResponse,
    ErrorDetailResponse,
    ErrorResponse,
    HealthResponse,
    ReadyResponse,
    SystemPromptResponse,
    VerifyRequest,
    VerifyResponse,
)
from isnad_core.api.serializers import (
    capabilities_response,
    readiness_response,
    verification_response,
)
from isnad_core.engine import VerificationEngine
from isnad_core.errors import SourceUnavailable
from isnad_core.hadith import InvalidHadithReference
from isnad_core.models import VerificationInput
from isnad_core.prompt import system_prompt_response
from isnad_core.quran import InvalidQuranReference
from isnad_core.streaming import (
    MAX_STREAM_CHARACTERS,
    MAX_STREAM_CHUNK_CHARACTERS,
    MAX_WEBSOCKET_MESSAGE_BYTES,
    StreamEvent,
    StreamingCitationGate,
)

SERVICE_NAME = "isnad-core"
MAX_HTTP_BODY_BYTES = 64 * 1024
_API_DIRECTORY = Path(__file__).resolve().parent
_GUI_TEMPLATE_PATH = _API_DIRECTORY / "templates" / "gui.html"
_GUI_STANDALONE_PATH = _API_DIRECTORY / "static" / "isnad-gui.html"
# The interface is a single file with no external assets, so the served policy
# can stay this narrow: no frames other than the deployment's own, no external
# script or style origins, and no fetched assets. Connections stay open because
# the page also streams from whichever model endpoint the user configures; it
# loads nothing on its own initiative.
_GUI_CSP = (
    "default-src 'none'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src *; "
    "form-action 'none'; "
    "base-uri 'none'"
)
_STREAM_STOP = object()
_DEFAULT_CORS_ORIGINS = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)


def _cors_origins_from_environment() -> list[str]:
    configured = os.environ.get("ISNAD_CORS_ORIGINS")
    raw_origins = _DEFAULT_CORS_ORIGINS if configured is None else configured.split(",")
    origins = [origin.strip() for origin in raw_origins if origin.strip()]
    if "*" in origins and len(origins) != 1:
        raise ValueError("ISNAD_CORS_ORIGINS cannot combine '*' with explicit origins.")
    return origins


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: list[ErrorDetailResponse] | None = None,
) -> JSONResponse:
    payload = ErrorResponse(
        request_id=getattr(request.state, "request_id", ""),
        error={"code": code, "message": message, "details": details or []},
    )
    return JSONResponse(status_code=status_code, content=payload.model_dump(mode="json"))


def _stream_event_payload(event: StreamEvent) -> dict[str, object]:
    payload: dict[str, object] = {"type": event.type.value}
    if event.text is not None:
        payload["text"] = event.text
    if event.citation_id is not None:
        payload["citation_id"] = event.citation_id
    if event.placeholder is not None:
        payload["placeholder"] = event.placeholder
    if event.result is not None:
        payload["result"] = verification_response(event.result).model_dump(mode="json")
    if event.code is not None:
        payload["code"] = event.code
    return payload


def _gui_content_security_policy() -> str:
    """Return the interface policy, allowing extra frame ancestors only if configured."""

    frame_ancestors = os.environ.get("ISNAD_GUI_FRAME_ANCESTORS", "").strip()
    if not frame_ancestors:
        return _GUI_CSP
    return f"{_GUI_CSP}; frame-ancestors {frame_ancestors}"


def _next_stream_event(events: Iterator[StreamEvent]) -> StreamEvent | object:
    try:
        return next(events)
    except StopIteration:
        return _STREAM_STOP


def create_app(
    engine: VerificationEngine | None = None,
    *,
    cors_origins: list[str] | None = None,
) -> FastAPI:
    """Create an API app; inject an engine in tests or use the pinned local corpora."""

    app = FastAPI(
        title="Isnad Core API",
        summary="Source-grounded Arabic and English Qur'an and hadith citation verification.",
        version=__version__,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.engine = engine if engine is not None else VerificationEngine()
    origins = cors_origins if cors_origins is not None else _cors_origins_from_environment()
    if "*" in origins and len(origins) != 1:
        raise ValueError("CORS cannot combine '*' with explicit origins.")
    app.state.allowed_websocket_origins = frozenset(origins)
    app.state.allow_any_websocket_origin = origins == ["*"]
    app.add_middleware(RequestBodyLimitMiddleware, max_body_bytes=MAX_HTTP_BODY_BYTES)

    @app.middleware("http")
    async def response_security_headers(request: Request, call_next):
        request_id = uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-Request-ID"],
        max_age=600,
    )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = [
            ErrorDetailResponse(
                location=[part for part in error.get("loc", ()) if part != "body"],
                code=str(error.get("type", "invalid")),
            )
            for error in exc.errors()
        ]
        return _error_response(
            request,
            status_code=422,
            code="invalid_request",
            message="Request body did not match the API contract.",
            details=details,
        )

    @app.exception_handler(InvalidQuranReference)
    async def invalid_reference_error_handler(
        request: Request, _exc: InvalidQuranReference
    ) -> JSONResponse:
        return _error_response(
            request,
            status_code=422,
            code="invalid_reference",
            message="The reference is malformed or outside the pinned Qur'an corpus.",
        )

    @app.exception_handler(InvalidHadithReference)
    async def invalid_hadith_reference_error_handler(
        request: Request, _exc: InvalidHadithReference
    ) -> JSONResponse:
        return _error_response(
            request,
            status_code=422,
            code="invalid_reference",
            message="Hadith references must use the HadeethEnc record form hadeethenc:<id>.",
        )

    @app.exception_handler(SourceUnavailable)
    async def source_unavailable_error_handler(
        request: Request, _exc: SourceUnavailable
    ) -> JSONResponse:
        response = _error_response(
            request,
            status_code=503,
            code="source_unavailable",
            message=(
                "A checked source is temporarily unavailable; no not-found result was inferred."
            ),
        )
        response.headers["Retry-After"] = "10"
        return response

    @app.exception_handler(ValueError)
    async def invalid_verification_error_handler(
        request: Request, _exc: ValueError
    ) -> JSONResponse:
        return _error_response(
            request,
            status_code=422,
            code="invalid_citation",
            message="The citation could not be compared; check the quote and reference fields.",
        )

    @app.get("/health/live", response_model=HealthResponse, tags=["health"])
    def health_live() -> HealthResponse:
        return HealthResponse(status="live", service=SERVICE_NAME, version=__version__)

    @app.get("/health/ready", response_model=ReadyResponse, tags=["health"])
    def health_ready() -> ReadyResponse:
        return readiness_response(app.state.engine, version=__version__)

    @app.get("/v1/capabilities", response_model=CapabilitiesResponse, tags=["metadata"])
    def capabilities() -> CapabilitiesResponse:
        return capabilities_response(app.state.engine)

    @app.get("/v1/system-prompt", response_model=SystemPromptResponse, tags=["metadata"])
    def system_prompt() -> SystemPromptResponse:
        """Serve the citation protocol prompt a client injects into a model session."""

        return SystemPromptResponse(**system_prompt_response())

    @app.post("/v1/verify", response_model=VerifyResponse, tags=["verification"])
    def verify_citation(payload: VerifyRequest) -> VerifyResponse:
        result = app.state.engine.verify(
            VerificationInput(
                source=payload.source_type,
                language=payload.language,
                quote=payload.quote,
                reference=payload.reference,
            )
        )
        return verification_response(result)

    # The interface is part of this deployment, not a separate front end: it is
    # served from the same origin with a restrictive policy, and it talks to the
    # API routes above. `/gui` is an alias so the page is reachable when another
    # service owns the root path.
    @app.get("/", include_in_schema=False)
    @app.get("/gui", include_in_schema=False)
    def verification_interface() -> FileResponse:
        return FileResponse(
            _GUI_TEMPLATE_PATH,
            media_type="text/html; charset=utf-8",
            headers={"Content-Security-Policy": _gui_content_security_policy()},
        )

    # The same interface as one self-contained download: open it from disk and
    # point it at any deployment through Connection settings.
    @app.get("/gui/standalone.html", include_in_schema=False)
    def standalone_interface() -> FileResponse:
        return FileResponse(
            _GUI_STANDALONE_PATH,
            media_type="text/html; charset=utf-8",
            filename="isnad-gui.html",
            headers={"Content-Security-Policy": _gui_content_security_policy()},
        )

    @app.websocket("/v1/stream")
    async def stream_gate(websocket: WebSocket) -> None:
        origin = websocket.headers.get("origin")
        if (
            origin is not None
            and not app.state.allow_any_websocket_origin
            and origin not in app.state.allowed_websocket_origins
        ):
            await websocket.close(code=1008)
            return

        await websocket.accept()
        gate = StreamingCitationGate(app.state.engine)
        total_characters = 0

        async def send_gate_events(events: Iterator[StreamEvent]) -> None:
            while True:
                event = await run_in_threadpool(_next_stream_event, events)
                if event is _STREAM_STOP:
                    return
                await websocket.send_json(_stream_event_payload(event))

        async def close_with_error(code: str, close_code: int = 1003) -> None:
            await websocket.send_json({"type": "stream_error", "error": {"code": code}})
            await websocket.close(code=close_code)

        while True:
            try:
                raw_message = await websocket.receive_text()
            except WebSocketDisconnect:
                return

            if len(raw_message.encode("utf-8")) > MAX_WEBSOCKET_MESSAGE_BYTES:
                await close_with_error("stream_message_too_large", close_code=1009)
                return
            try:
                message = json.loads(raw_message)
            except json.JSONDecodeError:
                await close_with_error("invalid_stream_message")
                return
            if not isinstance(message, dict) or not isinstance(message.get("type"), str):
                await close_with_error("invalid_stream_message")
                return

            if message["type"] == "chunk":
                if set(message) != {"type", "text"} or not isinstance(message["text"], str):
                    await close_with_error("invalid_stream_message")
                    return
                text = message["text"]
                if len(text) > MAX_STREAM_CHUNK_CHARACTERS:
                    await close_with_error("stream_chunk_too_large", close_code=1009)
                    return
                total_characters += len(text)
                if total_characters > MAX_STREAM_CHARACTERS:
                    await close_with_error("stream_too_large", close_code=1009)
                    return
                await send_gate_events(gate.feed(text))
                continue

            if message["type"] == "finish" and set(message) == {"type"}:
                await send_gate_events(gate.finish())
                await websocket.send_json({"type": "stream_complete"})
                await websocket.close(code=1000)
                return

            await close_with_error("invalid_stream_message")
            return

    return app


app = create_app()
