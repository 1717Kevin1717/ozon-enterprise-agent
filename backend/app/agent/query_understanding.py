"""Intent-first decomposition over existing routing; no enterprise facts here."""
import asyncio
import json
import re
from dataclasses import dataclass

from pydantic import ValidationError

from app.agent.entity_resolution import EntityResolution, normalize_entity, query_mentions, resolve_entity
from app.agent.tool_registry import TOOL_REGISTRY
from app.schemas.agent import ContextSnapshot, QueryUnderstanding, SemanticContextPacket, SemanticTaskContextPacket
from app.schemas.semantic_vocabulary import (
    INTENT_ALLOWED_DIMENSIONS,
    METRIC_TO_DIMENSIONS,
    SEMANTIC_PLANNER_INTENTS,
    allowed_dimensions_for,
    normalize_semantic_values,
)


REFERENCE = re.compile(
    r"刚才那个(?:商品|产品)?|刚才(?:那|这)(?:两个|几个)(?:商品|产品|候选)?|这个(?:商品|产品|价格|利润|指标|数据|数)?|那个(?:商品|产品)?|"
    r"这几个(?:商品|产品|候选)?|这两个(?:商品|产品|候选)?|这些(?:商品|产品|候选)?|"
    r"我选的这些|这款|那款|该款|它|第[一二两三四五六七八九十\d]+个|前一个|后一个|那利润(?:呢)?|现在比较(?:一下)?|"
    r"剩下(?:的|那个|那些)?|已找到(?:的|那个|那些)?|没找到(?:的|那个|那些)?"
)
COLLECTION_REFERENCE = re.compile(r"候选池|候选集合|商品池|当前候选|这批(?:商品|候选)?|公司商品库|企业商品库|当前(?:结果|集合)|刚才(?:那批|那些|的结果)")
SEMANTIC_TYPES = {"provenance_fact", "calculation_explanation", "decision_explanation", "data_quality_answer", "data_quality_policy"}
SEMANTIC_HANDOFF_TOOLS = ("get_product", "calculate_profit", "get_sales_trend")
REFERENCE_SLOT_ALIASES = {
    "active_collection": "session_state",
    "current_collection": "session_state",
    "collection_state": "session_state",
    "active_collection_ranking": "session_state",
    "collection_ranking": "session_state",
    "current_ranking": "session_state",
    "ranking_state": "session_state",
    "active_result_set": "session_state",
    "ordinal": "ordinal_reference",
    "ordinal_pair": "ordinal_reference",
    "rank_pair": "ordinal_reference",
    "ranked_members": "ordinal_reference",
}


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
        diagnostics: dict | None = None,
    ):
        super().__init__(reason_code)
        self.failure_code = failure_code
        self.rule_id = rule_id
        self.reason_code = reason_code
        self.field = field
        self.stage = stage
        self.safe_detail = safe_detail
        self.diagnostics = diagnostics or {}


@dataclass(frozen=True)
class EntityArbitration:
    """Runtime-only role arbitration before catalogue entity resolution."""

    resolution: EntityResolution
    candidate_roles: tuple[str, ...]
    candidate_statuses: tuple[str, ...]
    ignored_semantic_candidate_count: int = 0


def _reject(
    rule_id: str,
    reason_code: str,
    field: str | None,
    *,
    safe_detail: str | None = None,
    diagnostics: dict | None = None,
):
    raise SemanticPlanValidationFailure(
        failure_code="SEMANTIC_PLAN_VALIDATION_FAILED",
        rule_id=rule_id,
        reason_code=reason_code,
        field=field,
        stage="SEMANTIC_VALIDATE",
        safe_detail=safe_detail,
        diagnostics=diagnostics,
    )


_COLLECTION_TASK_SIGNAL = re.compile(
    r"(?:最值得|优先|推荐|挑|选出|前[一二三四五六七八九十\d]+|top\s*\d+|"
    r"证据|缺口|还缺|资料|进一步(?:验证|研究|调研)|商品方向)",
    re.I,
)
_EXPLICIT_FILTER_SIGNAL = re.compile(
    r"(?:筛选|筛出|只保留|排除|剔除|过滤|满足.*条件|"
    r"(?:哪些|什么).*(?:因为|由于).*(?:不能|无法|不允许).*(?:审核|上架|入选)|"
    r"(?:大于|小于|高于|低于|不低于|不高于|超过|至少|至多|>=|<=|>|<|=)\s*\d)",
    re.I,
)

# Phrase-level vocabulary used by structural collection parsing. These are
# semantic atoms, not complete test utterances: word order and optional prose
# around them do not change the resulting backend contract.
_PLURAL_SCOPE_TERMS = (
    "哪些商品", "哪些候选", "谁", "这批商品", "这批候选", "这批货",
    "这些商品", "这些候选", "当前商品", "当前候选",
)
_EVIDENCE_TERMS = ("证据缺口", "数据缺口", "证据不足", "数据不足", "证据不完整", "缺数据", "缺证据")
_HUMAN_REVIEW_TERMS = ("审核", "复核", "人工确认", "人工介入", "人工查看", "人审", "让人看")
_REVIEW_PRIORITY_TERMS = ("优先", "顺序", "先审", "先看", "最需要", "重点", "紧急程度")
_HISTORY_TERMS = ("历史放弃", "历史拒绝", "历史失败", "历史淘汰", "以前放弃", "被放弃", "类似历史", "同品牌", "同类目")
_DIAGNOSIS_ACTION_TERMS = ("改善", "优化", "调整", "压缩", "降低", "卡在", "影响", "诊断")
_MARGIN_BELOW_TARGET_TERMS = ("低于目标", "未达目标", "未达到目标", "没达目标", "没达到目标", "没有达到目标", "不达目标", "不足目标", "不达标", "未达标", "没达标")


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _safe_semantic_diagnostics(value, normalized) -> dict:
    """Keep finite semantic labels only; never persist provider prose or facts."""
    raw_dimensions = [
        str(item).strip()[:64]
        for item in value.requested_dimensions
        if isinstance(item, str) and item.strip()
    ][:20]
    return {
        "semantic_operation": value.operation,
        "reference_slots": list(value.reference_slots)[:10],
        "raw_requested_dimensions": raw_dimensions,
        "canonical_requested_dimensions": list(normalized.dimensions)[:20],
        "normalization_failures": list(normalized.unknown_dimensions)[:20],
    }


def _scenario_frame_has_deterministic_backend_path(value, task_get) -> bool:
    """Allow advisory labels only when Scenario execution is backend-verifiable."""
    if value.intent != "scenario_analysis":
        return False

    operation = value.operation
    has_active_scenario = bool(task_get("has_active_scenario", False))
    if operation in {"EXPLAIN_SCENARIO", "COMPARE_SCENARIO", "RESET_SCENARIO"}:
        return has_active_scenario
    if operation == "REMOVE_OVERRIDE":
        return has_active_scenario and bool(value.scenario_field)
    if operation not in {"CREATE_SCENARIO", "UPDATE_SCENARIO"}:
        return False
    if operation == "UPDATE_SCENARIO" and not has_active_scenario:
        return False

    if not all((value.scenario_field, value.mutation_type, value.hypothetical_unit)):
        return False
    if value.hypothetical_value is None:
        return False
    try:
        # This is the same finite whitelist/unit/range boundary used by Scenario
        # execution. It creates no business fact and mutates no state.
        from app.agent.scenario_state import ScenarioStateError, normalize_override
        normalize_override(
            field=value.scenario_field,
            operation=value.mutation_type,
            value=value.hypothetical_value,
            unit=value.hypothetical_unit,
            created_revision=1,
        )
    except (ScenarioStateError, TypeError, ValueError):
        return False

    if value.scenario_reference_role == "ACTIVE_COLLECTION":
        has_collection = bool(
            task_get("active_collection_type", "")
            and task_get("collection_member_count", 0)
        )
        return bool(
            has_collection
            and value.scenario_collection_scope
            and (
                value.scenario_collection_scope != "TOP_K"
                or value.scenario_top_k is not None
            )
        )
    return bool(
        value.scenario_reference_role == "EXPLICIT_ENTITY"
        and value.entity_mentions
    )


def _anchor_collection_semantics(query: str, value: QueryUnderstanding) -> QueryUnderstanding:
    """Stabilize provider task labels for explicit collection-analysis language."""
    has_collection = bool(COLLECTION_REFERENCE.search(query))
    has_collection_task = bool(_COLLECTION_TASK_SIGNAL.search(query))
    valid_collection_scenario_mutation = bool(
        has_collection
        and value.intent == "scenario_analysis"
        and value.operation in {"CREATE_SCENARIO", "UPDATE_SCENARIO"}
        and value.scenario_field is not None
        and value.mutation_type is not None
        and value.hypothetical_value is not None
        and value.hypothetical_unit is not None
        and value.scenario_collection_scope is not None
    )
    if valid_collection_scenario_mutation:
        # Collection wording supplies the target of a valid hypothetical
        # mutation; it must not replace the scenario intent.  Normalize stale
        # provider references so downstream resolution reads the session-local
        # ActiveCollection rather than an unrelated ResultSet.
        return value.model_copy(update={
            "scenario_reference_role": "ACTIVE_COLLECTION",
            "collection_reference": "current_collection",
            "requires_context": True,
            "requires_task_state": True,
        })
    if not (has_collection and has_collection_task) or _EXPLICIT_FILTER_SIGNAL.search(query):
        return value

    dimensions = ["recommendation", "decision"]
    for dimension, pattern in (
        ("profit", r"利润|净利率|赚钱|盈利"),
        ("roi", r"\broi\b"),
        ("risk", r"风险"),
        ("demand", r"需求|销量|趋势"),
        ("competition", r"竞争"),
        ("compliance", r"合规"),
        ("provenance", r"来源|出处|可追溯"),
        ("freshness", r"时效|新鲜度|最近更新"),
    ):
        if re.search(pattern, query, re.I):
            dimensions.append(dimension)
    gaps = bool(re.search(r"证据|缺口|缺失|还缺|资料|数据不足|不完整", query))
    if gaps:
        dimensions.extend(["evidence", "evidence_gap"])

    top_match = re.search(r"(?:前|top|推荐|选出|挑).*?(\d+)\s*(?:个|件|款)?", query, re.I)
    chinese_top = next(
        (count for word, count in (("一个", 1), ("两个", 2), ("三个", 3), ("四个", 4), ("五个", 5)) if word in query),
        None,
    )
    top_k = value.top_k or (max(1, min(20, int(top_match.group(1)))) if top_match else chinese_top)
    operation = "RECOMMEND_TOP_K" if top_k else "IDENTIFY_EVIDENCE_GAPS" if gaps else "ANALYZE_COLLECTION"
    return value.model_copy(update={
        "intent": "collection_analysis",
        "task_type": "collection_analysis",
        "question_type": "collection_analysis",
        "operation": operation,
        "requested_dimensions": list(dict.fromkeys(dimensions)),
        "top_k": top_k,
        "evidence_gap_requested": gaps,
        "clarification_required": False,
    })


def _anchor_active_collection_ranking(
    query: str,
    value: QueryUnderstanding,
    task_context,
) -> QueryUnderstanding:
    """Bind ordinal ranking explanations to the active collection revision."""
    task_get = (
        task_context.get
        if isinstance(task_context, dict)
        else lambda key, default=None: getattr(task_context, key, default)
    ) if task_context is not None else lambda key, default=None: default
    has_collection = bool(task_get("collection_member_count", 0))
    has_ranking = bool(task_get("has_collection_ranking_state", False))
    has_result_set = bool(task_get("result_set_count", 0))
    if not ((has_collection and has_ranking) or has_result_set) or _EXPLICIT_CANONICAL_COLLECTION.search(query):
        return value
    if is_ranking_policy_question(query):
        return value
    if value.intent == "product_detail" and value.ordinal_reference is not None:
        return value
    if not has_collection and has_result_set and value.intent not in {"unknown", "collection_analysis"}:
        return value
    if task_get("has_active_scenario", False) and _scenario_rank_reference(query, value) != (None, None):
        return value
    # Ordinals are positions in the current ranking, never catalogue names.
    ordinals = _rank_ordinals(query)
    if not ordinals or _EXPLICIT_SCENARIO_MUTATION.search(query):
        return value
    if value.entity_mentions and any(not _is_ordinal_span(item) for item in value.entity_mentions):
        return value
    asks_reason = bool(re.search(r"为什么|为何|因何|原因|依据|凭什么|怎么排|值得|差在哪|高过", query))
    _, explicit_metrics, _ = _explicit_scenario_dimensions(query)
    if len(ordinals) == 1 and explicit_metrics and not asks_reason:
        return value
    # Existing focused-member continuation owns a single "第 N 个" reason
    # request and records FocusedMemberState; ranking ordinals use this anchor.
    if asks_reason and len(ordinals) == 1 and re.search(r"第\s*[一二两三四五六七八九十\d]+\s*个", query):
        return value
    comparing = len(ordinals) > 1 or bool(re.search(r"前[两二2]名|前[两二2]个|top\s*1\s*(?:vs|和|与|跟)\s*top\s*2", query, re.I))
    if comparing and len(ordinals) == 1:
        ordinals = [1, 2]
    operation = "EXPLAIN_RANKING" if asks_reason else "COMPARE_COLLECTION_MEMBERS" if comparing else "LOOKUP_ORDINAL_MEMBER"
    dimensions = list(task_get("collection_ranking_dimensions", []) or []) if has_collection else []
    if not dimensions:
        dimensions = ["recommendation"]
    slots = list(dict.fromkeys([*value.reference_slots, "session_state", "ordinal_reference"]))
    return value.model_copy(update={
        "intent": "collection_analysis",
        "task_type": "collection_analysis",
        "question_type": "collection_analysis",
        "operation": operation,
        "entity_mentions": [],
        "entity_roles": [],
        "collection_reference": "current_collection" if has_collection else "active_result_set",
        "requested_dimensions": dimensions,
        "reference_slots": slots,
        "ordinal_reference": ordinals[0],
        "ordinal_references": ordinals,
        "top_k": len(ordinals),
        "requires_context": True,
        "requires_task_state": True,
        "requires_reasoning": False,
        "clarification_required": False,
    })


_RANK_ORDINAL = re.compile(
    r"第\s*(?P<number>[一二两三四五六七八九十\d]+)\s*(?:名|位|个)|"
    r"(?P<leader>榜首|首位|排头名|最靠前|最前面|排名最高|(?:榜单|排名|排在|排|前面)第?(?:一|1)|top\s*1)|"
    r"(?P<second>top\s*2)", re.I,
)


def _rank_ordinals(query: str) -> list[int]:
    values = []
    for match in _RANK_ORDINAL.finditer(query):
        number = _finite_chinese_number(match.group("number")) if match.group("number") else 2 if match.group("second") else 1
        if number is not None and number not in values:
            values.append(number)
    if re.search(r"前(?:面)?(?:的)?[两二2](?:名|个|位|款)|前两个", query) and len(values) < 2:
        values = [1, 2]
    return values


def _is_ordinal_span(value: str) -> bool:
    value = re.sub(r"^(?:呃|嗯|额|哦|麻烦|请问|帮我|刚才)[，,\s]*", "", value.strip())
    return not value or bool(_RANK_ORDINAL.search(value) or re.search(r"前(?:面)?(?:的)?[两二2](?:名|个|位|款)", value))


def is_ranking_policy_question(query: str) -> bool:
    """A question about whether relative position/score authorizes a decision."""
    return bool(
        re.search(r"排名|第一名|评分|分数|综合分", query)
        and re.search(r"推荐|上架|决策|审核", query)
        and re.search(r"是否|是不是|代表|意味着|表示|等于|能否|能不能|绕过", query)
    )


_SCENARIO_ORDINAL = re.compile(
    r"第(?P<ordinal>[一二两三四五六七八九十\d]+)(?:名|个|位)|"
    r"(?:排名|排在|排)第?(?P<rank>[一二两三四五六七八九十\d]+)|"
    r"(?P<leader>榜首|首位|排头名|榜单第一|排名第一|谁排第一|最靠前|最前面|排名最高|top\s*1)", re.I
)
_SCENARIO_TOP_K = re.compile(r"(?:前|top\s*)(?P<count>[一二两三四五六七八九十\d]+)(?:名|个|位)?", re.I)
_EXPLICIT_CANONICAL_COLLECTION = re.compile(
    r"(?:原(?:来的?)?(?:候选池|候选集合|集合|排名)|"
    r"原始(?:候选池|候选集合|集合|排名)|canonical)",
    re.I,
)
_EXPLICIT_SCENARIO_MUTATION = re.compile(
    r"(?:假设|如果|若|下降|降低|减少|上升|提高|增加|"
    r"改成|改为|设为|调整|恢复|还原|重置|取消.*(?:假设|调整|限制))"
)


def _finite_chinese_number(raw: str | None) -> int | None:
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    digits = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if raw == "十":
        return 10
    if "十" in raw:
        left, right = raw.split("十", 1)
        return digits.get(left, 1) * 10 + digits.get(right, 0)
    return digits.get(raw)


def _scenario_rank_reference(query: str, value: QueryUnderstanding) -> tuple[int | None, int | None]:
    """Return one-based ordinal/top-k semantics without resolving product identity."""
    top_match = _SCENARIO_TOP_K.search(query)
    top_k = _finite_chinese_number(top_match.group("count")) if top_match else None
    ordinal_match = _SCENARIO_ORDINAL.search(query)
    ordinal = value.ordinal_reference
    if ordinal is None and ordinal_match:
        ordinal = 1 if ordinal_match.group("leader") else _finite_chinese_number(
            ordinal_match.group("ordinal") or ordinal_match.group("rank")
        )
    if ordinal is None and value.ordinal_references:
        ordinal = value.ordinal_references[0]
    return ordinal, top_k


def _explicit_scenario_dimensions(query: str) -> tuple[list[str], list[str], str | None]:
    dimensions: list[str] = []
    metrics: list[str] = []
    metric = None
    for dimension, pattern in (
        ("profit", r"利润|净利率|利润率|赚钱|盈利"),
        ("roi", r"roi|投资回报率"),
        ("risk", r"风险"),
        ("recommendation", r"推荐|值得|继续研究"),
    ):
        if re.search(pattern, query, re.I):
            dimensions.append(dimension)
    for candidate, pattern in (
        ("net_margin", r"净利率|利润率"),
        ("net_profit", r"净利润"),
        ("roi", r"roi|投资回报率"),
        ("risk_level", r"风险(?:等级|级别)?"),
        ("recommendation_score", r"推荐分|推荐度"),
    ):
        if re.search(pattern, query, re.I):
            metrics.append(candidate)
            metric = metric or candidate
    return list(dict.fromkeys(dimensions)), list(dict.fromkeys(metrics)), metric


def _anchor_active_scenario_ordinal(
    query: str,
    value: QueryUnderstanding,
    task_context,
) -> QueryUnderstanding:
    """Prefer the session-local hypothetical ranking for ordinal follow-ups."""
    task_get = (
        task_context.get
        if isinstance(task_context, dict)
        else lambda key, default=None: getattr(task_context, key, default)
    ) if task_context is not None else lambda key, default=None: default
    ordinal, top_k = _scenario_rank_reference(query, value)
    has_rank_reference = ordinal is not None or top_k is not None
    mutation = bool(
        value.operation in {
            "CREATE_SCENARIO", "UPDATE_SCENARIO", "REMOVE_OVERRIDE",
            "RESET_SCENARIO", "COMPARE_SCENARIO",
        }
        or value.scenario_field is not None
        or value.mutation_type is not None
        or _EXPLICIT_SCENARIO_MUTATION.search(query)
    )
    if not (
        task_get("has_active_scenario", False)
        and has_rank_reference
        and not mutation
        and not _EXPLICIT_CANONICAL_COLLECTION.search(query)
    ):
        return value

    explicit_dimensions, explicit_metrics, explicit_metric = _explicit_scenario_dimensions(query)
    dimensions = explicit_dimensions or ["recommendation", "decision"]
    ordinals = [ordinal] if ordinal is not None else []
    slots = list(dict.fromkeys([*value.reference_slots, "session_state", "ordinal_reference"]))
    return value.model_copy(update={
        "intent": "scenario_analysis",
        "task_type": "scenario_analysis",
        "question_type": "scenario_analysis",
        "operation": "EXPLAIN_SCENARIO",
        "entity_mentions": [],
        "entity_roles": [],
        "scenario_reference_role": "ACTIVE_SCENARIO",
        "collection_reference": None,
        "requested_dimensions": dimensions,
        "metrics": explicit_metrics,
        "metric": explicit_metric,
        "reference_slots": slots,
        "ordinal_reference": ordinal,
        "ordinal_references": ordinals,
        "top_k": top_k,
        "requires_context": True,
        "requires_task_state": True,
        "requires_reasoning": False,
        "clarification_required": False,
        "clarification_reason": None,
        "confidence": max(value.confidence, 0.95),
    })


def semantic_policy(intent, dimensions, selected=()):
    from app.agent.intent_engine import ParsedIntent, _policy
    tools = () if intent in {"data_quality_policy", "compliance_policy", "recommendation_policy", "unknown"} else (
        ("filter_products",) if intent == "product_filter" else
        ("compare_products",) if intent == "product_comparison" else
        ("compare_products", "calculate_profit") if intent == "profit_comparison" else
        ("filter_products", "compare_products") if intent == "selection_recommendation" else
        ("analyze_collection",) if intent == "collection_analysis" else
        ("get_product", "get_sales_trend") if "sales_snapshot" in dimensions else
        ("get_product", "calculate_profit") if intent == "calculation_explanation" else
        ("get_product", "calculate_profit") if intent == "product_detail" and ({"profit", "roi"} & set(dimensions)) else
        ("get_product",)
    )
    budget = 10 if intent == "data_quality_answer" else 11 if intent == "profit_comparison" else 1 if intent in {"selection_recommendation", "collection_analysis"} else len(tools)
    selection = "ignore" if not tools or intent == "collection_analysis" else "query_first"
    return ParsedIntent(intent, selected_product_ids=tuple(selected or ()), policy=_policy(tools, budget, tuple(dimensions or ()), selection=selection, fast=True))


def semantic_collection_composite_policy(dimensions, selected=(), analyses=()):
    from app.agent.intent_engine import ParsedIntent, _policy
    tools = ["analyze_collection"]
    if "PROFIT_DIAGNOSIS" in analyses:
        tools.append("calculate_profit")
    if "HISTORY_ANALYSIS" in analyses:
        tools.append("find_historical_failures")
    tools = tuple(tools)
    return ParsedIntent(
        "collection_analysis", selected_product_ids=tuple(selected or ()),
        plan=(
            {"tool": "analyze_collection", "purpose": "对会话内候选集合执行确定性选择、筛选与排序"},
            {"tool": "find_historical_failures", "purpose": "仅在用户请求历史决策分析时查询企业案例"},
        ),
        policy=_policy(tools, 21, tuple(dimensions or ()), selection="ignore", fast=False),
    )


def semantic_intent(query, selected=(), context_snapshot: ContextSnapshot | None = None):
    """Grammatical question/metric patterns, independent of catalogue titles."""
    q = query.casefold()
    previous_dimensions = tuple(context_snapshot.last_requested_dimensions) if context_snapshot else ()
    previous_fact = context_snapshot.last_fact_dimension if context_snapshot else None
    single_recommendation = bool(re.search(r"推荐\s*(?:一|1)个", q)) and not re.search(r"证据|缺口|分析.*集合|分析.*池", q)
    implicit_collection_request = bool(re.search(r"哪些.*最值得.*(?:验证|研究|调研)", q))
    if (COLLECTION_REFERENCE.search(q) or implicit_collection_request) and not single_recommendation and re.search(r"分析|研究|方向|最值得|推荐|优先|挑|前[一二三四五\d]+|证据|缺口|资料|重排|重新?排|验证", q):
        dimensions = ["recommendation", "decision"]
        for dimension, pattern in (
            ("profit", r"利润|净利率|赚钱"), ("roi", r"\broi\b"), ("risk", r"风险"),
            ("demand", r"需求|销量|趋势"), ("competition", r"竞争"), ("compliance", r"合规"),
        ):
            if re.search(pattern, q, re.I):
                dimensions.append(dimension)
        if re.search(r"证据|缺口|缺失|资料|数据不足", q):
            dimensions.extend(["evidence", "evidence_gap"])
        return semantic_policy("collection_analysis", tuple(dict.fromkeys(dimensions)), selected)
    if re.search(r"过期|新鲜|时效|旧了", q) and re.search(r"推荐|维持|继续", q):
        return semantic_policy("decision_explanation", ("decision_reason", "evidence", "freshness", "recommendation"), selected)
    if "完整度" in q and re.search(r"代表|意味|等于|足够|可靠|靠谱|直接.*上架|就.*上架", q):
        return semantic_policy("data_quality_policy", ("data_quality", "policy"))
    if re.search(r"过期|旧了|多久.*采|何时.*采|什么时候.*采|前采", q) and re.search(r"作为.*依据|能否.*使用|还能.*用|是否.*可信", q) and not re.search(r"这个|那个|它|具体|商品名|product[_ -]?id", q):
        return semantic_policy("data_quality_policy", ("freshness", "policy"))
    if re.search(r"第[一二两三四五六七八九十\d]+个|排第[一二两三四五六七八九十\d]+|前一个|后一个", q) and re.search(r"为什么|为何|怎么.*低|原因", q):
        return semantic_policy("decision_explanation", ("decision_reason", "evidence"), selected)
    if re.search(r"为什么|为何|主要卡在", q) and re.search(r"推荐|上架|不能上|卡在", q):
        return semantic_policy("decision_explanation", ("decision_reason", "evidence"), selected)
    if re.search(r"怎么算|如何算|怎么.*(?:算|得|来)|计算(?:依据|公式|过程)|如何.*计算|亏在哪些成本|成本.*构成|成本.*(?:最大|最高|主要|前[一二两三四五六七八九十\d]+|top)|利润依据", q) and re.search(r"利润|roi|%|％|指标|这个数|成本", q):
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


def _collection_composition(query: str) -> dict | None:
    """Extract collection reference, selector/filter, and requested analyses only."""
    implicit_metric_collection = bool(re.search(
        r"(?:当前|这批|这些)?.*?(?:推荐度|推荐分|综合表现|最值得|利润|净利率|roi|风险|竞争|需求|合规)"
        r".*?(?:最高|最低|最好|倒数)(?:的)?.*?[\d一二两三四五六七八九十]+(?:个|名)|"
        r".*?(?:前[\d一二两三四五六七八九十]+|top\s*\d+)"
        r".*?(?:商品|候选|比较|对比|分析|拿出来)",
        query, re.I,
    ))
    implicit_dual_selector = bool(
        len(re.findall(
            r"(?:推荐度|推荐分|净利润|(?<!净)利润).{0,6}?(?:top\s*\d+|前[\d一二两三四五六七八九十]+)",
            query,
            re.I,
        )) >= 2
        and _has_any(query, ("比较", "对比", "区别", "差异", "同一批"))
    )
    implicit_ranked_collection = implicit_metric_collection or implicit_dual_selector or bool(re.search(
        r"(?:前[\d一二两三四五六七八九十]+名|排名前[\d一二两三四五六七八九十]+|"
        r"(?:推荐度|推荐分|最值得|利润|净利率|roi|风险|竞争|需求|合规).*?(?:最高|最低|研究).*?[\d一二两三四五六七八九十]+个)"
        r".*?(?:比较|对比|分析|说明|拿出来|同一批|区别|差异)",
        query, re.I,
    )) or bool(re.search(
        r"最值得(?:继续)?(?:研究|验证|调研).*?[\d一二两三四五六七八九十]+(?:个|款|件)?(?:商品|候选)",
        query, re.I,
    ))
    evidence_analysis = _has_any(query, _EVIDENCE_TERMS) or bool(
        ("数据" in query or "证据" in query) and _has_any(query, ("缺口", "缺失", "不足", "不完整"))
    )
    human_review_language = _has_any(query, _HUMAN_REVIEW_TERMS) or bool(
        "人工" in query and _has_any(query, ("审核", "复核", "确认", "介入", "查看"))
    )
    review_analysis = human_review_language and (
        _has_any(query, _REVIEW_PRIORITY_TERMS)
        or _has_any(query, ("需要人工", "应该人工", "人工重点"))
        or bool(re.search(r"先.{0,8}(?:人工)?(?:审核|复核|确认|介入|查看|让人看)", query))
    )
    history_analysis = _has_any(query, _HISTORY_TERMS)
    profit_diagnosis_analysis = bool(
        re.search(r"净利率|净利润率|利润率", query)
        and ("价格" in query or "成本" in query)
        and _has_any(query, _DIAGNOSIS_ACTION_TERMS)
    )
    implicit_review_collection = bool(
        review_analysis
        and not re.search(r"这几个|这两个|利润.*优先|风险.*其次|只能留|怎么选", query)
        and not re.search(r"能不能|是否可以|可不可以|能否|上架|售卖|先卖", query)
    )
    plural_collection_analysis = (
        _has_any(query, _PLURAL_SCOPE_TERMS)
        and (evidence_analysis or review_analysis or history_analysis or profit_diagnosis_analysis)
    ) or implicit_review_collection or bool(
        profit_diagnosis_analysis and _has_any(query, ("候选", "这批", "当前"))
    )
    if not COLLECTION_REFERENCE.search(query) and not implicit_ranked_collection and not plural_collection_analysis:
        return None
    source = (
        "COMPANY_CATALOG" if re.search(r"公司商品库|企业商品库", query) else
        "ACTIVE_RESULT_SET" if re.search(r"当前结果|刚才.*结果|刚才筛出", query) else
        "ACTIVE_COLLECTION"
    )
    metric_patterns = (
        ("recommendation_score", r"推荐度|推荐分|综合表现|最值得"),
        ("net_margin", r"净利率|净利润率|利润率"),
        ("net_profit", r"净利润|利润"),
        ("roi", r"(?<![A-Za-z])roi(?![A-Za-z])|投资回报"),
        ("risk", r"风险"), ("competition", r"竞争"),
        ("demand", r"需求|销量"), ("compliance", r"合规"),
    )
    metric = next((name for name, pattern in metric_patterns if re.search(pattern, query, re.I)), None)
    count_match = re.search(
        r"(?:最高|最低|最好|前|top\s*)的?([\d一二两三四五六七八九十]+)个?|"
        r"最值得(?:继续)?(?:研究|验证|调研).*?([\d一二两三四五六七八九十]+)(?:个|款|件)?",
        query, re.I,
    )
    limit = _finite_chinese_number(next((item for item in count_match.groups() if item), None)) if count_match else None
    selector = None
    if metric and re.search(r"最高|最低|最好|前[\d一二两三四五六七八九十]+|倒数|top|最值得(?:继续)?(?:研究|验证|调研)", query, re.I):
        selector = {
            "source": source, "metric": metric,
            "direction": "ASC" if re.search(r"最低|倒数", query) else "DESC",
            "limit": max(1, min(20, limit)) if limit else 1,
        }
    # A comparison may select more than one ranked group from the same active
    # collection (for example, profit Top-K versus recommendation Top-K). Keep
    # those selectors structured instead of collapsing them into whichever
    # metric happens to appear first in the vocabulary table.
    ranked_groups = []
    for name, pattern in metric_patterns:
        group_match = re.search(
            rf"(?:{pattern}).{{0,8}}?(?:(最高|最低|倒数).{{0,4}}?([\d一二两三四五六七八九十]+)|(?:top\s*|前)([\d一二两三四五六七八九十]+))\s*(?:个|名)?",
            query,
            re.I,
        )
        if not group_match:
            continue
        group_limit = _finite_chinese_number(group_match.group(2) or group_match.group(3))
        ranked_groups.append({
            "metric": name,
            "direction": "ASC" if group_match.group(1) in {"最低", "倒数"} else "DESC",
            "limit": max(1, min(20, group_limit or 1)),
        })
    if len(ranked_groups) > 1:
        selector = {"source": source, "groups": ranked_groups}
    filters = []
    if re.search(r"净利率|净利润率|利润率", query):
        if _has_any(query, _MARGIN_BELOW_TARGET_TERMS):
            filters.append({
                "field": "net_margin", "operator": "LT", "value": None,
                "reference_field": "target_margin_rate", "reference_value_source": "PRODUCT_MASTER",
            })
        else:
            number = re.search(r"(?:低于|小于|高于|大于|不低于|不高于) *(\d+(?:\.\d+)?)\s*%", query)
            if number:
                operator = "GTE" if re.search(r"不低于", query) else "LTE" if re.search(r"不高于", query) else "GT" if re.search(r"高于|大于", query) else "LT"
                filters.append({"field": "net_margin", "operator": operator, "value": float(number.group(1)) / 100, "reference_field": None, "reference_value_source": "QUERY"})
    roi_threshold = re.search(
        r"(?:roi|投资回报率)\s*(?:低于|小于|不到|高于|大于|不低于|不高于|超过|至少|未达|不达)\s*(\d+(?:\.\d+)?)\s*[%％]",
        query, re.I,
    )
    if roi_threshold:
        operator = "GTE" if re.search(r"不低于|至少", query) else "LTE" if re.search(r"不高于", query) else "GT" if re.search(r"高于|大于|超过", query) else "LT"
        filters.append({"field": "roi", "operator": operator, "value": float(roi_threshold.group(1)) / 100, "reference_field": None, "reference_value_source": "QUERY"})
    if re.search(r"合规.*(?:未通过|没通过|失败|阻断)", query):
        filters.append({"field": "compliance_status", "operator": "NE", "value": "approved", "reference_field": None, "reference_value_source": "QUERY"})
    risk_match = re.search(r"([高中低])风险|风险(?:等级)?(?:为|是)?([高中低])(?!于|过|达|\d)", query)
    if risk_match:
        risk_value = {"低": 0, "中": 1, "高": 2}[risk_match.group(1) or risk_match.group(2)]
        filters.append({"field": "risk", "operator": "EQ", "value": risk_value, "reference_field": None, "reference_value_source": "QUERY"})
    evidence_filter_requested = evidence_analysis and _has_any(
        query, ("哪些", "存在", "筛出", "找出", "谁缺", "谁有", "谁的"),
    )
    if evidence_filter_requested:
        filters.append({"field": "evidence_gap", "operator": "MISSING", "value": True, "reference_field": None, "reference_value_source": "BACKEND_EVIDENCE"})

    analyses = []
    if implicit_dual_selector or _has_any(query, ("比较", "对比", "区别", "差异", "是否同一批")):
        analyses.append("COMPARE")
    if profit_diagnosis_analysis:
        analyses.append("PROFIT_DIAGNOSIS")
    if history_analysis:
        analyses.append("HISTORY_ANALYSIS")
    if review_analysis:
        analyses.append("HUMAN_REVIEW_PRIORITY")
    if evidence_analysis:
        analyses.append("EVIDENCE_GAP")
    if not (selector or filters or analyses):
        return None
    dimensions = []
    for dimension, pattern in (
        ("profit", r"利润|净利率"), ("roi", r"(?<![A-Za-z])roi(?![A-Za-z])"),
        ("competition", r"竞争"), ("compliance", r"合规"),
        ("risk", r"风险"), ("recommendation", r"推荐|最值得"),
    ):
        if re.search(pattern, query, re.I):
            dimensions.append(dimension)
    if "EVIDENCE_GAP" in analyses:
        if selector:
            dimensions.append("decision")
        dimensions.extend(["evidence", "evidence_gap"])
        if not selector:
            dimensions.append("provenance")
    if "HUMAN_REVIEW_PRIORITY" in analyses:
        dimensions.append("decision")
    return {
        "source": source, "selector": selector, "filters": filters,
        "analyses": list(dict.fromkeys(analyses)),
        "dimensions": list(dict.fromkeys(dimensions or ["recommendation", "decision"])),
    }


def understand_query(query, products, selected=None, context_snapshot: ContextSnapshot | None = None):
    from app.agent.intent_engine import parse_intent
    from app.agent.scenario_state import parse_scenario_command, scenario_entity_text
    vague_profit_quality = bool(
        re.search(r"(?<!净)利润\s*(?:不好|不佳|较差|差劲|差|不达标|未达标|不足)", query)
        and not re.search(r"净利润|净利率|利润率|\broi\b|投资回报|\d+(?:\.\d+)?\s*[%％]", query, re.I)
    )
    if vague_profit_quality:
        parsed = semantic_policy("unknown", ())
        return parsed, QueryUnderstanding(
            intent="unknown", question_type="metric_clarification",
            clarification_required=True,
            clarification_reason="你希望按净利润、净利率还是 ROI 判断？",
            confidence=0.95, route="CLARIFICATION", planned_tools=[],
        )
    has_rank_state = bool(context_snapshot and (
        context_snapshot.has_active_scenario
        or context_snapshot.task_state.active_collection
        or context_snapshot.task_state.active_result_set
        or context_snapshot.task_state.active_comparison_set
    ))
    ranking_policy_question = is_ranking_policy_question(query)
    if _rank_ordinals(query) and not has_rank_state and not ranking_policy_question and not any(
        product.title and product.title in query for product in products
    ):
        parsed = semantic_policy("unknown", ())
        return parsed, QueryUnderstanding(
            intent="unknown", question_type="ranking_context_clarification",
            clarification_required=True,
            clarification_reason="当前会话没有可引用的商品排名，请先建立候选集合或筛选结果。",
            confidence=0.95, route="CLARIFICATION", planned_tools=[],
        )
    # A relative metric threshold is not a product title. Without a formal
    # threshold for this metric, ask for the value instead of borrowing a
    # different metric's target or guessing from prior product context.
    roi_target_request = bool(
        COLLECTION_REFERENCE.search(query)
        and re.search(r"(?<![A-Za-z])roi(?![A-Za-z])|投资回报率", query, re.I)
        and re.search(r"(?:低于|小于|高于|大于|超过|不低于|不高于|未达|不达).*?目标|目标.*?(?:低于|小于|高于|大于|超过)", query)
    )
    if roi_target_request and not re.search(r"\d+(?:\.\d+)?\s*[%％]", query):
        parsed = semantic_policy("unknown", ())
        return parsed, QueryUnderstanding(
            intent="unknown", question_type="metric_threshold_clarification",
            entity_mentions=[], references=[], requested_dimensions=[],
            collection_reference="current_collection" if context_snapshot and context_snapshot.task_state.active_collection else "candidate_pool",
            clarification_required=True,
            clarification_reason="你希望用多少 ROI 作为目标？请提供百分比。",
            confidence=0.95, route="CLARIFICATION", planned_tools=[],
        )
    scenario_command = parse_scenario_command(
        query,
        has_active_scenario=bool(context_snapshot and context_snapshot.has_active_scenario),
    )
    composition = None if scenario_command else _collection_composition(query)
    if (
        composition
        and _EXPLICIT_FILTER_SIGNAL.search(query)
        and not composition["selector"]
        and (not composition["analyses"] or not COLLECTION_REFERENCE.search(query))
        and not (context_snapshot and context_snapshot.task_state.active_collection and composition["source"] == "ACTIVE_COLLECTION")
    ):
        composition = None
    parsed = (
        semantic_policy("scenario_analysis", ("profit", "roi", "risk", "recommendation", "decision"), selected)
        if scenario_command else semantic_collection_composite_policy(
            composition["dimensions"], selected, composition["analyses"],
        )
        if composition else parse_intent(query, selected, context_snapshot)
    )
    ambiguous_profit_quality = bool(
        not scenario_command
        and not composition
        and re.search(r"利润.*(?:不好|不佳|较差|差劲)", query)
        and not re.search(r"净利润|净利率|利润率|\broi\b|投资回报", query, re.I)
        and not _EXPLICIT_FILTER_SIGNAL.search(query)
    )
    if ambiguous_profit_quality:
        parsed = semantic_policy("unknown", (), selected)
    names = tuple(value for p in products for value in (p.title, p.id, p.external_product_id, p.sku) if value)
    references = list(dict.fromkeys(REFERENCE.findall(query)))
    collection_match = COLLECTION_REFERENCE.search(query)
    collection_reference = None
    if collection_match:
        has_active_collection = bool(context_snapshot and context_snapshot.task_state.active_collection)
        collection_reference = (
            "current_collection" if has_active_collection and re.search(r"候选池|候选集合|商品池|当前候选|这批", collection_match.group(0)) else
            "candidate_pool" if re.search(r"候选池|候选集合|商品池|当前候选|这批", collection_match.group(0)) else
            "company_catalog" if re.search(r"公司商品库|企业商品库", collection_match.group(0)) else
            "active_result_set" if re.search(r"当前结果|刚才.*结果", collection_match.group(0)) else
            "current_collection"
        )
    elif parsed.name == "collection_analysis":
        collection_reference = (
            "current_collection"
            if composition and composition["source"] == "ACTIVE_COLLECTION"
            else "candidate_pool"
        )
    elif context_snapshot and context_snapshot.task_state.active_collection and re.search(r"第[一二两三四五六七八九十\d]+个|这几个|这三个|重新?排|重排|缺什么证据|证据缺口|只看", query):
        collection_reference = "current_collection"
    orphan_collection_followup = bool(
        context_snapshot
        and not context_snapshot.task_state.active_collection
        and not context_snapshot.task_state.active_comparison_set
        and not context_snapshot.task_state.active_result_set
        and re.search(r"第[一二两三四五六七八九十\d]+个|第一名|第二名|第三名", query)
        and re.search(r"缺.*(?:证据|资料|数据)|为什么|为何|原因|依据", query)
    )
    if orphan_collection_followup:
        wants_gaps = bool(re.search(r"缺.*(?:证据|资料|数据)", query))
        parsed = semantic_policy(
            "collection_analysis",
            ("evidence", "evidence_gap") if wants_gaps else ("recommendation", "decision"),
        )
        collection_reference = "current_collection"
    top_match = re.search(r"(?:前|top|推荐|选出|找出).*?(\d+)\s*(?:个|件|款)?", query, re.I)
    chinese_top = next((value for text, value in (("三个", 3), ("两个", 2), ("五个", 5), ("四个", 4), ("一个", 1)) if text in query), None)
    chinese_rank = next((value for text, value in (("前一", 1), ("前二", 2), ("前三", 3), ("前四", 4), ("前五", 5)) if text in query), None)
    top_k = scenario_command.top_k if scenario_command and scenario_command.top_k else max(1, min(20, int(top_match.group(1)))) if top_match else chinese_top or chinese_rank
    if scenario_command and scenario_command.collection_scope:
        collection_reference = "current_collection"
    evidence_gap_requested = bool(re.search(r"证据缺口|缺.*(?:证据|资料|数据)|数据不足", query))
    exclude_insufficient_data = bool(re.search(r"排除|不要|去掉|剔除", query) and re.search(r"证据不足|数据不足|不充分|INSUFFICIENT", query, re.I))
    contextual_scenario_reference = bool(
        scenario_command
        and context_snapshot
        and context_snapshot.has_active_scenario
        and re.search(r"^(?:再|继续|另外|同时|并且|然后)|(?:当前|这个|刚才(?:那个)?).*?(?:假设|情景)|恢复|还原|重置", query)
    )
    entity_query = (
        "" if composition else
        "" if contextual_scenario_reference else scenario_entity_text(query)
        if scenario_command and scenario_command.operation in {"CREATE_SCENARIO", "UPDATE_SCENARIO"}
        else "" if scenario_command else query
    )
    mentions = query_mentions(entity_query, names) if parsed.policy.selection_mode != "ignore" else []
    # A reference is contextual metadata, not a literal catalogue lookup.
    mentions = [m for m in mentions if not REFERENCE.fullmatch(m) and m not in {"这个", "那个", "那些", "这", "那", "哪些", "哪些还是新鲜"}]
    if parsed.name in SEMANTIC_TYPES and references:
        mentions = [m for m in mentions if not any(m == ref or m.startswith(ref + "的") for ref in references)]
    dims = list(parsed.policy.requested_dimensions)
    metrics = [field for field, tokens in (("current_price", ("价格", "售价")), ("net_margin", ("净利率", "利润率")), ("sales_growth_rate", ("增速", "增长率"))) if any(token in query for token in tokens)]
    if re.search(r"成本.*(?:最大|最高|主要|前[一二两三四五六七八九十\d]+|top)", query, re.I):
        metrics = list(dict.fromkeys([*metrics, "cost_breakdown"]))
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
    # A recognized intent is not enough for a deterministic fact lookup: the
    # rule layer must also have a trustworthy target span.  If the query
    # contains a tenant-scoped catalogue title but the extracted span cannot be
    # resolved, semantic orchestration gets the chance to normalize that span
    # instead of FastPath executing against a malformed pseudo-entity.
    unresolved_known_target = bool(
        parsed.policy.selection_mode != "ignore"
        and mentions
        and not any(item.resolved for item in resolved)
        and any(product.title and product.title in query for product in products)
    )
    comparison_refine_signal = bool(
        context_snapshot
        and context_snapshot.task_state.active_comparison_set
        and re.search(r"只比较|只看|只保留|仅比较|再加|加上", query)
    )
    task_operation_signal = comparison_refine_signal or bool(re.search(
        r"(?:取消|移除|删除|去掉|重新|重跑|再次执行|其他.*不变|排序|重排|优先|"
        r"第[一二两三四五六七八九十\d]+个|这些结果|刚才.*条件|"
        r"(?:这些|这批|这几个|当前结果).*(?:最高|最低|最大|最小|最多|最少)|"
        r"(?:第一|榜首).*(?:第二).*(?:为什么|为何|原因)|"
        r"成本.*(?:最大|最高|主要|前[一二两三四五六七八九十\d]+|top)|"
        r"忽略.*(?:没找到|不存在)|只分析.*(?:剩下|已找到)|推荐.*(?:继续调研|调查)|"
        r"(?:改|调整).*(?:净利率|利润率|竞争|风险|合规|条件)|"
        r"(?:净利率|利润率|竞争|风险|合规|条件).*(?:改|调整)|"
        r"假设|如果.*(?:降低|增加|减少)|换成.*(?:看|分析)|不是|改口|"
        r"不做|不要|不分析|无需|不需要|不比较)",
        query,
    ))
    semantic_complex = (
        bool(re.search(r"哪个好|如果只能留一个|利润优先|风险其次|你怎么看|为什么这么低", query))
        or contextual_handoff
        or unresolved_known_target
        or task_operation_signal
        or parsed.name == "collection_analysis"
        or composition is not None
    )
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
    operation = scenario_command.operation if scenario_command else (
        "COMPARE_COLLECTION_MEMBERS" if composition and "COMPARE" in composition["analyses"] else
        "ANALYZE_COLLECTION" if parsed.name == "collection_analysis" else "CREATE"
    )
    if parsed.name == "collection_analysis" and top_k and operation == "ANALYZE_COLLECTION":
        operation = "RECOMMEND_TOP_K"
    if orphan_collection_followup:
        operation = "IDENTIFY_EVIDENCE_GAPS" if evidence_gap_requested else "EXPLAIN_RANKING"
    if orphan_collection_followup:
        route = "DETERMINISTIC_FAST_PATH"
    understanding = QueryUnderstanding(
        intent=parsed.name, question_type=parsed.name,
        entity_mentions=mentions, entity_roles=["EXPLICIT_PRODUCT" for _ in mentions], references=references, metrics=metrics,
        metric_values=[float(v) for v in re.findall(r"([+-]?\d+(?:\.\d+)?)\s*[%％]", query)],
        requested_dimensions=dims if supported else [], collection_reference=collection_reference,
        collection_selector=composition["selector"] if composition else None,
        collection_filters=composition["filters"] if composition else [],
        analysis_requests=composition["analyses"] if composition else [],
        top_k=top_k, evidence_gap_requested=evidence_gap_requested,
        exclude_insufficient_data=exclude_insufficient_data, operation=operation,
        comparison_requested="comparison" in parsed.name,
        explanation_requested=parsed.name in {"calculation_explanation", "decision_explanation"},
        provenance_requested=parsed.name == "provenance_fact", calculation_requested=parsed.name == "calculation_explanation",
        policy_requested="policy" in parsed.name, requires_context=bool(references) or (not mentions and parsed.policy.selection_mode != "ignore") or bool(composition and composition["source"] in {"ACTIVE_COLLECTION", "ACTIVE_RESULT_SET"}),
        requires_task_state=task_operation_signal or bool(composition and composition["source"] in {"ACTIVE_COLLECTION", "ACTIVE_RESULT_SET"}),
        confidence=0.95 if parsed.recognized else 0.65 if any(item.resolved for item in resolved) else 0.4,
        route=route, planned_tools=list(parsed.policy.allowed_tools),
        scenario_field=scenario_command.field if scenario_command else None,
        mutation_type=scenario_command.mutation_type if scenario_command else None,
        hypothetical_value=scenario_command.value if scenario_command else None,
        hypothetical_unit=scenario_command.unit if scenario_command else None,
        scenario_reference_role=(
            "ACTIVE_COLLECTION" if scenario_command and scenario_command.collection_scope else
            "ACTIVE_SCENARIO" if context_snapshot and context_snapshot.has_active_scenario and not mentions else
            "EXPLICIT_ENTITY" if mentions else "NONE"
        ) if scenario_command else None,
        scenario_collection_scope=scenario_command.collection_scope if scenario_command else None,
        scenario_top_k=scenario_command.top_k if scenario_command else None,
        scenario_ranking_dimensions=list(scenario_command.ranking_dimensions) if scenario_command else [],
    )
    # Provider-side semantic normalization applies the same rule later, but a
    # deterministic/offline run must not lose an active Scenario ordinal before
    # provider invocation. This central anchor is state-bound and therefore does
    # not affect ordinary Collection or ResultSet ordinal behavior.
    understanding = _anchor_active_scenario_ordinal(query, understanding, context_snapshot)
    if understanding.intent == "scenario_analysis" and parsed.name != "scenario_analysis":
        parsed = semantic_policy("scenario_analysis", understanding.requested_dimensions, selected)
    if understanding.intent != "scenario_analysis" and context_snapshot:
        task = context_snapshot.task_state
        understanding = _anchor_active_collection_ranking(query, understanding, {
            "collection_member_count": len(task.active_collection.product_ids) if task.active_collection else 0,
            "has_collection_ranking_state": bool(task.active_collection and task.active_collection.ranked_product_ids),
            "collection_ranking_dimensions": task.active_collection.ranking_dimensions if task.active_collection else [],
            "result_set_count": len(task.active_result_set.product_ids) if task.active_result_set else 0,
            "has_active_scenario": context_snapshot.has_active_scenario,
        })
        if understanding.intent == "collection_analysis" and parsed.name != "collection_analysis":
            parsed = semantic_policy("collection_analysis", understanding.requested_dimensions, selected)
            understanding = understanding.model_copy(update={"route": "DETERMINISTIC_FAST_PATH", "confidence": 0.95})
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
    task = snapshot.task_state
    task_entity_ids = list(dict.fromkeys(
        task.active_entities
        + (task.active_result_set.product_ids if task.active_result_set else [])
        + (task.active_comparison_set.product_ids if task.active_comparison_set else [])
        + (task.active_collection.top_product_ids if task.active_collection else [])
    ))[:10]
    result_count = len(task.active_result_set.product_ids) if task.active_result_set else 0
    comparison_count = len(task.active_comparison_set.product_ids) if task.active_comparison_set else 0
    collection_count = len(task.active_collection.product_ids) if task.active_collection else 0
    collection_top_count = len(task.active_collection.top_product_ids) if task.active_collection else 0
    task_context = SemanticTaskContextPacket(
        active_task_type=task.task_type,
        active_operation=task.operation,
        active_entities_display_names=[names[item] for item in task_entity_ids if item in names],
        result_set_count=result_count,
        comparison_count=comparison_count,
        active_filter_spec=task.filter_spec,
        active_sort_spec=task.sort_spec,
        active_dimensions=(task.active_comparison_set.dimensions if task.active_comparison_set else task.active_collection.requested_dimensions if task.active_collection else task.requested_dimensions),
        ordinal_capacity=comparison_count or collection_top_count or result_count,
        has_pending_unresolved_entity=any(item.status in {"NOT_FOUND", "AMBIGUOUS", "LOW_CONFIDENCE"} for item in task.pending_entities),
        pending_entity_statuses=[item.status for item in task.pending_entities],
        has_active_recommendation=task.active_recommendation is not None,
        recommendation_candidate_count=len(task.active_recommendation.candidate_product_ids) if task.active_recommendation else 0,
        active_collection_type=task.active_collection.collection_type if task.active_collection else "",
        collection_member_count=collection_count,
        collection_top_count=collection_top_count,
        collection_top_k=task.active_collection.top_k if task.active_collection else 0,
        collection_ranking_dimensions=task.active_collection.ranking_dimensions if task.active_collection else [],
        has_focused_collection_member=task.focused_collection_member is not None,
        has_collection_ranking_state=bool(task.active_collection and task.active_collection.ranked_product_ids),
        has_active_scenario=snapshot.has_active_scenario,
        last_successful_action=task.last_successful_action,
    )
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
        task_context=task_context,
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
    for index, mention in enumerate(understanding.entity_mentions):
        normalized = normalize_entity(mention)
        backend_explicit = bool(normalized) and any(
            normalized == candidate
            or (len(normalized) >= 3 and (normalized in candidate or candidate in normalized))
            for candidate in normalized_backend
        )
        provider_role = understanding.entity_roles[index] if index < len(understanding.entity_roles) else None
        if provider_role not in {None, "EXPLICIT_PRODUCT", "PRODUCT_ALIAS"} and not backend_explicit:
            ignored += 1
            roles.append(provider_role)
            statuses.append("IGNORED_SEMANTIC_CANDIDATE")
            continue
        result = resolve_entity(products, mention)
        if result.resolved or backend_explicit:
            retained.append(result)
            roles.append("PRODUCT_ALIAS" if provider_role == "PRODUCT_ALIAS" else "EXPLICIT_PRODUCT")
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
        payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
        raw_slots = payload.get("reference_slots")
        if isinstance(raw_slots, list):
            normalized_slots = []
            for raw_slot in raw_slots:
                slot = re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", str(raw_slot).strip().casefold())).strip("_")
                normalized_slots.append(REFERENCE_SLOT_ALIASES.get(slot, slot))
            payload["reference_slots"] = list(dict.fromkeys(normalized_slots))
        ordinal_value = payload.get("ordinal_reference")
        plural_value = payload.get("ordinal_references")
        if isinstance(ordinal_value, list):
            if plural_value not in (None, [], ordinal_value):
                raise ValueError("conflicting ordinal reference shapes")
            payload["ordinal_references"] = ordinal_value
            payload["ordinal_reference"] = ordinal_value[0] if ordinal_value else None
        elif plural_value is not None and not isinstance(plural_value, list):
            payload["ordinal_references"] = [plural_value]
        value = QueryUnderstanding.model_validate(payload)
        ordinals = list(dict.fromkeys(value.ordinal_references or ([value.ordinal_reference] if value.ordinal_reference else [])))
        updates = {"ordinal_references": ordinals}
        if ordinals and value.ordinal_reference is None:
            updates["ordinal_reference"] = ordinals[0]
        if value.intent == "collection_analysis" and len(ordinals) >= 2 and value.operation == "COMPARE_COLLECTION_MEMBERS":
            updates["operation"] = "EXPLAIN_RANKING"
        value = value.model_copy(update=updates)
    except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
        if isinstance(exc, ValidationError):
            error = exc.errors(include_url=False, include_context=False)[0] if exc.errors() else {}
            field = ".".join(str(part) for part in error.get("loc", ())) or "root"
            reason = str(error.get("type") or "STRUCTURAL_SCHEMA_INVALID")
        else:
            field = "ordinal_references" if "ordinal" in str(exc).casefold() else "root"
            reason = "STRUCTURAL_SCHEMA_INVALID"
        raise SemanticPlanValidationFailure(
            failure_code="SCHEMA_VALIDATION_FAILED",
            rule_id="SEM000",
            reason_code=reason,
            field=field,
            stage="SCHEMA_VALIDATE",
        ) from exc

    # Rule inventory:
    # SEM001/5/6/7/9 are safety boundaries. SEM002/3 are semantic consistency.
    # SEM004 supplies a deterministic default only where intent fully implies scope.
    # SEM008 deliberately defers missing identity/context to downstream resolution.
    packet_get = (
        context_packet.get
        if isinstance(context_packet, dict)
        else lambda key, default=None: getattr(context_packet, key, default)
    ) if context_packet is not None else lambda key, default=None: default
    # A session-owned Scenario ranking plus an explicit ordinal is a stronger,
    # deterministic signal than a provider's generic/low-confidence task label.
    # Anchor it before the confidence gate so transport-fallback validation uses
    # the same canonical frame as a successful provider response.
    value = _anchor_active_scenario_ordinal(query, value, packet_get("task_context", {}))
    if value.intent != "scenario_analysis":
        value = _anchor_active_collection_ranking(query, value, packet_get("task_context", {}))
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
    # Canonicalize and reject genuinely unknown provider enum values before any
    # task anchoring.  This prevents a collection anchor from hiding an invalid
    # dimension while still accepting finite aliases deterministically.
    initial_normalized = normalize_semantic_values(
        value.requested_dimensions, value.metrics, value.metric,
    )
    initial_diagnostics = _safe_semantic_diagnostics(value, initial_normalized)
    if initial_normalized.unknown_dimensions:
        _reject(
            "SEM004", "INVALID_DIMENSION", "requested_dimensions",
            diagnostics=initial_diagnostics,
        )
    if initial_normalized.unknown_metrics:
        field = "metric" if value.metric and not value.metrics else "metrics"
        initial_diagnostics["normalization_failures"] = list(initial_normalized.unknown_metrics)[:20]
        _reject("SEM004", "INVALID_METRIC", field, diagnostics=initial_diagnostics)
    value = _anchor_collection_semantics(query, value)
    composition = _collection_composition(query)
    if (
        composition
        and value.intent == "product_filter"
        and _EXPLICIT_FILTER_SIGNAL.search(query)
        and not composition["analyses"]
        and not composition["selector"]
    ):
        composition = None
    if composition:
        task_context = packet_get("task_context", {})
        task_get = (
            task_context.get if isinstance(task_context, dict)
            else lambda key, default=None: getattr(task_context, key, default)
        )
        has_active_collection = bool(task_get("collection_member_count", 0))
        collection_reference = {
            "ACTIVE_COLLECTION": "current_collection" if has_active_collection else "candidate_pool",
            "ACTIVE_RESULT_SET": "active_result_set",
            "COMPANY_CATALOG": "company_catalog",
        }.get(composition["source"], value.collection_reference)
        value = value.model_copy(update={
            "intent": "collection_analysis",
            "task_type": "collection_analysis",
            "question_type": "collection_analysis",
            "operation": (
                "COMPARE_COLLECTION_MEMBERS"
                if "COMPARE" in composition["analyses"] else "ANALYZE_COLLECTION"
            ),
            "entity_mentions": [], "entity_roles": [],
            "collection_reference": collection_reference,
            "collection_selector": composition["selector"],
            "collection_filters": composition["filters"],
            "analysis_requests": composition["analyses"],
            "requested_dimensions": composition["dimensions"],
            "top_k": (composition["selector"] or {}).get("limit") or value.top_k,
            "requires_context": collection_reference in {"current_collection", "active_result_set"},
            "requires_task_state": collection_reference in {"current_collection", "active_result_set"},
        })
    value = _anchor_active_collection_ranking(query, value, packet_get("task_context", {}))
    value = _anchor_active_scenario_ordinal(query, value, packet_get("task_context", {}))

    ordinal_continuation = bool(
        value.ordinal_reference
        or value.ordinal_references
        or re.search(r"第[一二两三四五六七八九十\d]+个|前一个|后一个", query)
    )
    if (
        ordinal_continuation
        and value.intent not in {"scenario_analysis", "collection_analysis"}
        and value.requires_context
        and not value.entity_mentions
        and _has_context_target(context_packet)
        and not value.metrics
        and value.metric is None
    ):
        inherited = normalize_semantic_values(packet_get("last_requested_dimensions", []))
        inherited_dimensions = [
            dimension for dimension in inherited.dimensions
            if dimension in INTENT_ALLOWED_DIMENSIONS["product_detail"]
        ]
        value = value.model_copy(update={
            "intent": "product_price" if inherited_dimensions == ["price"] else "product_detail",
            "requested_dimensions": inherited_dimensions or ["detail"],
            "clarification_required": False,
        })
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

    if value.intent == "collection_analysis" and value.collection_reference is None:
        match = COLLECTION_REFERENCE.search(query)
        current_task = packet_get("task_context", {}) or {}
        current_task_get = current_task.get if isinstance(current_task, dict) else lambda key, default=None: getattr(current_task, key, default)
        has_active_collection = bool(current_task_get("collection_member_count", 0))
        reference = (
            "candidate_pool" if match and (
                re.search(r"候选池|候选集合|商品池|当前候选", match.group(0))
                or (not has_active_collection and re.search(r"这批(?:商品|候选)?", match.group(0)))
            ) else
            "company_catalog" if match and re.search(r"公司商品库|企业商品库", match.group(0)) else
            "active_result_set" if match and re.search(r"当前结果|刚才.*结果", match.group(0)) else
            "current_collection" if match or current_task_get("collection_member_count", 0) or re.search(r"第[一二两三四五六七八九十\d]+个|第一名|第二名|第三名|这批|这几个|这三个", query) else
            "candidate_pool"
        )
        value = value.model_copy(update={"collection_reference": reference})
    if value.intent == "collection_analysis":
        top_match = re.search(r"(?:前|top|推荐|选出|找出).*?(\d+)\s*(?:个|件|款)?", query, re.I)
        chinese_top = next((count for word, count in (("一个", 1), ("两个", 2), ("三个", 3), ("四个", 4), ("五个", 5)) if word in query), None)
        chinese_rank = next((count for word, count in (("前一", 1), ("前二", 2), ("前三", 3), ("前四", 4), ("前五", 5)) if word in query), None)
        top_k = value.top_k or (max(1, min(20, int(top_match.group(1)))) if top_match else chinese_top or chinese_rank)
        gaps = value.evidence_gap_requested or bool(re.search(r"证据缺口|缺.*(?:证据|资料|数据)|数据不足", query))
        updates = {"top_k": top_k, "evidence_gap_requested": gaps}
        if top_k and value.operation in {"CREATE", "ANALYZE_COLLECTION"}:
            updates["operation"] = "RECOMMEND_TOP_K"
        value = value.model_copy(update=updates)

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

    task_context = (
        context_packet.get("task_context")
        if isinstance(context_packet, dict)
        else getattr(context_packet, "task_context", None)
    ) if context_packet is not None else None
    task_get = (
        task_context.get
        if isinstance(task_context, dict)
        else lambda key, default=None: getattr(task_context, key, default)
    )
    if (
        value.operation in {"RECOVER", "REFINE"}
        and task_context is not None
        and task_get("has_pending_unresolved_entity", False)
        and task_get("active_task_type", "") in {"product_comparison", "profit_comparison"}
    ):
        value = value.model_copy(update={
            "intent": "product_detail",
            "operation": "RECOVER",
            "comparison_required": False,
        })

    normalized = normalize_semantic_values(value.requested_dimensions, value.metrics, value.metric)
    if normalized.unknown_dimensions:
        _reject(
            "SEM004", "INVALID_DIMENSION", "requested_dimensions",
            diagnostics=_safe_semantic_diagnostics(value, normalized),
        )
    if normalized.unknown_metrics:
        field = "metric" if value.metric and not value.metrics else "metrics"
        diagnostics = _safe_semantic_diagnostics(value, normalized)
        diagnostics["normalization_failures"] = list(normalized.unknown_metrics)[:20]
        _reject("SEM004", "INVALID_METRIC", field, diagnostics=diagnostics)
    value = value.model_copy(update={
        "requested_dimensions": list(normalized.dimensions),
        "metrics": list(normalized.metrics),
        "metric": normalized.metric,
    })
    # A provider can correctly identify the cost-breakdown metric while using
    # the broader product-detail task label.  The metric is more specific than
    # that label, so normalize it into the existing deterministic calculation
    # contract before intent-scope validation.  Product identity and every
    # amount remain backend-owned.
    if normalized.metric == "cost_breakdown":
        value = value.model_copy(update={
            "intent": "calculation_explanation",
            "explanation_requested": True,
            "calculation_requested": True,
            "requires_tools": True,
        })
    # A RECOVER operation can collapse a partially resolved comparison into a
    # single-product task.  In that transition, comparison-only dimensions are
    # no longer applicable.  Narrow them against the canonical single-product
    # contract; product identity and business facts remain backend-owned.
    if (
        value.operation in {"RECOVER", "REFINE"}
        and value.intent == "product_detail"
        and task_context is not None
        and task_get("has_pending_unresolved_entity", False)
    ):
        narrowed = [
            dimension for dimension in value.requested_dimensions
            if dimension in INTENT_ALLOWED_DIMENSIONS["product_detail"]
        ]
        value = value.model_copy(update={"requested_dimensions": narrowed or ["detail"]})
    if not value.requested_dimensions and value.intent == "product_price":
        value = value.model_copy(update={"requested_dimensions": ["price"]})
    if not value.requested_dimensions and value.intent == "collection_analysis":
        value = value.model_copy(update={"requested_dimensions": ["recommendation", "decision"]})
    if not value.requested_dimensions and value.intent == "scenario_analysis":
        value = value.model_copy(update={"requested_dimensions": ["profit", "roi", "risk", "recommendation", "decision"]})
    allowed_dimensions = allowed_dimensions_for(value.intent, value.operation)
    if not value.requested_dimensions or not set(value.requested_dimensions) <= allowed_dimensions:
        _reject(
            "SEM004", "INVALID_DIMENSION", "requested_dimensions",
            diagnostics=_safe_semantic_diagnostics(value, normalized),
        )
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
        and value.intent not in {"data_quality_policy", "product_filter", "selection_recommendation", "collection_analysis", "scenario_analysis"}
    )
    parsed = (
        semantic_collection_composite_policy(value.requested_dimensions, selected, value.analysis_requests)
        if value.intent == "collection_analysis" and value.analysis_requests
        else semantic_policy(value.intent, value.requested_dimensions, selected)
    )
    unknown_tools = [tool for tool in value.planned_tools if TOOL_REGISTRY.get(tool) is None]
    # Collection suggestions and complete, backend-verifiable Scenario frames
    # treat provider tool labels as advisory. Unknown labels remain visible as
    # mismatches but never reach execution. Every other intent keeps SEM009.
    advisory_unknown_tools_allowed = (
        value.intent == "collection_analysis"
        or _scenario_frame_has_deterministic_backend_path(value, task_get)
    )
    if unknown_tools and not advisory_unknown_tools_allowed:
        _reject("SEM009", "UNSUPPORTED_ACTION", "planned_tools")
    backend_tools = list(parsed.policy.allowed_tools)
    advisory_mismatch = [tool for tool in dict.fromkeys(value.planned_tools) if tool not in parsed.policy.allowed_tools]
    value = value.model_copy(update={"route": "SEMANTIC_PLANNER", "planner_calls": 1, "failure_code": None,
                                    "requires_context": value.requires_context or (
                                        not value.entity_mentions
                                        and parsed.policy.selection_mode != "ignore"
                                        and value.intent not in {"selection_recommendation", "collection_analysis", "scenario_analysis"}
                                    ),
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
