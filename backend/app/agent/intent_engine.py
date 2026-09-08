import re
from dataclasses import dataclass, field
from typing import Any, Literal

from app.agent.tool_registry import FilterProductsInput


SelectionMode = Literal["ignore", "query_first", "selection_required"]


@dataclass(frozen=True)
class IntentPolicy:
    allowed_tools: tuple[str, ...]
    max_tool_calls: int
    requested_dimensions: tuple[str, ...]
    selection_mode: SelectionMode = "ignore"
    forbidden_tools: tuple[str, ...] = ()
    fast_path: bool = False


@dataclass(frozen=True)
class ParsedIntent:
    name: str
    recognized: bool = True
    filters: dict[str, Any] = field(default_factory=dict)
    limit: int = 10
    proposed_price: float | None = None
    selected_product_ids: tuple[str, ...] = ()
    plan: tuple[dict[str, str], ...] = ()
    policy: IntentPolicy = field(default_factory=lambda: IntentPolicy(("filter_products",), 1, ("recommendation",), fast_path=True))


def _number_after(patterns: list[str], query: str) -> float | None:
    for pattern in patterns:
        match = re.search(pattern, query, flags=re.IGNORECASE)
        if match:
            return float(match.group(1))
    return None


def _policy(tools: tuple[str, ...], max_calls: int, dimensions: tuple[str, ...], *, selection: SelectionMode = "ignore", fast: bool = False) -> IntentPolicy:
    known = {
        "count_products", "filter_products", "search_products", "get_product", "compare_products", "calculate_profit",
        "get_sales_trend", "get_price_trend", "analyze_competition", "simulate_price_change",
        "find_historical_failures", "search_company_memory", "search_company_knowledge",
    }
    return IntentPolicy(tools, max_calls, dimensions, selection, tuple(sorted(known - set(tools))), fast)


def _category_filter(query: str) -> str:
    matches = re.findall(r"[\u4e00-\u9fff]{2,8}(?:用品|产品)", query)
    for match in matches:
        cleaned = re.sub(r"^(?:推荐|筛选|选择|找出|找|几个|个|款)+", "", match)
        if len(cleaned) >= 4:
            return cleaned
    return ""


def _validated_filters(**values: Any) -> dict[str, Any]:
    """Use the callable tool schema as the single FilterCriteria contract."""

    return FilterProductsInput.model_validate(values).model_dump(exclude_defaults=True)


def parse_intent(query: str, selected_product_ids: list[str] | None = None) -> ParsedIntent:
    """Route explicit query semantics before considering stale UI selection state."""

    normalized = " ".join(query.strip().split())
    lowered = normalized.casefold()
    selected = tuple(dict.fromkeys(selected_product_ids or []))[:10]
    from app.agent.query_understanding import semantic_intent
    semantic = semantic_intent(normalized, selected)
    if semantic is not None:
        return semantic
    comparison_words = any(token in lowered for token in ("比较", "对比", "哪个更", "这两个", "这几个", "这4个", "这四个"))
    reference_words = any(token in lowered for token in ("刚才", "这个", "那个", "它", "对比中心", "这两个", "这几个", "这4个", "这四个"))
    positive_scope = re.split(r"不要|不分析|无需|不需要|不比较", lowered, maxsplit=1)[0]
    full_comparison = any(token in positive_scope for token in ("全面", "综合", "全维度", "最值得上架"))
    profit_only = comparison_words and not full_comparison and any(token in positive_scope for token in ("利润", "roi")) and not any(token in positive_scope for token in ("需求", "竞争", "合规", "风险", "趋势"))
    count_question = bool(re.search(r"(?:公司|企业|商品库)?.*?(?:一共|总共|共有|总数).*?(?:多少|几).*?(?:商品|候选)|(?:公司|企业).*?(?:多少|几).*?(?:商品|候选)", normalized))
    compliance_policy = "合规" in lowered and any(token in lowered for token in ("能上架", "可以上架", "能不能上架", "没通过", "未通过"))
    price_lookup = any(token in lowered for token in ("售价多少", "售价是多少", "价格多少", "价格是多少", "多少钱", "现在售价")) and not any(token in lowered for token in ("调整", "改为", "模拟"))
    risk_fact = bool(re.search(r"风险(?:等级|级别).*(?:多少|什么|如何)", lowered)) and not comparison_words and not any(token in lowered for token in ("上架", "推荐", "决策", "筛选"))
    snapshot_question = any(token in lowered for token in ("销量快照", "上涨还是下降", "未来销量趋势", "销量趋势"))
    score_threshold = _number_after([r"(?:分数|评分|推荐度).*?(\d+(?:\.\d+)?)\s*(?:分)?\s*(?:以上|及以上|达到|大于|>=)", r"(\d+(?:\.\d+)?)\s*(?:分)?\s*(?:以上|及以上)"], normalized)
    proposed_price = _number_after([r"(?:售价|价格|定价).*?(?:到|为|=)\s*(\d+(?:\.\d+)?)", r"(\d+(?:\.\d+)?)\s*(?:rub|卢布)"], normalized)
    top_limit = _number_after([r"(?:最值得|优先|前|top|推荐)\s*(\d+)\s*(?:个|件|款)?", r"选出.*?(\d+)\s*(?:个|件|款)"], normalized)
    limit = max(1, min(20, int(top_limit or 10)))
    min_margin_percent = _number_after([r"(?:净?利润(?:率)?|毛利率).*?(\d+(?:\.\d+)?)\s*%\s*(?:以上|及以上|大于|≥|>=)?", r"(?:净?利润(?:率)?|毛利率).*?(?:达到|大于|≥|>=)\s*(\d+(?:\.\d+)?)"], normalized)
    max_saturation = _number_after([r"(?:竞争|饱和度).*?(?:低于|小于|不超过|≤|<=)\s*(\d+(?:\.\d+)?)", r"(?:竞争|饱和度).*?(\d+(?:\.\d+)?)\s*(?:以下|以内)"], normalized)
    category = _category_filter(normalized)
    risk_level = (
        "low" if any(token in lowered for token in ("低风险", "风险低", "风险为低", "风险等级低"))
        else "medium" if any(token in lowered for token in ("中风险", "风险中等", "风险为中"))
        else "high" if any(token in lowered for token in ("高风险", "风险高", "风险为高", "风险等级高"))
        else ""
    )
    compliance_status = "approved" if "合规" in lowered and any(
        token in lowered for token in ("已通过", "通过商品", "合规通过", "合规状态通过", "状态为通过", "状态已通过")
    ) else ""
    ranking_policy = (
        any(token in lowered for token in ("排名第一", "第一名", "相对排名", "排在第一"))
        and any(token in lowered for token in ("推荐", "上架"))
        and any(token in lowered for token in ("代表", "是不是", "是否", "意味着", "等于"))
    )
    recommendation_gate_policy = (
        any(token in lowered for token in ("评分", "分数", "综合分", "必须推荐"))
        and any(token in lowered for token in ("推荐", "上架"))
        and any(token in lowered for token in ("证据不足", "数据不足", "高风险", "风险高", "必须推荐"))
    )

    if count_question:
        return ParsedIntent("company_product_count", plan=({"tool": "count_products", "purpose": "确定性统计当前企业商品总数"},), policy=_policy(("count_products",), 1, ("count",), fast=True))
    if compliance_policy:
        return ParsedIntent("compliance_policy", plan=(), policy=_policy((), 0, ("compliance", "decision"), fast=True))
    if ranking_policy or recommendation_gate_policy:
        return ParsedIntent("recommendation_policy", plan=(), policy=_policy((), 0, ("recommendation_policy", "decision"), fast=True))
    if price_lookup:
        return ParsedIntent("product_price", selected_product_ids=selected if reference_words else (), plan=({"tool": "get_product", "purpose": "读取唯一解析商品的价格字段"},), policy=_policy(("get_product",), 1, ("price",), selection="query_first", fast=True))
    if risk_fact:
        return ParsedIntent("product_detail", selected_product_ids=selected if reference_words else (), plan=({"tool": "get_product", "purpose": "读取商品当前风险等级与来源"},), policy=_policy(("get_product",), 1, ("risk",), selection="query_first", fast=True))
    if proposed_price is not None and any(token in lowered for token in ("价格", "售价", "定价", "rub", "卢布")):
        return ParsedIntent("product_detail", proposed_price=proposed_price, selected_product_ids=selected, plan=({"tool": "get_product", "purpose": "读取目标商品"}, {"tool": "simulate_price_change", "purpose": "按目标售价重算利润与风险"}), policy=_policy(("get_product", "simulate_price_change"), 2, ("price", "profit", "risk"), selection="query_first"))
    if snapshot_question:
        return ParsedIntent("product_detail", selected_product_ids=selected if reference_words else (), plan=({"tool": "get_product", "purpose": "读取唯一商品主档"}, {"tool": "get_sales_trend", "purpose": "核验快照数量与趋势充分性"}), policy=_policy(("get_product", "get_sales_trend"), 2, ("sales_snapshot", "data_sufficiency"), selection="query_first", fast=True))
    if any(token in lowered for token in ("数据不完整", "缺数据", "缺失", "不能进入最终审核", "不能审核")):
        filters = _validated_filters(completeness="incomplete", sort_by="completeness", sort_direction="asc")
        return ParsedIntent("product_filter", filters=filters, limit=100, plan=({"tool": "filter_products", "purpose": "筛选证据不完整商品"},), policy=_policy(("filter_products",), 1, ("data_completeness",), fast=True))
    if score_threshold is not None or min_margin_percent is not None or max_saturation is not None or risk_level or compliance_status:
        raw_filters: dict[str, Any] = {"sort_by": "margin_rate" if min_margin_percent is not None else "recommendation_score", "sort_direction": "desc"}
        if score_threshold is not None: raw_filters["min_score"] = score_threshold
        if min_margin_percent is not None: raw_filters["min_margin_rate"] = min_margin_percent / 100
        if max_saturation is not None: raw_filters["max_market_saturation"] = max_saturation
        if risk_level: raw_filters["risk_level"] = risk_level
        if compliance_status: raw_filters["compliance_status"] = compliance_status
        if category: raw_filters["category"] = category
        filters = _validated_filters(**raw_filters)
        dimensions = ["filters"]
        if min_margin_percent is not None: dimensions.append("profit")
        if risk_level: dimensions.append("risk")
        if compliance_status: dimensions.append("compliance")
        if max_saturation is not None: dimensions.append("competition")
        if score_threshold is not None: dimensions.append("recommendation_score")
        return ParsedIntent("product_filter", filters=filters, limit=100, plan=({"tool": "filter_products", "purpose": "按用户明确条件确定性筛选"},), policy=_policy(("filter_products",), 1, tuple(dimensions), fast=True))
    if top_limit is not None or any(token in lowered for token in ("最值得上架", "最值得测试", "优先测试", "高潜", "推荐几个", "好的商品", "推荐商品")):
        filters = {"sort_by": "recommendation_score", "sort_direction": "desc"}
        if category: filters["category"] = category
        filters = _validated_filters(**filters)
        return ParsedIntent("selection_recommendation", filters=filters, limit=limit if top_limit else 5, selected_product_ids=selected if reference_words else (), plan=({"tool": "filter_products", "purpose": "筛选候选池；已有明确选择时改为读取所选商品"},), policy=_policy(("filter_products", "compare_products"), 1, ("recommendation", "decision"), selection="query_first", fast=True))
    if profit_only:
        return ParsedIntent("profit_comparison", selected_product_ids=selected if reference_words else (), limit=10, plan=({"tool": "compare_products", "purpose": "读取已解析商品"}, {"tool": "calculate_profit", "purpose": "仅计算利润与 ROI"}), policy=_policy(("compare_products", "calculate_profit"), 11, ("profit", "roi"), selection="query_first"))
    if comparison_words:
        dimensions = ("profit", "demand", "competition", "compliance", "risk") if full_comparison else tuple(dimension for dimension, tokens in (("profit", ("利润", "roi")), ("demand", ("需求", "销量", "趋势")), ("competition", ("竞争",)), ("compliance", ("合规",)), ("risk", ("风险",))) if any(token in positive_scope for token in tokens))
        return ParsedIntent("product_comparison", selected_product_ids=selected if reference_words else (), limit=10, plan=({"tool": "compare_products", "purpose": "比较明确解析或当前引用的商品"},), policy=_policy(("compare_products",), 1, dimensions or ("identity", "price"), selection="query_first"))
    if any(token in lowered for token in ("失败", "放弃", "历史案例", "历史上")):
        return ParsedIntent("product_detail", selected_product_ids=selected if reference_words else (), plan=({"tool": "find_historical_failures", "purpose": "匹配本企业历史放弃商品"}, {"tool": "search_company_memory", "purpose": "检索失败原因"}), policy=_policy(("find_historical_failures", "search_company_memory"), 4, ("history",), selection="query_first"))
    if any(token in lowered for token in ("利润怎么样", "roi", "利润如何")):
        return ParsedIntent("product_detail", selected_product_ids=selected if reference_words else (), plan=({"tool": "get_product", "purpose": "读取唯一商品"}, {"tool": "calculate_profit", "purpose": "核算利润与 ROI"}), policy=_policy(("get_product", "calculate_profit"), 2, ("profit", "roi"), selection="query_first", fast=True))
    detail_requested = bool(re.search(r"是否值得|值不值得|是否推荐|能否上架|详情|怎么样", lowered))
    return ParsedIntent("product_detail", recognized=detail_requested, selected_product_ids=selected if reference_words else (), plan=({"tool": "get_product", "purpose": "读取唯一商品主档与分析"},), policy=_policy(("get_product",), 1, ("detail",), selection="query_first", fast=True))
