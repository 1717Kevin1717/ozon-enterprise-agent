import pytest
from types import SimpleNamespace

from app.agent.entity_resolution import query_mentions
from app.agent.model_router import semantic_context_slots
from app.agent.query_understanding import (
    arbitrate_entity_mentions,
    build_semantic_context_packet,
    understand_query,
)
from app.schemas.agent import (
    ComparisonState,
    ContextSnapshot,
    QueryUnderstanding,
    ResultSetState,
    TaskEntitySlot,
    TaskState,
)
from test_stability_sprint import ask, by_title, deterministic_client, seed
from test_model_router import MockProvider, frame, install_router


def _new_catalog(client, company):
    return seed(client, company)


@pytest.mark.parametrize(
    ("query", "protected", "expected"),
    [
        ("帮我筛选净利率不低于30%的商品", (), []),
        ("把结果按利润从高到低排序", (), []),
        (
            "商品甲和商品乙哪个更值得做",
            ("商品甲", "商品乙"),
            ["商品甲", "商品乙"],
        ),
    ],
)
def test_entity_role_arbitration_keeps_task_language_out_of_product_spans(query, protected, expected):
    assert query_mentions(query, protected) == expected


def test_task_state_filter_mutations_are_session_scoped_and_deterministic(deterministic_client):
    headers, _ = _new_catalog(deterministic_client, "task-state-mutations")
    created = ask(
        deterministic_client,
        headers,
        "筛选净利润率不低于20%，竞争评分低于60的商品，返回5个",
    )
    session_id = created["session_id"]

    assert created["task_state"]["task_type"] == "product_filter"
    assert created["task_state"]["operation"] == "CREATE"
    assert created["task_state"]["filter_spec"]["min_margin_rate"] == pytest.approx(0.20)
    assert created["task_state"]["filter_spec"]["max_competition_score"] == pytest.approx(60)
    assert len(created["products"]) <= 5

    added = ask(deterministic_client, headers, "再加一条，不能是高风险商品", session_id=session_id)
    assert added["task_state"]["operation"] == "ADD_CONSTRAINT"
    assert added["task_state"]["filter_spec"]["excluded_risk_levels"] == ["high"]
    assert added["task_state"]["filter_spec"]["min_margin_rate"] == pytest.approx(0.20)
    assert all(item["risk_level"] != "high" for item in added["products"])

    updated = ask(deterministic_client, headers, "把净利润率改成30%，其他条件不变", session_id=session_id)
    assert updated["task_state"]["operation"] == "UPDATE_CONSTRAINT"
    assert updated["task_state"]["filter_spec"]["min_margin_rate"] == pytest.approx(0.30)
    assert updated["task_state"]["filter_spec"]["excluded_risk_levels"] == ["high"]
    assert all(item["current_margin_rate"] >= 0.30 for item in updated["products"])

    removed = ask(deterministic_client, headers, "取消风险限制", session_id=session_id)
    assert removed["task_state"]["operation"] == "REMOVE_CONSTRAINT"
    assert "excluded_risk_levels" not in removed["task_state"]["filter_spec"]
    assert removed["task_state"]["filter_spec"]["min_margin_rate"] == pytest.approx(0.30)


def test_task_state_inspect_and_rerun_reuse_normalized_filter(deterministic_client):
    headers, _ = _new_catalog(deterministic_client, "task-state-inspect-rerun")
    created = ask(deterministic_client, headers, "筛选风险低的商品，返回3个")
    session_id = created["session_id"]

    inspected = ask(deterministic_client, headers, "刚才的筛选条件是什么", session_id=session_id)
    assert inspected["task_state"]["operation"] == "INSPECT"
    assert inspected["tool_call_count"] == 0
    assert inspected["filter_criteria"]["risk_level"] == "low"

    rerun = ask(deterministic_client, headers, "重新执行筛选", session_id=session_id)
    assert rerun["task_state"]["operation"] == "RERUN"
    assert rerun["filter_criteria"]["risk_level"] == "low"
    assert rerun["tool_call_count"] == 1
    assert all(item["risk_level"] == "low" for item in rerun["products"])


def test_task_state_result_order_supports_reorder_and_ordinal_reference(deterministic_client):
    headers, _ = _new_catalog(deterministic_client, "task-state-result-order")
    created = ask(deterministic_client, headers, "筛选合规状态通过的商品，返回4个")
    session_id = created["session_id"]
    reordered = ask(deterministic_client, headers, "把结果按利润从高到低排序", session_id=session_id)

    assert reordered["task_state"]["operation"] == "REORDER"
    assert reordered["task_state"]["sort_spec"] == ["net_profit", "desc"]
    profits = [item["net_profit"] for item in reordered["products"]]
    assert profits == sorted(profits, reverse=True)

    second = ask(deterministic_client, headers, "第二个的价格呢", session_id=session_id)
    assert second["context_source"] == "ordinal_reference"
    assert second["product_ids"] == [reordered["product_ids"][1]], second


def test_task_state_does_not_cross_session_boundary(deterministic_client):
    headers, _ = _new_catalog(deterministic_client, "task-state-isolation")
    created = ask(deterministic_client, headers, "筛选风险低的商品，返回3个")
    new_session_id = deterministic_client.post(
        "/api/v1/agent/sessions", headers=headers, json={}
    ).json()["data"]["id"]

    isolated = ask(deterministic_client, headers, "重新执行筛选", session_id=new_session_id)
    assert isolated["session_id"] != created["session_id"]
    assert isolated["response_type"] == "clarification"
    assert isolated["task_state"]["status"] in {"EMPTY", "NEEDS_CLARIFICATION"}
    assert isolated["tool_call_count"] == 0


def test_partial_entity_recovery_retains_only_backend_resolved_identity(deterministic_client):
    headers, items = _new_catalog(deterministic_client, "task-state-recover")
    known = next(item for item in items if "桌面理线器" in item["title"])
    blocked = ask(deterministic_client, headers, "虚构量子厨具和桌面理线器哪个更值得做")

    statuses = {item["mention"]: item["status"] for item in blocked["task_state"]["pending_entities"]}
    assert blocked["response_type"] == "not_found"
    assert statuses["虚构量子厨具"] == "NOT_FOUND"
    assert blocked["task_state"]["active_entities"] == [known["id"]]

    recovered = ask(
        deterministic_client, headers, "先不管没找到的那个，只分析已找到的商品",
        session_id=blocked["session_id"],
    )
    assert recovered["task_state"]["operation"] == "RECOVER"
    assert recovered["product_ids"] == [known["id"]]
    assert recovered["task_state"]["pending_entities"] == []
    assert recovered["fallback_reason"] is None
    assert recovered["tool_call_count"] >= 1


def test_comparison_dimension_refine_reuses_ordered_comparison_set(deterministic_client):
    headers, items = _new_catalog(deterministic_client, "task-state-compare-refine")
    first, second = by_title(items, "桌面理线器"), by_title(items, "智能温湿度计")
    compared = ask(deterministic_client, headers, f"详细比较{first['title']}和{second['title']}")
    assert compared["task_state"]["active_comparison_set"], compared
    refined = ask(
        deterministic_client, headers, "只保留利润、风险和推荐度三个维度",
        session_id=compared["session_id"],
    )

    assert refined["task_state"]["operation"] == "REFINE"
    assert refined["product_ids"] == compared["product_ids"]
    assert refined["requested_dimensions"] == ["profit", "risk", "recommendation"]
    assert refined["task_state"]["active_comparison_set"]["dimensions"] == ["profit", "risk", "recommendation"]
    assert refined["tool_call_count"] == 1

    second_result = ask(
        deterministic_client, headers, "第二个净利率多少",
        session_id=compared["session_id"],
    )
    assert second_result["context_source"] == "ordinal_reference"
    assert second_result["product_ids"] == [compared["product_ids"][1]]
    assert second_result["response_type"] == "product_detail"


def test_semantic_task_packet_is_normalized_and_outbound_slots_are_anonymous():
    state = TaskState(
        task_id="internal-task-id", revision=3, task_type="product_filter", operation="UPDATE_CONSTRAINT",
        status="COMPLETED", active_entities=["internal-product-a"],
        active_result_set=ResultSetState(
            product_ids=["internal-product-a", "internal-product-b"], source_task_type="product_filter",
            filter_spec={"min_margin_rate": 0.2}, sort_spec=["net_profit", "desc"], created_turn=2,
        ),
        active_comparison_set=ComparisonState(
            product_ids=["internal-product-a", "internal-product-b"], dimensions=["profit", "risk"],
            revision=2, created_turn=3,
        ),
        pending_entities=[TaskEntitySlot(mention="不存在商品", status="NOT_FOUND")],
        filter_spec={"min_margin_rate": 0.2}, sort_spec=["net_profit", "desc"],
        requested_dimensions=["profit", "risk"], last_successful_action="UPDATE_CONSTRAINT",
    )
    snapshot = ContextSnapshot(
        session_id="internal-session", company_id="internal-company", user_id="internal-user",
        current_query="只看利润", task_state=state,
    )
    understanding = QueryUnderstanding(
        intent="product_comparison", operation="REFINE", requested_dimensions=["profit", "risk"],
        confidence=0.9, route="SEMANTIC_PLANNER",
    )
    products = [
        SimpleNamespace(id="internal-product-a", title="商品甲"),
        SimpleNamespace(id="internal-product-b", title="商品乙"),
    ]

    packet = build_semantic_context_packet("只看利润", understanding, snapshot, products)
    assert packet.task_context.result_set_count == 2
    assert packet.task_context.comparison_count == 2
    assert packet.task_context.active_filter_spec == {"min_margin_rate": 0.2}
    assert packet.task_context.has_pending_unresolved_entity is True

    outbound = semantic_context_slots(packet.model_dump(mode="json"))
    serialized = str(outbound)
    assert "internal-product" not in serialized
    assert "internal-session" not in serialized
    assert "internal-company" not in serialized
    assert "internal-user" not in serialized
    assert "商品甲" not in serialized and "商品乙" not in serialized
    assert outbound["task_context"]["active_entity_slots"] == ["T1", "T2"]


def test_fastpath_gate_routes_mutation_and_ordinal_to_semantic_but_keeps_clean_fact():
    active = TaskState(
        task_id="t", revision=1, task_type="product_filter", status="COMPLETED",
        active_result_set=ResultSetState(
            product_ids=["p1", "p2"], source_task_type="product_filter",
            filter_spec={"min_margin_rate": 0.1}, created_turn=1,
        ),
    )
    snapshot = ContextSnapshot(
        session_id="s", company_id="c", user_id="u", current_query="test", task_state=active,
    )
    products = [SimpleNamespace(id="p1", title="商品甲", external_product_id="ext1", sku="sku1")]

    _, mutation = understand_query("把净利率改成20%", products, context_snapshot=snapshot)
    _, ordinal = understand_query("第二个净利率多少", products, context_snapshot=snapshot)
    _, fact = understand_query("商品甲多少钱", products, context_snapshot=snapshot)

    assert mutation.route == "SEMANTIC_PLANNER"
    assert ordinal.route == "SEMANTIC_PLANNER"
    assert fact.route == "DETERMINISTIC_FAST_PATH"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("换成智能温湿度计看看", ["智能温湿度计"]),
        ("桌面理线器哪个更值得做", ["桌面理线器"]),
        ("帮我从公司商品库找5个净利率大于10%的商品", []),
    ],
)
def test_entity_span_separates_product_source_scope_and_action_suffix(query, expected):
    names = ("智能温湿度计", "桌面理线器")
    assert query_mentions(query, names) == expected


@pytest.mark.parametrize(
    "query",
    [
        "这些结果是不是都来自公司商品库",
        "按风险优先重新排",
        "第二个净利率多少",
        "挑一个值得进一步研究的商品",
        "假设广告支出减半",
    ],
)
def test_fastpath_gate_blocks_stateful_or_semantic_operations(query):
    state = TaskState(
        task_id="t", revision=1, task_type="product_filter", status="COMPLETED",
        active_result_set=ResultSetState(
            product_ids=["p1", "p2"], source_task_type="product_filter", created_turn=1,
        ),
    )
    snapshot = ContextSnapshot(
        session_id="s", company_id="c", user_id="u", current_query=query, task_state=state,
    )
    _, understood = understand_query(query, [], context_snapshot=snapshot)
    assert understood.route != "DETERMINISTIC_FAST_PATH"


def test_non_product_provider_role_never_reaches_catalogue_resolution():
    product = SimpleNamespace(
        id="p1", title="桌面理线器", external_product_id="ext1", sku="sku1",
    )
    understood = QueryUnderstanding(
        intent="product_filter",
        entity_mentions=["公司商品库找5个"],
        entity_roles=["SOURCE_SCOPE"],
        requested_dimensions=["filters"],
        confidence=0.9,
        route="SEMANTIC_PLANNER",
    )

    arbitration = arbitrate_entity_mentions([product], understood, "从公司商品库找5个候选")

    assert arbitration.resolution.requested_entities == []
    assert arbitration.candidate_roles == ("SOURCE_SCOPE",)
    assert arbitration.ignored_semantic_candidate_count == 1


def test_mock_semantic_mutation_is_advisory_and_backend_task_state_is_authoritative(
    deterministic_client, monkeypatch,
):
    headers, _ = _new_catalog(deterministic_client, "task-state-provider-authority")
    created = ask(deterministic_client, headers, "找5个净利率大于10%的商品")
    provider_frame = frame("product_filter", ["filters", "profit"], ["filter_products"])
    provider_frame.update({
        "operation": "UPDATE_CONSTRAINT",
        "filter_updates": {"min_margin_rate": 0.99},
        "constraints": {"min_margin_rate": 0.99},
    })
    qwen = MockProvider("qwen", [provider_frame])
    from app.agent.model_router import ModelRouter
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))

    result = ask(
        deterministic_client, headers, "把净利率门槛改成20%，其他条件保持不变",
        session_id=created["session_id"], provider_execution_policy="QWEN_ONLY",
    )

    assert len(qwen.calls) == 1
    assert qwen.calls[0][2]["task_context"]["active_task_type"] == "product_filter"
    assert result["task_state"]["operation"] == "UPDATE_CONSTRAINT"
    assert result["filter_criteria"]["min_margin_rate"] == pytest.approx(0.20)
    assert result["filter_criteria"]["min_margin_rate"] != pytest.approx(0.99)
    assert result["active_provider"] == "qwen"


def test_empty_session_recommendation_planner_discovers_candidates_without_final_decision(
    deterministic_client, monkeypatch,
):
    headers, _ = _new_catalog(deterministic_client, "task-state-recommendation")
    provider_frame = frame(
        "selection_recommendation",
        ["profit", "risk", "compliance", "recommendation", "decision"],
        ["filter_products"],
    )
    qwen = MockProvider("qwen", [provider_frame])
    from app.agent.model_router import ModelRouter
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))

    result = ask(
        deterministic_client, headers,
        "挑一个值得继续调查的候选，但最终是否采用仍由审核人决定",
        provider_execution_policy="QWEN_ONLY",
    )

    assert len(qwen.calls) == 1
    assert result["intent"] == "selection_recommendation"
    assert result["response_type"] == "decision_report"
    assert result["tool_call_count"] == 1
    assert [item["tool_name"] for item in result["tool_results"]] == ["filter_products"]
    assert result["products"]
    assert result["human_review_required"] is True, result
    assert all(item["decision_status"] != "RECOMMENDED" or item["compliance_status"] != "rejected" for item in result["products"])


def test_result_set_extreme_and_ranking_explanation_use_backend_order(deterministic_client):
    headers, _ = _new_catalog(deterministic_client, "task-state-result-analysis")
    created = ask(deterministic_client, headers, "筛选净利率高于10%的商品，返回5个")
    session_id = created["session_id"]

    leader = ask(deterministic_client, headers, "这批候选里最赚钱的是谁", session_id=session_id)
    expected = max(created["products"], key=lambda item: (item["net_profit"], item["id"]))
    assert leader["task_state"]["operation"] == "ARGMAX"
    assert leader["product_ids"] == [expected["id"]]
    assert leader["tool_call_count"] == 1
    assert leader["fallback_reason"] is None

    reordered = ask(deterministic_client, headers, "把当前结果按利润从高到低重排", session_id=session_id)
    assert reordered["task_state"]["operation"] == "REORDER"
    assert reordered["task_state"]["ranking_spec"] == ["net_profit:desc"]
    assert [item["net_profit"] for item in reordered["products"]] == sorted(
        [item["net_profit"] for item in reordered["products"]], reverse=True,
    )

    multi_rank = ask(deterministic_client, headers, "改成风险优先、利润其次", session_id=session_id)
    assert multi_rank["task_state"]["ranking_spec"] == ["risk:asc", "net_profit:desc"]

    explained = ask(deterministic_client, headers, "榜首为何领先第二名", session_id=session_id)
    assert explained["task_state"]["operation"] == "EXPLAIN_RANKING"
    assert explained["product_ids"] == multi_rank["product_ids"][:2]
    assert "第一名" in explained["answer"] and "第二名" in explained["answer"]
    assert "风险" in explained["answer"] and "净利润" in explained["answer"]


def test_calculation_followups_recompute_one_consistent_backend_evidence(deterministic_client, monkeypatch):
    headers, items = _new_catalog(deterministic_client, "task-state-calculation-continuity")
    product = by_title(items, "桌面理线器")
    roi_frame = frame("product_detail", ["profit", "roi"], ["get_product", "calculate_profit"], context=True)
    # Providers may scope an ROI explanation without repeating the broader
    # profit dimension; the backend must still run the same calculator.
    calculation_frame = frame("calculation_explanation", ["roi", "calculation", "evidence"], ["get_product", "calculate_profit"], context=True)
    cost_frame = frame("calculation_explanation", ["profit", "roi", "calculation", "evidence"], ["get_product", "calculate_profit"], context=True)
    cost_frame["metric"] = "cost_breakdown"
    qwen = MockProvider("qwen", [roi_frame, calculation_frame, cost_frame])
    from app.agent.model_router import ModelRouter
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))

    profit = ask(deterministic_client, headers, f"{product['title']}利润怎么样", provider_execution_policy="QWEN_ONLY")
    roi = ask(deterministic_client, headers, "那ROI呢", session_id=profit["session_id"], provider_execution_policy="QWEN_ONLY")
    explanation = ask(deterministic_client, headers, "刚才这个ROI如何计算", session_id=profit["session_id"], provider_execution_policy="QWEN_ONLY")
    costs = ask(deterministic_client, headers, "主要成本中金额前三项有哪些", session_id=profit["session_id"], provider_execution_policy="QWEN_ONLY")

    assert roi["product_ids"] == [product["id"]]
    assert explanation["response_type"] == "calculation_explanation"
    assert f"售价 {product['current_price']:.2f} RUB" in explanation["answer"]
    assert "售价 0.00 RUB" not in explanation["answer"]
    assert f"净利润 {explanation['products'][0]['net_profit']:.2f} RUB" in explanation["answer"]
    assert "ROI 计算" in explanation["answer"]
    assert costs["response_type"] == "calculation_explanation"
    assert costs["understanding"]["metric"] == "cost_breakdown"
    assert all(f"{index}. " in costs["answer"] for index in (1, 2, 3))
    assert "4. " not in costs["answer"]


def test_recommendation_state_binds_followups_and_remains_session_scoped(deterministic_client, monkeypatch):
    headers, items = _new_catalog(deterministic_client, "task-state-recommendation-followups")
    recommendation_frame = frame(
        "selection_recommendation",
        ["profit", "risk", "compliance", "recommendation", "decision"],
        ["filter_products"],
    )
    missing_context_frame = frame("decision_explanation", ["decision_reason", "evidence"], ["get_product"], context=True)
    explicit = by_title(items, "智能温湿度计")
    explicit_frame = frame(
        "decision_explanation", ["decision_reason", "evidence"], ["get_product"],
        mentions=[explicit["title"]], context=False,
    )
    qwen = MockProvider("qwen", [recommendation_frame, missing_context_frame, explicit_frame])
    from app.agent.model_router import ModelRouter
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))

    recommendation = ask(
        deterministic_client, headers, "选择一个适合继续研究的候选，最终决定交给人工",
        provider_execution_policy="QWEN_ONLY",
    )
    state = recommendation["task_state"]["active_recommendation"]
    assert recommendation["response_type"] == "decision_report"
    assert state["selected_product_id"] == recommendation["product_ids"][0]
    assert state["ordered_candidates"] == recommendation["product_ids"]

    why = ask(deterministic_client, headers, "推荐它的依据是什么", session_id=recommendation["session_id"], provider_execution_policy="QWEN_ONLY")
    provenance = ask(deterministic_client, headers, "这些推荐证据的来源呢", session_id=recommendation["session_id"], provider_execution_policy="QWEN_ONLY")
    freshness = ask(deterministic_client, headers, "如果关键资料已经过期还会推荐吗", session_id=recommendation["session_id"], provider_execution_policy="QWEN_ONLY")

    assert why["product_ids"] == [state["selected_product_id"]]
    assert why["response_type"] == "decision_explanation"
    assert "推荐度" in why["answer"] and "人工决策" in why["answer"]
    assert provenance["product_ids"] == [state["selected_product_id"]]
    assert provenance["response_type"] == "provenance_fact"
    assert "推荐证据来源" in provenance["answer"]
    assert freshness["product_ids"] == [state["selected_product_id"]]
    assert freshness["response_type"] == "decision_explanation"
    assert "维持正式推荐" in freshness["answer"] or "最终动作继续由人工审核" in freshness["answer"]

    new_session_id = deterministic_client.post("/api/v1/agent/sessions", headers=headers, json={}).json()["data"]["id"]
    isolated = ask(deterministic_client, headers, "为何会推荐它", session_id=new_session_id, provider_execution_policy="QWEN_ONLY")
    assert isolated["response_type"] == "clarification"
    assert isolated["product_ids"] == []
    assert isolated["tool_call_count"] == 0

    overridden = ask(
        deterministic_client, headers, f"{explicit['title']}为什么没有通过推荐门禁",
        session_id=recommendation["session_id"], provider_execution_policy="QWEN_ONLY",
    )
    assert overridden["product_ids"] == [explicit["id"]]
    assert overridden["context_source"] == "explicit_query"


def test_recommendation_discovery_does_not_require_existing_product_context(
    deterministic_client,
):
    headers, _ = _new_catalog(deterministic_client, "recommendation-discovery-no-context")

    result = ask(deterministic_client, headers, "从公司商品库里推荐一个最值得测试的商品")

    assert result["intent"] == "selection_recommendation"
    assert result["response_type"] == "decision_report"
    assert result["clarification_code"] is None
    assert result["product_ids"]
    assert result["task_state"]["active_recommendation"]["selected_product_id"] == result["product_ids"][0]
