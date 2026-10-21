"""Offline four-operation contract regression; no live Seller API access."""

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.connectors.base import FieldStatus
from app.connectors.ozon_seller_contract import (
    SellerContractError, SellerPage, build_product_info_request,
    build_product_list_request, build_product_prices_request,
    build_product_stocks_request, collect_pages, decode_product_info,
    decode_product_list, decode_product_prices, decode_product_stocks,
)
from app.connectors.ozon_seller_offline import (
    OzonSellerOfflineClient, SellerCredentials, adapt_seller_observation,
    observation_from_detail_and_price,
)
from app.db.models import Base, ProductSnapshot, Store
from app.repositories.products import ProductRepository
from app.services.product_ingestion import ingest_product_result, ingest_seller_stock_observation


class FixtureCredentials:
    async def for_store(self, company_id, store_id):
        return SellerCredentials(company_id, store_id, "fixture-client", "fixture-key")


async def _repo():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _list_item(product_id=101, offer_id="same-offer"):
    return {"product_id": product_id, "offer_id": offer_id, "sku": 701}


def _detail(product_id=101, title="Fixture product"):
    return {"id": product_id, "offer_id": "same-offer", "sku": 701,
            "name": title, "updated_at": "2026-10-20T12:00:00Z"}


def _price(product_id=101, amount=125.5, currency="RUB"):
    return {"product_id": product_id, "offer_id": "same-offer",
            "price": {"price": amount, "currency_code": currency}}


def _stock(product_id=101, present=0, reserved=None):
    return {"product_id": product_id, "offer_id": "same-offer",
            "stocks": [{"sku": 701, "type": "fbs", "shipment_type": "SHIPMENT_TYPE_GENERAL",
                        "warehouse_ids": [10], "present": present, "reserved": reserved}]}


def test_request_builders_use_four_distinct_extract_contracts():
    assert build_product_list_request(last_id="next", limit=10, product_ids=("101",)) == {
        "filter": {"product_id": ["101"]}, "last_id": "next", "limit": 10,
    }
    assert build_product_info_request(product_ids=("101", "102")) == {"product_id": ["101", "102"]}
    assert build_product_prices_request(cursor="p", limit=20)["filter"] == {"visibility": "ALL"}
    assert build_product_stocks_request(cursor="s", limit=20)["cursor"] == "s"
    for build in (build_product_list_request, build_product_prices_request, build_product_stocks_request):
        with pytest.raises(SellerContractError, match="INVALID_LIMIT"):
            build(limit=0)
        with pytest.raises(SellerContractError, match="INVALID_LIMIT"):
            build(limit=1001)
    with pytest.raises(SellerContractError, match="IDENTIFIER_FILTER_REQUIRED"):
        build_product_info_request()
    with pytest.raises(SellerContractError, match="IDENTIFIER_FILTER_REQUIRED"):
        build_product_info_request(product_ids=("101",), offer_ids=("offer",))
    with pytest.raises(SellerContractError, match="INVALID_IDENTIFIER_BATCH"):
        build_product_info_request(product_ids=tuple(str(i) for i in range(1001)))


def test_list_result_last_id_identity_and_missing_or_invalid_structure():
    page = decode_product_list({"result": {"items": [_list_item()], "last_id": "next"}})
    assert page.cursor == "next" and page.items[0].product_id == "101"
    assert page.items[0].offer_id == "same-offer" and page.items[0].sku == "701"
    assert decode_product_list({"result": {"items": [], "last_id": ""}}).items == ()
    for body in ({"items": []}, {"result": {"items": []}},
                 {"result": {"items": [{"product_id": True}], "last_id": ""}}):
        with pytest.raises(SellerContractError):
            decode_product_list(body)


def test_info_batch_identity_missing_fields_and_type_rejection():
    row = decode_product_info({"items": [_detail()]})[0]
    assert row.product_id == "101" and row.title == "Fixture product"
    assert row.updated_at == datetime(2026, 10, 20, 12, tzinfo=UTC)
    assert decode_product_info({"items": [{"id": 102}]})[0].title is None
    assert decode_product_info({"items": []}) == ()
    for body in ({}, {"items": [{"id": "101"}]},
                 {"items": [{"id": 101, "updated_at": "not-a-time"}]},
                 {"items": [{"id": 101, "name": 23}]}):
        with pytest.raises(SellerContractError):
            decode_product_info(body)


def test_price_number_null_zero_currency_and_invalid_values():
    page = decode_product_prices({"items": [_price()], "cursor": "p2"})
    assert page.cursor == "p2" and page.items[0].price == Decimal("125.5")
    assert page.items[0].currency == "RUB"
    assert decode_product_prices({"items": [_price(amount=0)], "cursor": ""}).items[0].price == 0
    assert decode_product_prices({"items": [_price(amount=None, currency=None)], "cursor": ""}).items[0].price is None
    for bad in ("125.5", -1, True, float("nan")):
        with pytest.raises(SellerContractError, match="INVALID_PRICE"):
            decode_product_prices({"items": [_price(amount=bad)], "cursor": ""})
    with pytest.raises(SellerContractError, match="INVALID_CURSOR"):
        decode_product_prices({"items": [], "cursor": None})


def test_stock_granularity_unknown_zero_and_invalid_values():
    unit = decode_product_stocks({"items": [_stock()], "cursor": ""}).items[0].stocks[0]
    assert unit.sku == "701" and unit.fulfillment_type == "fbs"
    assert unit.warehouse_ids == (10,) and unit.present == 0 and unit.reserved is None
    assert decode_product_stocks({"items": [_stock(reserved=0)], "cursor": ""}).items[0].stocks[0].reserved == 0
    assert decode_product_stocks({"items": [{"product_id": 101, "stocks": []}], "cursor": ""}).items[0].stocks == ()
    unknown_warehouse = _stock()
    unknown_warehouse["stocks"][0].pop("warehouse_ids")
    assert decode_product_stocks({"items": [unknown_warehouse], "cursor": ""}).items[0].stocks[0].warehouse_ids is None
    duplicated = _stock()
    conflicting = {**duplicated["stocks"][0], "present": 3}
    duplicated["stocks"].append(conflicting)
    with pytest.raises(SellerContractError, match="DUPLICATE_STOCK_GRANULE"):
        decode_product_stocks({"items": [duplicated], "cursor": ""})
    for altered in ({"present": "0"}, {"warehouse_ids": ["10"]},
                    {"shipment_type": "UNKNOWN"}, {"reserved": -1}):
        bad = _stock()
        bad["stocks"][0].update(altered)
        with pytest.raises(SellerContractError):
            decode_product_stocks({"items": [bad], "cursor": ""})


def test_cursor_dedup_cycle_conflict_limit_and_partial_failure():
    async def run():
        item = decode_product_list({"result": {"items": [_list_item()], "last_id": ""}}).items[0]
        other = decode_product_list({"result": {"items": [_list_item(102)], "last_id": ""}}).items[0]
        async def pages(cursor, limit):
            return SellerPage((item,), "next") if not cursor else SellerPage((item, other), "")
        assert [x.product_id for x in await collect_pages(pages, lambda x: x.product_id, limit=2)] == ["101", "102"]
        async def cycle(cursor, limit):
            return SellerPage((item,), "same")
        with pytest.raises(SellerContractError, match="PAGINATION_CYCLE"):
            await collect_pages(cycle, lambda x: x.product_id)
        with pytest.raises(SellerContractError, match="PAGINATION_LIMIT"):
            await collect_pages(pages, lambda x: x.product_id, max_pages=1)
        changed = decode_product_list({"result": {"items": [_list_item(101, "changed")], "last_id": ""}}).items[0]
        async def conflict(cursor, limit):
            return SellerPage((item,), "next") if not cursor else SellerPage((changed,), "")
        with pytest.raises(SellerContractError, match="CONFLICTING_DUPLICATE_PRODUCT"):
            await collect_pages(conflict, lambda x: x.product_id)
        async def failed(cursor, limit):
            if cursor:
                raise SellerContractError("SECOND_PAGE_FAILED")
            return SellerPage((item,), "next")
        with pytest.raises(SellerContractError, match="SECOND_PAGE_FAILED"):
            await collect_pages(failed, lambda x: x.product_id)
    asyncio.run(run())


def test_four_mock_http_operations_and_filter_result_guard():
    async def run():
        engine, sessions = await _repo()
        try:
            async with sessions() as db:
                db.add(Store(id="store-a", company_id="tenant-a", store_name="A"))
                await db.commit()
                seen = []
                def respond(request):
                    seen.append((request.url.path, request.method, request.headers.get("Client-Id"), request.content))
                    path = request.url.path
                    if path.endswith("/product/list"):
                        return httpx.Response(200, json={"result": {"items": [_list_item()], "last_id": ""}})
                    if path.endswith("/product/info/list"):
                        return httpx.Response(200, json={"items": [_detail()]})
                    if path.endswith("/product/info/prices"):
                        return httpx.Response(200, json={"items": [_price()], "cursor": ""})
                    return httpx.Response(200, json={"items": [_stock()], "cursor": ""})
                client = OzonSellerOfflineClient(ProductRepository(db, "tenant-a"), "store-a",
                    FixtureCredentials(), transport=httpx.MockTransport(respond))
                assert (await client.product_list_page()).items[0].product_id == "101"
                assert (await client.product_info(product_ids=("101",)))[0].sku == "701"
                assert (await client.product_prices_page()).items[0].price == Decimal("125.5")
                assert (await client.product_stocks_page()).items[0].stocks[0].present == 0
                assert [row[0] for row in seen] == ["/v3/product/list", "/v3/product/info/list",
                                                       "/v5/product/info/prices", "/v4/product/info/stocks"]
                assert all(row[1:3] == ("POST", "fixture-client") for row in seen)
                assert b'"product_id":["101"]' in seen[1][3]
                with pytest.raises(SellerContractError, match="FILTER_RESULT_MISMATCH"):
                    await client.product_info(product_ids=("999",))
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_mock_http_pagination_uses_endpoint_specific_cursor_and_deduplicates():
    async def run():
        engine, sessions = await _repo()
        try:
            async with sessions() as db:
                db.add(Store(id="store-a", company_id="tenant-a", store_name="A"))
                await db.commit()
                seen = []
                def respond(request):
                    body = json.loads(request.content)
                    path = request.url.path
                    seen.append((path, body))
                    if path.endswith("/product/list"):
                        return httpx.Response(200, json={"result": {"items": [_list_item()] if not body["last_id"] else
                            [_list_item(), _list_item(102)], "last_id": "next" if not body["last_id"] else ""}})
                    if path.endswith("/prices"):
                        return httpx.Response(200, json={"items": [_price()] if not body["cursor"] else
                            [_price(), _price(102)], "cursor": "next" if not body["cursor"] else ""})
                    return httpx.Response(200, json={"items": [_stock()] if not body["cursor"] else
                        [_stock(), _stock(102)], "cursor": "next" if not body["cursor"] else ""})
                client = OzonSellerOfflineClient(ProductRepository(db, "tenant-a"), "store-a",
                    FixtureCredentials(), transport=httpx.MockTransport(respond))
                assert [x.product_id for x in await client.collect_product_list(limit=1)] == ["101", "102"]
                assert [x.product_id for x in await client.collect_product_prices(limit=1)] == ["101", "102"]
                assert [x.product_id for x in await client.collect_product_stocks(limit=1)] == ["101", "102"]
                assert [body["last_id"] for path, body in seen if path.endswith("/product/list")] == ["", "next"]
                assert [body["cursor"] for path, body in seen if path.endswith("/prices")] == ["", "next"]
                assert [body["cursor"] for path, body in seen if path.endswith("/stocks")] == ["", "next"]
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_failed_second_page_and_invalid_price_cannot_ingest_partial_product():
    async def run():
        engine, sessions = await _repo()
        try:
            async with sessions() as db:
                db.add(Store(id="store-a", company_id="tenant-a", store_name="A"))
                await db.commit()
                def respond(request):
                    body = json.loads(request.content)
                    if body["last_id"]:
                        return httpx.Response(200, json={"result": {"items": [{"product_id": "bad"}], "last_id": ""}})
                    return httpx.Response(200, json={"result": {"items": [_list_item()], "last_id": "next"}})
                repo = ProductRepository(db, "tenant-a")
                client = OzonSellerOfflineClient(repo, "store-a", FixtureCredentials(),
                                                  transport=httpx.MockTransport(respond))
                with pytest.raises(SellerContractError, match="INVALID_PRODUCT_ID"):
                    await client.collect_product_list()
                assert await repo.get_by_external_id("ozon:store-a:product:101") is None
                with pytest.raises(SellerContractError, match="INVALID_PRICE"):
                    decode_product_prices({"items": [_price(amount="100")], "cursor": ""})
                assert await repo.get_by_external_id("ozon:store-a:product:101") is None
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_join_then_ingest_preserves_tenant_store_cost_and_field_provenance():
    async def run():
        engine, sessions = await _repo()
        try:
            async with sessions() as db:
                db.add_all([Store(id="store-a", company_id="tenant-a", store_name="A"),
                            Store(id="store-b", company_id="tenant-a", store_name="B")])
                await db.commit()
                detail = decode_product_info({"items": [_detail()]})[0]
                price = decode_product_prices({"items": [_price()], "cursor": ""}).items[0]
                observed = datetime(2026, 10, 21, tzinfo=UTC)
                adapter = adapt_seller_observation("store-a", observation_from_detail_and_price(detail, price, observed_at=observed))
                assert adapter.source_updated_at is None
                assert adapter.field_lineage["title"]["updated_at"] == "2026-10-20T12:00:00+00:00"
                repo = ProductRepository(db, "tenant-a")
                product = await ingest_product_result(repo, adapter)
                assert product.external_product_id == "ozon:store-a:product:101"
                assert product.field_lineage["current_price"]["source_type"] == "marketplace"
                assert product.field_lineage["current_price"]["is_mock"] is True
                assert product.sync_status == "offline_fixture"
                product.procurement_cost = 44
                product.compliance_status = "manual_review"
                await db.commit()
                second = await ingest_product_result(repo, adapter)
                assert second.id == product.id and second.procurement_cost == 44
                assert second.compliance_status == "manual_review"
                assert await db.scalar(select(func.count()).select_from(ProductSnapshot).where(ProductSnapshot.product_id == product.id)) == 1
                separate = await ingest_product_result(repo, adapt_seller_observation("store-b",
                    observation_from_detail_and_price(detail, price, observed_at=observed)))
                assert separate.id != product.id
                assert await ProductRepository(db, "tenant-b").get_by_external_id(product.external_product_id) is None
                null_price = decode_product_prices({"items": [_price(amount=None)], "cursor": ""}).items[0]
                safe = adapt_seller_observation("store-a", observation_from_detail_and_price(detail, null_price, observed_at=observed))
                assert safe.field_status["current_price"] == FieldStatus.UNKNOWN
                await ingest_product_result(repo, safe)
                assert (await repo.get(product.id)).current_price == 125.5
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_stock_observation_persists_granules_without_changing_product_facts():
    async def run():
        engine, sessions = await _repo()
        try:
            async with sessions() as db:
                db.add_all([Store(id="store-a", company_id="tenant-a", store_name="A"),
                            Store(id="store-b", company_id="tenant-a", store_name="B")])
                await db.commit()
                detail = decode_product_info({"items": [_detail()]})[0]
                price = decode_product_prices({"items": [_price()], "cursor": ""}).items[0]
                observed = datetime(2026, 10, 21, tzinfo=UTC)
                repo = ProductRepository(db, "tenant-a")
                product = await ingest_product_result(repo, adapt_seller_observation("store-a",
                    observation_from_detail_and_price(detail, price, observed_at=observed)))
                product.procurement_cost = 44
                product.compliance_status = "manual_review"
                await db.commit()
                item = decode_product_stocks({"items": [_stock(present=0, reserved=None)], "cursor": ""}).items[0]
                saved = await ingest_seller_stock_observation(repo, "store-a", item, observed_at=observed)
                inventory = saved.raw_payload["_seller_inventory"]
                assert inventory["is_mock"] is True and inventory["source_type"] == "OZON_SELLER"
                assert inventory["stock_units"] == [{"sku": 701, "type": "fbs",
                    "shipment_type": "SHIPMENT_TYPE_GENERAL", "warehouse_ids": [10],
                    "present": 0, "reserved": None}]
                assert saved.current_price == 125.5 and saved.procurement_cost == 44
                assert saved.compliance_status == "manual_review"
                assert saved.latest_30d_sales == 0  # unchanged Product default, not an inferred stock-to-sales value
                count = await db.scalar(select(func.count()).select_from(ProductSnapshot).where(ProductSnapshot.product_id == product.id))
                assert count == 2
                await ingest_seller_stock_observation(repo, "store-a", item, observed_at=observed)
                assert await db.scalar(select(func.count()).select_from(ProductSnapshot).where(ProductSnapshot.product_id == product.id)) == count
                next_observed = datetime(2026, 10, 22, tzinfo=UTC)
                new_price = decode_product_prices({"items": [_price(amount=130)], "cursor": ""}).items[0]
                refreshed = await ingest_product_result(repo, adapt_seller_observation("store-a",
                    observation_from_detail_and_price(detail, new_price, observed_at=next_observed)))
                assert refreshed.current_price == 130
                assert refreshed.raw_payload["_seller_inventory"] == inventory
                with pytest.raises(ValueError, match="store and tenant"):
                    await ingest_seller_stock_observation(ProductRepository(db, "tenant-b"), "store-a", item, observed_at=observed)
                with pytest.raises(ValueError, match="store and tenant"):
                    await ingest_seller_stock_observation(repo, "store-b", item, observed_at=observed)
                wrong_offer = decode_product_stocks({"items": [_stock()], "cursor": ""}).items[0]
                from dataclasses import replace
                with pytest.raises(ValueError, match="offer ID"):
                    await ingest_seller_stock_observation(repo, "store-a", replace(wrong_offer, offer_id="other"),
                                                          observed_at=observed)
                assert (await repo.get(product.id)).raw_payload["_seller_inventory"] == inventory
        finally:
            await engine.dispose()
    asyncio.run(run())
