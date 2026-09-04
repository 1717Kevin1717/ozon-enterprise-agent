# OZON Enterprise Agent — Stability Sprint v2 Report

## 1. Background

本 Sprint 基于 Stability Sprint v1 和 2026-09-03 的人工 Golden Case 记录收尾，不新增产品功能，不调用真实 GLM，不修改 `.env`、真实数据库、数据库备份或 API Key。目标是修复筛选、响应范围、趋势口径、推荐政策和异常回退的一致性问题。

## 2. Baseline Problems

- 筛选问题中的“风险低”“合规状态通过”没有被旧解析器识别，导致后台实际只执行净利率条件，界面出现 medium/high risk 商品。
- `AgentRunResult` 已存在，但缺少 `run_id`、`response_type`、实体 ID、工具结果、告警和统一决策状态等可验证来源字段。
- 所有 Agent 响应共用“企业选品决策报告”Renderer，数量、价格和政策问答被附加未请求的推荐、趋势、风险和人工审核信息。
- 快照工具已经区分外部报告增速与快照观察趋势，但 Agent 丢失部分字段并无条件拼接历史分析风险，形成“一次快照却声称下降”的冲突。
- Recommendation Gate 已能阻止不合格商品，但通用“排名第一是否等于推荐”问题仍会被识别成商品比较并受页面选择状态影响。
- 成功工具结果已经可以复用；未知工具和参数拒绝没有完整进入 RunState 审计，部分异常响应分类不精确。

## 3. Root Causes

1. Intent Router 使用少量固定词序识别风险与合规条件，没有将解析结果立即交给现有工具输入 Schema 验证。
2. 后端响应没有显式表达业务响应类型和显示范围，前端只能套用单一大模板。
3. 风险、证据和缺失数据在 Renderer 前被无条件聚合，没有按 `requested_dimensions` 收敛。
4. `sales_growth_rate`（外部/人工报告指标）和 snapshot trend（系统观测结果）虽来源不同，但没有共同的充分性契约。
5. 排名政策缺少零工具 Fast Path；旧 comparison 关键词优先捕获“这4个”。

## 4. Fixes

- 复用 `FilterProductsInput` 作为唯一 FilterCriteria，补齐“风险低/中/高”和“合规状态通过”等通用词序，所有字段进入确定性工具前先由 Pydantic 校验。
- `filter_products` 对所有非空条件逐项 AND，返回同一次执行产生的 `criteria/total_count/matched_count/displayed_count/product_ids`，无结果时保持 0，不放宽条件。
- 扩展 `AgentRunResult`：补齐 `run_id`、`response_type`、`entities/product_ids`、`tool_results`、`decision_status`、`warnings`、`human_review_required`、`display_scope` 和结构化 `data_sufficiency`。
- 将 Gate 状态计算集中到 `recommendation_gate_status`；分数、相对排名、正式推荐和最终决策保持独立。
- 新增 `recommendation_policy` 零工具 Fast Path；显式规则问题覆盖 selected product state。
- 新增销量充分性函数：至少 2 个有效且最近一次不超过 30 天的快照才可形成 observed trend；reported metric 始终保留独立来源。
- Agent 按 response type 和 requested dimensions 生成证据、告警、缺失项与动作；简单事实不再携带人工审核或推荐风险。
- 前端根据后端 `response_type/display_scope` 分别渲染 simple fact、policy、insufficient data、filter、comparison 和 decision report；利润对比不展示未请求的趋势、竞争和合规内容，只有合规阻断可作为硬 Gate 提示。
- 未知工具、无权限工具和非法参数写入 RunState，但不计为真实工具执行；成功结果继续幂等复用。

## 5. Changed Architecture

调用链保持原架构：

`User Query → Intent Router → Entity Resolution → Current State → Planner → Tool Registry/Pydantic → Deterministic Tool → AgentRunResult v2 → Renderer → Audit Trace`

关键变化是把业务事实收敛到两个既有中心：

- 筛选事实：`FilterProductsInput` + `filter_products` 返回值。
- 响应事实：经过 Pydantic 验证的 `AgentRunResult`。

没有新增数据库表、框架、消息队列、Multi-Agent、MCP、长期记忆或完整 Chat History。

## 6. New Regression Tests

新增 `tests/test_stability_sprint_v2.py`，共 30 个测试节点：

- FL01–FL07：单条件筛选、三条件 AND、0 结果、排序、计数/行一致性。
- D01–D07：0/1/足量快照、reported metric 分离、Renderer 口径、过期快照、无有效销量。
- RP01–RP07：ranking 不等于 recommendation、合规硬 Gate、证据不足、高风险、全部未通过、“必须推荐”不可绕过、selected state 不污染政策问答。
- 统一响应契约、简单事实 scope、利润对比 scope、前端 response type 分支。
- MockLLM：malformed output、unknown tool、tool timeout、重复成功调用、backend tool error。
- 当前演示数据价格 Ground Truth：桌面理线器 3791 RUB；便携蓝牙音箱 4件装 3157 RUB。

## 7. Case Results

| Case | 自动回归结果 | 核心断言 |
|---|---|---|
| CASE01 商品总数 | PASS | 60，`simple_fact`，1 tool，不受 selected IDs 影响 |
| CASE02 桌面理线器售价 | PASS | 唯一实体，3791 RUB，`simple_fact`，无推荐/趋势告警 |
| CASE03 便携蓝牙音箱售价 | PASS | 唯一实体“便携蓝牙音箱 4件装”，3157 RUB |
| CASE04 只比较利润 | PASS | 2 商品，3 tools，仅 profit/ROI scope，无趋势下降告警 |
| CASE05 三条件筛选 | PASS | 每个结果同时满足 margin >= 30%、risk=low、compliance=approved |
| CASE06 selected state 后查询总数 | PASS | 60，1 tool，selection source=none |
| CASE07 合规失败能否上架 | PASS | `BLOCKED`，0 tools |
| CASE08 第一名是否代表推荐 | PASS | `recommendation_policy`，0 tools，明确回答 NO |
| CASE09 一次销量快照 | PASS | snapshot_count=1，observed trend=`INSUFFICIENT_DATA`，无无来源下降结论 |

## 8. Tool Call Changes

| 场景 | 人工基线 | Sprint v2 |
|---|---:|---:|
| 商品总数 | 1 | 1 |
| 单商品价格 | 1 | 1 |
| 利润双商品对比 | 3 | 3 |
| 三条件筛选 | 1 | 1 |
| 合规政策 | 0 | 0 |
| 排名/推荐通用政策（存在已选商品） | 1 或错误进入比较 | 0 |
| 单商品销量快照核验 | 2 | 2 |
| GLM 成功工具后超时回退 | 3 个实际业务工具；compare 复用 | 3 个实际业务工具；重复执行 0 |

本 Sprint 的目标不是一味减少调用数量，而是确保每次调用符合 Intent Policy，且相同 tool + normalized args 成功后不重复执行。

## 9. Remaining Limitations

- 中文条件解析仍是受控规则解析，不是完整自然语言查询语言；明确 OR 组合尚未实现，本 Sprint 只保证所有已识别显式条件严格 AND。
- 快照 freshness 使用与“近 30 天销量”口径一致的 30 天运行规则，未来若企业另有 SLA 应改成配置项。
- 本轮只使用 MockLLM 验证异常和工具编排，没有验证真实 GLM 网络、额度或模型行为。
- 前端完成脚本语法检查和后端契约回归；仍需在正式运行服务中做 CASE01–CASE09 浏览器人工验收。
- 当前项目目录没有 `.git` 元数据，无法生成真实 `git status`；只能提供预期 modified/new 文件清单。

## 10. Known Risks

- 旧数据库中保存的历史分析文案可能仍含旧措辞；Agent 响应已按新来源规则清洗，但商品详情页若直接展示旧 analysis JSON，仍需未来通过受控重分析或数据迁移处理。本 Sprint 按要求未修改真实数据库。
- `zhipu_agent.py` 保留一个顶层 provider 边界 `except Exception`，用于统一脱敏并安全回退；内部工具参数、输出和超时仍分别校验，不会向用户暴露 traceback。
- Tool cache 生命周期仅限单次 Agent Run，不存在跨 tenant 复用；如未来引入持久缓存，必须把 company_id 加入 key。

## 11. Next Recommended Sprint

先由人工运行 CASE01–CASE09 验收正式服务的视觉范围和实际数据。只有发现回归时才进入下一次稳定性修复；在本轮验收通过前，不建议扩展 Memory、Multi-Agent、MCP、RAG 或数据库架构。

## Verified Commands

- Existing v1 stability scoped tests: `10 passed`.
- New v2 stability scoped tests: `30 passed`.
- Adjacent tool/planner/decision/compare/workspace scoped tests: `29 passed`.
- Frontend JavaScript syntax: PASS (`node --check`).
- Full suite: `76 passed, 1 warning in 12.96s` using `pytest tests -q -p no:cacheprovider`.
- Warning: existing Starlette `TestClient` deprecation notice only.

