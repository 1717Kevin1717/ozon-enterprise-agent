"""Offline D1 import contract, repository safety and real API boundary."""

import asyncio
import csv
import io
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.router import context
from app.connectors.merchant_csv import MerchantCsvError, parse_merchant_csv
from app.core.config import settings
from app.db.models import Base, MerchantImportPreview, Product, ProductSnapshot
from app.db.session import get_session
from app.main import app
from app.repositories.products import ProductRepository, product_view
from app.services.decision_engine import analyze, simulate_price, to_dict
from app.services.product_import import ImportConflict, confirm_import, preview_import
from app.schemas.products import ProductPatch
from app.agent.scenario_state import ScenarioStateError, snapshot_product
from app.agent.tools import calculate_profit, compare_products, filter_products


HEADERS = ["merchant_product_id", "name", "expected_price_rub", "purchase_cost", "purchase_currency",
           "logistics_cost", "logistics_currency", "platform_commission_rate", "platform_fee_rub",
           "advertising_cost_rub", "warehousing_cost_rub", "tax_cost_rub", "return_loss_reserve_rub",
           "other_cost", "other_cost_currency", "source_updated_at"]


def csv_bytes(*rows):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=HEADERS)
    writer.writeheader()
    for row in rows:
        writer.writerow({"purchase_currency": "RUB", "logistics_currency": "RUB", "other_cost_currency": "RUB", **row})
    return output.getvalue().encode("utf-8-sig")


def complete_item(merchant_product_id="M-1", **changes):
    return {"merchant_product_id": merchant_product_id, "name": "Merchant sample item",
            "expected_price_rub": "100", "purchase_cost": "20", "logistics_cost": "5",
            "platform_commission_rate": "0.1", "platform_fee_rub": "0", "advertising_cost_rub": "0",
            "warehousing_cost_rub": "0", "tax_cost_rub": "0", "return_loss_reserve_rub": "0",
            "other_cost": "0", "source_updated_at": "2026-01-01T00:00:00Z", **changes}


async def database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def test_csv_validation_distinguishes_missing_zero_and_rejects_unsafe_input():
    parsed = parse_merchant_csv(csv_bytes(complete_item()))
    assert parsed.rows[0].statuses["platform_fee"] == "KNOWN_ZERO"
    assert parsed.rows[0].statuses["procurement_cost"] == "PRESENT"
    missing = parse_merchant_csv(csv_bytes(complete_item(purchase_cost="")))
    assert missing.rows[0].statuses["procurement_cost"] == "MISSING"
    for change, code in [({"purchase_cost": "NaN"}, "INVALID_NUMBER"),
                         ({"purchase_cost": "Infinity"}, "INVALID_NUMBER"),
                         ({"purchase_cost": "-5"}, "INVALID_NUMBER"),
                         ({"platform_commission_rate": "1.2"}, "COMMISSION_RATE_INVALID"),
                         ({"purchase_currency": "CNY"}, "TRUSTED_FX_REQUIRED"),
                         ({"name": "=HYPERLINK(1)"}, "UNSAFE_TEXT"),
                         ({"name": "<script>alert(1)</script>"}, "UNSAFE_TEXT"),
                         ({"source_updated_at": "bad"}, "SOURCE_TIME_INVALID")]:
        row = parse_merchant_csv(csv_bytes(complete_item(**change))).rows[0]
        assert code in [issue.code for issue in row.issues]
    duplicated = parse_merchant_csv(csv_bytes(complete_item(), complete_item(name="Other")))
    assert all("DUPLICATE_MERCHANT_ID" in [issue.code for issue in row.issues] for row in duplicated.rows)
    with pytest.raises(MerchantCsvError, match="CSV_COLUMNS_INVALID"):
        parse_merchant_csv(b"merchant_product_id,name\nA,B\n")
    with pytest.raises(MerchantCsvError, match="CSV_UTF8_REQUIRED"):
        parse_merchant_csv(b"\xff\xfe")


def test_preview_confirm_idempotency_tenant_and_financial_gate():
    async def run():
        engine, sessions = await database()
        try:
            async with sessions() as session:
                content = csv_bytes(complete_item(), complete_item("M-2", name="Incomplete", purchase_cost=""))
                preview = await preview_import(session, "tenant-a", "user-a", content)
                assert (preview.total_rows, preview.create_count, preview.reject_count) == (2, 2, 0)
                assert preview.rows[1].profit_status == "INSUFFICIENT_DATA"
                assert await session.scalar(select(func.count()).select_from(Product)) == 0
                with pytest.raises(ImportConflict, match="PREVIEW_NOT_FOUND"):
                    await confirm_import(session, "tenant-b", "user-a", preview.preview_id, content)
                with pytest.raises(ImportConflict, match="FILE_DIGEST_MISMATCH"):
                    await confirm_import(session, "tenant-a", "user-a", preview.preview_id, csv_bytes(complete_item(name="Changed")))
                result = await confirm_import(session, "tenant-a", "user-a", preview.preview_id, content)
                assert result.created == 2 and not result.replayed
                assert (await confirm_import(session, "tenant-a", "user-a", preview.preview_id, content)).replayed
                assert await session.scalar(select(func.count()).select_from(ProductSnapshot)) == 2
                repo = ProductRepository(session, "tenant-a")
                first = await repo.get_by_external_id("merchant_internal:M-1")
                second = await repo.get_by_external_id("merchant_internal:M-2")
                assert first.id != "M-1" and first.currency == "RUB"
                assert first.raw_payload["_merchant_import"]["price_kind"] == "merchant_estimate"
                assert first.field_lineage["procurement_cost"]["source_type"] == "enterprise_internal"
                assert snapshot_product(first).product_id == first.id
                assert simulate_price(first, 90)["net_profit"] == 56
                first.currency = "CNY"
                assert to_dict(analyze(first))["net_profit"] is None
                first.currency = "RUB"
                assert product_view(second)["procurement_cost"] is None
                await repo.analyze(second)
                # Ordinary edits may change raw JSON; immutable external identity still keeps the import gate active.
                await repo.patch(second, ProductPatch.model_validate({"raw_payload": {
                    "_merchant_import": {}, "_merchant_field_status": {"procurement_cost": "KNOWN_ZERO"}}}))
                assert product_view(second)["financial_status"] == "INSUFFICIENT_DATA"
                assert await repo.latest_analysis(second.id) is None
                assert to_dict(analyze(second))["net_profit"] is None
                assert simulate_price(second, 90)["status"] == "INSUFFICIENT_DATA"
                with pytest.raises(ScenarioStateError, match="verified cost inputs"):
                    snapshot_product(second)
                assert (await calculate_profit(repo, second.id, "analyst"))["data"]["net_profit"] is None
                assert (await compare_products(repo, [first.id, second.id], "analyst"))["data"][1]["financial_status"] == "INSUFFICIENT_DATA"
                filtered = await filter_products(repo, "analyst", min_margin_rate=0.1)
                assert first.id in [item["id"] for item in filtered["data"]]
                assert second.id not in [item["id"] for item in filtered["data"]]
                assert await ProductRepository(session, "tenant-b").get(first.id) is None
                other_preview = await preview_import(session, "tenant-b", "user-b", csv_bytes(complete_item()))
                other = await confirm_import(session, "tenant-b", "user-b", other_preview.preview_id, csv_bytes(complete_item()))
                assert other.created == 1 and other.product_ids[0] != first.id
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_batch_failure_rolls_back_and_manual_fields_are_not_overwritten(monkeypatch):
    async def run():
        engine, sessions = await database()
        try:
            async with sessions() as session:
                original = csv_bytes(complete_item(), complete_item("M-2"))
                preview = await preview_import(session, "tenant-a", "user-a", original)
                import app.services.product_import as service
                ingestion = service.ingest_product_result
                calls = 0
                async def fail_second(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        raise RuntimeError("injected transaction failure")
                    return await ingestion(*args, **kwargs)
                monkeypatch.setattr(service, "ingest_product_result", fail_second)
                with pytest.raises(RuntimeError, match="injected transaction failure"):
                    await confirm_import(session, "tenant-a", "user-a", preview.preview_id, original)
                assert await session.scalar(select(func.count()).select_from(Product)) == 0
                assert await session.scalar(select(func.count()).select_from(ProductSnapshot)) == 0
                assert (await session.get(MerchantImportPreview, preview.preview_id)).confirmed_at is None
                monkeypatch.setattr(service, "ingest_product_result", ingestion)
                await confirm_import(session, "tenant-a", "user-a", preview.preview_id, original)
                repo = ProductRepository(session, "tenant-a")
                item = await repo.get_by_external_id("merchant_internal:M-1")
                await repo.patch(item, ProductPatch.model_validate({"procurement_cost": 33,
                    "compliance_status": "rejected", "raw_payload": {"source": "enterprise_workbench"}}))
                replacement = csv_bytes(complete_item(expected_price_rub="115", purchase_cost="12",
                                                       source_updated_at="2026-02-01T00:00:00Z"))
                fresh = await preview_import(session, "tenant-a", "user-a", replacement)
                await confirm_import(session, "tenant-a", "user-a", fresh.preview_id, replacement)
                item = await repo.get_by_external_id("merchant_internal:M-1")
                assert item.current_price == 115
                assert item.procurement_cost == 33
                assert item.compliance_status == "rejected"
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_stale_preview_rejected_and_repeated_file_does_not_duplicate_snapshot():
    async def run():
        engine, sessions = await database()
        try:
            async with sessions() as session:
                original = csv_bytes(complete_item())
                a = await preview_import(session, "tenant-a", "user-a", original)
                b = await preview_import(session, "tenant-a", "user-a", original)
                await confirm_import(session, "tenant-a", "user-a", a.preview_id, original)
                with pytest.raises(ImportConflict, match="PREVIEW_STALE"):
                    await confirm_import(session, "tenant-a", "user-a", b.preview_id, original)
                fresh = await preview_import(session, "tenant-a", "user-a", original)
                repeat = await confirm_import(session, "tenant-a", "user-a", fresh.preview_id, original)
                assert repeat.unchanged == 1
                assert await session.scalar(select(func.count()).select_from(ProductSnapshot)) == 1
                later = csv_bytes(complete_item(expected_price_rub="110", source_updated_at="2025-01-01T00:00:00Z"))
                old = await preview_import(session, "tenant-a", "user-a", later)
                assert old.rows[0].issues[0].code == "OLDER_SOURCE_UPDATE"
                undated = await preview_import(session, "tenant-a", "user-a",
                    csv_bytes(complete_item(expected_price_rub="120", source_updated_at="")))
                assert "SOURCE_TIME_REQUIRED_FOR_UPDATE" in [issue.code for issue in undated.rows[0].issues]
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_imported_product_agent_profit_and_incomplete_data_stay_distinct(monkeypatch):
    async def run():
        engine, sessions = await database()
        async def override_session():
            async with sessions() as session:
                yield session
        app.dependency_overrides[get_session] = override_session
        try:
            monkeypatch.setattr(settings, "app_env", "test")
            monkeypatch.setattr(settings, "demo_identity_enabled", True)
            monkeypatch.setattr(settings, "llm_provider", "disabled")
            client = TestClient(app)
            content = csv_bytes(complete_item(name="企业待测收纳盒"),
                                complete_item("M-2", name="企业待补收纳袋", purchase_cost=""))
            preview = client.post("/api/v1/products/import/preview", files={"file": ("items.csv", content, "text/csv")})
            assert preview.status_code == 200
            confirmed = client.post("/api/v1/products/import/confirm", data={"preview_id": preview.json()["data"]["preview_id"]},
                                    files={"file": ("items.csv", content, "text/csv")})
            assert confirmed.status_code == 200
            dashboard = client.get("/api/v1/dashboard/overview")
            assert dashboard.status_code == 200, dashboard.text
            assert dashboard.json()["data"]["metrics"]["total_products"] == 2
            found = client.post("/api/v1/agent/ask", json={"query": "企业待测收纳盒的利润是多少？"})
            assert found.status_code == 200, found.text
            payload = found.json()["data"]
            assert payload["products"] and payload["products"][0]["title"] == "企业待测收纳盒"
            assert payload["products"][0]["net_profit"] == 65
            incomplete = client.post("/api/v1/agent/ask", json={"query": "企业待补收纳袋的利润是多少？"})
            assert incomplete.status_code == 200, incomplete.text
            pending = incomplete.json()["data"]
            assert pending["products"] and pending["products"][0]["financial_status"] == "INSUFFICIENT_DATA"
            assert pending["products"][0]["net_profit"] is None
            assert "无法可靠计算" in pending["answer"]
            assert "0.00%" not in pending["answer"]
            scenario = client.post("/api/v1/agent/ask", json={
                "query": "如果它售价下降10%，利润率会变成多少？", "session_id": payload["session_id"],
            })
            assert scenario.status_code == 200, scenario.text
            scenario_data = scenario.json()["data"]
            assert scenario_data["response_type"] == "scenario_report"
            assert scenario_data["session_id"] == payload["session_id"]
            reset = client.post("/api/v1/agent/ask", json={
                "query": "撤销刚才的假设", "session_id": payload["session_id"],
            })
            assert reset.status_code == 200, reset.text
            reset_data = reset.json()["data"]
            assert reset_data["response_type"] == "scenario_report"
            assert reset_data["simulation"]["operation"] == "RESET_SCENARIO"
            assert reset_data["simulation"]["overrides"] == []
            assert reset_data["session_id"] == payload["session_id"]
        finally:
            app.dependency_overrides.pop(get_session, None)
            await engine.dispose()
    asyncio.run(run())


def test_api_preview_does_not_write_product_and_production_rejects_anonymous(monkeypatch):
    async def run():
        engine, sessions = await database()
        async def override_session():
            async with sessions() as session:
                yield session
        app.dependency_overrides[get_session] = override_session
        try:
            monkeypatch.setattr(settings, "app_env", "test")
            monkeypatch.setattr(settings, "demo_identity_enabled", True)
            client = TestClient(app)
            content = csv_bytes(complete_item())
            preview = client.post("/api/v1/products/import/preview", files={"file": ("candidate.csv", content, "text/csv")})
            assert preview.status_code == 200, preview.text
            assert preview.json()["data"]["create_count"] == 1
            async with sessions() as session:
                assert await session.scalar(select(func.count()).select_from(Product)) == 0
            payload = {"preview_id": preview.json()["data"]["preview_id"]}
            confirmed = client.post("/api/v1/products/import/confirm", data=payload,
                                    files={"file": ("candidate.csv", content, "text/csv")})
            assert confirmed.status_code == 200, confirmed.text
            assert confirmed.json()["data"]["created"] == 1
            monkeypatch.setattr(settings, "llm_provider", "disabled")
            query = client.post("/api/v1/agent/ask", json={"query": "Merchant sample item 的售价是多少？"})
            assert query.status_code == 200, query.text
            assert query.json()["data"]["response_type"] == "simple_fact"
            monkeypatch.setattr(settings, "app_env", "production")
            assert client.post("/api/v1/products/import/preview", files={"file": ("candidate.csv", content, "text/csv")},
                               headers={"X-Company-ID": "tenant-a"}).status_code == 401
        finally:
            app.dependency_overrides.pop(get_session, None)
            await engine.dispose()
    asyncio.run(run())
