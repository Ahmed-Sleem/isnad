# Integration surfaces

All integrations use the same shared router, evidence model, response schema, status semantics, and streaming gate. They do not implement their own citation matcher.

## MCP server

Install the optional, version-pinned MCP SDK and the project extra:

```bash
python -m pip install 'isnad-core[mcp]'
isnad-mcp
```

`isnad-mcp` starts the official MCP Python SDK server over stdio. The tools are:

- `verify_citation(source_type, language, quote?, reference?)` — returns the same structured verification fields as REST.
- `isnad_capabilities()` — returns supported editions, languages, references, statuses, limits, and streaming contract.

A source outage is returned as a safe MCP tool error that explicitly says it is not a not-found result. Invalid locators and validation errors do not echo the quote. Hadith grade/attribution fields remain separate, and the server does not infer a grader or issue a ruling.

## Agent skill

The installable agent instruction is [`skills/isnad-citation-verification/SKILL.md`](../skills/isnad-citation-verification/SKILL.md). It documents when to call the MCP tool, how to interpret the nine statuses, source-local references, provenance and grade fields, and the exact streaming marker syntax. Add that skill to the agent host's skill directory/configuration; installing the Python package does not automatically modify an agent's local configuration.

## LiteLLM proxy guardrail

Install the optional pinned LiteLLM adapter:

```bash
python -m pip install 'isnad-core[litellm]'
```

Example proxy configuration (`examples/litellm_config.yaml`):

```yaml
model_list:
  - model_name: your-model
    litellm_params:
      model: openai/your-model
      api_key: os.environ/OPENAI_API_KEY

guardrails:
  - guardrail_name: isnad-citation-gate
    litellm_params:
      guardrail: isnad_core.integrations.litellm_guardrail.IsnadCitationGuardrail
      mode: post_call
```

Call the proxy using Chat Completions streaming, `stream: true`, and `guardrails: ["isnad-citation-gate"]`. The hook emits ordinary safe prose as normal text deltas and adds lifecycle/result objects in the non-standard top-level `isnad_event` field on citation-event chunks. A UI must explicitly read this extension; generic OpenAI clients may ignore unknown fields. Source-backed `citation_result` events are emitted only after the shared verifier returns.

The interface served at `/` verifies through the REST endpoint above. It does **not** consume the proxy stream and does not read `isnad_event`; a UI that displays a proxied model answer must read that extension itself, or call `/v1/verify` per citation. Pointing this interface at a LiteLLM proxy only works if the proxy exposes compatible `/v1/capabilities` and `/v1/verify` routes.

The current guardrail intentionally supports one plain-text choice (`n=1`) only. It fails closed for tool/function calls, reasoning-only, audio, image, structured, or other non-text delta channels because those channels do not use the text citation protocol. Non-streaming output containing a citation marker is blocked; use streaming or the REST verification endpoint instead. This optional plugin was unit-tested against the pinned LiteLLM response model; deployments must still verify the exact proxy version/configuration and end-to-end SSE serialization before enabling public traffic.

Consult the upstream [LiteLLM custom guardrail documentation](https://docs.litellm.ai/docs/proxy/guardrails/custom_guardrail) when upgrading the pinned adapter; the version in `pyproject.toml` must be reviewed and tested before changing.

## Model endpoint for the chat surface

The chat surface streams from any OpenAI-compatible `POST {base}/chat/completions` endpoint with `stream: true`, reading `choices[0].delta.content`. Before the first request it fetches `GET /v1/system-prompt` and sends that text as the conversation's system message, so the model marks its quotations in the form the verifier recognises. When a marked block closes, the page posts the quote and the model's own reference to `POST /v1/verify` and holds the text until the source answers.

Configuration lives in the interface (Model settings), not in the deployment: provider preset, base URL, model name, optional key, temperature. Two consequences are worth stating plainly:

- A provider that refuses browser requests (no CORS, key-only server access) cannot be called from the page directly. Put a local proxy in front of it — LiteLLM is one, `examples/litellm_config.yaml` is a starting point — and point the interface at the proxy's OpenAI-compatible base URL.
- The key is kept in the tab's memory unless the user ticks "remember the key on this device", which writes it to this browser's local storage in readable form. A deployment that must not allow that can serve the page without the Model panel; the verification API itself needs no model key.

To exercise the chat path without a provider account, run the scripted test double and point the interface at it:

```bash
python examples/fake_openai_provider.py --port 8123
# Model settings: provider "Other OpenAI-compatible endpoint",
# base URL http://127.0.0.1:8123/v1, model name fake-citation-model
```

It streams a fixed answer containing one quotation the pinned corpus contains and one it does not, in chunks small enough to split the markers. `tests/test_fake_provider_pipeline.py` and `tests/test_gui_browser.py` use the same double, so the chat path, the marker splitting, and the citation cards are covered by the test suite rather than by hand. It is a test double: it is not a model, and its scripted quotations are checked exactly like a real model's output.

## GUI integration recommendation

For a GUI of your own, call `GET /v1/capabilities` at startup, fetch `GET /v1/system-prompt` for the protocol text to inject, use `POST /v1/verify` for discrete requests, and use the `/v1/stream` WebSocket when the server owns the stream and should emit gate events. This keeps source/language availability and normalization rules in the backend. CORS allow-lists are not authentication; deploy behind the application's trusted/authenticated boundary.

The interface shipped here follows that shape: it streams from the user's model endpoint in the browser, checks each marked quotation through `POST /v1/verify`, and renders one source-backed card per citation. It does not use the WebSocket, and it does not read the LiteLLM `isnad_event` extension.

## Upstream MCP SDK

The MCP server uses the official Python SDK with stdio as its local-host transport. See the [SDK server guide](https://py.sdk.modelcontextprotocol.io/v2/get-started/real-host/) for host setup and [SDK testing guide](https://py.sdk.modelcontextprotocol.io/v2/get-started/testing/) for in-memory tests.
