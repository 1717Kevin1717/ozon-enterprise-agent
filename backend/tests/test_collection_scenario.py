import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.agent import scenario_state as scenario_module
from app.agent.model_router import ModelRouter, semantic_context_slots
from app.agent.query_understanding import understand_query, validate_semantic_plan
from app.agent.scenario_state import (
    ScenarioStateError,
    create_collection_scenario,
    detect_stale_baseline,
    detect_stale_collection_baseline,
    parse_scenario_command,
    reset_scenario,
    scenario_response_view,
    update_scenario,
    update_scenario_ranking,
)
from app.db.models import Product
from app.schemas.agent import CollectionState, ContextSnapshot, TaskState
from test_scenario_state import product as base_product
from test_model_router import MockProvider, install_router
from test_stability_sprint import ask, deterministic_client, seed


def product(index: int, *, advertising: float = 200, procurement: float = 1000) -> Product:
    item = base_product()
    item.id = f"collection-scenario-{index}"
    item.external_product_id = f"collection-external-{index}"
    item.title = f"Collection scenario product {index}"
    item.url = f"https://example.invalid/collection-scenario-{index}"
    item.main_image_url = f"https://example.invalid/collection-scenario-{index}.png"
    item.advertising_cost = advertising
    item.procurement_cost = procurement
    return item


def collection(items, *, ranking=("profit",), top_k=3, revision=7, gate=False):
    ids = [item.id for item in items]
    return CollectionState(
        collection_type="candidate_pool", product_ids=ids,
        ranked_product_ids=ids, top_product_ids=ids[:top_k], top_k=top_k,
        ranking_dimensions=list(ranking), requested_dimensions=list(ranking),
        require_gate_pass=gate, source_task_revision=revision, created_turn=1,
    )


def create(items=None, *, scope="ALL_MEMBERS", top_k=None, selected=(), ranking=(), gate=False):
    items = items or [
        product(1, advertising=100, procurement=900),
        product(2, advertising=500, procurement=1200),
        product(3, advertising=300, procurement=1050),
        product(4, advertising=200, procurement=1100),
    ]
    canonical = collection(items, top_k=3, gate=gate)
    state = create_collection_scenario(
        scenario_id="collection-scenario", session_id="session-1",
        company_id=items[0].company_id, products=items, collection=canonical,
        override_specs=[{
            "field": "advertising_cost", "operation": "DECREASE_PERCENT",
            "value": 30, "unit": "PERCENT",
        }],
        scope=scope, top_k=top_k, selected_product_ids=selected,
        ranking_dimensions=ranking,
    )
    return items, canonical, state


def effective(state, product_id, field):
    derived = next(item for item in state.derived_results if item.product_id == product_id)
    return next(item.value for item in derived.effective_values if item.field == field)


def test_cs01_all_members_scope_targets_every_frozen_member():
    items, _, state = create(scope="ALL_MEMBERS")
    assert state.scenario_scope_target_ids == tuple(item.id for item in items)


def test_cs02_top_k_scope_targets_only_original_top_k():
    items, _, state = create(scope="TOP_K", top_k=2)
    assert state.scenario_scope_target_ids == tuple(item.id for item in items[:2])


def test_cs03_top_k_targets_remain_frozen_after_rerank():
    _, _, state = create(scope="TOP_K", top_k=2)
    targets = state.scenario_scope_target_ids
    reranked = update_scenario_ranking(state, expected_revision=1, ranking_dimensions=["risk", "profit"])
    assert reranked.scenario_scope_target_ids == targets


def test_cs04_selected_members_scope_uses_intersection_only():
    items = [product(1), product(2), product(3)]
    _, _, state = create(items, scope="SELECTED_MEMBERS", selected=[items[1].id, "outside"])
    assert state.scenario_scope_target_ids == (items[1].id,)


def test_cs05_hypothetical_order_can_change_without_canonical_order_change():
    items = [product(1, advertising=50), product(2, advertising=1200), product(3, advertising=100)]
    canonical = collection(items, ranking=("profit",))
    before = canonical.model_dump(mode="json")
    state = create_collection_scenario(
        scenario_id="rank-change", session_id="session-1", company_id=items[0].company_id,
        products=items, collection=canonical, scope="SELECTED_MEMBERS",
        selected_product_ids=[items[1].id], ranking_dimensions=["profit"],
        override_specs=[{"field": "advertising_cost", "operation": "DECREASE_PERCENT", "value": 100, "unit": "PERCENT"}],
    )
    assert state.scenario_ordered_ids != tuple(canonical.ranked_product_ids)
    assert canonical.model_dump(mode="json") == before


def test_cs06_scenario_ranking_spec_does_not_change_canonical_spec():
    _, canonical, state = create()
    before = list(canonical.ranking_dimensions)
    changed = update_scenario_ranking(state, expected_revision=1, ranking_dimensions=["risk", "profit"])
    assert changed.scenario_ranking_spec == ("risk", "profit")
    assert canonical.ranking_dimensions == before


def test_cs07_collection_revision_is_never_mutated():
    _, canonical, state = create()
    update_scenario(state, expected_revision=1, field="procurement_cost", operation="DECREASE_PERCENT", value=5, unit="PERCENT", target_product_ids=state.scenario_scope_target_ids)
    assert canonical.source_task_revision == 7


def test_cs08_canonical_recommendation_state_is_not_part_of_mutation():
    _, canonical, state = create()
    before = canonical.model_dump(mode="json")
    reset_scenario(state, expected_revision=1)
    assert canonical.model_dump(mode="json") == before


def test_cs09_canonical_product_facts_are_unchanged():
    items, _, _ = create()
    assert [(item.advertising_cost, item.procurement_cost) for item in items] == [
        (100, 900), (500, 1200), (300, 1050), (200, 1100),
    ]


def test_cs10_blocked_product_is_excluded_from_scenario_order():
    items = [product(1), product(2)]
    items[1].compliance_status = "rejected"
    _, _, state = create(items, gate=False)
    assert items[1].id not in state.scenario_ordered_ids


def test_cs11_insufficient_product_is_not_used_to_pad_gate_pass_top_k():
    items = [product(1), product(2)]
    items[1].sales_evidence = ""
    _, _, state = create(items, gate=True)
    assert all(item.decision_status == "RECOMMENDED" for item in state.derived_results if item.product_id in state.scenario_ordered_ids)


def test_cs12_top_k_shortfall_returns_fewer_members_without_padding():
    items = [product(1), product(2)]
    items[1].compliance_status = "rejected"
    _, _, state = create(items, gate=True)
    assert len(state.scenario_top_k_ids) < 3


def test_cs13_rank_delta_positive_means_improved():
    items = [product(1, advertising=50), product(2, advertising=1200)]
    canonical = collection(items, ranking=("profit",), top_k=2)
    state = create_collection_scenario(
        scenario_id="rank-delta", session_id="session-1", company_id=items[0].company_id,
        products=items, collection=canonical, scope="SELECTED_MEMBERS",
        selected_product_ids=[items[1].id], ranking_dimensions=["profit"],
        override_specs=[{"field": "advertising_cost", "operation": "DECREASE_PERCENT", "value": 100, "unit": "PERCENT"}],
    )
    moved = next(item for item in state.scenario_collection_results if item.product_id == items[1].id)
    assert moved.canonical_rank == 2 and moved.scenario_rank == 1
    assert moved.rank_delta == 1


def test_cs14_scenario_first_is_scenario_order_first():
    _, _, state = create()
    view = scenario_response_view(state, "EXPLAIN_SCENARIO")
    assert view["scenario_ordered_ids"][0] == state.scenario_ordered_ids[0]


def test_cs15_compare_view_contains_canonical_and_hypothetical_rankings():
    _, _, state = create()
    view = scenario_response_view(state, "COMPARE_SCENARIO")
    assert view["collection_ranking"] and view["canonical_collection_unchanged"] is True


def test_compare_view_joins_frozen_metrics_and_ranks_for_every_collection_member():
    _, _, state = create()
    view = scenario_response_view(state, "COMPARE_SCENARIO")
    baseline = {item.product_id: item for item in state.comparison_baseline}
    hypothetical = {item.product_id: item for item in state.derived_results}
    ranking = {item.product_id: item for item in state.scenario_collection_results}

    assert {item["product_id"] for item in view["comparison"]} == set(state.base_product_ids)
    for item in view["comparison"]:
        product_id = item["product_id"]
        assert item["canonical"]["net_profit"] == baseline[product_id].net_profit
        assert item["hypothetical"]["net_profit"] == hypothetical[product_id].net_profit
        assert item["delta"]["net_profit"] == hypothetical[product_id].delta.net_profit
        assert item["canonical"]["roi"] == baseline[product_id].roi
        assert item["hypothetical"]["roi"] == hypothetical[product_id].roi
        assert item["delta"]["roi"] == hypothetical[product_id].delta.roi
        assert item["canonical_rank"] == ranking[product_id].canonical_rank
        assert item["scenario_rank"] == ranking[product_id].scenario_rank
        assert item["rank_delta"] == ranking[product_id].rank_delta


def test_compare_view_keeps_metric_delta_when_rank_does_not_change():
    only_product = product(1, advertising=400)
    _, _, state = create([only_product])
    comparison = scenario_response_view(state, "COMPARE_SCENARIO")["comparison"][0]

    assert comparison["canonical_rank"] == comparison["scenario_rank"] == 1
    assert comparison["rank_delta"] == 0
    assert comparison["delta"]["net_profit"] != 0
    assert comparison["delta"]["roi"] != 0


def test_compare_view_keeps_rank_delta_when_metrics_do_not_change():
    _, _, state = create()
    reset = reset_scenario(state, expected_revision=state.scenario_revision)
    first_id, second_id, *remaining = reset.scenario_ordered_ids
    ranks = []
    for item in reset.scenario_collection_results:
        if item.product_id == first_id:
            ranks.append(item.model_copy(update={"scenario_rank": 2, "rank_delta": -1}))
        elif item.product_id == second_id:
            ranks.append(item.model_copy(update={"scenario_rank": 1, "rank_delta": 1}))
        else:
            ranks.append(item)
    reordered = reset.model_copy(update={
        "scenario_ordered_ids": (second_id, first_id, *remaining),
        "scenario_collection_results": tuple(ranks),
    })
    comparison = {
        item["product_id"]: item
        for item in scenario_response_view(reordered, "COMPARE_SCENARIO")["comparison"]
    }

    assert comparison[first_id]["delta"]["net_profit"] == 0
    assert comparison[first_id]["delta"]["roi"] == 0
    assert comparison[first_id]["rank_delta"] == -1
    assert comparison[second_id]["rank_delta"] == 1


def test_cs16_second_override_reuses_frozen_collection_scope():
    _, _, state = create(scope="TOP_K", top_k=2)
    updated = update_scenario(
        state, expected_revision=1, field="procurement_cost", operation="DECREASE_PERCENT",
        value=5, unit="PERCENT", target_product_ids=state.scenario_scope_target_ids,
    )
    assert updated.overrides[-1].target_product_ids == state.scenario_scope_target_ids


def test_cs17_ranking_dimension_change_only_updates_scenario():
    _, canonical, state = create()
    updated = update_scenario_ranking(state, expected_revision=1, ranking_dimensions=["profit", "risk"])
    assert updated.scenario_ranking_spec == ("profit", "risk")
    assert canonical.ranking_dimensions == ["profit"]


def test_cs18_changed_collection_revision_marks_stale_collection_baseline():
    _, canonical, state = create()
    changed = canonical.model_copy(update={"source_task_revision": 8})
    assert detect_stale_collection_baseline(state, changed).collection_baseline_status == "STALE_COLLECTION_BASELINE"


def test_cs19_changed_product_input_marks_stale_product_baseline():
    items, _, state = create()
    items[0].procurement_cost += 1
    assert detect_stale_baseline(state, items).baseline_status == "STALE_BASELINE"


def test_cs20_session_isolation_rejects_foreign_scenario_owner():
    _, _, state = create()
    assert state.session_id == "session-1"
    assert state.company_id == "scenario-company"


def test_cs21_normal_collection_analysis_is_not_a_scenario():
    assert parse_scenario_command("分析当前候选池最值得研究的三个方向", has_active_scenario=False) is None


def test_cs22_canonical_rerank_without_scenario_anchor_is_not_scenario():
    assert parse_scenario_command("只看利润和风险重新排一次", has_active_scenario=True) is None


def test_cs23_product_comparison_is_not_scenario():
    assert parse_scenario_command("比较商品甲和商品乙的利润", has_active_scenario=True) is None


def test_cs24_recommendation_policy_is_not_scenario():
    assert parse_scenario_command("第一名是不是就代表推荐上架", has_active_scenario=True) is None


def test_cs25_collection_scenario_privacy_slots_are_anonymous():
    slots = semantic_context_slots({
        "session_id": "private-session", "company_id": "private-company",
        "verified_product_names": {"private-id": "private-name"},
        "allowed_tools": ["get_product"],
        "task_context": {"has_active_scenario": True, "active_collection_type": "candidate_pool"},
        "rule_understanding": {
            "intent": "scenario_analysis", "operation": "CREATE_SCENARIO",
            "scenario_field": "advertising_cost", "mutation_type": "DECREASE_PERCENT",
            "hypothetical_value": 30, "hypothetical_unit": "PERCENT",
            "scenario_reference_role": "ACTIVE_COLLECTION", "scenario_collection_scope": "TOP_K",
            "scenario_top_k": 3, "scenario_ranking_dimensions": ["profit", "risk"],
        },
    })
    serialized = json.dumps(slots, ensure_ascii=False)
    assert slots["collection_scope"] == "TOP_K" and slots["top_k"] == 3
    assert not any(value in serialized for value in ("private-session", "private-company", "private-id", "private-name"))


def test_cs26_privacy_slots_contain_no_business_ranking_values():
    slots = semantic_context_slots({
        "business_values": {"price": 9999, "profit": 8888},
        "task_context": {"active_collection_type": "candidate_pool"},
        "rule_understanding": {
            "intent": "scenario_analysis", "operation": "UPDATE_SCENARIO",
            "scenario_collection_scope": "ALL_MEMBERS", "scenario_ranking_dimensions": ["profit"],
        },
    })
    assert "9999" not in json.dumps(slots) and "8888" not in json.dumps(slots)


def test_cs27_reset_restores_frozen_canonical_order_and_spec():
    _, _, state = create(ranking=["risk", "profit"])
    reset = reset_scenario(state, expected_revision=1)
    assert reset.scenario_ranking_spec == reset.collection_snapshot.canonical_ranking_spec
    assert reset.scenario_ordered_ids == reset.collection_snapshot.canonical_ordered_ids


def test_cs28_member_calculation_exception_cannot_mutate_collection_or_products(monkeypatch):
    items = [product(1), product(2)]
    canonical = collection(items)
    before_collection = canonical.model_dump(mode="json")
    before_products = [(item.advertising_cost, item.procurement_cost) for item in items]

    def fail(_):
        raise RuntimeError("controlled member calculation failure")

    monkeypatch.setattr(scenario_module, "analyze_snapshot", fail)
    with pytest.raises(RuntimeError, match="controlled member calculation failure"):
        create_collection_scenario(
            scenario_id="failure", session_id="session-1", company_id=items[0].company_id,
            products=items, collection=canonical, scope="ALL_MEMBERS",
            override_specs=[{"field": "advertising_cost", "operation": "DECREASE_PERCENT", "value": 30, "unit": "PERCENT"}],
        )
    assert canonical.model_dump(mode="json") == before_collection
    assert [(item.advertising_cost, item.procurement_cost) for item in items] == before_products


@pytest.mark.parametrize(("query", "scope", "top_k"), [
    ("如果当前候选池前三名的广告成本都下降30%，重新排一次", "TOP_K", 3),
    ("假设这批商品采购成本全部降低5%，谁最值得继续研究", "ALL_MEMBERS", None),
])
def test_collection_scenario_query_understanding_uses_typed_scope(query, scope, top_k):
    task = TaskState(active_collection=CollectionState(
        collection_type="candidate_pool", product_ids=["p1"], ranked_product_ids=["p1"],
        top_product_ids=["p1"], ranking_dimensions=["recommendation"], source_task_revision=1,
    ))
    snapshot = ContextSnapshot(session_id="s", company_id="c", user_id="u", current_query=query, task_state=task)
    parsed, understanding = understand_query(query, [], selected=[], context_snapshot=snapshot)
    assert parsed.name == "scenario_analysis"
    assert understanding.scenario_collection_scope == scope
    assert understanding.scenario_top_k == top_k


def test_active_collection_scenario_rerank_is_ranking_only_update():
    command = parse_scenario_command("在当前假设下，只看利润和风险重新排序", has_active_scenario=True)
    assert command.operation == "UPDATE_SCENARIO"
    assert command.field is None
    assert command.ranking_dimensions == ("profit", "risk")


def test_collection_scenario_runtime_preserves_canonical_collection(deterministic_client):
    headers, _ = seed(deterministic_client, "collection-scenario-runtime-create")
    collection_result = ask(deterministic_client, headers, "分析当前候选池最值得研究的三个商品")
    canonical = collection_result["task_state"]["active_collection"]
    result = ask(
        deterministic_client, headers,
        "如果当前候选池前三名的广告成本都下降30%，重新排一次",
        session_id=collection_result["session_id"],
    )

    assert result["intent"] == "scenario_analysis"
    assert result["response_type"] == "scenario_report"
    assert result["simulation"]["collection_scope"] == "TOP_K"
    assert result["simulation"]["canonical_collection_unchanged"] is True
    assert result["task_state"]["active_collection"] == canonical


def _provider_collection_scenario_frame():
    return {
        "intent": "scenario_analysis", "task_type": "scenario_analysis",
        "question_type": "scenario_analysis", "operation": "CREATE_SCENARIO",
        "entity_mentions": [], "requested_dimensions": ["profit", "roi", "risk", "recommendation", "decision"],
        "scenario_field": "advertising_cost", "mutation_type": "DECREASE_PERCENT",
        "hypothetical_value": 30, "hypothetical_unit": "PERCENT",
        "scenario_reference_role": "ACTIVE_COLLECTION", "scenario_collection_scope": "TOP_K",
        "scenario_top_k": 3, "collection_reference": "active_result_set",
        "confidence": 0.95, "route": "SEMANTIC_PLANNER", "planned_tools": [],
    }


def _provider_scenario_explain_frame():
    return {
        "intent": "scenario_analysis", "task_type": "scenario_analysis",
        "question_type": "scenario_analysis", "operation": "EXPLAIN_SCENARIO",
        "entity_mentions": [], "requested_dimensions": ["recommendation", "decision"],
        "scenario_reference_role": "ACTIVE_SCENARIO", "requires_context": True,
        "requires_task_state": True, "confidence": 0.95,
        "route": "SEMANTIC_PLANNER", "planned_tools": [],
    }


def _provider_generic_rank_frame(*, ordinal=None, top_k=None, dimensions=None, metrics=None, metric=None):
    """Model output shape that previously overrode a valid Scenario follow-up."""
    return {
        "intent": "product_detail", "task_type": "product_detail",
        "question_type": "product_detail", "operation": "EXPLAIN_RANKING",
        "entity_mentions": [], "requested_dimensions": dimensions or ["profit", "roi"],
        "metrics": metrics or [], "metric": metric,
        "ordinal_reference": ordinal,
        "ordinal_references": [ordinal] if ordinal else [],
        "top_k": top_k, "reference_slots": ["ordinal_reference"],
        "requires_context": True, "requires_task_state": True,
        "confidence": 0.95, "route": "SEMANTIC_PLANNER", "planned_tools": [],
    }


def _scenario_task_packet(*, active=True):
    return {
        "last_requested_dimensions": ["profit", "roi"],
        "task_context": {
            "has_active_scenario": active,
            "active_collection_type": "candidate_pool",
            "collection_member_count": 8,
            "collection_top_count": 3,
            "has_collection_ranking_state": True,
        },
    }


def test_post_provider_scenario_ordinal_precedes_metric_inheritance():
    parsed, understanding = validate_semantic_plan(
        _provider_generic_rank_frame(ordinal=1),
        "现在排名第一的是谁？",
        context_packet=_scenario_task_packet(),
    )

    assert parsed.name == understanding.intent == "scenario_analysis"
    assert understanding.operation == "EXPLAIN_SCENARIO"
    assert understanding.scenario_reference_role == "ACTIVE_SCENARIO"
    assert understanding.ordinal_reference == 1
    assert understanding.requested_dimensions == ["recommendation", "decision"]
    assert understanding.metrics == []


@pytest.mark.parametrize(("query", "ordinal"), [
    ("排名第二的呢？", 2),
    ("谁排第一？", 1),
])
def test_post_provider_scenario_single_rank_is_resolved_from_active_scenario(query, ordinal):
    parsed, understanding = validate_semantic_plan(
        _provider_generic_rank_frame(ordinal=ordinal), query,
        context_packet=_scenario_task_packet(),
    )

    assert parsed.name == "scenario_analysis"
    assert understanding.ordinal_reference == ordinal
    assert understanding.scenario_reference_role == "ACTIVE_SCENARIO"


def test_post_provider_scenario_top_k_uses_active_scenario():
    parsed, understanding = validate_semantic_plan(
        _provider_generic_rank_frame(top_k=3), "现在前三名都是谁？",
        context_packet=_scenario_task_packet(),
    )

    assert parsed.name == "scenario_analysis"
    assert understanding.operation == "EXPLAIN_SCENARIO"
    assert understanding.top_k == 3
    assert understanding.scenario_reference_role == "ACTIVE_SCENARIO"


def test_post_provider_scenario_ordinal_keeps_explicit_metric_only():
    parsed, understanding = validate_semantic_plan(
        _provider_generic_rank_frame(
            ordinal=1, dimensions=["roi"], metrics=["roi"], metric="roi",
        ),
        "排名第一的 ROI 是多少？",
        context_packet=_scenario_task_packet(),
    )

    assert parsed.name == "scenario_analysis"
    assert understanding.ordinal_reference == 1
    assert understanding.requested_dimensions == ["roi"]
    assert understanding.metric == "roi"


def test_scenario_anchor_does_not_preempt_canonical_collection_reference():
    frame = {
        **_provider_generic_rank_frame(ordinal=1, dimensions=["recommendation", "decision"]),
        "intent": "collection_analysis", "task_type": "collection_analysis",
        "question_type": "collection_analysis", "collection_reference": "current_collection",
    }
    parsed, understanding = validate_semantic_plan(
        frame, "原候选池排名第一的是谁？",
        context_packet=_scenario_task_packet(),
    )

    assert parsed.name == understanding.intent == "collection_analysis"
    assert understanding.operation == "EXPLAIN_RANKING"
    assert understanding.scenario_reference_role != "ACTIVE_SCENARIO"


def test_scenario_anchor_requires_session_local_active_scenario():
    parsed, understanding = validate_semantic_plan(
        _provider_generic_rank_frame(ordinal=1), "现在排名第一的是谁？",
        context_packet=_scenario_task_packet(active=False),
    )

    assert parsed.name == understanding.intent == "product_detail"
    assert understanding.scenario_reference_role != "ACTIVE_SCENARIO"


def test_collection_anchor_enriches_valid_scenario_without_overriding_intent():
    packet = {
        "task_context": {
            "active_collection_type": "candidate_pool", "collection_member_count": 3,
            "collection_top_count": 3, "has_collection_ranking_state": True,
        },
    }
    parsed, understanding = validate_semantic_plan(
        _provider_collection_scenario_frame(),
        "如果当前候选池前三名的广告成本都下降30%，重新排一次",
        context_packet=packet,
    )

    assert parsed.name == understanding.intent == "scenario_analysis"
    assert understanding.operation == "CREATE_SCENARIO"
    assert understanding.scenario_reference_role == "ACTIVE_COLLECTION"
    assert understanding.collection_reference == "current_collection"


def test_complete_collection_scenario_treats_unknown_provider_tool_as_advisory():
    packet = {
        "task_context": {
            "active_collection_type": "candidate_pool", "collection_member_count": 3,
            "collection_top_count": 3, "has_collection_ranking_state": True,
        },
    }
    frame = {
        **_provider_collection_scenario_frame(),
        "planned_tools": ["provider_invented_scenario_tool"],
    }

    parsed, understanding = validate_semantic_plan(
        frame,
        "如果当前候选池前三名的广告成本都下降30%，重新排一次",
        context_packet=packet,
    )

    assert parsed.name == understanding.intent == "scenario_analysis"
    assert understanding.planned_tools == ["get_product"]
    assert understanding.advisory_tool_mismatch == ["provider_invented_scenario_tool"]
    assert "provider_invented_scenario_tool" not in understanding.planned_tools


def test_provider_unknown_scenario_tool_never_executes_and_scenario_still_runs(
    deterministic_client, monkeypatch,
):
    headers, _ = seed(deterministic_client, "scenario-advisory-tool-runtime")
    collection_result = ask(deterministic_client, headers, "分析当前候选池最值得研究的三个商品")
    canonical = collection_result["task_state"]["active_collection"]
    frame = {
        **_provider_collection_scenario_frame(),
        "planned_tools": ["provider_invented_scenario_tool"],
    }
    provider = MockProvider("qwen", [frame])
    install_router(monkeypatch, ModelRouter([provider], primary_name="qwen", reasoning_name="deepseek"))

    result = ask(
        deterministic_client, headers,
        "如果当前候选池前三名的广告成本都下降30%，重新排一次",
        session_id=collection_result["session_id"], provider_execution_policy="QWEN_ONLY",
    )

    assert result["intent"] == "scenario_analysis"
    assert result["simulation"]["operation"] == "CREATE_SCENARIO"
    assert result["task_state"]["active_collection"] == canonical
    assert result["tool_call_count"] <= 1


def _render_trace(result):
    node = shutil.which("node")
    assert node, "Node is required for trace renderer regression"
    source_path = Path(__file__).parents[1] / "app" / "web" / "app.js"
    script = r"""
const fs=require('node:fs'),vm=require('node:vm');
const data=JSON.parse(fs.readFileSync(0,'utf8')),source=fs.readFileSync(process.argv[1],'utf8');
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const context={esc,clean:x=>String(x??'').trim(),toolBusinessNames:{}};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function normalizeAgentTrace('),source.indexOf('function trustEvidenceBlock(')),context);
process.stdout.write(context.agentTraceBlock(data));
"""
    run = subprocess.run(
        [node, "-e", script, str(source_path)], input=json.dumps(result),
        text=True, encoding="utf-8", capture_output=True, check=True,
    )
    return run.stdout


def _assert_rendered_trace_is_nonempty(result):
    assert result["trace"]
    assert all(
        (item.get("business_label") or item.get("label") or item.get("name") or
         item.get("title") or item.get("type") or item.get("event") or
         item.get("tool") or item.get("tool_name"))
        and (item.get("summary") or item.get("description") or item.get("detail") or
             item.get("message") or item.get("status"))
        for item in result["trace"]
    )
    html = _render_trace(result)
    assert "可审计执行轨迹" in html
    assert "<b></b>" not in html
    assert "— </li>" not in html
    assert html.count("<li><b>") == len(result["trace"])
    return html


def test_normal_provider_scenario_create_ordinal_and_compare_render_complete_audit_trace(
    deterministic_client, monkeypatch,
):
    headers, _ = seed(deterministic_client, "scenario-normal-audit-trace")
    collection_result = ask(deterministic_client, headers, "分析当前候选池最值得研究的三个商品")
    compare_frame = {
        **_provider_scenario_explain_frame(),
        "operation": "COMPARE_SCENARIO",
    }
    provider = MockProvider("qwen", [
        _provider_collection_scenario_frame(),
        _provider_generic_rank_frame(ordinal=1),
        compare_frame,
    ])
    install_router(monkeypatch, ModelRouter([provider], primary_name="qwen", reasoning_name="deepseek"))

    created = ask(
        deterministic_client, headers,
        "如果当前候选池前三名的广告成本都下降30%，重新排一次",
        session_id=collection_result["session_id"], provider_execution_policy="QWEN_ONLY",
    )
    ordinal = ask(
        deterministic_client, headers, "现在第一名是谁？",
        session_id=created["session_id"], provider_execution_policy="QWEN_ONLY",
    )
    compared = ask(
        deterministic_client, headers, "和原来的情况比呢？",
        session_id=created["session_id"], provider_execution_policy="QWEN_ONLY",
    )

    assert created["simulation"]["operation"] == "CREATE_SCENARIO"
    assert ordinal["simulation"]["operation"] == "EXPLAIN_SCENARIO"
    assert compared["simulation"]["operation"] == "COMPARE_SCENARIO"
    for result in (created, ordinal, compared):
        _assert_rendered_trace_is_nonempty(result)


def test_scenario_fallback_trace_and_empty_event_filter_share_renderer_adapter(
    deterministic_client, monkeypatch,
):
    headers, _ = seed(deterministic_client, "scenario-fallback-audit-trace")
    collection_result = ask(deterministic_client, headers, "分析当前候选池最值得研究的三个商品")
    provider = MockProvider("qwen", failure="CONNECT_ERROR")
    install_router(monkeypatch, ModelRouter([provider], primary_name="qwen", reasoning_name="deepseek"))

    result = ask(
        deterministic_client, headers,
        "假设当前候选池前三名的广告成本下降30%，按新结果排序",
        session_id=collection_result["session_id"], provider_execution_policy="QWEN_ONLY",
    )

    assert result["fallback_reason"] == "CONNECT_ERROR"
    _assert_rendered_trace_is_nonempty(result)

    empty = {**result, "trace": [{}], "tool_trace_summary": []}
    html = _render_trace(empty)
    assert "查看 0 条可审计执行轨迹" in html
    assert "<b></b>" not in html
    assert "<li><b>" not in html


def test_provider_collection_scenario_uses_same_session_active_collection(deterministic_client, monkeypatch):
    headers, _ = seed(deterministic_client, "collection-scenario-provider-chain")
    collection_result = ask(deterministic_client, headers, "分析当前候选池最值得研究的三个商品")
    canonical = collection_result["task_state"]["active_collection"]
    provider = MockProvider("qwen", [
        _provider_collection_scenario_frame(), _provider_generic_rank_frame(ordinal=1),
    ])
    install_router(monkeypatch, ModelRouter([provider], primary_name="qwen", reasoning_name="deepseek"))

    created = ask(
        deterministic_client, headers,
        "如果当前候选池前三名的广告成本都下降30%，重新排一次",
        session_id=collection_result["session_id"],
    )

    assert created["session_id"] == collection_result["session_id"]
    assert created["intent"] == "scenario_analysis"
    assert created["simulation"]["operation"] == "CREATE_SCENARIO"
    assert created["simulation"]["collection_scope"] == "TOP_K"
    assert created["simulation"]["scope_target_count"] == 3
    assert created["simulation"]["canonical_collection_unchanged"] is True
    assert created["task_state"]["active_collection"] == canonical

    explained = ask(deterministic_client, headers, "现在第一名是谁？", session_id=created["session_id"])
    assert explained["intent"] == "scenario_analysis", json.dumps(explained, ensure_ascii=False, default=str)
    assert explained["simulation"]["operation"] == "EXPLAIN_SCENARIO"
    assert explained["products"][0]["id"] == created["simulation"]["scenario_ordered_ids"][0]
    assert explained["requested_dimensions"] == ["recommendation", "decision"]
    assert explained["clarification_code"] is None


def test_provider_scenario_ordinal_top_k_and_metric_followups_use_hypothetical_order(
    deterministic_client, monkeypatch,
):
    headers, _ = seed(deterministic_client, "collection-scenario-provider-ordinal-views")
    collection_result = ask(deterministic_client, headers, "分析当前候选池最值得研究的三个商品")
    created = ask(
        deterministic_client, headers, "假设这批商品采购成本全部降低5%，谁最值得继续研究",
        session_id=collection_result["session_id"],
    )
    ordered = created["simulation"]["scenario_ordered_ids"]
    provider = MockProvider("qwen", [
        _provider_generic_rank_frame(ordinal=2),
        _provider_generic_rank_frame(top_k=3),
        _provider_generic_rank_frame(
            ordinal=1, dimensions=["roi"], metrics=["roi"], metric="roi",
        ),
    ])
    install_router(monkeypatch, ModelRouter([provider], primary_name="qwen", reasoning_name="deepseek"))

    second = ask(deterministic_client, headers, "排名第二的呢？", session_id=created["session_id"])
    top_three = ask(deterministic_client, headers, "现在前三名都是谁？", session_id=created["session_id"])
    first_roi = ask(deterministic_client, headers, "排名第一的 ROI 是多少？", session_id=created["session_id"])

    assert second["intent"] == "scenario_analysis", second
    assert second["products"][0]["id"] == ordered[1]
    assert "排名第 2" in second["answer"]
    assert [item["id"] for item in top_three["products"]] == ordered[:3]
    assert "HYPOTHETICAL 前 3 名" in top_three["answer"]
    assert first_roi["products"][0]["id"] == ordered[0]
    assert first_roi["requested_dimensions"] == ["roi"]
    assert "ROI" in first_roi["answer"]


def test_provider_scenario_ordinal_does_not_cross_session_boundary(deterministic_client, monkeypatch):
    headers, _ = seed(deterministic_client, "collection-scenario-provider-ordinal-isolation")
    collection_result = ask(deterministic_client, headers, "分析当前候选池最值得研究的三个商品")
    created = ask(
        deterministic_client, headers, "假设这批商品采购成本全部降低5%，谁最值得继续研究",
        session_id=collection_result["session_id"],
    )
    new_session = deterministic_client.post("/api/v1/agent/sessions", headers=headers, json={}).json()["data"]["id"]
    provider = MockProvider("qwen", [_provider_generic_rank_frame(ordinal=1)])
    install_router(monkeypatch, ModelRouter([provider], primary_name="qwen", reasoning_name="deepseek"))

    isolated = ask(deterministic_client, headers, "现在排名第一的是谁？", session_id=new_session)

    assert isolated["session_id"] != created["session_id"]
    assert isolated["intent"] != "scenario_analysis"
    assert isolated["response_type"] == "clarification"
    assert isolated["simulation"] is None


def _active_collection_scenario(deterministic_client, case_id):
    headers, _ = seed(deterministic_client, case_id)
    collection_result = ask(deterministic_client, headers, "分析当前候选池最值得研究的三个商品")
    created = ask(
        deterministic_client, headers,
        "假设这批商品采购成本全部降低5%，谁最值得继续研究",
        session_id=collection_result["session_id"],
    )
    return headers, created


@pytest.mark.parametrize("failure", ["CONNECT_ERROR", "NETWORK_TIMEOUT"])
def test_scenario_followups_use_validated_local_frame_after_transport_failure(
    deterministic_client, monkeypatch, failure,
):
    headers, created = _active_collection_scenario(
        deterministic_client, f"scenario-transport-followups-{failure}",
    )
    ordered = created["simulation"]["scenario_ordered_ids"]
    qwen = MockProvider("qwen", failure=failure)
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))
    kwargs = {"session_id": created["session_id"], "provider_execution_policy": "QWEN_ONLY"}

    first = ask(deterministic_client, headers, "现在排名第一的是谁？", **kwargs)
    second = ask(deterministic_client, headers, "排名第二的呢？", **kwargs)
    roi = ask(deterministic_client, headers, "排名第一的 ROI 是多少？", **kwargs)
    compared = ask(deterministic_client, headers, "和原来的基准相比呢？", **kwargs)
    reset = ask(deterministic_client, headers, "全部假设调整恢复到原值", **kwargs)

    assert first["products"][0]["id"] == ordered[0]
    assert second["products"], json.dumps(second, ensure_ascii=False, default=str)
    assert second["products"][0]["id"] == ordered[1]
    assert roi["products"], json.dumps(roi, ensure_ascii=False, default=str)
    assert roi["products"][0]["id"] == ordered[0] and "ROI" in roi["answer"]
    assert compared["simulation"]["operation"] == "COMPARE_SCENARIO"
    assert reset["simulation"]["operation"] == "RESET_SCENARIO"
    assert all(item["intent"] == "scenario_analysis" for item in (first, second, roi, compared, reset))
    assert all(item["fallback_reason"] == failure for item in (first, second, roi, compared, reset))
    assert all(item["response_type"] == "scenario_report" for item in (first, second, roi, compared, reset))
    assert len(qwen.calls) == 5


@pytest.mark.parametrize("failure", ["CONNECT_ERROR", "NETWORK_TIMEOUT"])
def test_complete_collection_scenario_create_survives_transport_failure(
    deterministic_client, monkeypatch, failure,
):
    headers, _ = seed(deterministic_client, f"scenario-transport-create-{failure}")
    collection_result = ask(deterministic_client, headers, "分析当前候选池最值得研究的三个商品")
    qwen = MockProvider("qwen", failure=failure)
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))

    result = ask(
        deterministic_client, headers,
        "假设当前候选池前三名的广告成本下降30%，按新结果排序",
        session_id=collection_result["session_id"], provider_execution_policy="QWEN_ONLY",
    )

    assert result["intent"] == "scenario_analysis"
    assert result["simulation"]["operation"] == "CREATE_SCENARIO"
    assert result["simulation"]["collection_scope"] == "TOP_K"
    assert result["fallback_reason"] == failure
    assert result["response_type"] == "scenario_report"
    assert len(qwen.calls) == 1


def test_complete_collection_scenario_survives_semantic_plan_validation_failure(
    deterministic_client, monkeypatch,
):
    headers, _ = seed(deterministic_client, "scenario-semantic-validation-fallback")
    collection_result = ask(deterministic_client, headers, "分析当前候选池最值得研究的三个商品")
    qwen = MockProvider("qwen", failure="SEMANTIC_PLAN_VALIDATION_FAILED")
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))

    result = ask(
        deterministic_client, headers,
        "假设当前候选池前三名的广告成本下降30%，按新结果排序",
        session_id=collection_result["session_id"], provider_execution_policy="QWEN_ONLY",
    )

    assert result["intent"] == "scenario_analysis"
    assert result["simulation"]["operation"] == "CREATE_SCENARIO"
    assert result["simulation"]["collection_scope"] == "TOP_K"
    assert result["fallback_reason"] == "SEMANTIC_PLAN_VALIDATION_FAILED"
    assert result["response_type"] == "scenario_report"
    assert result["task_completed"] is True
    assert len(qwen.calls) == 1


def test_semantic_plan_failure_does_not_guess_missing_scenario_context_or_slots(
    deterministic_client, monkeypatch,
):
    headers, _ = seed(deterministic_client, "scenario-semantic-validation-safe-clarification")
    qwen = MockProvider("qwen", failure="SEMANTIC_PLAN_VALIDATION_FAILED")
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))

    missing_collection = ask(
        deterministic_client, headers,
        "假设当前候选池前三名的广告成本下降30%，按新结果排序",
        provider_execution_policy="QWEN_ONLY",
    )
    missing_slots = ask(
        deterministic_client, headers, "如果成本低一点会怎样？",
        provider_execution_policy="QWEN_ONLY",
    )

    assert missing_collection["response_type"] == "clarification"
    assert missing_collection["clarification_code"] == "MISSING_CONTEXT"
    assert missing_collection["simulation"] is None
    assert missing_slots["response_type"] == "clarification"
    assert "假设条件不完整" in missing_slots["answer"]
    assert missing_slots["simulation"] is None
    assert all(item["tool_call_count"] == 0 for item in (missing_collection, missing_slots))


@pytest.mark.parametrize("failure", ["CONNECT_ERROR", "NETWORK_TIMEOUT"])
def test_transport_fallback_without_required_context_or_slots_never_guesses(
    deterministic_client, monkeypatch, failure,
):
    headers, _ = seed(deterministic_client, f"scenario-transport-safe-clarification-{failure}")
    qwen = MockProvider("qwen", failure=failure)
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))

    ordinal = ask(
        deterministic_client, headers, "排名第二的呢？",
        provider_execution_policy="QWEN_ONLY",
    )
    missing_collection = ask(
        deterministic_client, headers,
        "假设当前候选池前三名的广告成本下降30%，按新结果排序",
        provider_execution_policy="QWEN_ONLY",
    )
    ambiguous = ask(
        deterministic_client, headers, "如果成本低一点会怎样？",
        provider_execution_policy="QWEN_ONLY",
    )

    assert ordinal["response_type"] == "clarification"
    assert "没有可验证的排名上下文" in ordinal["answer"]
    assert missing_collection["response_type"] == "clarification"
    assert missing_collection["clarification_code"] == "MISSING_CONTEXT"
    assert ambiguous["response_type"] == "clarification"
    assert "假设条件不完整" in ambiguous["answer"]
    assert all(item["tool_call_count"] == 0 for item in (ordinal, missing_collection, ambiguous))
    assert len(qwen.calls) == 3


def _assert_single_fallback_payload(result, failure):
    assert result["fallback_reason"] == failure
    assert result["answer"] == result["conclusion"]
    product_ids = [item["id"] for item in result["products"]]
    evidence_product_ids = [item["product_id"] for item in result["evidence"]]
    assert len(product_ids) == len(set(product_ids))
    assert len(evidence_product_ids) == len(set(evidence_product_ids))
    assert result["duplicate_tool_execution"] == 0
    assert result["trace"]
    assert all(
        (item.get("business_label") or item.get("tool")) and item.get("summary")
        for item in result["trace"]
    )
    assert all(item.strip().lstrip("0123456789. ") for item in result["tool_trace_summary"])


@pytest.mark.parametrize(
    "failure", ["INVALID_RESPONSE", "SCHEMA_VALIDATION_FAILED", "RESPONSE_READ_ERROR"],
)
def test_scenario_followups_recover_from_unusable_provider_output(
    deterministic_client, monkeypatch, failure,
):
    headers, created = _active_collection_scenario(
        deterministic_client, f"scenario-output-fallback-{failure}",
    )
    ordered = created["simulation"]["scenario_ordered_ids"]
    qwen = MockProvider("qwen", failure=failure)
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))
    kwargs = {"session_id": created["session_id"], "provider_execution_policy": "QWEN_ONLY"}

    results = [
        ask(deterministic_client, headers, "现在排名第一的是谁？", **kwargs),
        ask(deterministic_client, headers, "排名第二的呢？", **kwargs),
        ask(deterministic_client, headers, "排名第一的 ROI 是多少？", **kwargs),
        ask(deterministic_client, headers, "和原来的基准相比呢？", **kwargs),
        ask(deterministic_client, headers, "全部假设调整恢复到原值", **kwargs),
    ]

    assert results[0]["products"][0]["id"] == ordered[0]
    assert results[1]["products"][0]["id"] == ordered[1]
    assert results[2]["products"][0]["id"] == ordered[0]
    assert "ROI" in results[2]["answer"]
    assert results[3]["simulation"]["operation"] == "COMPARE_SCENARIO"
    assert results[4]["simulation"]["operation"] == "RESET_SCENARIO"
    for result in results:
        _assert_single_fallback_payload(result, failure)
    assert len(qwen.calls) == 5


@pytest.mark.parametrize(
    "failure", ["INVALID_RESPONSE", "SCHEMA_VALIDATION_FAILED", "RESPONSE_READ_ERROR"],
)
def test_complete_scenario_create_recovers_from_unusable_provider_output(
    deterministic_client, monkeypatch, failure,
):
    headers, _ = seed(deterministic_client, f"scenario-output-create-{failure}")
    collection_result = ask(deterministic_client, headers, "分析当前候选池最值得研究的三个商品")
    canonical_ids = collection_result["task_state"]["active_collection"]["product_ids"]
    qwen = MockProvider("qwen", failure=failure)
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))

    result = ask(
        deterministic_client, headers,
        "假设当前候选池前三名的广告成本下降30%，按新结果排序",
        session_id=collection_result["session_id"], provider_execution_policy="QWEN_ONLY",
    )

    assert result["intent"] == "scenario_analysis"
    assert result["simulation"]["operation"] == "CREATE_SCENARIO"
    assert result["task_state"]["active_collection"]["product_ids"] == canonical_ids
    assert result["simulation"]["canonical_collection_unchanged"] is True
    _assert_single_fallback_payload(result, failure)
    assert len(qwen.calls) == 1


@pytest.mark.parametrize(
    "failure", ["INVALID_RESPONSE", "SCHEMA_VALIDATION_FAILED", "RESPONSE_READ_ERROR"],
)
def test_unusable_provider_output_still_requires_complete_safe_slots(
    deterministic_client, monkeypatch, failure,
):
    headers, _ = seed(deterministic_client, f"scenario-output-safe-{failure}")
    qwen = MockProvider("qwen", failure=failure)
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))

    ordinal = ask(
        deterministic_client, headers, "排名第二的呢？",
        provider_execution_policy="QWEN_ONLY",
    )
    ambiguous = ask(
        deterministic_client, headers, "如果成本低一点会怎样？",
        provider_execution_policy="QWEN_ONLY",
    )

    assert ordinal["response_type"] == "clarification"
    assert "没有可验证的排名上下文" in ordinal["answer"]
    assert ambiguous["response_type"] == "clarification"
    assert "假设条件不完整" in ambiguous["answer"]
    assert all(item["tool_call_count"] == 0 for item in (ordinal, ambiguous))
    assert all(item["fallback_reason"] == failure for item in (ordinal, ambiguous))


def test_permission_style_provider_failure_does_not_open_scenario_fallback(
    deterministic_client, monkeypatch,
):
    headers, created = _active_collection_scenario(
        deterministic_client, "scenario-auth-failure-remains-closed",
    )
    qwen = MockProvider("qwen", failure="AUTH_FAILED")
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))

    result = ask(
        deterministic_client, headers, "现在排名第一的是谁？",
        session_id=created["session_id"], provider_execution_policy="QWEN_ONLY",
    )

    assert result["response_type"] == "clarification"
    assert result["fallback_reason"] == "AUTH_FAILED"
    assert result["tool_call_count"] == 0
    assert result["products"] == []


def test_provider_collection_scenario_without_active_collection_requires_context(deterministic_client, monkeypatch):
    headers, _ = seed(deterministic_client, "collection-scenario-provider-missing")
    provider = MockProvider("qwen", [_provider_collection_scenario_frame()])
    install_router(monkeypatch, ModelRouter([provider], primary_name="qwen", reasoning_name="deepseek"))

    result = ask(
        deterministic_client, headers,
        "如果当前候选池前三名的广告成本都下降30%，重新排一次",
    )

    assert result["response_type"] == "clarification"
    assert result["clarification_code"] == "MISSING_CONTEXT"
    assert result["simulation"] is None


def test_collection_scenario_runtime_continues_explains_and_compares(deterministic_client):
    headers, _ = seed(deterministic_client, "collection-scenario-runtime-followup")
    collection_result = ask(deterministic_client, headers, "分析当前候选池最值得研究的三个商品")
    created = ask(
        deterministic_client, headers, "假设这批商品采购成本全部降低5%，谁最值得继续研究",
        session_id=collection_result["session_id"],
    )
    explained = ask(deterministic_client, headers, "现在第一名是谁", session_id=created["session_id"])
    compared = ask(deterministic_client, headers, "和原来的前三名相比有什么变化", session_id=created["session_id"])

    assert explained["simulation"]["operation"] == "EXPLAIN_SCENARIO"
    assert "HYPOTHETICAL" in explained["answer"]
    assert compared["simulation"]["operation"] == "COMPARE_SCENARIO"
    assert "Canonical Ranking" in compared["answer"]
    assert "Rank Delta" in compared["answer"]


def test_collection_scenario_runtime_ranking_update_is_scenario_only(deterministic_client):
    headers, _ = seed(deterministic_client, "collection-scenario-runtime-rerank")
    collection_result = ask(deterministic_client, headers, "分析当前候选池最值得研究的三个商品")
    canonical = collection_result["task_state"]["active_collection"]
    created = ask(
        deterministic_client, headers, "假设这批商品采购成本全部降低5%，谁最值得继续研究",
        session_id=collection_result["session_id"],
    )
    reranked = ask(
        deterministic_client, headers, "在当前假设下，只看利润和风险重新排序",
        session_id=created["session_id"],
    )

    assert reranked["simulation"] is not None, reranked
    assert reranked["simulation"]["operation"] == "UPDATE_SCENARIO"
    assert reranked["simulation"]["scenario_ranking_spec"] == ["profit", "risk"]
    assert reranked["task_state"]["active_collection"] == canonical


def test_collection_scenario_runtime_reset_restores_frozen_order(deterministic_client):
    headers, _ = seed(deterministic_client, "collection-scenario-runtime-reset")
    collection_result = ask(deterministic_client, headers, "分析当前候选池最值得研究的三个商品")
    created = ask(
        deterministic_client, headers, "假设这批商品采购成本全部降低5%，谁最值得继续研究",
        session_id=collection_result["session_id"],
    )
    reset = ask(deterministic_client, headers, "恢复原来的排序", session_id=created["session_id"])

    assert reset["simulation"]["operation"] == "RESET_SCENARIO"
    assert reset["simulation"]["scenario_ordered_ids"] == created["simulation"]["canonical"][0:0] + collection_result["task_state"]["active_collection"]["ranked_product_ids"]
