import pytest
import json

from app.agent.query_understanding import (
    SemanticPlanValidationFailure,
    arbitrate_entity_mentions,
    resolve_understanding,
    validate_semantic_plan,
)
from app.schemas.agent import ContextSnapshot
from app.schemas.semantic_vocabulary import (
    CANONICAL_DIMENSIONS,
    CANONICAL_METRICS,
    INTENT_ALLOWED_DIMENSIONS,
    semantic_output_contract,
)
from app.agent.tool_registry import TOOL_REGISTRY
from test_stability_sprint_v3 import product


def plan(intent="product_price", **updates):
    value = {
        "intent": intent,
        "confidence": 0.92,
        "route": "SEMANTIC_PLANNER",
        "entity_mentions": [],
        "references": [],
        "reference_slots": [],
        "requested_dimensions": ["price"] if intent == "product_price" else [],
        "planned_tools": ["get_product"] if intent == "product_price" else [],
    }
    return {**value, **updates}


def snapshot(*, last=None, comparison=None):
    return ContextSnapshot(
        session_id="test-session", company_id="test-company", user_id="test-user",
        current_query="test", last_explicit_product_ids=last or [],
        last_resolved_product_ids=last or [], last_comparison_order=comparison or [],
    )


def understanding_for(query, **updates):
    _, understood = validate_semantic_plan(
        plan(
            "product_detail",
            requested_dimensions=["profit"],
            planned_tools=["get_product", "calculate_profit"],
            **updates,
        ),
        query,
        context_packet={"last_explicit_product_ids": ["opaque-context-slot"]},
    )
    return understood


def test_e01_resolved_explicit_product_remains_authoritative_with_old_context():
    catalog = [product("商品甲", "p-a", "catalog-a"), product("商品乙", "p-b", "catalog-b")]
    understood = understanding_for("商品乙利润如何", entity_mentions=["商品乙"])
    arbitration = arbitrate_entity_mentions(catalog, understood, "商品乙利润如何")
    assert [item.title for item in arbitration.resolution.products] == ["商品乙"]
    assert arbitration.candidate_roles == ("EXPLICIT_PRODUCT",)


def test_e02_reference_only_followup_uses_context_instead_of_product_resolution():
    catalog = [product("商品甲", "p-a", "catalog-a")]
    understood = understanding_for(
        "它利润如何", entity_mentions=["它"], references=["它"],
        reference_slots=["last_explicit_entity"], requires_context=True,
    )
    arbitration = arbitrate_entity_mentions(catalog, understood, "它利润如何")
    assert arbitration.resolution.requested_entities == []
    assert arbitration.candidate_roles == ("REFERENCE",)


def test_e03_reference_role_candidate_does_not_become_product_not_found():
    catalog = [product("商品甲", "p-a", "catalog-a")]
    understood = understanding_for(
        "这个价格的利润如何", entity_mentions=["这个价格"], references=["这个价格"],
        reference_slots=["last_explicit_entity"], requires_context=True,
    )
    arbitration = arbitrate_entity_mentions(catalog, understood, "这个价格的利润如何")
    assert not arbitration.resolution.has_unresolved
    assert arbitration.ignored_semantic_candidate_count == 1


def test_e04_metric_candidate_does_not_become_product_not_found():
    catalog = [product("商品甲", "p-a", "catalog-a")]
    understood = understanding_for(
        "利润怎么算", entity_mentions=["利润"], metrics=["net_margin"],
        reference_slots=["last_resolved_entity"], requires_context=True,
    )
    arbitration = arbitrate_entity_mentions(catalog, understood, "利润怎么算")
    assert arbitration.resolution.requested_entities == []
    assert arbitration.ignored_semantic_candidate_count == 1


def test_e05_explicit_unknown_product_never_falls_back_to_old_context():
    catalog = [product("商品甲", "p-a", "catalog-a")]
    query = "火星牌量子空气炸锅利润如何"
    understood = understanding_for(query, entity_mentions=["火星牌量子空气炸锅"])
    arbitration = arbitrate_entity_mentions(catalog, understood, query)
    assert arbitration.resolution.has_unresolved
    assert arbitration.resolution.requested_entities[0].status == "NOT_FOUND"
    assert arbitration.candidate_roles == ("EXPLICIT_PRODUCT",)


def test_e06_two_explicit_products_remain_required_for_comparison():
    catalog = [product("商品甲", "p-a", "catalog-a"), product("商品乙", "p-b", "catalog-b")]
    _, understood = validate_semantic_plan(
        plan(
            "profit_comparison", requested_dimensions=["profit", "roi"],
            planned_tools=["compare_products", "calculate_profit"],
            entity_mentions=["商品甲", "商品乙"],
        ),
        "比较商品甲和商品乙的利润",
    )
    arbitration = arbitrate_entity_mentions(catalog, understood, "比较商品甲和商品乙的利润")
    assert [item.title for item in arbitration.resolution.products] == ["商品甲", "商品乙"]
    assert not arbitration.resolution.has_unresolved


def test_e07_ordinal_reference_is_not_a_product_candidate():
    catalog = [product("商品甲", "p-a", "catalog-a")]
    understood = understanding_for(
        "第二个利润如何", entity_mentions=["第二个"], references=["第二个"],
        reference_slots=["ordinal_reference"], requires_context=True,
    )
    arbitration = arbitrate_entity_mentions(catalog, understood, "第二个利润如何")
    assert arbitration.resolution.requested_entities == []
    assert arbitration.candidate_roles == ("REFERENCE",)


def test_e08_new_session_reference_noise_does_not_invent_a_product():
    catalog = [product("商品甲", "p-a", "catalog-a")]
    understood = understanding_for(
        "它利润如何", entity_mentions=["它"], references=["它"], requires_context=True,
    )
    arbitration = arbitrate_entity_mentions(catalog, understood, "它利润如何")
    assert arbitration.resolution.products == []
    assert not arbitration.resolution.has_unresolved


def test_e09_explicit_entity_switch_is_preserved():
    catalog = [product("商品甲", "p-a", "catalog-a"), product("商品乙", "p-b", "catalog-b")]
    understood = understanding_for("换成商品乙呢", entity_mentions=["商品乙"])
    arbitration = arbitrate_entity_mentions(catalog, understood, "换成商品乙呢")
    assert [item.title for item in arbitration.resolution.products] == ["商品乙"]


def test_e10_correction_reference_retains_product_and_switches_metric():
    catalog = [product("商品甲", "p-a", "catalog-a")]
    understood = understanding_for(
        "不是价格，我想看它净利率", entity_mentions=["它"], references=["它"],
        metrics=["net_margin"], reference_slots=["last_explicit_entity"], requires_context=True,
    )
    arbitration = arbitrate_entity_mentions(catalog, understood, "不是价格，我想看它净利率")
    assert arbitration.resolution.requested_entities == []
    assert understood.metric == "net_margin"


def test_raw_alias_mention_passes_before_backend_entity_resolution():
    query = "理线器目前是什么价格"
    parsed, understood = validate_semantic_plan(
        plan(entity_mentions=["理线器"]), query,
    )
    catalog = [product("桌面理线器")]

    resolution = resolve_understanding(catalog, understood)

    assert parsed.name == "product_price"
    assert understood.entity_mentions == ["理线器"]
    assert resolution.requested_entities[0].status == "UNIQUE_ALIAS_MATCH"
    assert resolution.products[0].title == "桌面理线器"


def test_context_only_profit_plan_passes_for_context_resolver():
    parsed, understood = validate_semantic_plan(
        plan(
            "product_detail",
            requested_dimensions=["profit", "roi"],
            planned_tools=["get_product", "calculate_profit"],
            reference_slots=["last_explicit_entity"],
            requires_context=True,
        ),
        "盈利能力如何",
        context_packet={"last_explicit_product_ids": ["opaque-context-slot"]},
    )

    assert parsed.name == "product_detail"
    assert understood.requires_context is True
    assert understood.clarification_required is False


def test_missing_target_is_deferred_to_downstream_clarification():
    parsed, understood = validate_semantic_plan(
        plan(requested_dimensions=[], planned_tools=[]),
        "目前是什么价格",
    )

    assert parsed.name == "product_price"
    assert understood.requested_dimensions == ["price"]
    assert understood.clarification_required is True
    assert understood.requires_context is True


@pytest.mark.parametrize("unsafe_field", ["current_price", "product_id", "sku"])
def test_business_fact_or_internal_identity_fields_fail_structural_schema(unsafe_field):
    with pytest.raises(SemanticPlanValidationFailure) as caught:
        validate_semantic_plan(
            {**plan(entity_mentions=["商品甲"]), unsafe_field: "forbidden"},
            "商品甲目前是什么价格",
        )

    assert caught.value.failure_code == "SCHEMA_VALIDATION_FAILED"
    assert caught.value.rule_id == "SEM000"
    assert caught.value.stage == "SCHEMA_VALIDATE"
    assert caught.value.field == unsafe_field


def test_comparison_raw_mentions_pass_before_resolution():
    query = "比较商品甲和商品乙的利润"
    parsed, understood = validate_semantic_plan(
        plan(
            "profit_comparison",
            requested_dimensions=["profit", "roi"],
            planned_tools=["compare_products", "calculate_profit"],
            entity_mentions=["商品甲", "商品乙"],
        ),
        query,
    )

    resolution = resolve_understanding([
        product("商品甲", "p-a", "catalog-a"),
        product("商品乙", "p-b", "catalog-b"),
    ], understood)

    assert parsed.name == "profit_comparison"
    assert len(resolution.products) == 2


def test_reference_only_comparison_passes_to_context_resolver():
    parsed, understood = validate_semantic_plan(
        plan(
            "product_comparison",
            requested_dimensions=["profit", "risk"],
            planned_tools=["compare_products"],
            references=["刚才那两个"],
            reference_slots=["last_comparison"],
            requires_context=True,
        ),
        "和刚才那两个相比",
        context_packet={"last_comparison_order": ["opaque-1", "opaque-2"]},
    )

    assert parsed.name == "product_comparison"
    assert understood.requires_context is True
    assert understood.comparison_required is True


@pytest.mark.parametrize(
    ("intent", "dimensions", "metrics", "expected_dimensions", "expected_metric"),
    [
        ("product_detail", ["profit"], [], ["profit"], None),
        ("product_detail", ["profit"], ["net_margin"], ["profit"], "net_margin"),
        ("calculation_explanation", ["profit", "calculation"], [], ["profit", "calculation"], None),
        ("calculation_explanation", ["cost_breakdown"], [], ["profit"], "cost_breakdown"),
        ("data_quality_answer", ["price_freshness"], [], ["price", "freshness"], "freshness"),
        ("product_detail", ["profitability"], [], ["profit"], None),
    ],
)
def test_provider_semantic_values_normalize_before_sem004(
    intent, dimensions, metrics, expected_dimensions, expected_metric,
):
    planned_tools = (
        ["get_product", "calculate_profit"]
        if intent == "calculation_explanation" or "profit" in expected_dimensions
        else ["get_product"]
    )
    parsed, understood = validate_semantic_plan(
        plan(
            intent,
            requested_dimensions=dimensions,
            metrics=metrics,
            planned_tools=planned_tools,
            reference_slots=["last_explicit_entity"],
            requires_context=True,
        ),
        "继续分析这个商品",
        context_packet={"last_explicit_product_ids": ["opaque-context-slot"]},
    )

    assert parsed.policy.requested_dimensions == tuple(expected_dimensions)
    assert understood.requested_dimensions == expected_dimensions
    assert understood.metric == expected_metric
    assert understood.clarification_required is False


def test_cost_breakdown_normalizes_to_profit_and_keeps_backend_tool_plan():
    parsed, understood = validate_semantic_plan(
        plan(
            "calculation_explanation",
            requested_dimensions=["cost_structure"],
            planned_tools=["get_product", "calculate_profit"],
            reference_slots=["last_resolved_entity"],
            requires_context=True,
        ),
        "继续解释该商品",
        context_packet={"last_resolved_product_ids": ["opaque-context-slot"]},
    )

    assert understood.requested_dimensions == ["profit"]
    assert understood.metric == "cost_breakdown"
    assert parsed.policy.allowed_tools == ("get_product", "calculate_profit")


def test_context_only_metric_supplies_its_canonical_dimension():
    parsed, understood = validate_semantic_plan(
        plan(
            "product_detail",
            requested_dimensions=[],
            metrics=["net_profit_margin"],
            planned_tools=["get_product", "calculate_profit"],
            reference_slots=["last_explicit_entity"],
            requires_context=True,
        ),
        "继续查看该指标",
        context_packet={"last_explicit_product_ids": ["opaque-context-slot"]},
    )

    assert parsed.name == "product_detail"
    assert understood.requested_dimensions == ["profit"]
    assert understood.metrics == ["net_margin"]
    assert understood.clarification_required is False


def test_prompt_contract_and_validator_share_the_same_canonical_values():
    contract = json.loads(semantic_output_contract())["semantic_value_contract"]

    assert set(contract["requested_dimensions"]) == set(CANONICAL_DIMENSIONS)
    assert set(contract["metrics"]) == set(CANONICAL_METRICS)
    assert {
        key: set(values) for key, values in contract["intent_allowed_dimensions"].items()
    } == {
        key: set(values) for key, values in INTENT_ALLOWED_DIMENSIONS.items()
    }


def test_unknown_provider_dimension_still_fails_sem004():
    with pytest.raises(SemanticPlanValidationFailure) as caught:
        validate_semantic_plan(
            plan(
                "product_detail",
                requested_dimensions=["quantum_market_signal"],
                planned_tools=["get_product"],
                reference_slots=["last_explicit_entity"],
                requires_context=True,
            ),
            "继续查看该商品",
            context_packet={"last_explicit_product_ids": ["opaque-context-slot"]},
        )

    assert (caught.value.rule_id, caught.value.reason_code, caught.value.field) == (
        "SEM004", "INVALID_DIMENSION", "requested_dimensions",
    )


def test_t01_profit_semantic_uses_backend_profit_plan():
    parsed, understood = validate_semantic_plan(
        plan("product_detail", requested_dimensions=["profit"], planned_tools=[]),
        "商品甲利润如何",
    )
    assert parsed.policy.allowed_tools == ("get_product", "calculate_profit")
    assert understood.planned_tools == ["get_product", "calculate_profit"]


def test_t02_net_margin_metric_uses_backend_profit_plan():
    _, understood = validate_semantic_plan(
        plan("product_detail", requested_dimensions=[], metrics=["net_margin"], planned_tools=[]),
        "商品甲净利率如何",
    )
    assert understood.metric == "net_margin"
    assert understood.planned_tools == ["get_product", "calculate_profit"]


def test_t03_cost_breakdown_uses_deterministic_cost_profit_capability():
    _, understood = validate_semantic_plan(
        plan("calculation_explanation", requested_dimensions=["cost_breakdown"], planned_tools=[]),
        "商品甲成本构成如何",
    )
    assert understood.metric == "cost_breakdown"
    assert understood.planned_tools == ["get_product", "calculate_profit"]


def test_t04_freshness_uses_backend_fact_read_plan():
    _, understood = validate_semantic_plan(
        plan("data_quality_answer", requested_dimensions=["price_freshness"], planned_tools=[]),
        "商品甲价格最近吗",
    )
    assert understood.metric == "freshness"
    assert understood.planned_tools == ["get_product"]


def test_t05_unknown_tool_suggestion_remains_sem009():
    with pytest.raises(SemanticPlanValidationFailure) as caught:
        validate_semantic_plan(
            plan("product_detail", requested_dimensions=["profit"], planned_tools=["unregistered_write_tool"]),
            "商品甲利润如何",
        )
    assert (caught.value.rule_id, caught.value.reason_code) == ("SEM009", "UNSUPPORTED_ACTION")


def test_t06_registered_irrelevant_advisory_tool_is_ignored_not_executed():
    _, understood = validate_semantic_plan(
        plan("product_price", requested_dimensions=["price"], planned_tools=["get_sales_trend"]),
        "商品甲价格如何",
    )
    assert understood.planned_tools == ["get_product"]
    assert understood.advisory_tool_mismatch == ["get_sales_trend"]


def test_t07_registry_permission_still_denies_analysis_tool_to_viewer():
    assert TOOL_REGISTRY.allows("get_product", "viewer") is True
    assert TOOL_REGISTRY.allows("calculate_profit", "viewer") is False


def test_t08_absent_advisory_tools_are_filled_by_backend():
    _, understood = validate_semantic_plan(
        plan("calculation_explanation", requested_dimensions=["profit", "calculation"], planned_tools=[]),
        "商品甲利润怎么算",
    )
    assert understood.planned_tools == ["get_product", "calculate_profit"]
    assert understood.requires_tools is True


def test_a06_minimal_reference_only_profit_frame_is_accepted():
    parsed, understood = validate_semantic_plan(
        {
            "intent": "product_detail",
            "confidence": 0.91,
            "route": "SEMANTIC_PLANNER",
            "reference_slots": ["last_explicit_entity"],
            "requires_context": True,
            "requested_dimensions": ["profit"],
        },
        "查看当前指标",
        context_packet={"last_explicit_product_ids": ["opaque-context-slot"]},
    )

    assert parsed.policy.allowed_tools == ("get_product", "calculate_profit")
    assert understood.planned_tools == ["get_product", "calculate_profit"]
    assert understood.clarification_required is False


@pytest.mark.parametrize("context_packet", [
    {"last_explicit_product_ids": ["opaque-context-slot"]},
    {},
])
def test_implicit_provider_reference_label_defers_to_typed_context(context_packet):
    parsed, understood = validate_semantic_plan(
        {
            "intent": "product_detail",
            "confidence": 0.91,
            "route": "SEMANTIC_PLANNER",
            "references": ["implicit_previous_item"],
            "reference_slots": ["last_explicit_entity"],
            "requires_context": True,
            "requested_dimensions": ["profit"],
        },
        "盈利表现如何",
        context_packet=context_packet,
    )

    assert parsed.policy.allowed_tools == ("get_product", "calculate_profit")
    assert understood.references == []
    assert understood.requires_context is True
    assert understood.clarification_required is (not bool(context_packet))


def test_single_target_profit_inquiry_cannot_expand_into_comparison():
    parsed, understood = validate_semantic_plan(
        {
            "intent": "profit_comparison",
            "task_type": "profit_inquiry",
            "question_type": "performance_status",
            "confidence": 0.95,
            "route": "SEMANTIC_PLANNER",
            "reference_slots": ["last_resolved_entity"],
            "requires_context": True,
            "comparison_requested": False,
            "requested_dimensions": ["profit", "roi"],
            "metrics": ["net_margin"],
            "planned_tools": ["compare_products", "calculate_profit"],
        },
        "盈利表现如何",
        context_packet={"last_resolved_product_ids": ["opaque-context-slot"]},
    )

    assert parsed.name == "product_detail"
    assert understood.intent == "product_detail"
    assert understood.comparison_required is False
    assert parsed.policy.allowed_tools == ("get_product", "calculate_profit")
    assert "compare_products" in understood.advisory_tool_mismatch


def test_real_comparison_signals_are_not_narrowed():
    parsed, understood = validate_semantic_plan(
        {
            "intent": "profit_comparison",
            "confidence": 0.95,
            "route": "SEMANTIC_PLANNER",
            "entity_mentions": ["商品甲", "商品乙"],
            "comparison_requested": True,
            "requested_dimensions": ["profit", "roi"],
        },
        "比较商品甲和商品乙的利润",
    )

    assert parsed.name == understood.intent == "profit_comparison"
    assert understood.comparison_required is True


def test_low_confidence_context_only_frame_can_only_clarify_without_target():
    parsed, understood = validate_semantic_plan(
        {
            "intent": "product_detail",
            "confidence": 0.62,
            "route": "SEMANTIC_PLANNER",
            "reference_slots": ["last_resolved_entity"],
            "requires_context": True,
            "requested_dimensions": ["profit", "roi"],
        },
        "盈利表现如何",
        context_packet={},
    )

    assert parsed.name == "product_detail"
    assert understood.clarification_required is True
    assert understood.requires_context is True


def test_unknown_context_frame_with_single_product_dimension_uses_backend_clarification():
    parsed, understood = validate_semantic_plan(
        {
            "intent": "unknown",
            "confidence": 0.60,
            "route": "SEMANTIC_PLANNER",
            "reference_slots": ["none"],
            "requires_context": True,
            "clarification_required": True,
            "clarification_reason": "Provider free-form text must not become the business answer.",
            "requested_dimensions": ["profit"],
        },
        "盈利表现如何",
        context_packet={},
    )

    assert parsed.name == understood.intent == "product_detail"
    assert understood.clarification_required is True
    assert understood.requires_context is True


@pytest.mark.parametrize(
    ("updates", "query", "rule_id", "reason_code", "field"),
    [
        ({"confidence": 0.4, "entity_mentions": ["商品甲"]}, "商品甲目前是什么价格", "SEM002", "LOW_CONFIDENCE_REQUIRES_CLARIFICATION", "confidence"),
        ({"requested_dimensions": ["risk"], "entity_mentions": ["商品甲"]}, "商品甲目前是什么价格", "SEM004", "INVALID_DIMENSION", "requested_dimensions"),
        ({"negative_scope": ["price"], "entity_mentions": ["商品甲"]}, "商品甲目前是什么价格", "SEM005", "CONFLICTING_SCOPE", "negative_scope"),
        ({"entity_mentions": ["商品乙"]}, "商品甲目前是什么价格", "SEM006", "INVALID_ENTITY_SPAN", "entity_mentions"),
        ({"references": ["此前商品"]}, "商品甲目前是什么价格", "SEM007", "INVALID_REFERENCE", "references"),
        ({"planned_tools": ["delete_all"], "entity_mentions": ["商品甲"]}, "商品甲目前是什么价格", "SEM009", "UNSUPPORTED_ACTION", "planned_tools"),
    ],
)
def test_semantic_guard_returns_structured_reason(updates, query, rule_id, reason_code, field):
    with pytest.raises(SemanticPlanValidationFailure) as caught:
        validate_semantic_plan(plan(**updates), query)

    failure = caught.value
    assert failure.failure_code == "SEMANTIC_PLAN_VALIDATION_FAILED"
    assert failure.stage == "SEMANTIC_VALIDATE"
    assert (failure.rule_id, failure.reason_code, failure.field) == (rule_id, reason_code, field)
