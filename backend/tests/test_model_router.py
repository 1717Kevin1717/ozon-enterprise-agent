import json

import pytest

from app.api import router as api_router
from app.agent.model_agent import _reasoning_conflicts
from app.agent.model_router import ProviderExecutionPolicy, ModelRouter, redact_external_query, semantic_context_slots
from app.agent.providers.base import ModelCapabilities, ModelProvider, ProviderCallResult, ProviderFailure
from app.schemas.agent import GroundedReasoningOutput, ModelInput
from test_stability_sprint import ask, by_title, deterministic_client, seed


class MockProvider(ModelProvider):
    capabilities = ModelCapabilities(supports_text=True, supports_structured_output=True)

    def __init__(self, name, frames=None, failure=None, *, multimodal=False, reasoning=False):
        self.provider_name = name
        self.model_name = name + "-mock"
        self.api_key = "mock-not-real"
        self.base_url = "https://mock.invalid"
        self.timeout_seconds = 1
        self.frames = list(frames or [])
        self.failure = failure
        self.calls = []
        self.capabilities = ModelCapabilities(
            supports_text=True, supports_multimodal=multimodal, supports_structured_output=True,
            supports_reasoning=reasoning, supports_tool_planning=True,
        )

    async def semantic_interpret(self, query, context_slots):
        self.calls.append(("semantic", query, context_slots))
        if self.failure:
            raise ProviderFailure(self.failure, self.provider_name)
        return ProviderCallResult(json.dumps(self.frames.pop(0), ensure_ascii=False), self.provider_name, self.model_name, {"total_tokens": 20})

    async def reason(self, decision_packet):
        self.calls.append(("reason", decision_packet))
        if self.failure:
            raise ProviderFailure(self.failure, self.provider_name)
        return ProviderCallResult(json.dumps(self.frames.pop(0), ensure_ascii=False), self.provider_name, self.model_name, {"total_tokens": 30})


def test_provider_status_distinguishes_not_configured_from_verified(deterministic_client, monkeypatch):
    headers, _ = seed(deterministic_client, "provider-status-unconfigured")
    provider = MockProvider("qwen")
    provider.api_key = ""
    router = ModelRouter([provider], primary_name="qwen", reasoning_name="deepseek")
    monkeypatch.setattr(api_router.settings, "llm_provider", "dual")
    monkeypatch.setattr(api_router, "build_model_router", lambda: router)

    payload = deterministic_client.get("/api/v1/agent/status", headers=headers).json()["data"]

    assert payload["status"] == "not_configured"
    assert payload["configured"] is False
    assert payload["real_smoke_verified"] is False
    assert payload["ready_for_mock"] is True
    assert payload["legacy"] is False
    assert payload["providers"][0]["status"] == "not_configured"
    assert payload["providers"][0]["ready_for_mock"] is True
    assert payload["capability_matrix"]["qwen"]["supports_tool_planning"] is True


def test_provider_status_key_presence_is_only_configured_unverified(deterministic_client, monkeypatch):
    headers, _ = seed(deterministic_client, "provider-status-unverified")
    router = ModelRouter([MockProvider("qwen")], primary_name="qwen", reasoning_name="deepseek")
    monkeypatch.setattr(api_router.settings, "llm_provider", "dual")
    monkeypatch.setattr(api_router, "build_model_router", lambda: router)

    payload = deterministic_client.get("/api/v1/agent/status", headers=headers).json()["data"]

    assert payload["status"] == "configured_unverified"
    assert payload["reachable"] is None
    assert payload["last_call_success"] is None
    assert payload["real_smoke_verified"] is False


def frame(intent, dimensions, tools, *, mentions=None, context=False, negative=None, preferences=None, clarification=False, reasoning=False):
    return {
        "intent": intent,
        "task_type": intent,
        "question_type": intent,
        "entity_mentions": mentions or [],
        "references": [],
        "reference_slots": ["last_explicit_entity"] if context else [],
        "requested_dimensions": dimensions,
        "negative_scope": negative or [],
        "preference_order": preferences or [],
        "confidence": 0.93,
        "route": "SEMANTIC_PLANNER",
        "planned_tools": tools,
        "requires_context": context,
        "requires_reasoning": reasoning,
        "clarification_required": clarification,
    }


def install_router(monkeypatch, model_router):
    from app.agent import model_agent
    from app.api import router as api_router

    monkeypatch.setattr(api_router.settings, "llm_provider", "qwen")
    monkeypatch.setattr(model_agent, "build_model_router", lambda: model_router)


def test_model_router_capabilities_and_multimodal_boundary():
    qwen = MockProvider("qwen", multimodal=True)
    deepseek = MockProvider("deepseek", reasoning=True)
    router = ModelRouter([qwen, deepseek], primary_name="qwen", reasoning_name="deepseek")

    assert [item.provider_name for item in router.semantic_candidates()] == ["qwen", "deepseek"]
    assert [item.provider_name for item in router.semantic_candidates(has_multimodal_input=True)] == ["qwen"]
    assert qwen.capabilities.supports_multimodal
    assert not deepseek.capabilities.supports_multimodal
    assert qwen.capabilities.supports_tool_planning and deepseek.capabilities.supports_tool_planning


def test_multimodal_contract_routes_only_to_qwen():
    qwen = MockProvider("qwen", multimodal=True)
    deepseek = MockProvider("deepseek", reasoning=True)
    router = ModelRouter([qwen, deepseek], primary_name="qwen", reasoning_name="deepseek")

    decision = router.route_input(ModelInput(text="检查图片", images=["opaque:image-1"]))

    assert decision.route == "QWEN_SEMANTIC"
    assert [item.provider_name for item in router.semantic_candidates(has_multimodal_input=True)] == ["qwen"]


def test_p01_qwen_only_candidate_policy_excludes_all_fallbacks():
    qwen = MockProvider("qwen")
    deepseek = MockProvider("deepseek", reasoning=True)
    zhipu = MockProvider("zhipu")
    router = ModelRouter([qwen, deepseek, zhipu], primary_name="qwen", reasoning_name="deepseek")

    candidates = router.semantic_candidates(policy=ProviderExecutionPolicy("QWEN_ONLY"))

    assert [item.provider_name for item in candidates] == ["qwen"]


def test_p01_qwen_only_success_executes_qwen_and_no_fallback(deterministic_client, monkeypatch):
    headers, items = seed(deterministic_client, "provider-policy-qwen-success")
    target = by_title(items, "桌面理线器")
    qwen = MockProvider("qwen", [frame("product_detail", ["profit"], [], context=True)])
    deepseek = MockProvider("deepseek", reasoning=True)
    install_router(monkeypatch, ModelRouter([qwen, deepseek], primary_name="qwen", reasoning_name="deepseek"))
    first = ask(deterministic_client, headers, target["title"] + "多少钱？")

    result = ask(
        deterministic_client, headers, "盈利能力如何？", session_id=first["session_id"],
        provider_execution_policy="QWEN_ONLY",
    )

    assert len(qwen.calls) == 1
    assert deepseek.calls == []
    assert result["active_provider"] == "qwen"
    assert {item["tool_name"] for item in result["tool_results"]} == {"get_product", "calculate_profit"}


def test_p02_qwen_only_provider_failure_never_calls_fallback(deterministic_client, monkeypatch):
    headers, items = seed(deterministic_client, "provider-policy-qwen-failure")
    target = by_title(items, "桌面理线器")
    qwen = MockProvider("qwen", failure="NETWORK_TIMEOUT")
    deepseek = MockProvider("deepseek", [frame("product_detail", ["profit"], [], context=True)], reasoning=True)
    install_router(monkeypatch, ModelRouter([qwen, deepseek], primary_name="qwen", reasoning_name="deepseek"))
    first = ask(deterministic_client, headers, target["title"] + "多少钱？")

    result = ask(
        deterministic_client, headers, "那利润呢？", session_id=first["session_id"],
        provider_execution_policy="QWEN_ONLY",
    )

    assert len(qwen.calls) == 1
    assert deepseek.calls == []
    assert result["fallback_reason"] == "NETWORK_TIMEOUT"


def test_p03_qwen_only_semantic_validation_failure_never_calls_fallback(deterministic_client, monkeypatch):
    headers, items = seed(deterministic_client, "provider-policy-qwen-schema")
    target = by_title(items, "桌面理线器")
    invalid = frame("product_detail", ["profit"], ["unregistered_write_tool"], context=True)
    qwen = MockProvider("qwen", [invalid])
    deepseek = MockProvider("deepseek", [frame("product_detail", ["profit"], [], context=True)], reasoning=True)
    install_router(monkeypatch, ModelRouter([qwen, deepseek], primary_name="qwen", reasoning_name="deepseek"))
    first = ask(deterministic_client, headers, target["title"] + "多少钱？")

    result = ask(
        deterministic_client, headers, "那利润呢？", session_id=first["session_id"],
        provider_execution_policy="QWEN_ONLY",
    )

    assert len(qwen.calls) == 1
    assert deepseek.calls == []
    assert result["fallback_reason"] == "SEMANTIC_PLAN_VALIDATION_FAILED"


def test_p04_auto_keeps_existing_semantic_fallback_order():
    qwen = MockProvider("qwen")
    deepseek = MockProvider("deepseek", reasoning=True)
    router = ModelRouter([qwen, deepseek], primary_name="qwen", reasoning_name="deepseek")

    candidates = router.semantic_candidates(policy=ProviderExecutionPolicy("AUTO"))

    assert [item.provider_name for item in candidates] == ["qwen", "deepseek"]


def test_p05_request_constraint_overrides_global_auto_candidate_set():
    qwen = MockProvider("qwen")
    deepseek = MockProvider("deepseek", reasoning=True)
    router = ModelRouter([qwen, deepseek], primary_name="qwen", reasoning_name="deepseek")

    auto = router.semantic_candidates()
    constrained = router.semantic_candidates(policy=ProviderExecutionPolicy("QWEN_ONLY"))

    assert [item.provider_name for item in auto] == ["qwen", "deepseek"]
    assert [item.provider_name for item in constrained] == ["qwen"]


def test_p06_non_admin_cannot_override_provider_policy(deterministic_client, monkeypatch):
    headers, _ = seed(deterministic_client, "provider-policy-auth")
    install_router(monkeypatch, ModelRouter([MockProvider("qwen")], primary_name="qwen", reasoning_name="deepseek"))
    analyst_headers = {**headers, "X-Role": "analyst"}

    response = deterministic_client.post(
        "/api/v1/agent/ask", headers=analyst_headers,
        json={"query": "分析当前商品", "provider_execution_policy": "QWEN_ONLY"},
    )

    assert response.status_code == 403


def test_semantic_context_packet_redacts_internal_identity():
    packet = {
        "session_id": "secret-session", "company_id": "secret-tenant",
        "last_explicit_product_ids": ["secret-product"],
        "last_resolved_product_ids": ["secret-product"],
        "last_comparison_order": ["secret-product"],
        "verified_product_names": {"secret-product": "private-name"},
        "last_intent": "product_price", "last_metric": "current_price",
        "last_requested_dimensions": ["price"], "last_preference_order": ["profit", "risk"],
        "last_negative_scope": ["competition"], "allowed_tools": ["get_product"],
    }

    slots = semantic_context_slots(packet)
    serialized = json.dumps(slots)

    assert "secret-session" not in serialized
    assert "secret-tenant" not in serialized
    assert "secret-product" not in serialized
    assert "private-name" not in serialized
    assert slots["last_metric"] == "current_price"
    assert slots["available_sources"] == ["last_explicit_entity", "last_resolved_entity", "last_comparison"]


def test_external_query_redacts_known_product_identifiers(deterministic_client):
    headers, items = seed(deterministic_client, "model-router-query-redaction")
    target = by_title(items, "桌面理线器")

    safe_query, placeholders = redact_external_query(
        f"请解释 {target['id']} 的利润", [type("ProductRef", (), {
            "id": target["id"], "external_product_id": target.get("external_product_id"), "sku": target.get("sku"),
        })()]
    )

    assert target["id"] not in safe_query
    assert list(placeholders.values()) == [target["id"]]
    assert "[PRODUCT_REF_1]" in safe_query


def test_nl01_deterministic_fast_path_does_not_call_qwen(deterministic_client, monkeypatch):
    headers, items = seed(deterministic_client, "model-router-fast")
    target = by_title(items, "桌面理线器")
    qwen = MockProvider("qwen")
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))

    result = ask(deterministic_client, headers, target["title"] + "多少钱？")

    assert qwen.calls == []
    assert result["response_type"] == "simple_fact"
    assert result["active_provider"] == "deterministic_planner"


@pytest.mark.parametrize("query_template", ["{}现在卖多少来着", "理线器目前挂的什么价"])
def test_nl02_nl03_qwen_semantic_price(deterministic_client, monkeypatch, query_template):
    headers, items = seed(deterministic_client, "model-router-price-" + str(abs(hash(query_template))))
    target = by_title(items, "桌面理线器")
    query = query_template.format(target["title"])
    mention = target["title"] if target["title"] in query else "理线器"
    qwen = MockProvider("qwen", [frame("product_price", ["price"], ["get_product"], mentions=[mention])])
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))

    result = ask(deterministic_client, headers, query)

    assert len(qwen.calls) == 1
    assert result["intent"] == "product_price"
    assert result["product_ids"] == [target["id"]]
    assert result["active_provider"] == "qwen"
    assert result["tool_call_count"] == 1


@pytest.mark.parametrize("follow_up", ["赚得如何", "这东西挣钱能力如何"])
def test_nl04_nl05_contextual_profit_routes_qwen_then_backend(deterministic_client, monkeypatch, follow_up):
    headers, items = seed(deterministic_client, "model-router-profit-" + str(abs(hash(follow_up))))
    target = by_title(items, "桌面理线器")
    qwen = MockProvider("qwen", [frame("product_detail", ["profit", "roi"], ["get_product", "calculate_profit"], context=True)])
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))
    first = ask(deterministic_client, headers, target["title"] + "多少钱？")

    result = ask(deterministic_client, headers, follow_up, session_id=first["session_id"])

    assert len(qwen.calls) == 1
    assert qwen.calls[0][2]["last_metric"] == "current_price"
    assert result["product_ids"] == [target["id"]]
    assert result["requested_dimensions"] == ["profit", "roi"]
    assert {item["tool_name"] for item in result["tool_results"]} == {"get_product", "calculate_profit"}


def test_qwen_technical_failure_falls_back_once_to_deepseek(deterministic_client, monkeypatch):
    headers, items = seed(deterministic_client, "model-router-fallback")
    target = by_title(items, "桌面理线器")
    query = target["title"] + "目前挂的什么价"
    qwen = MockProvider("qwen", failure="NETWORK_TIMEOUT")
    deepseek = MockProvider("deepseek", [frame("product_price", ["price"], ["get_product"], mentions=[target["title"]])], reasoning=True)
    install_router(monkeypatch, ModelRouter([qwen, deepseek], primary_name="qwen", reasoning_name="deepseek"))

    result = ask(deterministic_client, headers, query)

    assert len(qwen.calls) == 1 and len(deepseek.calls) == 1
    assert result["active_provider"] == "deepseek"
    assert result["response_type"] == "simple_fact"
    assert result["fallback_used"] is True
    assert result["fallback_reason"] == "NETWORK_TIMEOUT"
    assert result["requested_provider"] == "qwen"
    assert result["fallback_provider"] == "deepseek"
    assert [item["status"] for item in result["provider_calls"]] == ["failed", "success"]


def test_provider_transport_diagnostics_reach_agent_audit(deterministic_client, monkeypatch):
    headers, items = seed(deterministic_client, "model-router-transport-audit")
    target = by_title(items, "桌面理线器")
    qwen = MockProvider("qwen")

    async def fail_with_safe_diagnostics(query, context_slots):
        raise ProviderFailure(
            "CONNECT_ERROR", "qwen", exception_class="ConnectError",
            failure_stage="DNS_CONNECT", elapsed_ms=23, upstream_status=None,
        )

    qwen.semantic_interpret = fail_with_safe_diagnostics
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))

    result = ask(deterministic_client, headers, target["title"] + "目前挂的什么价")

    audit = result["provider_calls"][0]
    assert audit["failure_code"] == "CONNECT_ERROR"
    assert audit["exception_class"] == "ConnectError"
    assert audit["failure_stage"] == "DNS_CONNECT"
    assert audit["latency_ms"] == 23
    assert audit["upstream_status"] is None
    assert result["fallback_used"] is True


def test_negative_scope_controls_backend_dimensions(deterministic_client, monkeypatch):
    headers, items = seed(deterministic_client, "model-router-negative")
    cable = by_title(items, "桌面理线器")
    meter = by_title(items, "智能温湿度计")
    query = f"{cable['title']}和{meter['title']}只看赚钱能力，不做综合判断"
    qwen = MockProvider("qwen", [frame(
        "profit_comparison", ["profit", "roi"], ["compare_products", "calculate_profit"],
        mentions=[cable["title"], meter["title"]], negative=["competition", "demand", "recommendation"],
    )])
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))

    result = ask(deterministic_client, headers, query)

    assert result["response_type"] == "comparison_result", result
    assert result["requested_dimensions"] == ["profit", "roi"]
    assert not ({"competition", "demand", "recommendation"} & set(result["display_scope"]))


def test_semantic_frame_cannot_inject_business_facts(deterministic_client, monkeypatch):
    headers, items = seed(deterministic_client, "model-router-fact-injection")
    target = by_title(items, "桌面理线器")
    invalid = frame("product_price", ["price"], ["get_product"], mentions=[target["title"]])
    invalid["price"] = 1
    qwen = MockProvider("qwen", [invalid])
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))

    result = ask(deterministic_client, headers, target["title"] + "目前挂的什么价")

    assert len(qwen.calls) == 1
    assert result["response_type"] == "clarification"
    assert result["fallback_reason"] == "SCHEMA_VALIDATION_FAILED"
    assert result["clarification_code"] is None
    assert result["products"] == []
    assert result["provider_calls"][0]["failure_stage"] == "SCHEMA_VALIDATE"
    assert result["provider_calls"][0]["exception_class"] == "SemanticPlanValidationFailure"
    assert result["provider_calls"][0]["validation_rule_id"] == "SEM000"
    assert result["provider_calls"][0]["validation_field"] == "price"


def test_c03_technical_invalid_response_is_not_user_clarification(deterministic_client, monkeypatch):
    headers, items = seed(deterministic_client, "model-router-invalid-response")
    target = by_title(items, "桌面理线器")
    qwen = MockProvider("qwen", failure="INVALID_RESPONSE")
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))
    first = ask(deterministic_client, headers, target["title"] + "多少钱？")

    result = ask(
        deterministic_client, headers, "盈利情况如何？", session_id=first["session_id"],
        provider_execution_policy="QWEN_ONLY",
    )

    assert result["response_type"] == "clarification"
    assert result["fallback_reason"] == "INVALID_RESPONSE"
    assert result["clarification_code"] is None
    assert result["tool_call_count"] == 0


def test_new_session_does_not_expose_prior_entity_to_qwen(deterministic_client, monkeypatch):
    headers, items = seed(deterministic_client, "model-router-isolation")
    target = by_title(items, "桌面理线器")
    clarification = frame("unknown", [], [], clarification=True)
    qwen = MockProvider("qwen", [clarification])
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))
    ask(deterministic_client, headers, target["title"] + "多少钱？")

    result = ask(deterministic_client, headers, "这个现在怎么个说法？")

    assert qwen.calls[0][2]["available_sources"] == []
    assert result["response_type"] == "clarification"
    assert result["product_ids"] == []


def test_reasoning_router_requires_complex_task_configured_provider_and_data_policy():
    deepseek = MockProvider("deepseek", reasoning=True)
    router = ModelRouter([deepseek], primary_name="qwen", reasoning_name="deepseek", data_mode="mock_only")
    understanding = type("U", (), {"intent": "selection_recommendation", "preference_order": ["profit", "risk"], "constraints": {}, "requires_reasoning": True})()

    assert router.should_reason(understanding, product_count=2)
    assert router.reasoning_data_allowed([{"is_mock": True}, {"is_mock": True}])
    assert not router.reasoning_data_allowed([{"is_mock": False}])


def test_deepseek_receives_only_pseudonymized_mock_facts(deterministic_client, monkeypatch):
    headers, items = seed(deterministic_client, "model-router-reasoning")
    selected = items[:3]
    qwen = MockProvider("qwen", [frame(
        "selection_recommendation", ["profit", "risk", "compliance", "recommendation", "decision"],
        ["compare_products"], context=True, preferences=["profit", "risk", "compliance"], reasoning=True,
    )])
    deepseek = MockProvider("deepseek", [{
        "summary": "P1 的相对表现较高，但正式状态仍以后端门禁为准。",
        "priority_labels": ["P1"],
        "dimensions_used": ["profit", "risk", "compliance"],
        "formal_recommendation_label": None,
        "human_review_required": True,
    }], reasoning=True)
    install_router(monkeypatch, ModelRouter(
        [qwen, deepseek], primary_name="qwen", reasoning_name="deepseek", data_mode="mock_only",
    ))
    session = ask(deterministic_client, headers, "目前公司一共有多少商品？")

    result = ask(
        deterministic_client, headers, "这几个里利润优先、风险其次，合规不过直接排除",
        session_id=session["session_id"], selected_product_ids=[item["id"] for item in selected],
        selection_revision=1, selection_bound_session_id=session["session_id"],
    )

    assert len(qwen.calls) == 1 and len(deepseek.calls) == 1, {
        key: result.get(key) for key in (
            "response_type", "context_source", "selection_source", "product_ids", "tool_results",
            "requested_dimensions", "fallback_reason", "understanding",
        )
    }
    reasoning_packet = deepseek.calls[0][1]
    serialized = json.dumps(reasoning_packet, ensure_ascii=False)
    assert all(item["id"] not in serialized and item["title"] not in serialized for item in selected)
    assert [item["label"] for item in reasoning_packet["products"]] == ["P1", "P2", "P3"]
    assert result["active_provider"] == "qwen+deepseek"
    assert result["answer"] == result["conclusion"]
    assert result["model_summary"]
    assert result["decision_status"] != "NOT_APPLICABLE"


def test_deepseek_cannot_reference_unknown_product_label(deterministic_client, monkeypatch):
    headers, items = seed(deterministic_client, "model-router-reasoning-label")
    qwen = MockProvider("qwen", [frame(
        "selection_recommendation", ["profit", "risk", "compliance", "recommendation", "decision"],
        ["compare_products"], context=True, preferences=["profit", "risk"], reasoning=True,
    )])
    deepseek = MockProvider("deepseek", [{
        "summary": "虚构标签不应进入最终解释。", "priority_labels": ["P999"],
        "dimensions_used": ["profit"], "formal_recommendation_label": None,
        "human_review_required": False,
    }], reasoning=True)
    install_router(monkeypatch, ModelRouter(
        [qwen, deepseek], primary_name="qwen", reasoning_name="deepseek", data_mode="mock_only",
    ))
    session = ask(deterministic_client, headers, "目前公司一共有多少商品？")

    result = ask(
        deterministic_client, headers, "这几个里利润优先、风险其次，帮我做决策",
        session_id=session["session_id"], selected_product_ids=[item["id"] for item in items[:3]],
        selection_revision=1, selection_bound_session_id=session["session_id"],
    )

    assert len(deepseek.calls) == 1
    assert result["active_provider"] == "qwen"
    assert result["model_summary"] == ""
    assert "虚构标签" not in result["answer"]


def test_reasoning_cannot_upgrade_insufficient_snapshot_trend():
    frame_value = GroundedReasoningOutput(
        summary="销量趋势明显向好，已经确认上涨。", dimensions_used=["sales_snapshot"]
    )
    packet = {
        "requested_dimensions": ["sales_snapshot"],
        "data_sufficiency": {"status": "INSUFFICIENT_DATA", "observed_trend": "INSUFFICIENT_DATA"},
    }

    assert _reasoning_conflicts(frame_value, packet, {}) is True


def test_deepseek_cannot_override_compliance_gate(deterministic_client, monkeypatch):
    headers, items = seed(deterministic_client, "model-router-compliance-gate")
    rejected = next(item for item in items if item.get("compliance_status") == "rejected")
    selected = [rejected, *[item for item in items if item["id"] != rejected["id"]][:2]]
    qwen = MockProvider("qwen", [frame(
        "selection_recommendation", ["profit", "risk", "compliance", "recommendation", "decision"],
        ["compare_products"], context=True, preferences=["profit", "risk"], reasoning=True,
    )])
    deepseek = MockProvider("deepseek", [{
        "summary": "利润很高，建议立即上架。", "priority_labels": ["P1"],
        "dimensions_used": ["profit", "risk", "compliance"], "formal_recommendation_label": None,
        "human_review_required": False,
    }], reasoning=True)
    install_router(monkeypatch, ModelRouter(
        [qwen, deepseek], primary_name="qwen", reasoning_name="deepseek", data_mode="mock_only",
    ))
    session = ask(deterministic_client, headers, "目前公司一共有多少商品？")

    result = ask(
        deterministic_client, headers, "这几个里利润优先、风险其次，请解释审核顺序",
        session_id=session["session_id"], selected_product_ids=[item["id"] for item in selected],
        selection_revision=1, selection_bound_session_id=session["session_id"],
    )

    rejected_result = next(item for item in result["products"] if item["id"] == rejected["id"])
    assert rejected_result["decision_status"] == "BLOCKED"
    assert result["model_summary"] == ""
    assert "立即上架" not in result["answer"]
    assert result["provider_calls"][-1]["failure_code"] == "SCHEMA_VALIDATION_FAILED"


def test_reasoning_cannot_lower_backend_high_risk():
    frame_value = GroundedReasoningOutput(
        summary="P1 属于低风险，可以继续。", dimensions_used=["risk"]
    )
    labels = {"P1": {"risk_level": "high", "decision_status": "HUMAN_REVIEW_REQUIRED"}}

    assert _reasoning_conflicts(frame_value, {"requested_dimensions": ["risk"]}, labels) is True
