"""Offline connector contract and source-owned repository mapping."""

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.connectors.base import AdapterResult, DataSourceType, ExternalProductIdentity, FieldStatus
from app.connectors.mock_products import MockProductConnector
from app.db.models import Base, Product, ProductSnapshot, Store
from app.repositories.products import ProductRepository, product_view
from app.services.product_ingestion import ingest_product_result


async def _database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _result(source, identity, data, *, statuses=None, observed=None, updated=None):
    return AdapterResult(
        data=data, provider="offline_contract", source_reference="fixture-record",
        source_type=source, identity=identity, field_status=statuses or {},
        observed_at=observed, source_updated_at=updated,
    )


def test_mock_connector_implements_existing_product_contract():
    connector = MockProductConnector()
    rows = connector.list_products()
    assert len(rows) == 60
    assert all(row.source_type == DataSourceType.DEMO_MOCK for row in rows)
    assert all(row.identity.repository_key() == row.data["external_product_id"] for row in rows)
    assert all(row.observed_at is None for row in rows)
    assert all(row.data["raw_payload"]["demo_data"] is True for row in rows)
    assert asyncio.run(connector.get_product(rows[0].identity.external_product_id)).data["title"] == rows[0].data["title"]
    with pytest.raises(LookupError):
        asyncio.run(connector.get_product("absent"))


def test_mock_ingestion_reuses_product_identity_and_snapshot_for_identical_observation():
    async def run():
        engine, sessions = await _database()
        try:
            row = MockProductConnector().list_products()[0]
            async with sessions() as session:
                repo = ProductRepository(session, "company-a")
                first = await ingest_product_result(repo, row)
                original_id = first.id
                first_analysis = await repo.analyze(first)
                second = await ingest_product_result(repo, row)
                count = await session.scalar(select(func.count()).select_from(ProductSnapshot).where(ProductSnapshot.product_id == original_id))
                assert second.id == original_id != row.identity.external_product_id
                assert count == 1
                assert second.sync_status == "demo_mock"
                assert second.last_sync_at is not None
                view = product_view(second, first_analysis)
                assert view["data_trust"]["is_mock"] is True
                assert view["analysis"]["net_profit"] == first_analysis.net_profit
                assert second.raw_payload["_ingestion"]["source_type"] == "DEMO_MOCK"
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_external_identity_namespaces_and_store_tenant_boundary():
    assert ExternalProductIdentity(DataSourceType.OZON_SELLER, "offer-7", "store-a", "123", "offer-7", "sku-9").repository_key() == "ozon:store-a:product:123"
    with pytest.raises(ValueError):
        ExternalProductIdentity(DataSourceType.OZON_SELLER, "offer-7", None, "123").repository_key()

    async def run():
        engine, sessions = await _database()
        try:
            async with sessions() as session:
                session.add(Store(id="store-a", company_id="company-a", store_name="A"))
                await session.commit()
                identity = ExternalProductIdentity(DataSourceType.OZON_SELLER, "offer-7", "store-a", "123", "offer-7", "sku-9")
                result = _result(DataSourceType.OZON_SELLER, identity, {"title": "Adapter product", "current_price": 100, "sku": "sku-9"})
                a = await ingest_product_result(ProductRepository(session, "company-a"), result)
                assert a.store_id == "store-a"
                assert a.external_product_id == "ozon:store-a:product:123"
                assert a.id not in {"123", "offer-7", "sku-9"}
                assert a.raw_payload["_ingestion"]["identity"]["offer_id"] == "offer-7"
                with pytest.raises(ValueError, match="Store does not belong"):
                    await ingest_product_result(ProductRepository(session, "company-b"), result)
                assert await ProductRepository(session, "company-b").get(a.id) is None
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_partial_sync_preserves_manual_cost_and_compliance_and_distinguishes_zero():
    async def run():
        engine, sessions = await _database()
        try:
            async with sessions() as session:
                session.add(Store(id="store-a", company_id="company-a", store_name="A"))
                await session.commit()
                repo = ProductRepository(session, "company-a")
                identity = ExternalProductIdentity(DataSourceType.OZON_SELLER, "offer-7", "store-a", "123", "offer-7", "sku-9")
                created = await ingest_product_result(repo, _result(DataSourceType.OZON_SELLER, identity, {"title": "Adapter product", "current_price": 100}))
                internal = ExternalProductIdentity(DataSourceType.MERCHANT_INTERNAL, "cost-book", internal_product_id=created.id)
                await ingest_product_result(repo, _result(DataSourceType.MERCHANT_INTERNAL, internal, {
                    "procurement_cost": 38, "compliance_status": "approved", "compliance_note": "Manual review",
                    "manual_risk_level": "low",
                }))
                observed = datetime(2026, 10, 1, tzinfo=UTC)
                updated = datetime(2026, 9, 30, tzinfo=UTC)
                partial = _result(DataSourceType.OZON_SELLER, identity, {
                    "current_price": 0, "procurement_cost": None, "compliance_status": None,
                    "rating": 4.7,
                }, statuses={"current_price": FieldStatus.KNOWN_ZERO,
                             "procurement_cost": FieldStatus.UNKNOWN,
                             "compliance_status": FieldStatus.MISSING,
                             "rating": FieldStatus.UNVERIFIED,
                             "sales_growth_rate": FieldStatus.UNKNOWN}, observed=observed, updated=updated)
                changed = await ingest_product_result(repo, partial)
                assert changed.id == created.id
                assert changed.current_price == 0
                assert changed.procurement_cost == 38
                assert changed.compliance_status == "approved"
                assert changed.compliance_note == "Manual review"
                assert changed.manual_risk_level == "low"
                assert changed.rating == 0  # Unverified 4.7 was not accepted as a business fact.
                assert changed.source_updated_at == updated.replace(tzinfo=None)
                assert changed.raw_payload["_ingestion"]["field_status"]["current_price"] == "KNOWN_ZERO"
                assert changed.raw_payload["_ingestion"]["field_status"]["rating"] == "UNVERIFIED"
                assert changed.field_lineage["procurement_cost"]["source_type"] == "enterprise_internal"
                assert changed.field_lineage["current_price"]["source_type"] == "marketplace"
                assert changed.field_lineage["current_price"]["observed_at"] == observed.isoformat()
                assert changed.raw_payload["_external_identities"]["OZON_SELLER"]["ozon_product_id"] == "123"
                assert changed.raw_payload["_external_identities"]["OZON_SELLER"]["offer_id"] == "offer-7"
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_unknown_or_unverified_required_values_cannot_create_product():
    async def run():
        engine, sessions = await _database()
        try:
            async with sessions() as session:
                session.add(Store(id="store-a", company_id="company-a", store_name="A"))
                await session.commit()
                repo = ProductRepository(session, "company-a")
                identity = ExternalProductIdentity(DataSourceType.OZON_SELLER, "offer-7", "store-a", "123")
                with pytest.raises(ValueError, match="sourced title and price"):
                    await ingest_product_result(repo, _result(DataSourceType.OZON_SELLER, identity,
                        {"title": "Adapter product", "current_price": 100}, statuses={"current_price": FieldStatus.UNVERIFIED}))
                assert await repo.get_by_external_id(identity.repository_key()) is None
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_authorized_market_signal_cannot_change_cost_or_other_tenant_product():
    async def run():
        engine, sessions = await _database()
        try:
            async with sessions() as session:
                repo_a = ProductRepository(session, "company-a")
                product = await ingest_product_result(repo_a, MockProductConnector().list_products()[0])
                original_cost, original_compliance = product.procurement_cost, product.compliance_status
                identity = ExternalProductIdentity(DataSourceType.AUTHORIZED_MARKET_DATA, "market-sample", internal_product_id=product.id)
                signal = _result(DataSourceType.AUTHORIZED_MARKET_DATA, identity, {
                    "competitor_count": 0, "procurement_cost": 1,
                    "compliance_status": "rejected",
                }, statuses={"competitor_count": FieldStatus.KNOWN_ZERO})
                changed = await ingest_product_result(repo_a, signal)
                assert changed.competitor_count == 0
                assert changed.procurement_cost == original_cost
                assert changed.compliance_status == original_compliance
                assert changed.raw_payload["_ingestion"]["field_status"]["competitor_count"] == "KNOWN_ZERO"
                with pytest.raises(ValueError, match="Target product does not belong"):
                    await ingest_product_result(ProductRepository(session, "company-b"), signal)
                assert await ProductRepository(session, "company-b").get(product.id) is None
        finally:
            await engine.dispose()
    asyncio.run(run())
