import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ParsedIntent:
    name: str
    filters: dict[str, Any] = field(default_factory=dict)
    limit: int = 10
    proposed_price: float | None = None
    selected_product_ids: tuple[str, ...] = ()
    plan: tuple[dict[str, str], ...] = ()


def _number_after(patterns: list[str], query: str) -> float | None:
    for pattern in patterns:
        match = re.search(pattern, query, flags=re.IGNORECASE)
        if match:
            return float(match.group(1))
    return None


def parse_intent(query: str, selected_product_ids: list[str] | None = None) -> ParsedIntent:
    """Parse business intent and deterministic filters without category hard-coding."""

    normalized = " ".join(query.strip().split())
    lowered = normalized.casefold()
    selected = tuple(dict.fromkeys(selected_product_ids or []))[:10]
    score_threshold = _number_after(
        [
            r"(?:分数|评分|推荐度).*?(\d+(?:\.\d+)?)\s*(?:分)?\s*(?:以上|及以上|达到|大于|>=)",
            r"(\d+(?:\.\d+)?)\s*(?:分)?\s*(?:以上|及以上)",
        ],
        normalized,
    )
    proposed_price = _number_after(
        [
            r"(?:售价|价格|定价).*?(?:到|为|=)\s*(\d+(?:\.\d+)?)",
            r"(\d+(?:\.\d+)?)\s*(?:rub|卢布)",
        ],
        normalized,
    )
    top_limit = _number_after(
        [
            r"(?:最值得|优先|前|top)\s*(\d+)\s*(?:个|件|款)?",
            r"选出.*?(\d+)\s*(?:个|件|款)",
        ],
        normalized,
    )
    limit = max(1, min(20, int(top_limit or 10)))
    min_margin_percent = _number_after(
        [
            r"(?:净?利润(?:率)?|毛利率).*?(\d+(?:\.\d+)?)\s*%\s*(?:以上|及以上|大于|>=)?",
            r"(?:净?利润(?:率)?|毛利率).*?(?:达到|大于|>=)\s*(\d+(?:\.\d+)?)",
        ],
        normalized,
    )
    max_saturation = _number_after(
        [
            r"(?:竞争|饱和度).*?(?:低于|小于|不超过|<=)\s*(\d+(?:\.\d+)?)",
            r"(?:竞争|饱和度).*?(\d+(?:\.\d+)?)\s*(?:以下|以内)",
        ],
        normalized,
    )

    if proposed_price is not None and any(token in lowered for token in ("价格", "售价", "定价", "rub", "卢布")):
        return ParsedIntent(
            "price_simulation",
            limit=1,
            proposed_price=proposed_price,
            selected_product_ids=selected,
            plan=(
                {"tool": "get_product", "purpose": "读取明确选择或问题中匹配的商品"},
                {"tool": "simulate_price_change", "purpose": "由后端按目标售价重算利润与风险"},
            ),
        )
    if any(token in lowered for token in ("失败", "放弃", "历史案例", "历史上")):
        return ParsedIntent(
            "historical_failure",
            selected_product_ids=selected,
            plan=(
                {"tool": "find_historical_failures", "purpose": "匹配本企业历史放弃商品"},
                {"tool": "search_company_memory", "purpose": "检索已保存的失败原因"},
            ),
        )
    if len(selected) >= 2 or any(token in lowered for token in ("比较", "对比", "哪个更", "这两个", "这些商品")):
        return ParsedIntent(
            "comparison",
            limit=max(2, min(10, len(selected) or 5)),
            selected_product_ids=selected,
            plan=(
                {"tool": "compare_products", "purpose": "读取同一商品主档下的四维分析"},
                {"tool": "calculate_profit", "purpose": "由后端核算每个商品利润"},
                {"tool": "analyze_competition", "purpose": "核对竞争证据完整度"},
            ),
        )
    if any(token in lowered for token in ("数据不完整", "缺数据", "缺失", "不能进入最终审核", "不能审核")):
        return ParsedIntent(
            "incomplete_products",
            filters={"completeness": "incomplete", "sort_by": "completeness", "sort_direction": "asc"},
            limit=100,
            selected_product_ids=selected,
            plan=({"tool": "filter_products", "purpose": "筛选证据不完整且不能进入最终审核的商品"},),
        )
    if score_threshold is not None and min_margin_percent is None:
        return ParsedIntent(
            "score_filter",
            filters={"min_score": score_threshold, "sort_by": "recommendation_score", "sort_direction": "desc"},
            limit=100,
            selected_product_ids=selected,
            plan=({"tool": "filter_products", "purpose": "由后端按分数阈值精确筛选并排序"},),
        )
    if min_margin_percent is not None or max_saturation is not None:
        filters: dict[str, Any] = {"sort_by": "margin_rate", "sort_direction": "desc"}
        if min_margin_percent is not None:
            filters["min_margin_rate"] = min_margin_percent / 100
        if max_saturation is not None:
            filters["max_market_saturation"] = max_saturation
        return ParsedIntent(
            "constraint_filter",
            filters=filters,
            limit=100,
            selected_product_ids=selected,
            plan=({"tool": "filter_products", "purpose": "按利润率和市场竞争约束筛选企业商品"},),
        )
    if top_limit is not None or any(token in lowered for token in ("最值得", "优先测试", "高潜", "推荐几个")):
        return ParsedIntent(
            "top_selection",
            filters={"sort_by": "recommendation_score", "sort_direction": "desc"},
            limit=limit,
            selected_product_ids=selected,
            plan=({"tool": "filter_products", "purpose": "按动态推荐度筛选最值得验证的候选"},),
        )
    return ParsedIntent(
        "single_product_decision" if len(selected) == 1 or any(token in lowered for token in ("值得", "要不要", "可以吗", "分析")) else "portfolio_overview",
        filters={"sort_by": "recommendation_score", "sort_direction": "desc"},
        limit=1 if len(selected) == 1 else 5,
        selected_product_ids=selected,
        plan=(
            {"tool": "search_products", "purpose": "定位问题中的商品或候选范围"},
            {"tool": "get_product", "purpose": "读取商品主档和四维分析"},
            {"tool": "calculate_profit", "purpose": "核对利润口径"},
            {"tool": "get_sales_trend", "purpose": "读取已保存的销量趋势"},
            {"tool": "analyze_competition", "purpose": "检查竞争证据"},
            {"tool": "find_historical_failures", "purpose": "检查历史失败特征"},
        ),
    )
