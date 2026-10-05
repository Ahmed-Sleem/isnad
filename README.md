# Isnad Core

Source-grounded verification for Qur'an and hadith citations, in Arabic and English.

Give it a quotation and the reference you were handed. It looks the wording up in a pinned edition and answers with one of nine documented **textual correspondence** statuses, the source wording verbatim, and the provenance of the edition that supplied it. It never invents a grade, a grader, or a religious ruling, and a source that cannot be reached is reported as exactly that — not as an absence from the corpus.

The repository contains the verifier, a REST and WebSocket API, an MCP server, a LiteLLM guardrail adapter, an agent skill, and a self-contained browser interface that chats with a model of your choice while checking every quotation the model marks before you see it.

## What it is not

- Not a fatwa or grading service. Match status answers "does this wording appear at this reference in this edition?". It says nothing about a hadith's authenticity.
- Not comprehensive. The pinned HadeethEnc edition is a curated selection, not all hadith; a not-found result means "not in the edition that was searched".
- Not a substitute for scholarship, nor for an ijāzah-bearing teacher, in any matter of practice.

## Statuses

`exact_match`, `normalized_match`, `partial_match`, `mismatch_at_cited_reference`, `quote_found_wrong_reference`, `reference_found_without_quote`, `not_found_in_checked_corpus`, `ambiguous_multiple_matches`, `unsupported_source_or_language`.

`GET /v1/capabilities` is the authoritative list, with the limits and edition metadata in force for a given deployment. `docs/api-contract.md` documents the fields.

## Sources

| Edition | Language | Role |
| --- | --- | --- |
| Tanzil Uthmani 1.1 | `ar` | Qur'an, pinned locally with a checksum |
| QuranEnc `english_saheeh` 1.1.2 | `en` | Qur'an translation, pinned locally with a checksum |
| HadeethEnc official API v1 | `ar`, `en` | Hadith, fetched from the official remote API |

Local editions are pinned by SHA-256 and verified on load; the hadith edition is remote, so its availability is checked at request time and a failure is reported as a source outage. Provenance, version, licence, and coverage notes travel with every result. See `NOTICE.md`.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
python -m pip install -e '.[dev]'

uvicorn isnad_core.api.app:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/> for the interface, <http://127.0.0.1:8000/docs> for the OpenAPI page, and try the API directly:

```bash
curl -s http://127.0.0.1:8000/v1/verify \
  -H 'content-type: application/json' \
  -d '{"source_type":"quran","language":"ar","quote":"بِسْمِ ٱللَّهِ ٱلرَّحْمَـٰنِ ٱلرَّحِيمِ","reference":"1:1"}'
```

Run the checks the project is expected to pass — lint, formatting, the full test suite, the packaging check, and the secret scan:

```bash
bash scripts/verify.sh
```

## Using the interface

1. Start the API (above) and open `/`.
2. **Settings → Model**: choose a provider preset or "Other OpenAI-compatible endpoint", set the base URL and model name, and paste a key if the provider needs one. The key stays in the tab's memory unless you tick "remember the key on this device", which writes it to this browser's local storage in readable form.
3. Chat normally. Prose streams as it arrives. When the model marks a quotation with the citation protocol, the marked block is held, checked against the pinned source, and then shown as a card with the source wording, the reference, the status, and the provenance. Ordinary answers, and every part of an answer that is not a marked quotation, stream untouched.
4. **Verify a quote** in the composer switches to the model-free surface: paste a quotation, a reference, or both, and read the same report without any model involved.

The protocol text is served by the API at `GET /v1/system-prompt` and injected as the system message, so the model and the checker cannot drift apart about the markers. "Citation protocol prompt" in any reply's menu shows the exact text in force.

A page opened from disk (`isnad-gui.html`, downloadable from `/gui/standalone.html`) behaves the same way; point it at an API base URL in Settings. Set `ISNAD_CORS_ORIGINS` to the origins you allow, and `ISNAD_GUI_FRAME_ANCESTORS` if the page is embedded in a frame.

### A model endpoint you can test against

Providers that block browser requests need a local proxy; LiteLLM is one (`docs/integrations.md`, `examples/litellm_config.yaml`). To exercise the chat path with no account at all, run the scripted double and point the interface at it:

```bash
python examples/fake_openai_provider.py --port 8123
```

It streams a fixed answer with one quotation the corpus contains and one it does not, in chunks small enough to split the citation markers.

## Other integration surfaces

- **MCP**: `python -m pip install 'isnad-core[mcp]'` then `isnad-mcp` (stdio). Tools: `verify_citation`, `isnad_capabilities`.
- **LiteLLM guardrail**: `python -m pip install 'isnad-core[litellm]'`; the streaming hook withholds marked blocks until they are checked and reports results in an `isnad_event` extension field.
- **Agent skill**: `skills/isnad-citation-verification/SKILL.md`.
- **Streaming gate**: `WS /v1/stream` for hosts that want the server to own the gate. `WS /v1/stream` and `POST /v1/verify` share one implementation.

`docs/integrations.md` covers all four, including what each one does **not** cover.

## Documentation

- `docs/api-contract.md` — endpoints, fields, errors, statuses, limits, CORS and WebSocket origins.
- `docs/integrations.md` — MCP, LiteLLM, agent skill, model endpoints, and the GUI's contract.
- `docs/evaluation.md` — what the synthetic evaluation measures, and what it does not.
- `evaluation/reports/` — the reports it produces.

## Honest limits

- The evaluation harness is a controlled same-source check of normalization and matching behaviour, not field evidence. It does not measure real-world citation accuracy.
- The LiteLLM adapter's extension field is unit-tested against the pinned response model; end-to-end SSE serialization through a running proxy is a deployment step, not something this repository has verified.
- HadeethEnc reachability is observed at request time, not guaranteed by tests.
- The bundled interface is a reference client. Its security posture is documented above and in `docs/integrations.md`; deploy it behind your own authentication boundary, and account for the model API key handling when you do.

## Licence and attribution

Code: see `LICENSE`. Source editions keep their own terms — Tanzil, QuranEnc, and HadeethEnc attribution, versions, and licence notes are recorded in `NOTICE.md` and returned with every result.
