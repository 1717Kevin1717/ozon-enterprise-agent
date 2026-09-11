# TaskState Follow-up & Regression Closure Sprint v1 — Checkpoint

## Stable base and scope

- Stable remote base commit: `423590d`.
- This closure extends the existing FastAPI agent runtime and existing conversation `state_json`; it adds no database migration, framework, ScenarioState, UI redesign, Multi-Agent, MCP, or RAG.
- No commit or push was performed. The dirty working tree is the expected Sprint checkpoint.
- Enterprise facts, calculations, filtering, ranking, recommendation gates, provenance, permissions, and tenant scope remain backend-owned.

## Closed systemic roots

### ResultSet analytical follow-ups

- Added typed `ARGMAX`, `ARGMIN`, `REORDER`, and `EXPLAIN_RANKING` operations over the session-scoped `ResultSetState`.
- Ranking keys are normalized and executed deterministically by the backend. The provider may interpret an operation but cannot choose the winning product or invent metric values.
- ResultSet identity and order remain session-scoped; explicit product queries retain priority over stored state.

### Profit calculation and cost evidence

- Profit, ROI, and calculation follow-ups now execute `calculate_profit` whenever any compatible calculation dimension is requested.
- ROI explanations use the backend-verified equation and inputs. Missing calculation evidence is reported as unavailable instead of being rendered as zero.
- Cost-pressure answers use the backend `calculation_evidence.cost_breakdown`, rank amounts deterministically, and show the top three items.
- Provider labels such as cost, costs, cost analysis, cost components, cost structure, and cost breakdown normalize into the finite calculation contract. A specific `cost_breakdown` metric takes precedence over a provider's overly broad `product_detail` label before scope validation.

### RecommendationState binding

- Added typed, session-scoped `RecommendationState` for the chosen candidate, ordered candidates, score/gate status, requested dimensions, evidence fields, freshness summary, and review status.
- Recommendation discovery no longer requires a pre-existing product referent. A successful discovery returns a decision report and binds its backend-selected target.
- Follow-ups for recommendation reason, provenance, and freshness resolve against that bound target. Explicit product queries override it, and new sessions do not inherit it.
- Technical semantic failures remain distinct from user clarification; no stale recommendation is injected into a new session.

## Real Qwen acceptance

Policy was `QWEN_ONLY`. This closure used `12 / 12` authorized Qwen calls. DeepSeek calls: `0`. Zhipu calls: `0`. All runtime data used an isolated in-memory SQLite database; no business database was modified.

- ResultSet chain: real PASS. Filter creation, profit maximum, two ranking mutations, and first-versus-second explanation retained the same backend ResultSet and deterministic facts.
- ROI explanation: real PASS. The runtime executed `get_product` and `calculate_profit`; displayed net profit, cost total, and ROI were mutually consistent.
- Cost Top 3: real PASS on a general paraphrase, returning procurement cost, platform commission, and advertising cost in backend amount order. The exact initial wording exposed two SEM004 compatibility failures; the final metric-first arbitration fix is protected offline but was not re-called after the 12-call budget was exhausted.
- Recommendation discovery: real PASS after the response-scope fix. It returned `decision_report`, no clarification code, and persisted `RecommendationState`.
- Recommendation reason, evidence source, and freshness: real PASS through deterministic backend follow-ups.
- New-session recommendation reference: real PASS as clarification with no product, no tool call, and no cross-session state leakage.
- One real Qwen request was manually interrupted after an extended no-response wait. No automatic cross-provider fallback was used.

## Verification

- Focused calculation and semantic contract tests: `83 passed, 0 failed, 1 warning`.
- Combined scoped regression: `304 passed, 0 failed, 1 warning`.
- First full pytest run: `389 passed, 1 failed, 1 warning`; the single failure was a recommendation follow-up signal incorrectly routing an explicit-product decision question to the semantic planner.
- Related fix regression: `38 passed, 0 failed, 1 warning`.
- Final full pytest: `390 passed, 0 failed, 1 warning`.
- Full Golden Eval: `94 / 94` cases PASS.
- Evaluator assertions: `1385 / 1385` PASS.
- Existing Golden expected values changed: `NO`.
- New Golden cases added: `0`.
- Runtime full-query hardcode review: no ACT full-query branches or fixed-answer routing were found. Test queries remain confined to tests.
- `git diff --check`: PASS at closure time.

## Known limitations

1. The exact cost-Top-3 wording was not re-sent to Qwen after the final generic metric-first normalization because the authorized real-call budget was exhausted; its provider-shape variants are covered by deterministic semantic-contract tests.
2. Recommendation provenance is accurate but verbose because it intentionally exposes multiple backend field sources; UI presentation was outside this closure scope.
3. `ScenarioState` remains unimplemented by design. Existing single-price simulation is unchanged.
4. The Mock Enterprise Dataset is not live Ozon data. Passing deterministic, Golden, and limited real-provider acceptance does not prove arbitrary-language accuracy or production readiness.
5. The only automated warning is the existing Starlette `TestClient`/httpx deprecation warning.

## Continuation point

The TaskState follow-up roots are closed in code and automated regression. Before a future commit, the user may optionally perform one manual or authorized real retest of the exact cost-Top-3 wording; no architecture change is indicated by current evidence.
