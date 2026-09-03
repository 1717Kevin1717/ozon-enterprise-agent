import re
import time
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.intent_engine import ParsedIntent, parse_intent
from app.agent.tool_registry import TOOL_REGISTRY
from app.db.models import AgentRun, ConversationMessage, ConversationSession, Memory, Product
from app.repositories.products import ProductRepository, product_view
from app.schemas.agent import validate_agent_answer
from app.services.decision_engine import analyze, simulate_price, to_dict
from app.services.knowledge_base import search_documents

TOOL_POLICY = {spec.tool_name: list(spec.allowed_roles) for spec in TOOL_REGISTRY.all()}
BUSINESS_TOOL_LABELS = {
    "filter_products": "筛选企业商品库",
    "search_products": "检索企业商品库",
    "get_product": "读取商品主档与证据",
    "compare_products": "建立商品决策对比",
    "calculate_profit": "分析利润模型",
    "get_sales_trend": "核验销量趋势",
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


async def search_products(repo: ProductRepository, keyword: str, role: str) -> dict:
    if not allow("search_products", role):
        return tool_error("search_products", "FORBIDDEN")
    rows = await repo.list(keyword)
    return {"tool": "search_products", "success": True, "data": [product_view(item, await repo.latest_analysis(item.id)) for item in rows]}


async def filter_products(repo: ProductRepository, role: str, **filters: Any) -> dict:
    if not allow("filter_products", role):
        return tool_error("filter_products", "FORBIDDEN")
    keyword = str(filters.get("keyword") or "")
    limit = max(1, min(100, int(filters.get("limit") or 20)))
    views: list[dict[str, Any]] = []
    for product in await repo.list(keyword, limit=500):
        latest = await repo.latest_analysis(product.id)
        view = product_view(product, latest)
        if not latest:
            view["analysis"] = to_dict(analyze(product))
        analysis = view.get("analysis") or {}
        score_value = float(analysis.get("recommendation_score") or 0)
        completeness_grade = (analysis.get("evidence_completeness") or {}).get("grade")
        if filters.get("min_score") is not None and score_value < float(filters["min_score"]):
            continue
        if filters.get("max_score") is not None and score_value > float(filters["max_score"]):
            continue
        if filters.get("min_margin_rate") is not None and float(view.get("current_margin_rate") or analysis.get("net_margin") or 0) < float(filters["min_margin_rate"]):
            continue
        if filters.get("min_roi") is not None and float(analysis.get("roi") or 0) < float(filters["min_roi"]):
            continue
        if filters.get("max_market_saturation") is not None and float(view.get("market_saturation") or 0) > float(filters["max_market_saturation"]):
            continue
        if filters.get("brand") and str(filters["brand"]).casefold() not in str(view.get("brand") or "").casefold():
            continue
        if filters.get("category") and str(filters["category"]).casefold() not in str(view.get("category_path") or "").casefold():
            continue
        if filters.get("lifecycle_status") and view.get("lifecycle_status") != filters["lifecycle_status"]:
            continue
        if filters.get("risk_level") and analysis.get("risk_level") != filters["risk_level"]:
            continue
        if filters.get("completeness") == "complete" and completeness_grade != "complete":
            continue
        if filters.get("completeness") == "incomplete" and completeness_grade == "complete":
            continue
        updated_since = filters.get("updated_since")
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
            "margin_rate": float(item.get("current_margin_rate") or 0),
        }
        return mapping.get(str(filters.get("sort_by") or "recommendation_score"), mapping["recommendation_score"])

    views.sort(key=sort_value, reverse=filters.get("sort_direction", "desc") != "asc")
    return {"tool": "filter_products", "success": True, "data": views[:limit], "message": "筛选、计数和排序均由后端确定性执行。"}


async def get_product(repo: ProductRepository, product_id: str, role: str) -> dict:
    if not allow("get_product", role):
        return tool_error("get_product", "FORBIDDEN")
    product = await repo.get(product_id)
    if not product:
        return tool_error("get_product", "NOT_FOUND", "商品不存在或不属于当前企业。")
    return {"tool": "get_product", "success": True, "data": product_view(product, await repo.latest_analysis(product.id))}


async def compare_products(repo: ProductRepository, product_ids: list[str], role: str) -> dict:
    if not allow("compare_products", role):
        return tool_error("compare_products", "FORBIDDEN")
    rows = []
    for product_id in dict.fromkeys(product_ids):
        product = await repo.get(product_id)
        if product:
            rows.append(product_view(product, await repo.latest_analysis(product.id)))
    return {"tool": "compare_products", "success": True, "data": rows, "warning": "仅比较已保存的本企业数据；缺失证据不会被补造。"}


async def get_product_history(repo: ProductRepository, product_id: str, role: str) -> dict:
    if not allow("get_product_history", role):
        return tool_error("get_product_history", "FORBIDDEN")
    if not await repo.get(product_id):
        return tool_error("get_product_history", "NOT_FOUND")
    rows = await repo.history(product_id)
    data = [{"captured_at": item.captured_at, "price": item.price, "sales_30d": item.sales_30d, "rating": item.rating, "review_count": item.review_count, "competitor_count": item.competitor_count} for item in rows]
    return {"tool": "get_product_history", "success": True, "data": data}


async def calculate_profit(repo: ProductRepository, product_id: str, role: str) -> dict:
    if not allow("calculate_profit", role):
        return tool_error("calculate_profit", "FORBIDDEN")
    product = await repo.get(product_id)
    if not product:
        return tool_error("calculate_profit", "NOT_FOUND")
    result = to_dict(analyze(product))
    fields = ("gross_profit", "gross_margin", "net_profit", "net_margin", "roi", "expected_profit", "current_margin_rate", "break_even_price", "target_price", "profit_score", "risk_score", "data_confidence", "missing_data", "evidence_completeness")
    return {"tool": "calculate_profit", "success": True, "data": {key: result[key] for key in fields}}


async def analyze_competition(repo: ProductRepository, product_id: str, role: str) -> dict:
    if not allow("analyze_competition", role):
        return tool_error("analyze_competition", "FORBIDDEN")
    product = await repo.get(product_id)
    if not product:
        return tool_error("analyze_competition", "NOT_FOUND")
    result = analyze(product)
    return {"tool": "analyze_competition", "success": True, "data": {"score": result.competition_score, "competitor_count": product.competitor_count, "competitor_price_min": product.competitor_price_min, "competitor_price_avg": product.competitor_price_avg, "price_position": result.price_position, "market_saturation": product.market_saturation, "evidence": product.competition_evidence, "complete": result.evidence_completeness["groups"]["competition"]["ready"]}}


async def get_sales_trend(repo: ProductRepository, product_id: str, role: str) -> dict:
    if not allow("get_sales_trend", role):
        return tool_error("get_sales_trend", "FORBIDDEN")
    rows = (await get_product_history(repo, product_id, role)).get("data", [])
    values = [item["sales_30d"] for item in rows if item["sales_30d"] > 0]
    trend = "insufficient" if len(values) < 2 else "growing" if values[-1] > values[0] else "declining" if values[-1] < values[0] else "flat"
    return {"tool": "get_sales_trend", "success": True, "data": {"points": rows, "trend": trend, "notice": "趋势只基于已保存快照，不对缺失期间作推断。"}}


async def get_price_trend(repo: ProductRepository, product_id: str, role: str) -> dict:
    if not allow("get_price_trend", role):
        return tool_error("get_price_trend", "FORBIDDEN")
    history = await get_product_history(repo, product_id, role)
    if not history.get("success"):
        return tool_error("get_price_trend", history.get("error_code", "TOOL_FAILED"), history.get("message", ""))
    rows = history.get("data", [])
    values = [item["price"] for item in rows if item["price"] > 0]
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


def _entity_score(product: Product, query: str) -> int:
    lowered = query.casefold()
    title = (product.title or "").casefold()
    brand = (product.brand or "").casefold()
    if title and title in lowered:
        return 100
    if brand and brand in lowered:
        return 80
    score = 0
    for size in range(6, 1, -1):
        if any(fragment in title for fragment in (lowered[index:index + size] for index in range(max(0, len(lowered) - size + 1)))):
            score = size
            break
    return score


def select_targets(products: list[Product], query: str, selected_product_ids: list[str] | None = None, limit: int = 5) -> list[Product]:
    by_id = {product.id: product for product in products}
    explicit = [by_id[item] for item in dict.fromkeys(selected_product_ids or []) if item in by_id]
    if explicit:
        return explicit[:limit]
    ranked = [(product, _entity_score(product, query)) for product in products]
    ranked = [item for item in ranked if item[1] >= 2]
    ranked.sort(key=lambda item: item[1], reverse=True)
    return [item[0] for item in ranked[:limit]]


def plan_query(query: str, target_count: int) -> tuple[str, list[dict[str, str]]]:
    selected = [f"selected-{index}" for index in range(target_count)]
    parsed = parse_intent(query, selected)
    return parsed.name, list(parsed.plan)


def _agent_product(view: dict[str, Any]) -> dict[str, Any]:
    analysis = view.get("analysis") or {}
    completeness = analysis.get("evidence_completeness") or {}
    return {
        "id": str(view.get("id") or ""),
        "external_product_id": str(view.get("external_product_id") or ""),
        "title": str(view.get("title") or ""),
        "brand": str(view.get("brand") or ""),
        "category_path": str(view.get("category_path") or ""),
        "score": float(analysis.get("recommendation_score") or 0),
        "recommendation_grade": str(analysis.get("recommendation_grade") or "C"),
        "profit_score": float(analysis.get("profit_score") or 0),
        "demand_score": float(analysis.get("demand_score") or 0),
        "competition_score": float(analysis.get("competition_score") or 0),
        "compliance_score": float(analysis.get("compliance_score") or 0),
        "risk_score": float(analysis.get("risk_score") or 0),
        "confidence": float(analysis.get("data_confidence") or 0),
        "completeness": int(completeness.get("percent") or analysis.get("data_completeness") or 0),
        "risk_level": str(analysis.get("risk_level") or "unknown"),
        "lifecycle_status": str(view.get("lifecycle_status") or "candidate"),
        "current_price": float(view.get("current_price") or 0),
        "current_margin_rate": float(view.get("current_margin_rate") or analysis.get("net_margin") or 0),
        "net_profit": float(analysis.get("net_profit") or 0),
        "roi": float(analysis.get("roi") or 0),
        "missing_fields": list(analysis.get("missing_fields") or []),
        "url": str(view.get("url") or ""),
        "main_image_url": str(view.get("main_image_url") or ""),
    }


def _trace_summary(trace: list[dict[str, Any]]) -> list[str]:
    labels = []
    for item in trace:
        if item.get("event") == "tool_finished":
            label = str(item.get("business_label") or item.get("tool") or "")
            if label and label not in labels:
                labels.append(label)
    return [f"{index + 1}. {label}" for index, label in enumerate(labels)]


def _unique(items: list[str], limit: int = 8) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))[:limit]


async def _ensure_conversation(session: AsyncSession, company_id: str, user_id: str, query: str, session_id: str | None) -> ConversationSession:
    conversation = await session.get(ConversationSession, session_id) if session_id else None
    if not conversation or conversation.company_id != company_id:
        conversation = ConversationSession(company_id=company_id, user_id=user_id, title=query[:80], goal_summary=query[:300])
        session.add(conversation)
        await session.flush()
    return conversation


async def agent_v1_ask(
    session: AsyncSession,
    company_id: str,
    user_id: str,
    role: str,
    query: str,
    session_id: str | None,
    selected_product_ids: list[str] | None = None,
    record_user_message: bool = True,
    response_mode: str = "rule_engine",
    fallback_reason: str | None = None,
    provider_notice: str = "",
    trace_prefix: list[dict[str, Any]] | None = None,
    active_model_override: str | None = None,
    token_usage_override: dict[str, Any] | None = None,
    model_summary: str = "",
) -> dict:
    started = time.perf_counter()
    repo = ProductRepository(session, company_id)
    trace: list[dict[str, Any]] = list(trace_prefix or [])
    conversation = await _ensure_conversation(session, company_id, user_id, query, session_id)
    session_id = conversation.id
    if record_user_message:
        session.add(ConversationMessage(company_id=company_id, session_id=session_id, role="user", content=query, metadata_json={"selected_product_ids": selected_product_ids or []}))
    all_products = await repo.list(limit=500)
    explicit_ids = list(dict.fromkeys(selected_product_ids or []))
    if not explicit_ids and any(token in query for token in ("刚才", "那个", "它", "这两个", "相比")):
        explicit_ids = list(conversation.last_product_ids or [])
    matched_targets = select_targets(all_products, query, explicit_ids, limit=10)
    parsed: ParsedIntent = parse_intent(query, [item.id for item in matched_targets] if matched_targets else explicit_ids)
    event(trace, "planning_finished", "planner", f"已识别业务意图：{parsed.name}。", plan=list(parsed.plan))

    views: list[dict[str, Any]] = []
    simulation: dict[str, Any] | None = None
    historical_matches: list[dict[str, Any]] = []
    if parsed.name in {"score_filter", "constraint_filter", "top_selection", "incomplete_products", "portfolio_overview"}:
        event(trace, "tool_started", "filter_products", "按业务条件筛选企业候选。")
        result = await filter_products(repo, role, **parsed.filters, limit=parsed.limit)
        views = result.get("data", []) if result.get("success") else []
        event(trace, "tool_finished", "filter_products", f"筛选得到 {len(views)} 个商品。")
    elif parsed.name == "comparison":
        ids = [item.id for item in matched_targets]
        if len(ids) >= 2:
            event(trace, "tool_started", "compare_products", "读取明确选择的商品进行横向比较。")
            compared = await compare_products(repo, ids[:10], role)
            views = compared.get("data", []) if compared.get("success") else []
            event(trace, "tool_finished", "compare_products", f"已比较 {len(views)} 个商品。")
            for view in views:
                product_id = view["id"]
                event(trace, "tool_started", "calculate_profit", f"核算 {view['title']} 的利润。", product_id=product_id)
                await calculate_profit(repo, product_id, role)
                event(trace, "tool_finished", "calculate_profit", f"已核算 {view['title']} 的利润。", product_id=product_id)
                event(trace, "tool_started", "analyze_competition", f"核验 {view['title']} 的竞争证据。", product_id=product_id)
                await analyze_competition(repo, product_id, role)
                event(trace, "tool_finished", "analyze_competition", f"已核验 {view['title']} 的竞争证据。", product_id=product_id)
            views.sort(key=lambda item: float((item.get("analysis") or {}).get("recommendation_score") or 0), reverse=True)
    elif parsed.name == "price_simulation":
        target = matched_targets[0] if matched_targets else None
        if target and parsed.proposed_price is not None:
            detail = await get_product(repo, target.id, role)
            if detail.get("success"):
                views = [detail["data"]]
            event(trace, "tool_started", "simulate_price_change", f"按 {parsed.proposed_price:g} RUB 重算利润与风险。", product_id=target.id)
            result = await simulate_price_change(repo, target.id, parsed.proposed_price, role)
            simulation = result.get("data") if result.get("success") else None
            event(trace, "tool_finished", "simulate_price_change", "价格情景模拟完成。", product_id=target.id)
    elif parsed.name == "historical_failure":
        targets = matched_targets or select_targets(all_products, query, conversation.last_product_ids or [], limit=3)
        for target in targets:
            view = product_view(target, await repo.latest_analysis(target.id))
            views.append(view)
            event(trace, "tool_started", "find_historical_failures", f"查询 {target.title} 的相似失败案例。", product_id=target.id)
            result = await find_historical_failures(session, company_id, target, role)
            historical_matches.extend(result.get("data", []))
            event(trace, "tool_finished", "find_historical_failures", f"找到 {len(result.get('data', []))} 条相似失败记录。", product_id=target.id)
        event(trace, "tool_started", "search_company_memory", "检索企业保存的失败原因。")
        memory_result = await search_company_memory(session, company_id, query, role)
        event(trace, "tool_finished", "search_company_memory", f"检索到 {len(memory_result.get('data', []))} 条企业记忆。")
    else:
        targets = matched_targets[:1]
        if targets:
            target = targets[0]
            for tool_name, call in [
                ("get_product", lambda: get_product(repo, target.id, role)),
                ("calculate_profit", lambda: calculate_profit(repo, target.id, role)),
                ("get_sales_trend", lambda: get_sales_trend(repo, target.id, role)),
                ("analyze_competition", lambda: analyze_competition(repo, target.id, role)),
            ]:
                event(trace, "tool_started", tool_name, f"处理 {target.title}。", product_id=target.id)
                result = await call()
                event(trace, "tool_finished", tool_name, f"已完成 {BUSINESS_TOOL_LABELS[tool_name]}。", product_id=target.id)
                if tool_name == "get_product" and result.get("success"):
                    views = [result["data"]]
        else:
            event(trace, "tool_started", "filter_products", "读取当前最高优先级候选。")
            result = await filter_products(repo, role, sort_by="recommendation_score", sort_direction="desc", limit=5)
            views = result.get("data", []) if result.get("success") else []
            event(trace, "tool_finished", "filter_products", f"读取到 {len(views)} 个候选。")

    products = [_agent_product(view) for view in views]
    missing_data = []
    risk_messages: list[str] = []
    actions: list[str] = []
    evidence = []
    for view in views:
        analysis = view.get("analysis") or {}
        for item in analysis.get("missing_data") or []:
            missing_data.append({"product_id": view.get("id"), "title": view.get("title"), **item})
            actions.append(str(item.get("action") or ""))
        for item in analysis.get("risks") or []:
            risk_messages.append(f"{view.get('title')}：{item.get('message')}")
        evidence.append({"product_id": str(view.get("id") or ""), "title": str(view.get("title") or ""), "source": "company_product_database", "summary": f"推荐度 {float(analysis.get('recommendation_score') or 0):.1f}，完整度 {int((analysis.get('evidence_completeness') or {}).get('percent') or 0)}%，风险 {analysis.get('risk_level', 'unknown')}。", "url": str(view.get("url") or "")})

    if parsed.name == "score_filter":
        threshold = float(parsed.filters.get("min_score") or 0)
        answer = f"企业内共有 {len(products)} 个商品的动态推荐度达到 {threshold:g} 分以上，已按分数从高到低排列。"
    elif parsed.name == "constraint_filter":
        margin = float(parsed.filters.get("min_margin_rate") or 0)
        saturation = parsed.filters.get("max_market_saturation")
        constraint = f"净利率不低于 {margin:.0%}" + (f"、市场饱和度不高于 {float(saturation):g}" if saturation is not None else "")
        answer = f"企业内共有 {len(products)} 个商品满足{constraint}，筛选和计算均由后端完成。"
    elif parsed.name == "top_selection":
        answer = f"已从企业候选池选出最值得测试的 {len(products)} 个商品；第一名是 {products[0]['title']}（{products[0]['score']:.1f} 分）。" if products else "当前没有满足条件的可测试商品。"
    elif parsed.name == "incomplete_products":
        answer = f"共有 {len(products)} 个商品因关键证据不完整，当前不能进入最终审核。"
    elif parsed.name == "comparison":
        answer = f"已完成 {len(products)} 个商品的利润、需求、竞争、合规和风险对比；当前综合领先的是 {products[0]['title']}。" if products else "需要明确选择至少两个本企业商品后才能比较。"
    elif parsed.name == "price_simulation":
        answer = f"售价调整为 {simulation['proposed_price']:g} RUB 后，单件净利润为 {simulation['net_profit']:.2f} RUB，净利率为 {simulation['margin_rate']:.1%}，推荐等级为 {simulation['recommendation_grade']}。" if simulation else "需要明确选择一个商品并给出目标售价后才能模拟。"
    elif parsed.name == "historical_failure":
        answer = f"企业历史中找到 {len(historical_matches)} 条相似失败商品记录；失败原因必须由人工结合当前证据复核。"
        risk_messages.extend(f"历史案例 {item['title']}：{item['reason']}" for item in historical_matches[:5])
    elif products:
        answer = f"{products[0]['title']} 当前动态推荐度为 {products[0]['score']:.1f}，等级 {('S' if products[0]['score'] >= 85 else 'A' if products[0]['score'] >= 75 else 'B' if products[0]['score'] >= 60 else 'C')}；结论仍需人工审核。"
    else:
        answer = "当前企业数据中没有识别到可分析商品，请选择商品或补充更明确的名称。"

    source_badge = "GLM 实际调用成功" if response_mode == "glm_success" else "确定性 Planner 回退" if response_mode == "deterministic_fallback" else "规则引擎结果"
    latency_ms = round((time.perf_counter() - started) * 1000)
    payload = {
        "session_id": session_id,
        "answer": answer,
        "matched_count": len(products),
        "products": products,
        "evidence": evidence,
        "risks": _unique(risk_messages),
        "missing_data": missing_data,
        "next_actions": _unique(actions) or ["查看商品证据后，由企业审核人决定是否进入下一阶段。"],
        "requires_human_review": True,
        "tool_trace_summary": _trace_summary(trace),
        "response_mode": response_mode,
        "source_badge": source_badge,
        "active_provider": "deterministic_planner" if response_mode != "glm_success" else "zhipu",
        "active_model": active_model_override or ("agent-v2-rule-planner" if response_mode != "glm_success" else "glm"),
        "fallback_reason": fallback_reason,
        "latency_ms": latency_ms,
        "token_usage": token_usage_override or {},
        "intent": parsed.name,
        "mode": "zhipu_controlled_function_calling" if response_mode == "glm_success" else "agent_v2_deterministic_fallback" if response_mode == "deterministic_fallback" else "agent_v2_rule_engine",
        "conclusion": answer,
        "trace": trace,
        "simulation": simulation,
        "data_completeness": (views[0].get("analysis") or {}).get("evidence_completeness") if views else None,
        "provider_notice": provider_notice,
        "model_summary": model_summary,
    }
    validated = validate_agent_answer(payload)
    conversation.last_product_ids = [item["id"] for item in products[:10]]
    conversation.conclusion_summary = answer[:1000]
    conversation.state_json = {"last_intent": parsed.name, "last_product_ids": conversation.last_product_ids, "last_tool_summary": validated["tool_trace_summary"], "response_mode": response_mode}
    if not conversation.goal_summary:
        conversation.goal_summary = query[:300]
    session.add(ConversationMessage(company_id=company_id, session_id=session_id, role="assistant", content=answer, metadata_json={"response_mode": response_mode, "selected_product_ids": conversation.last_product_ids, "tool_trace_summary": validated["tool_trace_summary"], "missing_data": missing_data, "model_summary": model_summary}))
    session.add(AgentRun(company_id=company_id, user_id=user_id, session_id=session_id, query=query, model=validated["active_model"], active_provider=validated["active_provider"], response_mode=response_mode, fallback_reason=fallback_reason or "", intent=parsed.name, prompt_version="agent-answer-v2", tool_registry_version="v2", tools_used=[item["tool"] for item in trace if item["event"] == "tool_started"], trace_json=trace, answer=answer, latency_ms=latency_ms, token_usage=token_usage_override or {}))
    await session.commit()
    return validated


rule_agent_ask = agent_v1_ask
