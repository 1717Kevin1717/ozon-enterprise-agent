import re
import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.context import build_context_snapshot
from app.agent.model_router import ProviderExecutionPolicy, build_model_router, redact_external_query, semantic_context_slots
from app.agent.providers import ProviderFailure
from app.agent.query_understanding import (
    SemanticPlanValidationFailure, build_semantic_context_packet, semantic_policy,
    understand_query, validate_semantic_plan,
)
from app.agent.scenario_state import ScenarioStateError, load_scenario_state
from app.agent.tools import rule_agent_ask
from app.agent.task_state import task_mutation_intent
from app.db.models import ConversationSession
from app.repositories.products import ProductRepository
from app.schemas.agent import GroundedReasoningOutput


SAFE_PROVIDER_MESSAGES = {
    "KEY_MISSING": "主语义模型尚未配置，当前需要更明确的问题描述。",
    "PROVIDER_NOT_CONFIGURED": "主语义模型尚未配置；确定性查询仍可使用，语义问题暂不能处理。",
    "RATE_LIMITED": "语义模型额度或频率受限，本次未生成业务事实。",
    "NETWORK_TIMEOUT": "语义模型网络请求超时，本次未生成业务事实。",
    "CONNECT_ERROR": "语义模型连接失败，本次未生成业务事实。",
    "TLS_ERROR": "语义模型安全连接失败，本次未生成业务事实。",
    "PROXY_ERROR": "语义模型代理连接失败，本次未生成业务事实。",
    "REMOTE_PROTOCOL_ERROR": "语义模型连接协议异常，本次未生成业务事实。",
    "HTTP_ERROR": "语义模型 HTTP 请求失败，本次未生成业务事实。",
    "RESPONSE_READ_ERROR": "语义模型响应读取失败，本次未生成业务事实。",
    "REQUEST_SERIALIZATION_ERROR": "语义模型请求构造失败，本次未发送业务事实。",
    "AUTH_FAILED": "语义模型认证失败，请检查服务端配置。",
    "MODEL_UNAVAILABLE": "配置的语义模型当前不可用。",
    "MODEL_NOT_FOUND": "配置的模型名称不存在或当前账号不可用。",
    "INVALID_RESPONSE": "语义模型响应结构不完整，后端已拒绝使用。",
    "SCHEMA_VALIDATION_FAILED": "语义模型输出未通过后端 Schema 校验。",
    "SEMANTIC_PLAN_VALIDATION_FAILED": "语义模型计划与后端约束不一致，本次未生成业务事实。",
    "PROVIDER_UNAVAILABLE": "语义模型服务暂时不可用。",
    "PROVIDER_ERROR": "语义模型调用失败。",
    "PROVIDER_INTERNAL_ERROR": "语义模型服务内部异常。",
}

# Only failures that leave the locally produced, backend-verifiable semantic
# frame trustworthy may use the deterministic Scenario recovery path. Provider
# authorization, model availability, HTTP policy, and SEM guard failures remain
# outside this set.
_RECOVERABLE_SCENARIO_PROVIDER_FAILURES = frozenset({
    "CONNECT_ERROR",
    "NETWORK_TIMEOUT",
    "INVALID_RESPONSE",
    "SCHEMA_VALIDATION_FAILED",
    "SEMANTIC_PLAN_VALIDATION_FAILED",
    "RESPONSE_READ_ERROR",
})
_INCOMPLETE_SCENARIO_SIGNAL = re.compile(
    r"假设|假如|如果|情景|模拟|会怎样|会怎么样|如何变化|有什么影响"
)
_ORDINAL_SIGNAL = re.compile(
    r"第[一二两三四五六七八九十\d]+(?:名|个|位)|"
    r"前[一二两三四五六七八九十\d]+名|榜首|排名"
)


def _validated_scenario_transport_fallback(
    query: str,
    understanding,
    packet,
    selected_product_ids,
    snapshot,
):
    """Reuse only a complete backend-verifiable Scenario frame after transport failure."""
    try:
        fallback_parsed, fallback_understanding = validate_semantic_plan(
            understanding.model_dump(mode="json"), query,
            selected_product_ids or (), packet,
        )
    except (SemanticPlanValidationFailure, ValueError, TypeError):
        return None
    if fallback_understanding.intent != "scenario_analysis":
        return None

    operation = fallback_understanding.operation
    if operation in {"EXPLAIN_SCENARIO", "COMPARE_SCENARIO", "RESET_SCENARIO"}:
        return (fallback_parsed, fallback_understanding) if snapshot.has_active_scenario else None
    if operation == "REMOVE_OVERRIDE":
        return (
            (fallback_parsed, fallback_understanding)
            if snapshot.has_active_scenario and fallback_understanding.scenario_field
            else None
        )
    if operation != "CREATE_SCENARIO":
        return None

    complete_mutation = all((
        fallback_understanding.scenario_field,
        fallback_understanding.mutation_type,
        fallback_understanding.hypothetical_unit,
    )) and fallback_understanding.hypothetical_value is not None
    try:
        from app.agent.scenario_state import normalize_override
        normalize_override(
            field=fallback_understanding.scenario_field,
            operation=fallback_understanding.mutation_type,
            value=fallback_understanding.hypothetical_value,
            unit=fallback_understanding.hypothetical_unit,
            created_revision=1,
        )
    except (ScenarioStateError, TypeError, ValueError):
        return None

    active_collection = snapshot.task_state.active_collection
    if fallback_understanding.scenario_reference_role == "ACTIVE_COLLECTION":
        complete_scope = bool(
            active_collection
            and active_collection.product_ids
            and fallback_understanding.scenario_collection_scope
            and (
                fallback_understanding.scenario_collection_scope != "TOP_K"
                or fallback_understanding.scenario_top_k is not None
            )
        )
    else:
        complete_scope = bool(
            fallback_understanding.scenario_reference_role == "EXPLICIT_ENTITY"
            and fallback_understanding.entity_mentions
        )
    return (
        (fallback_parsed, fallback_understanding)
        if complete_mutation and complete_scope
        else None
    )


def _validated_collection_transport_fallback(
    query: str,
    understanding,
    packet,
    selected_product_ids,
    snapshot,
):
    """Reuse only a complete, session-scoped collection frame after provider failure."""
    try:
        fallback_parsed, fallback_understanding = validate_semantic_plan(
            understanding.model_dump(mode="json"), query,
            selected_product_ids or (), packet,
        )
    except (SemanticPlanValidationFailure, ValueError, TypeError):
        return None
    if fallback_understanding.intent != "collection_analysis":
        return None
    if not (
        fallback_understanding.collection_selector
        or fallback_understanding.collection_filters
        or fallback_understanding.analysis_requests
        or fallback_understanding.operation in {
            "ANALYZE_COLLECTION", "COMPARE_COLLECTION_MEMBERS", "RECOMMEND_TOP_K",
            "IDENTIFY_EVIDENCE_GAPS", "EXPLAIN_RANKING", "LOOKUP_ORDINAL_MEMBER",
        }
    ):
        return None

    reference = fallback_understanding.collection_reference
    if reference == "current_collection":
        collection = snapshot.task_state.active_collection
        if not collection or not collection.product_ids:
            return None
    elif reference == "active_result_set":
        result_set = snapshot.task_state.active_result_set
        if not result_set or not result_set.product_ids:
            return None
    elif reference not in {"candidate_pool", "company_catalog"}:
        return None
    return fallback_parsed, fallback_understanding


def _validated_fact_transport_fallback(query, understanding, packet, selected_product_ids, snapshot):
    """Reuse a complete fact frame; identity and values remain backend-owned."""
    if understanding.intent not in {"product_price", "product_detail"}:
        return None
    if not understanding.entity_mentions and not (
        understanding.requires_context
        and (snapshot.last_explicit_product_ids or snapshot.last_resolved_product_ids)
    ):
        return None
    try:
        parsed, validated = validate_semantic_plan(
            understanding.model_dump(mode="json"), query,
            selected_product_ids or (), packet,
        )
    except (SemanticPlanValidationFailure, ValueError, TypeError):
        return None
    if validated.intent not in {"product_price", "product_detail"}:
        return None
    return parsed, validated


def _controlled_transport_clarification(query: str, understanding, snapshot):
    """Return a business clarification when structural slots remain incomplete."""
    complete_collection_request = bool(
        understanding.intent == "scenario_analysis"
        and understanding.operation == "CREATE_SCENARIO"
        and understanding.scenario_reference_role == "ACTIVE_COLLECTION"
        and understanding.scenario_collection_scope
        and all((
            understanding.scenario_field,
            understanding.mutation_type,
            understanding.hypothetical_unit,
        ))
        and understanding.hypothetical_value is not None
    )
    if complete_collection_request:
        # Preserve the typed request so the backend Scenario resolver can emit
        # MISSING_CONTEXT. The execution boundary still rejects an absent
        # session-local ActiveCollection before any calculation or mutation.
        return semantic_policy(
            "scenario_analysis", understanding.requested_dimensions,
        ), understanding.model_copy(update={
            "route": "SEMANTIC_PLANNER",
            "failure_code": None,
            "clarification_required": False,
            "clarification_reason": None,
        })
    if _INCOMPLETE_SCENARIO_SIGNAL.search(query):
        reason = "假设条件不完整：请明确调整的价格或成本字段、调整方式、数值，以及目标商品或候选集合。"
    elif _ORDINAL_SIGNAL.search(query):
        reason = "当前会话没有可验证的排名上下文；请先建立候选集合或假设情景。"
    else:
        has_product = bool(
            understanding.entity_mentions
            or snapshot.current_selected_product_ids
            or snapshot.last_explicit_product_ids
            or snapshot.last_resolved_product_ids
        )
        has_metric = bool(understanding.metric or understanding.metrics or understanding.requested_dimensions)
        if not has_product and not has_metric:
            category = "MISSING_PRODUCT"
            missing_slots = ["product", "metric"]
            reason = "请明确要查询的商品，以及要查看的指标（例如价格、净利润、净利率或 ROI）。"
        elif not has_product:
            category = "MISSING_PRODUCT"
            missing_slots = ["product"]
            reason = "请告诉我你想查询哪个商品。"
        elif not has_metric:
            category = "MISSING_METRIC"
            missing_slots = ["metric"]
            reason = "你希望查看价格、净利润、净利率还是 ROI？"
        else:
            return None
        return semantic_policy("unknown", ()), understanding.model_copy(update={
            "intent": "unknown", "question_type": "business_clarification",
            "route": "CLARIFICATION", "clarification_required": True,
            "clarification_reason": reason, "clarification_category": category,
            "missing_slots": missing_slots,
        })
    clarification = understanding.model_copy(update={
        "intent": "unknown", "task_type": "unknown", "question_type": "unknown",
        "route": "CLARIFICATION", "failure_code": None,
        "clarification_required": True, "clarification_reason": reason,
        "planned_tools": [], "requires_tools": False,
    })
    return semantic_policy("unknown", ()), clarification


async def _conversation(session, company_id, user_id, query, session_id):
    conversation = await session.get(ConversationSession, session_id) if session_id else None
    if not conversation or conversation.company_id != company_id or conversation.user_id != user_id:
        conversation = ConversationSession(company_id=company_id, user_id=user_id, title=query[:80], goal_summary=query[:300])
        session.add(conversation)
        await session.flush()
    return conversation


def _reasoning_packet(result: dict[str, Any]) -> tuple[dict[str, Any], dict[str, dict]]:
    labels: dict[str, dict] = {}
    products = []
    for index, item in enumerate(result.get("products") or []):
        label = f"P{index + 1}"
        labels[label] = item
        products.append({
            "label": label,
            "net_profit": item.get("net_profit"),
            "margin_rate": item.get("current_margin_rate"),
            "roi": item.get("roi"),
            "risk_level": item.get("risk_level"),
            "compliance_status": item.get("compliance_status"),
            "decision_status": item.get("decision_status"),
            "data_completeness": item.get("completeness"),
        })
    return {
        "requested_dimensions": result.get("requested_dimensions") or [],
        "decision_status": result.get("decision_status"),
        "human_review_required": result.get("human_review_required", False),
        "data_sufficiency": result.get("data_sufficiency"),
        "products": products,
    }, labels


def _reasoning_conflicts(frame: GroundedReasoningOutput, packet: dict[str, Any], labels: dict[str, dict]) -> bool:
    if set(frame.dimensions_used) - set(packet.get("requested_dimensions") or []):
        return True
    if frame.formal_recommendation_label:
        target = labels.get(frame.formal_recommendation_label)
        if not target or target.get("decision_status") != "RECOMMENDED":
            return True
    text = frame.summary.casefold()
    if any(item.get("decision_status") == "BLOCKED" for item in labels.values()) and re.search(r"建议.*上架|立即上架|可以上架", text):
        return True
    if any(item.get("risk_level") == "high" for item in labels.values()) and re.search(r"低风险|风险(?:很)?低", text):
        return True
    sufficiency = packet.get("data_sufficiency") or {}
    if sufficiency.get("status") == "INSUFFICIENT_DATA" and re.search(r"趋势.*(?:确认|明显向好)|销量.*(?:确认)?上涨", text):
        return True
    return False


async def dual_model_agent_ask(
    session: AsyncSession, company_id: str, user_id: str, role: str, query: str, session_id: str | None,
    selected_product_ids: list[str] | None = None, selection_revision: int = 0,
    selection_bound_session_id: str | None = None,
    provider_policy: ProviderExecutionPolicy | None = None,
) -> dict:
    execution_policy = provider_policy or ProviderExecutionPolicy()
    repo = ProductRepository(session, company_id)
    products = await repo.list(limit=500)
    conversation = await _conversation(session, company_id, user_id, query, session_id)
    has_active_scenario = False
    try:
        has_active_scenario = load_scenario_state(
            conversation.state_json or {},
            session_id=conversation.id,
            company_id=company_id,
            valid_product_ids={item.id for item in products},
        ) is not None
    except ScenarioStateError:
        # An invalid, stale, or foreign Scenario is never eligible context.
        has_active_scenario = False
    snapshot = build_context_snapshot(
        conversation, company_id=company_id, user_id=user_id, query=query, products=products,
        selected_product_ids=selected_product_ids, selection_revision=selection_revision,
        selection_bound_session_id=selection_bound_session_id,
        has_active_scenario=has_active_scenario,
    )
    parsed, understanding = understand_query(query, products, selected_product_ids, snapshot)
    mutation_result = task_mutation_intent(
        query, snapshot.task_state, selected_product_ids or (), understanding.entity_mentions,
    )
    if mutation_result:
        parsed, mutation = mutation_result
        understanding = understanding.model_copy(update={
            "intent": parsed.name,
            "task_type": snapshot.task_state.task_type or parsed.name,
            "operation": mutation["operation"],
            "filter_updates": mutation.get("filter_updates", {}),
            "sort_updates": mutation.get("sort_updates", []),
            "metric": mutation.get("metric", understanding.metric),
            "metrics": [mutation["metric"]] if mutation.get("metric") else understanding.metrics,
            "requires_task_state": True,
            "requires_context": mutation.get("requires_context", False),
            "requested_dimensions": list(parsed.policy.requested_dimensions),
            "planned_tools": list(parsed.policy.allowed_tools),
            "route": "DETERMINISTIC_FAST_PATH" if mutation.get("deterministic") else "SEMANTIC_PLANNER",
            "failure_code": None,
        })
    router = build_model_router()
    decision = router.semantic_route(understanding, policy=execution_policy)
    if decision.route == "DETERMINISTIC_FAST_PATH":
        return await rule_agent_ask(
            session, company_id, user_id, role, query, conversation.id,
            selected_product_ids, selection_revision, selection_bound_session_id,
            provider_notice="该问题由确定性快速路径完成，无需调用外部模型。",
        )
    if decision.route == "CLARIFICATION" and understanding.route != "SEMANTIC_PLANNER":
        return await rule_agent_ask(
            session, company_id, user_id, role, query, conversation.id,
            selected_product_ids, selection_revision, selection_bound_session_id,
            understanding_override=understanding, parsed_override=parsed,
        )

    packet = build_semantic_context_packet(query, understanding, snapshot, products)
    pre_provider_understanding = understanding
    packet_dict = packet.model_dump(mode="json")
    slots = semantic_context_slots(packet_dict)
    external_query, redacted_identifiers = redact_external_query(query, products)
    provider_result = None
    requested_provider = router.primary_name
    failure_code = "PROVIDER_NOT_CONFIGURED"
    primary_failure_code: str | None = None
    provider_failures: list[str] = []
    provider_calls: list[dict[str, Any]] = []
    primary = router.provider(requested_provider)
    if not primary or not primary.configured:
        primary_failure_code = failure_code
        provider_failures.append(failure_code)
        provider_calls.append({
            "requested_provider": requested_provider, "actual_provider": requested_provider,
            "model_name": primary.model_name if primary else "", "route": "QWEN_SEMANTIC",
            "status": "skipped", "failure_code": failure_code, "latency_ms": None, "token_usage": {},
        })
    candidates = router.semantic_candidates(policy=execution_policy)
    for provider in candidates:
        call_started = time.perf_counter()
        candidate = None
        try:
            candidate = await provider.semantic_interpret(external_query, slots)
            parsed, understanding = validate_semantic_plan(candidate.content, external_query, selected_product_ids or (), packet)
            if redacted_identifiers:
                understanding = understanding.model_copy(update={
                    "entity_mentions": [redacted_identifiers.get(item, item) for item in understanding.entity_mentions]
                })
            understanding = understanding.model_copy(update={"semantic_provider": candidate.provider_name})
            provider_calls.append({
                "requested_provider": requested_provider, "actual_provider": candidate.provider_name,
                "model_name": candidate.model_name, "route": "QWEN_SEMANTIC", "status": "success",
                "failure_code": None, "latency_ms": candidate.latency_ms if candidate.latency_ms is not None else round((time.perf_counter() - call_started) * 1000),
                "token_usage": candidate.usage, "exception_class": None,
                "failure_stage": None, "upstream_status": candidate.upstream_status,
            })
            provider_result = candidate
            failure_code = ""
            break
        except ProviderFailure as exc:
            failure_code = exc.code
            primary_failure_code = primary_failure_code or failure_code
            provider_failures.append(exc.code)
            provider_calls.append({
                "requested_provider": requested_provider, "actual_provider": provider.provider_name,
                "model_name": provider.model_name, "route": "QWEN_SEMANTIC", "status": "failed",
                "failure_code": exc.code, "latency_ms": exc.elapsed_ms if exc.elapsed_ms is not None else round((time.perf_counter() - call_started) * 1000), "token_usage": {},
                "exception_class": exc.exception_class, "failure_stage": exc.failure_stage,
                "upstream_status": exc.upstream_status,
            })
        except SemanticPlanValidationFailure as exc:
            failure_code = exc.failure_code
            primary_failure_code = primary_failure_code or failure_code
            provider_failures.append(failure_code)
            provider_calls.append({
                "requested_provider": requested_provider, "actual_provider": provider.provider_name,
                "model_name": provider.model_name, "route": "QWEN_SEMANTIC", "status": "failed",
                "failure_code": failure_code, "latency_ms": round((time.perf_counter() - call_started) * 1000), "token_usage": {},
                "exception_class": type(exc).__name__, "failure_stage": exc.stage,
                "upstream_status": candidate.upstream_status if candidate else None,
                "validation_rule_id": exc.rule_id, "validation_reason_code": exc.reason_code,
                "validation_field": exc.field,
                "semantic_operation": exc.diagnostics.get("semantic_operation"),
                "reference_slots": exc.diagnostics.get("reference_slots", []),
                "raw_requested_dimensions": exc.diagnostics.get("raw_requested_dimensions", []),
                "canonical_requested_dimensions": exc.diagnostics.get("canonical_requested_dimensions", []),
                "normalization_failures": exc.diagnostics.get("normalization_failures", []),
            })
        except (ValueError, TypeError) as exc:
            failure_code = "SEMANTIC_PLAN_VALIDATION_FAILED"
            primary_failure_code = primary_failure_code or failure_code
            provider_failures.append(failure_code)
            provider_calls.append({
                "requested_provider": requested_provider, "actual_provider": provider.provider_name,
                "model_name": provider.model_name, "route": "QWEN_SEMANTIC", "status": "failed",
                "failure_code": failure_code, "latency_ms": round((time.perf_counter() - call_started) * 1000), "token_usage": {},
                "exception_class": type(exc).__name__, "failure_stage": "SEMANTIC_VALIDATE",
                "upstream_status": candidate.upstream_status if candidate else None,
                "validation_rule_id": None, "validation_reason_code": "UNKNOWN_SEMANTIC_PLAN_ERROR",
                "validation_field": None,
            })
    provider_trace = [{
        "event": "provider_call", "tool": "model_router", "business_label": "模型路由调用",
        "summary": (
            f"{item['actual_provider']} 语义调用成功。"
            if item.get("status") == "success"
            else f"{item['actual_provider']} 语义调用失败：{item.get('failure_code') or 'PROVIDER_ERROR'}。"
        ),
        **item,
    } for item in provider_calls]
    if provider_result is None:
        failure_code = primary_failure_code or failure_code
        deterministic_plan = None
        if failure_code in _RECOVERABLE_SCENARIO_PROVIDER_FAILURES:
            deterministic_plan = _validated_scenario_transport_fallback(
                query, pre_provider_understanding, packet,
                selected_product_ids, snapshot,
            )
            if deterministic_plan is None:
                deterministic_plan = _validated_collection_transport_fallback(
                    query, pre_provider_understanding, packet,
                    selected_product_ids, snapshot,
                )
            if deterministic_plan is None:
                deterministic_plan = _validated_fact_transport_fallback(
                    query, pre_provider_understanding, packet,
                    selected_product_ids, snapshot,
                )
        if deterministic_plan is not None:
            parsed, understanding = deterministic_plan
            return await rule_agent_ask(
                session, company_id, user_id, role, query, conversation.id,
                selected_product_ids, selection_revision, selection_bound_session_id,
                understanding_override=understanding, parsed_override=parsed,
                response_mode="deterministic_fallback", fallback_reason=failure_code,
                provider_notice="语义模型本次未产生可用结构化结果；已复用调用前的安全语义，并继续执行后端校验。",
                fallback_used_override=True, requested_provider_override=requested_provider,
                model_route_override="QWEN_SEMANTIC", provider_calls_override=provider_calls,
                trace_prefix=provider_trace,
            )
        controlled_clarification = (
            _controlled_transport_clarification(query, pre_provider_understanding, snapshot)
            if failure_code in _RECOVERABLE_SCENARIO_PROVIDER_FAILURES else None
        )
        if controlled_clarification is not None:
            parsed, understanding = controlled_clarification
            understanding = understanding.model_copy(update={"failure_code": failure_code})
            return await rule_agent_ask(
                session, company_id, user_id, role, query, conversation.id,
                selected_product_ids, selection_revision, selection_bound_session_id,
                understanding_override=understanding, parsed_override=parsed,
                response_mode="deterministic_fallback", fallback_reason=failure_code,
                provider_notice="语义模型本次未产生可用结构化结果；后端未猜测缺失条件，请补充必要信息。",
                fallback_used_override=True, requested_provider_override=requested_provider,
                model_route_override="QWEN_SEMANTIC", provider_calls_override=provider_calls,
                trace_prefix=provider_trace,
            )
        parsed = semantic_policy("unknown", ())
        understanding = understanding.model_copy(update={
            "intent": "unknown", "question_type": "unknown", "route": "CLARIFICATION",
            "planner_calls": 1 if candidates else 0, "failure_code": failure_code,
            "clarification_required": True, "clarification_reason": SAFE_PROVIDER_MESSAGES.get(failure_code),
        })
        return await rule_agent_ask(
            session, company_id, user_id, role, query, conversation.id,
            selected_product_ids, selection_revision, selection_bound_session_id,
            understanding_override=understanding, parsed_override=parsed,
            response_mode="deterministic_fallback", fallback_reason=failure_code,
            provider_notice=SAFE_PROVIDER_MESSAGES.get(failure_code, "语义模型未能完成问题理解。"),
            fallback_used_override=bool(provider_failures), requested_provider_override=requested_provider,
            model_route_override="CLARIFICATION", provider_calls_override=provider_calls, trace_prefix=provider_trace,
        )

    async def reasoning_handler(validated_result: dict[str, Any]) -> dict[str, Any] | None:
        reasoner = router.provider(router.reasoning_name)
        if not execution_policy.allows(router.reasoning_name, router.primary_name):
            return None
        if not reasoner or not router.should_reason(understanding, product_count=len(validated_result.get("products") or [])):
            return None
        if not router.reasoning_data_allowed(validated_result.get("products") or []):
            return None
        reasoning_packet, labels = _reasoning_packet(validated_result)
        call_started = time.perf_counter()
        try:
            response = await reasoner.reason(reasoning_packet)
            frame = GroundedReasoningOutput.model_validate_json(response.content)
        except ProviderFailure as exc:
            return {"failed": True, "provider_call": {
                "requested_provider": router.reasoning_name, "actual_provider": reasoner.provider_name,
                "model_name": reasoner.model_name, "route": "DEEPSEEK_REASONING", "status": "failed",
                "failure_code": exc.code, "latency_ms": round((time.perf_counter() - call_started) * 1000), "token_usage": {},
            }}
        except ValueError:
            return {"failed": True, "provider_call": {
                "requested_provider": router.reasoning_name, "actual_provider": reasoner.provider_name,
                "model_name": reasoner.model_name, "route": "DEEPSEEK_REASONING", "status": "failed",
                "failure_code": "SCHEMA_VALIDATION_FAILED", "latency_ms": round((time.perf_counter() - call_started) * 1000), "token_usage": {},
            }}
        if any(label not in labels for label in frame.priority_labels) or _reasoning_conflicts(frame, reasoning_packet, labels):
            return {"failed": True, "provider_call": {
                "requested_provider": router.reasoning_name, "actual_provider": response.provider_name,
                "model_name": response.model_name, "route": "DEEPSEEK_REASONING", "status": "failed",
                "failure_code": "SCHEMA_VALIDATION_FAILED", "latency_ms": response.latency_ms if response.latency_ms is not None else round((time.perf_counter() - call_started) * 1000), "token_usage": response.usage,
            }}
        audit = {
            "requested_provider": router.reasoning_name, "actual_provider": response.provider_name,
            "model_name": response.model_name, "route": "DEEPSEEK_REASONING", "status": "success",
            "failure_code": None, "latency_ms": response.latency_ms if response.latency_ms is not None else round((time.perf_counter() - call_started) * 1000), "token_usage": response.usage,
        }
        return {"summary": frame.summary, "provider": response.provider_name, "model": response.model_name, "usage": response.usage, "provider_call": audit}

    return await rule_agent_ask(
        session, company_id, user_id, role, query, conversation.id,
        selected_product_ids, selection_revision, selection_bound_session_id,
        understanding_override=understanding, parsed_override=parsed,
        response_mode="model_success", fallback_reason=provider_failures[0] if provider_failures else None,
        fallback_used_override=bool(provider_failures),
        requested_provider_override=requested_provider,
        fallback_provider_override=provider_result.provider_name if provider_result.provider_name != requested_provider else None,
        model_route_override="QWEN_SEMANTIC", provider_calls_override=provider_calls, trace_prefix=provider_trace,
        provider_notice=f"{provider_result.provider_name} 仅完成语义理解；企业事实由后端工具提供。",
        active_provider_override=provider_result.provider_name, active_model_override=provider_result.model_name,
        token_usage_override=provider_result.usage, reasoning_handler=reasoning_handler,
    )
