# Dual-Model Provider & Semantic Routing Sprint v1

## 1. Outcome

The existing FastAPI, deterministic tool registry, product repository and `AgentRunResult` contract were retained. A provider adapter and router now support:

- Qwen as the primary natural-language semantic interpreter.
- DeepSeek as an optional reasoning layer for complex, multi-product decisions.
- Zhipu as a legacy text fallback.
- Deterministic Backend tools as the only source of product facts, calculations and decision gates.

No database migration, new agent framework, Multi-Agent, MCP, Memory or RAG capability was added.

## 2. Routing Boundary

| Route | Responsibility | Business facts |
| --- | --- | --- |
| Deterministic Fast Path | Count, explicit price/risk, structured filters and policy questions | Read/calculated by Backend |
| Qwen Semantic | Intent, entity spans, references, requested dimensions, negative scope and preference order | Forbidden |
| DeepSeek Reasoning | Optional explanation over validated, pseudonymized Mock facts | Cannot replace Backend answer or gates |
| Clarification | Used when required information remains missing or no semantic provider can safely resolve the request | None |

The provider response is validated as `QueryUnderstanding`. Extra fields such as model-generated price or other business facts are rejected. The validated plan may select only tools allowed by the Backend intent policy.

## 3. Provider Adapter

`ModelProvider` exposes provider name, model, configuration status and capabilities. Qwen, DeepSeek and Zhipu use a shared OpenAI-compatible transport adapter while retaining independent capability declarations.

The following safe failure codes are preserved across the provider boundary:

- `AUTH_FAILED`
- `RATE_LIMITED`
- `NETWORK_TIMEOUT`
- `MODEL_UNAVAILABLE`
- `PROVIDER_UNAVAILABLE`
- `INVALID_RESPONSE`
- `SCHEMA_VALIDATION_FAILED`
- `PROVIDER_ERROR`

There is no unbounded retry. Qwen technical/schema failure may attempt the next configured text provider once. A successful secondary-provider result records `fallback_used=true` and the primary failure code.

## 4. Data Egress Contract

The Qwen semantic request contains only the current user query and anonymous context slots. Tenant ID, session ID, internal product ID, saved product title and API key are excluded from the context payload. Known product IDs, external IDs and SKUs appearing in a query are replaced with local placeholders before transmission and restored only inside the Backend.

DeepSeek is disabled for business data by default. Its configurable modes are:

- `disabled`
- `mock_only`
- `anonymized`

Under `mock_only`, it receives P1/P2-style labels plus validated numeric/status fields. It receives no tenant, session, product ID or product title. Its output is a separate `model_summary`; it cannot replace `answer`, `decision_status`, Compliance Gate, Recommendation Gate or Human Review.

Multimodal support is declared only as a provider capability boundary. This Sprint did not implement image upload, OCR or multimodal UI.

## 5. Context and Scope Fixes

Two common routing defects were fixed:

1. An unparsed natural-language scope phrase attached to a valid product name no longer triggers pre-semantic low-confidence clarification. The semantic provider first extracts exact entity spans.
2. The word “排除” no longer automatically drops UI Selection. Selection is ignored only when the user explicitly says to ignore/exclude the current or selected set.

Negative scope and preference order remain structured fields and are persisted only as short-term session routing context, not business facts.

## 6. UI and Observability

The source badge reports the actual provider used. Provider status and governance UI no longer hardcode “智谱”; they display the active provider/model and safe failure reason. A deterministic answer still identifies itself as the rule/deterministic engine.

## 7. Tests Actually Executed

| Scope | Result |
| --- | --- |
| Provider adapters + model router | 25 passed, 1 existing warning |
| Semantic handoff + context + intelligence + model router | 82 passed, 1 existing warning |
| Conversation context + Stability v1/v2/v3 + Provenance | 122 passed, 1 existing warning |
| Existing provider status/dashboard focused tests | 2 passed, 1 existing warning |
| JavaScript syntax + changed Python module AST parse | PASS |

The warning is the existing Starlette `TestClient` / `httpx` deprecation warning.

## 8. Real Provider Smoke Status

No real Qwen, DeepSeek or Zhipu request was made in this Sprint.

At verification time:

- Qwen key configured: false
- DeepSeek key configured: false
- Runtime selector: `zhipu`
- External reasoning data mode: `disabled`

Therefore no real availability, latency, token, cost, semantic quality or rate-limit conclusion is claimed. Full Golden and Full pytest were intentionally not run because the Sprint order required real Smoke first when provider keys are available.

## 9. Configuration Handoff

`.env.example` documents the new variables without secrets. To activate the new path later, the operator must explicitly set local `.env` values such as `LLM_PROVIDER=dual`, a Qwen key, and optionally a DeepSeek key. `EXTERNAL_REASONING_DATA_MODE` should remain `disabled` unless an approved Mock/anonymized data policy is intentionally enabled.

The real `.env`, API keys and business database were not modified.

## 10. Known Limitations

- Real provider behavior has not been verified because Qwen/DeepSeek keys are absent.
- DeepSeek reasoning is advisory and currently limited to validated Mock/anonymized packets.
- Multimodal behavior is only an interface contract.
- Semantic schemas constrain model output but do not imply arbitrary-language 100% accuracy.
- Mock Dataset is not real Ozon data.
- This Sprint does not claim Production Ready.
- Full Golden and Full pytest remain pending until the approved real Smoke can be run.

## 11. Git Boundary

No commit or push was performed. The working tree also contains the previously completed, uncommitted Conversation Context / Semantic Orchestration changes; those were preserved rather than rewritten.
