import json

import pytest

from app.agent.model_router import semantic_context_slots
from app.agent.query_understanding import validate_semantic_plan
from app.agent.scenario_state import create_scenario, detect_stale_baseline, parse_scenario_command
from test_scenario_state import product as scenario_product
from test_stability_sprint import ask, by_title, deterministic_client, seed


def _start(client, company: str):
    headers, items = seed(client, company)
    target = by_title(items, "后备箱收纳箱")
    created = ask(client, headers, f"如果{target['title']}广告成本减半，利润会怎么样？")
    return headers, items, target, created


def test_c01_c03_create_persists_and_update_reuses_id_with_next_revision(deterministic_client):
    headers, _, _, created = _start(deterministic_client, "scenario-continuity-create")
    updated = ask(deterministic_client, headers, "再把采购成本降5%", session_id=created["session_id"])

    assert created["intent"] == updated["intent"] == "scenario_analysis"
    assert created["simulation"]["operation"] == "CREATE_SCENARIO"
    assert updated["simulation"]["operation"] == "UPDATE_SCENARIO"
    assert created["simulation"]["scenario_id"] == updated["simulation"]["scenario_id"]
    assert (created["simulation"]["scenario_revision"], updated["simulation"]["scenario_revision"]) == (1, 2)
    assert {item["field"] for item in updated["simulation"]["overrides"]} == {"advertising_cost", "procurement_cost"}


def test_c04_explain_reads_current_scenario_without_new_override(deterministic_client):
    headers, _, _, created = _start(deterministic_client, "scenario-continuity-explain")
    result = ask(deterministic_client, headers, "现在ROI多少？", session_id=created["session_id"])

    assert result["simulation"]["operation"] == "EXPLAIN_SCENARIO"
    assert result["simulation"]["scenario_revision"] == 1
    assert len(result["simulation"]["overrides"]) == 1
    assert result["decision_status"] == "HYPOTHETICAL"
    assert "在当前假设条件下" in result["answer"]


def test_c05_compare_uses_immutable_canonical_baseline(deterministic_client):
    headers, _, _, created = _start(deterministic_client, "scenario-continuity-compare")
    result = ask(deterministic_client, headers, "和原来的情况比呢？", session_id=created["session_id"])

    assert result["simulation"]["operation"] == "COMPARE_SCENARIO"
    assert "Canonical ROI" in result["answer"]
    assert "Hypothetical ROI" in result["answer"]
    assert "Delta ROI" in result["answer"]


def test_compare_response_exposes_metric_and_rank_contract_without_duplicate_rows(deterministic_client):
    headers, _, _, created = _start(deterministic_client, "scenario-continuity-metric-contract")
    result = ask(deterministic_client, headers, "和原来的情况比呢？", session_id=created["session_id"])
    comparison = result["simulation"]["comparison"]

    assert len(comparison) == 1
    item = comparison[0]
    assert set(item["canonical"]) >= {"net_profit", "net_margin", "roi", "recommendation_score"}
    assert set(item["hypothetical"]) >= {"net_profit", "net_margin", "roi", "recommendation_score"}
    assert set(item["delta"]) >= {"net_profit", "net_margin", "roi", "recommendation_score"}
    assert item["canonical"]["net_profit"] == created["simulation"]["canonical"][0]["net_profit"]
    assert item["hypothetical"]["net_profit"] == created["simulation"]["hypothetical"][0]["net_profit"]
    assert "净利润" in result["answer"] and "净利率" in result["answer"] and "ROI" in result["answer"]
    assert result["answer"].count(result["products"][0]["title"]) == 1


def test_stale_compare_never_rebases_against_current_product_rows(deterministic_client, monkeypatch):
    from app.agent import tools as tools_module

    headers, _, _, created = _start(deterministic_client, "scenario-stale-compare-contract")
    frozen = created["simulation"]["canonical"]

    def stale(state, _products):
        return state.model_copy(update={"baseline_status": "STALE_BASELINE"})

    monkeypatch.setattr(tools_module, "detect_stale_baseline", stale)
    result = ask(deterministic_client, headers, "和原来的情况比呢？", session_id=created["session_id"])

    assert result["task_completed"] is False
    assert result["products"] == []
    assert result["simulation"]["baseline_status"] == "STALE_BASELINE"
    assert result["simulation"]["canonical"] == frozen
    assert "STALE_BASELINE" in result["answer"]


def test_c06_remove_one_override_keeps_other_override(deterministic_client):
    headers, _, _, created = _start(deterministic_client, "scenario-continuity-remove")
    updated = ask(deterministic_client, headers, "再把采购成本降5%", session_id=created["session_id"])
    restored = ask(deterministic_client, headers, "恢复广告成本", session_id=updated["session_id"])

    assert restored["simulation"]["operation"] == "REMOVE_OVERRIDE"
    assert [item["field"] for item in restored["simulation"]["overrides"]] == ["procurement_cost"]


def test_c07_reset_clears_overrides_and_restores_baseline(deterministic_client):
    headers, _, _, created = _start(deterministic_client, "scenario-continuity-reset")
    reset = ask(deterministic_client, headers, "全部恢复原值", session_id=created["session_id"])

    assert reset["simulation"]["operation"] == "RESET_SCENARIO"
    assert reset["simulation"]["overrides"] == []
    hypothetical = reset["simulation"]["hypothetical"][0]
    canonical = reset["simulation"]["canonical"][0]
    assert hypothetical["net_profit"] == canonical["net_profit"]
    assert hypothetical["roi"] == canonical["roi"]


@pytest.mark.parametrize("case_id", ["c08", "c09"])
def test_c08_c09_new_session_isolated_even_for_same_tenant_and_user(deterministic_client, case_id):
    headers, _, _, _ = _start(deterministic_client, "scenario-session-isolation-" + case_id)
    new_session = deterministic_client.post("/api/v1/agent/sessions", headers=headers, json={}).json()["data"]["id"]
    result = ask(deterministic_client, headers, "现在ROI多少？", session_id=new_session)

    assert result["response_type"] == "clarification"
    assert result["clarification_code"] == "MISSING_CONTEXT"
    assert result["simulation"] is None


def test_c10_update_without_active_scenario_requires_context(deterministic_client):
    headers, _ = seed(deterministic_client, "scenario-missing-context")
    result = ask(deterministic_client, headers, "再把采购成本降5%")

    assert result["intent"] == "scenario_analysis"
    assert result["response_type"] == "clarification"
    assert result["clarification_code"] == "MISSING_CONTEXT"
    assert result["tool_call_count"] == 0


def test_c11_c12_stale_baseline_detected_without_rebase():
    item = scenario_product()
    state = create_scenario(
        scenario_id="stale-scenario", session_id="stale-session", company_id=item.company_id,
        products=[item], override_specs=[{
            "field": "advertising_cost", "operation": "DECREASE_PERCENT", "value": 20, "unit": "PERCENT",
        }],
    )
    original_snapshot = state.canonical_snapshots[0]
    item.procurement_cost += 1

    stale = detect_stale_baseline(state, [item])

    assert stale.baseline_status == "STALE_BASELINE"
    assert stale.canonical_snapshots[0] == original_snapshot
    assert stale.derived_results == state.derived_results


def test_stale_runtime_blocks_follow_up_and_returns_explicit_status(deterministic_client, monkeypatch):
    from app.agent import tools as tools_module

    headers, _, _, created = _start(deterministic_client, "scenario-stale-runtime")

    def stale(state, _products):
        return state.model_copy(update={"baseline_status": "STALE_BASELINE"})

    monkeypatch.setattr(tools_module, "detect_stale_baseline", stale)
    result = ask(deterministic_client, headers, "再把采购成本降5%", session_id=created["session_id"])

    assert result["simulation"]["baseline_status"] == "STALE_BASELINE"
    assert result["simulation"]["scenario_id"] == created["simulation"]["scenario_id"]
    assert result["task_completed"] is False
    assert "STALE_BASELINE" in result["answer"]


def test_c13_c14_qwen_packet_exposes_only_anonymous_scenario_semantics():
    packet = {
        "session_id": "private-session", "company_id": "private-company",
        "last_explicit_product_ids": ["private-product"],
        "last_resolved_product_ids": ["private-product"],
        "verified_product_names": {"private-product": "private-product-name"},
        "allowed_tools": ["get_product"],
        "task_context": {"has_active_scenario": True, "active_entities_display_names": ["private-product-name"]},
        "rule_understanding": {
            "intent": "scenario_analysis", "operation": "UPDATE_SCENARIO",
            "scenario_field": "procurement_cost", "mutation_type": "DECREASE_PERCENT",
            "hypothetical_value": 5, "hypothetical_unit": "PERCENT",
            "scenario_reference_role": "ACTIVE_SCENARIO",
        },
        "canonical_price": 3999, "canonical_cost": 1000, "profit": 999, "roi": 0.5,
    }
    slots = semantic_context_slots(packet)
    serialized = json.dumps(slots, ensure_ascii=False)

    assert slots == {
        "intent": "scenario_analysis", "operation": "UPDATE_SCENARIO",
        "field": "procurement_cost", "mutation_type": "DECREASE_PERCENT",
        "value": 5, "unit": "PERCENT", "reference_role": "ACTIVE_SCENARIO",
        "has_active_scenario": True, "allowed_tools": ["get_product"],
    }
    assert not any(secret in serialized for secret in ("private-session", "private-company", "private-product", "3999", "1000", "999", "0.5"))


def test_scenario_semantic_frame_validates_against_canonical_contract():
    raw = {
        "intent": "scenario_analysis", "task_type": "scenario_analysis",
        "operation": "UPDATE_SCENARIO", "question_type": "what_if",
        "entity_mentions": [], "entity_roles": [], "references": ["再"],
        "reference_slots": ["session_state"], "requested_dimensions": ["profit", "roi"],
        "scenario_field": "procurement_cost", "mutation_type": "DECREASE_PERCENT",
        "hypothetical_value": 5, "hypothetical_unit": "PERCENT",
        "scenario_reference_role": "ACTIVE_SCENARIO", "requires_context": True,
        "requires_task_state": True, "requires_tools": True, "confidence": 0.95,
        "route": "SEMANTIC_PLANNER", "planned_tools": ["get_product"],
    }
    packet = {
        "last_resolved_product_ids": ["anonymous-backend-reference"],
        "task_context": {"has_active_scenario": True},
    }

    parsed, understanding = validate_semantic_plan(raw, "再把采购成本降5%", context_packet=packet)

    assert parsed.name == "scenario_analysis"
    assert understanding.operation == "UPDATE_SCENARIO"
    assert understanding.scenario_field == "procurement_cost"
    assert understanding.hypothetical_value == 5


@pytest.mark.parametrize(("query", "expected_intent"), [
    ("桌面理线器目前是什么价位", "product_price"),
    ("比较桌面理线器和便携蓝牙音箱，只看利润", "profit_comparison"),
    ("当前候选池最值得继续研究的三个方向", "collection_analysis"),
    ("第一名是不是就代表推荐上架？", "recommendation_policy"),
])
def test_c15_c19_negative_routing_stays_outside_scenario(deterministic_client, query, expected_intent):
    headers, _ = seed(deterministic_client, "scenario-negative-" + str(abs(hash(query))))
    result = ask(deterministic_client, headers, query)

    assert result["intent"] == expected_intent
    assert result["response_type"] != "scenario_report"


def test_c18_existing_collection_rerank_grammar_is_not_scenario():
    command = parse_scenario_command("只看利润和风险重新排一次", has_active_scenario=True)
    assert command is None


def test_c20_advertising_cost_fact_is_not_scenario():
    assert parse_scenario_command("后备箱收纳箱广告成本是多少？", has_active_scenario=False) is None


def test_c21_unsupported_hypothetical_field_is_safely_rejected(deterministic_client):
    headers, _ = seed(deterministic_client, "scenario-unsupported-field")
    result = ask(deterministic_client, headers, "如果桌面理线器评分提高20%，利润会怎么样？")

    assert result["intent"] == "scenario_analysis"
    assert result["response_type"] == "clarification"
    assert result["clarification_code"] == "UNSUPPORTED_SCENARIO_FIELD"
    assert result["simulation"] is None


def test_c22_ambiguous_entity_never_creates_scenario(deterministic_client):
    headers, items = seed(deterministic_client, "scenario-ambiguous")
    result = ask(
        deterministic_client, headers, "如果这两个商品的广告成本降低20%，利润会怎么样？",
        selected_product_ids=[item["id"] for item in items[:2]], selection_revision=1,
    )

    assert result["response_type"] == "clarification"
    assert result["simulation"] is None
    follow_up = ask(deterministic_client, headers, "现在ROI多少？", session_id=result["session_id"])
    assert follow_up["clarification_code"] == "MISSING_CONTEXT"
