"""Typed, session-scoped task continuity over ConversationSession.state_json."""
from __future__ import annotations

import re
from uuid import uuid4

from app.agent.intent_engine import ParsedIntent, _policy
from app.agent.tool_registry import FilterProductsInput
from app.schemas.agent import ComparisonState, RecommendationState, ResultSetState, TaskEntitySlot, TaskState


_TASK_NOUN = re.compile(r"筛选|条件|结果|排序|排名|比较|对比|任务")
_REMOVE = re.compile(r"取消|移除|删除|去掉|不再限制")
_RERUN = re.compile(r"重新|重跑|再次执行|再执行")
_REORDER = re.compile(r"排序|重排|优先|从高到低|从低到高")
_INSPECT = re.compile(r"(?:什么|哪些|查看|说下|告诉我).*(?:筛选|条件)|(?:筛选|条件).*(?:什么|哪些|查看)")
_RECOVER = re.compile(r"忽略|不管|去掉|移除|排除|只(?:看|分析|保留).*(?:剩下|已找到|找到)")
_REFINE_COMPARISON = re.compile(r"只(?:比较|看|保留)|仅(?:比较|看)|再加|加上|补充.*(?:维度|分析)")
_RESULT_REFERENCE = re.compile(r"这些|这批|这几个|当前结果|现有结果|刚才.*(?:筛|找|结果)")
_EXTREME = re.compile(r"最高|最大|最多|最赚钱|最低|最小|最少")
_RANKING_REASON = re.compile(r"(?=.*(?:第一|榜首|第1))(?=.*(?:第二|第2))(?=.*(?:为什么|为何|原因))")
_RECOMMENDATION_REASON = re.compile(r"为什么|为何|原因|依据")
_RECOMMENDATION_EVIDENCE = re.compile(r"来源|来自|出处|追溯")
_RECOMMENDATION_FRESHNESS = re.compile(r"过期|新鲜|时效|多久|旧了")
_ENTITY_STATUSES = {
    "EXACT_MATCH", "NORMALIZED_MATCH", "UNIQUE_ALIAS_MATCH", "FUZZY_UNIQUE_MATCH",
    "AMBIGUOUS", "NOT_FOUND", "LOW_CONFIDENCE",
}


def _comparison_dimensions(query: str, current: list[str]) -> list[str]:
    found = [
        dimension for dimension, pattern in (
            ("price", r"价格|售价"), ("profit", r"利润|净利率|赚钱"),
            ("roi", r"\broi\b"), ("risk", r"风险"), ("recommendation", r"推荐度|推荐"),
            ("demand", r"需求|销量|趋势"), ("competition", r"竞争"), ("compliance", r"合规"),
        ) if re.search(pattern, query, re.I)
    ]
    if re.search(r"再加|加上|补充", query):
        return list(dict.fromkeys([*current, *found]))
    return found


def load_task_state(raw_state: dict | None, valid_product_ids: set[str]) -> TaskState:
    raw = (raw_state or {}).get("task_state") or {}
    try:
        state = TaskState.model_validate(raw)
    except Exception:
        return TaskState()
    state.active_entities = [value for value in state.active_entities if value in valid_product_ids]
    state.last_execution_product_ids = [value for value in state.last_execution_product_ids if value in valid_product_ids]
    if state.active_result_set:
        state.active_result_set.product_ids = [value for value in state.active_result_set.product_ids if value in valid_product_ids]
    if state.active_comparison_set:
        state.active_comparison_set.product_ids = [value for value in state.active_comparison_set.product_ids if value in valid_product_ids]
    if state.active_recommendation:
        recommendation = state.active_recommendation
        recommendation.candidate_product_ids = [value for value in recommendation.candidate_product_ids if value in valid_product_ids]
        recommendation.ordered_candidates = [value for value in recommendation.ordered_candidates if value in valid_product_ids]
        if recommendation.selected_product_id not in valid_product_ids:
            state.active_recommendation = None
    for entity in state.pending_entities:
        if entity.product_id and entity.product_id not in valid_product_ids:
            entity.product_id = None
            entity.display_name = None
    return state


def _constraint_updates(query: str) -> dict:
    updates: dict = {}
    margin = re.search(r"(?:净利率|净利润率|利润率|毛利率).*?(?:至少|不低于|大于|超过|改成|调整为|门槛)?\s*(\d+(?:\.\d+)?)\s*[%％]", query, re.I)
    competition = re.search(r"竞争(?:度|评分)?.*?(?:低于|小于|不超过|少于)\s*(\d+(?:\.\d+)?)", query, re.I)
    limit = re.search(r"(?:找|给|保留|返回|前|top)\s*(\d+)\s*(?:个|款|件)", query, re.I)
    if margin:
        updates["min_margin_rate"] = float(margin.group(1)) / 100
    if competition:
        updates["max_competition_score"] = float(competition.group(1))
    if re.search(r"风险.*(?:不能|不是|排除|不得).*high|(?:不能|排除).*高风险", query, re.I):
        updates["excluded_risk_levels"] = ["high"]
    if limit:
        updates["limit"] = max(1, min(100, int(limit.group(1))))
    return updates


def _remove_fields(query: str) -> list[str]:
    fields = []
    if re.search(r"竞争", query):
        fields += ["max_competition_score", "max_market_saturation"]
    if re.search(r"风险", query):
        fields += ["risk_level", "excluded_risk_levels"]
    if re.search(r"利润|毛利", query):
        fields += ["min_margin_rate", "min_roi"]
    if re.search(r"评分|分数", query):
        fields += ["min_score", "max_score"]
    if re.search(r"合规", query):
        fields += ["compliance_status"]
    return fields


def _sort_update(query: str) -> tuple[str, str] | None:
    direction = "asc" if re.search(r"从低到高|升序", query) else "desc"
    if re.search(r"风险.*优先", query):
        return "risk", "asc"
    if re.search(r"利润|赚钱|赚头", query):
        return "net_profit", direction
    if re.search(r"推荐|评分|排名", query):
        return "recommendation_score", direction
    return None


def _ranking_update(query: str) -> list[str]:
    """Return ordered backend ranking keys, never product facts."""
    dimensions: list[str] = []
    for field, pattern, direction in (
        ("risk", r"风险", "asc"),
        ("net_profit", r"利润(?!率)|赚钱|赚头", "desc"),
        ("margin_rate", r"净利率|利润率", "desc"),
        ("recommendation_score", r"推荐|评分", "desc"),
    ):
        match = re.search(pattern, query, re.I)
        if match:
            dimensions.append(f"{field}:{direction}")
    return dimensions[:3]


def _extreme_metric(query: str) -> tuple[str, str, str]:
    direction = "ARGMIN" if re.search(r"最低|最小|最少", query) else "ARGMAX"
    if re.search(r"净利率|利润率", query, re.I):
        return "net_margin", "profit", direction
    if re.search(r"利润|赚钱|赚头", query, re.I):
        return "net_profit", "profit", direction
    if re.search(r"风险", query, re.I):
        return "risk_level", "risk", direction
    return "recommendation_score", "recommendation", direction


def task_mutation_intent(query: str, state: TaskState, selected=(), explicit_entity_mentions=()) -> tuple[ParsedIntent, dict] | None:
    """Recognize only structural task operations; never infer product identity."""
    if state.status == "EMPTY":
        return None
    operation = None
    filters = dict(state.filter_spec)
    update_fields: dict = {}
    recommendation_reference = re.search(r"它|这个|这些|该(?:推荐|商品|候选)|刚才", query) or (
        not explicit_entity_mentions and re.search(r"推荐|维持", query)
    )
    recommendation = state.active_recommendation if recommendation_reference else None
    if recommendation and _RECOMMENDATION_FRESHNESS.search(query) and re.search(r"推荐|维持|继续", query):
        parsed = ParsedIntent(
            "decision_explanation", selected_product_ids=(recommendation.selected_product_id,),
            plan=({"tool": "get_product", "purpose": "读取当前推荐商品的后端门禁与字段时效"},),
            policy=_policy(("get_product",), 1, ("decision_reason", "evidence", "freshness"), selection="ignore", fast=True),
        )
        return parsed, {"operation": "EXPLAIN", "filter_updates": {}, "requires_task_state": True, "requires_context": True, "deterministic": True}
    if recommendation and _RECOMMENDATION_EVIDENCE.search(query) and re.search(r"推荐|这些|它", query):
        parsed = ParsedIntent(
            "provenance_fact", selected_product_ids=(recommendation.selected_product_id,),
            plan=({"tool": "get_product", "purpose": "读取当前推荐商品的后端字段来源"},),
            policy=_policy(("get_product",), 1, ("provenance",), selection="ignore", fast=True),
        )
        return parsed, {"operation": "EXPLAIN", "filter_updates": {}, "requires_task_state": True, "requires_context": True, "deterministic": True}
    if recommendation and _RECOMMENDATION_REASON.search(query) and re.search(r"推荐|它|这个", query):
        parsed = ParsedIntent(
            "decision_explanation", selected_product_ids=(recommendation.selected_product_id,),
            plan=({"tool": "get_product", "purpose": "读取当前推荐商品的后端评分与门禁依据"},),
            policy=_policy(("get_product",), 1, ("decision_reason", "evidence"), selection="ignore", fast=True),
        )
        return parsed, {"operation": "EXPLAIN", "filter_updates": {}, "requires_task_state": True, "requires_context": True, "deterministic": True}
    if state.active_result_set and _RANKING_REASON.search(query):
        ranking = state.ranking_spec or [f"{state.active_result_set.sort_spec[0]}:{state.active_result_set.sort_spec[1]}" if len(state.active_result_set.sort_spec) >= 2 else "recommendation_score:desc"]
        dimensions = tuple(dict.fromkeys(
            "risk" if item.startswith("risk:") else "profit" if item.startswith(("net_profit:", "margin_rate:")) else "recommendation"
            for item in ranking
        ))
        parsed = ParsedIntent(
            "product_comparison", selected_product_ids=tuple(state.active_result_set.product_ids[:2]),
            plan=({"tool": "compare_products", "purpose": "读取当前排名前两项的后端指标"},),
            policy=_policy(("compare_products",), 1, dimensions or ("recommendation",), selection="ignore", fast=True),
        )
        return parsed, {"operation": "EXPLAIN_RANKING", "filter_updates": {}, "requires_task_state": True, "requires_context": True, "deterministic": True, "sort_updates": ranking}
    if state.active_result_set and _RESULT_REFERENCE.search(query) and _EXTREME.search(query):
        metric, dimension, extreme_operation = _extreme_metric(query)
        parsed = ParsedIntent(
            "product_detail", selected_product_ids=tuple(state.active_result_set.product_ids),
            plan=({"tool": "compare_products", "purpose": "在当前结果集中确定性查找指标极值"},),
            policy=_policy(("compare_products",), 1, (dimension,), selection="ignore", fast=True),
        )
        return parsed, {"operation": extreme_operation, "filter_updates": {}, "requires_task_state": True, "requires_context": True, "deterministic": True, "metric": metric}
    if state.pending_entities and _RECOVER.search(query):
        operation = "RECOVER"
        current_dimensions = tuple(state.requested_dimensions or ("detail",))
        if "recommendation" in current_dimensions or state.task_type == "selection_recommendation":
            dimensions = tuple(item for item in current_dimensions if item in {"profit", "risk", "compliance", "recommendation", "decision"}) or ("recommendation", "decision")
            parsed = ParsedIntent(
                "selection_recommendation", selected_product_ids=tuple(state.active_entities),
                plan=({"tool": "compare_products", "purpose": "读取恢复后的已解析商品并执行后端推荐门禁"},),
                policy=_policy(("compare_products",), 1, dimensions, selection="query_first"),
            )
        else:
            dimensions = tuple(item for item in current_dimensions if item in {"price", "profit", "roi", "risk", "detail"}) or ("detail",)
            tools = ("get_product", "calculate_profit") if {"profit", "roi"} & set(dimensions) else ("get_product",)
            parsed = ParsedIntent(
                "product_detail", selected_product_ids=tuple(state.active_entities),
                plan=tuple({"tool": tool, "purpose": "读取恢复后的已解析商品"} for tool in tools),
                policy=_policy(tools, len(tools), dimensions, selection="query_first"),
            )
        return parsed, {"operation": operation, "filter_updates": {}, "requires_task_state": True, "requires_context": True}
    if state.active_comparison_set and _REFINE_COMPARISON.search(query):
        dimensions = _comparison_dimensions(query, state.active_comparison_set.dimensions)
        if dimensions:
            operation = "REFINE"
            profit_only = set(dimensions) <= {"profit", "roi"}
            intent = "profit_comparison" if profit_only else "product_comparison"
            tools = ("compare_products", "calculate_profit") if profit_only else ("compare_products",)
            parsed = ParsedIntent(
                intent, selected_product_ids=tuple(state.active_comparison_set.product_ids), limit=10,
                plan=tuple({"tool": tool, "purpose": "按更新后的比较维度重新读取后端事实"} for tool in tools),
                policy=_policy(tools, 11 if profit_only else 1, tuple(dimensions), selection="query_first"),
            )
            return parsed, {"operation": operation, "filter_updates": {}, "requires_task_state": True, "requires_context": True}
    if _INSPECT.search(query) and _TASK_NOUN.search(query):
        operation = "INSPECT"
    elif _RERUN.search(query) and (re.search(r"执行|运行|筛选|任务", query) or len(query) <= 12):
        operation = "RERUN"
    elif _REMOVE.search(query) and (_TASK_NOUN.search(query) or _remove_fields(query)):
        operation = "REMOVE_CONSTRAINT"
        for field in _remove_fields(query):
            filters.pop(field, None)
    else:
        update_fields = _constraint_updates(query)
        if update_fields and re.search(r"再|增加|加上|同时|还要|要求", query):
            operation = "ADD_CONSTRAINT"
        elif update_fields and re.search(r"改|调整|变成|其他.*不变", query):
            operation = "UPDATE_CONSTRAINT"
        elif _REORDER.search(query) and state.active_result_set:
            operation = "REORDER"
            sort = _sort_update(query)
            if sort:
                update_fields.update(sort_by=sort[0], sort_direction=sort[1])
    if not operation:
        return None
    filters.update(update_fields)
    filters = FilterProductsInput.model_validate(filters).model_dump(exclude_defaults=True)
    limit = int(filters.pop("limit", 20))
    if operation == "INSPECT":
        parsed = ParsedIntent("product_filter", filters=filters, limit=limit, policy=_policy((), 0, ("filters",), fast=True))
    else:
        parsed = ParsedIntent("product_filter", filters=filters, limit=limit, plan=({"tool": "filter_products", "purpose": "按当前任务状态执行筛选"},), policy=_policy(("filter_products",), 1, ("filters",), fast=True))
    return parsed, {"operation": operation, "filter_updates": update_fields, "requires_task_state": True, "requires_context": False, "sort_updates": _ranking_update(query) if operation == "REORDER" else []}


def evolve_task_state(
    previous: TaskState, *, intent: str, operation: str, turn_index: int,
    product_ids: list[str], filters: dict, dimensions: list[str], task_completed: bool,
    resolved_product_ids: list[str] | None = None, requested_entities=(),
    ranking_spec: list[str] | None = None, recommendation: dict | None = None,
) -> TaskState:
    if not task_completed:
        slots = []
        for item in requested_entities:
            getter = item.get if isinstance(item, dict) else lambda key, default=None: getattr(item, key, default)
            mention = getter("mention", "")
            status = getter("status", "NOT_FOUND")
            if mention and status in _ENTITY_STATUSES:
                slots.append(TaskEntitySlot(
                    mention=mention, status=status, product_id=getter("product_id"), display_name=getter("name"),
                ))
        known = list(dict.fromkeys(resolved_product_ids or product_ids or previous.active_entities))[:10]
        return previous.model_copy(deep=True, update={
            "task_id": previous.task_id or uuid4().hex, "revision": previous.revision + 1,
            "task_type": intent, "operation": operation, "status": "NEEDS_CLARIFICATION",
            "active_entities": known, "pending_entities": slots,
            "requested_dimensions": dimensions[:10], "pending_clarification": "entity_resolution",
        })
    task_id = previous.task_id or uuid4().hex
    revision = previous.revision + 1
    if operation in {"INSPECT", "ARGMAX", "ARGMIN", "EXPLAIN_RANKING"}:
        return previous.model_copy(deep=True, update={
            "task_id": task_id, "revision": revision, "operation": operation,
            "active_entities": product_ids[:10] or previous.active_entities,
            "last_successful_action": operation, "last_execution_product_ids": product_ids[:100] or previous.last_execution_product_ids,
            "ranking_spec": (ranking_spec or previous.ranking_spec)[:10], "pending_clarification": None,
        })
    state = previous.model_copy(deep=True, update={
        "task_id": task_id, "revision": revision, "task_type": intent,
        "operation": operation, "status": "COMPLETED", "active_entities": product_ids[:10],
        "requested_dimensions": dimensions[:10], "last_successful_action": operation,
        "last_execution_product_ids": product_ids[:100], "pending_entities": [], "pending_clarification": None,
    })
    if intent == "product_filter":
        normalized = FilterProductsInput.model_validate(filters).model_dump(exclude_defaults=True)
        state.filter_spec = normalized
        state.sort_spec = [str(normalized.get("sort_by", "recommendation_score")), str(normalized.get("sort_direction", "desc"))]
        state.ranking_spec = (ranking_spec or [f"{state.sort_spec[0]}:{state.sort_spec[1]}"])[:10]
        state.active_result_set = ResultSetState(
            product_ids=product_ids[:100], source_task_type=intent, filter_spec=normalized,
            sort_spec=state.sort_spec, ranking_dimensions=state.ranking_spec,
            source_task_revision=revision, created_turn=turn_index,
        )
    if intent in {"profit_comparison", "product_comparison"} and len(product_ids) >= 2:
        previous_revision = previous.active_comparison_set.revision if previous.active_comparison_set else 0
        state.active_comparison_set = ComparisonState(
            product_ids=product_ids[:10], dimensions=dimensions[:10], revision=previous_revision + 1,
            created_turn=turn_index, source_task=intent,
        )
    if intent == "selection_recommendation" and recommendation:
        state.active_recommendation = RecommendationState.model_validate(recommendation)
    return state
