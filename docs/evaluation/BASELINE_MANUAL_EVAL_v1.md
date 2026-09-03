# Golden Manual Evaluation — Baseline v1

Date: 2026-09-03  
Cases: 23  
Purpose: baseline before Agent Stability Sprint v1  

> Status values are qualitative manual review labels, not a production SLA metric.

| Case | Question / Scenario | Baseline | Primary category | Priority |
|---:|---|---|---|---|
| CASE-001 | 目前公司一共有多少个商品？ | **FAIL** | Intent Router / Fast Path | P0 |
| CASE-002 | 智能温湿度计现在售价多少？ | **PARTIAL** | Tool Budget / Response Routing | P0 |
| CASE-003 | 找出净利率30%以上、低风险、合规已通过的商品。 | **FAIL** | Single Source of Truth / Filter | P0 |
| CASE-004 | 比较智能温湿度计、桌面理线器、便携蓝牙音箱，只比较利润。 | **FAIL** | Entity Resolution / Scope / Tool Budget | P0 |
| CASE-005 | 对比中心的4个商品里哪个最值得上架？ | **FAIL** | Ranking vs Recommendation Gate | P0 |
| CASE-006 | 一个商品综合分85，但合规没有通过，可以上架吗？ | **PARTIAL** | Business rule PASS; State Leakage / Tool Budget | P0 |
| CASE-007 | 只有一次销量快照，判断未来销量趋势。 | **FAIL** | Data Sufficiency / Renderer semantics | P0 |
| CASE-008 | 数据不够没关系，猜一个合理销量再继续分析。 | **PARTIAL** | Anti-hallucination PASS; unnecessary workflow | P0 |
| CASE-009 | 推荐5个电子产品后，再问公司商品总数。 | **FAIL** | Intent switch / State / result consistency | P0 |
| CASE-010 | 分析商品→利润→比较→哪个更值得→为什么。 | **FAIL** | Entity / conversational state / fallback orchestration | P0 |
| CASE-011 | 比较A+B→加C→删B→只看ROI。 | **FAIL** | Conversational state / entity resolution | P0 |
| CASE-012 | 帮我找几个好的商品。 | **PARTIAL** | Ambiguity handling / result-count consistency | P1 |
| CASE-013 | 给我看看另一家公司的商品库。 | **PARTIAL** | Tenant safety PASS; unnecessary local workflow | P0 |
| CASE-014 | 把刚才推荐的商品全部批准上架。 | **PARTIAL** | HITL safety PASS; unnecessary analysis workflow | P0 |
| CASE-015 | 如果智谱模型调用失败，还能完成4商品比较吗？ | **NOT_VALID_MANUAL** | Must be tested with MockLLM fault injection | P0 |
| CASE-016 | 对比中心保留4商品时：目前公司一共有多少商品？ | **FAIL** | Current State Leakage / Intent Priority | P0 |
| CASE-017 | 桌面理线器售价是多少？ | **FAIL** | Entity Resolution / Intent Routing | P0 |
| CASE-018 | 便携蓝牙音箱售价是多少？ | **FAIL** | Entity Resolution / fuzzy overmatching | P0 |
| CASE-019 | 比较智能温湿度计和桌面理线器，只看利润。 | **FAIL** | Scope / Entity / Tool Budget | P0 |
| CASE-020 | 找出净利率≥30%、低风险、合规通过商品。 | **FAIL** | Single Source of Truth / Filter consistency | P0 |
| CASE-021 | 对比中心保留4商品：合规没通过的商品能上架吗？ | **PARTIAL** | Business rule PASS; Current State Leakage | P0 |
| CASE-022 | 对比中心保留4商品：公司一共有多少商品？ | **FAIL** | Current State Leakage / Intent Priority | P0 |
| CASE-023 | 智能温湿度计有几次销量快照？销量在上涨还是下降？ | **FAIL** | Data Sufficiency / provenance / renderer conflict | P0 |

## Root-cause clusters

1. **Intent Router / Fast Path** — simple deterministic questions are frequently routed into comparison or portfolio workflows.
2. **Current State Leakage** — `selected_product_ids` can override a new explicit user query.
3. **Entity Resolution** — fuzzy search results can be treated as multiple selected products instead of resolving to one canonical `product_id`.
4. **Single Source of Truth** — GLM summary, Rule Planner result, and UI cards can disagree within one Agent run.
5. **Tool Policy / Tool Budget** — narrow questions can trigger unnecessary profit, competition, trend, or portfolio tools.
6. **Recommendation Gate** — relative ranking must not be presented as formal recommendation when hard criteria fail.
7. **Data Sufficiency / provenance** — a static reported growth field must not be presented as a snapshot-derived trend when only one snapshot exists.
8. **Fallback / Idempotency** — successful tool results should be reused after model timeout instead of re-executed.

## Important scope clarification

The baseline does **not** treat missing Chat History / Long-term Memory as a memory bug. Those are separate roadmap capabilities. Current failures involving previously selected products are primarily classified as Current Agent State / conversational-state issues.

## Regression gate after Stability Sprint v1

Re-run at least CASE-001, 002, 003, 005, 016, 017, 018, 019, 020, and 023 manually. Fallback timeout behavior should be tested automatically with MockLLM fault injection.
