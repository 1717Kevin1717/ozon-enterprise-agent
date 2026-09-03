# OZON Enterprise Agent — Stability Sprint v1

## Execution requirements

1. First perform a read-only Root Cause Audit and identify involved files/call chains before editing.
2. Fix shared root causes rather than hard-coding the 23 individual case strings.
3. Avoid repeated full-repository scans; use scoped inspection and scoped tests during implementation.
4. Run the full relevant regression suite at the end of the sprint.
5. Do not call the real GLM API for debugging; use MockLLM / local deterministic tests.
6. Do not rewrite the FastAPI + database + Tool Calling architecture.
7. Do not add unrelated product features in this sprint.
8. If a requested change would require a large re-platform/rewrite, stop and report it instead of proceeding.

## Important current state

The current product does not yet have complete Chat History / Conversational Memory / Long-term Memory. Do not misclassify current selected-product leakage as a Long-term Memory bug.

## P0-01 Intent Router / Fast Path

Define explicit intents such as:

- `company_product_count`
- `product_price`
- `product_filter`
- `product_detail`
- `profit_comparison`
- `product_comparison`
- `compliance_policy`
- `selection_recommendation`

Simple deterministic questions should use Fast Path. The current explicit query must have higher priority than stale `selected_product_ids`.

## P0-02 Entity Resolution

Resolve query entities to canonical product identity:

`query entity → candidate lookup → exact/normalized match → unique product_id`

Do not add all fuzzy matches to `selected_product_ids`. Use `product_id` as the internal source of truth after resolution.

## P0-03 Single Source of Truth

Create / enforce one validated structured `AgentRunResult`. GLM summary, frontend rendering, API response, and audit timeline should consume the same validated result instead of independently deriving business truth.

## P0-04 Tool Policy / Tool Budget

Each intent should declare allowed/forbidden tools, tool budget, and requested dimensions. Example: profit-only comparison should not execute competition/trend/portfolio workflows.

Target budgets:

- simple count: ≤1 deterministic read
- simple price: ≤1–2 reads
- two-product profit comparison: only entity resolution + profit calculation required

## P0-05 Current State Leakage

`selected_product_ids` should be used only when the current query actually references the active selection/comparison. A new explicit intent such as company product count must override stale comparison state.

## P0-06 Recommendation Gate

Separate `rank`, `score`, and `decision`.

- compliance failed → `BLOCKED`
- margin below hard threshold → `NOT_RECOMMENDED` / `REVIEW_REQUIRED`
- insufficient evidence → `INSUFFICIENT_DATA`
- high risk → `HUMAN_REVIEW_REQUIRED`

If every candidate fails formal criteria, say that no product reaches the formal recommendation standard even if one is relatively ranked first.

## P0-07 Data Sufficiency / Provenance

Separate `data_completeness` from `data_sufficiency`. One snapshot must produce snapshot trend `INSUFFICIENT_DATA`. A static/external `sales_growth_rate` needs explicit source/provenance and must not be rendered as snapshot-derived trend.

## P0-08 Fallback / Idempotency

Persist completed tool results inside RunState/AgentState with normalized arguments and status. Within one Agent run, the same successful tool + normalized args must not execute again. On model timeout, fallback reuses successful results and executes only missing steps.

Use MockLLM fault injection such as:

```text
Round 1: success
Later round: timeout
```

Assert:

- `fallback_used = true`
- task still completes when deterministic data is already available
- `duplicate_tool_execution = 0`

## Regression scope

At minimum automate:

1. company product count
2. price lookup
3. exact entity resolution
4. profit-only comparison
5. empty-filter-result consistency
6. selected-product state leakage
7. compliance hard gate
8. ranking vs recommendation
9. single-snapshot insufficiency
10. fallback no duplicate execution

## Final report

Return:

- Root Cause
- Changed Files
- Architecture Changes
- New Tests
- PASS / FAIL
- tool-call counts before / after
- Known Limitations

Do not claim Chat History or Long-term Memory unless they are actually implemented and tested. Stop after this sprint and wait for manual regression acceptance.
