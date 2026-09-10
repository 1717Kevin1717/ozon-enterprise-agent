# Offline Provider Readiness & ACT8 Evaluation Hardening Sprint v1

Date: 2026-09-09
Baseline commit: `6fff200`
Scope: offline provider/router/context/evaluation hardening only

## 1. Provider Contract

`ModelProvider` remains the single provider interface. Qwen, DeepSeek and legacy Zhipu return the same `ProviderCallResult`; capabilities cover text, multimodal, structured output, reasoning and tool planning. Provider failures use sanitized codes and never expose credentials or Authorization headers.

## 2. Model Router

The router preserves deterministic fast paths, sends semantic/context tasks to Qwen, and permits DeepSeek reasoning only when the validated SemanticFrame sets `requires_reasoning=true`. A semantic request has at most one fallback and cannot loop between providers. Ordinary queries do not chain Qwen and DeepSeek.

## 3. SemanticFrame

The existing `QueryUnderstanding` contract was extended rather than duplicated. It carries intent, task type, entity spans, reference slots, requested dimensions, metric, constraints, negative scope, preferences, comparison/tool/reasoning/clarification flags, confidence and semantic provider. It cannot carry authoritative price, profit, ROI, sales, risk, compliance or recommendation facts.

## 4. Context Sanitization

The Qwen packet serializer sends task metadata and anonymous slot structure only. Session, tenant, company, user, internal/external product IDs and SKU are excluded. Identifiers appearing in an authorized query are replaced before crossing the provider boundary. Sanitization tests use generated identifiers rather than one demo ID.

## 5. DeepSeek Reasoning Data Policy

Supported modes are `disabled`, `mock_only` and `anonymized`. The packet uses P1/P2/P3 labels and only Backend-validated fields. `disabled` prevents the call; `mock_only` admits only Mock products. DeepSeek remains an explanation layer and cannot overwrite Backend facts or gates.

## 6. Failure / Fallback

The failure contract distinguishes `RATE_LIMITED`, `NETWORK_TIMEOUT`, `AUTH_FAILED`, `MODEL_NOT_FOUND`, `MODEL_UNAVAILABLE`, `INVALID_RESPONSE`, `SCHEMA_VALIDATION_FAILED` and `PROVIDER_INTERNAL_ERROR`. Fault-injection covers HTTP mapping, malformed/empty/invalid structured output, unavailable providers and timeout fallback. Qwen can use at most one compatible semantic fallback; DeepSeek reasoning failure retains the deterministic Backend result.

## 7. Provider Status

Backend status exposes provider capability matrix, `configured`/`not_configured`, `disabled`, `ready_for_mock`, `configured_unverified`, `real_smoke_verified` and `legacy` metadata. Key presence is not treated as health. Qwen and DeepSeek keys are not configured for this Sprint; their real health is therefore unknown.

## 8. ACT8 Offline Golden

15 new cases passed: natural-language generalization 3/3, contextual follow-up 6/6 and model routing 6/6. All ran with in-memory SQLite, static SemanticFrame fixtures and blocked network access. These results describe the Mock Enterprise Dataset, not real Ozon data.

## 9. Paraphrase Robustness

The added Golden cases vary price, profit, calculation, entity-switch and UI-selection wording. A regression in colloquial follow-up routing was fixed generically: contextual utterances no longer become premature catalogue NOT_FOUND results, while explicit product facts retain the deterministic path. Mock providers return case-supplied frames and do not implement an answer dictionary or a second regex NLP engine.

## 10. Model Router Eval

Deterministic evaluation covers canonical facts, colloquial semantics, follow-up, complex reasoning after tools, policy fast path, missing context and safe provider failure. Final Full Golden metric: model routing 15/15 assertions passed.

## 11. Gate Override Tests

Mock DeepSeek output cannot convert compliance-rejected products to recommended, insufficient snapshot evidence to confirmed trend, or high risk to low risk. Invalid claims are rejected with `SCHEMA_VALIDATION_FAILED`; the deterministic answer and decision status remain authoritative.

## 12. Multimodal Contract

`ModelInput` supports text plus future image/video references. Qwen advertises multimodal capability; DeepSeek does not. Router tests prevent multimodal input from selecting DeepSeek. No upload UI, OCR, image analysis business flow or real image token processing was implemented.

## 13. Full Golden

Single full run: 94/94 cases passed; 1,385/1,385 assertions passed.

- Original deterministic/provenance Golden: 39/39
- Semantic Intelligence Golden: 20/20
- Context Golden: 20/20
- Natural language + contextual follow-up Golden: 9/9
- Model routing Golden: 6/6
- Full total: 94/94

Machine report: `backend/evals/reports/eval_v1_20260909T001637_524775Z.json`
Markdown report: `backend/evals/reports/eval_v1_20260909T001637_524775Z.md`

## 14. Stability

Stability v1/v2/v3 and Provenance were run together: 104 tests passed. Existing count/price fast paths, filter AND semantics, entity outcomes, explicit-query priority, gates, data sufficiency, tool reuse and tenant boundaries remained covered.

## 15. Provenance

`tests/test_provenance.py` passed as part of the 104-test scoped run. Backend remains the Source of Truth for facts and provenance. Completeness, sufficiency, freshness, provenance and recommendation remain separate concepts.

## 16. Full pytest

The single allowed full run produced `257 passed, 1 failed, 1 warning`. The failure was an obsolete test allowlist that did not yet accept the new truthful `configured_unverified` status. The allowlist was updated and the exact failed test then passed (`1 passed, 1 warning`). Per the Sprint constraint, Full pytest was not run a second time. The warning is Starlette's `httpx` TestClient deprecation notice.

Additional scoped results: provider/router/status 33 passed; the earlier context/semantic/eval group produced 115 passes before its one metadata-coverage assertion was corrected; the corrected assertion passed in subsequent scope.

## 17. Hardcode Review

Application code was scanned for ACT8/Golden full phrases, forbidden colloquial strings, demo product-specific branches and fixed `3791`/`3474`/`3157`/`count=60` routing values. No matches were found in Agent/Web routing logic. The only equality match found was the legitimate generic normalized-title comparison. Golden and fixtures retain Mock ground truth values by design.

`node --check app/web/app.js` passed. `git diff --check` passed; only Windows LF-to-CRLF notices were emitted.

## 18. Real API Call Count

**0.** No real Qwen, DeepSeek or Zhipu call was made. Evaluation blocks HTTP and clears provider credentials before application imports.

## 19. Changed Files

Provider/runtime work spans `app/agent/providers/`, `model_router.py`, `model_agent.py`, context/query/intent/entity/tool integration, schemas, config, status API and the compact provider display in `app/web/app.js`. Evaluation changes span the existing Golden JSONL, schemas, fixtures, evaluators and provider/router/context tests. No database migration was added and `.env` was not modified.

## 20. Git Status

The working tree remains intentionally dirty with the pre-existing Context/Semantic/Provider work plus this Sprint's tests and documentation. No reset, restore, clean, stash, commit or push was performed. Final file list is reported by `git status --short` in the handoff.

## 21. Tomorrow Smoke Runbook

See `backend/docs/agent/REAL_PROVIDER_SMOKE_RUNBOOK.md`. It limits Qwen to 6 authorized Smoke calls and DeepSeek to 4, starts DeepSeek in `mock_only`, stops repeated requests on provider errors, and requires ACT8 v2 plus final regression after the real Smoke.

## 22. Known Limitations

- Qwen key is not configured; DeepSeek key is not configured; Real Smoke has not been executed.
- Mock PASS does not imply real model availability, latency, quota behavior or semantic accuracy.
- ContextSnapshot is short-term session context, not Long-term Memory or complete Chat History.
- Backend is the business Source of Truth; DeepSeek cannot override Compliance or Recommendation gates.
- Multimodal work is contract-only.
- Semantic evaluation cannot prove arbitrary natural-language understanding is 100% accurate.
- Provider status cannot truthfully become verified until an authorized real call is recorded.
- The final full suite was not rerun after the isolated status-test allowlist fix; only that failing test was rerun and passed.
- This Sprint does not claim Production Ready.
