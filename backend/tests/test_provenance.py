"""Data trust regression. Fixtures only; no live service/key/database required."""
import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.schemas.agent import AgentEvidence
from app.schemas.provenance import FieldEvidence, ProductDataTrust
from app.services.provenance import build_trust, raw_evidence, derived_evidence, freshness, scoped_fields


NOW = datetime(2026, 9, 4, 12, tzinfo=UTC)


def product(**values):
    defaults = dict(id="test-product", company_id="test-tenant", field_lineage={}, raw_payload={}, sales_source="not_provided", current_price=125, sales_growth_rate=-9.6)
    return SimpleNamespace(**{**defaults, **values})


@pytest.mark.parametrize("days,status", [(0, "FRESH"), (7, "FRESH"), (7.00001, "STALE"), (180, "STALE"), (-1, "UNKNOWN"), (None, "UNKNOWN")])
def test_freshness_boundary(days, status):
    timestamp = NOW - timedelta(days=days) if days is not None else None
    assert freshness("current_price", timestamp, now=NOW).status == status


def test_freshness_timezone_and_missing_policy():
    assert freshness("current_price", "2026-09-04T20:00:00+08:00", now=NOW).age_days == 0
    assert freshness("unspecified", NOW, now=NOW).status == "UNKNOWN"
    assert freshness("current_price", "bad-date", now=NOW).status == "UNKNOWN"


def test_legacy_numeric_snapshot_without_binding_has_no_fabricated_evidence():
    from app.services.provenance import snapshot_view
    row = SimpleNamespace(captured_at=NOW, price=321, sales_30d=0, review_count=0, rating=0, competitor_count=0)
    result = snapshot_view(row)
    assert result["price"] == 321 and result["field_provenance"] == {}
    assert "来源未知" in result["capture_notice"]


@pytest.mark.parametrize("kind,provider", [("manual", "manual_operator"), ("imported", "csv_import"), ("marketplace", "ozon"), ("enterprise_internal", "seller_report"), ("mock", "mock_enterprise_catalog")])
def test_sources_do_not_require_url(kind, provider):
    item = raw_evidence(product(field_lineage={"current_price": {"source_type": kind, "provider": provider, "collected_at": NOW}}), "current_price", now=NOW)
    assert item.source_type == kind and item.provider == provider and item.source_url is None
    assert item.evidence_status == "TRACEABLE"
    assert FieldEvidence.model_validate(item.model_dump()) == item


def test_schema_rejects_mock_real_and_unbound_derived():
    p = product(raw_payload={"demo_data": True})
    raw = raw_evidence(p, "current_price", now=NOW)
    for updates in ({"source_type": "marketplace"}, {"is_mock": False}, {"invented": "extra"}):
        with pytest.raises(ValidationError):
            FieldEvidence.model_validate({**raw.model_dump(), **updates})
    derived = derived_evidence(p, "net_profit", 42, [raw], "test expression", now=NOW)
    assert derived.is_mock
    for updates in ({"inputs": []}, {"derived_from": []}, {"is_mock": False}, {"product_id": "other"}, {"company_id": "other-tenant"}):
        with pytest.raises(ValidationError):
            FieldEvidence.model_validate({**derived.model_dump(), **updates})
    with pytest.raises(ValidationError):
        AgentEvidence(product_id="wrong", title="Wrong", source="test", summary="test", fields=[raw])


def test_mock_cannot_be_relabelled_marketplace_and_url_not_proof():
    p = product(raw_payload={"demo_data": True}, field_lineage={"current_price": {"source_type": "marketplace", "provider": "ozon", "source_url": "https://www.ozon.ru/search/?text=test", "collected_at": NOW}})
    item = raw_evidence(p, "current_price", now=NOW)
    assert item.is_mock and item.source_type == "mock" and item.source_url is None


def test_updated_at_does_not_make_unknown_or_old_observations_fresh():
    p = product(updated_at=NOW, field_lineage={"currentPriceRub": "manual_operator"})
    assert raw_evidence(p, "current_price", now=NOW).freshness.status == "UNKNOWN"
    p.field_lineage = {"current_price": {"provider": "manual_operator", "observed_at": NOW-timedelta(days=180), "collected_at": NOW, "updated_at": NOW}}
    assert raw_evidence(p, "current_price", now=NOW).freshness.status == "STALE"


def test_derived_stale_and_missing_inputs_propagate():
    p = product(field_lineage={"current_price": {"source_type": "manual", "provider": "manual_operator", "collected_at": NOW-timedelta(days=8)}})
    price = raw_evidence(p, "current_price", now=NOW)
    cost = raw_evidence(p, "procurement_cost", now=NOW)
    output = derived_evidence(p, "net_profit", 50, [price, cost], "price - cost", now=NOW)
    assert output.freshness.status == "STALE" and output.evidence_status == "SOURCE_MISSING"
    assert output.inputs[0].value == 125 and output.inputs[1].value is None


def test_complete_presence_is_not_freshness_or_trend_sufficiency():
    from app.agent.tools import sales_data_sufficiency
    from app.services.provenance import RAW_FIELDS
    p = product(**{key: 1 for key in RAW_FIELDS if key not in {"current_price", "sales_growth_rate"}}, raw_payload={"demo_data": True})
    trust = build_trust(p, now=NOW)
    assert trust.completeness["percent"] == 100
    assert trust.fields["current_price"].freshness.status == "UNKNOWN"
    suff = sales_data_sufficiency([{"sales_30d": 10, "captured_at": NOW}], -9.6, "mock_report", now=NOW)
    assert suff["status"] == "INSUFFICIENT_DATA"
    assert suff["reported_metric"] == -9.6 and suff["observed_trend"] == "INSUFFICIENT_DATA"


def profit_evidence_bundle(**updates):
    from app.agent.tools import _profit_calculation_evidence
    from app.services.decision_engine import analyze, to_dict
    from app.services.demo_dataset import enterprise_demo_candidates

    candidate = enterprise_demo_candidates()[0].model_copy(update=updates)
    subject = SimpleNamespace(id="profit-evidence-product", company_id="test-tenant", currency="RUB", **candidate.model_dump())
    result = to_dict(analyze(subject))
    data = {
        "net_profit": result["net_profit"],
        "net_margin": result["net_margin"],
        "roi": result["roi"],
        "provenance": scoped_fields({"fields": result["evidence"]["field_provenance"]}, ("profit", "roi")),
    }
    return candidate, result, data, _profit_calculation_evidence(data)


def test_b01_profit_result_and_evidence_are_same_calculation():
    _candidate, result, _data, calculation = profit_evidence_bundle()
    assert calculation["result"] == result["net_profit"]
    assert calculation["metric"] == "net_profit" and calculation["unit"] == "RUB"
    assert calculation["derived_from"]


def test_b02_profit_evidence_contains_current_price_input():
    candidate, _result, _data, calculation = profit_evidence_bundle(current_price=777)
    inputs = {item["field"]: item for item in calculation["inputs"]}
    assert inputs["current_price"]["value"] == candidate.current_price == 777
    assert calculation["input_sources"]["current_price"] != "unknown"


def test_b03_margin_uses_same_current_price_and_profit_result():
    candidate, result, _data, calculation = profit_evidence_bundle(current_price=777)
    inputs = {item["field"]: item for item in calculation["inputs"]}
    assert inputs["current_price"]["value"] == 777
    assert result["net_margin"] == pytest.approx(result["net_profit"] / candidate.current_price, abs=0.0001)


def test_b04_unknown_cost_inputs_are_disclosed_as_zero_normalization():
    _candidate, _result, _data, calculation = profit_evidence_bundle(
        procurement_cost=0, shipping_cost=0, fulfillment_cost=0, advertising_cost=0,
        platform_fee=0, warehousing_cost=0, tax_cost=0, return_loss_reserve=0,
        other_cost=0, platform_commission_rate=0, field_lineage={},
        raw_payload={"source": "unidentified_import"},
    )
    inputs = {item["field"]: item for item in calculation["inputs"]}
    assert inputs["procurement_cost"]["presence"] == "UNKNOWN"
    assert "normalized to 0" in calculation["formula"]


def test_b05_missing_source_propagates_to_calculation_evidence_status():
    _candidate, _result, _data, calculation = profit_evidence_bundle(
        field_lineage={}, raw_payload={"source": "unidentified_import"},
    )
    assert calculation["evidence_status"] == "SOURCE_MISSING"
    assert any(item["evidence_status"] == "SOURCE_MISSING" for item in calculation["inputs"])


def test_b06_cost_breakdown_is_backend_input_projection_sorted_by_amount():
    candidate, result, _data, calculation = profit_evidence_bundle()
    amounts = [item["amount"] for item in calculation["cost_breakdown"]]
    assert amounts == sorted(amounts, reverse=True)
    assert sum(amounts) == pytest.approx(candidate.current_price - result["net_profit"], abs=0.01)
    assert calculation["total_cost"] == pytest.approx(sum(amounts), abs=0.01)
    assert calculation["cost_breakdown"][0]["share_of_price"] == pytest.approx(amounts[0] / candidate.current_price, abs=0.0001)


def test_b07_calculation_evidence_does_not_reverse_engineer_inputs_from_result():
    _candidate, _result, data, calculation = profit_evidence_bundle()
    original_breakdown = calculation["cost_breakdown"]
    data["net_profit"] = 999999
    from app.agent.tools import _profit_calculation_evidence
    changed = _profit_calculation_evidence(data)
    assert changed["result"] == 999999
    assert changed["cost_breakdown"] == original_breakdown


def test_b08_mock_disclosure_is_preserved_in_profit_evidence():
    _candidate, _result, _data, calculation = profit_evidence_bundle()
    assert any(item["is_mock"] for item in calculation["inputs"])
    assert all(item["provider"] != "ozon" for item in calculation["inputs"] if item["is_mock"])


def test_product_binding_rejects_foreign_fields():
    trust = build_trust(product(), now=NOW).model_dump()
    trust["fields"]["current_price"]["company_id"] = "other"
    with pytest.raises(ValidationError):
        ProductDataTrust.model_validate(trust)


async def repository_scenario():
    from app.db.models import Base, Company
    from app.repositories.products import ProductRepository, product_view
    from app.schemas.products import ProductIn, ProductPatch
    from app.agent.tools import rule_agent_ask, get_product, get_product_history, calculate_profit, merge_profit_calculation
    from app.services.demo_dataset import enterprise_demo_candidates
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            session.add_all([Company(id="owner", name="Owner"), Company(id="outsider", name="Other")])
            await session.commit()
            repo, other = ProductRepository(session, "owner"), ProductRepository(session, "outsider")
            data = enterprise_demo_candidates()[0]
            data = data.model_copy(update={"title": "证据测试设备", "external_product_id": "provenance-product", "current_price": 125, "captured_at": NOW-timedelta(days=180)})
            p = await repo.upsert(data)
            a = await repo.analyze(p)
            original = product_view(p, a)
            assert original["data_trust"]["fields"]["current_price"]["freshness"]["status"] == "STALE"
            raw_history = await repo.history(p.id)
            assert len(raw_history) == 1 and raw_history[0].raw_payload["_field_lineage"]
            result = await rule_agent_ask(session, "owner", "tester", "company_admin", p.title+"售价是多少？", None, [])
            assert result["fact"]["value"] == 125
            assert result["fact"]["provenance"]["is_mock"]
            assert any(item["code"] == "STALE_DATA" for item in result["trust_notices"])
            assert not result["warnings"] and result["tool_call_count"] == 1
            assert result["evidence"][0]["fields"][0]["product_id"] == p.id
            assert result["evidence"][0]["fields"][0]["company_id"] == "owner"
            for tool in (get_product, get_product_history, calculate_profit):
                denied = await tool(other, p.id, "company_admin")
                assert not denied["success"] and denied["error_code"] == "NOT_FOUND" and "data" not in denied
            # Saved analysis must remain attached to its original inputs after editing.
            before_sales = p.field_lineage["latest_30d_sales"]
            p = await repo.patch(p, ProductPatch(current_price=170, raw_payload={"source": "enterprise_workbench"}))
            view = product_view(p, a)
            assert p.field_lineage["latest_30d_sales"] == before_sales
            assert view["data_trust"]["fields"]["current_price"]["value"] == 170
            net = view["data_trust"]["fields"]["net_profit"]
            assert net["inputs"][0]["value"] == 125 and net["freshness"]["status"] == "STALE"
            assert view["data_trust"]["fields"]["current_price"]["is_mock"]
            a = await repo.analyze(p)
            latest = product_view(p, a)["data_trust"]["fields"]["net_profit"]
            assert latest["inputs"][0]["value"] == 170
            assert latest["is_mock"]
            # Legacy analyses have unknown provenance, never a retroactively invented source.
            legacy = a.evidence_json.copy()
            legacy.pop("field_provenance")
            a.evidence_json = legacy
            legacy_view = product_view(p, a)
            assert legacy_view["data_trust"]["fields"]["net_profit"]["inputs"][0]["value"] is None
            assert any(item["code"] == "LEGACY_ANALYSIS" for item in legacy_view["data_trust"]["notices"])
            current_profit = await calculate_profit(repo, p.id, "company_admin")
            merge_profit_calculation(legacy_view, current_profit)
            rebound = legacy_view["data_trust"]["fields"]["net_profit"]
            assert rebound["inputs"][0]["value"] == 170
            assert legacy_view["analysis"]["net_profit"] == current_profit["data"]["net_profit"]
            assert not any(item["code"] == "LEGACY_ANALYSIS" for item in legacy_view["data_trust"]["notices"])
            # A subsequent raw update without evidence invalidates only the changed field's source.
            manual = await repo.upsert(ProductIn(external_product_id="manual-test", title="手工设备", current_price=90, raw_payload={"source": "enterprise_workbench"}))
            assert raw_evidence(manual, "current_price").source_type == "manual"
            manual = await repo.patch(manual, ProductPatch(current_price=91, raw_payload={"source": "unidentified_import"}))
            assert raw_evidence(manual, "current_price").source_type == "unknown"
            return result
    finally:
        await engine.dispose()


def test_repository_agent_snapshot_edit_and_tenant_evidence():
    asyncio.run(repository_scenario())


def test_actual_renderer_shows_mock_stale_source_without_scope_expansion():
    from test_stability_sprint_v3 import render_result
    result = asyncio.run(repository_scenario())
    html = render_result(result)
    assert "演示 / Mock" in html and "已过期" in html and "mock_enterprise_catalog" in html
    assert "采集" in html and "证据编号" in html
    for forbidden in ("推荐度", "竞争评分", "销量趋势", "人工审核", "完整决策报告"):
        assert forbidden not in html


def test_renderer_escapes_untrusted_provider_text():
    from test_stability_sprint_v3 import render_result
    item = raw_evidence(product(field_lineage={"current_price": {"source_type": "manual", "provider": "<script>unsafe</script>"}}), "current_price").model_dump(mode="json")
    html = render_result({"response_type": "simple_fact", "answer": "125 RUB", "fact": {"name": "product_price", "value": 125}, "evidence": [{"product_id": "test-product", "fields": [item], "disclosure": "测试来源"}]})
    assert "<script>unsafe</script>" not in html and "&lt;script&gt;" in html


def test_provenance_evaluator_rejects_wrong_binding():
    from evals.schemas import GoldenCase
    from evals.evaluators import FactGroundingEvaluator
    case = GoldenCase(case_id="PV_BIND", category="provenance", query="测试字段", provenance="Deterministic binding contract, not real Ozon", expected={"provenance": {"field": "current_price"}})
    field = raw_evidence(product(), "current_price").model_dump(mode="json")
    run = {"company_id": "wrong-tenant", "catalog": [], "result": {"evidence": [{"product_id": "test-product", "fields": [field]}]}}
    checks = FactGroundingEvaluator().evaluate(case, run)
    assert any(not row.passed and row.assertion == "evidence_product_tenant_binding" for row in checks)
