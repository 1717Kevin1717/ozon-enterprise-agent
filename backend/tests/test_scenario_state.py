import asyncio
from math import inf, nan

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent import scenario_state as scenario_module
from app.agent.scenario_state import (
    ScenarioStateError,
    apply_overrides,
    compare_to_baseline,
    create_scenario,
    normalize_override,
    remove_override,
    reset_scenario,
    update_scenario,
)
from app.db.models import Base, Company, Product
from app.schemas.agent import RecommendationState, TaskState
from app.services.decision_engine import analyze_snapshot


def product() -> Product:
    return Product(
        id="scenario-product-1",
        company_id="scenario-company",
        external_product_id="scenario-external-1",
        title="Scenario fixture product",
        brand="Fixture",
        category_path="Test > Scenario",
        url="https://example.invalid/scenario-product",
        main_image_url="https://example.invalid/scenario-product.png",
        currency="RUB",
        current_price=3000,
        competitor_price_min=2800,
        competitor_price_avg=3100,
        rating=4.7,
        review_count=1500,
        latest_30d_sales=420,
        sales_growth_rate=8,
        search_volume=28000,
        trend_score=72,
        competitor_count=12,
        price_competition_score=35,
        market_saturation=42,
        compliance_status="approved",
        compliance_note="fixture evidence",
        certificates=["fixture-certificate"],
        manual_risk_level="low",
        procurement_cost=1000,
        fulfillment_cost=0,
        shipping_cost=250,
        platform_fee=50,
        advertising_cost=200,
        warehousing_cost=40,
        tax_cost=100,
        return_loss_reserve=60,
        other_cost=20,
        platform_commission_rate=0.12,
        target_margin_rate=0.30,
        sales_source="fixture_report",
        sales_evidence="fixture-sales-evidence",
        competition_evidence="fixture-competition-evidence",
        field_lineage={"currentPriceRub": "fixture", "search_volume": "fixture"},
    )


def create_with(*override_specs: dict) -> tuple[Product, scenario_module.ScenarioState]:
    item = product()
    state = create_scenario(
        scenario_id="scenario-1",
        session_id="session-1",
        company_id=item.company_id,
        products=[item],
        override_specs=override_specs,
    )
    return item, state


def effective(state: scenario_module.ScenarioState, field: str) -> float:
    return next(item.value for item in state.derived_results[0].effective_values if item.field == field)


def test_s01_advertising_cost_half_changes_profit_without_canonical_mutation():
    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        try:
            async with sessions() as session:
                item = product()
                session.add(Company(id=item.company_id, name="Scenario Company"))
                session.add(item)
                await session.commit()
                state = create_scenario(
                    scenario_id="scenario-1",
                    session_id="session-1",
                    company_id=item.company_id,
                    products=[item],
                    override_specs=[{
                        "field": "advertising_cost", "operation": "DECREASE_PERCENT",
                        "value": 50, "unit": "PERCENT",
                    }],
                )
            async with sessions() as verification_session:
                stored = await verification_session.get(Product, "scenario-product-1")
                return state, stored.advertising_cost
        finally:
            await engine.dispose()

    state, stored_advertising_cost = asyncio.run(run())
    assert state.derived_results[0].net_profit > state.comparison_baseline[0].net_profit
    assert effective(state, "advertising_cost") == 100
    assert stored_advertising_cost == 200


def test_s02_procurement_cost_reduction_reuses_deterministic_roi_calculation():
    _, state = create_with({
        "field": "procurement_cost", "operation": "DECREASE_PERCENT",
        "value": 5, "unit": "PERCENT",
    })
    calculated_input = apply_overrides(state.canonical_snapshots[0], state.overrides)
    expected = analyze_snapshot(calculated_input)

    assert state.derived_results[0].roi == expected.roi
    assert effective(state, "procurement_cost") == 950


def test_s03_continuous_update_increments_revision():
    _, state = create_with()
    first = update_scenario(
        state, expected_revision=1, field="advertising_cost",
        operation="DECREASE_PERCENT", value=50, unit="PERCENT",
    )
    second = update_scenario(
        first, expected_revision=2, field="procurement_cost",
        operation="DECREASE_PERCENT", value=5, unit="PERCENT",
    )

    assert (state.scenario_revision, first.scenario_revision, second.scenario_revision) == (1, 2, 3)


def test_s04_remove_override_restores_only_requested_field():
    _, state = create_with(
        {"field": "advertising_cost", "operation": "DECREASE_PERCENT", "value": 50, "unit": "PERCENT"},
        {"field": "procurement_cost", "operation": "DECREASE_PERCENT", "value": 5, "unit": "PERCENT"},
    )
    updated = remove_override(state, field="advertising_cost", expected_revision=1)

    assert effective(updated, "advertising_cost") == 200
    assert effective(updated, "procurement_cost") == 950
    assert {item.field for item in updated.overrides} == {"procurement_cost"}


def test_s05_reset_returns_canonical_baseline():
    _, state = create_with({
        "field": "advertising_cost", "operation": "DECREASE_PERCENT",
        "value": 50, "unit": "PERCENT",
    })
    reset = reset_scenario(state, expected_revision=1)

    assert reset.overrides == ()
    assert reset.derived_results[0].net_profit == reset.comparison_baseline[0].net_profit
    assert reset.derived_results[0].roi == reset.comparison_baseline[0].roi


def test_s06_scenario_comparison_delta_matches_derived_and_baseline():
    _, state = create_with({
        "field": "advertising_cost", "operation": "DECREASE_PERCENT",
        "value": 50, "unit": "PERCENT",
    })
    delta = compare_to_baseline(state)[state.base_product_ids[0]]

    assert delta["net_profit"] == pytest.approx(
        state.derived_results[0].net_profit - state.comparison_baseline[0].net_profit
    )
    assert delta["roi"] == pytest.approx(
        state.derived_results[0].roi - state.comparison_baseline[0].roi
    )


def test_s07_scenario_recommendation_is_explicitly_hypothetical():
    _, state = create_with({
        "field": "current_price", "operation": "SET", "value": 3999, "unit": "RUB",
    })

    assert state.status == "SIMULATION"
    assert state.derived_results[0].recommendation_label == "HYPOTHETICAL"


def test_s08_scenario_does_not_overwrite_recommendation_state():
    recommendation = RecommendationState(
        selected_product_id="scenario-product-1",
        candidate_product_ids=["scenario-product-1"],
        ordered_candidates=["scenario-product-1"],
        decision_status="REVIEW_REQUIRED",
        gate_status="REVIEW_REQUIRED",
    )
    task = TaskState(active_recommendation=recommendation)
    before = task.model_dump(mode="json")

    create_with({"field": "current_price", "operation": "SET", "value": 3999, "unit": "RUB"})

    assert task.model_dump(mode="json") == before


def test_s14_unsupported_override_field_is_rejected():
    with pytest.raises(ScenarioStateError, match="Unsupported scenario field") as caught:
        normalize_override(
            field="compliance_status", operation="SET", value=1, unit="RATIO", created_revision=1,
        )
    assert caught.value.code == "UNSUPPORTED_OVERRIDE_FIELD"


@pytest.mark.parametrize(
    ("field", "operation", "value", "unit"),
    [
        ("current_price", "SET", nan, "RUB"),
        ("current_price", "SET", inf, "RUB"),
        ("current_price", "SET", -1, "RUB"),
        ("advertising_cost", "DECREASE_PERCENT", 101, "PERCENT"),
        ("advertising_cost", "MULTIPLY", 0, "MULTIPLIER"),
        ("platform_commission_rate", "SET", 1.5, "RATIO"),
        ("advertising_cost", "SET", 10, "PERCENT"),
    ],
)
def test_s15_invalid_number_range_or_unit_is_rejected(field, operation, value, unit):
    with pytest.raises(ScenarioStateError):
        normalize_override(
            field=field, operation=operation, value=value, unit=unit, created_revision=1,
        )


def test_s16_evaluation_exception_cannot_mutate_canonical_product(monkeypatch):
    item = product()
    before = (item.current_price, item.procurement_cost, item.advertising_cost)

    def fail(_):
        raise RuntimeError("controlled evaluator failure")

    monkeypatch.setattr(scenario_module, "analyze_snapshot", fail)
    with pytest.raises(RuntimeError, match="controlled evaluator failure"):
        create_scenario(
            scenario_id="scenario-failure",
            session_id="session-1",
            company_id=item.company_id,
            products=[item],
            override_specs=[{
                "field": "advertising_cost", "operation": "DECREASE_PERCENT",
                "value": 50, "unit": "PERCENT",
            }],
        )

    assert (item.current_price, item.procurement_cost, item.advertising_cost) == before


def test_s17_stale_revision_is_rejected_without_state_change():
    _, state = create_with()
    before = state.model_dump(mode="json")

    with pytest.raises(ScenarioStateError, match="revision is stale") as caught:
        update_scenario(
            state, expected_revision=0, field="advertising_cost",
            operation="DECREASE_PERCENT", value=50, unit="PERCENT",
        )

    assert caught.value.code == "STALE_SCENARIO_REVISION"
    assert state.model_dump(mode="json") == before


def test_s18_percentage_and_normalized_ratio_are_equivalent():
    percentage = normalize_override(
        field="procurement_cost", operation="DECREASE_PERCENT",
        value=5, unit="PERCENT", created_revision=1,
    )
    ratio = normalize_override(
        field="procurement_cost", operation="DECREASE_PERCENT",
        value=0.05, unit="RATIO", created_revision=1,
    )

    assert percentage.normalized_value == ratio.normalized_value == 0.05


def test_s19_same_field_overrides_follow_revision_and_insertion_order():
    _, state = create_with({
        "field": "advertising_cost", "operation": "DECREASE_PERCENT",
        "value": 50, "unit": "PERCENT",
    })
    updated = update_scenario(
        state, expected_revision=1, field="advertising_cost",
        operation="DECREASE_PERCENT", value=10, unit="PERCENT",
    )

    assert effective(updated, "advertising_cost") == pytest.approx(200 * 0.5 * 0.9)


def test_s20_remove_same_field_overrides_restores_canonical_value():
    _, state = create_with({
        "field": "advertising_cost", "operation": "DECREASE_PERCENT",
        "value": 50, "unit": "PERCENT",
    })
    state = update_scenario(
        state, expected_revision=1, field="advertising_cost",
        operation="DECREASE_PERCENT", value=10, unit="PERCENT",
    )
    restored = remove_override(state, field="advertising_cost", expected_revision=2)

    assert effective(restored, "advertising_cost") == 200
    assert all(item.field != "advertising_cost" for item in restored.overrides)
