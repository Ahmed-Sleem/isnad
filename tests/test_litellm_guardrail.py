"""The LiteLLM proxy hook streams only safe prose plus typed Isnad events."""

import pytest

from isnad_core.engine import VerificationEngine
from isnad_core.quran import QuranCorpus


def _litellm_types_and_guardrail():
    pytest.importorskip("litellm")
    from litellm.types.utils import Delta, ModelResponseStream, StreamingChoices

    from isnad_core.integrations.litellm_guardrail import IsnadCitationGuardrail

    return Delta, ModelResponseStream, StreamingChoices, IsnadCitationGuardrail


def _chunk_factory():
    Delta, ModelResponseStream, StreamingChoices, _ = _litellm_types_and_guardrail()

    def create(content: str | None = None, *, finish_reason: str | None = None):
        return ModelResponseStream(
            id="chatcmpl-test",
            created=1,
            model="isnad-test",
            choices=[
                StreamingChoices(
                    index=0,
                    delta=Delta(content=content),
                    finish_reason=finish_reason,
                )
            ],
        )

    return create


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_litellm_hook_withholds_quote_and_emits_source_result_extension() -> None:
    _, ModelResponseStream, _, Guardrail = _litellm_types_and_guardrail()
    create_chunk = _chunk_factory()
    engine = VerificationEngine()
    guardrail = Guardrail(engine=engine)
    source_text = QuranCorpus.load_default().verses_by_reference["1:1"].text
    marker = (
        f"[[ISNAD-CITATION source=quran language=ar reference=1:1]]{source_text}[[/ISNAD-CITATION]]"
    )

    async def upstream():
        yield create_chunk("Normal prose. ")
        yield create_chunk(marker)
        yield create_chunk(" Continued prose.", finish_reason="stop")

    try:
        output = [
            item
            async for item in guardrail.async_post_call_streaming_iterator_hook(
                None, upstream(), {"stream": True}
            )
        ]
    finally:
        engine.close()

    assert all(isinstance(item, ModelResponseStream) for item in output)
    text = "".join(item.choices[0].delta.content or "" for item in output)
    assert text == "Normal prose.  Continued prose."
    event_chunks = [
        item.model_dump(exclude_none=True)["isnad_event"]
        for item in output
        if "isnad_event" in item.model_dump(exclude_none=True)
    ]
    assert [item["type"] for item in event_chunks] == [
        "citation_checking",
        "citation_result",
    ]
    assert event_chunks[0]["placeholder"] == "checking_citation"
    assert event_chunks[1]["result"]["status"] == "exact_match"
    assert event_chunks[1]["result"]["evidence"][0]["source_text"] == source_text
    assert output[-1].choices[0].finish_reason == "stop"
    assert all(item.choices[0].finish_reason is None for item in output[:-1])


@pytest.mark.anyio
async def test_litellm_guardrail_blocks_unchecked_reasoning_channels() -> None:
    Delta, ModelResponseStream, StreamingChoices, Guardrail = _litellm_types_and_guardrail()
    engine = VerificationEngine()
    guardrail = Guardrail(engine=engine)
    untrusted = ModelResponseStream(
        id="chatcmpl-test",
        created=1,
        model="isnad-test",
        choices=[
            StreamingChoices(
                index=0,
                delta=Delta(reasoning_content="hidden quote must not pass"),
            )
        ],
    )

    async def upstream():
        yield untrusted

    try:
        with pytest.raises(RuntimeError, match="plain text only"):
            async for _ in guardrail.async_post_call_streaming_iterator_hook(
                None, upstream(), {"stream": True}
            ):
                pytest.fail("The unsupported reasoning channel must not be emitted.")
    finally:
        engine.close()


@pytest.mark.anyio
async def test_litellm_guardrail_fails_closed_for_nonstream_citations_and_n_gt_one() -> None:
    _, _, _, Guardrail = _litellm_types_and_guardrail()
    engine = VerificationEngine()
    guardrail = Guardrail(engine=engine)
    marker = "[[ISNAD-CITATION"
    try:
        with pytest.raises(RuntimeError, match="requires streaming"):
            await guardrail.apply_guardrail(
                {"texts": [f"unverified quote {marker}"]},
                {},
                "response",
            )
        with pytest.raises(RuntimeError, match="n=1"):
            await guardrail.async_pre_call_hook(None, None, {"stream": True, "n": 2}, "completion")
        with pytest.raises(RuntimeError, match="tool calls"):
            await guardrail.async_pre_call_hook(
                None, None, {"stream": True, "tools": [{"type": "function"}]}, "completion"
            )
        with pytest.raises(RuntimeError, match="Chat Completions only"):
            await guardrail.async_pre_call_hook(None, None, {"stream": True}, "text_completion")
    finally:
        engine.close()
