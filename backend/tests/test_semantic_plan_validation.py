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
    allowed_dimensions_for,
    normalize_semantic_values,
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


def collection_ranking_plan(**updates):
    values = {
        "operation": "EXPLAIN_RANKING",
        "requested_dimensions": ["profit", "risk"],
        "planned_tools": ["analyze_collection"],
        "references": ["第一名", "第二名"],
        "reference_slots": ["session_state", "ordinal_reference"],
        "requires_context": True,
    }
    values.update(updates)
    return plan("collection_analysis", **values)


def test_collection_ranking_canonical_single_ordinal_schema():
    _, understood = validate_semantic_plan(
        collection_ranking_plan(ordinal_reference=1),
        "第一名为什么领先第二名",
        context_packet={"task_context": {"collection_member_count": 3}},
    )
    assert understood.ordinal_reference == 1
    assert understood.ordinal_references == [1]


@pytest.mark.parametrize(
    "shape",
    [
        {"ordinal_reference": [1, 2]},
        {
            "ordinal_references": [1, 2],
            "operation": "COMPARE_COLLECTION_MEMBERS",
            "reference_slots": ["active_collection_ranking", "ordinal_pair"],
        },
    ],
)
def test_collection_ranking_equivalent_ordinal_pair_shapes_are_normalized(shape):
    _, understood = validate_semantic_plan(
        collection_ranking_plan(**shape),
        "第一名为什么领先第二名",
        context_packet={"task_context": {"collection_member_count": 3}},
    )
    assert understood.ordinal_reference == 1
    assert understood.ordinal_references == [1, 2]
    assert understood.operation == "EXPLAIN_RANKING"
    assert set(understood.reference_slots) <= {"session_state", "ordinal_reference"}


def test_collection_ranking_invalid_ordinal_pair_shape_is_rejected():
    with pytest.raises(SemanticPlanValidationFailure) as exc:
        validate_semantic_plan(
            collection_ranking_plan(ordinal_reference={"first": 1, "second": 2}),
            "第一名为什么领先第二名",
            context_packet={"task_context": {"collection_member_count": 3}},
        )
    assert exc.value.failure_code == "SCHEMA_VALIDATION_FAILED"


def test_collection_ranking_unknown_reference_slot_is_still_rejected():
    with pytest.raises(SemanticPlanValidationFailure) as exc:
        validate_semantic_plan(
            collection_ranking_plan(reference_slots=["unbounded_provider_reference"]),
            "第一名为什么领先第二名",
            context_packet={"task_context": {"collection_member_count": 3}},
        )
    assert exc.value.failure_code == "SCHEMA_VALIDATION_FAILED"


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


@pytest.mark.parametrize(
    ("operation", "provider_dimensions", "expected_dimensions"),
    [
        ("RECOVER", ["identity", "price"], ["price"]),
        ("REFINE", ["analysis"], ["detail"]),
    ],
)
def test_recover_from_partial_comparison_narrows_to_single_product_scope(
    operation, provider_dimensions, expected_dimensions,
):
    parsed, understood = validate_semantic_plan(
        plan(
            "product_detail",
            operation=operation,
            requested_dimensions=provider_dimensions,
            planned_tools=["get_product"],
            requires_context=True,
            reference_slots=["last_resolved_entity"],
        ),
        "继续分析剩余候选",
        context_packet={
            "last_resolved_product_ids": ["opaque-context-slot"],
            "task_context": {"has_pending_unresolved_entity": True},
        },
    )

    assert parsed.name == "product_detail"
    assert understood.requested_dimensions == expected_dimensions


def test_partial_comparison_task_state_overrides_provider_filter_label_on_recover():
    parsed, understood = validate_semantic_plan(
        plan(
            "product_filter",
            operation="REFINE",
            requested_dimensions=["identity", "price"],
            planned_tools=["get_product"],
            requires_context=True,
            reference_slots=["last_resolved_entity", "ordinal_reference"],
        ),
        "继续分析剩余候选",
        context_packet={
            "last_resolved_product_ids": ["opaque-context-slot"],
            "task_context": {
                "active_task_type": "product_comparison",
                "has_pending_unresolved_entity": True,
            },
        },
    )

    assert parsed.name == "product_detail"
    assert understood.operation == "RECOVER"
    assert understood.requested_dimensions == ["price"]


def test_ordinal_continuation_inherits_last_verified_dimension():
    parsed, understood = validate_semantic_plan(
        plan(
            "product_detail",
            requested_dimensions=["identity", "price"],
            planned_tools=[],
            requires_context=True,
            ordinal_reference=1,
            reference_slots=["ordinal_reference"],
            clarification_required=True,
        ),
        "第一个呢",
        context_packet={
            "last_comparison_order": ["opaque-1", "opaque-2"],
            "last_requested_dimensions": ["profit"],
        },
    )

    assert parsed.name == "product_detail"
    assert understood.requested_dimensions == ["profit"]
    assert understood.clarification_required is False


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


@pytest.mark.parametrize("provider_dimension", ["cost", "costs", "cost_analysis", "cost_components"])
def test_provider_cost_dimension_aliases_normalize_to_cost_breakdown(provider_dimension):
    parsed, understood = validate_semantic_plan(
        plan(
            "calculation_explanation",
            requested_dimensions=[provider_dimension],
            planned_tools=["get_product", "calculate_profit"],
            reference_slots=["last_resolved_entity"],
            requires_context=True,
        ),
        "继续解释该商品",
        context_packet={"last_resolved_product_ids": ["opaque-context-slot"]},
    )

    assert understood.requested_dimensions == ["profit"]
    assert understood.metric == "cost_breakdown"
    assert parsed.policy.requested_dimensions == ("profit",)


def test_cost_breakdown_metric_arbitrates_broad_product_detail_intent():
    parsed, understood = validate_semantic_plan(
        plan(
            "product_detail",
            requested_dimensions=["profit", "calculation"],
            metrics=["cost_breakdown"],
            metric="cost_breakdown",
            planned_tools=["get_product", "calculate_profit"],
            reference_slots=["last_resolved_entity"],
            requires_context=True,
        ),
        "继续解释该商品",
        context_packet={"last_resolved_product_ids": ["opaque-context-slot"]},
    )

    assert parsed.name == "calculation_explanation"
    assert understood.intent == "calculation_explanation"
    assert understood.requested_dimensions == ["profit", "calculation"]
    assert understood.metric == "cost_breakdown"


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
    assert caught.value.diagnostics["normalization_failures"] == ["quantum_market_signal"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("evidence_gap", ("evidence_gap",)),
        ("Evidence Gap Analysis", ("evidence_gap",)),
        ("missing_information", ("evidence_gap",)),
        ("data completeness", ("data_quality",)),
        ("source_gap", ("provenance", "evidence_gap")),
        ("证据缺口", ("evidence_gap",)),
        ("数据哪里不完整", ("evidence_gap", "data_quality")),
    ],
)
def test_semantic_dimension_aliases_normalize_to_canonical_values(raw, expected):
    normalized = normalize_semantic_values([raw])

    assert normalized.dimensions == expected
    assert normalized.unknown_dimensions == ()


def test_mixed_canonical_and_alias_dimensions_are_deduplicated():
    normalized = normalize_semantic_values([
        "recommendation", "missing_data", "evidence_gap", "source traceability",
    ])

    assert normalized.dimensions == ("recommendation", "evidence_gap", "provenance")
    assert normalized.unknown_dimensions == ()


def test_collection_operation_dimension_registry_uses_existing_canonical_terms():
    top_k = allowed_dimensions_for("collection_analysis", "RECOMMEND_TOP_K")
    gaps = allowed_dimensions_for("collection_analysis", "IDENTIFY_EVIDENCE_GAPS")

    assert {"recommendation", "profit", "risk", "evidence_gap", "data_quality", "freshness", "provenance"} <= top_k
    assert {"evidence", "evidence_gap", "data_quality", "freshness", "provenance"} <= gaps
    assert not ({"quantum_signal", "provider_free_text"} & top_k)


@pytest.mark.parametrize(
    "provider_frame",
    [
        plan("collection_analysis", operation="ANALYZE_COLLECTION", requested_dimensions=["recommendation", "evidence_gap"]),
        plan("collection_analysis", operation="RECOMMEND_TOP_K", requested_dimensions=["recommendation", "missing_data"]),
        plan("product_filter", operation="CREATE", requested_dimensions=["recommendation", "evidence_gaps"]),
        plan("unknown", operation="CREATE", requested_dimensions=["recommendation", "data_gap_analysis"], clarification_required=True),
        plan("collection_analysis", operation="ANALYZE_COLLECTION", requested_dimensions=["decision", "证据缺口"]),
        plan("product_filter", operation="CREATE", requested_dimensions=["recommendation", "missing_information"]),
        plan("collection_analysis", operation="RECOMMEND_TOP_K", requested_dimensions=["recommendation", "source_gap"]),
        plan("collection_analysis", operation="CREATE", requested_dimensions=["recommendation", "data_completeness"]),
        plan("product_filter", operation="CREATE", requested_dimensions=["recommendation", "incomplete_data"]),
        plan("unknown", operation="CREATE", requested_dimensions=["recommendation", "evidence_quality"], clarification_required=True),
    ],
)
def test_collection_semantic_anchor_is_repeatable_across_legal_frame_variants(provider_frame):
    parsed, understood = validate_semantic_plan(
        provider_frame,
        "请分析当前候选池里最值得继续研究的三个商品方向，并说明证据缺口",
    )

    assert parsed.name == understood.intent == "collection_analysis"
    assert understood.operation == "RECOMMEND_TOP_K"
    assert understood.collection_reference == "candidate_pool"
    assert understood.top_k == 3
    assert understood.evidence_gap_requested is True
    assert understood.requested_dimensions == ["recommendation", "decision", "evidence", "evidence_gap"]
    assert understood.planned_tools == ["analyze_collection"]


@pytest.mark.parametrize(
    "query",
    [
        "请分析候选集合中最值得进一步验证的三个方向，并列出资料缺口",
        "从当前候选里挑三个优先研究对象，同时说明还缺哪些证据",
        "候选池前3个值得调研的方向有哪些，数据哪里不完整",
        "这批候选最值得研究的三个是什么，还需要补充什么资料",
    ],
)
def test_collection_analysis_paraphrases_share_one_backend_intent(query):
    parsed, understood = validate_semantic_plan(
        plan("product_filter", requested_dimensions=["recommendation", "missing_information"]),
        query,
    )

    assert parsed.name == understood.intent == "collection_analysis"
    assert understood.operation == "RECOMMEND_TOP_K"
    assert understood.evidence_gap_requested is True


@pytest.mark.parametrize(
    "query",
    [
        "找净利率超过30%且低风险的商品",
        "从公司商品库筛出利润高于1000且低风险的商品",
        "在候选池里只保留合规通过并且净利率不低于20%的商品",
    ],
)
def test_explicit_constraint_queries_remain_product_filters(query):
    parsed, understood = validate_semantic_plan(
        plan("product_filter", requested_dimensions=["filters", "profit", "risk", "compliance"]),
        query,
    )

    assert parsed.name == understood.intent == "product_filter"


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


def test_collection_unknown_tool_suggestion_is_advisory_and_never_executed():
    parsed, understood = validate_semantic_plan(
        plan(
            "collection_analysis",
            operation="RECOMMEND_TOP_K",
            requested_dimensions=["recommendation", "evidence_gap"],
            planned_tools=["provider_invented_collection_tool"],
            top_k=3,
        ),
        "当前候选池里挑三个值得研究的方向，并说明证据缺口",
    )

    assert parsed.policy.allowed_tools == ("analyze_collection",)
    assert understood.planned_tools == ["analyze_collection"]
    assert understood.advisory_tool_mismatch == ["provider_invented_collection_tool"]
    assert "provider_invented_collection_tool" not in understood.planned_tools


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
