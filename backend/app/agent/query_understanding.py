"""Intent-first decomposition over existing routing; no enterprise facts here."""
import asyncio
import re

from app.agent.entity_resolution import EntityResolution, query_mentions, resolve_entity
from app.schemas.agent import QueryUnderstanding


REFERENCE = re.compile(r"刚才那个(?:商品)?|这个(?:商品|产品|价格|指标|数据)|这几个(?:商品)?|它")
SEMANTIC_TYPES = {"provenance_fact", "calculation_explanation", "decision_explanation", "data_quality_answer", "data_quality_policy"}


def semantic_policy(intent, dimensions, selected=()):
    from app.agent.intent_engine import ParsedIntent, _policy
    tools = () if intent in {"data_quality_policy", "unknown"} else ("get_product", "get_sales_trend") if "sales_snapshot" in dimensions else ("get_product", "calculate_profit") if intent == "calculation_explanation" else ("get_product",)
    budget = 10 if intent == "data_quality_answer" else len(tools)
    return ParsedIntent(intent, selected_product_ids=tuple(selected), policy=_policy(tools, budget, tuple(dimensions), selection="ignore" if not tools else "query_first", fast=True))


def semantic_intent(query, selected=()):
    """Grammatical question/metric patterns, independent of catalogue titles."""
    q = query.casefold()
    if "完整度" in q and re.search(r"代表|意味|等于|足够|可靠|直接.*上架|就.*上架", q):
        return semantic_policy("data_quality_policy", ("data_quality", "policy"))
    if re.search(r"为什么|为何|主要卡在", q) and re.search(r"推荐|上架|不能上|卡在", q):
        return semantic_policy("decision_explanation", ("decision_reason", "evidence"), selected)
    if re.search(r"怎么算|如何算|怎么.*(?:算|得|来)|计算(?:依据|公式|过程)|如何.*计算", q) and re.search(r"利润|roi|%|％|指标", q):
        return semantic_policy("calculation_explanation", ("profit", "roi", "calculation", "evidence"), selected)
    if re.search(r"过期|新鲜|旧了|多久.*采|何时.*采|什么时候.*采|现在.*还能用", q):
        dims = ("price", "freshness") if re.search(r"价格|售价", q) else ("freshness",)
        return semantic_policy("data_quality_answer", dims, selected)
    if re.search(r"来源|来自|哪里来|谁.*采集|追溯|实时|真实|靠谱|可信|可靠", q) or (re.search(r"增速|增长率", q) and "快照" in q):
        if re.search(r"增速|增长率|销量", q):
            dims = ("sales_snapshot", "data_sufficiency", "provenance")
        elif re.search(r"利润|roi", q):
            dims = ("profit", "provenance")
        else:
            dims = ("price", "provenance") if re.search(r"价格|售价|多少钱", q) else ("provenance",)
        return semantic_policy("provenance_fact", dims, selected)
    return None


def understand_query(query, products, selected=None):
    from app.agent.intent_engine import parse_intent
    parsed = parse_intent(query, selected)
    names = tuple(value for p in products for value in (p.title, p.id, p.external_product_id, p.sku) if value)
    references = list(dict.fromkeys(REFERENCE.findall(query)))
    mentions = query_mentions(query, names) if parsed.policy.selection_mode != "ignore" else []
    # A reference is contextual metadata, not a literal catalogue lookup.
    mentions = [m for m in mentions if not REFERENCE.fullmatch(m) and m not in {"这个", "那个", "哪些", "哪些还是新鲜"}]
    if parsed.name in SEMANTIC_TYPES and references:
        mentions = [m for m in mentions if not any(m == ref or m.startswith(ref + "的") for ref in references)]
    dims = list(parsed.policy.requested_dimensions)
    metrics = [field for field, tokens in (("current_price", ("价格", "售价")), ("net_margin", ("利润率",)), ("sales_growth_rate", ("增速", "增长率"))) if any(token in query for token in tokens)]
    resolved = [resolve_entity(products, mention) for mention in mentions]
    # Default detail is high confidence only for a bare name or explicit detail verb.
    known_detail = bool(mentions) and not re.search(r"为什么|怎么|哪里|是否|[?？]|\b(?:why|how)\b", query, re.I) and (
        any(item.resolved for item in resolved) or bool(re.match(r"^(?:请)?(?:查询|查看|分析|对比|比较)", query))
    )
    supported = parsed.recognized or known_detail
    route = "DETERMINISTIC_FAST_PATH" if supported else "SEMANTIC_PLANNER"
    if supported and any(item.status in {"AMBIGUOUS", "LOW_CONFIDENCE"} for item in resolved):
        route = "CLARIFICATION"
    if not supported:
        # Do not assert that an unparsed sentence is a missing product.
        mentions = []
        parsed = semantic_policy("unknown", ())
    understanding = QueryUnderstanding(
        intent=parsed.name, question_type=parsed.name,
        entity_mentions=mentions, references=references, metrics=metrics,
        metric_values=[float(v) for v in re.findall(r"([+-]?\d+(?:\.\d+)?)\s*[%％]", query)],
        requested_dimensions=dims if supported else [],
        comparison_requested="comparison" in parsed.name,
        explanation_requested=parsed.name in {"calculation_explanation", "decision_explanation"},
        provenance_requested=parsed.name == "provenance_fact", calculation_requested=parsed.name == "calculation_explanation",
        policy_requested="policy" in parsed.name, requires_context=not mentions and parsed.policy.selection_mode != "ignore",
        confidence=0.95 if supported else 0.4, route=route, planned_tools=list(parsed.policy.allowed_tools),
    )
    return parsed, understanding


def resolve_understanding(products, understanding):
    results = [resolve_entity(products, mention) for mention in understanding.entity_mentions]
    identities = {p.id: p for p in products}
    ids = list(dict.fromkeys(item.product_id for item in results if item.resolved))
    return EntityResolution([identities[pid] for pid in ids], results)


def validate_semantic_plan(raw, query, selected=()):
    value = QueryUnderstanding.model_validate_json(raw) if isinstance(raw, str) else QueryUnderstanding.model_validate(raw)
    # Planner cannot create arbitrary tool arguments, predicates, values, or IDs.
    if value.intent not in SEMANTIC_TYPES | {"product_price", "product_detail"} or value.confidence < 0.75:
        raise ValueError("Unsupported or low-confidence semantic intent")
    allowed = {
        "provenance_fact": {"price", "profit", "provenance", "sales_snapshot", "data_sufficiency"},
        "calculation_explanation": {"profit", "roi", "calculation", "evidence"},
        "decision_explanation": {"decision_reason", "evidence"},
        "data_quality_answer": {"price", "freshness"}, "data_quality_policy": {"data_quality", "policy"},
        "product_price": {"price"}, "product_detail": {"risk", "detail"},
    }
    if not value.requested_dimensions or not set(value.requested_dimensions) <= allowed[value.intent]:
        raise ValueError("Invalid requested dimensions")
    if any(not mention.strip() or mention not in query or re.search(r"为什么|怎么|来自哪里|[?？]", mention) for mention in value.entity_mentions):
        raise ValueError("Planner entity must be an explicit span in this query")
    if any(reference not in query for reference in value.references):
        raise ValueError("Planner reference must be present in query")
    if not value.entity_mentions and not value.references and value.intent != "data_quality_policy":
        raise ValueError("Missing explicit entity/reference; do not substitute old UI state")
    parsed = semantic_policy(value.intent, value.requested_dimensions, selected)
    if set(value.planned_tools) - set(parsed.policy.allowed_tools):
        raise ValueError("Illegal tool for semantic intent")
    value = value.model_copy(update={"route": "SEMANTIC_PLANNER", "planner_calls": 1, "failure_code": None,
                                    "requires_context": not value.entity_mentions and parsed.policy.selection_mode != "ignore",
                                    "planned_tools": list(dict.fromkeys(value.planned_tools))})
    return parsed, value


async def semantic_fallback(query, understanding, planner, selected=()):
    if planner is None:
        return semantic_policy("unknown", ()), understanding.model_copy(update={"route": "CLARIFICATION", "failure_code": "SEMANTIC_UNAVAILABLE"})
    try:
        raw = await asyncio.wait_for(planner(query, understanding.model_dump(mode="json")), timeout=15)
        return validate_semantic_plan(raw, query, selected)
    except Exception:
        # Single provider boundary: explicitly record failure, never emit exception text
        # or convert an unsupported utterance into a catalogue NOT_FOUND.
        return semantic_policy("unknown", ()), understanding.model_copy(update={"route": "CLARIFICATION", "planner_calls": 1, "failure_code": "SEMANTIC_INVALID_OR_TIMEOUT"})
