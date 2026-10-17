"""ACT16-18 ranking semantics remain deterministic and collection-safe."""

from copy import deepcopy
from datetime import datetime
import os

import pytest

from app.agent.query_understanding import understand_query
from test_demo_reliability import active_collection_snapshot
from test_stability_sprint import ask, deterministic_client, seed


def _create_collection(client, headers):
    return ask(client, headers, "分析当前候选池最值得继续研究的三个商品")


def test_margin_diagnosis_does_not_become_ranking_explanation():
    _, margin_filter = understand_query(
        "哪些商品净利率没达到目标？", [], (), active_collection_snapshot(),
    )
    assert margin_filter.operation == "ANALYZE_COLLECTION"
    assert margin_filter.collection_filters[0]["field"] == "net_margin"

    _, analysis = understand_query(
        "哪些商品净利率没达到目标？请分析主要成本。",
        [], (), active_collection_snapshot(),
    )
    assert analysis.operation == "ANALYZE_COLLECTION"
    assert "PROFIT_DIAGNOSIS" in analysis.analysis_requests

    _, explanation = understand_query(
        "为什么第一名？", [], (), active_collection_snapshot(),
    )
    assert explanation.operation == "EXPLAIN_RANKING"
    assert explanation.question_type == "ranking_explanation"


def test_explicit_metric_selector_precedes_preference_change():
    _, selector = understand_query(
        "这批候选按 ROI 从高到低取前三", [], (), active_collection_snapshot(),
    )
    assert selector.collection_selector["metric"] == "roi"
    assert selector.collection_selector["limit"] == 3

    _, preference = understand_query(
        "如果更关注利润，排名会变化吗？", [], (), active_collection_snapshot(),
    )
    assert preference.collection_selector is None


def test_ranking_explanation_contains_backend_metrics_and_runner_up_delta(deterministic_client):
    headers, _ = seed(deterministic_client, "ranking-decision-explanation")
    created = _create_collection(deterministic_client, headers)

    result = ask(deterministic_client, headers, "为什么第一名？", session_id=created["session_id"])

    analysis = result["ranking_analysis"]
    assert result["understanding"]["question_type"] == "ranking_explanation"
    assert analysis["winner"]["product_id"] == created["product_ids"][0]
    assert analysis["runner_up"]["product_id"] == created["product_ids"][1]
    assert {"net_profit", "net_margin", "roi"} <= set(analysis["winner"]["metric_evidence"])
    assert {"net_profit", "roi_percentage_points"} <= set(analysis["metric_differences"])
    assert "净利润" in result["answer"] and "ROI" in result["answer"] and "第二名" in result["answer"]


def test_ranking_compare_is_readonly_over_existing_collection(deterministic_client):
    headers, _ = seed(deterministic_client, "ranking-readonly-compare")
    created = _create_collection(deterministic_client, headers)
    before = deepcopy(created["task_state"]["active_collection"])

    result = ask(
        deterministic_client, headers,
        "第一名和第二名相比优势在哪里？", session_id=created["session_id"],
    )

    assert result["understanding"]["question_type"] == "ranking_compare"
    assert result["product_ids"] == before["top_product_ids"][:2]
    assert result["task_state"]["active_collection"] == before
    assert result["ranking_analysis"]["canonical_collection_mutation"] == 0
    assert result["ranking_analysis"]["source_collection_result_id"] == before["result_id"]


def test_metric_preference_change_previews_ranking_without_canonical_mutation(deterministic_client):
    headers, _ = seed(deterministic_client, "ranking-preference-preview")
    created = _create_collection(deterministic_client, headers)
    before = deepcopy(created["task_state"]["active_collection"])

    result = ask(
        deterministic_client, headers,
        "如果我更关注利润，而不是综合推荐度，排名会变化吗？",
        session_id=created["session_id"],
    )

    assert result["response_type"] != "clarification"
    assert result["understanding"]["question_type"] == "ranking_preference_change"
    assert result["requested_dimensions"] == ["profit"]
    assert result["ranking_analysis"]["original_ranking_dimensions"] == before["ranking_dimensions"]
    assert result["ranking_analysis"]["ranking_dimensions"] == ["profit"]
    assert result["ranking_analysis"]["canonical_collection_mutation"] == 0
    assert result["task_state"]["active_collection"] == before
    assert "原排序依据" in result["answer"] and "临时排序依据" in result["answer"]


@pytest.mark.parametrize(
    ("query", "dimension"),
    [
        ("我更关注 ROI，排名会怎么变化？", "roi"),
        ("风险优先时排名会怎样？", "risk"),
        ("我更看重竞争，排名会怎么变化？", "competition"),
    ],
)
def test_metric_preference_dimensions_are_generalized_and_readonly(
    deterministic_client, query, dimension,
):
    headers, _ = seed(deterministic_client, "ranking-preference-" + dimension)
    created = _create_collection(deterministic_client, headers)
    before = deepcopy(created["task_state"]["active_collection"])

    result = ask(deterministic_client, headers, query, session_id=created["session_id"])

    assert result["understanding"]["question_type"] == "ranking_preference_change"
    assert result["requested_dimensions"] == [dimension]
    assert result["task_state"]["active_collection"] == before
    assert result["ranking_analysis"]["canonical_collection_mutation"] == 0


def test_agent_response_exposes_non_sensitive_runtime_identity(deterministic_client):
    headers, _ = seed(deterministic_client, "runtime-identity")

    result = ask(deterministic_client, headers, "目前公司有多少商品？")
    identity = result["runtime_identity"]

    assert identity["git_commit"] != "unknown"
    assert identity["runtime_path"].replace("/", "\\").endswith("OZON AI 选品\\backend")
    assert identity["app_version"] == "0.1.0"
    assert identity["process_id"] == os.getpid()
    assert datetime.fromisoformat(identity["started_at_utc"]).tzinfo is not None
    health = deterministic_client.get("/api/v1/health").json()["data"]
    assert health["runtime_identity"] == identity
