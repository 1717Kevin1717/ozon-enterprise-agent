from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools import filter_products, rule_agent_ask
from app.db.models import AgentEvaluation, AgentRun
from app.repositories.products import ProductRepository


EVAL_SUITE_VERSION = "enterprise-selection-stability-v1"


@dataclass(frozen=True)
class EvalCase:
    name: str
    query: str
    filters: dict[str, Any]
    limit: int
    expected_tool: str = "filter_products"


CASES = (
    EvalCase("推荐度阈值筛选", "企业有哪些商品分数达到60以上？", {"min_score": 60, "sort_by": "recommendation_score", "sort_direction": "desc"}, 100),
    EvalCase("Top5优先级", "从候选池选出最值得测试的5个商品，并说明证据和缺口。", {"sort_by": "recommendation_score", "sort_direction": "desc"}, 5),
    EvalCase("数据缺失阻断", "哪些商品因为数据不完整不能进入最终审核？", {"completeness": "incomplete", "sort_by": "completeness", "sort_direction": "asc"}, 100),
    EvalCase("利润与竞争约束", "找利润30%以上，竞争低于40的商品。", {"min_margin_rate": 0.30, "max_competition_score": 40, "sort_by": "margin_rate", "sort_direction": "desc"}, 100),
)


def _accuracy(expected_ids: list[str], actual_ids: list[str]) -> float:
    expected, actual = set(expected_ids), set(actual_ids)
    if not expected and not actual:
        return 1.0
    union = expected | actual
    return round(len(expected & actual) / len(union), 4) if union else 0.0


def evaluation_view(row: AgentEvaluation) -> dict[str, Any]:
    return {
        "id": row.id,
        "task_name": row.task_name,
        "task_input": row.task_input,
        "task_success_rate": row.task_success_rate,
        "answer_accuracy": row.answer_accuracy,
        "tool_calling_accuracy": row.tool_calling_accuracy,
        "data_completeness": row.data_completeness,
        "judge_mode": row.judge_mode,
        "status": row.status,
        "details": row.details_json,
        "created_at": row.created_at,
    }


async def run_deterministic_suite(session: AsyncSession, company_id: str, user_id: str, role: str) -> list[AgentEvaluation]:
    repo = ProductRepository(session, company_id)
    evaluations: list[AgentEvaluation] = []
    for case in CASES:
        expected_result = await filter_products(repo, role, **case.filters, limit=case.limit)
        expected_ids = [str(item["id"]) for item in expected_result.get("data", [])]
        actual = await rule_agent_ask(session, company_id, user_id, role, case.query, None, [], response_mode="rule_engine", provider_notice="本次为离线确定性回归评测，不调用外部模型。")
        actual_ids = [str(item["id"]) for item in actual.get("products", [])]
        tools = [str(item.get("tool")) for item in actual.get("trace", []) if item.get("event") == "tool_finished" and item.get("success")]
        answer_accuracy = _accuracy(expected_ids, actual_ids)
        task_success = 1.0 if expected_ids == actual_ids and actual.get("matched_count") == len(expected_ids) else 0.0
        tool_accuracy = 1.0 if case.expected_tool in tools else 0.0
        completeness_values = [float(item.get("completeness") or 0) / 100 for item in actual.get("products", [])]
        data_completeness = round(sum(completeness_values) / len(completeness_values), 4) if completeness_values else 1.0
        run = await session.scalar(select(AgentRun).where(AgentRun.company_id == company_id, AgentRun.session_id == actual.get("session_id")).order_by(desc(AgentRun.created_at)))
        status = "passed" if task_success == answer_accuracy == tool_accuracy == 1.0 else "failed"
        row = AgentEvaluation(
            company_id=company_id,
            agent_run_id=run.id if run else None,
            task_name=case.name,
            task_input={"query": case.query, "suite_version": EVAL_SUITE_VERSION},
            expected_output={"product_ids": expected_ids, "expected_tool": case.expected_tool},
            actual_output={"product_ids": actual_ids, "matched_count": actual.get("matched_count"), "tools": tools, "tool_call_count": actual.get("tool_call_count"), "response_mode": actual.get("response_mode")},
            task_success_rate=task_success,
            answer_accuracy=answer_accuracy,
            tool_calling_accuracy=tool_accuracy,
            data_completeness=data_completeness,
            judge_mode="deterministic_no_llm",
            status=status,
            details_json={"suite_version": EVAL_SUITE_VERSION, "source_badge": actual.get("source_badge"), "expected_count": len(expected_ids), "actual_count": len(actual_ids)},
        )
        session.add(row)
        evaluations.append(row)
    await session.commit()
    for row in evaluations:
        await session.refresh(row)
    return evaluations


async def list_evaluations(session: AsyncSession, company_id: str, limit: int = 50) -> list[AgentEvaluation]:
    return list((await session.scalars(select(AgentEvaluation).where(AgentEvaluation.company_id == company_id).order_by(desc(AgentEvaluation.created_at)).limit(limit))).all())


async def evaluation_summary(session: AsyncSession, company_id: str) -> dict[str, Any]:
    values = await session.execute(
        select(
            func.count(AgentEvaluation.id),
            func.avg(AgentEvaluation.task_success_rate),
            func.avg(AgentEvaluation.answer_accuracy),
            func.avg(AgentEvaluation.tool_calling_accuracy),
            func.avg(AgentEvaluation.data_completeness),
        ).where(AgentEvaluation.company_id == company_id)
    )
    count, task_success, answer_accuracy, tool_accuracy, completeness = values.one()
    return {
        "evaluation_count": int(count or 0),
        "task_success_rate": round(float(task_success or 0), 4),
        "answer_accuracy": round(float(answer_accuracy or 0), 4),
        "tool_calling_accuracy": round(float(tool_accuracy or 0), 4),
        "data_completeness": round(float(completeness or 0), 4),
        "suite_version": EVAL_SUITE_VERSION,
        "judge_mode": "deterministic_no_llm",
    }
