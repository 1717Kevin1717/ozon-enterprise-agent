# Evaluation Sprint v1 — Golden Cases & Agent Quality Baseline

日期：2026-09-04。项目：`E:/OZON AI 选品`。
被测业务版本 HEAD：`08ddb1ef974b9ac7159645cf47bf5b94b44e730d`。
本轮未 commit / push；业务应用、数据库 Schema / Migration 未修改。

## 1. Audit Findings

只读检查了当前 agent、schemas、services，三份 Stability 测试及 v2/v3 报告。以当前代码为准，不以历史报告中的旧测试数字或旧 Git 状态替代本轮执行证据。

| 审计问题 | 结论与来源 |
| --- | --- |
| AgentRunResult 可评测字段 | run_id、intent、response_type、requested_entities、entities、product_ids、products、requested_dimensions、display_scope、tool_results、tool_call_count、decision_status、human_review_required、fact、matched_count、total_count、displayed_count、filter_criteria、warnings、missing_data、data_sufficiency、fallback_used、task_completed、duplicate_tool_execution、trace；定义于 app/schemas/agent.py |
| intent / dimensions 在哪里生成 | app/agent/intent_engine.py 的 parse_intent 与 IntentPolicy |
| 实体状态 / 身份在哪里生成 | app/agent/entity_resolution.py；tools.py 将结果放入已有 AgentRunResult |
| 回答类型 / 事实 / 风险提示在哪里生成 | app/agent/tools.py 的 _response_type、agent_v1_ask、_tool_result_contract，最后由 validate_agent_answer 校验；decision_engine.py 提供确定性分析及 Gate |
| 工具 / 回退 / 幂等在哪里生成 | tool_registry.py 校验工具，runtime.py 的 AgentRunState 记录实际执行和复用，zhipu_agent.py 处理模型边界和回退 |
| 什么是确定性 Ground Truth | 人工确认的 Mock 商品名/售价/总数；明确的 AND 条件；合规硬 Gate、排名不等于推荐；一次快照不足；已成功同参工具不得重执行 |
| 什么依赖 MockLLM | Round 1 发出真实后端 compare_products 请求，Round 2 模拟超时；重复工具请求后总结。Mock 不产生商品事实或正确答案，也不做 Judge |
| 哪些现有测试被复用为场景来源 | v1 的 count/price/state/fallback；v2 的过滤/Scope/Gate/充分性；v3 的实体六类语义、部分多实体、风险事实和利润范围 |
| 如何隔离数据库 | 每个 Case 一个新子进程；配置导入前强制内存 URL，屏蔽 dotenv/secret directory；只在该内存库创建现有 ORM 表并调用原 Repository / Agent |
| 是否已经存在 Evaluation | app/services/agent_evaluation.py 与 evaluation_router.py 已有在线四场景演示 API，写入请求所属企业 DB，且部分 expected 使用被测 filter_products 自身计算。不存在独立 JSONL Golden CLI。保留该 API，不新增第二个在线服务或看板；本轮新增的 evals 仅承担离线质量门禁，复用既有数据工厂与 Agent 契约 |

## 2. Eval Architecture

`JSONL → Pydantic 校验 → Case/category 选择 → 每 Case 独立 Worker → 内存 Fixture → 原 Agent → 原 AgentRunResult 校验 → 10 个确定性 Evaluator → JSON/Markdown`

- evals/__init__.py：无副作用包入口，不导入运行配置。
- evals/schemas.py：Case、Setup、Expected 与 AssertionResult；未知字段、错误类型、非法枚举、重复 case_id 均拒绝。
- evals/fixtures.py：安全启动、复用 enterprise_demo_candidates / ProductRepository，按需添加仅存在于内存的歧义商品，执行原 Agent；Mock 只控制工具规划与超时。
- evals/evaluators.py：独立逐项断言，不参与修改 Agent 输出。
- evals/runner.py：命令行、进程隔离、60 秒单 Case 超时、统计和报告；Case 失败退出码 1，配置/选择失败退出码 2。
- evals/cases/golden_cases_v1.jsonl：唯一离线 Golden Dataset。
- evals/reports/：机器报告及可读报告；不写业务 AgentEvaluation 表。

没有新增框架、Memory、Chat History、Multi-Agent、MCP、RAG、RBAC 或数据库迁移。未更改 Planner、业务答案、现有在线评测 API。

## 3. Golden Dataset 来源

共 **29 个 Case**：Simple Fact 6、Entity 6、Filtering 3、Comparison 4、Policy 3、Data Sufficiency 2、State 3、Fallback 2。

来源为人工 ACT3 与已确认的 Stability v1/v2/v3 场景和明确业务规则。每条 JSONL 均带 provenance。沿用 `enterprise-mock-v2-60` 数据工厂，不重新生成一套商品主数据。

已确认的 Mock 固定值：商品总数 60；桌面理线器 3791 RUB；智能温湿度计 3474 RUB；便携蓝牙音箱 4件装 3157 RUB。

AMBIGUOUS Case 在隔离库添加“室外防水蓝牙音箱”；NORMALIZED Case 添加“USB 桌面风扇 3件装”。这些是显式合成 Fixture，不是新企业业务商品。案例之间互不共享这些添加项。

**Mock Dataset ≠ real Ozon data。** 图片/链接即使出现在原数据工厂中，本轮也不联网验证，不下载图片。

## 4. Ground Truth 规则

- 已确认的总数和售价直接作为版本化 Fixture 常量写入 Golden Dataset，不写入业务应用。
- 商品主键随机生成，因此 Case 用名称或 `{product_id:商品标题}` 引用 Fixture，Runner 在查询前绑定当前内存库 ID。
- 筛选 Oracle 读取 Agent 执行前的 Fixture 快照，用独立 AND 谓词计算符合条件的 ID 集；不调用 filter_products，也不读取实际回答来生成 expected。
- 同时检查每个明确 filter_criteria、匹配 ID、数量、展示数量以及不得偷偷放宽条件。
- 这里的利润率/风险值来自既有确定性分析器生成的 Fixture 字段：独立验证的是筛选集合与约束，不声称独立验证全部利润/评分公式。现有 decision_engine 测试继续负责公式保护。
- 政策使用明确 Gate 规则和必要结论断言；充分性区分 reported metric 与 observed trend。
- 幂等既检查 reported duplicate_tool_execution，也从成功执行记录独立计数，并要求真实存在 tool_reused 轨迹和恰好一次成功 compare 执行。
- 不使用 LLM-as-a-Judge；没有让 LLM 同时生成问题、Ground Truth 和裁判。本轮没有真实 GLM 稳定性结论。

## 5. Evaluators

所有断言均保留 evaluator、assertion、pass/fail、expected、actual 和简短 reason。

| Evaluator | 检查 |
| --- | --- |
| IntentEvaluator | 意图与 Golden Truth 一致 |
| ResponseTypeEvaluator | 响应类型 |
| EntityResolutionEvaluator | 状态顺序、已解析名称/ID、商品清单、候选列表 |
| ToolScopeEvaluator | 实际工具集合与工具预算 |
| RequestedDimensionEvaluator | 请求维度、禁止展示范围、禁用文案、应为空的提示字段 |
| DecisionPolicyEvaluator | 决策状态、人工审核标志和必要政策结论 |
| DataSufficiencyEvaluator | 快照数、充分性、观测趋势、外部指标及来源 |
| FactGroundingEvaluator | 固定事实、Fixture 商品 ID/名称/售价、独立过滤集合与数量 |
| StateLeakageEvaluator | Selection 来源是否符合当前查询 |
| IdempotencyEvaluator | 回退、任务完成、工具复用及重复执行为 0 |

另有 ExecutionSafetyEvaluator 作为 Worker 执行门禁，执行失败、非内存配置或发生被阻断的 HTTP 尝试均令 Case 失败，不计成空结果 PASS。

## 6. Runner 使用方式

在 `E:/OZON AI 选品/backend` 目录使用项目 Python：

```powershell
.\.venv-py314\Scripts\python.exe -m evals.runner
.\.venv-py314\Scripts\python.exe -m evals.runner --case-id SF02
.\.venv-py314\Scripts\python.exe -m evals.runner --category fallback
.\.venv-py314\Scripts\python.exe -m evals.runner --report-dir evals/reports
```

Runner 本身负责环境隔离，不需要编辑 `.env`、启动后台服务或提供 API Key。每次生成新时间戳报告，使用独占创建，不覆盖旧报告。筛选不存在的 ID/category 会报错，不产生虚假的“0 Case 全通过”。

## 7. 实际 Baseline Results

本轮 Golden 全量执行一次：**29/29 Case PASS，470/470 assertion PASS，Failed=0**。

| 指标 | PASS / 适用 Case | 比例 |
| --- | --- | --- |
| overall_case_pass_rate | 29 / 29 | 100% |
| assertion_pass_rate | 470 / 470 条断言 | 100% |
| intent_accuracy | 29 / 29 | 100% |
| entity_resolution_accuracy | 27 / 27 | 100% |
| response_type_accuracy | 29 / 29 | 100% |
| response_scope_accuracy | 20 / 20 | 100% |
| tool_scope_accuracy | 29 / 29 | 100% |
| decision_policy_accuracy | 15 / 15 | 100% |
| data_sufficiency_accuracy | 2 / 2 | 100% |
| fact_grounding_accuracy | 20 / 20 | 100% |
| state_accuracy | 14 / 14 | 100% |
| fallback_accuracy | 2 / 2 | 100% |
| task_completion_accuracy | 6 / 6 | 100% |
| execution_safety | 29 / 29 | 100% |

每项 accuracy 以“包含该类断言的 Case”为分母，只有该 Case 的该指标全部断言通过才算通过；未适用项不按 PASS 填充。例如 policy 指标还覆盖简单事实的“不应要求人工审核”标志，专门的 Policy 类场景本身为 3/3；entity 指标覆盖有商品清单/身份断言的 Case，专门 Entity 类场景为 6/6。分类统计与断言明细均在 JSON 中。

筛选运行证据：FL01（利润率≥30%、低风险、合规通过）6 个；FL02（99% 阈值加相同约束）0 个；FL03（低风险多结果）6 个。均与独立 Oracle 一致。

Mock 回退与重复请求的两个 Case 均为 3 次实际工具执行，compare_products 成功结果被复用，duplicate_tool_execution=0；Mock 交互各 2 轮。

不计算 Precision / Recall / F1：本数据结构和标注范围不足以声称这些指标。

## 8. Failed Cases

Golden 基线无失败 Case，未通过修改 expected 让指标变绿。

框架开发首轮 scoped tests 曾有 2 失败 / 13 通过：新 Fixture 使用了错误的数据工厂导入名；安全测试的 Windows 子进程未指定 UTF-8。修复仅发生于新 Eval Fixture 与测试代码，修后 15/15 通过。未改原业务代码或 Golden 答案。

## 9. 自动测试结果

严格按要求执行：

1. Eval framework scoped：**15 passed in 5.32s**。
2. Golden Eval 全量一次：**29 passed / 0 failed；470 条断言全通过**。
3. 原 Stability scoped：**81 passed, 1 warning in 17.04s**。
4. Full pytest 一次：**132 passed, 1 warning in 25.27s**。
5. git diff --check：PASS。
6. Hardcode review：见下一节。
7. git status：只有本轮 Eval / 新测试 / 报告新增文件，原业务 tracked files 无修改。

框架 pytest 覆盖 JSONL 校验、重复 ID、错误 Expected、独立 Case、不污染下一 Case、失败明细、分类/ID 过滤、报告生成、分母与全断言规则、AND 筛选错误检测、dotenv/真实密钥/生产 DB 访问防护。只通过 Runner 跑少量隔离冒烟，不将 29 个 Golden Case 复制为巨大 pytest 参数集。

原 Stability 和 full pytest 启动前临时将 BaseSettings 初始化的 `_env_file` / `_secrets_dir` 设为 None，测试环境变量禁用模型、清空模型 key；现有 conftest 强制内存 DB。没有修改 conftest 或 `.env`。唯一 warning 是既有 Starlette TestClient/httpx 弃用提示。

## 10. Hardcode Review

- app / alembic 路径没有 Git 差异：没有为了 Eval 修改业务答案、解析条件或数据库规则。
- Evaluator / Runner / Fixture 中没有按具体测试商品、完整 query、固定价格或 count=60 返回 PASS 的分支。
- 检索到的 `case.case_id == case_id` 仅是 CLI 动态筛选参数，不是对某个固定 Case 特判。
- 具体价格、未知测试名称和预期状态只在 Golden 数据与测试 Fixture 中作为可审计 Ground Truth 出现；这是允许的固定版本测试数据。
- 没有 Prompt 注入测试答案，没有 LLM Judge，没有隐藏失败。JSON 和 Markdown 均保留所有失败断言的 expected / actual / reason。

## 11. Data / Secret Safety

- Worker 使用环境变量白名单，不继承模型凭据与运行 DB 配置。
- 导入 app.core.config 前屏蔽 dotenv / secrets 目录读取；只有成功建立内存 engine 才能执行。
- 工具仍为原后端实现；httpx 同步/异步真实发送、urllib 请求被禁止；MockClient 仅返回内存响应。
- 安全测试提供一个带哨兵内容的“生产 DB”文件和假秘密，继承环境故意指向它；验证文件字节不变、秘密不进入输出。同时对 `.env` 打开操作设陷阱，测试通过。
- 真实 `.env`、API Key、业务 DB、备份和本地运行数据未修改。本轮不通过读取真实密钥内容来证明安全。
- 新报告仅含 Mock 数据；不应将将来的真实企业数据直接导入可提交报告目录。

## 12. Known Limitations

- Eval v1 主要验证确定性 Agent 行为与结构化响应契约；29 Case 是已知回归样本，100% 不是开放问题分布上的泛化结论，也不是 Production Ready 声明。
- 对自然语言答案采用必要/禁止内容断言，不是完整语义正确性证明；没有 LLM-as-a-Judge。
- 没有真实 GLM 质量、时延、成本或服务可用性结论；Mock 只验证已声明的交互和错误分支。
- Filter Oracle 独立于筛选工具，但使用原分析引擎产生的 Fixture 利润/风险字段，不是该分析引擎的独立数学证明。
- 主键、run_id、时间戳每次不同；评测比较稳定的业务语义，不要求报告文件字节相同。JSON 保存 Golden 数据和数据工厂 SHA-256，便于追溯 Fixture 版本。
- 当前评测直接调用后端 Agent，不通过浏览器；UI Scope 仍由已有 Stability Renderer 测试保护。没有新增网页看板或在线 Eval 服务。
- 暂未覆盖所有自然语言变体、极大商品库、全权限矩阵、真实扩展页面或生产部署。没有新增 Memory / Chat History 能力。
- 原在线四场景演示评测保留，但不将其同工具自计算的结果冒充本离线 Golden 基线。

## 13. 报告文件与下一步

本轮真实输出：

- `backend/evals/reports/eval_v1_20260904T034709_056447Z.json`
- `backend/evals/reports/eval_v1_20260904T034709_056447Z.md`
- 本报告：`backend/docs/evaluation/EVALUATION_SPRINT_V1_REPORT.md`

本轮新增实现：evals/__init__.py、schemas.py、fixtures.py、evaluators.py、runner.py、cases/golden_cases_v1.jsonl、reports/.gitkeep，以及 tests/test_eval_framework.py。

下一步仅由人工检查 Golden Truth、指标分母与 Git 新增文件后决定是否提交。未来发现真实回归时，先明确其人工/业务规则来源再增加 Golden Case；不自动扩展到新产品功能或真实 GLM 测试。

本 Sprint 完成后停止，等待人工确认；未 commit / push。
