"""ASGI request limits also bound chunked bodies without Content-Length."""

import asyncio

from isnad_core.api.middleware import RequestBodyLimitMiddleware


def _http_scope() -> dict[str, object]:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/verify",
        "raw_path": b"/v1/verify",
        "query_string": b"",
        "headers": [],
        "server": ("test", 80),
        "client": ("127.0.0.1", 1234),
        "root_path": "",
        "state": {"request_id": "synthetic-request"},
    }


def test_chunked_request_over_limit_is_rejected_before_route() -> None:
    request_messages = [
        {"type": "http.request", "body": b"abc", "more_body": True},
        {"type": "http.request", "body": b"def", "more_body": False},
    ]
    sent_messages: list[dict[str, object]] = []
    route_called = False

    async def receive() -> dict[str, object]:
        if request_messages:
            return request_messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        sent_messages.append(message)

    async def route(_scope, _receive, _send) -> None:
        nonlocal route_called
        route_called = True

    middleware = RequestBodyLimitMiddleware(route, max_body_bytes=5)
    asyncio.run(middleware(_http_scope(), receive, send))

    response_start = next(
        message for message in sent_messages if message["type"] == "http.response.start"
    )
    response_body = next(
        message for message in sent_messages if message["type"] == "http.response.body"
    )
    assert response_start["status"] == 413
    assert b'"request_too_large"' in response_body["body"]
    assert b"synthetic-request" in response_body["body"]
    assert route_called is False


def test_body_at_limit_is_replayed_to_downstream_route() -> None:
    request_messages = [
        {"type": "http.request", "body": b"abc", "more_body": True},
        {"type": "http.request", "body": b"def", "more_body": False},
    ]
    sent_messages: list[dict[str, object]] = []
    received_body = bytearray()

    async def receive() -> dict[str, object]:
        if request_messages:
            return request_messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        sent_messages.append(message)

    async def route(_scope, replay_receive, downstream_send) -> None:
        while True:
            message = await replay_receive()
            received_body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        await downstream_send({"type": "http.response.start", "status": 200, "headers": []})
        await downstream_send({"type": "http.response.body", "body": b"ok"})

    middleware = RequestBodyLimitMiddleware(route, max_body_bytes=6)
    asyncio.run(middleware(_http_scope(), receive, send))

    assert bytes(received_body) == b"abcdef"
    assert sent_messages[0]["status"] == 200
