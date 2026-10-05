"""LiteLLM proxy guardrail that filters citations through the shared stream gate.

Install the optional adapter with ``pip install 'isnad-core[litellm]'`` and configure
``isnad_core.integrations.litellm_guardrail.IsnadCitationGuardrail`` in LiteLLM's
custom-guardrail registry. The stream extension field is ``isnad_event``; clients that
need event-aware rendering should use Chat Completions streaming and read that field.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncGenerator, AsyncIterator, Iterable, Mapping
from typing import Any

try:
    from litellm.integrations.custom_guardrail import CustomGuardrail
except ImportError as exc:  # pragma: no cover - depends on optional extra
    raise RuntimeError(
        "The LiteLLM adapter requires the optional dependency: pip install 'isnad-core[litellm]'."
    ) from exc

from isnad_core.api.serializers import verification_response
from isnad_core.engine import VerificationEngine
from isnad_core.streaming import (
    MAX_STREAM_CHARACTERS,
    StreamEvent,
    StreamEventType,
    StreamingCitationGate,
)

_STOP = object()


class IsnadCitationGuardrail(CustomGuardrail):
    """Withhold marked citation blocks in LiteLLM chat streams until core verification."""

    def __init__(self, *, engine: VerificationEngine | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._engine = engine if engine is not None else VerificationEngine()

    async def apply_guardrail(
        self,
        inputs: dict[str, Any],
        request_data: dict[str, Any],
        input_type: str,
        logging_obj: Any = None,
    ) -> dict[str, Any]:
        """Pass normal text through but fail closed on marked citations in non-stream calls."""

        del request_data, logging_obj
        if input_type == "response":
            texts = inputs.get("texts") or []
            if inputs.get("tool_calls"):
                raise RuntimeError(
                    "Isnad post-call gating supports plain text, not tool-call output."
                )
            if any(_contains_citation_marker(text) for text in texts if isinstance(text, str)):
                raise RuntimeError(
                    "Citation-marked output requires streaming mode so the block can be "
                    "withheld and verified before display."
                )
        return inputs

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict[str, Any],
        call_type: Any,
    ) -> dict[str, Any]:
        """Reject multi-choice streams, which need one independent gate per choice."""

        del user_api_key_dict, cache
        if call_type not in {None, "completion"}:
            raise RuntimeError("Isnad citation gating supports Chat Completions only.")
        if data.get("stream") and data.get("n", 1) != 1:
            raise RuntimeError(
                "Isnad citation gating supports one streamed completion choice (n=1)."
            )
        if data.get("stream") and (data.get("tools") or data.get("tool_choice")):
            raise RuntimeError("Isnad citation gating supports plain-text streams, not tool calls.")
        return data

    async def async_post_call_streaming_iterator_hook(
        self,
        user_api_key_dict: Any,
        response: AsyncIterator[Any],
        request_data: dict[str, Any],
    ) -> AsyncGenerator[Any, None]:
        """Emit safe text deltas and citation lifecycle events as LiteLLM chunks."""

        del user_api_key_dict, request_data
        gate = StreamingCitationGate(self._engine)
        total_characters = 0
        last_chunk: Any | None = None
        finished = False

        async for chunk in response:
            last_chunk = chunk
            content = _chunk_content(chunk)
            finish_reason = _chunk_finish_reason(chunk)

            emitted_event = False
            if content is not None:
                total_characters += len(content)
                if total_characters > MAX_STREAM_CHARACTERS:
                    raise RuntimeError(
                        "Isnad gated stream exceeded its one-million-character limit."
                    )
                async for event in _async_events(gate.feed(content)):
                    emitted_event = True
                    yield _chunk_for_event(chunk, event)

            if finish_reason is not None:
                async for event in _async_events(gate.finish()):
                    yield _chunk_for_event(chunk, event)
                finished = True
                yield _copy_chunk(chunk, text=None, finish_reason=finish_reason)
            elif content is None or (content == "" and not emitted_event):
                yield chunk

        if not finished and last_chunk is not None:
            async for event in _async_events(gate.finish()):
                yield _chunk_for_event(last_chunk, event)


def _contains_citation_marker(text: str) -> bool:
    folded = text.casefold()
    return "[[isnad-citation" in folded or "[[/isnad-citation" in folded


def _chunk_content(chunk: Any) -> str | None:
    choices = _value(chunk, "choices")
    if not choices:
        raise RuntimeError("Isnad citation gating supports Chat Completions chunks only.")
    if len(choices) != 1:
        raise RuntimeError("Isnad citation gating requires exactly one streamed choice.")
    delta = _value(choices[0], "delta")
    if delta is None:
        raise RuntimeError("Isnad citation gating requires a text-delta chat stream.")
    if any(
        _value(delta, field)
        for field in (
            "tool_calls",
            "function_call",
            "reasoning_content",
            "thinking_blocks",
            "annotations",
            "audio",
            "images",
        )
    ):
        raise RuntimeError(
            "Isnad citation gating accepts plain text only; tool, reasoning, audio, "
            "image, and structured-output channels are blocked."
        )
    content = _value(delta, "content") if delta is not None else None
    if content is not None and not isinstance(content, str):
        raise RuntimeError(
            "Isnad citation gating only supports string chat-completion deltas; "
            "non-text output was blocked."
        )
    return content


def _chunk_finish_reason(chunk: Any) -> str | None:
    choices = _value(chunk, "choices")
    if not choices:
        return None
    if len(choices) != 1:
        raise RuntimeError("Isnad citation gating requires exactly one streamed choice.")
    return _value(choices[0], "finish_reason")


def _value(item: Any, key: str) -> Any:
    return item.get(key) if isinstance(item, Mapping) else getattr(item, key, None)


def _async_events(events: Iterable[StreamEvent]) -> AsyncIterator[StreamEvent]:
    async def iterate() -> AsyncGenerator[StreamEvent, None]:
        iterator = iter(events)
        while True:
            event = await asyncio.to_thread(_next_event, iterator)
            if event is _STOP:
                return
            yield event

    return iterate()


def _next_event(events: Any) -> StreamEvent | object:
    try:
        return next(events)
    except StopIteration:
        return _STOP


def _chunk_for_event(chunk: Any, event: StreamEvent) -> Any:
    if event.type is StreamEventType.TEXT:
        return _copy_chunk(chunk, text=event.text)

    event_payload: dict[str, Any] = {"type": event.type.value}
    if event.citation_id is not None:
        event_payload["citation_id"] = event.citation_id
    if event.placeholder is not None:
        event_payload["placeholder"] = event.placeholder
    if event.code is not None:
        event_payload["code"] = event.code
    if event.result is not None:
        event_payload["result"] = verification_response(event.result).model_dump(mode="json")
    return _copy_chunk(chunk, text=None, event=event_payload)


def _copy_chunk(
    chunk: Any,
    *,
    text: str | None,
    event: dict[str, Any] | None = None,
    finish_reason: str | None = None,
) -> Any:
    copied = copy.deepcopy(chunk)
    if isinstance(copied, dict):
        choices = copied.get("choices") or []
        for choice in choices:
            delta = choice.get("delta")
            if isinstance(delta, dict):
                delta["content"] = text
            choice["finish_reason"] = finish_reason
        if event is not None:
            copied["isnad_event"] = event
        return copied

    choices = getattr(copied, "choices", None) or []
    for choice in choices:
        delta = getattr(choice, "delta", None)
        if delta is not None and hasattr(delta, "content"):
            delta.content = text
        choice.finish_reason = finish_reason
    if event is not None:
        try:
            copied.isnad_event = event
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "This LiteLLM response model cannot serialize the required isnad_event "
                "extension; use the Isnad WebSocket stream contract instead."
            ) from exc
        if hasattr(copied, "model_dump") and "isnad_event" not in copied.model_dump():
            raise RuntimeError(
                "LiteLLM dropped the isnad_event extension; refusing to stream a citation block."
            )
    return copied
