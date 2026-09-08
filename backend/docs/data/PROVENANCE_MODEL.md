# Data Trust & Provenance v1

本轮是作品集的数据可信基础，不是数据真实性认证服务。Mock Enterprise Dataset 不等于真实 Ozon 数据。

## 1. 四个独立维度

| 维度 | 回答的问题 | 当前契约 |
| --- | --- | --- |
| Completeness | 关注的字段是否存在？ | `Product.data_trust.completeness`：PRESENT / MISSING / UNKNOWN 的字段存在率 |
| Sufficiency | 是否足以支持本次结论？ | 已有 `AgentRunResult.data_sufficiency`；一次有效销量快照仍为 INSUFFICIENT_DATA |
| Freshness | 观察/采集的数据距今多久？ | 每条 `FieldEvidence.freshness`：FRESH / STALE / UNKNOWN |
| Provenance | 来源是什么，能否追溯输入？ | 每条字段证据的来源、标识、产品/企业绑定及 inputs |

原 `analysis.evidence_completeness` / `analysis.data_completeness` 是历史加权证据就绪口径，包含身份、销量、成本、竞争、合规及 lineage。为保留已通过的评分/筛选/Hard Gate，本轮不改变其公式或数值。不能把旧的 100% 理解为数据新鲜、来源真实或趋势充分；商品详情新增字段存在率及口径说明。

数字 0 不一律等于缺失：明确提供的 0 可以 PRESENT；老记录中的默认 0 且无来源/提供记录时为 UNKNOWN。既不伪造缺失值，也不把数据库默认值当作已采集事实。

## 2. Source Types 与 Provider

| source_type | 含义 | provider 示例 |
| --- | --- | --- |
| mock | 模拟或演示资料 | mock_enterprise_catalog、mock_authorized_seller_report |
| manual | 人工录入声明 | manual_operator |
| imported | 文件导入声明 | csv_import、excel_import |
| marketplace | 平台页面/API来源声明 | ozon |
| enterprise_internal | 企业内部报告 | seller_report |
| derived | 确定性计算结果 | deterministic_evaluation_engine |
| unknown | 无法可靠归类的来源 | unknown，或保留原未知 provider |

`provider` 说明数据提供方/方法，不代表该方已经通过本系统的真实性核验。没有 URL 的人工和内部来源合法。未知字符串不能自动升级成“企业已验证”。

兼容旧 `field_lineage` 字符串/字典与 `currentPriceRub` 等既有别名。明确的 Mock 标记或 mock_/demo_ 来源优先于 marketplace 声明；不得通过改变标签把 Mock 转成真实平台数据。不根据特定商品名或 demo 商品 ID 分类。

## 3. Field-Level Provenance

字段契约在 `app/schemas/provenance.py`，后端构建在 `app/services/provenance.py`。

重要字段：

- `field / value / unit`：字段、值与单位。
- `company_id / product_id`：由已过滤的数据库记录绑定，不采用外部输入的身份。
- `source_type / provider / source_url`：来源声明；平台参考链接不会自动成为事实证据。
- `collected_at`：采集/接收该来源数据的时间。
- `observed_at`：来源所描述的数据观察时间，若存在则优先用于时效判断。
- `updated_at`：该字段证据更新的时间；不能替代观察时间。
- `evidence_id`：根据企业、商品、字段、值、来源和时间生成的内容标识，不是外部证书编号，也不是真实性签名。
- `is_mock / is_derived`：数据性质；Mock 输入会传递到派生结果。
- `presence`：字段存在性。
- `evidence_status`：TRACEABLE / SOURCE_MISSING。可追溯不等于外部已验证。
- `freshness`：独立时效结果及判断时间。
- `derived_from / inputs / calculation`：派生输入名称、带值的完整输入证据、已有算法的计算说明。

覆盖售价、销量、报告增速、评论、评分、竞争、成本、佣金、物流、利润、净利率、ROI、风险与合规；风险输入还包含身份及销量/竞争证据字段。没有默认 freshness 阈值的字段保留 UNKNOWN。

## 4. Derived Data 与 Evidence Chain

利润证据沿用现有公式，不让 LLM 计算或补事实：

`net_profit → current_price / procurement_cost / shipping_cost or fulfillment_cost / commission_rate / platform_fee / advertising / warehousing / tax / return reserve / other`

`net_margin → 同一利润输入 / price`；`ROI → 同一利润输入 / total_cost`。

物流公式原本采用 shipping 优先、否则 fulfillment。本轮同时记录两个输入，公式说明分支，不重复计费。售价为 0、总成本为 0 时仍沿用现有引擎行为。

风险等级附带当前算法版本对应的输入集合及计算说明，不新增另一套风险规则。它是规则计算，不是外部风险认证。

新分析把完整证据保存在现有 `ProductAnalysis.evidence_json.field_provenance`。分析后再编辑价格，旧分析证据仍保留原来的输入价格；重新分析才产生新链。证据时效在读取时重算，不因为重新计算就把陈旧输入变新。

旧分析没有字段级输入凭证时，保留已有业务结果，但旧输入标为未知，输出 LEGACY_ANALYSIS/SOURCE_MISSING 提示；不把当前主档来源倒灌成历史事实。不进行真实数据库回填。某次旧分析与已编辑主档的业务值是否仍适配，仍需重新分析确认。

## 5. Freshness Policy

集中定义：`FRESHNESS_MAX_AGE_DAYS`，属于 Portfolio v1 默认策略，尚未经过真实企业业务 SLA 审批。

| 字段组 | 最大年龄 |
| --- | --- |
| 当前价/常规价/竞品价格 | 7 天 |
| 销量快照、销量/增速、评论/评分、市场与竞争指标 | 30 天 |
| 成本、佣金、人工风险等经营输入 | 30 天 |
| 合规状态、材料与证书 | 90 天 |

边界：年龄等于阈值仍 FRESH；超过则 STALE。无观察/采集时间、未来时间或未定义字段策略为 UNKNOWN。

派生值：任一输入 STALE → STALE；否则任一 UNKNOWN → UNKNOWN；全部 FRESH 才 FRESH。计算时间不能重置输入年龄。

销量快照沿用 Stability 的充分性逻辑，复用集中 30 天阈值。报告中的增长率与系统观察快照趋势分开；本轮不重写趋势算法。

## 6. 写入、兼容与 Mock Disclosure

- 无数据库 migration、无新增表/列；`Product.field_lineage` 继续存来源；`raw_payload._provided_fields` 记录实际提交字段。
- 新快照用 `raw_payload._field_lineage` 保存采集时的来源副本。快照保存时间不自动等于外部原始观察时间。
- 手工修改仅更新值实际改变的字段来源；不刷新无关销量时间。对已有 Mock 商品继续保留 Mock 属性。
- 无新来源的改值不继承旧的字段来源；没有 metadata 的历史记录按 UNKNOWN 处理。
- 新生成的演示数据记录生成/采集时间，意思是“Mock 样本在此时生成”，不是实时市场采集。
- API 商品/分析/历史/经营总览携带来源或披露；Agent 的事实和 evidence.fields 携带后端验证的数据。
- `AgentRunResult.trust_notices` 与业务风险 warnings 分开：价格时效提示不会把一次查价变成完整风险分析。
- Agent 审计轨迹包含本次引用的 evidence_ids；继续使用原 RunState 的结果复用机制。
- 页面保留原导航。顶部及商品列表标记 Mock，Agent 和详情可展开字段来源/时效；平台链接标注为参考，非 Mock 价格凭证。

## 7. 学习与边界

面试时可以这样解释：**数值计算正确，只说明算术正确；还需分别验证输入是否齐全、足以支撑结论、足够新、来源可追溯。** 本轮是在已有决策系统上补证据链，而不是新增一个“数据质量总分”。

重要限制：没有真实 Ozon 连接、没有来源签名/外部证据核验、没有真实 GLM 稳定性结论。租户过滤测试不代表现有客户端角色头已经是安全生产认证。Knowledge 保留文档级来源，本轮未把文档拆成商品级证据，也未扩展 RAG。
