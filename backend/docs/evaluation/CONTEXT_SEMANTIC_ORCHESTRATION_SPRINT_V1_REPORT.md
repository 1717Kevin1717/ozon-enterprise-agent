# Conversation Context & Semantic Orchestration Sprint v1 Report

## 1. Root Cause

The Agent persisted one overloaded `last_product_ids` list and used scattered lexical reference checks. It could not distinguish explicit query entities, current UI selection, resolved entities, comparison order or generic policy language. Unknown or contextual language therefore reached catalogue entity resolution too early or fell into clarification before a semantic/context route could use the available state.

## 2. Architecture Before / After

Before:

`query + selected_product_ids -> intent/entity heuristics -> last_product_ids fallback -> tools`

After:

`authenticated session + current UI state -> ContextSnapshot -> query/entity spans -> typed Reference Resolver -> three-level route -> deterministic tools -> validated AgentRunResult -> typed context update`

The Backend remains the Source of Truth. The context stores identities, roles, ordering and tool-result references, not price/profit/risk/compliance facts.

## 3. Changed Files

- `app/schemas/agent.py`
- `app/schemas/products.py`
- `app/agent/context.py` (new)
- `app/agent/query_understanding.py`
- `app/agent/intent_engine.py`
- `app/agent/entity_resolution.py`
- `app/agent/tools.py`
- `app/agent/zhipu_agent.py`
- `app/api/router.py`
- `app/web/app.js`
- `evals/schemas.py`
- `evals/evaluators.py`
- `evals/cases/golden_cases_v1.jsonl`
- `tests/test_conversation_context.py` (new)
- `tests/test_eval_framework.py`
- `docs/agent/CONVERSATION_CONTEXT.md` (new)
- `docs/evaluation/CONTEXT_SEMANTIC_ORCHESTRATION_SPRINT_V1_REPORT.md` (new)

Generated Eval artifacts under `evals/reports/` are also present in the working tree according to repository ignore rules/status.

## 4. DB Migration

No. Existing `ConversationSession.state_json` stores the typed context payload. No table, column or migration was added, and no real database was modified.

## 5. ContextSnapshot Schema

The schema records session/turn identity, verified current Selection, selection revision/binding, last explicit/resolved products, previous intent/response/dimensions/metric, stable comparison order and successful tool-result references. IDs are revalidated against the current tenant catalogue every turn. Invalid IDs become context warnings and are excluded.

## 6. Session Isolation

Session reads and message retrieval now require both `company_id` and `user_id`. A new frontend Agent session clears `lastAgentResult` and the Agent-selection binding while preserving the global Compare workspace. The first turn of a new session cannot consume a stale singular Selection as implicit context.

## 7. Selection Binding

UI Selection is used for explicit selection language (“这几个”, “这些商品”, “用对比中心”, “现在比较一下”). Explicit product names always override Selection. One deterministic compatibility rule permits exactly one selected product for an explicit price-scenario simulation. The response adds typed `context_source` while retaining the older `selection_source` API field.

## 8. Reference Resolution

The resolver supports singular pronouns, elliptical metric questions, entity replacement, plural Selection, ordinals, previous comparison references and single-choice continuations. Ordinals bind to saved comparison input order, not UI sorting. Generic policy subjects are removed before Product Resolution.

## 9. Semantic Router

High-confidence count/price/risk/filter/policy questions retain deterministic Fast Path. Recognized contextual tasks can execute deterministically when no semantic provider is available. Semantically complex comparison/preference questions can route to the Semantic Planner. Clarification is reserved for missing Selection, missing prior comparison, invalid ordinal, ambiguous entity or missing referent.

## 10. LLM Context Packet

`SemanticContextPacket` is implemented and used by the controlled semantic-planner boundary and Mock tests. It contains task metadata, allowed tools and verified reference roles. This Sprint did not newly transmit session-derived product IDs/names to the external GLM endpoint because that would expand organizational-data egress without a separately approved external payload contract. The existing external adapter still receives the user query; no real GLM call was made during this Sprint.

## 11. Grounded Synthesis

Context and LLM planning may choose entities, dimensions and allowed tools. Product existence, profit, ROI, risk, compliance, provenance, permissions and final gate status remain deterministic Backend outputs. Selection recommendation reuses the existing Compliance and Recommendation gates; LLM output cannot re-include a blocked product as formally recommended.

## 12. Added Golden Cases

20 cases were added:

- `conversation_context`: 15
- `semantic_orchestration`: 5

They cover ellipsis, pronouns, calculation explanation, replacement entities, UI Selection, ordinals, explicit-query priority, new-session behavior, generic policy questions, semantic comparison, preference planning and grounded single-choice continuation. Existing 59 Golden expected values were not edited.

Mock Enterprise Dataset != real Ozon data. Golden ground truth comes from ACT7 observations plus deterministic context/policy rules, not LLM-as-a-Judge.

## 13. Context Eval Results

Scoped Context tests: **18 passed**.

New Golden results: **20/20 passed**.

Selected metrics from the full run:

- `context_resolution_accuracy`: 19/19
- `reference_resolution_accuracy`: 20/20
- `selection_binding_accuracy`: 3/3
- `session_isolation_accuracy`: 1/1
- `semantic_route_accuracy`: 22/22 eligible cases
- `contextual_scope_accuracy`: 7/7

## 14. Full Golden Result

Command: `python -X utf8 -m evals.runner`

- Cases: **79/79 passed**
- Assertions: **1237/1237 passed**
- Original Golden: **59/59 remained passing**
- New Context/Semantic Golden: **20/20 passed**
- Real GLM: not used
- Production DB / `.env`: not used by isolated workers

Full report artifacts:

- `evals/reports/eval_v1_20260908T072839_706102Z.json`
- `evals/reports/eval_v1_20260908T072839_706102Z.md`

## 15. Stability Result

Initial Stability v1/v2/v3 run: **80 passed, 1 failed**. The failure was the existing one-product Selection price-simulation behavior. A narrowly scoped generic rule restored “one selected product + explicit price scenario”; the failed test then passed **1/1**.

The later full pytest run covered 81 Stability tests before the subsequent historical-comparison reference fix. Count, Price, Filter AND, entity status, Comparison Scope, Compliance Gate, Ranking vs Recommendation, Data Sufficiency and tool idempotency were otherwise passing.

## 16. Provenance Result

`tests/test_provenance.py`: **23 passed, 1 Starlette deprecation warning**.

No provenance rule, freshness threshold or Mock disclosure source was changed.

## 17. Full pytest Result

The required single full run produced:

- **213 passed**
- **1 failed**
- **1 warning**

The sole failure was the existing follow-up “和刚才那两个相比”, where the comparison order had been persisted but its plural historical-reference form was not mapped to `LAST_COMPARISON`. The generic plural comparison-reference rule was added, and the exact failed test then passed **1/1** in scoped verification.

Per the Sprint instruction to run full pytest only once and avoid repeated full-suite consumption, the entire suite was not run a second time. Therefore this report does not claim a post-fix full-suite PASS.

## 18. Real GLM Usage

None. Context tests used the disabled provider; Semantic Planner tests and Golden cases used Mock planners. No Zhipu API Key, `.env`, real Ozon request or external network call was used for regression.

## 19. Tool Call / Idempotency

Existing `AgentRunState` was retained. Context resolution happens before tool execution. Full Golden `fallback_accuracy` was 4/4 and `tool_scope_accuracy` was 71/71 eligible cases. Existing duplicate-tool and result-reuse tests remained passing in the Stability run; the context changes did not introduce a second tool cache.

## 20. Hardcode Review

Application behavior uses grammar/reference classes, ContextSnapshot state and deterministic policies. No ACT7/CTX complete-query equality branch, test-product PASS branch, fixed 3791/3474/3157 price route or fixed count=60 route was added. Specific values and queries appear only in tests/Golden fixtures.

## 21. Known Limitations

- No Long-term Memory and no complete Chat History product.
- No Multi-Agent, MCP or new RAG.
- Context is session-scoped and intentionally short-term.
- The real external GLM path does not yet receive session-derived product identity context; a data-egress/privacy contract is required first.
- Some semantic recognition remains grammar-assisted; 20/20 Context Golden does not imply arbitrary-language 100% accuracy.
- UI Compare workspace remains browser-memory state and is not a durable cross-device selection service.
- Full pytest was not repeated after the final scoped fix; only the previously failing test was rerun successfully.
- One upstream Starlette/httpx deprecation warning remains.
- Mock Dataset != real Ozon data.
- The project is not claimed to be Production Ready.

## 22. Git Status

No commit or push was performed. Final modified/new files are reported from the final `git status` output after static checks.
