"""ACT16/17/18 manual conversations replayed through the public Agent API.

The queries come from the 10.13 ACT documents. Each journey retains one
session; assertions use backend-owned IDs and state, not demo answer strings.
"""

from copy import deepcopy

import pytest

from test_stability_sprint import ask, deterministic_client, seed


def _turn(client, headers, query, previous=None):
    result = ask(client, headers, query, **({"session_id": previous["session_id"]} if previous else {}))
    if previous:
        assert result["session_id"] == previous["session_id"]
    assert result["fallback_reason"] not in {"INVALID_RESPONSE", "SEMANTIC_PLAN_VALIDATION_FAILED"}, query
    return result


def _collection(result):
    collection = result["task_state"]["active_collection"]
    assert collection and len(collection["top_product_ids"]) >= 3
    return collection


def _unchanged_collection(result, original):
    current = _collection(result)
    for key in ("product_ids", "ranked_product_ids", "top_product_ids", "source_task_revision", "result_id"):
        assert current[key] == original[key], key


def _explanation(result, collection):
    analysis = result["ranking_analysis"]
    assert result["understanding"]["question_type"] == "ranking_explanation"
    assert analysis["winner"]["product_id"] == collection["top_product_ids"][0]
    assert analysis["runner_up"]["product_id"] == collection["top_product_ids"][1]
    assert analysis["winner"]["metric_evidence"]
    assert analysis["metric_differences"]
    assert "第二名" in result["answer"]
    assert analysis["canonical_collection_mutation"] == 0


@pytest.mark.parametrize("query", [
    "最终建议我优先研究哪个？为什么？",
    "为什么推荐这几个商品？",
    "为什么这几个排在前面？",
])
def test_active_collection_decision_reason_uses_existing_ranking(deterministic_client, query):
    headers, _ = seed(deterministic_client, "act-collection-decision-followup")
    first = _turn(deterministic_client, headers, "当前候选里先找三个值得继续看的")
    original = deepcopy(_collection(first))

    followup = _turn(deterministic_client, headers, query, first)

    assert followup["intent"] == "collection_analysis"
    assert followup["understanding"]["operation"] == "EXPLAIN_RANKING"
    assert followup["product_ids"][0] == original["top_product_ids"][0]
    assert followup["ranking_analysis"]["runner_up"]["product_id"] == original["top_product_ids"][1]
    assert followup["ranking_analysis"]["metric_differences"]
    assert followup["total_count"] != 60
    _unchanged_collection(followup, original)


def test_act16_complete_documented_conversation(deterministic_client):
    headers, _ = seed(deterministic_client, "act16-api-journey")
    q1 = _turn(deterministic_client, headers, "当前候选里先找三个值得继续看的")
    original = deepcopy(_collection(q1))
    assert q1["intent"] == "collection_analysis"
    assert len(q1["product_ids"]) == 3

    q2 = _turn(deterministic_client, headers, "第一名是谁？", q1)
    assert q2["product_ids"] == original["top_product_ids"][:1]
    _unchanged_collection(q2, original)
    q3 = _turn(deterministic_client, headers, "它为什么排第一？", q2)
    _explanation(q3, original)
    _unchanged_collection(q3, original)
    q4 = _turn(deterministic_client, headers, "第二个呢？", q3)
    assert q4["product_ids"] == original["top_product_ids"][1:2]
    q5 = _turn(deterministic_client, headers, "前两个比一下", q4)
    assert q5["product_ids"] == original["top_product_ids"][:2]
    _unchanged_collection(q5, original)

    q6 = _turn(deterministic_client, headers, "如果只看利润呢？", q5)
    assert q6["intent"] == "collection_analysis"
    assert q6["response_type"] != "clarification"
    assert q6["total_count"] != 60
    q6_collection = deepcopy(_collection(q6))
    q7 = _turn(deterministic_client, headers, "现在第一名是谁？", q6)
    assert q7["product_ids"] == q6_collection["top_product_ids"][:1]

    q8 = _turn(deterministic_client, headers, "假设第一名采购成本降低8%", q7)
    assert q8["intent"] == "scenario_analysis"
    assert q8["simulation"]["operation"] == "CREATE_SCENARIO"
    assert q8["task_state"]["conversation_focus"]["focus_type"] == "SCENARIO_RESULT"
    _unchanged_collection(q8, q6_collection)
    q9 = _turn(deterministic_client, headers, "和原来比怎么样？", q8)
    assert q9["simulation"]["operation"] == "COMPARE_SCENARIO"
    q10 = _turn(deterministic_client, headers, "撤销刚才的调整", q9)
    assert q10["simulation"]["operation"] in {"RESET_SCENARIO", "REMOVE_OVERRIDE"}
    assert q10["task_state"]["conversation_focus"]["focus_type"] == "CANONICAL_ORDERED_RESULT"
    q11 = _turn(deterministic_client, headers, "现在第一名是谁？", q10)
    assert q11["product_ids"] == q6_collection["top_product_ids"][:1]
    q12 = _turn(deterministic_client, headers, "这三个商品里面哪个风险最低？", q11)
    assert q12["response_type"] != "clarification"
    assert q12["total_count"] != 60
    q13 = _turn(deterministic_client, headers, "如果我是新卖家，你建议先看哪个？", q12)
    # ACT16 records this question but no observed A. Do not invent a Golden
    # answer; require a safe, non-technical outcome and preserved collection.
    assert q13["fallback_reason"] is None
    _unchanged_collection(q13, q6_collection)
    q14 = _turn(deterministic_client, headers, "有没有数据不足的商品？", q13)
    assert q14["response_type"] != "clarification"
    q15 = _turn(deterministic_client, headers, "为什么不是利润最高就直接推荐？", q14)
    assert q15["response_type"] != "clarification"


def test_act17_complete_documented_conversation(deterministic_client):
    headers, _ = seed(deterministic_client, "act17-api-journey")
    q1 = _turn(deterministic_client, headers, "当前候选里先找三个值得继续看的")
    initial = deepcopy(_collection(q1))
    q2 = _turn(deterministic_client, headers, "第一名是谁？", q1)
    assert q2["product_ids"] == initial["top_product_ids"][:1]
    q3 = _turn(deterministic_client, headers, "为什么它排第一？", q2)
    _explanation(q3, initial)
    q4 = _turn(deterministic_client, headers, "第二个呢？", q3)
    assert q4["product_ids"] == initial["top_product_ids"][1:2]
    q5 = _turn(deterministic_client, headers, "前两个对比一下", q4)
    assert q5["product_ids"] == initial["top_product_ids"][:2]
    _unchanged_collection(q5, initial)
    q6 = _turn(deterministic_client, headers, "如果我更关注利润，重新排一下", q5)
    assert q6["response_type"] != "clarification"
    assert "profit" in q6["requested_dimensions"]
    ranked = deepcopy(_collection(q6))
    q7 = _turn(deterministic_client, headers, "现在第一名是谁？", q6)
    assert q7["product_ids"] == ranked["top_product_ids"][:1]
    q8 = _turn(deterministic_client, headers, "为什么它现在排第一？", q7)
    _explanation(q8, ranked)
    q9 = _turn(deterministic_client, headers, "假设第一名采购成本降低8%", q8)
    assert q9["simulation"]["operation"] == "CREATE_SCENARIO"
    _unchanged_collection(q9, ranked)
    q10 = _turn(deterministic_client, headers, "这个调整后它的利润变化是多少？", q9)
    assert q10["simulation"]["operation"] in {"EXPLAIN_SCENARIO", "COMPARE_SCENARIO"}
    q11 = _turn(deterministic_client, headers, "这个调整会影响当前排名吗？", q10)
    assert q11["simulation"]["operation"] in {"EXPLAIN_SCENARIO", "COMPARE_SCENARIO"}
    q12 = _turn(deterministic_client, headers, "如果不考虑这个假设，恢复原来的排序", q11)
    assert q12["simulation"]["operation"] in {"RESET_SCENARIO", "REMOVE_OVERRIDE"}
    assert q12["task_state"]["conversation_focus"]["focus_type"] == "CANONICAL_ORDERED_RESULT"


def test_act18_complete_documented_conversation(deterministic_client):
    headers, _ = seed(deterministic_client, "act18-api-journey")
    q1 = _turn(deterministic_client, headers, "当前集合里先找三个值得继续看的")
    initial = deepcopy(_collection(q1))
    q2 = _turn(deterministic_client, headers, "第一名是谁？", q1)
    assert q2["product_ids"] == initial["top_product_ids"][:1]
    q3 = _turn(deterministic_client, headers, "为什么它排名第一？", q2)
    _explanation(q3, initial)
    q4 = _turn(deterministic_client, headers, "第二名是谁？", q3)
    assert q4["product_ids"] == initial["top_product_ids"][1:2]
    q5 = _turn(deterministic_client, headers, "第一名和第二名相比优势在哪里？", q4)
    assert q5["product_ids"] == initial["top_product_ids"][:2]
    assert q5["understanding"]["question_type"] == "ranking_compare"
    _unchanged_collection(q5, initial)
    q6 = _turn(deterministic_client, headers, "如果只看净利润，排名会变化吗？", q5)
    assert q6["understanding"]["question_type"] == "ranking_preference_change"
    _unchanged_collection(q6, initial)
    q7 = _turn(deterministic_client, headers, "按净利润重新排序当前三个商品", q6)
    assert q7["intent"] == "collection_analysis"
    assert q7["total_count"] != 60
    reranked = deepcopy(_collection(q7))
    assert set(reranked["top_product_ids"]) == set(initial["top_product_ids"])
    q8 = _turn(deterministic_client, headers, "重新排序后第一名是谁？", q7)
    assert q8["product_ids"] == reranked["top_product_ids"][:1], (q8["intent"], q8["understanding"], q8["task_state"]["conversation_focus"])
    assert q8["intent"] != "product_filter"
    q9 = _turn(deterministic_client, headers, "假设第一名采购成本下降8%，创建这个情景", q8)
    assert q9["simulation"]["operation"] == "CREATE_SCENARIO"
    _unchanged_collection(q9, reranked)
    q10 = _turn(deterministic_client, headers, "这个变化会影响排名吗？", q9)
    assert q10["simulation"]["operation"] in {"EXPLAIN_SCENARIO", "COMPARE_SCENARIO"}


@pytest.mark.parametrize("query,dimension", [
    ("如果利润比推荐度更重要，顺序有变化吗？", "profit"),
    ("更看重 ROI 时前三名会变吗？", "roi"),
    ("若优先规避风险，名次会怎么变？", "risk"),
    ("竞争压力优先考虑，谁会领先？", "competition"),
])
def test_act_metric_preference_paraphrases_are_readonly(deterministic_client, query, dimension):
    headers, _ = seed(deterministic_client, "act-preference-" + dimension)
    created = _turn(deterministic_client, headers, "当前候选池里先选三个重点")
    original = deepcopy(_collection(created))
    result = _turn(deterministic_client, headers, query, created)
    assert result["understanding"]["question_type"] == "ranking_preference_change"
    assert dimension in result["requested_dimensions"]
    assert result["ranking_analysis"]["canonical_collection_mutation"] == 0
    _unchanged_collection(result, original)


def test_act_scenario_branch_new_canonical_undo_and_old_branch_retained(deterministic_client):
    headers, _ = seed(deterministic_client, "act-scenario-branch")
    first = _turn(deterministic_client, headers, "当前候选里先找三个值得继续看的")
    old_collection = deepcopy(_collection(first))
    old_scenario = _turn(deterministic_client, headers, "假设第一名采购成本降低8%", first)
    old_id = old_scenario["simulation"]["scenario_id"]
    _unchanged_collection(old_scenario, old_collection)
    scenario_metric = _turn(deterministic_client, headers, "第一名 ROI 多少？", old_scenario)
    assert scenario_metric["intent"] == "scenario_analysis"
    assert scenario_metric["simulation"]["operation"] == "EXPLAIN_SCENARIO"
    assert scenario_metric["task_state"]["conversation_focus"]["focus_type"] == "SCENARIO_RESULT"
    new_canonical = _turn(deterministic_client, headers, "重新看当前候选，挑三个重点的", scenario_metric)
    latest_collection = deepcopy(_collection(new_canonical))
    assert latest_collection["result_id"] != old_collection["result_id"]
    assert new_canonical["task_state"]["conversation_focus"]["focus_type"] == "CANONICAL_ORDERED_RESULT"
    new_scenario = _turn(deterministic_client, headers, "假设第一名采购成本下降8%", new_canonical)
    assert new_scenario["simulation"]["scenario_id"] != old_id
    sessions = deterministic_client.get("/api/v1/agent/sessions", headers=headers).json()["data"]
    saved = next(item["state"] for item in sessions if item["id"] == first["session_id"])
    assert saved["scenario_state"]["baseline_result_id"] == latest_collection["result_id"]
    undone = _turn(deterministic_client, headers, "撤销刚才的调整", new_scenario)
    assert undone["task_state"]["conversation_focus"]["focus_type"] == "CANONICAL_ORDERED_RESULT"
    final = _turn(deterministic_client, headers, "现在第一名是谁？", undone)
    assert final["product_ids"] == latest_collection["top_product_ids"][:1]
    sessions = deterministic_client.get("/api/v1/agent/sessions", headers=headers).json()["data"]
    saved = next(item["state"] for item in sessions if item["id"] == first["session_id"])
    assert any(item["scenario_id"] == old_id for item in saved["scenario_history"])


def test_act_explicit_selectors_and_negative_route_contrasts(deterministic_client):
    headers, products = seed(deterministic_client, "act-selector-contrasts")
    created = _turn(deterministic_client, headers, "当前候选里先找三个值得继续看的")
    original = deepcopy(_collection(created))

    roi = _turn(deterministic_client, headers, "这批候选按 ROI 从高到低取前三", created)
    assert roi["intent"] == "collection_analysis"
    assert roi["understanding"]["collection_selector"]["metric"] == "roi"
    assert roi["understanding"]["question_type"] != "ranking_preference_change"
    _unchanged_collection(roi, original)

    dual = _turn(deterministic_client, headers, "推荐度前三和净利润前三有什么区别？", roi)
    groups = dual["understanding"]["collection_selector"]["groups"]
    assert [item["metric"] for item in groups] == ["recommendation_score", "net_profit"]
    assert dual["intent"] == "collection_analysis"
    _unchanged_collection(dual, original)

    named = next(item for item in products if "桌面理线器" in item["title"])
    price = _turn(deterministic_client, headers, "桌面理线器售价是多少？", dual)
    assert price["intent"] == "product_price"
    assert price["product_ids"] == [named["id"]]
    assert price["understanding"]["question_type"] != "ranking_explanation"
    _unchanged_collection(price, original)


def test_act_new_session_has_no_ranking_or_scenario_leak(deterministic_client):
    headers, _ = seed(deterministic_client, "act-isolation")
    created = _turn(deterministic_client, headers, "当前候选里先找三个值得继续看的")
    scenario = _turn(deterministic_client, headers, "假设第一名采购成本降低8%", created)
    assert scenario["simulation"]["operation"] == "CREATE_SCENARIO"
    new_id = deterministic_client.post("/api/v1/agent/sessions", headers=headers, json={}).json()["data"]["id"]
    result = ask(deterministic_client, headers, "现在第一名是谁？", session_id=new_id)
    assert result["response_type"] == "clarification"
    assert result["task_state"]["active_collection"] is None
    assert result["simulation"] is None
