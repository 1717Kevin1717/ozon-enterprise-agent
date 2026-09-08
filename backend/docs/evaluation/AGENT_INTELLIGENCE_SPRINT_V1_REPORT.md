# Agent Intelligence Sprint v1 Report

## 1 Root Cause

The previous deterministic Agent was stable but treated intent, product-name text, question clauses and metric values as one string-processing problem. The generic `product_detail` fallback was marked as a confident fast path, so an unsupported sentence could be sent to the product resolver and become a false `NOT_FOUND`. Provenance, calculation and decision-explanation questions shared the broad product-detail renderer. Existing conversation state kept recent product IDs but not the referenced fact dimension.

During final regression, the first low-confidence guard also rejected the generic decision form “某商品是否值得测试”. This was fixed by recognizing decision/detail verbs as a general query class. The exact failed sentence and product names were not hardcoded.

## 2 Final Architecture Changes

The established architecture remains intact. A bounded understanding layer now precedes product resolution:

`Query -> QueryUnderstanding -> confidence route -> tenant entity resolution -> validated tool policy -> deterministic backend -> scoped AgentRunResult -> renderer`

`QueryUnderstanding` records intent, question type, real entity spans, references, mentioned metrics/values, requested dimensions, semantic flags, confidence, route, planned tools, planner-call count, reference field and safe failure code. It is a plan and cannot represent price, margin, risk, compliance or approval facts.

## 3 Changed Files

Agent Intelligence Sprint v1 changed or added:

- `backend/app/agent/query_understanding.py` (new)
- `backend/app/agent/semantic_response.py` (new)
- `backend/app/agent/entity_resolution.py`
- `backend/app/agent/intent_engine.py`
- `backend/app/agent/tools.py`
- `backend/app/agent/zhipu_agent.py`
- `backend/app/schemas/agent.py`
- `backend/app/web/app.js`
- `backend/evals/cases/golden_cases_v1.jsonl`
- `backend/evals/evaluators.py`
- `backend/evals/fixtures.py`
- `backend/evals/runner.py`
- `backend/evals/schemas.py`
- `backend/tests/test_agent_intelligence.py` (new)
- `backend/tests/test_eval_framework.py`
- `backend/docs/agent/QUERY_UNDERSTANDING.md` (new)
- `backend/docs/evaluation/AGENT_INTELLIGENCE_SPRINT_V1_REPORT.md` (new)

The working tree also contains preserved, uncommitted Data Trust sprint files and two unrelated root untracked files. They were not deleted or presented as Intelligence changes.

## 4 Database Migration

No schema migration, database backfill or real business-data mutation was performed. Short-term reference metadata reuses the existing `ConversationSession.state_json` field.

## 5 Query Understanding Contract

The strict Pydantic contract is attached to `AgentRunResult.understanding`. Mentioned percentages are user input, not verified facts. Query route values are `DETERMINISTIC_FAST_PATH`, `SEMANTIC_PLANNER` and `CLARIFICATION`. Confidence is a routing heuristic, not a calibrated probability or evidence score.

## 6 Intent and Confidence Routing

Clear facts, provenance questions, calculations, decision explanations, freshness checks and data-quality policy questions use deterministic routes. The former generic detail fallback is marked unrecognized. Unsupported language may use one semantic-planning request; an unavailable or invalid planner returns a safe clarification rather than a false missing product. Generic “是否值得/是否推荐/能否上架” wording is recognized as a decision/detail request, preserving the compliance-gate path.

## 7 Entity Span Extraction

The catalogue protects exact names, IDs and SKUs before applying grammatical boundaries. Question clauses such as “为什么/怎么算/来自哪里” and percentage metrics are separated from product mentions. Legitimate digits in 65W, 4件装 and model identifiers remain intact. NOT_FOUND still requires a real extracted mention and zero tenant candidates; AMBIGUOUS and LOW_CONFIDENCE retain separate typed outcomes.

## 8 Short-term Reference Resolution

Reference priority is current explicit ID/name, current explicit UI selection, current user's current-tenant session entity, then clarification. `这个价格` binds to `current_price`; `这个指标` can reuse the last fact dimension. The session is checked by both company and user. Policy/clarification turns do not erase the last resolved product. This is bounded session state, not Long-term Memory or a complete Chat History system.

## 9 Semantic Planner Boundary

The semantic request may classify intent, spans, references, dimensions and allowed tool names. The backend rejects malformed output, unknown intents, unexpected fields, illegal dimensions/tools, invented spans and missing identity/reference plans. Tool arguments and product IDs are built only after tenant resolution. Mock tests cover valid output, malformed JSON, unknown intent, illegal tool, missing/ambiguous entity, timeout, repeated tool requests, unavailable planner and backend error.

No real GLM request was made in this Sprint. The semantic tests use controlled Mock planners. Therefore this report makes no real GLM availability, latency, cost or stability claim.

## 10 Response Scope

Provenance, calculation, decision explanation and freshness now have compact response types and a dedicated renderer branch. Provenance exposes saved source/provider/time/freshness/mock disclosures; calculation exposes the stored formula and input evidence; decision explanations consume existing gate status, risks and missing evidence; freshness shows FRESH/STALE/UNKNOWN without treating completeness as freshness. The renderer does not construct business truth.

## 11 Semantic Golden Cases

20 semantic-understanding Golden cases were appended. The original 39 JSONL rows and expected values were byte-for-byte retained as the prefix. Final semantic results: **20/20 cases PASS; 445/445 assertions PASS**. Rule fast-path cases were 18/18; Mock semantic-fallback cases were 2/2. These metrics cover the specified grammar and paraphrases, not arbitrary natural language.

## 12 Full Golden Result

Final isolated run on 2026-09-07: **59/59 cases PASS; 1029/1029 assertions PASS**. Key semantic metrics were intent understanding 20/20, entity span 20/20, semantic route 20/20, response scope 42/42 across the full suite, reference resolution 4/4, and tool scope 51/51. Report artifacts:

- `backend/evals/reports/eval_v1_20260907T065728_096474Z.json`
- `backend/evals/reports/eval_v1_20260907T065728_096474Z.md`

## 13 Stability and Provenance Results

- Final targeted fix: 1/1 PASS for the previously failing compliance/“是否值得测试” regression.
- Intelligence and Eval framework: 56 passed, 1 warning.
- Stability v1/v2/v3: 81 passed, 1 warning.
- Provenance: 23 passed, 1 warning.

The warning is the existing Starlette `TestClient` / httpx deprecation warning, not a test failure.

## 14 Full pytest Result

The one requested final full run completed with **196 passed, 1 warning, 0 failed** in 125.90 seconds. It used an in-memory SQLite database, disabled LLM credentials, prevented dotenv loading and redirected knowledge-test writes to a unique temporary directory.

## 15 Real GLM Calls

None. Semantic planning scenarios were exercised with Mock implementations. The Golden workers blocked external HTTP and loaded no real key.

## 16 Tool Calls and Idempotency

Provenance questions use one validated product read; calculation explanations use a product read plus deterministic profit calculation; the sales metric/snapshot distinction uses two reads; generic data-quality policy uses zero tools; selected-set freshness uses one product read per selected product. Semantic planning is limited to one request. Repeated planned tool names are deduplicated, and existing `AgentRunState` reuse remains protected. Full Golden tool-scope and fallback assertions passed, including `duplicate_tool_execution = 0` scenarios.

## 17 Hardcode Review

Application code was searched for ACT5/AI case IDs, complete test questions, test product names, fixed 3791/3474/3157 values and `count=60` routing logic. No matches were found in `backend/app`. Concrete fixture values remain only where allowed in tests/Golden data. `node --check app/web/app.js` and `git diff --check` passed; line-ending conversion warnings are informational.

## 18 Known Limitations

- No Long-term Memory, complete Chat History, Multi-Agent, MCP, new RAG, database migration or UI redesign was added.
- LLM only understands/plans; Backend tools, the decision engine and provenance remain the business Source of Truth.
- Mock Dataset != real Ozon data. Mock/provider declarations do not prove a real Ozon observation.
- Semantic Eval success does not mean arbitrary natural language is 100% accurate. The rule grammar and the semantic contract remain bounded.
- The catalogue resolver retains its existing per-tenant list limit. Confidence is not statistically calibrated.
- No real GLM or browser manual acceptance was run. This Sprint is not claimed Production Ready.
- Existing authentication limitations and the Starlette/httpx deprecation warning are outside this Sprint.

## 19 Git Status

The worktree is intentionally dirty and no commit/push was performed. Intelligence changes, preserved Data Trust changes, documentation/tests and two pre-existing unrelated untracked root files remain for manual review. See the final `git status --short` supplied with handoff.
