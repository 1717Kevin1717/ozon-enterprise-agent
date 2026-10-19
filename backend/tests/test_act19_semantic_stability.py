"""ACT19: session-scoped recommendation semantics across the public Agent API."""

from copy import deepcopy

import pytest

from app.agent.model_router import ModelRouter
from test_model_router import MockProvider, frame, install_router
from test_stability_sprint import ask, deterministic_client, headers as tenant_headers, seed


def _collection(client, headers):
    created = ask(client, headers, "当前候选里先找三个值得继续看的")
    collection = created["task_state"]["active_collection"]
    assert collection and len(collection["top_product_ids"]) == 3
    return created, deepcopy(collection)


def _ask_with_qwen(client, headers, query, session_id):
    return ask(
        client, headers, query, session_id=session_id,
        provider_execution_policy="QWEN_ONLY", require_semantic_handoff=True,
    )


@pytest.mark.parametrize("query", [
    "为什么推荐这几个商品？",
    "为什么这几个排在前面？",
])
def test_recommendation_reason_reuses_current_collection_despite_provider_filter_label(
    deterministic_client, monkeypatch, query,
):
    headers, _ = seed(deterministic_client, "act19-reason-" + str(abs(hash(query))))
    created, original = _collection(deterministic_client, headers)
    qwen = MockProvider("qwen", [frame("product_filter", ["recommendation", "decision_reason"], ["filter_products"])])
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))

    result = _ask_with_qwen(deterministic_client, headers, query, created["session_id"])

    assert len(qwen.calls) == 1
    assert result["active_provider"] == "qwen"
    assert result["fallback_used"] is False
    assert result["intent"] == "collection_analysis"
    assert result["understanding"]["operation"] == "EXPLAIN_RANKING"
    assert result["understanding"]["question_type"] == "ranking_explanation"
    assert result["tool_call_count"] == 1
    assert {item["tool_name"] for item in result["tool_results"]} == {"analyze_collection"}
    assert result["ranking_analysis"]["winner"]["product_id"] == original["top_product_ids"][0]
    assert result["ranking_analysis"]["runner_up"]["product_id"] == original["top_product_ids"][1]
    assert result["ranking_analysis"]["winner"]["metric_evidence"]
    assert result["task_state"]["active_collection"] == original


def test_explicit_new_filter_keeps_and_semantics_in_existing_collection_session(deterministic_client, monkeypatch):
    headers, _ = seed(deterministic_client, "act19-new-filter")
    created, _ = _collection(deterministic_client, headers)
    qwen = MockProvider("qwen", [frame("product_filter", ["filters", "profit", "risk", "compliance"], ["filter_products"])])
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))

    result = _ask_with_qwen(
        deterministic_client, headers,
        "筛选净利率不低于30%、风险低且合规通过的商品", created["session_id"],
    )

    assert len(qwen.calls) == 1
    assert result["intent"] == "product_filter"
    assert result["fallback_used"] is False
    assert result["matched_count"] == result["total_count"]
    assert result["matched_count"] > 0
    assert {item["tool_name"] for item in result["tool_results"]} == {"filter_products"}
    assert all(
        item["current_margin_rate"] >= 0.3
        and item["risk_level"] == "low"
        and item["compliance_status"] == "approved"
        for item in result["products"]
    )


def test_invalid_provider_dimension_cannot_hide_behind_existing_collection(deterministic_client, monkeypatch):
    headers, _ = seed(deterministic_client, "act19-invalid-dimension")
    created, original = _collection(deterministic_client, headers)
    qwen = MockProvider("qwen", [frame("product_filter", ["unregistered_business_dimension"], ["filter_products"])])
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))

    result = _ask_with_qwen(deterministic_client, headers, "为什么推荐这几个商品？", created["session_id"])

    assert len(qwen.calls) == 1
    assert result["fallback_reason"] == "SEMANTIC_PLAN_VALIDATION_FAILED"
    assert result["provider_calls"][0]["validation_rule_id"] == "SEM004"
    assert result["tool_call_count"] == 0
    assert result["products"] == []
    assert result["task_state"]["active_collection"] == original


def test_missing_collection_does_not_invent_recommendation_evidence(deterministic_client, monkeypatch):
    headers, _ = seed(deterministic_client, "act19-missing-collection")
    qwen = MockProvider("qwen", [frame("product_filter", ["recommendation", "decision_reason"], ["filter_products"])])
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))

    result = ask(
        deterministic_client, headers, "为什么推荐这几个商品？",
        provider_execution_policy="QWEN_ONLY", require_semantic_handoff=True,
    )

    assert len(qwen.calls) == 1
    assert result["response_type"] == "clarification"
    assert result["tool_call_count"] == 0
    assert result["products"] == []
    assert result["task_state"]["active_collection"] is None


def test_empty_catalog_has_no_recommendation_evidence(deterministic_client, monkeypatch):
    headers = tenant_headers("act19-empty-catalog")
    empty = ask(deterministic_client, headers, "当前候选里先找三个值得继续看的")
    assert empty["products"] == []
    qwen = MockProvider("qwen", [frame("product_filter", ["recommendation", "decision_reason"], ["filter_products"])])
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))

    result = _ask_with_qwen(deterministic_client, headers, "为什么推荐这几个商品？", empty["session_id"])

    assert len(qwen.calls) == 1
    assert result["products"] == []
    assert result["ranking_analysis"] == {}
    assert result["tool_call_count"] == 0


def test_provider_failure_does_not_mutate_collection_or_execute_unsupported_filter(deterministic_client, monkeypatch):
    headers, _ = seed(deterministic_client, "act19-provider-failure")
    created, original = _collection(deterministic_client, headers)
    qwen = MockProvider("qwen", failure="NETWORK_TIMEOUT")
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))

    result = _ask_with_qwen(deterministic_client, headers, "为什么推荐这几个商品？", created["session_id"])

    assert len(qwen.calls) == 1
    assert result["fallback_used"] is True
    assert result["fallback_reason"] == "NETWORK_TIMEOUT"
    assert all(item["tool_name"] != "filter_products" for item in result["tool_results"])
    assert result["task_state"]["active_collection"] == original


def test_recommendation_reason_uses_latest_collection_revision_after_rerank(deterministic_client, monkeypatch):
    headers, _ = seed(deterministic_client, "act19-latest-ranking")
    created, previous = _collection(deterministic_client, headers)
    reranked = ask(deterministic_client, headers, "按净利润重新排序当前三个商品", session_id=created["session_id"])
    current = deepcopy(reranked["task_state"]["active_collection"])
    assert current["result_id"] != previous["result_id"]
    qwen = MockProvider("qwen", [frame("product_filter", ["recommendation", "decision_reason"], ["filter_products"])])
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))

    result = _ask_with_qwen(deterministic_client, headers, "为什么这几个排在前面？", created["session_id"])

    assert len(qwen.calls) == 1
    assert result["ranking_analysis"]["source_collection_result_id"] == current["result_id"]
    assert result["ranking_analysis"]["source_collection_revision"] == current["source_task_revision"]
    assert result["ranking_analysis"]["winner"]["product_id"] == current["top_product_ids"][0]
    assert result["task_state"]["active_collection"] == current
