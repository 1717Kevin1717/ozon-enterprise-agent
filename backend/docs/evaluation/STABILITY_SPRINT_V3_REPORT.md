# Stability Sprint v3 — Entity Resolution & Scope Guard

日期：2026-09-04。状态：代码已同步 E 盘，等待人工 ACT3 回归；未 commit / push。

## 1. 输入与边界

- 人工依据：`C:/Users/zzh/Desktop/9.4 ACT3.docx`，20 个 Case 的文字已完整读取。文档转图因环境缺少 LibreOffice 不可用，未声称完成文档版面验收。
- 沿用当前 FastAPI、数据库、Tool Registry、AgentRunState、AgentRunResult 和前端，不新增框架、Memory、Multi-Agent、MCP、RAG 或 RBAC。
- 测试使用内存 SQLite；外部模型禁用或由 Mock 替代。未访问真实 GLM。
- 同步和完整测试前后，正式项目 `.env` 与 `backend/data` 下文件的 SHA-256 相同；未写业务数据库、数据库备份或密钥。
- 本次开始时 E 盘 Git 工作树干净；修改前逐个核对 staging 与正式文件哈希，防止覆盖其他工作。

## 2. 只读 Audit / Root Cause

| 根因 | 涉及位置 | 修复方式 |
| --- | --- | --- |
| 查询整体匹配，没有逐实体结果；零候选和歧义共用提示 | entity_resolution.py、tools.py | 分离实体提取与候选排序，每个名称保留类型化结果 |
| 简称只能依赖完整标题，模糊匹配仅在整次查询没有命中时执行 | entity_resolution.py | ID / 完整名 / 规范化 / 唯一名称后缀 / 保守模糊匹配逐级解析 |
| 已命中一部分名称后，丢弃未命中名称；可能继续比较子集 | tools.py、zhipu_agent.py | 必需实体有未解析项即阻断，保留已识别项和失败原因；在模型调用前同样拦截 |
| “多少钱”未进入价格 Fast Path；利润比较过度依赖“只” | intent_engine.py | 通用问价词；从明确请求维度生成利润/ROI Scope |
| 风险字段查询进入默认详情报告；比较默认扩展为五维 | intent_engine.py、tools.py、app.js | 风险事实复用 simple_fact；Summary、Evidence、卡片和表格按请求维度投影 |

实施顺序：只读审计 → 现有 Schema 扩展 → Resolver / Planner / Scope Guard → 前端消费契约 → scoped tests → 正式项目 full pytest 一次 → 硬编码检查 / 报告 / 停止。

## 3. Architecture Changes / 兼容方式

1. 复用 `AgentRunResult`，新增 `requested_entities`；原 `entities` / `products` / `product_ids` 保留。
2. 每个 `EntityResolutionResult` 区分 EXACT_MATCH、NORMALIZED_MATCH、UNIQUE_ALIAS_MATCH、FUZZY_UNIQUE_MATCH、AMBIGUOUS、NOT_FOUND、LOW_CONFIDENCE。
3. 只有成功解析项持有唯一 `product_id`；未解析项只能持有候选。Pydantic 拒绝“NOT_FOUND 却带 product_id”等矛盾对象。
4. 新增 `response_type=not_found`。AMBIGUOUS / LOW_CONFIDENCE 仍用 clarification，但前端依据具体实体状态展示不同说明及最多 5 个候选。
5. 比较前检查全部必需实体；已识别商品不会因另一名称失败而消失，也不会被旧 UI Selection 替换。显式三商品查询不会因旧 Selection 只有两个而被截断。
6. 风险等级查询使用现有 get_product + simple_fact：只展示商品、风险、来源和已有更新时间，不生成审核建议或推荐结论。
7. 利润比较复用 compare_products + calculate_profit，保留利润/ROI 维度；取消依综合评分排序后宣称“第一名”的越界答复。
8. 无指定维度的普通比较默认身份和价格；明确“全面/综合比较”才展开全部决策维度。保留 Compliance Hard Gate。
9. 数据库表、迁移和扩展同步协议不变；不新增第二套 Planner / Renderer 业务事实源。不新增任何会话或长期记忆能力。

## 4. Changed Files（E 盘 Git 相对路径）

Modified:

- `backend/app/agent/entity_resolution.py`
- `backend/app/agent/intent_engine.py`
- `backend/app/agent/tools.py`
- `backend/app/agent/zhipu_agent.py`
- `backend/app/schemas/agent.py`
- `backend/app/web/app.js`
- `backend/tests/test_enterprise_workspace_api.py`

New:

- `backend/tests/test_stability_sprint_v3.py`
- `backend/docs/evaluation/STABILITY_SPRINT_V3_REPORT.md`

## 5. New Tests / 实际执行结果

新增测试文件展开为 41 项，包括：六类核心实体语义、唯一简称、精确 ID/外部 ID/SKU、数字型号不混淆、名称内含语法词、部分多实体失败、两项已识别仍不得忽略第三项失败、显式实体优先、租户隔离、未解析不得调用模型、风险事实范围、利润/ROI 范围、默认比较范围、候选转义、价格模拟引用保护、ACT3 20 场景模板化回放。

前端测试通过 Node 执行实际结果 Renderer，浏览器依赖使用轻量桩；不是仅检查源码字符串。未启动真实服务、未自动访问浏览器或修改业务数据。

| 执行 | 真实结果 |
| --- | --- |
| 初始旧版 scoped 保护集 | 46 passed，1 warning，11.81s |
| 新增测试初轮 | 32 passed / 4 failed：暴露多字限定词模糊匹配过宽，以及测试创建状态码错误，均已修正 |
| ACT3 新增回放初轮 | 38 passed / 1 failed：演示库中“充电器”实际只有一个合理候选，纠正测试预期，不制造假歧义 |
| ACT3 与新测试复测（追加两项保护前） | 39 passed，1 warning，5.57s |
| 全部相关 scoped（含 v1/v2、Planner 和最终 v3） | 87 passed，1 warning，17.63s |
| **唯一一次完整 pytest：正式 E 盘项目** | **116 passed / 1 failed，1 warning，21.92s** |
| 修正失败测试前置数据后的定向复测 | **47 passed，1 warning，8.33s** |
| JavaScript 语法检查 | PASS |
| Git diff --check | PASS |

完整测试唯一失败为 `test_zhipu_failure_is_audited_and_dashboard_reports_safe_fallback`：旧测试没有创建商品 A/B，却期待直接触发 Mock 认证失败。新 Guard 正确提前阻断。修复仅为在内存库创建两个测试商品，并增加“Mock 确实收到一次请求”的断言；原认证错误、审计、回退来源断言未删除。最后定向复测包括该文件全部 6 项与 v3 全部 41 项，全部通过。

**没有第二次运行完整 pytest，不能将首次完整结果改写为 117 passed。** 应用代码在完整测试后没有再修改；仅修正旧测试的前置数据。

完整测试命令（正式 backend 目录）：

```text
.venv-py314/Scripts/python.exe -X utf8 -m pytest tests -q -p no:cacheprovider
```

定向复测：

```text
.venv-py314/Scripts/python.exe -X utf8 -m pytest tests/test_enterprise_workspace_api.py tests/test_stability_sprint_v3.py -q -p no:cacheprovider
```

运行进程设置 LLM_PROVIDER=disabled、ZHIPU_API_KEY 为空；需要模型行为的测试自行 monkeypatch Mock。这些仅为进程环境变量，不改 `.env`。Renderer 测试需要 Node 在 PATH 中。现有 warning 为 Starlette TestClient/httpx 弃用提示，本轮不升级依赖。

## 6. ACT3 Case Results / Tool Count

以下“修改前”来自人工 ACT3，“修改后”来自内存演示数据的模板化同义回放，不声称验证了真实数据库或真实 GLM。工具次数指注册工具实际执行，不等于底层 SQL 次数。

| Case | 修改后语义与展示 | 工具前 → 后 |
| --- | --- | --- |
| C1 / C2 / C10 / C11 | NOT_FOUND；明确未找到，不是歧义 | 0 → 0 |
| C3 温湿度计 / C4 理线器 | 唯一简称 → simple_fact | 0 → 1 |
| C5 智能温度湿度计 | 高置信近似 → 智能温湿度计 | 1 → 1 |
| C6 蓝牙音箱 / C7 充电器 | 当前演示库各一个合理候选，直接识别 | 0 → 1 |
| C8 完整名称 / C9 显式名称优先 / C20 外部 ID | EXACT_MATCH，价格事实 | 1 → 1 |
| C12 桌面收纳架 / C19 桌面那个 | LOW_CONFIDENCE，展示候选供确认 | 0 → 0 |
| C13 风险等级 | simple_fact，不再完整决策报告 | 1 → 1 |
| C14 利润比较 | 两商品，profit/roi 范围 | 1 → 3 |
| C15 一项不存在 | 保留第一项已识别 + 第二项 NOT_FOUND，停止比较 | 0 → 0 |
| C16 蓝牙音箱比较 | 当前演示库唯一匹配，完成两商品利润比较 | 0 → 3 |
| C17 那个电子产品 | 无合理名称候选，NOT_FOUND；不借用旧选中状态 | 0 → 0 |
| C18 智能温湿度汁 | 保守单字近似识别，simple_fact | 0 → 1 |

C14/C16 的 3 次为一次 compare_products 与每商品一次 calculate_profit，不包含竞争/趋势工具。新增另一个音箱候选的独立测试确认：此时 AMBIGUOUS，返回候选，比较为 0 次。不会为匹配人工 Case 而写死“总有多个蓝牙音箱”。

## 7. Hardcode / Safety Review

- 本轮修改的应用文件中未发现 ACT3 完整问题等值分支、特定测试商品特殊处理、demo-ozon ID 特判、固定 count=60、固定售价或 Prompt 写死答案。
- 测试文件保留人工场景名及合成数据，这是回归输入，不是应用业务判断。
- 业务事实仍由后端 validated result 提供；前端只投影已请求字段，不重算筛选、风险或推荐。
- 没有新增异常吞噬、无限 retry、跨请求/跨租户缓存；沿用现有每 Run 工具复用。Mock 超时与重复调用保护测试通过。
- 候选姓名和 ID 经前端转义；租户外名称和 ID 不会作为候选返回。
- 只有业务展示契约扩展，无数据库结构变更。新增响应类型需要前后端一起更新，本轮已同步。

## 8. 学习总结与剩余限制

- Entity Resolution 不是“搜索结果直接等于商品身份”：必须先区分唯一确认、多个候选、未找到和低置信，再决定是否允许执行。
- Partial Resolution 需要保留每个输入的处理状态；否则一句“至少两个商品”会隐藏真正的失败点。
- Scope Guard 是“用户问什么，就计算/展示什么”的共同约束；仅减少工具数、却仍展示完整推荐报告，不算完成修复。
- 回归失败需要检查业务前提：本轮修正的是缺少测试商品的旧 fixture，而不是绕过新的实体校验以让测试变绿。
- 匹配分值是启发式相似度，不是统计校准概率；复杂别名、跨语言翻译和多意图长句仍有局限，不能承诺任意表达准确解析。
- 沿用现有最多读取 500 条企业商品、一次最多处理 10 个比较商品的边界，未新增检索系统。
- 对于“电子产品”这类类目指代，本轮不新增类目到商品的自动猜测能力；无合理名称候选时明确未找到。
- 兼容 Product payload 仍含其他字段；范围约束落实在事实、证据、回答和 Renderer，而非删除旧 API 字段。
- 未调用真实 GLM、未对真实数据库逐 Case 验证，也未新增完整 Chat History / Long-term Memory。
- 需要人工按 ACT3 C1–C20 验收实际页面，重点检查候选列表、C13 风险卡片、C14 利润比较、C15 部分未找到。若真实库中新增同类商品，简称结果可能合理地由唯一变成歧义。

本 Sprint 到此停止。下一步仅为人工回归与确认 Git 差异；不自动进入新功能开发。
