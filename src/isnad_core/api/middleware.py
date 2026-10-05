"""Small ASGI middleware for bounded request bodies."""

from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestBodyLimitMiddleware:
    """Reject oversized buffered HTTP requests, including chunked bodies."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        if max_body_bytes < 1:
            raise ValueError("max_body_bytes must be positive")
        self.app = app
        self.max_body_bytes = max_body_bytes

    @staticmethod
    def _error_response(scope: Scope, *, status_code: int, code: str, message: str) -> JSONResponse:
        state = scope.get("state", {})
        return JSONResponse(
            status_code=status_code,
            content={
                "request_id": state.get("request_id", ""),
                "error": {"code": code, "message": message, "details": []},
            },
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {name.lower(): value for name, value in scope.get("headers", ())}
        raw_content_length = headers.get(b"content-length")
        if raw_content_length is not None:
            try:
                content_length = int(raw_content_length)
            except ValueError:
                response = self._error_response(
                    scope,
                    status_code=400,
                    code="invalid_content_length",
                    message="The request content length is invalid.",
                )
                await response(scope, receive, send)
                return
            if content_length < 0:
                response = self._error_response(
                    scope,
                    status_code=400,
                    code="invalid_content_length",
                    message="The request content length is invalid.",
                )
                await response(scope, receive, send)
                return
            if content_length > self.max_body_bytes:
                response = self._error_response(
                    scope,
                    status_code=413,
                    code="request_too_large",
                    message=f"The request body limit is {self.max_body_bytes} bytes.",
                )
                await response(scope, receive, send)
                return

        buffered: list[Message] = []
        bytes_received = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            buffered.append(message)
            if message["type"] != "http.request":
                continue
            bytes_received += len(message.get("body", b""))
            if bytes_received > self.max_body_bytes:
                response = self._error_response(
                    scope,
                    status_code=413,
                    code="request_too_large",
                    message=f"The request body limit is {self.max_body_bytes} bytes.",
                )
                await response(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        async def replay_receive() -> Message:
            if buffered:
                return buffered.pop(0)
            return await receive()

        await self.app(scope, replay_receive, send)
