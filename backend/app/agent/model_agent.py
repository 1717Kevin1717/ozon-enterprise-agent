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
    snapshot = build_context_snapshot(
        conversation, company_id=company_id, user_id=user_id, query=query, products=products,
        selected_product_ids=selected_product_ids, selection_revision=selection_revision,
        selection_bound_session_id=selection_bound_session_id,
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
    packet_dict = packet.model_dump(mode="json")
    slots = semantic_context_slots(packet_dict)
    external_query, redacted_identifiers = redact_external_query(query, products)
    provider_result = None
    requested_provider = router.primary_name
    failure_code = "PROVIDER_NOT_CONFIGURED"
    provider_failures: list[str] = []
    provider_calls: list[dict[str, Any]] = []
    primary = router.provider(requested_provider)
    if not primary or not primary.configured:
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
            provider_failures.append(failure_code)
            provider_calls.append({
                "requested_provider": requested_provider, "actual_provider": provider.provider_name,
                "model_name": provider.model_name, "route": "QWEN_SEMANTIC", "status": "failed",
                "failure_code": failure_code, "latency_ms": round((time.perf_counter() - call_started) * 1000), "token_usage": {},
                "exception_class": type(exc).__name__, "failure_stage": exc.stage,
                "upstream_status": candidate.upstream_status if candidate else None,
                "validation_rule_id": exc.rule_id, "validation_reason_code": exc.reason_code,
                "validation_field": exc.field,
            })
        except (ValueError, TypeError) as exc:
            failure_code = "SEMANTIC_PLAN_VALIDATION_FAILED"
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
        **item,
    } for item in provider_calls]
    if provider_result is None:
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
