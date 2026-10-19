"""Offline, API-level paraphrase and state-boundary matrix.

These are not live-model evaluations: the business path runs through the public
Agent API with deterministic planning or a controlled provider response.
"""

from copy import deepcopy
import asyncio

import pytest

from app.agent.model_router import ModelRouter
from app.db.models import ConversationSession
from app.db.session import SessionLocal
from test_model_router import MockProvider, frame, install_router
from test_stability_sprint import ask, deterministic_client, headers, seed


def _turn(client, tenant, query, previous=None, **extra):
    if previous is not None:
        extra["session_id"] = previous["session_id"]
    result = ask(client, tenant, query, **extra)
    if previous is not None:
        assert result["session_id"] == previous["session_id"]
    return result


def _start(client, tenant):
    result = _turn(client, tenant, "当前候选里先找三个值得继续看的")
    collection = result["task_state"]["active_collection"]
    assert result["intent"] == "collection_analysis" and len(collection["top_product_ids"]) == 3
    assert any(item["tool_name"] == "analyze_collection" for item in result["tool_results"])
    return result, deepcopy(collection)


def _same_collection(result, baseline):
    current = result["task_state"]["active_collection"]
    for key in ("result_id", "product_ids", "ranked_product_ids", "top_product_ids", "source_task_revision"):
        assert current[key] == baseline[key]


@pytest.mark.parametrize("query", [
    "从现有候选中挑出三个最值得继续验证的方向",
    "现有候选商品哪些更值得进一步研究？先给我三个",
], ids=["candidate-verification", "candidate-research"])
def test_collection_establishment_paraphrases(deterministic_client, query):
    tenant, _ = seed(deterministic_client, "gen-collection-" + str(abs(hash(query))))
    result = _turn(deterministic_client, tenant, query)
    assert result["intent"] == "collection_analysis"
    assert len(result["task_state"]["active_collection"]["top_product_ids"]) == 3
    assert {item["tool_name"] for item in result["tool_results"]} == {"analyze_collection"}


def test_collection_reuse_after_member_focus(deterministic_client):
    tenant, _ = seed(deterministic_client, "gen-focus-reuse")
    created, original = _start(deterministic_client, tenant)
    focused = _turn(deterministic_client, tenant, "第二个还缺哪些证据？", created)
    assert original["top_product_ids"][1] in focused["product_ids"]
    _same_collection(focused, original)
    reused = _turn(deterministic_client, tenant, "这批候选中现在排第一的是谁？", focused)
    assert reused["product_ids"] == original["top_product_ids"][:1]
    _same_collection(reused, original)


def test_new_canonical_collection_replaces_focus_not_old_scenario(deterministic_client):
    tenant, _ = seed(deterministic_client, "gen-collection-switch")
    created, original = _start(deterministic_client, tenant)
    scenario = _turn(deterministic_client, tenant, "假设第一名采购成本降低8%", created)
    assert scenario["simulation"]["operation"] == "CREATE_SCENARIO"
    refreshed = _turn(deterministic_client, tenant, "重新看当前候选，挑三个重点的", scenario)
    current = refreshed["task_state"]["active_collection"]
    assert current["result_id"] != original["result_id"]
    assert refreshed["task_state"]["conversation_focus"]["focus_type"] == "CANONICAL_ORDERED_RESULT"


def test_empty_tenant_cannot_invent_collection(deterministic_client):
    result = _turn(deterministic_client, headers("gen-empty-collection"), "现有候选里哪三个最值得验证？")
    assert result["products"] == []
    assert result["tool_call_count"] <= 1
    assert not result["task_state"]["active_collection"]["product_ids"]


@pytest.mark.parametrize("query", ["排在最前面的那个凭什么领先？", "头名为什么比后面更值得研究？"])
def test_ranking_explanation_paraphrases_are_grounded(deterministic_client, query):
    tenant, _ = seed(deterministic_client, "gen-rank-explain-" + str(abs(hash(query))))
    created, original = _start(deterministic_client, tenant)
    result = _turn(deterministic_client, tenant, query, created)
    assert result["understanding"]["question_type"] == "ranking_explanation"
    analysis = result["ranking_analysis"]
    assert analysis["winner"]["product_id"] == original["top_product_ids"][0]
    assert analysis["runner_up"]["product_id"] == original["top_product_ids"][1]
    assert analysis["winner"]["metric_evidence"] and analysis["metric_differences"]
    assert analysis["canonical_collection_mutation"] == 0
    _same_collection(result, original)


def test_collection_ranking_explanation_keeps_its_tool_policy_after_filter_result(
    deterministic_client, monkeypatch,
):
    tenant, _ = seed(deterministic_client, "gen-rank-after-filter")
    created, original = _start(deterministic_client, tenant)
    filtered = _turn(
        deterministic_client, tenant, "筛选净利率高于10%的商品，返回5个", created,
    )
    assert filtered["task_state"]["active_result_set"]["product_ids"]
    provider = MockProvider(
        "qwen", [frame("collection_analysis", ["recommendation"], ["analyze_collection"])],
    )
    install_router(monkeypatch, ModelRouter([provider], primary_name="qwen", reasoning_name="deepseek"))

    result = _turn(
        deterministic_client, tenant, "榜首为何领先？", filtered,
        provider_execution_policy="QWEN_ONLY", require_semantic_handoff=True,
    )

    assert len(provider.calls) == 1
    assert result["intent"] == "collection_analysis"
    assert result["understanding"]["operation"] == "EXPLAIN_RANKING"
    assert result["tool_call_count"] == 1
    assert [item["tool_name"] for item in result["tool_results"]] == ["analyze_collection"]
    assert result["ranking_analysis"]["winner"]["metric_evidence"]
    _same_collection(result, original)


def test_ranking_explanation_uses_newer_result_order_and_its_profit_basis(
    deterministic_client, monkeypatch,
):
    tenant, _ = seed(deterministic_client, "gen-rank-basis-after-filter")
    created, original = _start(deterministic_client, tenant)
    preview = _turn(
        deterministic_client, tenant,
        "如果更看重利润，前三名会变吗？", created,
    )
    assert preview["understanding"]["question_type"] == "ranking_preference_change"
    filtered = _turn(
        deterministic_client, tenant,
        "筛选净利率高于10%的商品，返回5个", preview,
    )
    result_set = deepcopy(filtered["task_state"]["active_result_set"])
    assert result_set["sort_spec"] == ["margin_rate", "desc"]
    assert len(result_set["product_ids"]) >= 2
    provider = MockProvider(
        "qwen", [frame("collection_analysis", ["recommendation"], ["analyze_collection"])],
    )
    install_router(monkeypatch, ModelRouter([provider], primary_name="qwen", reasoning_name="deepseek"))

    result = _turn(
        deterministic_client, tenant, "榜首为何领先？", filtered,
        provider_execution_policy="QWEN_ONLY", require_semantic_handoff=True,
    )

    assert len(provider.calls) == 1
    assert [item["tool_name"] for item in result["tool_results"]] == ["analyze_collection"]
    assert result["ranking_analysis"]["ranking_dimensions"] == ["profit"]
    assert result["ranking_analysis"]["winner"]["product_id"] == result_set["product_ids"][0]
    assert result["ranking_analysis"]["runner_up"]["product_id"] == result_set["product_ids"][1]
    assert "当前按profit排序" in result["answer"]
    assert result["task_state"]["active_result_set"]["product_ids"] == result_set["product_ids"]
    _same_collection(result, original)


@pytest.mark.parametrize("preference,dimension", [
    (None, "recommendation"),
    ("如果更看重利润，前三名会变吗？", "profit"),
    ("把回报率放在首位看，前三会换人吗？", "roi"),
])
def test_ranking_explanation_uses_current_order_and_backend_basis(
    deterministic_client, monkeypatch, preference, dimension,
):
    tenant, _ = seed(deterministic_client, "gen-ranking-basis-" + dimension)
    created, original = _start(deterministic_client, tenant)
    current = _turn(deterministic_client, tenant, preference, created) if preference else created
    if preference:
        ordered = current["task_state"]["active_result_set"]
        assert ordered["source_operation"] == "PREVIEW_RANKING"
        assert ordered["ranking_dimensions"] == [dimension]
        assert current["understanding"]["top_k"] == 3
        expected_ids = ordered["product_ids"]
        assert len(expected_ids) >= 2, current["understanding"]
        _same_collection(current, original)
    else:
        expected_ids = original["top_product_ids"]
    provider = MockProvider(
        "qwen", [frame("collection_analysis", ["recommendation"], ["analyze_collection"])],
    )
    install_router(monkeypatch, ModelRouter([provider], primary_name="qwen", reasoning_name="deepseek"))

    result = _turn(
        deterministic_client, tenant, "榜首为何领先？", current,
        provider_execution_policy="QWEN_ONLY", require_semantic_handoff=True,
    )

    assert len(provider.calls) == 1
    assert result["understanding"]["operation"] == "EXPLAIN_RANKING"
    assert [item["tool_name"] for item in result["tool_results"]] == ["analyze_collection"]
    analysis = result["ranking_analysis"]
    assert analysis["ranking_dimensions"] == [dimension]
    assert analysis["winner"]["product_id"] == expected_ids[0]
    assert analysis["runner_up"]["product_id"] == expected_ids[1]
    assert analysis["winner"]["metric_evidence"] and analysis["metric_differences"]
    assert f"当前按{dimension}排序" in result["answer"]
    assert analysis["winner"]["title"] in result["answer"]
    assert analysis["runner_up"]["title"] in result["answer"]
    assert analysis["canonical_collection_mutation"] == 0
    _same_collection(result, original)


def test_explanation_after_explicit_rerank_uses_new_canonical_order(deterministic_client):
    tenant, _ = seed(deterministic_client, "gen-ranking-after-explicit-rerank")
    created, _ = _start(deterministic_client, tenant)
    reranked = _turn(deterministic_client, tenant, "把当前三个按净利润从高到低重排", created)
    current = deepcopy(reranked["task_state"]["active_collection"])
    assert current["ranking_dimensions"] == ["profit"]
    result = _turn(deterministic_client, tenant, "榜首为何领先？", reranked)
    assert result["ranking_analysis"]["ranking_dimensions"] == ["profit"]
    assert result["ranking_analysis"]["winner"]["product_id"] == current["top_product_ids"][0]
    assert result["ranking_analysis"]["runner_up"]["product_id"] == current["top_product_ids"][1]
    assert "当前按profit排序" in result["answer"]
    _same_collection(result, current)


def test_recommendation_policy_after_temporary_ranking_needs_no_product_entity(deterministic_client):
    tenant, _ = seed(deterministic_client, "gen-policy-after-ranking-preview")
    created, original = _start(deterministic_client, tenant)
    preview = _turn(deterministic_client, tenant, "如果更看重利润，前三名会变吗？", created)
    result = _turn(deterministic_client, tenant, "利润最高就能直接推荐上架吗？", preview)
    assert result["intent"] == "recommendation_policy"
    assert result["tool_call_count"] == 0
    assert result["response_type"] == "policy_answer"
    assert result["fallback_reason"] is None
    _same_collection(result, original)


@pytest.mark.parametrize("missing_source", ["collection", "newer_result_set"])
def test_missing_current_ranking_basis_clarifies_without_guessing_or_tools(
    deterministic_client, missing_source,
):
    tenant, _ = seed(deterministic_client, "gen-ranking-missing-" + missing_source)
    created, original = _start(deterministic_client, tenant)
    current = (
        _turn(deterministic_client, tenant, "筛选净利率高于10%的商品，返回5个", created)
        if missing_source == "newer_result_set" else created
    )

    async def erase_basis():
        async with SessionLocal() as session:
            row = await session.get(ConversationSession, current["session_id"])
            state = deepcopy(row.state_json)
            target = state["task_state"]["active_collection" if missing_source == "collection" else "active_result_set"]
            target["ranking_dimensions"] = []
            target["ranking_spec" if missing_source == "collection" else "sort_spec"] = []
            row.state_json = state
            await session.commit()

    asyncio.run(erase_basis())
    result = _turn(deterministic_client, tenant, "榜首为何领先？", current)
    assert result["response_type"] == "clarification"
    assert result["clarification_code"] == "MISSING_RANKING_BASIS"
    assert result["tool_call_count"] == 0
    assert "缺少可验证的排序依据" in result["answer"]
    assert "当前按recommendation排序" not in result["answer"]
    _same_collection(result, original)


def test_ranking_comparison_does_not_shrink_collection(deterministic_client):
    tenant, _ = seed(deterministic_client, "gen-rank-compare")
    created, original = _start(deterministic_client, tenant)
    result = _turn(deterministic_client, tenant, "头两名各有什么优势？", created)
    assert result["understanding"]["question_type"] == "ranking_compare"
    assert result["product_ids"] == original["top_product_ids"][:2]
    assert result["tool_call_count"] >= 1
    _same_collection(result, original)


def test_research_reason_followup_reuses_existing_collection(deterministic_client):
    tenant, _ = seed(deterministic_client, "gen-research-reason")
    created, original = _start(deterministic_client, tenant)
    result = _turn(deterministic_client, tenant, "为什么这几个值得继续研究？", created)
    assert result["intent"] == "collection_analysis"
    assert result["understanding"]["operation"] == "EXPLAIN_RANKING"
    assert result["ranking_analysis"]["winner"]["product_id"] == original["top_product_ids"][0]
    _same_collection(result, original)


@pytest.mark.parametrize("query,dimension", [
    ("把回报率放在首位看，前三会换人吗？", "roi"),
    ("若先规避风险，排序会怎样变化？", "risk"),
])
def test_metric_preference_is_temporary(deterministic_client, query, dimension):
    tenant, _ = seed(deterministic_client, "gen-pref-" + dimension)
    created, original = _start(deterministic_client, tenant)
    result = _turn(deterministic_client, tenant, query, created)
    assert result["understanding"]["question_type"] == "ranking_preference_change"
    assert dimension in result["requested_dimensions"]
    assert result["ranking_analysis"]["canonical_collection_mutation"] == 0
    _same_collection(result, original)


def test_explicit_rerank_updates_revision_without_changing_members(deterministic_client):
    tenant, _ = seed(deterministic_client, "gen-rerank")
    created, original = _start(deterministic_client, tenant)
    result = _turn(deterministic_client, tenant, "把当前三个按净利润从高到低重排", created)
    current = result["task_state"]["active_collection"]
    assert result["intent"] == "collection_analysis"
    assert current["result_id"] != original["result_id"]
    assert set(current["top_product_ids"]) == set(original["top_product_ids"])
    assert "profit" in result["requested_dimensions"]


@pytest.mark.parametrize("query", [
    "只保留净利率至少30%、低风险并且合规通过的商品",
    "找合规已通过且风险低、净利率达到30%的候选",
])
def test_filter_and_paraphrases(deterministic_client, query):
    tenant, _ = seed(deterministic_client, "gen-filter-" + str(abs(hash(query))))
    result = _turn(deterministic_client, tenant, query)
    assert result["intent"] == "product_filter"
    assert {item["tool_name"] for item in result["tool_results"]} == {"filter_products"}
    assert result["matched_count"] == len(result["products"])
    assert all(item["current_margin_rate"] >= .30 and item["risk_level"] == "low"
               and item["compliance_status"] == "approved" for item in result["products"])


def test_filter_no_result_does_not_relax_conditions(deterministic_client):
    tenant, _ = seed(deterministic_client, "gen-filter-zero")
    result = _turn(deterministic_client, tenant, "筛出净利率至少99%且风险低的商品")
    assert result["intent"] == "product_filter"
    assert result["matched_count"] == 0 and result["products"] == []
    assert {item["tool_name"] for item in result["tool_results"]} == {"filter_products"}


def test_filter_without_criteria_does_not_scan_all_as_match(deterministic_client):
    tenant, _ = seed(deterministic_client, "gen-filter-missing")
    result = _turn(deterministic_client, tenant, "请按我的条件筛一下商品")
    assert result["matched_count"] == 0
    assert result["products"] == []
    assert all(item["tool_name"] != "filter_products" for item in result["tool_results"])


def test_unsupported_filter_cannot_silently_drop_its_condition(deterministic_client):
    tenant, _ = seed(deterministic_client, "gen-filter-unsupported")
    result = _turn(deterministic_client, tenant, "筛选价格低于100元且风险低的商品")
    assert result["response_type"] in {"clarification", "insufficient_data", "error"}
    assert result["products"] == []
    assert all(item["tool_name"] != "filter_products" for item in result["tool_results"])


def test_unsupported_price_filter_cannot_be_rescued_by_provider_label(deterministic_client, monkeypatch):
    tenant, _ = seed(deterministic_client, "gen-price-provider-boundary")
    provider = MockProvider("qwen", [frame("product_filter", ["filters", "risk"], ["filter_products"])])
    install_router(monkeypatch, ModelRouter([provider], primary_name="qwen", reasoning_name="deepseek"))
    result = _turn(
        deterministic_client, tenant, "找出售价低于100元且风险低的商品",
        provider_execution_policy="QWEN_ONLY", require_semantic_handoff=True,
    )
    assert result["response_type"] == "clarification"
    assert result["products"] == [] and result["tool_call_count"] == 0
    assert len(provider.calls) == 1  # Explicit semantic handoff may call a mock, never its proposed tool.


def test_price_hypothesis_is_not_an_unsupported_filter(deterministic_client):
    tenant, products = seed(deterministic_client, "gen-price-hypothesis")
    target = next(item for item in products if "桌面理线器" in item["title"])
    result = _turn(deterministic_client, tenant, "假设桌面理线器售价降低10%，利润会怎样？")
    assert result["intent"] == "scenario_analysis"
    assert result["simulation"]["operation"] == "CREATE_SCENARIO"
    assert result["product_ids"] == [target["id"]]


@pytest.mark.parametrize("query", [
    "假设第一名的采购成本减少8%会怎样？",
    "如果头名采购成本下降8%，算一下新结果",
])
def test_scenario_create_paraphrases_preserve_canonical(deterministic_client, query):
    tenant, _ = seed(deterministic_client, "gen-scenario-" + str(abs(hash(query))))
    created, original = _start(deterministic_client, tenant)
    result = _turn(deterministic_client, tenant, query, created)
    assert result["intent"] == "scenario_analysis"
    assert result["simulation"] is not None, (result["intent"], result["response_type"], result["understanding"], result["answer"])
    assert result["simulation"]["operation"] == "CREATE_SCENARIO"
    assert result["simulation"]["scenario_id"]
    assert result["simulation"]["scenario_id"]
    _same_collection(result, original)


def test_scenario_update_then_followup_keeps_branch(deterministic_client):
    tenant, _ = seed(deterministic_client, "gen-scenario-update")
    created, original = _start(deterministic_client, tenant)
    scenario = _turn(deterministic_client, tenant, "假设第一名采购成本降低8%", created)
    updated = _turn(deterministic_client, tenant, "再把广告成本降低5%", scenario)
    assert updated["simulation"]["operation"] in {"UPDATE_SCENARIO", "CREATE_SCENARIO"}
    assert updated["simulation"]["scenario_revision"] > scenario["simulation"]["scenario_revision"]
    followup = _turn(deterministic_client, tenant, "第一名的 ROI 变成多少？", updated)
    assert followup["intent"] == "scenario_analysis"
    assert followup["simulation"]["operation"] == "EXPLAIN_SCENARIO"
    _same_collection(followup, original)


def test_scenario_undo_restores_canonical_focus(deterministic_client):
    tenant, _ = seed(deterministic_client, "gen-scenario-undo")
    created, original = _start(deterministic_client, tenant)
    scenario = _turn(deterministic_client, tenant, "假设第一名采购成本降低8%", created)
    undone = _turn(deterministic_client, tenant, "撤销刚才的调整", scenario)
    assert undone["simulation"]["operation"] in {"RESET_SCENARIO", "REMOVE_OVERRIDE"}
    assert undone["task_state"]["conversation_focus"]["focus_type"] == "CANONICAL_ORDERED_RESULT"
    _same_collection(undone, original)


def test_ambiguous_scenario_cost_does_not_invent_field(deterministic_client):
    tenant, _ = seed(deterministic_client, "gen-scenario-ambiguous")
    created, original = _start(deterministic_client, tenant)
    result = _turn(deterministic_client, tenant, "假如第一名成本降一些", created)
    assert result["response_type"] in {"clarification", "insufficient_data"}
    assert result["simulation"] is None
    _same_collection(result, original)


@pytest.mark.parametrize("query,position", [("头名是哪款？", 0), ("排第二的商品呢？", 1)])
def test_ordinal_paraphrases_resolve_current_ranking(deterministic_client, query, position):
    tenant, _ = seed(deterministic_client, "gen-ordinal-" + str(position))
    created, original = _start(deterministic_client, tenant)
    result = _turn(deterministic_client, tenant, query, created)
    assert result["product_ids"] == original["top_product_ids"][position:position + 1]
    assert result["tool_call_count"] >= 1
    _same_collection(result, original)


def test_range_reference_stays_with_current_collection(deterministic_client):
    tenant, _ = seed(deterministic_client, "gen-range")
    created, original = _start(deterministic_client, tenant)
    result = _turn(deterministic_client, tenant, "前两名放在一起看看", created)
    assert result["product_ids"] == original["top_product_ids"][:2]
    assert result["intent"] != "product_filter"
    _same_collection(result, original)


def test_scenario_ordinal_does_not_take_canonical_rank(deterministic_client):
    tenant, _ = seed(deterministic_client, "gen-scenario-ordinal")
    created, original = _start(deterministic_client, tenant)
    scenario = _turn(deterministic_client, tenant, "假设第一名采购成本降低8%", created)
    followup = _turn(deterministic_client, tenant, "情景里排第一的是谁？", scenario)
    assert followup["intent"] == "scenario_analysis"
    assert followup["simulation"]["operation"] == "EXPLAIN_SCENARIO"
    assert followup["product_ids"][0] == scenario["simulation"]["scenario_ordered_ids"][0]
    _same_collection(followup, original)


def test_new_session_does_not_inherit_collection_or_scenario(deterministic_client):
    tenant, _ = seed(deterministic_client, "gen-session-isolation")
    created, _ = _start(deterministic_client, tenant)
    scenario = _turn(deterministic_client, tenant, "假设第一名采购成本降低8%", created)
    new_id = deterministic_client.post("/api/v1/agent/sessions", headers=tenant, json={}).json()["data"]["id"]
    result = ask(deterministic_client, tenant, "第二名呢？", session_id=new_id)
    assert result["response_type"] == "clarification" and result["tool_call_count"] == 0
    assert result["task_state"]["active_collection"] is None
    assert result["simulation"] is None
    assert scenario["session_id"] != new_id


@pytest.mark.parametrize("query", [
    "候选集里排第一就等于可以直接上架吗？",
    "评分很高但合规没过，可以直接卖吗？",
])
def test_policy_is_not_collection_ranking(deterministic_client, query):
    tenant, _ = seed(deterministic_client, "gen-policy-" + str(abs(hash(query))))
    created, original = _start(deterministic_client, tenant)
    result = _turn(deterministic_client, tenant, query, created)
    assert result["intent"] in {"recommendation_policy", "compliance_policy"}
    assert result["tool_call_count"] == 0
    assert result["products"] == []
    _same_collection(result, original)


@pytest.mark.parametrize("failure", ["NETWORK_TIMEOUT", "CONNECT_ERROR"])
@pytest.mark.parametrize("query", ["为什么推荐这几个商品？", "为什么这几个值得继续研究？"])
def test_provider_transport_failure_preserves_state(deterministic_client, monkeypatch, failure, query):
    tenant, _ = seed(deterministic_client, "gen-transport-" + failure + str(abs(hash(query))))
    created, original = _start(deterministic_client, tenant)
    provider = MockProvider("qwen", failure=failure)
    install_router(monkeypatch, ModelRouter([provider], primary_name="qwen", reasoning_name="deepseek"))
    result = _turn(deterministic_client, tenant, query, created,
                   provider_execution_policy="QWEN_ONLY", require_semantic_handoff=True)
    assert len(provider.calls) == 1
    assert result["fallback_used"] is True and result["fallback_reason"] == failure
    assert all(item["tool_name"] != "filter_products" for item in result["tool_results"])
    _same_collection(result, original)


@pytest.mark.parametrize("query", ["为什么推荐这几个商品？", "为什么这几个值得继续研究？"])
def test_illegal_sem004_plan_executes_no_tool(deterministic_client, monkeypatch, query):
    tenant, _ = seed(deterministic_client, "gen-sem004-" + str(abs(hash(query))))
    created, original = _start(deterministic_client, tenant)
    provider = MockProvider("qwen", [frame("product_filter", ["invalid_market_signal"], ["filter_products"])])
    install_router(monkeypatch, ModelRouter([provider], primary_name="qwen", reasoning_name="deepseek"))
    result = _turn(deterministic_client, tenant, query, created,
                   provider_execution_policy="QWEN_ONLY", require_semantic_handoff=True)
    assert result["fallback_reason"] == "SEMANTIC_PLAN_VALIDATION_FAILED"
    assert result["provider_calls"][0]["validation_rule_id"] == "SEM004"
    assert result["tool_call_count"] == 0 and result["products"] == []
    _same_collection(result, original)


def test_invalid_provider_structure_is_not_business_fact(deterministic_client, monkeypatch):
    tenant, _ = seed(deterministic_client, "gen-invalid-structure")
    created, original = _start(deterministic_client, tenant)
    provider = MockProvider("qwen", [{"not_a_semantic_frame": True}])
    install_router(monkeypatch, ModelRouter([provider], primary_name="qwen", reasoning_name="deepseek"))
    result = _turn(deterministic_client, tenant, "为什么推荐这几个商品？", created,
                   provider_execution_policy="QWEN_ONLY", require_semantic_handoff=True)
    assert result["fallback_used"] is True
    assert result["fallback_reason"] in {"SCHEMA_VALIDATION_FAILED", "INVALID_RESPONSE"}
    assert all(item["tool_name"] != "filter_products" for item in result["tool_results"])
    _same_collection(result, original)


def test_recovery_after_clarification_uses_new_explicit_entity(deterministic_client):
    tenant, products = seed(deterministic_client, "gen-recover-clarification")
    missing = _turn(deterministic_client, tenant, "它的净利率是多少？")
    assert missing["response_type"] == "clarification" and missing["tool_call_count"] == 0
    recovered = _turn(deterministic_client, tenant, "桌面理线器的净利率是多少？", missing)
    target = next(item for item in products if "桌面理线器" in item["title"])
    assert recovered["product_ids"] == [target["id"]]
    assert any(item["tool_name"] == "calculate_profit" for item in recovered["tool_results"])


def test_recovery_after_sem004_rejects_old_frame_but_accepts_new_task(deterministic_client, monkeypatch):
    tenant, _ = seed(deterministic_client, "gen-recover-sem004")
    created, original = _start(deterministic_client, tenant)
    provider = MockProvider("qwen", [frame("product_filter", ["invalid_market_signal"], ["filter_products"])])
    install_router(monkeypatch, ModelRouter([provider], primary_name="qwen", reasoning_name="deepseek"))
    rejected = _turn(deterministic_client, tenant, "为什么推荐这几个商品？", created,
                     provider_execution_policy="QWEN_ONLY", require_semantic_handoff=True)
    assert rejected["fallback_reason"] == "SEMANTIC_PLAN_VALIDATION_FAILED"
    next_result = _turn(deterministic_client, tenant, "这批候选中第一名是谁？", rejected)
    assert next_result["product_ids"] == original["top_product_ids"][:1]
    _same_collection(next_result, original)
