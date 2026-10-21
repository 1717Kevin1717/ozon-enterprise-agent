"""Offline wire contract for the user-supplied four-operation OpenAPI extract.

Source SHA-256: 259289b24a64e2a17636ad306927b03b5355806caf1bfdfbe0e36ba8f05fba5a
This is not independent verification of Ozon's current online contract.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Awaitable, Callable, Generic, TypeVar


class SellerContractError(ValueError):
    """A safe error code; never includes an upstream body or credentials."""


T = TypeVar("T")


@dataclass(frozen=True)
class SellerPage(Generic[T]):
    items: tuple[T, ...]
    cursor: str


@dataclass(frozen=True)
class SellerListItem:
    product_id: str
    offer_id: str | None
    sku: str | None


@dataclass(frozen=True)
class SellerDetailItem:
    product_id: str
    offer_id: str | None
    sku: str | None
    title: str | None
    updated_at: datetime | None


@dataclass(frozen=True)
class SellerPriceItem:
    product_id: str
    offer_id: str | None
    price: Decimal | None
    currency: str | None


@dataclass(frozen=True)
class SellerStockUnit:
    sku: str | None
    fulfillment_type: str | None
    shipment_type: str | None
    warehouse_ids: tuple[int, ...] | None
    present: int | None
    reserved: int | None


@dataclass(frozen=True)
class SellerStockItem:
    product_id: str
    offer_id: str | None
    stocks: tuple[SellerStockUnit, ...]


_SHIPMENT_TYPES = {"SHIPMENT_TYPE_GENERAL", "SHIPMENT_TYPE_BOX", "SHIPMENT_TYPE_PALLET"}


def _object(value: object, code: str) -> dict:
    if not isinstance(value, dict):
        raise SellerContractError(code)
    return value


def _items(value: object) -> list:
    if not isinstance(value, list):
        raise SellerContractError("INVALID_ITEMS")
    return value


def _id(value: object) -> str:
    # Response IDs are OpenAPI int64; requests carry decimal strings.
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise SellerContractError("INVALID_PRODUCT_ID")
    return str(value)


def _optional_id(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise SellerContractError("INVALID_SKU")
    return str(value)


def _optional_text(value: object, code: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SellerContractError(code)
    return value or None


def _cursor(value: object) -> str:
    if not isinstance(value, str):
        raise SellerContractError("INVALID_CURSOR")
    return value


def _limit(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 1000:
        raise SellerContractError("INVALID_LIMIT")
    return value


def _request_ids(values: tuple[str, ...] | list[str], code: str) -> list[str]:
    if not isinstance(values, (tuple, list)) or len(values) > 1000:
        raise SellerContractError(code)
    if any(not isinstance(item, str) or not item.strip() for item in values):
        raise SellerContractError(code)
    return list(values)


def _filter(*, offer_ids: tuple[str, ...] = (), product_ids: tuple[str, ...] = (),
            visibility: str | None = None) -> dict:
    result: dict = {}
    if offer_ids:
        result["offer_id"] = _request_ids(offer_ids, "INVALID_OFFER_IDS")
    if product_ids:
        result["product_id"] = _request_ids(product_ids, "INVALID_PRODUCT_IDS")
    if visibility is not None:
        # The extract enumerates more states, but ALL is the only default
        # needed for an unfiltered read. Do not accept an unchecked value.
        if visibility != "ALL":
            raise SellerContractError("UNSUPPORTED_VISIBILITY")
        result["visibility"] = visibility
    return result


def build_product_list_request(*, last_id: str = "", limit: int = 100,
                               offer_ids: tuple[str, ...] = (), product_ids: tuple[str, ...] = ()) -> dict:
    return {"filter": _filter(offer_ids=offer_ids, product_ids=product_ids),
            "last_id": _cursor(last_id), "limit": _limit(limit)}


def build_product_info_request(*, offer_ids: tuple[str, ...] = (),
                               product_ids: tuple[str, ...] = (), skus: tuple[str, ...] = ()) -> dict:
    supplied = [("offer_id", offer_ids), ("product_id", product_ids), ("sku", skus)]
    chosen = [(name, values) for name, values in supplied if values]
    if len(chosen) != 1:
        raise SellerContractError("IDENTIFIER_FILTER_REQUIRED")
    name, values = chosen[0]
    return {name: _request_ids(values, "INVALID_IDENTIFIER_BATCH")}


def build_product_prices_request(*, cursor: str = "", limit: int = 100,
                                 offer_ids: tuple[str, ...] = (), product_ids: tuple[str, ...] = ()) -> dict:
    return {"cursor": _cursor(cursor), "filter": _filter(offer_ids=offer_ids, product_ids=product_ids,
                                                           visibility="ALL"), "limit": _limit(limit)}


def build_product_stocks_request(*, cursor: str = "", limit: int = 100,
                                 offer_ids: tuple[str, ...] = (), product_ids: tuple[str, ...] = ()) -> dict:
    return {"cursor": _cursor(cursor), "filter": _filter(offer_ids=offer_ids, product_ids=product_ids,
                                                           visibility="ALL"), "limit": _limit(limit)}


def decode_product_list(body: object) -> SellerPage[SellerListItem]:
    result = _object(_object(body, "INVALID_RESPONSE").get("result"), "INVALID_RESULT")
    rows = []
    for raw in _items(result.get("items")):
        row = _object(raw, "INVALID_PRODUCT_ITEM")
        rows.append(SellerListItem(_id(row.get("product_id")),
                                   _optional_text(row.get("offer_id"), "INVALID_OFFER_ID"),
                                   _optional_id(row.get("sku"))))
    return SellerPage(tuple(rows), _cursor(result.get("last_id")))


def decode_product_info(body: object) -> tuple[SellerDetailItem, ...]:
    rows = []
    for raw in _items(_object(body, "INVALID_RESPONSE").get("items")):
        row = _object(raw, "INVALID_PRODUCT_ITEM")
        timestamp = row.get("updated_at")
        if timestamp is not None:
            if not isinstance(timestamp, str):
                raise SellerContractError("INVALID_UPDATED_AT")
            try:
                timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError:
                raise SellerContractError("INVALID_UPDATED_AT") from None
            if timestamp.tzinfo is None:
                raise SellerContractError("INVALID_UPDATED_AT")
        rows.append(SellerDetailItem(_id(row.get("id")),
                                     _optional_text(row.get("offer_id"), "INVALID_OFFER_ID"),
                                     _optional_id(row.get("sku")),
                                     _optional_text(row.get("name"), "INVALID_TITLE"), timestamp))
    return tuple(rows)


def _price(value: object) -> Decimal | None:
    if value is None:
        return None
    # OpenAPI declares a JSON number, never a numeric string for this field.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SellerContractError("INVALID_PRICE")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise SellerContractError("INVALID_PRICE") from None
    if not amount.is_finite() or amount < 0:
        raise SellerContractError("INVALID_PRICE")
    return amount


def decode_product_prices(body: object) -> SellerPage[SellerPriceItem]:
    doc = _object(body, "INVALID_RESPONSE")
    rows = []
    for raw in _items(doc.get("items")):
        row = _object(raw, "INVALID_PRICE_ITEM")
        price_obj = row.get("price")
        price_obj = {} if price_obj is None else _object(price_obj, "INVALID_PRICE_OBJECT")
        rows.append(SellerPriceItem(_id(row.get("product_id")),
                                    _optional_text(row.get("offer_id"), "INVALID_OFFER_ID"),
                                    _price(price_obj.get("price")),
                                    _optional_text(price_obj.get("currency_code"), "INVALID_CURRENCY")))
    return SellerPage(tuple(rows), _cursor(doc.get("cursor")))


def _quantity(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SellerContractError("INVALID_STOCK_QUANTITY")
    return value


def decode_product_stocks(body: object) -> SellerPage[SellerStockItem]:
    doc = _object(body, "INVALID_RESPONSE")
    rows = []
    for raw in _items(doc.get("items")):
        row = _object(raw, "INVALID_STOCK_ITEM")
        stocks = []
        seen_granules = set()
        for unit in _items(row.get("stocks")):
            unit = _object(unit, "INVALID_STOCK_UNIT")
            warehouse_value = unit.get("warehouse_ids")
            warehouses = None if warehouse_value is None else _items(warehouse_value)
            if warehouses is not None and any(not isinstance(w, int) or isinstance(w, bool) or w <= 0 for w in warehouses):
                raise SellerContractError("INVALID_WAREHOUSE_ID")
            shipment = _optional_text(unit.get("shipment_type"), "INVALID_SHIPMENT_TYPE")
            if shipment is not None and shipment not in _SHIPMENT_TYPES:
                raise SellerContractError("INVALID_SHIPMENT_TYPE")
            stock = SellerStockUnit(_optional_id(unit.get("sku")),
                                    _optional_text(unit.get("type"), "INVALID_FULFILLMENT_TYPE"),
                                    shipment, tuple(warehouses) if warehouses is not None else None,
                                    _quantity(unit.get("present")), _quantity(unit.get("reserved")))
            granule = (stock.sku, stock.fulfillment_type, stock.shipment_type,
                       tuple(sorted(stock.warehouse_ids)) if stock.warehouse_ids is not None else None)
            if granule in seen_granules:
                # Never sum or overwrite even conflicting values for one granule.
                raise SellerContractError("DUPLICATE_STOCK_GRANULE")
            seen_granules.add(granule)
            stocks.append(stock)
        rows.append(SellerStockItem(_id(row.get("product_id")),
                                    _optional_text(row.get("offer_id"), "INVALID_OFFER_ID"), tuple(stocks)))
    return SellerPage(tuple(rows), _cursor(doc.get("cursor")))


async def collect_pages(fetch_page: Callable[[str, int], Awaitable[SellerPage[T]]],
                        key: Callable[[T], str], *, limit: int = 100,
                        max_pages: int = 1000) -> tuple[T, ...]:
    """Return only a complete, bounded set; a failed page returns no partial data."""
    _limit(limit)
    if not isinstance(max_pages, int) or isinstance(max_pages, bool) or not 1 <= max_pages <= 1000:
        raise SellerContractError("INVALID_PAGE_BOUND")
    cursor = ""
    visited = {cursor}
    found: dict[str, T] = {}
    for _ in range(max_pages):
        page = await fetch_page(cursor, limit)
        for item in page.items:
            identity = key(item)
            if not identity:
                raise SellerContractError("INVALID_PRODUCT_ID")
            if identity in found and found[identity] != item:
                raise SellerContractError("CONFLICTING_DUPLICATE_PRODUCT")
            found.setdefault(identity, item)
        if not page.cursor:
            return tuple(found.values())
        if page.cursor in visited:
            raise SellerContractError("PAGINATION_CYCLE")
        visited.add(page.cursor)
        cursor = page.cursor
    raise SellerContractError("PAGINATION_LIMIT")
