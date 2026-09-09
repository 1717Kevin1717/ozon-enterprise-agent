# OZON Enterprise Product Selection Agent
# Ozon 企业级 AI 选品 Agent

Enterprise-oriented AI product-selection Agent prototype for cross-border e-commerce scenarios.

基于真实跨境电商选品场景设计的 AI Agent 产品原型，目标是降低商品数据收集、竞品分析、利润测算与历史经验复用中的人工成本。

> ⚠️ Current development/evaluation data is synthetic/mock data. It must not be presented as real Ozon market data.
>
> 当前开发与评估数据均为 synthetic / mock data，不代表真实 Ozon 市场数据。

---

## Recruiter Quick View｜招聘方快速了解

| 项目 | 内容 |
| --- | --- |
| 业务问题 | 商品信息分散、竞品分析依赖人工、利润判断标准不统一、历史经验难沉淀 |
| 核心用户 | 跨境电商运营人员 / 选品人员 |
| 产品目标 | 将商品检索、竞品分析、利润测算、风险识别与历史经验检索整合到 AI 辅助决策流程 |
| AI 能力 | RAG、Tool Calling、Memory、Structured Output、Agent Workflow |
| Evaluation | Task Success Rate、Tool Calling Accuracy、Answer/Data Accuracy、Latency、Cost |
| 当前阶段 | 核心架构及部分功能已实现，Agent 稳定性、数据完整度检查与 Evaluation 持续迭代中 |

---

## Why this project exists｜为什么做这个项目

The project targets common product-selection problems in cross-border e-commerce: fragmented product information, manual profit judgment, weak competitor comparison standards, compliance risk, and difficulty auditing AI-assisted decisions.

这个项目来源于实际跨境电商运营过程中观察到的问题：

- 商品信息需要在平台、插件、表格之间反复收集
- 竞品分析和利润判断大量依赖人工
- 不同运营人员判断标准不统一
- 历史选品经验难以系统沉淀
- AI 输出看起来合理，并不代表业务任务真的完成正确

因此，我尝试将选品过程拆解为结构化业务流程，并在真正需要动态判断的环节引入 Agent。

---

## Product Workflow｜产品流程

> 以下为目标产品流程，部分能力仍处于开发或评估阶段。

```mermaid
flowchart LR
    A[用户输入选品需求] --> B[信息完整度检查]
    B -->|缺少关键参数| C[主动追问]
    B -->|信息完整| D[商品与历史数据检索]
    D --> E[RAG / Memory]
    E --> F[Tool Calling]
    F --> G[竞品分析]
    F --> H[利润测算]
    F --> I[风险识别]
    G --> J[AI 综合判断]
    H --> J
    I --> J
    J --> K[Structured Output]
    K --> L[人工决策]
