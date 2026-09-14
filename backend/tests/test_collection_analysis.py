import pytest

from app.agent.model_router import ModelRouter, semantic_context_slots
from app.agent.query_understanding import build_semantic_context_packet, understand_query
from app.agent.tools import _collection_evidence_gaps
from app.agent.tool_registry import AnalyzeCollectionInput
from app.schemas.agent import ContextSnapshot, TaskState
from test_stability_sprint import ask, deterministic_client, seed
from test_model_router import MockProvider, install_router


def test_collection_tool_contract_is_strict():
    value = AnalyzeCollectionInput(
        collection_type="candidate_pool",
        top_k=3,
        ranking_dimensions=["profit", "risk"],
    )
    assert value.top_k == 3
    with pytest.raises(Exception):
        AnalyzeCollectionInput(collection_type="candidate_pool", top_k=0)


@pytest.mark.parametrize(
    ("query", "reference", "top_k", "wants_gaps"),
    [
        ("从候选集合挑出前4个方向并列出证据缺口", "candidate_pool", 4, True),
        ("企业商品库里推荐两个最值得研究的商品", "company_catalog", 2, False),
        ("当前候选中先给我前三", "candidate_pool", 3, False),
        ("看看当前集合还缺哪些资料", "current_collection", None, True),
        ("哪些商品最值得进一步验证", "candidate_pool", None, False),
    ],
)
def test_collection_paraphrases_map_to_typed_scope(query, reference, top_k, wants_gaps):
    task = TaskState()
    if reference == "current_collection":
        task = TaskState.model_validate({
            "task_type": "collection_analysis", "status": "COMPLETED",
            "active_collection": {"collection_type": "candidate_pool", "product_ids": ["p1"]},
        })
    snapshot = ContextSnapshot(
        session_id="s", company_id="c", user_id="u", current_query=query, task_state=task,
    )
    parsed, understanding = understand_query(query, [], context_snapshot=snapshot)
    assert parsed.name == "collection_analysis"
    assert understanding.collection_reference == reference
    assert understanding.top_k == top_k
    assert understanding.evidence_gap_requested is wants_gaps


def test_evidence_gap_taxonomy_reuses_provenance_and_readiness():
    view = {
        "analysis": {
            "data_confidence": 0.4,
            "missing_data": [{"dimension": "profit", "missing": "cost", "action": "补成本"}],
            "evidence_completeness": {"groups": {"demand": {"ready": False}}},
        },
        "data_trust": {"fields": {
            "current_price": {
                "presence": "MISSING", "evidence_status": "SOURCE_MISSING",
                "freshness": {"status": "STALE"},
            },
        }},
    }
    codes = {item["code"] for item in _collection_evidence_gaps(view, "REVIEW_REQUIRED")}
    assert codes == {
        "MISSING", "STALE", "LOW_CONFIDENCE", "NO_PROVENANCE",
        "INSUFFICIENT_SAMPLE", "NEEDS_HUMAN_REVIEW",
    }


def test_candidate_pool_top_k_uses_backend_gate_and_structured_gaps(deterministic_client):
    headers, _ = seed(deterministic_client, "collection-top-k")
    result = ask(
        deterministic_client,
        headers,
        "请分析当前候选池里最值得继续研究的三个商品方向，并说明证据缺口。",
    )

    collection = result["collection_analysis"]
    assert result["intent"] == "collection_analysis"
    assert result["response_type"] == "collection_report"
    assert result["tool_call_count"] == 1
    assert collection["collection_type"] == "candidate_pool"
    assert collection["requested_top_k"] == 3
    assert collection["returned_top_k"] == len(result["products"])
    assert collection["shortfall"] == 3 - len(result["products"])
    assert all(item["decision_status"] == "RECOMMENDED" for item in result["products"])
    assert all(item["eligible"] for item in collection["candidates"])
    assert all(
        item["ranking_reasons"] and item["key_positives"]
        and "main_risks" in item and "evidence_gaps" in item and "next_evidence_actions" in item
        for item in collection["candidates"]
    )
    assert "不等于最终上架决策" in result["answer"]
    assert result["task_state"]["active_collection"]["top_product_ids"] == result["product_ids"]


def test_candidate_pool_and_company_catalog_have_distinct_membership(deterministic_client):
    headers, _ = seed(deterministic_client, "collection-scope")
    candidate = ask(deterministic_client, headers, "分析候选池中最值得研究的三个商品")
    catalog = ask(deterministic_client, headers, "分析公司商品库中最值得研究的三个商品")

    assert candidate["collection_analysis"]["collection_type"] == "candidate_pool"
    assert catalog["collection_analysis"]["collection_type"] == "company_catalog"
    assert candidate["collection_analysis"]["member_count"] < catalog["collection_analysis"]["member_count"]


def test_top_k_never_pads_with_blocked_or_other_ineligible_products(deterministic_client):
    headers, _ = seed(deterministic_client, "collection-no-padding")
    result = ask(deterministic_client, headers, "从公司商品库选出最值得继续研究的20个商品并说明证据缺口")
    collection = result["collection_analysis"]

    assert collection["returned_top_k"] < 20
    assert collection["shortfall"] == 20 - collection["returned_top_k"]
    assert collection["ineligible_status_counts"].get("BLOCKED", 0) > 0
    assert all(item["decision_status"] == "RECOMMENDED" for item in result["products"])


def test_collection_followup_resolves_ordinal_and_recomputes_evidence_gaps(deterministic_client):
    headers, _ = seed(deterministic_client, "collection-gap-followup")
    created = ask(deterministic_client, headers, "分析候选池里最值得继续研究的三个商品，并说明证据缺口")
    assert len(created["product_ids"]) >= 2

    followup = ask(deterministic_client, headers, "第二个还缺什么证据", session_id=created["session_id"])

    assert followup["intent"] == "collection_analysis"
    assert followup["task_state"]["operation"] == "IDENTIFY_EVIDENCE_GAPS"
    assert followup["product_ids"] == [created["product_ids"][1]]
    assert followup["tool_call_count"] == 1
    assert followup["collection_analysis"]["candidates"][0]["evidence_gaps"] is not None


def test_collection_second_item_reason_then_pronoun_gap_keeps_original_collection(deterministic_client):
    headers, _ = seed(deterministic_client, "collection-second-chain")
    created = ask(deterministic_client, headers, "分析候选池里最值得继续研究的三个商品")
    original_collection = created["task_state"]["active_collection"]
    second_id = created["product_ids"][1]

    explained = ask(deterministic_client, headers, "第二个为什么值得继续研究", session_id=created["session_id"])
    assert explained["product_ids"] == [second_id]
    assert explained["task_state"]["active_collection"]["product_ids"] == original_collection["product_ids"]
    assert explained["task_state"]["active_collection"]["top_product_ids"] == original_collection["top_product_ids"]
    assert explained["task_state"]["focused_collection_member"]["product_id"] == second_id
    assert explained["task_state"]["focused_collection_member"]["original_ordinal"] == 2
    assert explained["collection_analysis"]["candidates"][0]["original_ordinal"] == 2
    assert "原集合第 2 位" in explained["answer"]

    gaps = ask(deterministic_client, headers, "它还缺哪些关键证据", session_id=created["session_id"])
    assert gaps["task_state"]["operation"] == "IDENTIFY_EVIDENCE_GAPS"
    assert gaps["product_ids"] == [second_id]
    assert gaps["response_type"] == "collection_report"
    assert gaps["tool_call_count"] == 1

    reranked = ask(deterministic_client, headers, "只看利润和风险重新排一次", session_id=created["session_id"])
    assert reranked["task_state"]["operation"] == "RERANK_COLLECTION"
    assert reranked["task_state"]["active_collection"]["product_ids"] == original_collection["product_ids"]
    assert reranked["task_state"]["active_collection"]["ranking_dimensions"] == ["profit", "risk"]
    assert reranked["task_state"]["focused_collection_member"] is None


def test_collection_rerank_preserves_members_and_changes_dimensions(deterministic_client):
    headers, _ = seed(deterministic_client, "collection-rerank")
    created = ask(deterministic_client, headers, "分析候选池里最值得继续研究的三个商品")
    before = created["task_state"]["active_collection"]

    reranked = ask(deterministic_client, headers, "只看利润和风险重新排一次", session_id=created["session_id"])
    after = reranked["task_state"]["active_collection"]

    assert reranked["task_state"]["operation"] == "RERANK_COLLECTION"
    assert after["product_ids"] == before["product_ids"]
    assert after["ranking_dimensions"] == ["profit", "risk"]
    assert reranked["tool_call_count"] == 1


def test_collection_ranking_explanation_compares_current_first_and_second(deterministic_client):
    headers, _ = seed(deterministic_client, "collection-ranking-pair")
    created = ask(deterministic_client, headers, "分析候选池里最值得继续研究的三个商品")
    reranked = ask(deterministic_client, headers, "只看利润和风险重新排一次", session_id=created["session_id"])
    current_top = reranked["task_state"]["active_collection"]["top_product_ids"][:2]

    explained = ask(
        deterministic_client, headers,
        "现在第一名为什么比第二名更值得继续研究",
        session_id=created["session_id"],
    )

    assert explained["product_ids"] == current_top
    assert len(explained["collection_analysis"]["candidates"]) == 2
    assert "第一名" in explained["answer"] and "第二名" in explained["answer"]
    assert explained["task_state"]["active_collection"]["ranking_dimensions"] == ["profit", "risk"]


def test_collection_can_analyze_active_result_set_without_widening_scope(deterministic_client):
    headers, _ = seed(deterministic_client, "collection-result-set")
    filtered = ask(deterministic_client, headers, "筛选风险低的商品，返回5个")
    analyzed = ask(
        deterministic_client,
        headers,
        "分析当前结果中最值得继续研究的三个商品",
        session_id=filtered["session_id"],
    )

    source_ids = set(filtered["task_state"]["active_result_set"]["product_ids"])
    assert analyzed["collection_analysis"]["collection_type"] == "active_result_set"
    assert set(analyzed["collection_analysis"]["member_product_ids"]) <= source_ids
    assert set(analyzed["product_ids"]) <= source_ids


def test_collection_state_does_not_cross_session_boundary(deterministic_client):
    headers, _ = seed(deterministic_client, "collection-session-isolation")
    created = ask(deterministic_client, headers, "分析候选池里最值得继续研究的三个商品")
    new_session = deterministic_client.post("/api/v1/agent/sessions", headers=headers, json={}).json()["data"]["id"]

    isolated = ask(deterministic_client, headers, "第二个还缺什么证据", session_id=new_session)

    assert isolated["session_id"] != created["session_id"]
    assert isolated["response_type"] == "clarification"
    assert isolated["clarification_code"] == "MISSING_CONTEXT"
    assert isolated["fallback_reason"] != "INVALID_RESPONSE"
    assert isolated["tool_call_count"] == 0
    assert isolated["task_state"]["active_collection"] is None


def test_collection_paraphrase_executes_generic_top_k_and_gap_contract(deterministic_client):
    headers, _ = seed(deterministic_client, "collection-paraphrase-runtime")
    result = ask(
        deterministic_client,
        headers,
        "这批候选里先挑三个最值得进一步验证的，并告诉我还缺什么数据",
    )

    assert result["intent"] == "collection_analysis"
    assert result["collection_analysis"]["collection_type"] == "candidate_pool"
    assert result["collection_analysis"]["requested_top_k"] == 3
    assert result["task_state"]["operation"] == "RECOMMEND_TOP_K"
    assert result["fallback_reason"] != "INVALID_RESPONSE"


def test_collection_semantic_packet_exposes_counts_not_internal_ids():
    task = TaskState.model_validate({
        "task_id": "internal-task", "revision": 1, "task_type": "collection_analysis",
        "operation": "RECOMMEND_TOP_K", "status": "COMPLETED",
        "active_collection": {
            "collection_type": "candidate_pool", "product_ids": ["internal-a", "internal-b"],
            "ranked_product_ids": ["internal-b", "internal-a"], "top_product_ids": ["internal-b"],
            "top_k": 1, "ranking_dimensions": ["recommendation"],
        },
    })
    snapshot = ContextSnapshot(
        session_id="internal-session", company_id="internal-company", user_id="internal-user",
        current_query="第二个还缺什么证据", task_state=task,
    )
    parsed, understanding = understand_query(snapshot.current_query, [], context_snapshot=snapshot)
    packet = build_semantic_context_packet(snapshot.current_query, understanding, snapshot, [])
    outbound = semantic_context_slots(packet.model_dump(mode="json"))
    serialized = str(outbound)

    assert set(outbound) == {
        "collection_type", "collection_scope", "operation", "top_k",
        "requested_dimensions", "ranking_dimensions", "reference_role",
        "ordinal_reference", "has_active_collection", "has_focused_member",
        "has_ranking_state", "evidence_gap_requested", "rerank_requested",
        "item_slots", "allowed_tools",
    }
    assert outbound["collection_type"] == "candidate_pool"
    assert outbound["top_k"] == 1
    assert outbound["item_slots"] == ["ITEM_1"]
    assert outbound["has_active_collection"] is True
    assert outbound["has_ranking_state"] is True
    assert "internal-a" not in serialized
    assert "internal-b" not in serialized
    assert "internal-session" not in serialized
    assert "internal-company" not in serialized
    assert "internal-user" not in serialized


def test_mock_qwen_semantic_frame_hands_collection_plan_to_backend(deterministic_client, monkeypatch):
    headers, _ = seed(deterministic_client, "collection-mock-qwen")
    frame = {
        "intent": "collection_analysis", "task_type": "collection_analysis",
        "operation": "RECOMMEND_TOP_K", "question_type": "collection_analysis",
        "entity_mentions": [], "entity_roles": [], "references": [], "reference_slots": [],
        "requested_dimensions": ["recommendation", "decision", "evidence_gap"],
        "collection_reference": "candidate_pool", "top_k": 3,
        "evidence_gap_requested": True, "confidence": 0.94,
        "route": "SEMANTIC_PLANNER", "planned_tools": ["analyze_collection"],
        "requires_context": False, "requires_task_state": False,
        "requires_tools": True, "requires_reasoning": False,
        "clarification_required": False,
    }
    qwen = MockProvider("qwen", [frame])
    deepseek = MockProvider("deepseek", reasoning=True)
    install_router(monkeypatch, ModelRouter([qwen, deepseek], primary_name="qwen", reasoning_name="deepseek"))

    result = ask(
        deterministic_client,
        headers,
        "请分析当前候选池里最值得继续研究的三个商品方向，并说明证据缺口。",
        provider_execution_policy="QWEN_ONLY",
    )

    assert len(qwen.calls) == 1
    assert deepseek.calls == []
    assert result["intent"] == "collection_analysis"
    assert result["active_provider"] == "qwen"
    assert result["tool_call_count"] == 1
    assert [item["tool_name"] for item in result["tool_results"]] == ["analyze_collection"]
    assert result["collection_analysis"]["requested_top_k"] == 3


def test_collection_connect_error_reuses_deterministic_ranking_state(deterministic_client, monkeypatch):
    headers, _ = seed(deterministic_client, "collection-connect-fallback")
    created = ask(deterministic_client, headers, "分析候选池里最值得继续研究的三个商品")
    expected_second = created["product_ids"][1]
    qwen = MockProvider("qwen", failure="CONNECT_ERROR")
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))

    result = ask(
        deterministic_client, headers, "第二个为什么值得继续研究",
        session_id=created["session_id"], provider_execution_policy="QWEN_ONLY",
    )

    assert len(qwen.calls) == 1
    assert result["fallback_used"] is True
    assert result["fallback_reason"] == "CONNECT_ERROR"
    assert result["product_ids"] == [expected_second]
    assert result["task_state"]["focused_collection_member"]["original_ordinal"] == 2


def test_provider_ranking_pair_uses_active_collection_without_result_set(deterministic_client, monkeypatch):
    headers, _ = seed(deterministic_client, "collection-provider-ranking-lifecycle")
    common = {
        "intent": "collection_analysis", "task_type": "collection_analysis",
        "question_type": "collection_analysis", "entity_mentions": [], "entity_roles": [],
        "references": [], "confidence": 0.94, "route": "SEMANTIC_PLANNER",
        "planned_tools": ["analyze_collection"], "requires_tools": True,
        "requires_reasoning": False, "clarification_required": False,
    }
    qwen = MockProvider("qwen", [
        {
            **common, "operation": "RECOMMEND_TOP_K", "reference_slots": [],
            "requested_dimensions": ["recommendation", "decision", "evidence_gap"],
            "collection_reference": "candidate_pool", "top_k": 3,
            "evidence_gap_requested": True, "requires_context": False,
            "requires_task_state": False,
        },
        {
            **common, "operation": "RERANK_COLLECTION", "reference_slots": ["session_state"],
            "requested_dimensions": ["profit", "risk"],
            "collection_reference": "current_collection", "top_k": 3,
            "sort_updates": ["profit:desc", "risk:asc"], "requires_context": True,
            "requires_task_state": True,
        },
        {
            **common, "intent": "decision_explanation", "task_type": "decision_explanation",
            "question_type": "decision_explanation", "operation": "EXPLAIN_RANKING",
            "references": ["第一名", "第二名"],
            "reference_slots": ["session_state", "ordinal_reference"],
            # A provider may default an explanation to recommendation. Backend
            # arbitration must bind it to the active collection ranking spec.
            "requested_dimensions": ["recommendation"],
            "planned_tools": ["get_product"], "requires_reasoning": True,
            # Simulates a valid provider frame choosing the wrong state family.
            # Backend task-state arbitration must still bind the active collection.
            "collection_reference": "active_result_set", "top_k": 2,
            "ordinal_reference": 1, "ordinal_references": [1, 2],
            "requires_context": True, "requires_task_state": True,
        },
    ])
    deepseek = MockProvider("deepseek", [{}], reasoning=True)
    install_router(monkeypatch, ModelRouter(
        [qwen, deepseek], primary_name="qwen", reasoning_name="deepseek", data_mode="mock_only",
    ))

    created = ask(
        deterministic_client, headers,
        "请分析当前候选池里最值得继续研究的三个商品方向，并说明证据缺口",
        provider_execution_policy="QWEN_ONLY",
    )
    before = created["task_state"]["active_collection"]
    assert before is not None
    assert len(before["product_ids"]) > 0
    assert len(before["top_product_ids"]) >= 3
    assert created["task_state"]["active_result_set"] is None

    reranked = ask(
        deterministic_client, headers, "只看利润和风险重新排一次",
        session_id=created["session_id"], provider_execution_policy="QWEN_ONLY",
    )
    current = reranked["task_state"]["active_collection"]
    current_top = current["top_product_ids"][:2]
    assert current["product_ids"] == before["product_ids"]
    assert current["ranking_dimensions"] == ["profit", "risk"]
    assert len(current_top) == 2
    assert current["source_task_revision"] > before["source_task_revision"]
    assert reranked["task_state"]["active_result_set"] is None

    explained = ask(
        deterministic_client, headers, "现在第一名为什么比第二名更值得继续研究",
        session_id=created["session_id"],
    )

    assert len(qwen.calls) == 3
    assert deepseek.calls == []
    assert explained["response_type"] != "clarification"
    assert explained["clarification_code"] is None
    assert explained["tool_call_count"] >= 1
    assert explained["product_ids"] == current_top
    assert explained["understanding"]["collection_reference"] == "current_collection"
    assert explained["requested_dimensions"] == ["profit", "risk"]
    assert explained["collection_analysis"]["ranking_dimensions"] == ["profit", "risk"]
    assert "当前按profit、risk排序" in explained["answer"]
    assert "推荐度由高到低" not in explained["answer"]
    assert explained["task_state"]["active_collection"]["ranked_product_ids"] == current["ranked_product_ids"]
    assert explained["task_state"]["active_collection"]["ranking_dimensions"] == current["ranking_dimensions"]
    assert explained["task_state"]["active_collection"]["source_task_revision"] == current["source_task_revision"]


def test_default_collection_ranking_explanation_still_uses_recommendation_score(deterministic_client):
    headers, _ = seed(deterministic_client, "collection-default-ranking-explanation")
    created = ask(deterministic_client, headers, "分析候选池里最值得继续研究的三个商品")

    explained = ask(
        deterministic_client, headers, "现在第一名为什么比第二名更值得继续研究",
        session_id=created["session_id"],
    )

    assert created["task_state"]["active_collection"]["ranking_dimensions"] == ["recommendation"]
    assert explained["collection_analysis"]["ranking_dimensions"] == ["recommendation"]
    assert "当前按recommendation排序" in explained["answer"]
    assert "动态推荐度" in explained["answer"]


@pytest.mark.parametrize(
    "query",
    [
        "只看利润和风险重新排一次",
        "按利润优先、风险其次重新排",
        "这批候选按利润和风险排序",
    ],
)
def test_active_collection_rerank_precedes_generic_filter(deterministic_client, query):
    headers, _ = seed(deterministic_client, "collection-rerank-precedence-" + str(abs(hash(query))))
    created = ask(deterministic_client, headers, "分析候选池里最值得继续研究的三个商品")
    before = created["task_state"]["active_collection"]

    result = ask(deterministic_client, headers, query, session_id=created["session_id"])

    assert result["intent"] == "collection_analysis"
    assert result["task_state"]["operation"] == "RERANK_COLLECTION"
    assert result["task_state"]["active_collection"]["product_ids"] == before["product_ids"]
    assert result["task_state"]["active_collection"]["ranking_dimensions"] == ["profit", "risk"]
    assert result["task_state"]["active_result_set"] is None
    assert result["tool_call_count"] == 1


@pytest.mark.parametrize(
    "query",
    [
        "找净利率超过30%且低风险的商品",
        "筛出利润高于1000且低风险的商品",
    ],
)
def test_explicit_filter_constraints_remain_product_filter(deterministic_client, query):
    headers, _ = seed(deterministic_client, "collection-filter-distinction-" + str(abs(hash(query))))

    result = ask(deterministic_client, headers, query)

    assert result["intent"] == "product_filter"
    assert result["task_state"]["operation"] != "RERANK_COLLECTION"


def test_orphan_collection_rerank_requires_context_instead_of_unconditional_filter(deterministic_client):
    headers, _ = seed(deterministic_client, "collection-rerank-missing-context")

    result = ask(deterministic_client, headers, "把刚才那些按利润和风险重新排")

    assert result["intent"] == "collection_analysis"
    assert result["response_type"] == "clarification"
    assert result["clarification_code"] == "MISSING_CONTEXT"
    assert result["tool_call_count"] == 0
    assert result["task_state"]["active_collection"] is None
    assert result["task_state"]["active_result_set"] is None
