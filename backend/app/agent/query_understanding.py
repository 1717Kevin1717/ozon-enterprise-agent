"""Intent-first decomposition over existing routing; no enterprise facts here."""
import asyncio
import re
from dataclasses import dataclass

from pydantic import ValidationError

from app.agent.entity_resolution import EntityResolution, normalize_entity, query_mentions, resolve_entity
from app.agent.tool_registry import TOOL_REGISTRY
from app.schemas.agent import ContextSnapshot, QueryUnderstanding, SemanticContextPacket
from app.schemas.semantic_vocabulary import (
    INTENT_ALLOWED_DIMENSIONS,
    METRIC_TO_DIMENSIONS,
    SEMANTIC_PLANNER_INTENTS,
    normalize_semantic_values,
)


REFERENCE = re.compile(
    r"刚才那个(?:商品|产品)?|刚才(?:那|这)(?:两个|几个)(?:商品|产品|候选)?|这个(?:商品|产品|价格|利润|指标|数据|数)?|那个(?:商品|产品)?|"
    r"这几个(?:商品|产品|候选)?|这两个(?:商品|产品|候选)?|这些(?:商品|产品|候选)?|"
    r"我选的这些|它|第[一二两三四五六七八九十\d]+个|前一个|后一个|那利润(?:呢)?|现在比较(?:一下)?"
)
SEMANTIC_TYPES = {"provenance_fact", "calculation_explanation", "decision_explanation", "data_quality_answer", "data_quality_policy"}
SEMANTIC_HANDOFF_TOOLS = ("get_product", "calculate_profit", "get_sales_trend")


class SemanticPlannerFailure(RuntimeError):
    """Sanitized provider failure crossing the semantic handoff boundary."""

    def __init__(self, failure_code: str):
        super().__init__(failure_code)
        self.failure_code = failure_code


class SemanticPlanValidationFailure(ValueError):
    """Structured, sanitized rejection from one semantic validation rule."""

    def __init__(
        self,
        *,
        failure_code: str,
        rule_id: str,
        reason_code: str,
        field: str | None,
        stage: str,
        safe_detail: str | None = None,
    ):
        super().__init__(reason_code)
        self.failure_code = failure_code
        self.rule_id = rule_id
        self.reason_code = reason_code
        self.field = field
        self.stage = stage
        self.safe_detail = safe_detail


@dataclass(frozen=True)
class EntityArbitration:
    """Runtime-only role arbitration before catalogue entity resolution."""

    resolution: EntityResolution
    candidate_roles: tuple[str, ...]
    candidate_statuses: tuple[str, ...]
    ignored_semantic_candidate_count: int = 0


def _reject(rule_id: str, reason_code: str, field: str | None, *, safe_detail: str | None = None):
    raise SemanticPlanValidationFailure(
        failure_code="SEMANTIC_PLAN_VALIDATION_FAILED",
        rule_id=rule_id,
        reason_code=reason_code,
        field=field,
        stage="SEMANTIC_VALIDATE",
        safe_detail=safe_detail,
    )


def semantic_policy(intent, dimensions, selected=()):
    from app.agent.intent_engine import ParsedIntent, _policy
    tools = () if intent in {"data_quality_policy", "compliance_policy", "recommendation_policy", "unknown"} else (
        ("compare_products",) if intent == "product_comparison" else
        ("compare_products", "calculate_profit") if intent == "profit_comparison" else
        ("compare_products",) if intent == "selection_recommendation" else
        ("get_product", "get_sales_trend") if "sales_snapshot" in dimensions else
        ("get_product", "calculate_profit") if intent == "calculation_explanation" else
        ("get_product", "calculate_profit") if intent == "product_detail" and ({"profit", "roi"} & set(dimensions)) else
        ("get_product",)
    )
    budget = 10 if intent == "data_quality_answer" else 11 if intent == "profit_comparison" else len(tools)
    return ParsedIntent(intent, selected_product_ids=tuple(selected), policy=_policy(tools, budget, tuple(dimensions), selection="ignore" if not tools else "query_first", fast=True))


def semantic_intent(query, selected=(), context_snapshot: ContextSnapshot | None = None):
    """Grammatical question/metric patterns, independent of catalogue titles."""
    q = query.casefold()
    previous_dimensions = tuple(context_snapshot.last_requested_dimensions) if context_snapshot else ()
    previous_fact = context_snapshot.last_fact_dimension if context_snapshot else None
    if "完整度" in q and re.search(r"代表|意味|等于|足够|可靠|靠谱|直接.*上架|就.*上架", q):
        return semantic_policy("data_quality_policy", ("data_quality", "policy"))
    if re.search(r"过期|旧了|多久.*采|何时.*采|什么时候.*采|前采", q) and re.search(r"作为.*依据|能否.*使用|还能.*用|是否.*可信", q) and not re.search(r"这个|那个|它|具体|商品名|product[_ -]?id", q):
        return semantic_policy("data_quality_policy", ("freshness", "policy"))
    if re.search(r"第[一二两三四五六七八九十\d]+个|排第[一二两三四五六七八九十\d]+|前一个|后一个", q) and re.search(r"为什么|为何|怎么.*低|原因", q):
        return semantic_policy("decision_explanation", ("decision_reason", "evidence"), selected)
    if re.search(r"为什么|为何|主要卡在", q) and re.search(r"推荐|上架|不能上|卡在", q):
        return semantic_policy("decision_explanation", ("decision_reason", "evidence"), selected)
    if re.search(r"怎么算|如何算|怎么.*(?:算|得|来)|计算(?:依据|公式|过程)|如何.*计算|亏在哪些成本|成本.*构成|利润依据", q) and re.search(r"利润|roi|%|％|指标|这个数|成本", q):
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
    if re.search(r"(?:换成|改成).+(?:呢|如何|怎么样)?$", q) and (previous_fact or previous_dimensions):
        inherited = METRIC_TO_DIMENSIONS.get(previous_fact, previous_dimensions or ("detail",))
        intent = "product_price" if inherited == ("price",) else "product_detail"
        return semantic_policy(intent, inherited, selected)
    if re.search(r"只能留一个|只留一个|必须选一个|二选一", q):
        return semantic_policy("selection_recommendation", ("recommendation", "decision"), selected)
    if re.search(r"这几个|这两个|这些商品|我选的这些", q) and re.search(r"谁.*(?:利润|赚).*(?:好|高|多)|利润.*(?:最好|最高)", q):
        return semantic_policy("profit_comparison", ("profit", "roi"), selected)
    if re.search(r"和|与|、", q) and re.search(r"利润.*(?:差|区别)|(?:差|区别).*利润", q):
        return semantic_policy("profit_comparison", ("profit", "roi"), selected)
    if re.search(r"利润.*优先", q) and re.search(r"风险.*其次", q) and re.search(r"合规.*(?:必须|直接排除|不过)", q):
        return semantic_policy("selection_recommendation", ("profit", "risk", "compliance", "recommendation", "decision"), selected)
    if re.search(r"哪个好|哪个更适合|怎么选", q) and re.search(r"和|与|、|这两个|这几个", q):
        return semantic_policy("product_comparison", ("profit", "demand", "competition", "compliance", "risk"), selected)
    return None


def understand_query(query, products, selected=None, context_snapshot: ContextSnapshot | None = None):
    from app.agent.intent_engine import parse_intent
    parsed = parse_intent(query, selected, context_snapshot)
    names = tuple(value for p in products for value in (p.title, p.id, p.external_product_id, p.sku) if value)
    references = list(dict.fromkeys(REFERENCE.findall(query)))
    mentions = query_mentions(query, names) if parsed.policy.selection_mode != "ignore" else []
    # A reference is contextual metadata, not a literal catalogue lookup.
    mentions = [m for m in mentions if not REFERENCE.fullmatch(m) and m not in {"这个", "那个", "那些", "这", "那", "哪些", "哪些还是新鲜"}]
    if parsed.name in SEMANTIC_TYPES and references:
        mentions = [m for m in mentions if not any(m == ref or m.startswith(ref + "的") for ref in references)]
    dims = list(parsed.policy.requested_dimensions)
    metrics = [field for field, tokens in (("current_price", ("价格", "售价")), ("net_margin", ("净利率", "利润率")), ("sales_growth_rate", ("增速", "增长率"))) if any(token in query for token in tokens)]
    resolved = [resolve_entity(products, mention) for mention in mentions]
    # Default detail is high confidence only for a bare name or explicit detail verb.
    known_detail = bool(mentions) and not re.search(r"为什么|怎么|哪里|是否|[?？]|\b(?:why|how)\b", query, re.I) and (
        any(item.resolved for item in resolved) or bool(re.match(r"^(?:请)?(?:查询|查看|分析|对比|比较)", query))
    )
    supported = parsed.recognized or known_detail
    has_context_target = bool(context_snapshot and any((
        context_snapshot.current_selected_product_ids,
        context_snapshot.last_explicit_product_ids,
        context_snapshot.last_resolved_product_ids,
        context_snapshot.last_comparison_order,
    )))
    contextual_handoff = has_context_target and (
        bool(references)
        or (not mentions and parsed.policy.selection_mode != "ignore")
        # A generic detail phrase whose apparent entity does not exist may be
        # colloquial task language. Let the semantic provider extract the span
        # instead of asserting NOT_FOUND before the task is understood.
        or (bool(mentions) and not any(item.resolved for item in resolved) and parsed.name == "product_detail")
    )
    semantic_complex = bool(re.search(r"哪个好|如果只能留一个|利润优先|风险其次|你怎么看|为什么这么低", query)) or contextual_handoff
    # A catalogue entity is evidence about identity, not evidence that the rule
    # router understood the user's task. Only a recognized deterministic grammar
    # may take the fast path; all other meaningful utterances need semantic handoff.
    route = "DETERMINISTIC_FAST_PATH" if parsed.recognized and not semantic_complex else "SEMANTIC_PLANNER"
    # Pre-semantic entity ambiguity is authoritative only when the deterministic
    # grammar already understood the task. For an otherwise unparsed utterance,
    # a trailing scope/preference phrase may be attached to a valid product name;
    # let the semantic provider extract the exact spans before asking the user.
    if parsed.recognized and supported and any(item.status in {"AMBIGUOUS", "LOW_CONFIDENCE"} for item in resolved):
        route = "CLARIFICATION"
    if not supported:
        # Do not assert that an unparsed sentence is a missing product.
        if not any(item.resolved for item in resolved):
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
        policy_requested="policy" in parsed.name, requires_context=bool(references) or (not mentions and parsed.policy.selection_mode != "ignore"),
        confidence=0.95 if parsed.recognized else 0.65 if any(item.resolved for item in resolved) else 0.4,
        route=route, planned_tools=list(parsed.policy.allowed_tools),
    )
    return parsed, understanding


def build_semantic_context_packet(
    query: str,
    understanding: QueryUnderstanding,
    snapshot: ContextSnapshot,
    products,
) -> SemanticContextPacket:
    """Publish minimal verified identities and task metadata, never product metrics."""
    names = {item.id: item.title for item in products}
    referenced_ids = list(dict.fromkeys(
        snapshot.current_selected_product_ids
        + snapshot.last_explicit_product_ids
        + snapshot.last_resolved_product_ids
        + snapshot.last_comparison_order
    ))
    return SemanticContextPacket(
        query=query,
        session_id=snapshot.session_id,
        turn_index=snapshot.turn_index,
        current_selected_product_ids=snapshot.current_selected_product_ids,
        last_explicit_product_ids=snapshot.last_explicit_product_ids,
        last_resolved_product_ids=snapshot.last_resolved_product_ids,
        last_comparison_order=snapshot.last_comparison_order,
        last_intent=snapshot.last_intent,
        last_response_type=snapshot.last_response_type,
        last_requested_dimensions=snapshot.last_requested_dimensions,
        last_metric=snapshot.last_metric,
        last_preference_order=snapshot.last_preference_order,
        last_negative_scope=snapshot.last_negative_scope,
        reference_candidates=list(understanding.references),
        verified_product_names={item: names[item] for item in referenced_ids if item in names},
        allowed_tools=list(understanding.planned_tools or SEMANTIC_HANDOFF_TOOLS),
        max_tool_calls=10,
        rule_understanding=understanding,
    )


def resolve_understanding(products, understanding):
    results = [resolve_entity(products, mention) for mention in understanding.entity_mentions]
    identities = {p.id: p for p in products}
    ids = list(dict.fromkeys(item.product_id for item in results if item.resolved))
    return EntityResolution([identities[pid] for pid in ids], results)


def arbitrate_entity_mentions(products, understanding, query: str) -> EntityArbitration:
    """Keep explicit catalogue candidates while removing reference/metric noise.

    Provider roles are advisory. A resolved catalogue identity always remains
    explicit. An unresolved span remains explicit when the backend query
    grammar independently extracted it, which prevents stale context from
    replacing a genuinely unknown product. Only unresolved, non-explicit
    spans supported by reference/semantic slots are ignored.
    """
    names = tuple(
        value
        for product in products
        for value in (product.title, product.id, product.external_product_id, product.sku)
        if value
    )
    backend_mentions = query_mentions(query, names)
    normalized_backend = tuple(normalize_entity(item) for item in backend_mentions if normalize_entity(item))
    reference_signal = bool(understanding.references or understanding.reference_slots or REFERENCE.search(query))
    semantic_signal = bool(
        understanding.metric
        or understanding.metrics
        or understanding.requested_dimensions
        or understanding.explanation_requested
        or understanding.provenance_requested
        or understanding.calculation_requested
    )

    retained = []
    roles: list[str] = []
    statuses: list[str] = []
    ignored = 0
    for mention in understanding.entity_mentions:
        result = resolve_entity(products, mention)
        normalized = normalize_entity(mention)
        backend_explicit = bool(normalized) and any(
            normalized == candidate
            or (len(normalized) >= 3 and (normalized in candidate or candidate in normalized))
            for candidate in normalized_backend
        )
        if result.resolved or backend_explicit:
            retained.append(result)
            roles.append("EXPLICIT_PRODUCT")
            statuses.append(result.status)
        elif reference_signal or semantic_signal:
            ignored += 1
            roles.append("REFERENCE" if reference_signal else "SEMANTIC_TERM")
            statuses.append("IGNORED_SEMANTIC_CANDIDATE")
        else:
            retained.append(result)
            roles.append("UNKNOWN")
            statuses.append(result.status)

    identities = {product.id: product for product in products}
    ids = list(dict.fromkeys(item.product_id for item in retained if item.resolved))
    return EntityArbitration(
        resolution=EntityResolution([identities[pid] for pid in ids], retained),
        candidate_roles=tuple(roles),
        candidate_statuses=tuple(statuses),
        ignored_semantic_candidate_count=ignored,
    )


def _has_context_target(context_packet) -> bool:
    if context_packet is None:
        return False
    getter = context_packet.get if isinstance(context_packet, dict) else lambda key, default=None: getattr(context_packet, key, default)
    return any(getter(key, []) for key in (
        "current_selected_product_ids", "last_explicit_product_ids",
        "last_resolved_product_ids", "last_comparison_order",
    ))


def validate_semantic_plan(raw, query, selected=(), context_packet=None):
    try:
        value = QueryUnderstanding.model_validate_json(raw) if isinstance(raw, str) else QueryUnderstanding.model_validate(raw)
    except ValidationError as exc:
        error = exc.errors(include_url=False, include_context=False)[0] if exc.errors() else {}
        field = ".".join(str(part) for part in error.get("loc", ())) or "root"
        raise SemanticPlanValidationFailure(
            failure_code="SCHEMA_VALIDATION_FAILED",
            rule_id="SEM000",
            reason_code=str(error.get("type") or "STRUCTURAL_SCHEMA_INVALID"),
            field=field,
            stage="SCHEMA_VALIDATE",
        ) from exc

    # Rule inventory:
    # SEM001/5/6/7/9 are safety boundaries. SEM002/3 are semantic consistency.
    # SEM004 supplies a deterministic default only where intent fully implies scope.
    # SEM008 deliberately defers missing identity/context to downstream resolution.
    if value.intent not in SEMANTIC_PLANNER_INTENTS:
        _reject("SEM001", "UNSUPPORTED_ACTION", "intent")
    if value.confidence < 0.75:
        safe_missing_context_clarification = (
            value.requires_context
            and not value.entity_mentions
            and not _has_context_target(context_packet)
        )
        if safe_missing_context_clarification:
            value = value.model_copy(update={
                "clarification_required": True,
                "clarification_reason": value.clarification_reason or "缺少可引用的商品上下文，请指定商品。",
            })
        else:
            _reject("SEM002", "LOW_CONFIDENCE_REQUIRES_CLARIFICATION", "confidence")
    if (
        value.intent == "unknown"
        and value.requires_context
        and value.clarification_required
        and not value.entity_mentions
        and value.requested_dimensions
    ):
        scoped = normalize_semantic_values(value.requested_dimensions, value.metrics, value.metric)
        if (
            not scoped.unknown_dimensions
            and not scoped.unknown_metrics
            and set(scoped.dimensions) <= INTENT_ALLOWED_DIMENSIONS["product_detail"]
        ):
            value = value.model_copy(update={"intent": "product_detail"})

    if value.intent == "unknown":
        if not value.clarification_required:
            _reject("SEM003", "INCONSISTENT_CLARIFICATION", "clarification_required")
        return semantic_policy("unknown", ()), value.model_copy(update={"route": "CLARIFICATION", "planner_calls": 1})

    # Scope can be narrowed deterministically when a provider labels an ordinary
    # single-product profit inquiry as a comparison while explicitly saying no
    # comparison was requested.  Two explicit entities or a typed prior-comparison
    # reference remain comparisons.  This never selects a product or creates facts.
    if (
        value.intent == "profit_comparison"
        and not value.comparison_requested
        and len(value.entity_mentions) < 2
        and "last_comparison" not in value.reference_slots
        and not ("ui_selection" in value.reference_slots and len(selected) >= 2)
    ):
        value = value.model_copy(update={"intent": "product_detail", "comparison_required": False})

    normalized = normalize_semantic_values(value.requested_dimensions, value.metrics, value.metric)
    if normalized.unknown_dimensions:
        _reject("SEM004", "INVALID_DIMENSION", "requested_dimensions")
    if normalized.unknown_metrics:
        field = "metric" if value.metric and not value.metrics else "metrics"
        _reject("SEM004", "INVALID_METRIC", field)
    value = value.model_copy(update={
        "requested_dimensions": list(normalized.dimensions),
        "metrics": list(normalized.metrics),
        "metric": normalized.metric,
    })
    if not value.requested_dimensions and value.intent == "product_price":
        value = value.model_copy(update={"requested_dimensions": ["price"]})
    if not value.requested_dimensions or not set(value.requested_dimensions) <= INTENT_ALLOWED_DIMENSIONS[value.intent]:
        _reject("SEM004", "INVALID_DIMENSION", "requested_dimensions")
    if set(value.requested_dimensions) & set(value.negative_scope):
        _reject("SEM005", "CONFLICTING_SCOPE", "negative_scope")
    if any(not mention.strip() or mention not in query or re.search(r"为什么|怎么|来自哪里|[?？]", mention) for mention in value.entity_mentions):
        _reject("SEM006", "INVALID_ENTITY_SPAN", "entity_mentions")
    nonliteral_references = [reference for reference in value.references if reference not in query]
    if nonliteral_references:
        # Some providers label an omitted subject (for example "implicit previous item")
        # instead of returning a literal query span.  Such labels carry no identity and
        # are safe to discard only for a context-required, entity-free frame.  The
        # backend Context Resolver still chooses the actual typed session target.
        if value.requires_context and not value.entity_mentions:
            value = value.model_copy(update={
                "references": [reference for reference in value.references if reference in query],
            })
        else:
            _reject("SEM007", "INVALID_REFERENCE", "references")
    contextual_target = value.requires_context and _has_context_target(context_packet)
    downstream_clarification = (
        not value.entity_mentions and not value.references and not contextual_target
        and value.intent not in {"data_quality_policy", "selection_recommendation"}
    )
    parsed = semantic_policy(value.intent, value.requested_dimensions, selected)
    unknown_tools = [tool for tool in value.planned_tools if TOOL_REGISTRY.get(tool) is None]
    if unknown_tools:
        _reject("SEM009", "UNSUPPORTED_ACTION", "planned_tools")
    backend_tools = list(parsed.policy.allowed_tools)
    advisory_mismatch = [tool for tool in dict.fromkeys(value.planned_tools) if tool not in parsed.policy.allowed_tools]
    value = value.model_copy(update={"route": "SEMANTIC_PLANNER", "planner_calls": 1, "failure_code": None,
                                    "requires_context": not value.entity_mentions and parsed.policy.selection_mode != "ignore",
                                    "requires_tools": bool(backend_tools),
                                    "clarification_required": value.clarification_required or downstream_clarification,
                                    "clarification_reason": value.clarification_reason or (
                                        "缺少可解析的商品或上下文目标。" if downstream_clarification else None
                                    ),
                                    "comparison_required": value.intent in {"product_comparison", "profit_comparison"},
                                    "metric": value.metric or (value.metrics[0] if value.metrics else None),
                                    "advisory_tool_mismatch": advisory_mismatch,
                                    "planned_tools": backend_tools})
    return parsed, value


async def semantic_fallback(query, understanding, planner, selected=(), context_packet=None):
    if planner is None:
        return semantic_policy("unknown", ()), understanding.model_copy(update={"route": "CLARIFICATION", "failure_code": "SEMANTIC_UNAVAILABLE"})
    try:
        planner_input = context_packet.model_dump(mode="json") if hasattr(context_packet, "model_dump") else context_packet or understanding.model_dump(mode="json")
        raw = await asyncio.wait_for(planner(query, planner_input), timeout=15)
        return validate_semantic_plan(raw, query, selected, context_packet)
    except SemanticPlannerFailure as exc:
        return semantic_policy("unknown", ()), understanding.model_copy(
            update={"route": "CLARIFICATION", "planner_calls": 1, "failure_code": exc.failure_code}
        )
    except SemanticPlanValidationFailure as exc:
        return semantic_policy("unknown", ()), understanding.model_copy(
            update={"route": "CLARIFICATION", "planner_calls": 1, "failure_code": exc.failure_code,
                    "clarification_reason": "语义计划未通过后端约束校验。"}
        )
    except TimeoutError:
        return semantic_policy("unknown", ()), understanding.model_copy(
            update={"route": "CLARIFICATION", "planner_calls": 1, "failure_code": "NETWORK_TIMEOUT"}
        )
    except Exception:
        # Single provider boundary: explicitly record failure, never emit exception text
        # or convert an unsupported utterance into a catalogue NOT_FOUND.
        return semantic_policy("unknown", ()), understanding.model_copy(update={"route": "CLARIFICATION", "planner_calls": 1, "failure_code": "SEMANTIC_INVALID_OR_TIMEOUT"})
