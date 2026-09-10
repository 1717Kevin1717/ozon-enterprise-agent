import time
import asyncio
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.entity_resolution import EntityResolution, resolve_query_entities, resolution_message
from app.agent.context import build_context_snapshot, resolve_context_reference
from app.agent.query_understanding import arbitrate_entity_mentions, build_semantic_context_packet, understand_query, semantic_fallback, semantic_policy
from app.agent.intent_engine import ParsedIntent, parse_intent
from app.agent.runtime import AgentRunState
from app.agent.tool_registry import TOOL_REGISTRY
from app.db.models import AgentRun, ConversationMessage, ConversationSession, Memory, Product, uid
from app.repositories.products import ProductRepository, product_view
from app.schemas.agent import validate_agent_answer
from app.services.decision_engine import analyze, recommendation_gate_status, simulate_price, to_dict
from app.services.knowledge_base import search_documents
from app.services.provenance import FRESHNESS_MAX_AGE_DAYS, evidence_notices, scoped_fields, snapshot_view
from app.schemas.provenance import FieldEvidence


TOOL_POLICY = {spec.tool_name: list(spec.allowed_roles) for spec in TOOL_REGISTRY.all()}
BUSINESS_TOOL_LABELS = {
    "count_products": "统计企业商品总数",
    "filter_products": "筛选企业商品库",
    "search_products": "检索企业商品库",
    "get_product": "读取商品主档与证据",
    "compare_products": "建立商品决策对比",
    "calculate_profit": "分析利润模型",
    "get_sales_trend": "核验销量快照趋势",
    "get_price_trend": "核验价格趋势",
    "analyze_competition": "分析竞争情况",
    "simulate_price_change": "执行价格情景模拟",
    "find_historical_failures": "查询企业失败案例",
    "search_company_memory": "检索企业历史记忆",
    "search_company_knowledge": "检索企业知识库",
    "rank_products": "生成候选优先级",
}


def allow(tool: str, role: str) -> bool:
    return TOOL_REGISTRY.allows(tool, role)


def tool_error(tool: str, code: str, message: str = "") -> dict:
    return {"tool": tool, "success": False, "error_code": code, "message": message}


def event(trace: list[dict], status: str, tool: str, summary: str, **extra: Any) -> None:
    trace.append({"event": status, "tool": tool, "business_label": BUSINESS_TOOL_LABELS.get(tool, tool), "summary": summary, **extra})


async def count_products(repo: ProductRepository, role: str) -> dict:
    if not allow("count_products", role):
        return tool_error("count_products", "FORBIDDEN")
    total = await repo.session.scalar(select(func.count(Product.id)).where(Product.company_id == repo.company_id))
    return {"tool": "count_products", "success": True, "data": {"count": int(total or 0)}}


async def search_products(repo: ProductRepository, keyword: str, role: str) -> dict:
    if not allow("search_products", role):
        return tool_error("search_products", "FORBIDDEN")
    rows = await repo.list(keyword)
    return {"tool": "search_products", "success": True, "data": [product_view(item, await repo.latest_analysis(item.id)) for item in rows]}


async def filter_products(repo: ProductRepository, role: str, **filters: Any) -> dict:
    if not allow("filter_products", role):
        return tool_error("filter_products", "FORBIDDEN")
    criteria = TOOL_REGISTRY.require("filter_products").input_schema.model_validate(filters).model_dump()
    keyword = str(criteria.get("keyword") or "")
    limit = int(criteria["limit"])
    views: list[dict[str, Any]] = []
    for product in await repo.list(keyword, limit=500):
        latest = await repo.latest_analysis(product.id)
        view = product_view(product, latest)
        if not latest:
            view["analysis"] = to_dict(analyze(product))
        analysis = view.get("analysis") or {}
        score_value = float(analysis.get("recommendation_score") or 0)
        completeness_grade = (analysis.get("evidence_completeness") or {}).get("grade")
        if criteria.get("min_score") is not None and score_value < float(criteria["min_score"]):
            continue
        if criteria.get("max_score") is not None and score_value > float(criteria["max_score"]):
            continue
        if criteria.get("min_margin_rate") is not None and float(view.get("current_margin_rate") or analysis.get("net_margin") or 0) < float(criteria["min_margin_rate"]):
            continue
        if criteria.get("min_roi") is not None and float(analysis.get("roi") or 0) < float(criteria["min_roi"]):
            continue
        if criteria.get("max_market_saturation") is not None and float(view.get("market_saturation") or 0) > float(criteria["max_market_saturation"]):
            continue
        if criteria.get("brand") and str(criteria["brand"]).casefold() not in str(view.get("brand") or "").casefold():
            continue
        if criteria.get("category") and str(criteria["category"]).casefold() not in str(view.get("category_path") or "").casefold():
            continue
        if criteria.get("lifecycle_status") and view.get("lifecycle_status") != criteria["lifecycle_status"]:
            continue
        if criteria.get("risk_level") and str(analysis.get("risk_level") or "").casefold() != criteria["risk_level"]:
            continue
        if criteria.get("compliance_status"):
            compliance = str(view.get("compliance_status") or "").casefold()
            expected = criteria["compliance_status"]
            if not ((expected == "approved" and compliance in {"approved", "通过"}) or compliance == expected):
                continue
        if criteria.get("completeness") == "complete" and completeness_grade != "complete":
            continue
        if criteria.get("completeness") == "incomplete" and completeness_grade == "complete":
            continue
        updated_since = criteria.get("updated_since")
        if updated_since:
            threshold = updated_since.replace(tzinfo=None) if updated_since.tzinfo else updated_since
            if not view.get("updated_at") or view["updated_at"] < threshold:
                continue
        views.append(view)

    def sort_value(item: dict[str, Any]) -> Any:
        analysis = item.get("analysis") or {}
        mapping = {
            "recommendation_score": float(analysis.get("recommendation_score") or 0),
            "completeness": float((analysis.get("evidence_completeness") or {}).get("percent") or 0),
            "updated_at": item.get("updated_at") or datetime.min,
            "margin_rate": float(item.get("current_margin_rate") or analysis.get("net_margin") or 0),
        }
        return mapping.get(str(criteria.get("sort_by") or "recommendation_score"), mapping["recommendation_score"])

    views.sort(key=sort_value, reverse=criteria.get("sort_direction", "desc") != "asc")
    displayed = views[:limit]
    return {
        "tool": "filter_products",
        "success": True,
        "data": displayed,
        "criteria": criteria,
        "total_count": len(views),
        "matched_count": len(views),
        "displayed_count": len(displayed),
        "message": "全部显式条件已由后端按 AND 语义执行；筛选、计数和排序共享同一结果。",
    }


async def get_product(repo: ProductRepository, product_id: str, role: str) -> dict:
    if not allow("get_product", role):
        return tool_error("get_product", "FORBIDDEN")
    product = await repo.get(product_id)
    if not product:
        return tool_error("get_product", "NOT_FOUND", "商品不存在或不属于当前企业。")
    view = product_view(product, await repo.latest_analysis(product.id))
    if not view.get("analysis"):
        view["analysis"] = to_dict(analyze(product))
    return {"tool": "get_product", "success": True, "data": view}


async def compare_products(repo: ProductRepository, product_ids: list[str], role: str) -> dict:
    if not allow("compare_products", role):
        return tool_error("compare_products", "FORBIDDEN")
    rows = []
    for product_id in dict.fromkeys(product_ids):
        product = await repo.get(product_id)
        if product:
            view = product_view(product, await repo.latest_analysis(product.id))
            if not view.get("analysis"):
                view["analysis"] = to_dict(analyze(product))
            rows.append(view)
    return {"tool": "compare_products", "success": True, "data": rows, "warning": "仅比较已保存的本企业数据；缺失证据不会被补造。"}


async def get_product_history(repo: ProductRepository, product_id: str, role: str) -> dict:
    if not allow("get_product_history", role):
        return tool_error("get_product_history", "FORBIDDEN")
    if not await repo.get(product_id):
        return tool_error("get_product_history", "NOT_FOUND")
    rows = await repo.history(product_id)
    data = [snapshot_view(item) for item in rows]
    return {"tool": "get_product_history", "success": True, "data": data}


async def calculate_profit(repo: ProductRepository, product_id: str, role: str) -> dict:
    if not allow("calculate_profit", role):
        return tool_error("calculate_profit", "FORBIDDEN")
    product = await repo.get(product_id)
    if not product:
        return tool_error("calculate_profit", "NOT_FOUND")
    result = to_dict(analyze(product))
    fields = ("gross_profit", "gross_margin", "net_profit", "net_margin", "roi", "expected_profit", "current_margin_rate", "break_even_price", "target_price", "profit_score", "risk_score", "data_confidence", "missing_data", "evidence_completeness")
    data = {key: result[key] for key in fields}
    data["provenance"] = scoped_fields({"fields": result["evidence"].get("field_provenance", {})}, ("profit", "roi"))
    calculation_evidence = _profit_calculation_evidence(data)
    if calculation_evidence:
        data["calculation_evidence"] = calculation_evidence
    return {"tool": "calculate_profit", "success": True, "data": data}


def _profit_calculation_evidence(data: dict[str, Any]) -> dict[str, Any]:
    """Project current raw inputs into an auditable calculation view without reverse engineering."""
    net_profit_evidence = next(
        (item for item in data.get("provenance", []) if item.get("field") == "net_profit"),
        None,
    )
    if not net_profit_evidence:
        return {}
    inputs = {item["field"]: item for item in net_profit_evidence.get("inputs", [])}

    def amount(field: str) -> float:
        value = inputs.get(field, {}).get("value")
        return float(value or 0)

    shipping_field = "shipping_cost" if amount("shipping_cost") else "fulfillment_cost"
    price = amount("current_price")
    commission_rate = amount("platform_commission_rate")
    commission_sources = [inputs.get("current_price", {}), inputs.get("platform_commission_rate", {})]
    cost_items = [
        ("procurement_cost", amount("procurement_cost"), inputs.get("procurement_cost", {})),
        (shipping_field, amount(shipping_field), inputs.get(shipping_field, {})),
        ("platform_commission", price * commission_rate, {
            "provider": "deterministic_evaluation_engine",
            "presence": "PRESENT" if all(item.get("presence") == "PRESENT" for item in commission_sources) else "UNKNOWN",
            "evidence_status": "TRACEABLE" if all(item.get("evidence_status") == "TRACEABLE" for item in commission_sources) else "SOURCE_MISSING",
        }),
        *[(field, amount(field), inputs.get(field, {})) for field in (
            "platform_fee", "advertising_cost", "warehousing_cost", "tax_cost",
            "return_loss_reserve", "other_cost",
        )],
    ]
    rendered_costs = [
        {
            "field": field,
            "amount": round(value, 4),
            "share_of_price": round(value / price, 4) if price else None,
            "provider": source.get("provider", "unknown"),
            "presence": source.get("presence", "UNKNOWN"),
            "evidence_status": source.get("evidence_status", "SOURCE_MISSING"),
        }
        for field, value, source in sorted(cost_items, key=lambda item: item[1], reverse=True)
    ]
    return {
        "metric": "net_profit",
        "formula": net_profit_evidence.get("calculation", ""),
        "inputs": net_profit_evidence.get("inputs", []),
        "input_sources": {field: item.get("provider", "unknown") for field, item in inputs.items()},
        "derived_from": net_profit_evidence.get("derived_from", []),
        "result": data.get("net_profit"),
        "net_margin": data.get("net_margin"),
        "roi": data.get("roi"),
        "sale_price": price,
        "total_cost": round(sum(item["amount"] for item in rendered_costs), 4),
        "unit": "RUB",
        "evidence_status": net_profit_evidence.get("evidence_status", "SOURCE_MISSING"),
        "input_gaps": [item["field"] for item in rendered_costs if item["presence"] != "PRESENT"],
        "cost_breakdown": rendered_costs,
    }


def merge_profit_calculation(view: dict[str, Any], tool_result: dict[str, Any]) -> None:
    """Bind current deterministic profit state and its evidence to one response view."""
    if not tool_result.get("success"):
        return
    data = tool_result.get("data") or {}
    analysis = view.setdefault("analysis", {})
    for field in (
        "gross_profit", "gross_margin", "net_profit", "net_margin", "roi",
        "current_margin_rate", "expected_profit", "break_even_price", "target_price",
        "profit_score", "risk_score", "data_confidence", "missing_data", "evidence_completeness",
    ):
        if field in data:
            analysis[field] = data[field]
    if "net_margin" in data:
        view["current_margin_rate"] = data["net_margin"]
    if data.get("calculation_evidence"):
        analysis["calculation_evidence"] = data["calculation_evidence"]

    trust = view.setdefault("data_trust", {})
    trust_fields = trust.setdefault("fields", {})
    for item in data.get("provenance") or []:
        trust_fields[item["field"]] = item
    if data.get("provenance"):
        trust["is_mock"] = bool(trust.get("is_mock") or any(item.get("is_mock") for item in data["provenance"]))
        trust["notices"] = [item for item in trust.get("notices", []) if item.get("code") != "LEGACY_ANALYSIS"]


async def analyze_competition(repo: ProductRepository, product_id: str, role: str) -> dict:
    if not allow("analyze_competition", role):
        return tool_error("analyze_competition", "FORBIDDEN")
    product = await repo.get(product_id)
    if not product:
        return tool_error("analyze_competition", "NOT_FOUND")
    result = analyze(product)
    return {"tool": "analyze_competition", "success": True, "data": {"score": result.competition_score, "competitor_count": product.competitor_count, "competitor_price_min": product.competitor_price_min, "competitor_price_avg": product.competitor_price_avg, "price_position": result.price_position, "market_saturation": product.market_saturation, "evidence": product.competition_evidence, "complete": result.evidence_completeness["groups"]["competition"]["ready"]}}


SALES_SNAPSHOT_MINIMUM = 2
SALES_SNAPSHOT_FRESHNESS_DAYS = FRESHNESS_MAX_AGE_DAYS["sales_snapshot"]


def sales_data_sufficiency(
    rows: list[dict[str, Any]],
    reported_metric: float | None,
    reported_metric_source: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate observed snapshots without promoting a reported metric into a trend."""

    current_time = (now or datetime.now(UTC)).replace(tzinfo=None)
    valid_rows = [item for item in rows if float(item.get("sales_30d") or 0) > 0]
    captured = [item.get("captured_at") for item in rows if isinstance(item.get("captured_at"), datetime)]
    latest = max(captured) if captured else None
    stale = bool(latest and current_time - latest.replace(tzinfo=None) > timedelta(days=SALES_SNAPSHOT_FRESHNESS_DAYS))
    sufficient = len(valid_rows) >= SALES_SNAPSHOT_MINIMUM and not stale
    if not sufficient:
        observed_trend = "INSUFFICIENT_DATA"
    else:
        first = float(valid_rows[0]["sales_30d"])
        last = float(valid_rows[-1]["sales_30d"])
        observed_trend = "GROWING" if last > first else "DECLINING" if last < first else "FLAT"
    return {
        "status": "SUFFICIENT" if sufficient else "INSUFFICIENT_DATA",
        "observed_trend": observed_trend,
        "snapshot_count": len(rows),
        "valid_sales_snapshot_count": len(valid_rows),
        "minimum_required": SALES_SNAPSHOT_MINIMUM,
        "available": len(valid_rows),
        "latest_snapshot_at": latest,
        "stale": stale,
        "reported_metric": reported_metric,
        "reported_metric_source": reported_metric_source or "not_provided",
        "notice": "快照趋势只基于有效且未过期的已保存快照；企业报告增速是独立来源，不能替代快照趋势。",
    }


async def get_sales_trend(repo: ProductRepository, product_id: str, role: str) -> dict:
    if not allow("get_sales_trend", role):
        return tool_error("get_sales_trend", "FORBIDDEN")
    product = await repo.get(product_id)
    if not product:
        return tool_error("get_sales_trend", "NOT_FOUND")
    history = await get_product_history(repo, product_id, role)
    rows = history.get("data", [])
    lineage_value = (product.field_lineage or {}).get("sales_growth_rate")
    reported_source = _source_name(lineage_value, str(product.sales_source or "not_provided"))
    has_reported_metric = bool(lineage_value) or reported_source not in {"", "not_provided", "未提供"}
    reported_metric = float(product.sales_growth_rate) if has_reported_metric else None
    sufficiency = sales_data_sufficiency(rows, reported_metric, reported_source)
    return {
        "tool": "get_sales_trend",
        "success": True,
        "data": {
            "points": rows,
            "snapshot_count": sufficiency["snapshot_count"],
            "valid_sales_snapshot_count": sufficiency["valid_sales_snapshot_count"],
            "snapshot_trend": sufficiency["observed_trend"],
            "trend": sufficiency["observed_trend"].lower(),
            "data_sufficiency": sufficiency,
            "static_sales_growth_rate": reported_metric,
            "static_sales_growth_source": reported_source,
            "notice": "快照趋势只基于已保存快照；主档销量增速单独展示，不等同于快照趋势。",
        },
    }


async def get_price_trend(repo: ProductRepository, product_id: str, role: str) -> dict:
    if not allow("get_price_trend", role):
        return tool_error("get_price_trend", "FORBIDDEN")
    history = await get_product_history(repo, product_id, role)
    if not history.get("success"):
        return tool_error("get_price_trend", history.get("error_code", "TOOL_FAILED"), history.get("message", ""))
    rows = history.get("data", [])
    values = [item["price"] for item in rows if float(item.get("price") or 0) > 0]
    trend = "insufficient" if len(values) < 2 else "rising" if values[-1] > values[0] else "falling" if values[-1] < values[0] else "flat"
    return {"tool": "get_price_trend", "success": True, "data": {"points": rows, "trend": trend, "notice": "趋势只基于已保存价格快照，不对缺失期间作推断。"}}


async def simulate_price_change(repo: ProductRepository, product_id: str, proposed_price: float, role: str) -> dict:
    if not allow("simulate_price_change", role):
        return tool_error("simulate_price_change", "FORBIDDEN")
    product = await repo.get(product_id)
    if not product:
        return tool_error("simulate_price_change", "NOT_FOUND")
    return {"tool": "simulate_price_change", "success": True, "data": simulate_price(product, proposed_price)}


async def find_historical_failures(session: AsyncSession, company_id: str, product: Product, role: str) -> dict:
    if not allow("find_historical_failures", role):
        return tool_error("find_historical_failures", "FORBIDDEN")
    failed = list((await session.scalars(select(Product).where(Product.company_id == company_id, Product.id != product.id, Product.lifecycle_status.in_(["abandoned", "已放弃"])).limit(100))).all())
    matches = []
    source_root = (product.category_path or "").split(">")[0].strip()
    for item in failed:
        target_root = (item.category_path or "").split(">")[0].strip()
        score = (0.55 if source_root and source_root == target_root else 0) + (0.45 if item.brand and item.brand == product.brand else 0)
        if score > 0:
            matches.append({"product_id": item.id, "title": item.title, "similarity": round(score, 2), "lifecycle_status": item.lifecycle_status, "reason": (item.raw_payload or {}).get("abandonReason", "") or "未记录失败原因"})
    matches.sort(key=lambda item: item["similarity"], reverse=True)
    return {"tool": "find_historical_failures", "success": True, "data": matches[:10], "notice": "相似度基于品牌与一级类目的确定性特征，不冒充语义相似度。"}


async def search_company_memory(session: AsyncSession, company_id: str, query: str, role: str) -> dict:
    if not allow("search_company_memory", role):
        return tool_error("search_company_memory", "FORBIDDEN")
    rows = list((await session.scalars(select(Memory).where(Memory.company_id == company_id, Memory.content.ilike(f"%{query}%")).limit(20))).all())
    return {"tool": "search_company_memory", "success": True, "data": [{"memory_type": item.memory_type, "content": item.content, "metadata": item.metadata_json, "created_at": item.created_at} for item in rows], "mode": "lexical_memory"}


async def search_company_knowledge(session: AsyncSession, company_id: str, query: str, role: str) -> dict:
    if not allow("search_company_knowledge", role):
        return tool_error("search_company_knowledge", "FORBIDDEN")
    return {"tool": "search_company_knowledge", "success": True, "data": await search_documents(session, company_id, query, 8), "mode": "local_hash_embedding_v1"}


async def rank_products(repo: ProductRepository, keyword: str, role: str) -> dict:
    return await filter_products(repo, role, keyword=keyword, sort_by="recommendation_score", sort_direction="desc", limit=100)


def select_targets(products: list[Product], query: str, selected_product_ids: list[str] | None = None, limit: int = 5) -> list[Product]:
    """Compatibility wrapper with query entities taking priority over UI state."""
    resolved = resolve_query_entities(products, query, limit)
    if resolved.products:
        return resolved.products
    by_id = {product.id: product for product in products}
    return [by_id[item] for item in dict.fromkeys(selected_product_ids or []) if item in by_id][:limit]


def plan_query(query: str, target_count: int) -> tuple[str, list[dict[str, str]]]:
    parsed = parse_intent(query, [f"selected-{index}" for index in range(target_count)])
    return parsed.name, list(parsed.plan)


def _agent_product(view: dict[str, Any]) -> dict[str, Any]:
    analysis = view.get("analysis") or {}
    completeness = analysis.get("evidence_completeness") or {}
    margin = float(view.get("current_margin_rate") or analysis.get("net_margin") or 0)
    decision_status = recommendation_gate_status(
        compliance_status=str(view.get("compliance_status") or ""),
        decision_ready=bool(completeness.get("decision_ready", False)),
        net_margin=margin,
        target_margin=float(view.get("target_margin_rate") or 0.30),
        risk_level=str(analysis.get("risk_level") or "unknown"),
        recommendation=str(analysis.get("recommendation") or "review_required"),
    )
    lineage = view.get("field_lineage") or {}
    price_lineage = lineage.get("current_price") or lineage.get("currentPriceRub") or "company_product_database"
    price_source = price_lineage.get("provider") or price_lineage.get("source", "company_product_database") if isinstance(price_lineage, dict) else str(price_lineage)
    return {
        "id": str(view.get("id") or ""), "external_product_id": str(view.get("external_product_id") or ""), "title": str(view.get("title") or ""),
        "brand": str(view.get("brand") or ""), "category_path": str(view.get("category_path") or ""), "score": float(analysis.get("recommendation_score") or 0),
        "recommendation_grade": str(analysis.get("recommendation_grade") or "C"), "decision_status": decision_status,
        "profit_score": float(analysis.get("profit_score") or 0), "demand_score": float(analysis.get("demand_score") or 0),
        "competition_score": float(analysis.get("competition_score") or 0), "compliance_score": float(analysis.get("compliance_score") or 0), "risk_score": float(analysis.get("risk_score") or 0),
        "confidence": float(analysis.get("data_confidence") or 0), "completeness": int(completeness.get("percent") or analysis.get("data_completeness") or 0),
        "risk_level": str(analysis.get("risk_level") or "unknown"), "compliance_status": str(view.get("compliance_status") or "pending"),
        "lifecycle_status": str(view.get("lifecycle_status") or "candidate"),
        "current_price": float(view.get("current_price") or 0), "currency": str(view.get("currency") or "RUB"), "price_source": price_source,
        "updated_at": view.get("updated_at"), "current_margin_rate": margin,
        "net_profit": float(analysis.get("net_profit") or 0), "roi": float(analysis.get("roi") or 0), "missing_fields": list(analysis.get("missing_fields") or []),
        "url": str(view.get("url") or ""), "main_image_url": str(view.get("main_image_url") or ""),
        "is_mock": bool((view.get("data_trust") or {}).get("is_mock")), "data_disclosure": str((view.get("data_trust") or {}).get("disclosure") or ""),
    }


def _trace_summary(trace: list[dict[str, Any]]) -> list[str]:
    labels = []
    for item in trace:
        if item.get("event") in {"tool_finished", "tool_reused"}:
            label = str(item.get("business_label") or item.get("tool") or "")
            if label and label not in labels:
                labels.append(label)
    return [f"{index + 1}. {label}" for index, label in enumerate(labels)]


def _unique(items: list[str], limit: int = 8) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))[:limit]


def _response_type(parsed: ParsedIntent, resolution: EntityResolution, products: list[dict[str, Any]], data_sufficiency: dict[str, Any] | None) -> str:
    if resolution.has_unresolved and parsed.policy.selection_mode != "ignore" and parsed.name != "selection_recommendation":
        return "not_found" if all(item.status == "NOT_FOUND" or item.resolved for item in resolution.requested_entities) else "clarification"
    if parsed.name in {"company_product_count", "product_price"}:
        return "simple_fact" if parsed.name == "company_product_count" or products else "clarification"
    if parsed.name == "product_detail" and parsed.policy.requested_dimensions == ("risk",):
        return "simple_fact" if products else "clarification"
    if parsed.name in {"compliance_policy", "recommendation_policy"}:
        return "policy_answer"
    if parsed.name == "product_filter":
        return "filter_result"
    if parsed.name in {"profit_comparison", "product_comparison"}:
        return "comparison_result" if len(products) >= 2 else "clarification"
    if parsed.name == "selection_recommendation":
        return "decision_report"
    if data_sufficiency and data_sufficiency.get("status") == "INSUFFICIENT_DATA":
        return "insufficient_data"
    return "product_detail" if products else "clarification"


def _display_scope(response_type: str, dimensions: tuple[str, ...]) -> list[str]:
    if response_type == "simple_fact":
        return ["answer", "fact", "source", "timestamp"]
    if response_type == "policy_answer":
        return ["answer", "policy", "source"]
    if response_type in {"clarification", "not_found"}:
        return ["answer", "requested_entities"]
    if response_type == "insufficient_data":
        return ["answer", "product", "data_sufficiency", "evidence", "warnings", "source", "tool_summary"]
    if response_type == "filter_result":
        return ["answer", "filter_criteria", "products", *dimensions, "evidence", "source", "tool_summary"]
    if response_type == "comparison_result":
        return ["answer", "products", *dimensions, "hard_gates", "evidence", "warnings", "source", "tool_summary"]
    if response_type == "decision_report":
        return ["answer", "products", "recommendation", "decision", "evidence", "warnings", "missing_data", "next_actions", "human_review", "source", "tool_summary"]
    return ["answer", "product", *dimensions, "evidence", "warnings", "missing_data", "next_actions", "human_review", "source", "tool_summary"]


def _source_name(lineage_value: Any, fallback: str) -> str:
    if isinstance(lineage_value, dict):
        return str(lineage_value.get("provider") or lineage_value.get("source") or fallback)
    return str(lineage_value or fallback)


def _scoped_risk_message(view: dict[str, Any], item: dict[str, Any]) -> str:
    code = str(item.get("code") or "")
    message = str(item.get("message") or "")
    if code in {"STATIC_SALES_GROWTH_DECLINE", "DECLINING_DEMAND"} or "销量趋势明显下降" in message:
        lineage = view.get("field_lineage") or {}
        source = _source_name(lineage.get("sales_growth_rate"), str(view.get("sales_source") or "product_master"))
        return f"{view.get('title')}：企业报告记录销量增速 {float(view.get('sales_growth_rate') or 0):.1f}%（来源：{source}）；该指标不是系统快照趋势。"
    return f"{view.get('title')}：{message}"


def _tool_result_contract(executions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    contracted: list[dict[str, Any]] = []
    for item in executions:
        raw = item.get("result") or {}
        data = raw.get("data")
        if isinstance(data, list):
            summary: dict[str, Any] = {
                "success": bool(raw.get("success")),
                "matched_count": int(raw.get("matched_count", len(data))),
                "displayed_count": int(raw.get("displayed_count", len(data))),
                "product_ids": [str(row.get("id") or row.get("product_id")) for row in data if isinstance(row, dict) and (row.get("id") or row.get("product_id"))],
            }
        elif isinstance(data, dict):
            summary = {"success": bool(raw.get("success"))}
            for key in ("count", "product_id", "snapshot_count", "snapshot_trend", "data_sufficiency"):
                if key in data:
                    summary[key] = data[key]
            if "id" in data:
                summary["product_id"] = data["id"]
        else:
            summary = {"success": bool(raw.get("success"))}
        if raw.get("error_code"):
            summary["error_code"] = raw["error_code"]
        contracted.append({
            "tool_name": str(item.get("tool_name") or ""),
            "normalized_args": item.get("normalized_args") or {},
            "status": str(item.get("status") or "unknown"),
            "result": summary,
        })
    return contracted


def _overall_decision_status(intent: str, products: list[dict[str, Any]]) -> str:
    if intent == "compliance_policy":
        return "BLOCKED"
    if intent == "recommendation_policy":
        return "POLICY_ONLY"
    if not products:
        return "NOT_APPLICABLE"
    statuses = [str(item.get("decision_status") or "REVIEW_REQUIRED") for item in products]
    if len(set(statuses)) == 1:
        return statuses[0]
    if "BLOCKED" in statuses:
        return "MIXED_WITH_BLOCKED"
    if "RECOMMENDED" in statuses:
        return "MIXED"
    return "REVIEW_REQUIRED"


async def _ensure_conversation(session: AsyncSession, company_id: str, user_id: str, query: str, session_id: str | None) -> ConversationSession:
    conversation = await session.get(ConversationSession, session_id) if session_id else None
    if not conversation or conversation.company_id != company_id or conversation.user_id != user_id:
        conversation = ConversationSession(company_id=company_id, user_id=user_id, title=query[:80], goal_summary=query[:300])
        session.add(conversation)
        await session.flush()
    return conversation


async def agent_v1_ask(
    session: AsyncSession, company_id: str, user_id: str, role: str, query: str, session_id: str | None,
    selected_product_ids: list[str] | None = None, selection_revision: int = 0, selection_bound_session_id: str | None = None,
    record_user_message: bool = True, response_mode: str = "rule_engine",
    fallback_reason: str | None = None, provider_notice: str = "", trace_prefix: list[dict[str, Any]] | None = None,
    active_model_override: str | None = None, token_usage_override: dict[str, Any] | None = None, model_summary: str = "",
    active_provider_override: str | None = None, reasoning_handler=None,
    fallback_used_override: bool | None = None,
    requested_provider_override: str | None = None, fallback_provider_override: str | None = None,
    model_route_override: str | None = None, provider_calls_override: list[dict[str, Any]] | None = None,
    run_state: AgentRunState | None = None,
    understanding_override=None, parsed_override=None, semantic_planner=None,
) -> dict:
    started = time.perf_counter()
    run_id = uid()
    repo = ProductRepository(session, company_id)
    trace: list[dict[str, Any]] = list(trace_prefix or [])
    conversation = await _ensure_conversation(session, company_id, user_id, query, session_id)
    session_id = conversation.id
    if record_user_message:
        session.add(ConversationMessage(company_id=company_id, session_id=session_id, role="user", content=query, metadata_json={"selected_product_ids": selected_product_ids or [], "selection_revision": selection_revision, "selection_bound_session_id": selection_bound_session_id}))

    all_products = await repo.list(limit=500)
    context_snapshot = build_context_snapshot(
        conversation, company_id=company_id, user_id=user_id, query=query, products=all_products,
        selected_product_ids=selected_product_ids, selection_revision=selection_revision,
        selection_bound_session_id=selection_bound_session_id,
    )
    parsed, understanding = (parsed_override, understanding_override) if understanding_override is not None else understand_query(query, all_products, selected_product_ids, context_snapshot)
    if understanding.route == "SEMANTIC_PLANNER" and not understanding.planner_calls:
        if semantic_planner is not None:
            packet = build_semantic_context_packet(query, understanding, context_snapshot, all_products)
            parsed, understanding = await semantic_fallback(query, understanding, semantic_planner, selected_product_ids or (), packet)
        elif not parsed.recognized:
            parsed, understanding = await semantic_fallback(query, understanding, None, selected_product_ids or ())
        else:
            understanding = understanding.model_copy(update={"route": "DETERMINISTIC_FAST_PATH"})
    if understanding.references and not understanding.entity_mentions:
        reference_field = "current_price" if "这个价格" in query else context_snapshot.last_fact_dimension
        understanding = understanding.model_copy(update={"reference_field": reference_field})
        if "这个指标" in query and parsed.name == "provenance_fact" and reference_field:
            dimensions = ("sales_snapshot", "data_sufficiency", "provenance") if reference_field == "sales_growth_rate" else ("profit", "provenance") if reference_field == "net_margin" else ("price", "provenance")
            parsed = semantic_policy(parsed.name, dimensions, selected_product_ids or ())
            understanding = understanding.model_copy(update={"requested_dimensions": list(dimensions), "metrics": [reference_field]})
    state = run_state or AgentRunState(parsed.policy)
    if not state.executions:
        state.policy = parsed.policy  # A resolved reference may narrow/extend its field tools.
    arbitration = (
        arbitrate_entity_mentions(all_products, understanding, query)
        if parsed.policy.selection_mode != "ignore"
        else None
    )
    resolution = arbitration.resolution if arbitration else EntityResolution()
    entity_scoped = parsed.policy.selection_mode != "ignore" and parsed.name != "selection_recommendation"
    explicit_ids = [item.id for item in resolution.products]
    context_resolution = resolve_context_reference(query, understanding, explicit_ids, context_snapshot)
    entity_blocked = entity_scoped and (resolution.has_unresolved or context_resolution.requires_clarification)
    context_source = context_resolution.source
    target_source = {
        "explicit_query": "query_entity", "ui_selection": "explicit_selection",
        "last_explicit_entity": "session_reference", "last_comparison": "session_reference",
        "last_resolved_entity": "session_reference", "ordinal_reference": "session_reference",
        "session_state": "session_reference",
    }.get(context_source, "none")
    by_id = {item.id: item for item in all_products}
    targets = [by_id[item] for item in context_resolution.product_ids if item in by_id]

    event(trace, "query_understood", "query_understanding", "已拆分问题意图、实体、指代和指标；业务事实仍由后端提供。", understanding=understanding.model_dump(mode="json"))

    event(trace, "planning_finished", "planner", f"已识别业务意图：{parsed.name}。", plan=list(parsed.plan), requested_dimensions=list(parsed.policy.requested_dimensions), selection_source=target_source)

    async def execute(tool_name: str, arguments: dict[str, Any], runner: Callable[[], Awaitable[dict[str, Any]]], summary: str) -> dict:
        spec = TOOL_REGISTRY.require(tool_name)
        normalized = spec.input_schema.model_validate(arguments).model_dump()

        async def validated_runner() -> dict[str, Any]:
            try:
                raw = await asyncio.wait_for(runner(), timeout=spec.timeout_seconds)
            except TimeoutError:
                return tool_error(tool_name, "TOOL_TIMEOUT", "工具执行超时，请稍后重试。")
            except Exception:
                # Boundary catch: retain a failed run entry, never expose driver/DB traceback.
                return tool_error(tool_name, "BACKEND_ERROR", "后端读取失败，本次未生成业务事实。")
            try:
                return spec.output_schema.model_validate(raw).model_dump(mode="python")
            except ValidationError:
                return tool_error(tool_name, "INVALID_TOOL_RESULT", "工具返回结果未通过后端 Schema 校验。")

        result, reused = await state.execute(tool_name, normalized, validated_runner)
        if reused:
            event(trace, "tool_reused", tool_name, f"复用本次任务已完成结果：{summary}", success=result.get("success", False))
        else:
            event(trace, "tool_finished", tool_name, summary, success=result.get("success", False), error_code=result.get("error_code"))
        return result

    views: list[dict[str, Any]] = []
    simulation: dict[str, Any] | None = None
    data_sufficiency: dict[str, Any] | None = None
    historical_matches: list[dict[str, Any]] = []
    matched_count_override: int | None = None
    total_count_override: int | None = None
    displayed_count_override: int | None = None
    filter_criteria: dict[str, Any] = {}
    event(
        trace, "entity_resolution", "entity_resolver", "已逐项保存本轮商品名称解析结果。",
        requested_entities=[{
            "status": item.status,
            "resolved": item.resolved,
            "candidate_count": len(item.candidates),
        } for item in resolution.requested_entities],
        entity_candidate_count=len(understanding.entity_mentions),
        candidate_role=list(arbitration.candidate_roles) if arbitration else [],
        candidate_resolution_status=list(arbitration.candidate_statuses) if arbitration else [],
        context_target_source=context_source,
        ignored_semantic_candidate_count=arbitration.ignored_semantic_candidate_count if arbitration else 0,
    )
    if entity_blocked:
        event(trace, "entity_resolution_blocked", "entity_resolver", "存在未解析实体，本次不执行商品工具或部分比较。")
    elif parsed.name == "company_product_count":
        result = await execute("count_products", {}, lambda: count_products(repo, role), "已统计当前企业商品主档总数。")
        matched_count_override = int((result.get("data") or {}).get("count") or 0)
    elif parsed.name in {"compliance_policy", "recommendation_policy", "data_quality_policy", "unknown"}:
        pass
    elif parsed.name == "data_quality_answer":
        for target in targets[:10]:
            detail = await execute("get_product", {"product_id": target.id}, lambda target=target: get_product(repo, target.id, role), f"已读取 {target.title} 的字段时效证据。")
            if detail.get("success"):
                views.append(detail["data"])
    elif parsed.name == "product_filter":
        args = {**parsed.filters, "limit": parsed.limit}
        result = await execute("filter_products", args, lambda: filter_products(repo, role, **args), "已按用户明确条件完成确定性筛选。")
        views = result.get("data", []) if result.get("success") else []
        if result.get("success"):
            filter_criteria = result.get("criteria") or {}
            matched_count_override = int(result.get("matched_count", len(views)))
            total_count_override = int(result.get("total_count", matched_count_override))
            displayed_count_override = int(result.get("displayed_count", len(views)))
    elif parsed.name == "selection_recommendation":
        if targets:
            ids = [item.id for item in targets[:10]]
            selected_result = await execute("compare_products", {"product_ids": ids}, lambda: compare_products(repo, ids, role), "已读取明确选择的候选并进行确定性排序。")
            views = selected_result.get("data", []) if selected_result.get("success") else []
            views.sort(key=lambda item: float((item.get("analysis") or {}).get("recommendation_score") or 0), reverse=True)
            views = views[:parsed.limit]
        else:
            args = {**parsed.filters, "limit": parsed.limit}
            result = await execute("filter_products", args, lambda: filter_products(repo, role, **args), "已生成当前候选的确定性优先级。")
            views = result.get("data", []) if result.get("success") else []
            if result.get("success"):
                filter_criteria = result.get("criteria") or {}
                total_count_override = int(result.get("total_count", len(views)))
    elif parsed.name in {"profit_comparison", "product_comparison"}:
        ids = [item.id for item in targets][:parsed.limit]
        if len(ids) >= 2:
            result = await execute("compare_products", {"product_ids": ids}, lambda: compare_products(repo, ids, role), "已读取明确解析的比较商品。")
            views = result.get("data", []) if result.get("success") else []
            if parsed.name == "profit_comparison":
                for view in views:
                    product_id = str(view["id"])
                    profit_result = await execute("calculate_profit", {"product_id": product_id}, lambda product_id=product_id: calculate_profit(repo, product_id, role), f"已核算 {view['title']} 的利润与 ROI。")
                    merge_profit_calculation(view, profit_result)
    elif parsed.name == "product_price":
        if len(targets) == 1:
            target = targets[0]
            result = await execute("get_product", {"product_id": target.id}, lambda: get_product(repo, target.id, role), f"已读取 {target.title} 的价格。")
            if result.get("success"):
                views = [result["data"]]
    else:
        target = targets[0] if len(targets) == 1 else None
        if target and parsed.proposed_price is not None:
            detail = await execute("get_product", {"product_id": target.id}, lambda: get_product(repo, target.id, role), f"已读取 {target.title}。")
            if detail.get("success"):
                views = [detail["data"]]
            result = await execute("simulate_price_change", {"product_id": target.id, "proposed_price": parsed.proposed_price}, lambda: simulate_price_change(repo, target.id, float(parsed.proposed_price), role), "价格情景模拟完成。")
            simulation = result.get("data") if result.get("success") else None
        elif target and "sales_snapshot" in parsed.policy.requested_dimensions:
            detail = await execute("get_product", {"product_id": target.id}, lambda: get_product(repo, target.id, role), f"已读取 {target.title}。")
            if detail.get("success"):
                views = [detail["data"]]
            trend = await execute("get_sales_trend", {"product_id": target.id}, lambda: get_sales_trend(repo, target.id, role), "已核验销量快照数量与趋势充分性。")
            data_sufficiency = (trend.get("data") or {}).get("data_sufficiency") if trend.get("success") else None
        elif target and "history" in parsed.policy.requested_dimensions:
            view = product_view(target, await repo.latest_analysis(target.id))
            if not view.get("analysis"):
                view["analysis"] = to_dict(analyze(target))
            views = [view]
            result = await execute("find_historical_failures", {"product_id": target.id}, lambda: find_historical_failures(session, company_id, target, role), f"已查询 {target.title} 的相似失败案例。")
            historical_matches.extend(result.get("data", []))
            memory = await execute("search_company_memory", {"query": query}, lambda: search_company_memory(session, company_id, query, role), "已检索企业保存的失败原因。")
            historical_matches.extend({"title": "企业记忆", "reason": item.get("content", "")} for item in memory.get("data", []))
        elif target:
            detail = await execute("get_product", {"product_id": target.id}, lambda: get_product(repo, target.id, role), f"已读取 {target.title}。")
            if detail.get("success"):
                views = [detail["data"]]
            if "profit" in parsed.policy.requested_dimensions:
                profit_result = await execute("calculate_profit", {"product_id": target.id}, lambda: calculate_profit(repo, target.id, role), f"已核算 {target.title} 的利润与 ROI。")
                if views:
                    merge_profit_calculation(views[0], profit_result)

    products = [_agent_product(view) for view in views]
    response_type = _response_type(parsed, resolution, products, data_sufficiency)
    if context_resolution.requires_clarification and parsed.policy.selection_mode != "ignore":
        response_type = "clarification"
    display_scope = _display_scope(response_type, parsed.policy.requested_dimensions)
    missing_data: list[dict[str, Any]] = []
    warning_messages: list[str] = []
    actions: list[str] = []
    evidence: list[dict[str, Any]] = []
    trust_notices = []
    for view, product in zip(views, products):
        analysis = view.get("analysis") or {}
        requested = set(parsed.policy.requested_dimensions)
        include_all_decision_evidence = response_type == "decision_report" or "detail" in requested
        for item in analysis.get("missing_data") or []:
            dimension = str(item.get("dimension") or "")
            if response_type != "simple_fact" and (include_all_decision_evidence or dimension in requested or (dimension == "profit" and "roi" in requested)):
                missing_data.append({"product_id": view.get("id"), "title": view.get("title"), **item})
                actions.append(str(item.get("action") or ""))

        if response_type == "simple_fact" and parsed.name == "product_price":
            evidence.append({
                "product_id": product["id"], "title": product["title"], "source": product["price_source"],
                "summary": f"当前售价 {product['current_price']:g} {product['currency']}，来自商品主档价格字段。", "url": product["url"],
            })
        elif response_type == "simple_fact" and requested == {"risk"}:
            evidence.append({"product_id": product["id"], "title": product["title"], "source": "deterministic_evaluation_engine", "summary": f"当前风险等级：{product['risk_level']}；来自当前保存的商品分析或后端确定性评估。", "url": product["url"]})
        elif response_type == "filter_result":
            conditions = []
            if filter_criteria.get("min_margin_rate") is not None: conditions.append(f"净利率 {product['current_margin_rate']:.1%}")
            if filter_criteria.get("risk_level"): conditions.append(f"风险 {product['risk_level']}")
            if filter_criteria.get("compliance_status"): conditions.append(f"合规 {view.get('compliance_status')}")
            if filter_criteria.get("min_score") is not None: conditions.append(f"推荐度 {product['score']:.1f}")
            evidence.append({
                "product_id": product["id"], "title": product["title"], "source": "deterministic_filter_result",
                "summary": "；".join(conditions) or "符合本次结构化筛选条件。", "url": product["url"],
            })
        elif response_type == "comparison_result" and parsed.name == "profit_comparison":
            evidence.append({
                "product_id": product["id"], "title": product["title"], "source": "deterministic_profit_engine",
                "summary": f"净利润 {product['net_profit']:.2f} {product['currency']}，净利率 {product['current_margin_rate']:.1%}，ROI {product['roi']:.1%}。", "url": product["url"],
            })
            if product["decision_status"] == "BLOCKED":
                warning_messages.append(f"{product['title']}：合规未通过，任何利润排名都不能绕过上架阻断。")
        elif response_type == "comparison_result":
            values = {"identity": product["title"], "price": f"售价 {product['current_price']:g} {product['currency']}", "profit": f"净利率 {product['current_margin_rate']:.1%}", "roi": f"ROI {product['roi']:.1%}", "demand": f"需求评分 {product['demand_score']:g}", "competition": f"竞争评分 {product['competition_score']:g}", "compliance": f"合规状态 {product['compliance_status']}", "risk": f"风险等级 {product['risk_level']}"}
            evidence.append({"product_id": product["id"], "title": product["title"], "source": "deterministic_comparison_result", "summary": "；".join(values[key] for key in parsed.policy.requested_dimensions if key in values), "url": product["url"]})
        elif response_type == "insufficient_data":
            evidence.append({
                "product_id": product["id"], "title": product["title"], "source": "product_snapshot_repository",
                "summary": f"已保存 {int((data_sufficiency or {}).get('snapshot_count', 0))} 次销量快照，其中有效 {int((data_sufficiency or {}).get('valid_sales_snapshot_count', 0))} 次。", "url": product["url"],
            })
        else:
            evidence.append({
                "product_id": product["id"], "title": product["title"], "source": "company_product_database",
                "summary": f"推荐度 {product['score']:.1f}，完整度 {product['completeness']}%，风险 {product['risk_level']}。", "url": product["url"],
            })
            if response_type in {"decision_report", "product_detail"} and (include_all_decision_evidence or "risk" in requested):
                warning_messages.extend(_scoped_risk_message(view, item) for item in analysis.get("risks") or [])

        trust = view.get("data_trust") or {}
        fields = scoped_fields(trust, parsed.policy.requested_dimensions, decision=include_all_decision_evidence, criteria=filter_criteria)
        # Existing response scopes stay intact. These are source caveats, not new business analyses.
        evidence[-1]["fields"] = fields
        evidence[-1]["disclosure"] = trust.get("disclosure", "来源未知。")
        trust_notices.extend(item.model_dump() for item in evidence_notices([FieldEvidence.model_validate(item) for item in fields]))
        if product["is_mock"]:
            evidence[-1]["url"] = ""  # Ozon search URL is a reference, not proof of a mock price.
        if response_type == "simple_fact" and fields:
            evidence[-1]["source"] = fields[0]["provider"]

    if data_sufficiency and data_sufficiency.get("status") == "INSUFFICIENT_DATA":
        reported = data_sufficiency.get("reported_metric")
        source = data_sufficiency.get("reported_metric_source") or "not_provided"
        snapshot_count = int(data_sufficiency.get("snapshot_count") or 0)
        if reported is not None and source != "not_provided":
            warning_messages.append(f"企业报告记录销量增速 {float(reported):.1f}%（来源：{source}），但当前只有 {snapshot_count} 次销量快照，无法据此验证快照趋势。")
        if data_sufficiency.get("stale"):
            warning_messages.append(f"最近一次销量快照已超过 {SALES_SNAPSHOT_FRESHNESS_DAYS} 天，不能代表当前销量趋势。")

    recommended = [item for item in products if item["decision_status"] == "RECOMMENDED"]
    decision_summary = {
        "ranked_count": len(products), "recommended_count": len(recommended),
        "blocked_count": sum(item["decision_status"] == "BLOCKED" for item in products),
        "formal_recommendation": "AVAILABLE" if recommended else "NONE",
        "statuses": {item["id"]: item["decision_status"] for item in products},
    }
    if entity_blocked:
        if resolution.has_unresolved:
            answer = resolution_message(resolution, comparison=parsed.name in {"profit_comparison", "product_comparison"})
        elif context_resolution.failure_code == "MISSING_UI_SELECTION":
            answer = "当前没有可引用的已选商品，请先在对比中心选择商品，或直接说出商品名称。"
        elif context_resolution.failure_code == "ORDINAL_OUT_OF_RANGE":
            answer = "当前会话没有对应序号的比较商品，请先完成一次明确的商品比较。"
        elif context_resolution.failure_code == "MISSING_CONTEXT" and (
            understanding.metric == "net_margin"
            or "net_margin" in understanding.metrics
            or understanding.reference_field == "net_margin"
        ):
            answer = "请告诉我你想查看哪个商品的净利率。"
        elif context_resolution.failure_code == "MISSING_CONTEXT" and "profit" in understanding.requested_dimensions:
            answer = "请告诉我你想查看哪个商品的利润或净利率。"
        else:
            answer = "当前会话中没有唯一可引用的商品，请直接说出商品名称。"
    elif parsed.name == "company_product_count":
        answer = f"目前公司商品主档一共有 {matched_count_override or 0} 个。"
    elif parsed.name == "compliance_policy":
        answer = "不能直接上架。合规未通过属于硬性阻断（BLOCKED），必须补齐材料并由人工重新审核通过后，才可进入上架流程。"
    elif parsed.name == "recommendation_policy":
        answer = "不是。相对排名第一只表示候选集中表现最好，不代表达到正式推荐标准。评分、相对排名、正式推荐状态和最终上架决策彼此独立；合规失败、证据不足或高风险均不能被‘必须推荐一个’绕过。"
    elif parsed.name == "product_filter":
        answer = f"符合全部条件的商品共有 {matched_count_override or 0} 个，清单与数量来自同一次后端 AND 筛选结果。"
    elif parsed.name == "selection_recommendation":
        if not products:
            answer = "当前没有符合筛选条件的候选商品。"
        elif recommended:
            answer = f"正式达到推荐标准的商品有 {len(recommended)} 个；当前第一名是 {products[0]['title']}（{products[0]['score']:.1f} 分）。"
        else:
            answer = f"相对排名第一的是 {products[0]['title']}（{products[0]['score']:.1f} 分），但当前无商品达到正式推荐标准。"
    elif parsed.name in {"profit_comparison", "product_comparison"}:
        labels = {"profit": "利润", "roi": "ROI", "demand": "需求", "competition": "竞争", "compliance": "合规", "risk": "风险", "identity": "商品身份", "price": "价格"}
        dimension = "、".join(labels.get(key, key) for key in parsed.policy.requested_dimensions)
        if products and parsed.name == "profit_comparison" and re.search(r"谁.*(?:利润|赚).*(?:好|高|多)|利润.*(?:最好|最高)", query):
            leader = max(products, key=lambda item: (item["net_profit"], item["current_margin_rate"], item["id"]))
            answer = f"按后端当前利润计算，{leader['title']} 的单件净利润最高，为 {leader['net_profit']:.2f} {leader['currency']}；本次只比较利润与 ROI，不代表推荐上架。"
        else:
            answer = f"已完成 {len(products)} 个明确商品的{dimension}对比；按本次请求的维度展示，不代表推荐上架。" if products else "请点名或选择至少两个不同的本企业商品后再比较。"
    elif parsed.name == "product_price":
        answer = f"{products[0]['title']} 当前售价为 {products[0]['current_price']:g} {products[0]['currency']}。" if products else "请提供要查询的商品名称或商品 ID。"
    elif products and response_type == "simple_fact" and parsed.policy.requested_dimensions == ("risk",):
        risk_label = {"low": "低", "medium": "中", "high": "高"}.get(products[0]["risk_level"], "未知")
        answer = f"{products[0]['title']} 当前风险等级为 {risk_label}（{products[0]['risk_level']}）。"
    elif simulation:
        answer = f"售价调整为 {simulation['proposed_price']:g} RUB 后，单件净利润为 {simulation['net_profit']:.2f} RUB，净利率为 {simulation['margin_rate']:.1%}，推荐等级为 {simulation['recommendation_grade']}。"
    elif data_sufficiency:
        status = data_sufficiency.get("status")
        count = int(data_sufficiency.get("snapshot_count") or 0)
        observed = str(data_sufficiency.get("observed_trend") or "INSUFFICIENT_DATA")
        labels = {"GROWING": "上涨", "DECLINING": "下降", "FLAT": "持平"}
        answer = f"{products[0]['title']} 当前有 {count} 次销量快照，证据不足，无法判断上涨或下降；快照趋势状态为 INSUFFICIENT_DATA。" if products and status == "INSUFFICIENT_DATA" else f"{products[0]['title']} 当前有 {count} 次销量快照，系统观察到的快照趋势为{labels.get(observed, observed)}（{observed}）。"
    elif historical_matches:
        answer = f"企业历史中找到 {len(historical_matches)} 条相关失败记录，失败原因需结合当前证据人工复核。"
        warning_messages.extend(f"历史案例 {item.get('title', '')}：{item.get('reason', '')}" for item in historical_matches[:5])
    elif products and "profit" in parsed.policy.requested_dimensions:
        answer = f"{products[0]['title']} 单件净利润为 {products[0]['net_profit']:.2f} RUB，净利率 {products[0]['current_margin_rate']:.1%}，ROI {products[0]['roi']:.1%}。"
    elif products:
        answer = f"{products[0]['title']} 当前动态推荐度为 {products[0]['score']:.1f}，决策状态为 {products[0]['decision_status']}；最终动作仍需人工审核。"
    else:
        answer = "请提供要查询的商品名称或商品 ID，或明确引用已选商品。"

    source_badge = (
        "GLM 实际调用成功" if response_mode == "glm_success" else
        f"{active_provider_override or '语义模型'} 实际调用成功" if response_mode == "model_success" else
        "确定性 Planner 回退" if response_mode == "deterministic_fallback" else "规则引擎结果"
    )
    latency_ms = round((time.perf_counter() - started) * 1000)
    matched_count = matched_count_override if matched_count_override is not None else len(products)
    total_count = total_count_override if total_count_override is not None else matched_count
    displayed_count = displayed_count_override if displayed_count_override is not None else len(products)
    clarification_code: str | None = None
    if response_type == "clarification":
        if context_resolution.requires_clarification:
            clarification_code = context_resolution.failure_code or "MISSING_CONTEXT"
        elif resolution.has_unresolved:
            statuses = {item.status for item in resolution.requested_entities if not item.resolved}
            if "AMBIGUOUS" in statuses:
                clarification_code = "ENTITY_AMBIGUOUS"
            elif "LOW_CONFIDENCE" in statuses:
                clarification_code = "ENTITY_LOW_CONFIDENCE"
        elif understanding.clarification_required and not understanding.failure_code:
            clarification_code = "USER_CLARIFICATION_REQUIRED"
    fact = None
    if parsed.name == "company_product_count":
        fact = {"name": "company_product_count", "value": matched_count, "unit": "products", "source": "company_product_database"}
    elif parsed.name == "product_price" and products:
        product = products[0]
        fact = {"name": "product_price", "value": product["current_price"], "unit": "price", "product_id": product["id"], "currency": product["currency"], "source": product["price_source"], "timestamp": product["updated_at"]}
    elif response_type == "simple_fact" and products and parsed.policy.requested_dimensions == ("risk",):
        product = products[0]
        fact = {"name": "product_risk_level", "value": product["risk_level"], "product_id": product["id"], "source": "deterministic_evaluation_engine", "timestamp": product["updated_at"]}
    if fact and fact.get("product_id") and evidence and evidence[0].get("fields"):
        fact["provenance"] = evidence[0]["fields"][0]
        fact["source"] = fact["provenance"]["provider"]
    if response_type == "decision_report" and trust_notices:
        answer += " 本次结论须结合来源说明使用；演示、过期或来源不足的数据不能视为已验证的当前市场事实。"
    entities = [{"product_id": item.id, "name": item.title, "resolution_method": target_source} for item in targets if target_source != "none"]
    decision_status = "NOT_APPLICABLE" if response_type == "simple_fact" else _overall_decision_status(parsed.name, products)
    if response_type == "simple_fact":
        decision_summary = {}
    human_review_required = (
        parsed.name == "compliance_policy"
        or response_type == "decision_report"
        or (response_type == "product_detail" and decision_status in {"BLOCKED", "INSUFFICIENT_DATA", "HUMAN_REVIEW_REQUIRED", "REVIEW_REQUIRED"})
    )
    scoped_actions = _unique(actions)
    if response_type == "decision_report" and not scoped_actions:
        scoped_actions = ["查看商品证据后，由企业审核人决定是否进入下一阶段。"]
    payload = {
        "run_id": run_id, "session_id": session_id, "response_type": response_type, "answer": answer,
        "understanding": understanding.model_dump(mode="json"),
        "matched_count": matched_count, "total_count": total_count, "displayed_count": displayed_count,
        "entities": entities, "requested_entities": [item.model_dump() for item in resolution.requested_entities], "product_ids": [item["id"] for item in products], "filter_criteria": filter_criteria, "fact": fact,
        "products": products, "tool_results": _tool_result_contract(state.executions), "evidence": evidence,
        "risks": _unique(warning_messages), "warnings": _unique(warning_messages), "trust_notices": trust_notices, "missing_data": missing_data, "next_actions": scoped_actions,
        "requires_human_review": human_review_required, "human_review_required": human_review_required,
        "tool_trace_summary": _trace_summary(trace), "response_mode": response_mode, "source_badge": source_badge,
        "active_provider": active_provider_override or ("zhipu" if response_mode == "glm_success" else "deterministic_planner"), "active_model": active_model_override or ("glm" if response_mode == "glm_success" else "agent-v3-rule-planner"),
        "requested_provider": requested_provider_override or ("zhipu" if response_mode == "glm_success" else "deterministic_planner"),
        "fallback_provider": fallback_provider_override,
        "model_route": model_route_override or ("QWEN_SEMANTIC" if response_mode == "model_success" else "DETERMINISTIC_FAST_PATH"),
        "provider_calls": provider_calls_override or [],
        "fallback_reason": fallback_reason, "clarification_code": clarification_code,
        "latency_ms": latency_ms, "token_usage": token_usage_override or {}, "intent": parsed.name,
        "task_completed": response_type not in {"clarification", "not_found", "error"},
        "fallback_used": response_mode == "deterministic_fallback" if fallback_used_override is None else fallback_used_override,
        "tool_call_count": state.actual_tool_calls,
        "duplicate_tool_execution": state.duplicate_tool_execution, "requested_dimensions": list(parsed.policy.requested_dimensions), "display_scope": display_scope, "selection_source": target_source, "context_source": context_source,
        "decision_status": decision_status, "decision_summary": decision_summary, "data_sufficiency": data_sufficiency,
        "mode": "provider_semantic_handoff" if response_mode == "model_success" else "zhipu_controlled_function_calling" if response_mode == "glm_success" else "agent_v3_deterministic_fallback" if response_mode == "deterministic_fallback" else "agent_v3_rule_engine",
        "conclusion": answer, "trace": trace, "simulation": simulation, "data_completeness": (views[0].get("analysis") or {}).get("evidence_completeness") if views else None,
        "provider_notice": provider_notice, "model_summary": model_summary,
    }
    from app.agent.semantic_response import scope_semantic_response
    scope_semantic_response(payload, views, understanding, entity_blocked)
    failed_steps = [item for item in state.executions if item["status"] == "failed" and not any(
        done["status"] == "success" and done["tool_name"] == item["tool_name"] and done["normalized_args"] == item["normalized_args"]
        for done in state.executions)]
    if failed_steps:
        payload.update(response_type="error", task_completed=False, answer="后端工具未能完整返回数据，本次无法给出可靠结果，请稍后重试。", fact=None)
        payload["conclusion"] = payload["answer"]
    if payload["evidence"]:
        event(trace, "evidence_bound", "provenance", "已绑定本次结论引用的字段证据；来源由后端提供。", evidence_ids=[field["evidence_id"] for item in payload["evidence"] for field in item.get("fields", [])])
    validated = validate_agent_answer(payload)
    if reasoning_handler is not None:
        reasoning = await reasoning_handler(validated)
        if reasoning:
            if reasoning.get("provider_call"):
                validated["provider_calls"] = [*validated.get("provider_calls", []), reasoning["provider_call"]]
            if reasoning.get("failed"):
                event(trace, "reasoning_failed", str(reasoning["provider_call"]["actual_provider"]), "辅助推理失败；保留后端确定性解释与门禁。", failure_code=reasoning["provider_call"].get("failure_code"))
                validated["trace"] = trace
                validated = validate_agent_answer(validated)
                reasoning = None
        if reasoning:
            event(trace, "reasoning_finished", str(reasoning["provider"]), "复杂解释已基于后端验证事实生成；未改变后端决策状态。")
            validated["trace"] = trace
            validated["model_summary"] = str(reasoning["summary"])
            validated["provider_notice"] = (validated.get("provider_notice") or "") + f" {reasoning['provider']} 仅提供基于已验证事实的辅助解释。"
            validated["active_provider"] = f"{validated['active_provider']}+{reasoning['provider']}"
            validated["active_model"] = f"{validated['active_model']}+{reasoning['model']}"
            validated["token_usage"] = {
                "semantic": validated.get("token_usage") or {},
                "reasoning": reasoning.get("usage") or {},
            }
            validated = validate_agent_answer(validated)
    # Persist typed references only after the validated run; never copy business facts into session state.
    previous_state = conversation.state_json or {}
    successful_context = bool(validated["task_completed"] and not entity_blocked)
    last_explicit_ids = list(previous_state.get("last_explicit_product_ids") or [])
    if successful_context and explicit_ids:
        last_explicit_ids = explicit_ids[:10]
    last_resolved_ids = list(previous_state.get("last_resolved_product_ids") or conversation.last_product_ids or [])
    if successful_context and targets:
        last_resolved_ids = [item.id for item in targets[:10]]
    comparison_ids = list(previous_state.get("last_comparison_product_ids") or [])
    comparison_order = list(previous_state.get("last_comparison_order") or [])
    if successful_context and parsed.name in {"profit_comparison", "product_comparison"} and len(targets) >= 2:
        comparison_order = [item.id for item in targets[:10]]
        comparison_ids = list(comparison_order)
    conversation.last_product_ids = last_resolved_ids
    conversation.conclusion_summary = validated["answer"][:1000]
    previous_dimension = previous_state.get("last_fact_dimension")
    current_dimension = understanding.metrics[0] if products and understanding.metrics else previous_dimension
    if products and response_type == "simple_fact" and parsed.policy.requested_dimensions == ("price",):
        current_dimension = "current_price"
    elif products and "profit" in parsed.policy.requested_dimensions:
        current_dimension = "net_margin"
    tool_refs = [f"{run_id}:{index}:{item['tool_name']}" for index, item in enumerate(state.executions) if item["status"] == "success"]
    conversation.state_json = {
        "schema_version": "context-v1", "turn_index": context_snapshot.turn_index,
        "last_intent": parsed.name, "last_response_type": validated["response_type"],
        "last_product_ids": last_resolved_ids, "last_explicit_product_ids": last_explicit_ids,
        "last_resolved_product_ids": last_resolved_ids,
        "last_comparison_product_ids": comparison_ids, "last_comparison_order": comparison_order,
        "last_tool_summary": validated["tool_trace_summary"], "last_tool_result_refs": tool_refs,
        "response_mode": response_mode, "requested_dimensions": list(parsed.policy.requested_dimensions),
        "last_requested_dimensions": list(parsed.policy.requested_dimensions),
        "last_metric": current_dimension or previous_state.get("last_metric"),
        "last_fact_dimension": current_dimension,
        "last_preference_order": understanding.preference_order or previous_state.get("last_preference_order") or [],
        "last_negative_scope": understanding.negative_scope or previous_state.get("last_negative_scope") or [],
    }
    answer = validated["answer"]
    if not conversation.goal_summary:
        conversation.goal_summary = query[:300]
    execution_log = state.executions
    session.add(ConversationMessage(company_id=company_id, session_id=session_id, role="assistant", content=answer, metadata_json={"run_id": run_id, "response_type": validated["response_type"], "response_mode": response_mode, "selected_product_ids": conversation.last_product_ids, "tool_trace_summary": validated["tool_trace_summary"], "missing_data": validated["missing_data"], "warnings": validated["warnings"], "decision_summary": validated["decision_summary"]}))
    session.add(AgentRun(id=run_id, company_id=company_id, user_id=user_id, session_id=session_id, query=query, model=validated["active_model"], active_provider=validated["active_provider"], response_mode=response_mode, fallback_reason=fallback_reason or "", intent=parsed.name, prompt_version="agent-run-result-v2", tool_registry_version="v3", tools_used=[item["tool_name"] for item in execution_log if item["status"] in {"success", "failed"}], trace_json=[*trace, {"event": "run_execution_state", "executions": execution_log}], answer=answer, latency_ms=latency_ms, token_usage=token_usage_override or {}))
    await session.commit()
    return validated


rule_agent_ask = agent_v1_ask
