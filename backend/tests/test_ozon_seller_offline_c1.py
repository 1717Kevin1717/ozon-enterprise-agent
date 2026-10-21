"""Mock-transport-only C1 tests; no real Seller API, credentials or merchant data."""

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.connectors.base import FieldStatus
from app.connectors.ozon_seller_offline import (
    OzonSellerOfflineClient, ReadOnlyEndpoint, SellerClientError,
    SellerCredentials, SellerProductObservation, adapt_seller_observation,
    collect_cursor_pages,
)
from app.db.models import Base, ProductSnapshot, Store
from app.repositories.products import ProductRepository
from app.services.product_ingestion import ingest_product_result


class OfflineCredentials:
    async def for_store(self, company_id: str, store_id: str):
        return SellerCredentials(company_id, store_id, "fixture-client", "fixture-key")


async def _db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _observation(product_id="101", *, price=100.0, price_status=FieldStatus.PRESENT, currency="RUB"):
    return SellerProductObservation(
        ozon_product_id=product_id, offer_id="merchant-offer", sku="sku-7", title="Fixture item",
        price=price, currency=currency, observed_at=datetime(2026, 10, 1, tzinfo=UTC),
        source_updated_at=datetime(2026, 9, 30, tzinfo=UTC),
        title_status=FieldStatus.PRESENT, price_status=price_status,
    )


def test_mock_transport_uses_only_allowlisted_read_endpoint_and_bound_store():
    async def run():
        engine, sessions = await _db()
        seen = []
        try:
            async with sessions() as db:
                db.add(Store(id="store-a", company_id="tenant-a", store_name="A"))
                await db.commit()
                def respond(request):
                    seen.append((request.method, request.url.path, request.headers.get("Client-Id")))
                    return httpx.Response(200, json={"result": {"items": []}})
                client = OzonSellerOfflineClient(ProductRepository(db, "tenant-a"), "store-a",
                    OfflineCredentials(), transport=httpx.MockTransport(respond))
                assert (await client.post(ReadOnlyEndpoint.PRODUCT_LIST, {}))["result"]["items"] == []
                assert seen == [("POST", "/v2/product/list", "fixture-client")]
                with pytest.raises(ValueError, match="allowlisted"):
                    await client.post("/v1/product/import", {})
                foreign = OzonSellerOfflineClient(ProductRepository(db, "tenant-b"), "store-a",
                    OfflineCredentials(), transport=httpx.MockTransport(respond))
                with pytest.raises(SellerClientError, match="STORE_FORBIDDEN"):
                    await foreign.post(ReadOnlyEndpoint.PRODUCT_LIST, {})
                assert len(seen) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_live_transport_is_not_accepted_and_credentials_are_not_represented():
    credentials = SellerCredentials("tenant-a", "store-a", "fixture-client", "fixture-key")
    assert "fixture-key" not in repr(credentials)
    with pytest.raises(ValueError, match="offline MockTransport"):
        OzonSellerOfflineClient(None, "store-a", OfflineCredentials(), transport=httpx.HTTPTransport())


@pytest.mark.parametrize("status,code", [(401, "AUTHENTICATION_FAILED"), (403, "PERMISSION_DENIED"),
                                        (404, "API_OR_CREDENTIAL_NOT_FOUND")])
def test_authentication_and_permission_failures_are_structured_not_retried(status, code):
    async def run():
        engine, sessions = await _db()
        calls = []
        try:
            async with sessions() as db:
                db.add(Store(id="store-a", company_id="tenant-a", store_name="A"))
                await db.commit()
                def respond(request):
                    calls.append(request)
                    return httpx.Response(status, text="secret-bearing upstream body")
                client = OzonSellerOfflineClient(ProductRepository(db, "tenant-a"), "store-a",
                    OfflineCredentials(), transport=httpx.MockTransport(respond))
                with pytest.raises(SellerClientError) as caught:
                    await client.post(ReadOnlyEndpoint.PRODUCT_INFO, {})
                assert (caught.value.code, caught.value.http_status, caught.value.attempts) == (code, status, 1)
                assert "secret-bearing" not in str(caught.value)
                assert "fixture-key" not in str(caught.value)
                assert len(calls) == 1
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.parametrize("statuses,expected,delays", [
    ([429, 200], None, [2.0]),
    ([503, 503, 503], "UPSTREAM_FAILURE", [1.0, 2.0]),
    ([429, 429, 429], "RATE_LIMITED", [2.0, 2.0]),
])
def test_rate_limit_retry_after_and_bounded_server_retries(statuses, expected, delays):
    async def run():
        engine, sessions = await _db()
        calls = []
        sleeps = []
        try:
            async with sessions() as db:
                db.add(Store(id="store-a", company_id="tenant-a", store_name="A"))
                await db.commit()
                def respond(request):
                    calls.append(request)
                    return httpx.Response(statuses[len(calls) - 1], headers={"Retry-After": "2"} if statuses[len(calls) - 1] == 429 else {}, json={})
                async def sleep(seconds):
                    sleeps.append(seconds)
                client = OzonSellerOfflineClient(ProductRepository(db, "tenant-a"), "store-a",
                    OfflineCredentials(), transport=httpx.MockTransport(respond), sleep=sleep)
                if expected:
                    with pytest.raises(SellerClientError) as caught:
                        await client.post(ReadOnlyEndpoint.PRODUCT_PRICES, {})
                    assert caught.value.code == expected and caught.value.attempts == 3
                else:
                    assert await client.post(ReadOnlyEndpoint.PRODUCT_PRICES, {}) == {}
                assert len(calls) == len(statuses)
                assert sleeps == delays
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_connection_failure_retries_without_exposing_exception_text():
    async def run():
        engine, sessions = await _db()
        calls = []
        try:
            async with sessions() as db:
                db.add(Store(id="store-a", company_id="tenant-a", store_name="A"))
                await db.commit()
                def respond(request):
                    calls.append(request)
                    raise httpx.ConnectError("fixture-key and private request must stay hidden")
                async def no_sleep(seconds):
                    pass
                client = OzonSellerOfflineClient(ProductRepository(db, "tenant-a"), "store-a",
                    OfflineCredentials(), transport=httpx.MockTransport(respond), sleep=no_sleep)
                with pytest.raises(SellerClientError) as caught:
                    await client.post(ReadOnlyEndpoint.PRODUCT_STOCKS, {})
                assert caught.value.code == "TRANSPORT_FAILURE" and caught.value.attempts == 3
                assert "fixture-key" not in str(caught.value)
                assert len(calls) == 3
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_cursor_pagination_deduplicates_and_rejects_cycles():
    async def run():
        async def page(cursor, limit):
            assert limit == 2
            return ([_observation("101"), _observation("102")], "next") if not cursor else ([_observation("102"), _observation("103")], "")
        assert [item.ozon_product_id for item in await collect_cursor_pages(page, limit=2)] == ["101", "102", "103"]
        async def cycle(cursor, limit):
            return ([_observation("101")], "again")
        with pytest.raises(SellerClientError, match="PAGINATION_CYCLE"):
            await collect_cursor_pages(cycle, limit=2)
    asyncio.run(run())


def test_observation_mapping_ingestion_idempotency_missing_values_and_tenant_scope():
    async def run():
        engine, sessions = await _db()
        try:
            async with sessions() as db:
                db.add(Store(id="store-a", company_id="tenant-a", store_name="A"))
                await db.commit()
                repo_a = ProductRepository(db, "tenant-a")
                first = adapt_seller_observation("store-a", _observation())
                assert first.identity.repository_key() == "ozon:store-a:product:101"
                assert first.identity.offer_id == "merchant-offer" and first.identity.sku == "sku-7"
                product = await ingest_product_result(repo_a, first)
                again = await ingest_product_result(repo_a, first)
                assert again.id == product.id
                count = await db.scalar(select(func.count()).select_from(ProductSnapshot).where(ProductSnapshot.product_id == product.id))
                assert count == 1
                assert product.procurement_cost == 0 and product.compliance_status == "pending"
                assert product.field_lineage["current_price"]["source_type"] == "marketplace"
                assert product.source_updated_at == datetime(2026, 9, 30)
                missing = adapt_seller_observation("store-a", _observation(price=None, price_status=FieldStatus.UNKNOWN))
                same = await ingest_product_result(repo_a, missing)
                assert same.current_price == 100
                assert await ProductRepository(db, "tenant-b").get(product.id) is None
                with pytest.raises(ValueError, match="Store does not belong"):
                    await ingest_product_result(ProductRepository(db, "tenant-b"), first)
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_unverified_or_unknown_currency_cannot_create_sourced_price():
    async def run():
        engine, sessions = await _db()
        try:
            async with sessions() as db:
                db.add(Store(id="store-a", company_id="tenant-a", store_name="A"))
                await db.commit()
                repo = ProductRepository(db, "tenant-a")
                for observation in (_observation(price_status=FieldStatus.UNVERIFIED), _observation(currency=None)):
                    with pytest.raises(ValueError, match="sourced title and price"):
                        await ingest_product_result(repo, adapt_seller_observation("store-a", observation))
                assert await repo.get_by_external_id("ozon:store-a:product:101") is None
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_known_zero_price_is_distinct_from_missing_and_is_source_traced():
    async def run():
        engine, sessions = await _db()
        try:
            async with sessions() as db:
                db.add(Store(id="store-a", company_id="tenant-a", store_name="A"))
                await db.commit()
                result = adapt_seller_observation("store-a", _observation(price=0, price_status=FieldStatus.KNOWN_ZERO))
                product = await ingest_product_result(ProductRepository(db, "tenant-a"), result)
                assert product.current_price == 0
                assert product.raw_payload["_ingestion"]["field_status"]["current_price"] == "KNOWN_ZERO"
                assert product.field_lineage["current_price"]["is_mock"] is False
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_credential_provider_must_match_the_tenant_and_store():
    class WrongCredentials:
        async def for_store(self, company_id, store_id):
            return SellerCredentials("other-tenant", store_id, "fixture-client", "fixture-key")

    async def run():
        engine, sessions = await _db()
        calls = []
        try:
            async with sessions() as db:
                db.add(Store(id="store-a", company_id="tenant-a", store_name="A"))
                await db.commit()
                client = OzonSellerOfflineClient(ProductRepository(db, "tenant-a"), "store-a",
                    WrongCredentials(), transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200, json={})))
                with pytest.raises(SellerClientError, match="CREDENTIAL_UNAVAILABLE"):
                    await client.post(ReadOnlyEndpoint.PRODUCT_LIST, {})
                assert calls == []
        finally:
            await engine.dispose()
    asyncio.run(run())
