import json

from app.agent.query_understanding import understand_query
from app.agent.model_router import ModelRouter
from app.schemas.agent import CollectionState, ContextSnapshot, TaskState
from test_model_router import MockProvider, install_router
from test_stability_sprint import ask, deterministic_client, seed


def active_collection_snapshot() -> ContextSnapshot:
    collection = CollectionState(
        collection_type="candidate_pool",
        product_ids=["p1", "p2", "p3", "p4"],
        ranked_product_ids=["p1", "p2", "p3", "p4"],
        top_product_ids=["p1", "p2", "p3"],
        top_k=3,
        ranking_dimensions=["recommendation"],
        requested_dimensions=["recommendation"],
        source_task_revision=1,
        created_turn=1,
    )
    return ContextSnapshot(
        session_id="demo-session",
        company_id="demo-company",
        user_id="demo-user",
        current_query="",
        task_state=TaskState(active_collection=collection),
    )


def test_ranked_collection_selector_composite_does_not_become_product_entity():
    parsed, frame = understand_query(
        "比较当前动态推荐度最高的三个商品，说明利润、竞争、合规和数据缺口，给出人工审核顺序。",
        [], (), active_collection_snapshot(),
    )

    assert parsed.name == "collection_analysis"
    assert frame.route == "SEMANTIC_PLANNER"
    assert frame.collection_reference == "current_collection"
    assert frame.entity_mentions == []
    assert frame.collection_selector == {
        "source": "ACTIVE_COLLECTION",
        "metric": "recommendation_score",
        "direction": "DESC",
        "limit": 3,
    }
    assert set(frame.analysis_requests) == {
        "COMPARE", "EVIDENCE_GAP", "HUMAN_REVIEW_PRIORITY",
    }


def test_collection_relative_margin_filter_and_profit_diagnosis_are_structured():
    parsed, frame = understand_query(
        "找出当前候选池中净利率低于目标的商品，并说明可调整的价格或成本因素。",
        [], (), active_collection_snapshot(),
    )

    assert parsed.name == "collection_analysis"
    assert frame.route == "SEMANTIC_PLANNER"
    assert frame.entity_mentions == []
    assert frame.collection_filters == [{
        "field": "net_margin",
        "operator": "LT",
        "value": None,
        "reference_field": "target_margin_rate",
        "reference_value_source": "PRODUCT_MASTER",
    }]
    assert frame.analysis_requests == ["PROFIT_DIAGNOSIS"]


def test_collection_history_analysis_does_not_become_product_entity():
    parsed, frame = understand_query(
        "检查当前候选是否存在同品牌或同类目的历史放弃案例，并列出需要人工复核的原因。",
        [], (), active_collection_snapshot(),
    )

    assert parsed.name == "collection_analysis"
    assert frame.route == "SEMANTIC_PLANNER"
    assert frame.entity_mentions == []
    assert frame.analysis_requests == ["HISTORY_ANALYSIS", "HUMAN_REVIEW_PRIORITY"]


def establish_collection(client, company: str):
    headers, _ = seed(client, company)
    created = ask(
        client, headers,
        "请分析当前候选池里最值得继续研究的三个商品方向，并说明证据缺口",
    )
    assert created["task_state"]["active_collection"] is not None
    return headers, created


def test_production_endpoint_executes_ranked_collection_composite(deterministic_client):
    headers, created = establish_collection(deterministic_client, "demo-ranked-composite")
    result = ask(
        deterministic_client, headers,
        "比较当前动态推荐度最高的三个商品，说明利润、竞争、合规和数据缺口，给出人工审核顺序。",
        session_id=created["session_id"],
    )

    assert result["intent"] == "collection_analysis"
    assert result["response_type"] == "collection_report"
    assert result["displayed_count"] <= 3, result
    assert result["human_review_priority"]
    assert result["product_ids"]
    assert result["tool_call_count"] >= 1
    assert result["clarification_code"] is None


def test_production_endpoint_executes_relative_margin_profit_diagnosis(deterministic_client):
    headers, created = establish_collection(deterministic_client, "demo-profit-diagnosis")
    result = ask(
        deterministic_client, headers,
        "找出当前候选池中净利率低于目标的商品，并说明可调整的价格或成本因素。",
        session_id=created["session_id"],
    )

    assert result["intent"] == "collection_analysis"
    assert result["response_type"] == "collection_report"
    assert result["profit_diagnosis"], json.dumps({
        "understanding": result["understanding"],
        "tool_results": result["tool_results"],
        "collection_analysis": result["collection_analysis"],
    }, ensure_ascii=False, default=str)
    assert all(item["current_net_margin"] < item["target_net_margin"] for item in result["profit_diagnosis"])
    assert all(
        "adjustable_scenario_fields" in item and "protected_fields" in item
        for item in result["profit_diagnosis"]
    )
    assert result["clarification_code"] is None


def test_production_endpoint_executes_historical_decision_analysis(deterministic_client):
    headers, created = establish_collection(deterministic_client, "demo-history-analysis")
    result = ask(
        deterministic_client, headers,
        "检查当前候选是否存在同品牌或同类目的历史放弃案例，并列出需要人工复核的原因。",
        session_id=created["session_id"],
    )

    assert result["intent"] == "collection_analysis"
    assert result["response_type"] == "collection_report"
    assert result["historical_decision_analysis"], result
    assert result["human_review_priority"]
    assert result["clarification_code"] is None


def test_collection_context_is_required_and_never_widens_to_company_catalog(deterministic_client):
    headers, _ = seed(deterministic_client, "demo-missing-collection")
    result = ask(
        deterministic_client, headers,
        "推荐度最高的三个拿出来比较一下，并说明数据缺口",
    )

    assert result["response_type"] == "clarification"
    assert result["clarification_code"] == "MISSING_CONTEXT"
    assert result["tool_call_count"] == 0
    assert result["task_state"]["active_collection"] is None


def test_invalid_provider_plan_recovers_only_complete_collection_frame(
    deterministic_client, monkeypatch,
):
    headers, created = establish_collection(deterministic_client, "demo-provider-fallback")
    qwen = MockProvider("qwen", failure="SEMANTIC_PLAN_VALIDATION_FAILED")
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))

    result = ask(
        deterministic_client, headers,
        "前三名按利润、竞争和合规对比，并说明证据缺口",
        session_id=created["session_id"], provider_execution_policy="QWEN_ONLY",
    )

    assert result["intent"] == "collection_analysis"
    assert result["response_type"] == "collection_report"
    assert result["fallback_used"] is True
    assert result["fallback_reason"] == "SEMANTIC_PLAN_VALIDATION_FAILED"
    assert result["tool_call_count"] >= 1
    assert len(qwen.calls) == 1


def test_capability_endpoint_gates_visible_suggestions_by_session_context(deterministic_client):
    headers, _ = seed(deterministic_client, "demo-capabilities")
    initial = deterministic_client.get("/api/v1/agent/capabilities", headers=headers).json()["data"]

    assert initial["context"]["has_active_collection"] is False
    assert [item["golden_case_id"] for item in initial["suggestions"]] == ["D01"]
    assert all(item["demo_verified"] for item in initial["suggestions"])

    created = ask(
        deterministic_client, headers,
        "请分析当前候选池里最值得继续研究的三个商品方向，并说明证据缺口",
    )
    active = deterministic_client.get(
        "/api/v1/agent/capabilities",
        headers=headers, params={"session_id": created["session_id"]},
    ).json()["data"]

    assert active["context"]["has_active_collection"] is True
    assert {item["golden_case_id"] for item in active["suggestions"]} == {"D01", "D02", "D04", "D05"}
    assert all(item["capability_id"] and item["golden_case_id"] for item in active["suggestions"])


def test_ranked_group_comparison_is_a_collection_operation():
    parsed, understanding = understand_query(
        "比较利润最高的三个和推荐度最高的三个",
        [], context_snapshot=active_collection_snapshot(),
    )
    assert parsed.name == "collection_analysis"
    assert understanding.operation == "COMPARE_COLLECTION_MEMBERS"
    assert understanding.analysis_requests == ["COMPARE"]
    groups = understanding.collection_selector["groups"]
    assert {(item["metric"], item["limit"]) for item in groups} == {
        ("net_profit", 3), ("recommendation_score", 3),
    }


def test_active_scenario_ordinal_is_anchored_before_provider():
    snapshot = active_collection_snapshot().model_copy(update={"has_active_scenario": True})
    parsed, understanding = understand_query(
        "第一名ROI多少？", [], context_snapshot=snapshot,
    )
    assert parsed.name == "scenario_analysis"
    assert understanding.intent == "scenario_analysis"
    assert understanding.operation == "EXPLAIN_SCENARIO"
    assert understanding.scenario_reference_role == "ACTIVE_SCENARIO"
    assert understanding.ordinal_reference == 1
    assert understanding.requested_dimensions == ["roi"]
