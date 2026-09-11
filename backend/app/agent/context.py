"""Typed, session-scoped conversational context.

This module stores references and ordering only. Product facts are always read
again through tenant-scoped repositories and deterministic tools.
"""
import re
from collections.abc import Iterable

from app.db.models import ConversationSession, Product
from app.schemas.agent import ContextSnapshot, QueryUnderstanding, ReferenceResolutionResult
from app.agent.task_state import load_task_state


_SELECTION_REFERENCE = re.compile(
    r"这几个|这两个|这些(?:商品|产品|候选)?|我选的这些|已选(?:商品|产品|候选)?|"
    r"对比中心|现在比较(?:一下|这些)?|用对比中心|哪些数据"
)
_IGNORE_SELECTION = re.compile(
    r"(?:先别看|不要看|不看|忽略)(?:这几个|这两个|这些|当前|已选|所选|选择|对比中心)?|"
    r"排除(?:这几个|这两个|这些|当前|已选|所选|选择|对比中心)"
)
_ORDINAL = re.compile(r"第([一二两三四五六七八九十\d]+)个|排第([一二两三四五六七八九十\d]+)(?:个|款)?|"
                      r"列表里的第([一二两三四五六七八九十\d]+)(?:个|款)?|前一个|后一个")
_SINGULAR_REFERENCE = re.compile(r"(?:它|这个(?:商品|产品)?|那个(?:商品|产品)?|刚才那个(?:商品|产品)?)")
_ELLIPSIS = re.compile(r"(?:换成|改成).+呢|(?:那|利润|价格|风险|净利率).*(?:呢|方面)|这个(?:利润|价格|数|指标)|它的")
_ONE_ONLY = re.compile(r"只能留一个|只留一个|必须选一个|二选一")
_LAST_COMPARISON_REFERENCE = re.compile(r"刚才(?:那|这)(?:两个|几个)|上面(?:那|这)?(?:两个|几个)|前面(?:那|这)?(?:两个|几个)|和刚才.*相比")

_CN_NUMBERS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _valid_ids(values: Iterable[str] | None, products: list[Product]) -> list[str]:
    available = {item.id for item in products}
    return [value for value in dict.fromkeys(values or ()) if value in available][:10]


def build_context_snapshot(
    conversation: ConversationSession,
    *,
    company_id: str,
    user_id: str,
    query: str,
    products: list[Product],
    selected_product_ids: list[str] | None = None,
    selection_revision: int = 0,
    selection_bound_session_id: str | None = None,
) -> ContextSnapshot:
    """Build the sole per-turn context view from verified session and UI data."""
    state = conversation.state_json or {}
    warnings: list[str] = []

    def valid_state_ids(key: str, fallback: Iterable[str] | None = None) -> list[str]:
        raw = state.get(key, fallback or [])
        valid = _valid_ids(raw if isinstance(raw, list) else [], products)
        if raw and len(valid) != len(list(dict.fromkeys(raw))):
            warnings.append(f"{key} 包含失效或越租户商品，已忽略。")
        return valid

    return ContextSnapshot(
        session_id=conversation.id,
        company_id=company_id,
        user_id=user_id,
        turn_index=max(1, int(state.get("turn_index") or 0) + 1),
        current_query=query,
        current_selected_product_ids=_valid_ids(selected_product_ids, products),
        selection_revision=max(0, int(selection_revision or 0)),
        selection_bound_session_id=selection_bound_session_id,
        last_explicit_product_ids=valid_state_ids("last_explicit_product_ids"),
        last_resolved_product_ids=valid_state_ids("last_resolved_product_ids", conversation.last_product_ids or []),
        last_intent=state.get("last_intent"),
        last_response_type=state.get("last_response_type"),
        last_requested_dimensions=list(state.get("last_requested_dimensions") or state.get("requested_dimensions") or [])[:10],
        # last_fact_dimension was the original canonical fact slot. Read it as
        # a compatibility fallback so sessions created before context-v1.1 can
        # still hand the previous metric to semantic understanding.
        last_metric=state.get("last_metric") or state.get("last_fact_dimension"),
        last_fact_dimension=state.get("last_fact_dimension"),
        last_comparison_product_ids=valid_state_ids("last_comparison_product_ids"),
        last_comparison_order=valid_state_ids("last_comparison_order"),
        last_tool_result_refs=list(state.get("last_tool_result_refs") or [])[:20],
        last_preference_order=list(state.get("last_preference_order") or [])[:10],
        last_negative_scope=list(state.get("last_negative_scope") or [])[:10],
        context_warnings=list(dict.fromkeys(warnings)),
        task_state=load_task_state(state, {item.id for item in products}),
    )


def _ordinal_index(query: str, order_size: int) -> int | None:
    match = _ORDINAL.search(query)
    if not match:
        return None
    if "前一个" in match.group(0):
        return max(0, order_size - 2) if order_size else None
    if "后一个" in match.group(0):
        return order_size - 1 if order_size else None
    raw = next((value for value in match.groups() if value), "")
    number = int(raw) if raw.isdigit() else _CN_NUMBERS.get(raw)
    return number - 1 if number and number > 0 else None


def _inherited_dimensions(query: str, understanding: QueryUnderstanding, snapshot: ContextSnapshot) -> list[str]:
    if understanding.requested_dimensions and not (
        set(understanding.requested_dimensions) <= {"detail"} and _ELLIPSIS.search(query)
    ):
        return []
    if not _ELLIPSIS.search(query):
        return []
    if snapshot.last_fact_dimension:
        mapping = {"current_price": "price", "net_margin": "profit", "sales_growth_rate": "sales_snapshot"}
        return [mapping.get(snapshot.last_fact_dimension, snapshot.last_fact_dimension)]
    return list(snapshot.last_requested_dimensions)


def resolve_context_reference(
    query: str,
    understanding: QueryUnderstanding,
    explicit_product_ids: list[str],
    snapshot: ContextSnapshot,
) -> ReferenceResolutionResult:
    """Resolve target precedence without reading or inventing product facts."""
    inherited = _inherited_dimensions(query, understanding, snapshot)
    if understanding.policy_requested:
        return ReferenceResolutionResult(source="none")
    if explicit_product_ids:
        return ReferenceResolutionResult(
            product_ids=list(dict.fromkeys(explicit_product_ids)), source="explicit_query",
            inherited_dimensions=inherited, confidence=1,
        )

    if (
        understanding.operation == "REFINE"
        and understanding.ordinal_reference is None
        and not _ORDINAL.search(query)
        and snapshot.task_state.active_comparison_set
    ):
        candidates = snapshot.task_state.active_comparison_set.product_ids
        if candidates:
            return ReferenceResolutionResult(
                product_ids=candidates, source="session_state", reference_expression="active_comparison_set",
                inherited_dimensions=inherited, confidence=0.98,
            )
    if understanding.operation == "RECOVER" and snapshot.task_state.active_entities:
        return ReferenceResolutionResult(
            product_ids=snapshot.task_state.active_entities, source="session_state",
            reference_expression="resolved_task_entities", inherited_dimensions=inherited, confidence=0.98,
        )

    if understanding.operation == "EXPLAIN_RANKING" and snapshot.task_state.active_result_set:
        ranked = snapshot.task_state.active_result_set.product_ids[:2]
        if len(ranked) == 2:
            return ReferenceResolutionResult(
                product_ids=ranked, source="session_state", reference_expression="active_ranking_top_two",
                inherited_dimensions=inherited, confidence=1,
            )

    recommendation = snapshot.task_state.active_recommendation
    if recommendation and understanding.requires_context and understanding.intent in {
        "decision_explanation", "provenance_fact", "data_quality_answer", "selection_recommendation",
    }:
        return ReferenceResolutionResult(
            product_ids=[recommendation.selected_product_id], source="session_state",
            reference_expression="active_recommendation", inherited_dimensions=inherited, confidence=1,
        )

    selection_reference = bool(_SELECTION_REFERENCE.search(query)) and not bool(_IGNORE_SELECTION.search(query))
    if selection_reference:
        if snapshot.current_selected_product_ids:
            return ReferenceResolutionResult(
                product_ids=snapshot.current_selected_product_ids, source="ui_selection",
                reference_expression=_SELECTION_REFERENCE.search(query).group(0),
                inherited_dimensions=inherited, confidence=0.98,
            )
        if snapshot.task_state.active_result_set and snapshot.task_state.active_result_set.product_ids:
            return ReferenceResolutionResult(
                product_ids=snapshot.task_state.active_result_set.product_ids,
                source="session_state", reference_expression=_SELECTION_REFERENCE.search(query).group(0),
                inherited_dimensions=inherited, confidence=0.96,
            )
        return ReferenceResolutionResult(
            source="ui_selection", reference_expression=_SELECTION_REFERENCE.search(query).group(0),
            inherited_dimensions=inherited, requires_clarification=True, failure_code="MISSING_UI_SELECTION",
        )

    if len(snapshot.current_selected_product_ids) == 1 and re.search(
        r"(?:售价|价格|定价).*(?:调整|改成|改为|设为|到)\s*\d", query, re.I
    ):
        return ReferenceResolutionResult(
            product_ids=snapshot.current_selected_product_ids, source="ui_selection",
            reference_expression="single_selection_price_scenario", inherited_dimensions=inherited, confidence=0.98,
        )

    task_order = (
        snapshot.task_state.active_comparison_set.product_ids
        if snapshot.task_state.active_comparison_set else
        snapshot.task_state.active_result_set.product_ids
        if snapshot.task_state.active_result_set else []
    )
    ordinal_order = snapshot.last_comparison_order or task_order
    ordinal = _ordinal_index(query, len(ordinal_order))
    if ordinal is not None:
        if 0 <= ordinal < len(ordinal_order):
            return ReferenceResolutionResult(
                product_ids=[ordinal_order[ordinal]], source="ordinal_reference",
                reference_expression=_ORDINAL.search(query).group(0), inherited_dimensions=inherited, confidence=1,
            )
        return ReferenceResolutionResult(
            source="ordinal_reference", reference_expression=_ORDINAL.search(query).group(0),
            inherited_dimensions=inherited, requires_clarification=True, failure_code="ORDINAL_OUT_OF_RANGE",
        )

    if _ONE_ONLY.search(query):
        candidates = snapshot.last_comparison_order or snapshot.last_comparison_product_ids or task_order
        if candidates:
            return ReferenceResolutionResult(
                product_ids=candidates, source="last_comparison", reference_expression=_ONE_ONLY.search(query).group(0),
                inherited_dimensions=inherited, confidence=0.98,
            )

    if _LAST_COMPARISON_REFERENCE.search(query):
        candidates = snapshot.last_comparison_order or snapshot.last_comparison_product_ids or task_order
        if candidates:
            return ReferenceResolutionResult(
                product_ids=candidates, source="last_comparison",
                reference_expression=_LAST_COMPARISON_REFERENCE.search(query).group(0),
                inherited_dimensions=inherited, confidence=0.98,
            )
        return ReferenceResolutionResult(
            source="last_comparison", reference_expression=_LAST_COMPARISON_REFERENCE.search(query).group(0),
            inherited_dimensions=inherited, requires_clarification=True, failure_code="MISSING_COMPARISON_CONTEXT",
        )

    singular = _SINGULAR_REFERENCE.search(query)
    if singular:
        # Compatibility for clients that predate selection_bound_session_id:
        # a one-item selection may bind after at least one successful session turn,
        # but never on the first turn of a newly created session.
        selection_is_bound = snapshot.selection_bound_session_id == snapshot.session_id or (
            snapshot.selection_bound_session_id is None and snapshot.turn_index > 1
        )
        if selection_is_bound and len(snapshot.current_selected_product_ids) == 1:
            return ReferenceResolutionResult(
                product_ids=snapshot.current_selected_product_ids, source="ui_selection",
                reference_expression=singular.group(0), inherited_dimensions=inherited, confidence=0.96,
            )
        for source, candidates in (
            ("last_explicit_entity", snapshot.last_explicit_product_ids),
            ("last_resolved_entity", snapshot.last_resolved_product_ids),
        ):
            if len(candidates) == 1:
                return ReferenceResolutionResult(
                    product_ids=candidates, source=source, reference_expression=singular.group(0),
                    inherited_dimensions=inherited, confidence=0.95,
                )
        return ReferenceResolutionResult(
            source="session_state", reference_expression=singular.group(0), inherited_dimensions=inherited,
            requires_clarification=True, failure_code="REFERENCE_UNRESOLVED",
        )

    if _ELLIPSIS.search(query):
        for source, candidates in (
            ("last_explicit_entity", snapshot.last_explicit_product_ids),
            ("last_comparison", snapshot.last_comparison_product_ids),
            ("last_resolved_entity", snapshot.last_resolved_product_ids),
        ):
            if len(candidates) == 1:
                return ReferenceResolutionResult(
                    product_ids=candidates, source=source, reference_expression=_ELLIPSIS.search(query).group(0),
                    inherited_dimensions=inherited, confidence=0.92,
                )

    if understanding.requires_context and understanding.intent in {
        "product_price", "product_detail", "provenance_fact", "calculation_explanation",
        "decision_explanation", "data_quality_answer",
    }:
        for source, candidates in (
            ("last_explicit_entity", snapshot.last_explicit_product_ids),
            ("last_resolved_entity", snapshot.last_resolved_product_ids),
        ):
            if len(candidates) == 1:
                return ReferenceResolutionResult(
                    product_ids=candidates, source=source, inherited_dimensions=inherited, confidence=0.9,
                )
        return ReferenceResolutionResult(
            source="session_state", inherited_dimensions=inherited, requires_clarification=True,
            failure_code="MISSING_CONTEXT",
        )

    if understanding.requires_context and understanding.intent == "selection_recommendation":
        candidates = snapshot.last_comparison_order or snapshot.last_comparison_product_ids or snapshot.last_resolved_product_ids
        if candidates:
            return ReferenceResolutionResult(
                product_ids=candidates, source="last_comparison" if snapshot.last_comparison_order or snapshot.last_comparison_product_ids else "last_resolved_entity",
                inherited_dimensions=inherited, confidence=0.9,
            )
        return ReferenceResolutionResult(
            source="session_state", inherited_dimensions=inherited, requires_clarification=True,
            failure_code="MISSING_CONTEXT",
        )

    return ReferenceResolutionResult(source="none", inherited_dimensions=inherited)
