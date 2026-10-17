"""A displayed ordering must remain usable through later session operations."""

import json

import pytest

from app.agent.context import resolve_context_reference
from app.agent.model_router import ModelRouter
from app.agent.providers.base import ModelCapabilities, ModelProvider, ProviderCallResult
from app.agent.query_understanding import _anchor_collection_semantics
from app.agent.task_state import evolve_task_state, task_mutation_intent
from app.schemas.agent import ContextSnapshot, QueryUnderstanding, TaskState
from test_stability_sprint import ask, deterministic_client, seed


class _SemanticFrameProvider(ModelProvider):
    provider_name = "qwen"

    def __init__(self, frames):
        super().__init__(
            api_key="offline-mock-not-real",
            base_url="https://offline.invalid",
            model_name="qwen-offline",
        )
        self.frames = list(frames)
        self.calls = 0
        self.capabilities = ModelCapabilities(
            supports_text=True,
            supports_structured_output=True,
            supports_tool_planning=True,
        )

    async def semantic_interpret(self, query, context_slots):
        frame = self.frames[self.calls]
        self.calls += 1
        return ProviderCallResult(
            json.dumps(frame, ensure_ascii=False),
            self.provider_name,
            self.model_name,
        )

    async def reason(self, decision_packet):
        raise AssertionError("collection semantic tests must not invoke reasoning")


def _provider_frame(intent="selection_recommendation", **updates):
    frame = {
        "intent": intent,
        "task_type": intent,
        "question_type": intent,
        "requested_dimensions": ["recommendation", "decision"],
        "confidence": 0.91,
        "route": "SEMANTIC_PLANNER",
        "planned_tools": ["filter_products"],
    }
    frame.update(updates)
    return frame


def _install_semantic_frames(monkeypatch, frames):
    from app.agent import model_agent
    from app.api import router

    provider = _SemanticFrameProvider(frames)
    model_router = ModelRouter(
        [provider], primary_name="qwen", reasoning_name="deepseek", data_mode="disabled",
    )
    monkeypatch.setattr(router.settings, "llm_provider", "qwen")
    monkeypatch.setattr(model_agent, "build_model_router", lambda: model_router)
    return provider


def _ordered(result):
    task = result["task_state"]
    collection = task.get("active_collection")
    result_set = task.get("active_result_set")
    return (result_set or {}).get("product_ids") or (collection or {}).get("top_product_ids") or []


def test_act14_ordered_result_continuous_chain(deterministic_client):
    headers, _ = seed(deterministic_client, "ordered-result-chain")
    history = []

    def turn(label, query, session_id=None):
        result = ask(deterministic_client, headers, query, session_id=session_id) if session_id else ask(deterministic_client, headers, query)
        history.append({
            "case": label, "operation": result.get("understanding", {}).get("operation"),
            "scope": result.get("understanding", {}).get("collection_reference"),
            "ordered_ids": _ordered(result), "product_ids": result.get("product_ids"),
            "task_revision": result.get("task_state", {}).get("revision"),
            "scenario_revision": (result.get("scenario_state") or {}).get("scenario_revision"),
        })
        return result

    c00 = turn("C00", "帮我分析一下当前候选池，先找三个值得继续看的。")
    session_id = c00["session_id"]
    initial = _ordered(c00)
    assert len(c00["products"]) == len(initial) == 3, history
    assert initial == c00["product_ids"], history
    collection = c00["task_state"]["active_collection"]
    assert collection["top_k"] == collection["effective_top_k"] == 3, history
    assert collection["collection_type"] == "candidate_pool", history

    c01 = turn("C01", "第一名是谁？", session_id)
    assert c01["product_ids"] == initial[:1], history
    assert all(item["tool_name"] != "search_products" for item in c01.get("tool_results", [])), history

    c02 = turn("C02", "它为什么排第一？", session_id)
    assert c02["product_ids"] == initial[:1], history

    c03 = turn("C03", "那第二个呢？", session_id)
    assert c03["product_ids"] == initial[1:2], history

    c04 = turn("C04", "前两个比一下。", session_id)
    assert c04["product_ids"] == initial[:2], history
    assert c04["task_state"]["active_collection"] == c03["task_state"]["active_collection"], history

    c05 = turn("C05", "如果只看利润呢？", session_id)
    reranked = _ordered(c05)
    assert c05["intent"] == "collection_analysis", history
    assert len(reranked) == 3 and set(reranked) == set(initial), history
    assert c05["total_count"] != 60, history
    assert all(item["tool_name"] != "filter_products" for item in c05.get("tool_results", [])), history

    c06 = turn("C06", "现在第一名是谁？", session_id)
    assert c06["product_ids"] == reranked[:1], history

    c07 = turn("C07", "假设第一名采购成本降8%。", session_id)
    assert c07["intent"] == "scenario_analysis", history
    assert c07["simulation"]["operation"] == "CREATE_SCENARIO", history
    assert c07["product_ids"] == reranked[:1], history
    assert c07["task_state"]["active_collection"] == c06["task_state"]["active_collection"], history

    c08 = turn("C08", "和原来比怎么样？", session_id)
    assert c08["simulation"]["operation"] == "COMPARE_SCENARIO", history

    c09 = turn("C09", "撤销刚才那个调整。", session_id)
    assert c09["simulation"]["operation"] in {"RESET_SCENARIO", "REMOVE_OVERRIDE"}, history


def test_act16_existing_scenario_yields_to_new_canonical_focus(deterministic_client):
    headers, _ = seed(deterministic_client, "act16-existing-scenario-focus")
    r1 = ask(deterministic_client, headers, "分析当前候选池，选三个值得继续研究的商品")
    session_id = r1["session_id"]
    r1_collection = r1["task_state"]["active_collection"]
    s1 = ask(deterministic_client, headers, "假设第一名采购成本降低8%", session_id=session_id)
    s1_id = s1["simulation"]["scenario_id"]
    assert s1["task_state"]["conversation_focus"]["focus_type"] == "SCENARIO_RESULT"

    r2 = ask(deterministic_client, headers, "重新看当前候选，挑三个重点的", session_id=session_id)
    r2_collection = r2["task_state"]["active_collection"]
    r2_order = r2_collection["top_product_ids"]
    assert r2_collection["result_id"] != r1_collection["result_id"]
    assert r2["task_state"]["conversation_focus"]["focus_type"] == "CANONICAL_ORDERED_RESULT"
    assert r2["task_state"]["conversation_focus"]["focus_id"] == r2_collection["result_id"]

    first = ask(deterministic_client, headers, "榜首是谁", session_id=session_id)
    assert first["intent"] == "collection_analysis"
    assert first["product_ids"] == r2_order[:1]
    assert first["understanding"]["operation"] == "LOOKUP_ORDINAL_MEMBER"

    why = ask(deterministic_client, headers, "它为何位居榜首", session_id=session_id)
    assert why["product_ids"] == r2_order[:1]
    assert why["understanding"]["operation"] == "EXPLAIN_RANKING"
    candidates = why["collection_analysis"]["candidates"]
    assert [item["product_id"] for item in candidates[:2]] == r2_order[:2]
    assert candidates[0]["ranking_reasons"] and candidates[1]["ranking_reasons"]
    assert candidates[0]["title"] in why["answer"] and candidates[1]["title"] in why["answer"]

    second = ask(deterministic_client, headers, "第二个怎么样", session_id=session_id)
    assert second["product_ids"] == r2_order[1:2]
    pair = ask(deterministic_client, headers, "前俩比较下", session_id=session_id)
    assert pair["product_ids"] == r2_order[:2]
    assert pair["understanding"]["operation"] == "COMPARE_COLLECTION_MEMBERS"

    r3 = ask(deterministic_client, headers, "按利润重新排", session_id=session_id)
    r3_collection = r3["task_state"]["active_collection"]
    r3_order = r3_collection["top_product_ids"]
    assert r3_collection["result_id"] != r2_collection["result_id"]
    assert r3["task_state"]["conversation_focus"]["focus_id"] == r3_collection["result_id"]
    current = ask(deterministic_client, headers, "现在排第一的是谁", session_id=session_id)
    assert current["product_ids"] == r3_order[:1]
    assert current["fallback_reason"] != "STALE_COLLECTION_BASELINE"

    s2 = ask(deterministic_client, headers, "假设榜首采购价降8%", session_id=session_id)
    assert s2["simulation"]["operation"] == "CREATE_SCENARIO"
    assert s2["simulation"]["scenario_id"] != s1_id
    assert s2["product_ids"] == r3_order[:1]
    assert s2["task_state"]["conversation_focus"]["focus_type"] == "SCENARIO_RESULT"
    sessions = deterministic_client.get("/api/v1/agent/sessions", headers=headers).json()["data"]
    stored = next(item["state"] for item in sessions if item["id"] == session_id)
    assert stored["scenario_state"]["baseline_result_id"] == r3_collection["result_id"]
    assert any(item["scenario_id"] == s1_id for item in stored["scenario_history"])

    compared = ask(deterministic_client, headers, "和原来的情况比呢", session_id=session_id)
    assert compared["simulation"]["operation"] == "COMPARE_SCENARIO"
    undone = ask(deterministic_client, headers, "撤销刚才的调整", session_id=session_id)
    assert undone["simulation"]["operation"] in {"RESET_SCENARIO", "REMOVE_OVERRIDE"}
    assert undone["task_state"]["conversation_focus"]["focus_type"] == "CANONICAL_ORDERED_RESULT"
    final = ask(deterministic_client, headers, "现在第一名是谁", session_id=session_id)
    assert final["product_ids"] == r3_order[:1]
    assert final["intent"] == "collection_analysis"


@pytest.mark.parametrize(("query", "expected"), [
    ("第二个是谁", [2]),
    ("前两个是谁", [1, 2]),
    ("前俩比较下", [1, 2]),
    ("第二和第三比较", [2, 3]),
    ("前三名是谁", [1, 2, 3]),
])
def test_act16_rank_range_and_single_ordinal_are_distinct(deterministic_client, query, expected):
    headers, _ = seed(deterministic_client, "act16-range-" + str(abs(hash(query))))
    created = ask(deterministic_client, headers, "当前候选池里选出三个重点商品")
    ordered = created["task_state"]["active_collection"]["top_product_ids"]
    result = ask(deterministic_client, headers, query, session_id=created["session_id"])
    assert result["product_ids"] == [ordered[index - 1] for index in expected]


@pytest.mark.parametrize(("query", "expected"), [
    ("当前候选池前三个给我看看", 3),
    ("当前候选池最值得继续看的三个商品", 3),
    ("当前候选池最值得继续看的五个商品", 5),
])
def test_visible_top_k_variants(deterministic_client, query, expected):
    headers, _ = seed(deterministic_client, "ordered-top-k-" + str(abs(hash(query))))
    result = ask(deterministic_client, headers, query)
    assert result["displayed_count"] == expected
    assert len(result["products"]) == expected
    assert _ordered(result) == result["product_ids"]
    assert result["task_state"]["active_collection"]["top_k"] == expected


def test_selection_recommendation_materializes_visible_order_without_losing_highlight():
    recommendation = {
        "selected_product_id": "p2", "ordered_candidates": ["p2", "p1", "p3"],
        "candidate_product_ids": ["p2", "p1", "p3"],
    }
    state = evolve_task_state(
        TaskState(), intent="selection_recommendation", operation="CREATE",
        turn_index=1, product_ids=["p2", "p1", "p3"], filters={},
        dimensions=["recommendation"], task_completed=True,
        recommendation=recommendation, session_id="s1", requested_top_k=3,
        scope_type="candidate_pool",
    )
    ordered = state.active_result_set
    assert ordered is not None
    assert ordered.product_ids == ["p2", "p1", "p3"]
    assert ordered.requested_top_k == ordered.effective_top_k == 3
    assert ordered.session_id == "s1" and ordered.result_id
    assert state.active_recommendation.selected_product_id == "p2"

    snapshot = ContextSnapshot(session_id="s1", company_id="c", user_id="u", current_query="第二名是谁", task_state=state)
    understanding = QueryUnderstanding(intent="product_detail", confidence=1, route="DETERMINISTIC_FAST_PATH")
    resolved = resolve_context_reference("第二名是谁", understanding, [], snapshot)
    assert resolved.product_ids == ["p1"] and resolved.source == "ordinal_reference"
    explicit = resolve_context_reference("p3 第二名是谁", understanding, ["p3"], snapshot)
    assert explicit.product_ids == ["p3"] and explicit.source == "explicit_query"
    mutation = task_mutation_intent("换成按净利润排", state)
    assert mutation is not None
    parsed, updates = mutation
    assert parsed.name == "collection_analysis"
    assert updates["collection_reference"] == "active_result_set"
    assert updates["operation"] == "RERANK_COLLECTION"


def test_ordinal_scenario_without_order_reports_missing_reference(deterministic_client):
    headers, _ = seed(deterministic_client, "ordered-scenario-no-context")
    result = ask(deterministic_client, headers, "假设第一名采购成本降8%")
    assert result["response_type"] == "clarification"
    assert result["tool_call_count"] == 0
    assert "字段" not in result["answer"]
    assert "排名" in result["answer"] or "目标商品" in result["answer"]


def test_provider_generic_recommendation_is_anchored_to_explicit_collection_top_k():
    provider_frame = QueryUnderstanding(
        intent="selection_recommendation", requested_dimensions=["recommendation"],
        confidence=0.9, route="SEMANTIC_PLANNER",
    )
    anchored = _anchor_collection_semantics(
        "帮我分析当前候选池，先找三个值得继续看的商品", provider_frame,
    )
    assert anchored.intent == "collection_analysis"
    assert anchored.operation == "RECOMMEND_TOP_K"
    assert anchored.top_k == 3
    assert _anchor_collection_semantics(
        "从公司商品库筛出净利率高于30%的商品", provider_frame,
    ).intent == "selection_recommendation"


def test_ordered_result_paraphrase_followups(deterministic_client):
    headers, _ = seed(deterministic_client, "ordered-paraphrase-chain")
    created = ask(deterministic_client, headers, "当前候选池前三个给我看看")
    session_id = created["session_id"]
    original = _ordered(created)
    assert len(original) == 3
    first = ask(deterministic_client, headers, "最前面的那个是谁", session_id=session_id)
    second = ask(deterministic_client, headers, "排第二的呢", session_id=session_id)
    pair = ask(deterministic_client, headers, "头两个对比一下", session_id=session_id)
    assert first["product_ids"] == original[:1]
    assert second["product_ids"] == original[1:2]
    assert pair["product_ids"] == original[:2]
    reranked = ask(deterministic_client, headers, "换成按净利润排", session_id=session_id)
    current = _ordered(reranked)
    assert len(current) == 3 and set(current) == set(original)
    scenario = ask(deterministic_client, headers, "榜首采购成本少8%会怎样", session_id=session_id)
    assert scenario["simulation"]["operation"] == "CREATE_SCENARIO"
    assert scenario["product_ids"] == current[:1]
    compared = ask(deterministic_client, headers, "跟原始情况比呢", session_id=session_id)
    assert compared["simulation"]["operation"] == "COMPARE_SCENARIO"
    removed = ask(deterministic_client, headers, "把这个假设撤掉", session_id=session_id)
    assert removed["simulation"]["operation"] in {"RESET_SCENARIO", "REMOVE_OVERRIDE"}


def test_explicit_scenario_and_canonical_focus_references(deterministic_client):
    headers, _ = seed(deterministic_client, "ordered-explicit-focus-reference")
    canonical = ask(deterministic_client, headers, "当前候选池前三个给我看看")
    session_id = canonical["session_id"]
    canonical_ids = _ordered(canonical)

    scenario = ask(
        deterministic_client, headers, "榜首采购成本少8%会怎样", session_id=session_id,
    )
    assert scenario["task_state"]["conversation_focus"]["focus_type"] == "SCENARIO_RESULT"

    scenario_view = ask(
        deterministic_client, headers, "回到刚才假设看看", session_id=session_id,
    )
    assert scenario_view["intent"] == "scenario_analysis"
    assert scenario_view["simulation"]["operation"] == "EXPLAIN_SCENARIO"

    canonical_view = ask(
        deterministic_client, headers, "回到当前候选", session_id=session_id,
    )
    assert canonical_view["intent"] == "collection_analysis"
    assert _ordered(canonical_view) == canonical_ids
    assert canonical_view["task_state"]["conversation_focus"]["focus_type"] == "CANONICAL_ORDERED_RESULT"


def test_ambiguous_cost_field_does_not_invent_procurement_override(deterministic_client):
    headers, _ = seed(deterministic_client, "ordered-ambiguous-cost")
    created = ask(deterministic_client, headers, "当前候选池前三个给我看看")
    result = ask(deterministic_client, headers, "榜首成本少8%会怎样", session_id=created["session_id"])
    assert result["response_type"] == "clarification"
    assert result["simulation"] is None


def test_semantic_contrasts_preserve_status_and_explicit_catalog_scope(deterministic_client):
    headers, _ = seed(deterministic_client, "ordered-semantic-contrasts")
    created = ask(deterministic_client, headers, "当前这些候选里挑三个最值得继续看的")
    session_id = created["session_id"]
    ordered = _ordered(created)

    review = ask(deterministic_client, headers, "第一名审核通过了吗", session_id=session_id)
    assert review["understanding"]["operation"] != "EXPLAIN_RANKING"
    assert review["product_ids"] in (ordered[:1], [])

    headers, _ = seed(deterministic_client, "ordered-scope-contrasts")
    created = ask(deterministic_client, headers, "当前这些候选里挑三个最值得继续看的")
    session_id = created["session_id"]
    ordered = _ordered(created)
    assert len(ordered) == 3, created
    scoped = ask(deterministic_client, headers, "只看利润呢", session_id=session_id)
    assert len(_ordered(scoped)) == 3
    assert set(_ordered(scoped)) == set(ordered)

    expanded = ask(deterministic_client, headers, "重新从全库按利润找三个", session_id=session_id)
    assert expanded["understanding"]["collection_reference"] == "company_catalog"
    assert expanded["understanding"]["top_k"] == 3


def test_fresh_session_collection_establishment_is_deterministic_across_provider_variance(
    deterministic_client, monkeypatch,
):
    frames = []
    variants = [
        _provider_frame(
            "collection_analysis", operation="RECOMMEND_TOP_K",
            collection_reference="candidate_pool", top_k=3,
            planned_tools=["analyze_collection"],
        ),
        _provider_frame(),
        _provider_frame(
            "unknown", requested_dimensions=[], clarification_required=True,
            clarification_reason="当前会话没有可引用的候选集合。", planned_tools=[],
        ),
        _provider_frame(
            "collection_analysis", operation="ANALYZE_COLLECTION",
            collection_reference="current_collection", requires_context=True,
            requested_dimensions=[], planned_tools=["analyze_collection"],
        ),
        _provider_frame(planned_tools=["suggest_collection_view"]),
    ]
    for _ in range(4):
        frames.extend(variants)
    provider = _install_semantic_frames(monkeypatch, frames)

    results = []
    for index in range(20):
        headers, _ = seed(deterministic_client, f"collection-establish-repeat-{index}")
        result = ask(deterministic_client, headers, "当前候选里先找三个值得继续看的。")
        results.append(result)

    assert provider.calls == 20
    assert all(result["intent"] == "collection_analysis" for result in results)
    assert all(result["response_type"] == "collection_report" for result in results)
    assert all(result["tool_call_count"] == 1 for result in results)
    assert all(result["understanding"]["operation"] == "RECOMMEND_TOP_K" for result in results)
    assert all(result["understanding"]["collection_reference"] == "candidate_pool" for result in results)
    assert all(result["understanding"]["top_k"] == 3 for result in results)
    assert all(len(result["products"]) == 3 for result in results)
    assert all(result["task_state"]["active_collection"] for result in results)


def test_collection_establishment_paraphrases_share_one_core_contract(
    deterministic_client, monkeypatch,
):
    queries = [
        "当前候选里先找三个值得继续看的。",
        "这批候选挑三个最值得研究的。",
        "候选商品里给我选 Top3。",
        "从当前这批里找三个优先分析的。",
        "先从候选池挑三个重点看的。",
        "这些候选里哪三个最值得继续研究？",
        "当前这些商品先排前三个。",
        "先挑三个优先级最高的候选。",
        "从现在的候选里给三个重点方向。",
        "当前候选 Top3 是哪些？",
    ]
    provider = _install_semantic_frames(monkeypatch, [_provider_frame() for _ in queries])
    results = []
    for index, query in enumerate(queries):
        headers, _ = seed(deterministic_client, f"collection-establish-paraphrase-{index}")
        results.append(ask(deterministic_client, headers, query))

    assert provider.calls == len(queries)
    assert all(result["intent"] == "collection_analysis" for result in results)
    assert all(result["understanding"]["operation"] == "RECOMMEND_TOP_K" for result in results)
    assert all(result["understanding"]["collection_reference"] == "candidate_pool" for result in results)
    assert all(result["understanding"]["top_k"] == 3 for result in results)
    assert all(result["tool_call_count"] == 1 for result in results)


def test_collection_establishment_negative_contrasts_remain_distinct(
    deterministic_client, monkeypatch,
):
    frames = [
        _provider_frame(),
        _provider_frame(),
        _provider_frame(
            "unknown", requested_dimensions=[], clarification_required=True,
            clarification_reason="缺少候选范围与数量。", planned_tools=[],
        ),
    ]
    provider = _install_semantic_frames(monkeypatch, frames)

    headers, _ = seed(deterministic_client, "collection-establish-negative-ordinal")
    ordinal = ask(deterministic_client, headers, "第一名是谁？")
    assert ordinal["response_type"] == "clarification"
    assert not ordinal["task_state"]["active_collection"]

    headers, _ = seed(deterministic_client, "collection-establish-negative-catalog")
    catalog = ask(deterministic_client, headers, "重新从企业全库找三个利润最高的。")
    assert catalog["response_type"] != "clarification"
    assert catalog["understanding"]["collection_reference"] == "company_catalog"

    headers, _ = seed(deterministic_client, "collection-establish-negative-profit")
    profit = ask(deterministic_client, headers, "当前候选里利润最高的是谁？")
    assert profit["response_type"] != "clarification"
    assert profit["understanding"]["collection_reference"] == "candidate_pool"

    headers, _ = seed(deterministic_client, "collection-establish-negative-vague")
    vague = ask(deterministic_client, headers, "帮我选几个。")
    assert vague["response_type"] == "clarification"
    assert not vague["task_state"]["active_collection"]
    assert provider.calls == 3
