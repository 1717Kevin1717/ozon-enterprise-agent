import pytest

from app.agent.query_understanding import understand_query
from app.agent.model_router import ModelRouter
from test_demo_reliability import active_collection_snapshot, establish_collection
from test_model_router import MockProvider, install_router
from test_stability_sprint import ask, deterministic_client


PROFIT_DIAGNOSIS_QUERIES = (
    "把当前候选池里净利率没达到目标的商品找出来，看看主要能从哪些价格或成本项改善。",
    "哪些候选的净利率还没达标？主要卡在哪些成本？",
    "把低于目标净利率的候选找出来并做成本诊断",
    "当前候选中利润率未达目标的，分析价格和成本改善空间",
    "这批商品净利润率不达标的是谁，哪些成本可以优化",
    "净利率没有达到目标要求的候选，请检查成本影响",
)

HISTORY_QUERIES = (
    "检查这批候选里有没有和历史放弃商品同品牌或同类目的，告诉我哪些需要人工重点复核。",
    "这批商品有没有类似历史淘汰案例，哪些需要人工确认？",
    "当前候选里哪些和以前放弃的同品牌或同类目，需要先复核谁？",
    "查一下这批候选的历史失败记录并给出人工复核顺序",
    "哪些商品有同品牌历史拒绝案例，需要人工重点查看？",
    "请核对当前商品的同类目历史放弃决策，并列出需要人工介入的候选",
)

DUAL_SELECTOR_QUERIES = (
    "推荐度最高的三个和利润最高的三个有什么区别？",
    "推荐分前三和利润前三是不是同一批？",
    "推荐度Top3与净利润Top3对比一下",
    "对比推荐分最高三名和净利润最高三名",
    "这批候选按推荐度取前三，再和利润前三比较",
    "当前商品中推荐度前三与利润前三有哪些差异？",
)

EVIDENCE_GAP_QUERIES = (
    "哪些商品现在存在数据缺口，为什么？",
    "哪些候选证据不完整？",
    "当前商品中谁缺数据，缺哪些？",
    "这批候选有哪些证据不足，原因是什么？",
    "谁的数据不完整，请列出受影响字段",
    "请找出当前候选的数据缺口并说明原因",
)

HUMAN_REVIEW_QUERIES = (
    "哪些商品应该优先进入人工审核？",
    "谁应该先人工复核？",
    "当前这些候选按审核优先级怎么排？",
    "这批货先让人看哪几个？",
    "哪些商品最需要人工确认，原因是什么？",
    "请根据风险、合规和证据完整性给出人工复核优先顺序。",
)


def _signature(query: str):
    parsed, frame = understand_query(query, [], (), active_collection_snapshot())
    return {
        "intent": parsed.name,
        "reference": frame.collection_reference,
        "selector": frame.collection_selector,
        "filters": frame.collection_filters,
        "analyses": frame.analysis_requests,
        "operation": frame.operation,
        "entities": frame.entity_mentions,
    }


@pytest.mark.parametrize("queries", (
    PROFIT_DIAGNOSIS_QUERIES,
    HISTORY_QUERIES,
    DUAL_SELECTOR_QUERIES,
    EVIDENCE_GAP_QUERIES,
    HUMAN_REVIEW_QUERIES,
))
def test_collection_semantic_classes_normalize_canonical_and_five_paraphrases(queries):
    canonical = _signature(queries[0])
    for query in queries[1:]:
        assert _signature(query) == canonical, query


def test_collection_semantic_negative_contrasts_are_not_overgeneralized():
    _, review_status = understand_query(
        "第一名是不是已经审核通过？", [], (), active_collection_snapshot(),
    )
    assert "HUMAN_REVIEW_PRIORITY" not in review_status.analysis_requests

    _, profit = understand_query(
        "当前候选利润最高三个", [], (), active_collection_snapshot(),
    )
    _, margin = understand_query(
        "当前候选净利率最高三个", [], (), active_collection_snapshot(),
    )
    assert profit.collection_selector["metric"] == "net_profit"
    assert margin.collection_selector["metric"] == "net_margin"

    parsed, ambiguous = understand_query(
        "当前候选里利润不好的商品", [], (), active_collection_snapshot(),
    )
    assert not ambiguous.collection_filters
    assert parsed.name in {"unknown", "collection_analysis"}


def _trace_text(item: dict) -> str:
    return str(
        item.get("business_label") or item.get("summary") or item.get("message")
        or item.get("tool") or item.get("event") or item.get("status")
        or item.get("failure_code") or ""
    ).strip()


def _assert_normal_result(result: dict):
    assert result["response_type"] == "collection_report", result
    assert result["clarification_code"] is None
    assert result["fallback_reason"] != "SEMANTIC_PLAN_VALIDATION_FAILED"
    assert result["trace"]
    assert all(_trace_text(item) for item in result["trace"]), result["trace"]


def test_act13_production_chain_preserves_collection_and_business_contracts(deterministic_client):
    headers, d00 = establish_collection(deterministic_client, "act13-production-chain")
    _assert_normal_result(d00)
    baseline = d00["task_state"]["active_collection"]
    baseline_identity = (
        baseline["product_ids"], baseline["ranked_product_ids"],
        baseline["top_product_ids"], baseline["source_task_revision"],
    )

    queries = (
        "比较当前动态推荐度最高的三个商品，说明利润、竞争、合规和数据缺口，给出人工审核顺序。",
        PROFIT_DIAGNOSIS_QUERIES[0],
        HISTORY_QUERIES[0],
        "当前候选池风险最低的三个商品是谁？",
        DUAL_SELECTOR_QUERIES[0],
        EVIDENCE_GAP_QUERIES[0],
        HUMAN_REVIEW_QUERIES[0],
    )
    results = [
        ask(deterministic_client, headers, query, session_id=d00["session_id"])
        for query in queries
    ]
    for result in results:
        _assert_normal_result(result)
        current = result["task_state"]["active_collection"]
        assert (
            current["product_ids"], current["ranked_product_ids"],
            current["top_product_ids"], current["source_task_revision"],
        ) == baseline_identity

    d01, d02, d03, d04, d05, d06, d07 = results
    assert d01["human_review_priority"]
    assert "建议人工复核顺序" in d01["answer"]

    assert d02["profit_diagnosis"]
    assert all(item["current_net_margin"] < item["target_net_margin"] for item in d02["profit_diagnosis"])
    assert all(item["margin_gap"] > 0 and item["known_cost_factors"] for item in d02["profit_diagnosis"])
    required_cost_fields = {
        "current_price", "procurement_cost", "advertising_cost", "shipping_cost",
        "fulfillment_cost", "platform_fee", "warehousing_cost", "tax_cost",
        "return_loss_reserve", "other_cost", "platform_commission_rate",
    }
    assert all(
        required_cost_fields <= {factor["field"] for factor in item["known_cost_factors"]}
        for item in d02["profit_diagnosis"]
    )

    assert d03["historical_decision_analysis"]
    assert all(
        {"product_id", "product_title", "historical_matches", "status", "review_reason_codes"} <= set(item)
        for item in d03["historical_decision_analysis"]
    )

    assert d04["understanding"]["collection_selector"]["metric"] == "risk"
    assert len(d04["product_ids"]) <= 3

    groups = d05["collection_analysis"]["selection_groups"]
    assert [item["metric"] for item in groups] == ["recommendation_score", "net_profit"]
    assert all(
        {"metric_value", "net_profit", "net_margin", "risk_level", "compliance_status", "decision_status"} <= set(item)
        for group in groups for item in group["items"]
    )
    assert d05["collection_analysis"]["selection_comparison"]
    assert "推荐度 Top3" in d05["answer"] and "净利润 Top3" in d05["answer"]

    gap_candidates = d06["collection_analysis"]["candidates"]
    assert gap_candidates and all(item["evidence_gaps"] for item in gap_candidates)
    assert all(
        gap.get("field") and gap.get("reason")
        for item in gap_candidates for gap in item["evidence_gaps"]
    )
    assert "字段" in d06["answer"]

    priorities = d07["human_review_priority"]
    assert priorities
    assert [item["priority_rank"] for item in priorities] == list(range(1, len(priorities) + 1))
    assert all(item["reason_codes"] and "evidence" in item for item in priorities)
    assert "建议人工复核顺序" in d07["answer"]


def test_collection_followup_top_k_stays_within_backend_tool_contract(deterministic_client):
    headers, created = establish_collection(deterministic_client, "collection-followup-top-k-boundary")
    session_id = created["session_id"]
    for query in (
        "这批商品里谁风险最低",
        "这批商品里谁利润最高",
        "第一名为什么值得继续研究",
    ):
        result = ask(deterministic_client, headers, query, session_id=session_id)
        assert result["response_type"] != "error"
        assert result["tool_call_count"] <= 1


def test_collection_roi_ranking_and_risk_filter_keep_active_scope(deterministic_client):
    roi = _signature("这批候选按 ROI 从高到低取前三个")
    risk = _signature("当前候选池中高风险商品有哪些")
    assert roi["intent"] == "collection_analysis"
    assert roi["selector"]["metric"] == "roi"
    assert risk["intent"] == "collection_analysis"
    assert all(risk["filters"][0].get(key) == value for key, value in {
        "field": "risk", "operator": "EQ", "value": 2,
    }.items())

    headers, created = establish_collection(deterministic_client, "collection-risk-scope-boundary")
    baseline_ids = set(created["task_state"]["active_collection"]["product_ids"])
    result = ask(
        deterministic_client, headers, "当前候选池中高风险商品有哪些",
        session_id=created["session_id"],
    )
    assert result["intent"] == "collection_analysis"
    assert set(result["product_ids"]) <= baseline_ids
    assert set(result["task_state"]["active_collection"]["product_ids"]) == baseline_ids


def test_provider_invalid_plan_recovers_complete_review_frame_with_auditable_trace(
    deterministic_client, monkeypatch,
):
    headers, created = establish_collection(deterministic_client, "act13-provider-recovery")
    qwen = MockProvider("qwen", failure="SEMANTIC_PLAN_VALIDATION_FAILED")
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))

    result = ask(
        deterministic_client, headers, HUMAN_REVIEW_QUERIES[1],
        session_id=created["session_id"], provider_execution_policy="QWEN_ONLY",
    )

    assert result["intent"] == "collection_analysis"
    assert result["fallback_used"] is True
    assert result["fallback_reason"] == "SEMANTIC_PLAN_VALIDATION_FAILED"
    assert result["human_review_priority"]
    assert result["trace"] and all(_trace_text(item) for item in result["trace"])
    assert len(qwen.calls) == 1


def _business_signature(result: dict, capability: str):
    if capability == "profit":
        return [
            (item["product_id"], item["current_net_margin"], item["target_net_margin"], item["margin_gap"])
            for item in result["profit_diagnosis"]
        ]
    if capability == "history":
        return [
            (
                item["product_id"], item["status"],
                tuple(match["historical_product_id"] for match in item["historical_matches"]),
            )
            for item in result["historical_decision_analysis"]
        ]
    if capability == "dual_selector":
        collection = result["collection_analysis"]
        return (
            tuple(
                (group["metric"], tuple(item["product_id"] for item in group["items"]))
                for group in collection["selection_groups"]
            ),
            collection["selection_comparison"],
        )
    if capability == "evidence":
        return [
            (
                item["product_id"],
                tuple(sorted((gap["code"], gap["field"]) for gap in item["evidence_gaps"])),
            )
            for item in result["collection_analysis"]["candidates"]
        ]
    return [
        (item["product_id"], item["priority_rank"], tuple(item["reason_codes"]))
        for item in result["human_review_priority"]
    ]


@pytest.mark.parametrize("capability,queries", (
    ("profit", PROFIT_DIAGNOSIS_QUERIES),
    ("history", HISTORY_QUERIES),
    ("dual_selector", DUAL_SELECTOR_QUERIES),
    ("evidence", EVIDENCE_GAP_QUERIES),
    ("review", HUMAN_REVIEW_QUERIES),
))
def test_collection_paraphrases_have_identical_backend_business_results(
    deterministic_client, capability, queries,
):
    headers, created = establish_collection(
        deterministic_client, f"act13-metamorphic-{capability}",
    )
    results = [
        ask(deterministic_client, headers, query, session_id=created["session_id"])
        for query in queries
    ]
    signatures = [_business_signature(result, capability) for result in results]
    assert all(signature == signatures[0] for signature in signatures[1:]), [
        (index, query, len(signature), signature == signatures[0])
        for index, (query, signature) in enumerate(zip(queries, signatures))
    ]
    assert all(result["clarification_code"] is None for result in results)
