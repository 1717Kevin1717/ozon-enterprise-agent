# Data Trust & Provenance Sprint v1 — Final Report

日期：2026-09-04。项目：`E:\OZON AI 选品`。

起始 HEAD：`338853f1d24cc7e229d25ef08a2fe64480f463ac`；起始 Git 工作区已只读确认干净。用户提供上轮基线：132 passed、Golden 29/29；本轮没有重复运行旧基线全套。

## 1. Audit Findings

| 审计问题 | 实际发现 |
| --- | --- |
| 当前 Product 来源 | `field_lineage`、`sales_source/evidence`、`competition_evidence`、`raw_payload`、`url`、`source_updated_at`、`last_sync_at`、created/updated 时间已经存在。没有统一的 Product.source_type/provider 列。 |
| Evidence/Snapshot | 没有独立 Evidence 表；`ProductAnalysis.evidence_json/input_snapshot` 已存在；`ProductSnapshot.captured_at/raw_payload` 可存采集时证据。 |
| Knowledge | 文档已有 source_type=user_upload、checksum、metadata、时间；Chunk 有文档 ID、metadata、时间；检索返回 document_id/chunk_id/filename/source。仍是文档级来源，不等于字段证据。 |
| 字段级缺口 | 旧 lineage 常见 currentPriceRub/latest30dSales 等少数字段；成本、佣金、物流、报告增长率、风险、合规通常只有商品级来源或固定标签。 |
| Agent Evidence | `agent/tools.py` 从商品视图/确定性分析拼装文本证据，旧 AgentEvidence 只含商品身份、source、summary、url。 |
| Completeness | `decision_engine.evidence_completeness` 是按六组加权的证据就绪率，并非纯字段存在率；本轮保留公式。 |
| Sufficiency | `sales_data_sufficiency` 已区分报告指标与快照趋势，要求至少两条有效销量快照；不重复实现。 |
| Freshness | 只对销量快照存在集中在工具文件里的 30 天检查，缺少通用字段策略。 |
| Mock 识别 | raw_payload.demo_data、mock_/demo_ provider 和样本说明存在，但 Agent 简化后可能丢失；分析还曾把来源固定写为 enterprise_verified/human_compliance_review。 |
| 展示风险 | Ozon 搜索 URL 被标为“来源”；经营总览“已验证月利润潜力”可能使模拟输入被误认为真实市场凭证。 |
| Migration | 不需要。现有 JSON 字段可承载本轮契约。 |

## 2. Architecture

`ProductIn / ProductPatch → 兼容 lineage 归一化 → 现有 Product + Snapshot JSON → 原确定性分析 → 绑定产品/企业的字段证据 → 原 AgentRunResult → 按 requested_dimensions 展示来源 → 原离线 Eval`

新增两个核心文件：一个 Pydantic 来源契约、一个无网络/数据库副作用的来源服务。没有新增 Agent、Tool、Planner、在线 Eval 服务或框架。数值筛选、评分和决策状态继续由原后端负责。

## 3. Changed Files

### Modified（12）

- `backend/app/schemas/agent.py`：既有 Fact/Evidence/Product/RunResult 的附加来源字段、产品绑定校验。
- `backend/app/services/decision_engine.py`：在现有数值计算后附上证据，不改变公式；移除固定的已验证来源断言。
- `backend/app/services/demo_dataset.py`：新生成的 Mock 数据附采集时间；原商品值/ID不变。
- `backend/app/repositories/products.py`：来源归一化、快照证据副本、新分析证据持久化、旧分析保守投影。
- `backend/app/agent/tools.py`：结果按范围引用证据、来源提示、审计证据 ID、复用集中销量新鲜度阈值。
- `backend/app/api/router.py`：分析/快照来源输出与经营总览 Mock 披露。
- `backend/app/web/app.js`：顶部/商品 Mock 标记、来源与时效展开区、参考链接和指标口径说明；导航不变。
- `backend/evals/schemas.py`：在现有 schema 增加 provenance setup/expected/category。
- `backend/evals/fixtures.py`：只在内存 fixture 中设置陈旧/未知/矛盾来源与另一个租户。
- `backend/evals/evaluators.py`：扩展现有 FactGroundingEvaluator，生成 provenance_accuracy 的逐项断言。
- `backend/evals/cases/golden_cases_v1.jsonl`：保留前 29 行，追加 PV01–PV10。
- `backend/tests/test_eval_framework.py`：更新数据集覆盖检查，不放宽原 29 个业务预期。

### New（5）

- `backend/app/schemas/provenance.py`
- `backend/app/services/provenance.py`
- `backend/tests/test_provenance.py`
- `backend/docs/data/PROVENANCE_MODEL.md`
- 本报告。

另外生成两份 Eval 输出，由现有 `backend/evals/reports/.gitignore` 忽略，不修改 ignore 规则：

- `backend/evals/reports/eval_v1_20260904T071809_792030Z.json`
- `backend/evals/reports/eval_v1_20260904T071809_792030Z.md`

## 4. Schema Changes / Compatibility

无 DB migration、无表/列修改、无真实数据回填。重用 `Product.field_lineage`、`Snapshot.raw_payload`、`Analysis.evidence_json`。API 新字段为附加项，原产品、比较、快照数值字段保留。

旧 lineage 字符串、字典及别名继续接受；无法解释的来源保持 UNKNOWN。旧分析没有历史输入凭证时不把现今商品字段倒灌成历史事实。缺少企业/商品身份的轻量快照仍返回旧数值，但不能生成绑定证据。

## 5. Provenance Contract

类型：mock / manual / imported / marketplace / enterprise_internal / derived / unknown。

字段：field、value、unit、company_id、product_id、source_type、provider、source_url、collected_at、observed_at、updated_at、evidence_id、is_mock、is_derived、presence、evidence_status、freshness、derived_from、inputs、calculation、notice。

派生字段必须有完整输入证据和计算说明；输入企业/商品一致；Mock 输入向派生输出传播。来源 URL 可为空。evidence_id 是内容标识，不是签名或外部原始凭证。

## 6. Freshness Policy

集中在 `FRESHNESS_MAX_AGE_DAYS`：价格 7 天，销量/评论/市场及经营成本一般 30 天，合规 90 天。策略未覆盖的字段、无时间或未来时间保持 UNKNOWN。

等于阈值仍 FRESH，超过才 STALE；观察时间优先于采集时间。不能用 Product.updated_at、读操作或重新计算重置数据年龄。派生结果保守继承输入的时效。

新 `data_trust.completeness` 是字段存在率；原加权证据就绪率、已有趋势充分性、新鲜度、来源分别保留，不合成为一个总分。

## 7. Agent Behavior / UI

- 事实、比较与决策引用后端 FieldEvidence，不接收 LLM 编造证据。
- 来源警告放在 `trust_notices`；原业务 warnings 与 Scope Guard 保留，查价不新增竞争/趋势/审核分析。
- 决策报告附来源限制说明，过期或来源不足的输入不能无提示作为当前强结论。
- Reported sales_growth 与 observed snapshot trend 继续分开，一次快照仍 INSUFFICIENT_DATA。
- 新分析保存原输入，改价不覆盖旧分析证据；需要重新分析生成新链。
- 页面显示 Mock、来源、时间、新鲜度，并把平台搜索链接明确标成参考而非凭证。
- 总览指标原 API key 保留兼容；UI 将“已验证月利润潜力”改成“按现有输入测算的月利润潜力”。
- 不增加工具次数。SF01 count=1、SF02 price=1、CO01 profit comparison=3、FB01/FB02=3，重复执行均为 0（来自本轮报告）。

## 8. New Golden Cases

| Case | 校验目标 | 结果 |
| --- | --- | --- |
| PV01 | Mock 价格显式来源、时间与标识 | PASS |
| PV02 | 派生利润追溯价格/成本/佣金/物流等输入 | PASS |
| PV03 | 一次快照仍然不充分 | PASS |
| PV04 | 180 天前价格产生陈旧警告 | PASS |
| PV05 | 1 天前价格不产生陈旧警告 | PASS |
| PV06 | 缺失来源/时间被明确提示 | PASS |
| PV07 | Mock 不能被 marketplace/ozon 声明重新包装成真实数据 | PASS |
| PV08 | 报告增长率 -9.6% 与快照趋势保持区分 | PASS |
| PV09 | 两商品派生证据分别绑定正确商品及企业 | PASS |
| PV10 | 外企业实体不可见，详情/快照/利润工具均拒绝外企业证据 | PASS |

Truth 来源：用户明确的数据可信规则、已人工确认的 Mock 样本与确定性业务约束。没有 LLM-as-a-Judge；没有让 LLM 创建答案作为 Ground Truth。

## 9. Actual Eval Results

全量 Golden 一次：39/39 Case PASS；584/584 assertions PASS。原 29 行与 Git HEAD 逐行比较未变化。新增 provenance_accuracy = 10/10。

| 指标 | Passed / Applicable |
| --- | --- |
| intent_accuracy | 30/30 |
| entity_resolution_accuracy | 29/29 |
| response_type_accuracy | 33/33 |
| response_scope_accuracy | 22/22 |
| tool_scope_accuracy | 31/31 |
| decision_policy_accuracy | 15/15 |
| data_sufficiency_accuracy | 4/4 |
| fact_grounding_accuracy | 29/29 |
| provenance_accuracy | 10/10 |
| state_accuracy | 14/14 |
| fallback_accuracy | 2/2 |
| execution_safety | 39/39 |

分母仅包含实际适用案例；Case 必须所有 required assertions 通过。未生成虚构的 Precision/Recall/F1。

使用：在 backend 中运行 `python -m evals.runner`；可加 `--category provenance` 或 `--case-id PV04`。仍使用原 Runner，每 Case 新进程/内存 SQLite、禁用 dotenv/真实密钥/HTTP。

## 10. Actual pytest Results（保留失败记录）

| 执行 | 真实结果 |
| --- | --- |
| 最初 scoped（沙箱） | 33 passed、4 setup errors；pytest 临时目录权限问题。换目录过程中另一次因父目录不存在失败；未改业务 expected。 |
| 修复运行环境后 scoped | 37 passed、1 warning，6.75 秒（22 provenance + 15 Eval framework）。 |
| 最终来源披露补充后 provenance scoped | 22 passed、1 warning，1.07 秒。 |
| Golden 全量 | 39/39，584/584 assertions。 |
| Stability v1/v2/v3 scoped | 81 passed、1 warning，55.78 秒。 |
| **完整 pytest（严格只执行一次）** | **153 passed、1 failed、1 warning，77.68 秒。** |
| 修复该回归后 scoped | **31 passed、1 warning，1.03 秒（23 provenance + 8 tool registry）。** |

完整测试失败：`test_registered_price_trend_has_a_real_implementation`。根因是新增 snapshot_view 假设所有快照都有 raw_payload/company_id/product_id，而旧数值快照 adapter 没有这些属性。修复为缺元数据时保留原数值和趋势路径、证据为空且提示来源未知，绝不构造假身份。未修改原失败测试；新增通用兼容回归。

**没有修复后全量 pytest PASS 的证据**：遵循用户“full pytest once”，只复测受影响范围。上述 Golden 全量和 Stability 全量范围测试发生在最终轻量快照兼容修复之前；真实 ORM 快照路径由修复后的 provenance scoped 再次覆盖。不能把 153+31 相加伪称为一次全量通过。

唯一 warning 为既有 Starlette TestClient/httpx 弃用提醒。实际前端渲染函数经 Node 执行测试 Mock/过期提示、范围与转义；`node --check app/web/app.js` 通过。

## 11. Hardcode Review

- 原 29 个 Golden case 文本/expected 未修改。
- 新业务来源服务没有测试商品名、demo-ozon ID、固定 3791/3474/3157 或 count=60 的分支。
- Mock 判断基于数据标识与来源命名约定，不基于某个商品身份。
- 正常 fixture/JSONL 的具体值属于测试输入，不是业务放行代码。
- 没有修改 Prompt/LLM 答案，没有在前端隐藏错误业务结果。
- 新服务无真实 HTTP、无 broad exception 吞错、无无限 retry、无跨企业缓存。
- `git diff --check` 通过；代码变更范围保持在上述文件。

## 12. Data / Secret Safety

没有读取/修改 .env 内容、API Key 或真实业务 DB，没有 migration/回填/删除数据。没有真实 GLM/Ozon 调用。未 commit/push。

Eval 沿用安全 worker：只允许内存 SQLite 引擎，BaseSettings 强制 `_env_file=None` / `_secrets_dir=None`，环境白名单与空密钥，拦截外部 HTTP。原 Eval 安全测试也验证伪生产 DB 文件不变。

所有 pytest 通过进程环境禁用 LLM、置空 key、指定内存 DB，并在导入应用前屏蔽 dotenv。完整测试额外将知识库服务 ROOT 重定向到本轮唯一临时目录，防止上传测试写入 backend/data。测试权限提升仅解决 Windows 临时目录 ACL 问题，不扩大到真实数据。

## 13. Known Limitations / Stop

- Mock Dataset ≠ real Ozon data。39 个已知案例通过只证明相应确定性约束，不是全面真实数据质量结论。
- 没有真实 GLM 的稳定性、延迟、token/成本结论。
- 来源由录入方声明，未联网验证、签名或验证证书；可追溯不等于真实。旧客户端角色请求头仍是现有认证限制，本轮不扩展 RBAC。
- 历史数据可能没有原始采集时间；UNKNOWN 是诚实结果，不自动补写当前时间。
- 旧分析输入未知，或分析后改过主档时，应重新分析。不会自动重算/改写真实业务数据。
- 时效阈值是集中默认策略，并非企业批准的 SLA；派生时效采用保守传播。
- 现有加权完整度仍包含合规/证据要求；新的字段存在率独立展示，不宣称重做了全部旧口径。
- 风险证据列出既有算法输入范围，不是独立外部认证或全公式形式化证明；复杂历史因子仍沿用原算法。
- Knowledge 仍是原文档级来源，本轮没有拓展文档字段级证据/RAG。
- 没有实时页面人工验收；已有实际 renderer 自动回归通过。
- 最终快照兼容补丁仅 scoped 复测，未再次跑完整 pytest。

本 Sprint 到此停止，等待人工审阅。没有自动开始 Memory、Chat History、Multi-Agent、MCP、抓取或新功能开发。
