"""Backend-owned capability and demo-suggestion contract."""

from __future__ import annotations

from typing import Any


CAPABILITIES: tuple[dict[str, Any], ...] = (
    {
        "capability_id": "collection.build",
        "intent_family": "collection_analysis",
        "operations": ["RECOMMEND_TOP_K", "IDENTIFY_EVIDENCE_GAPS"],
        "required_context": [],
        "supported_selectors": ["recommendation_score"],
        "supported_filters": [],
        "supported_dimensions": ["recommendation", "evidence_gap"],
        "backend_handlers": ["analyze_collection"],
        "renderer": "collection_report",
        "demo_verified": True,
        "suggestion": {
            "suggestion_id": "build_candidate_collection",
            "golden_case_id": "D01",
            "title": "建立候选集合",
            "description": "先从企业候选池形成可引用的 Top-K",
            "prompt": "请分析当前候选池里最值得继续研究的三个商品方向，并说明证据缺口",
        },
    },
    {
        "capability_id": "collection.composite_compare",
        "intent_family": "collection_analysis",
        "operations": ["COMPARE_COLLECTION_MEMBERS"],
        "required_context": ["ACTIVE_COLLECTION"],
        "supported_selectors": [
            "recommendation_score", "net_profit", "net_margin", "roi", "risk",
            "competition", "demand", "compliance",
        ],
        "supported_filters": [],
        "supported_dimensions": [
            "profit", "competition", "compliance", "risk", "evidence_gap", "decision",
        ],
        "backend_handlers": ["analyze_collection"],
        "renderer": "collection_report",
        "demo_verified": True,
        "suggestion": {
            "suggestion_id": "compare_collection_top_candidates",
            "golden_case_id": "D02",
            "title": "组合优先级",
            "description": "按选择器比较候选并给出复核顺序",
            "prompt": "比较当前动态推荐度最高的三个商品，说明利润、竞争、合规和数据缺口，给出人工审核顺序。",
        },
    },
    {
        "capability_id": "collection.profit_diagnosis",
        "intent_family": "collection_analysis",
        "operations": ["ANALYZE_COLLECTION", "PROFIT_DIAGNOSIS"],
        "required_context": ["ACTIVE_COLLECTION"],
        "supported_selectors": [],
        "supported_filters": ["net_margin"],
        "supported_dimensions": ["profit", "evidence_gap", "provenance"],
        "backend_handlers": ["analyze_collection"],
        "renderer": "collection_report",
        "demo_verified": True,
        "suggestion": {
            "suggestion_id": "diagnose_collection_margin_gap",
            "golden_case_id": "D04",
            "title": "利润诊断",
            "description": "筛出未达目标净利率的候选并核对可调输入",
            "prompt": "找出当前候选池中净利率低于目标的商品，并说明可调整的价格或成本因素。",
        },
    },
    {
        "capability_id": "collection.historical_decisions",
        "intent_family": "collection_analysis",
        "operations": ["HISTORY_ANALYSIS", "HUMAN_REVIEW_PRIORITY"],
        "required_context": ["ACTIVE_COLLECTION", "HISTORICAL_DECISIONS"],
        "supported_selectors": [],
        "supported_filters": [],
        "supported_dimensions": ["decision", "evidence_gap", "provenance"],
        "backend_handlers": ["analyze_collection", "find_historical_failures"],
        "renderer": "collection_report",
        "demo_verified": True,
        "suggestion": {
            "suggestion_id": "review_collection_history",
            "golden_case_id": "D05",
            "title": "历史风险",
            "description": "核对同品牌或同类目历史决策记录",
            "prompt": "检查当前候选是否存在同品牌或同类目的历史放弃案例，并列出需要人工复核的原因。",
        },
    },
)


def capability_view(*, has_active_collection: bool, has_active_scenario: bool, history_available: bool) -> dict[str, Any]:
    context = {
        "ACTIVE_COLLECTION": has_active_collection,
        "ACTIVE_SCENARIO": has_active_scenario,
        "HISTORICAL_DECISIONS": history_available,
    }
    capabilities = []
    suggestions = []
    for definition in CAPABILITIES:
        required = list(definition["required_context"])
        missing = [item for item in required if not context.get(item, False)]
        item = {**definition, "available": not missing, "missing_context": missing}
        capabilities.append(item)
        suggestion = definition.get("suggestion")
        if not suggestion or not definition.get("demo_verified"):
            continue
        # Context-dependent prompts are not advertised before their exact
        # session-scoped prerequisite exists. Historical data is displayed as
        # disabled once a collection exists so the reason is explicit.
        collection_missing = "ACTIVE_COLLECTION" in missing
        if collection_missing:
            continue
        enabled = not missing
        suggestions.append({
            **suggestion,
            "capability_id": definition["capability_id"],
            "required_context": required,
            "enabled": enabled,
            "disabled_reason": (
                "当前没有足够历史决策数据" if "HISTORICAL_DECISIONS" in missing
                else "当前会话缺少所需上下文" if missing else ""
            ),
            "demo_verified": True,
        })
    return {
        "context": {
            "has_active_collection": has_active_collection,
            "has_active_scenario": has_active_scenario,
            "history_available": history_available,
        },
        "capabilities": capabilities,
        "suggestions": suggestions,
    }
