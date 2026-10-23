"""Validated connector-to-repository boundary; no network access or model calls."""

from datetime import UTC, datetime
from typing import Any

from app.connectors.base import AdapterResult, DataSourceType, ExternalProductIdentity, FieldStatus
from app.connectors.ozon_seller_contract import SellerStockItem, decode_product_stocks
from app.db.models import Product
from app.repositories.products import ProductRepository
from app.schemas.products import ProductIn, ProductPatch
from app.services.provenance import ALIASES


_COST_AND_REVIEW = {
    "procurement_cost", "fulfillment_cost", "shipping_cost", "platform_fee",
    "advertising_cost", "warehousing_cost", "tax_cost", "return_loss_reserve",
    "other_cost", "platform_commission_rate", "target_margin_rate",
    "compliance_status", "compliance_note", "certificates", "manual_risk_level",
    "lifecycle_status", "tags",
}
_SELLER_FIELDS = {
    "title", "brand", "category_path", "sku", "url", "main_image_url",
    "image_urls", "seller_name", "current_price", "regular_price", "rating",
    "review_count", "latest_30d_sales", "sales_growth_rate", "sales_source",
    "sales_evidence",
}
_MARKET_FIELDS = {
    "competitor_price_min", "competitor_price_avg", "competitor_count",
    "price_competition_score", "market_saturation", "competition_evidence",
    "search_volume", "trend_score", "competitor_entries",
    "visible_lowest_competitor_price",
}
_INTERNAL_FIELDS = _COST_AND_REVIEW | {"latest_30d_sales", "sales_growth_rate", "sales_source", "sales_evidence"}
_MERCHANT_CSV_FIELDS = _INTERNAL_FIELDS | {"title", "category_path", "current_price"}
_META_FIELDS = {"external_product_id", "store_id", "raw_payload", "field_lineage", "captured_at"}
_SOURCE_KIND = {
    DataSourceType.DEMO_MOCK: "mock",
    DataSourceType.OZON_SELLER: "marketplace",
    DataSourceType.MERCHANT_INTERNAL: "enterprise_internal",
    DataSourceType.AUTHORIZED_MARKET_DATA: "marketplace",
}


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)).isoformat()


def _field_status(result: AdapterResult, field: str, value: Any) -> FieldStatus:
    if field in result.field_status:
        return FieldStatus(result.field_status[field])
    if value is None:
        return FieldStatus.UNKNOWN
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0:
        return FieldStatus.KNOWN_ZERO
    return FieldStatus.PRESENT


def _validated_fields(result: AdapterResult) -> tuple[dict[str, Any], dict[str, str], dict[str, dict]]:
    assert result.source_type is not None
    allowed = (
        set(ProductIn.model_fields) - _META_FIELDS
        if result.source_type == DataSourceType.DEMO_MOCK else
        _SELLER_FIELDS if result.source_type == DataSourceType.OZON_SELLER else
        _MARKET_FIELDS if result.source_type == DataSourceType.AUTHORIZED_MARKET_DATA else
        _MERCHANT_CSV_FIELDS if result.provider == "merchant_csv_import" else _INTERNAL_FIELDS
    )
    values: dict[str, Any] = {}
    statuses: dict[str, str] = {}
    lineage: dict[str, dict] = {}
    for name, value in result.data.items():
        if name in _META_FIELDS:
            continue
        if name not in ProductIn.model_fields:
            raise ValueError(f"Unknown product field: {name}")
        status = _field_status(result, name, value)
        statuses[name] = status.value
        if status in {FieldStatus.UNKNOWN, FieldStatus.MISSING, FieldStatus.UNVERIFIED}:
            continue
        if value is None or (status == FieldStatus.KNOWN_ZERO and (isinstance(value, bool) or value != 0)):
            raise ValueError(f"Inconsistent field status: {name}")
        if name not in allowed:
            # A source cannot claim ownership of costs or human review fields.
            continue
        values[name] = value
        old = result.field_lineage.get(name, result.field_lineage.get(ALIASES.get(name, ""), {}))
        entry = {"source": old} if isinstance(old, str) else dict(old) if isinstance(old, dict) else {}
        provider = str(entry.get("provider") or entry.get("source") or result.provider) if result.source_type == DataSourceType.DEMO_MOCK else result.provider
        entry.update({"source_type": _SOURCE_KIND[result.source_type], "provider": provider,
                      "is_mock": result.source_type == DataSourceType.DEMO_MOCK or result.confidence == "synthetic"})
        if result.observed_at is not None:
            entry["observed_at"] = _timestamp(result.observed_at)
        entry["collected_at"] = _timestamp(datetime.now(UTC))
        if result.source_updated_at is not None:
            entry["updated_at"] = _timestamp(result.source_updated_at)
        lineage[name] = entry
    for name, status in result.field_status.items():
        if name in _META_FIELDS or name in statuses:
            continue
        if name not in ProductIn.model_fields:
            raise ValueError(f"Unknown product field: {name}")
        normalized = FieldStatus(status)
        if normalized in {FieldStatus.PRESENT, FieldStatus.KNOWN_ZERO}:
            raise ValueError(f"Field status has no value: {name}")
        statuses[name] = normalized.value
    return values, statuses, lineage


async def ingest_product_result(repo: ProductRepository, result: AdapterResult, *, commit: bool = True) -> Product:
    """Apply only supplied, source-owned fields; absent data never becomes a zero update."""
    identity = result.identity
    if not identity or not result.source_type or identity.source_type != result.source_type:
        raise ValueError("Connector source and external identity must agree")
    if not result.provider or not result.source_reference:
        raise ValueError("Connector provider and source reference are required")
    key = identity.repository_key()
    if len(key) > 128:
        raise ValueError("External repository key is too long")
    if identity.sku and result.data.get("sku") not in (None, "", identity.sku):
        raise ValueError("SKU identity mismatch")
    if identity.store_id:
        from sqlalchemy import select
        from app.db.models import Store
        store = await repo.session.scalar(select(Store.id).where(Store.id == identity.store_id, Store.company_id == repo.company_id))
        if not store:
            raise ValueError("Store does not belong to this company")
    values, statuses, lineage = _validated_fields(result)
    if identity.sku and result.source_type in {DataSourceType.DEMO_MOCK, DataSourceType.OZON_SELLER}:
        values["sku"] = identity.sku
    product = await (repo.get(identity.internal_product_id) if identity.internal_product_id else repo.get_by_external_id(key))
    if identity.internal_product_id and product is None:
        raise ValueError("Target product does not belong to this company")
    if product and result.source_type in {DataSourceType.DEMO_MOCK, DataSourceType.OZON_SELLER} and product.external_product_id != key:
        raise ValueError("External identity does not match target product")
    if product and identity.store_id and product.store_id != identity.store_id:
        raise ValueError("Target product belongs to another store")
    merchant_csv = result.source_type == DataSourceType.MERCHANT_INTERNAL and result.provider == "merchant_csv_import"
    if product is None and result.source_type not in {DataSourceType.DEMO_MOCK, DataSourceType.OZON_SELLER} and not merchant_csv:
        raise ValueError("Internal or market signals require an existing tenant product")
    if product is not None and merchant_csv:
        if product.external_product_id != key:
            raise ValueError("Merchant identity does not match target product")
        if product.source_updated_at and result.source_updated_at:
            old_time = product.source_updated_at.replace(tzinfo=UTC) if product.source_updated_at.tzinfo is None else product.source_updated_at
            if result.source_updated_at < old_time:
                raise ValueError("OLDER_SOURCE_UPDATE")
        for name in tuple(values):
            old_lineage = (product.field_lineage or {}).get(name) or {}
            if isinstance(old_lineage, dict) and old_lineage.get("source_type") == "manual" and old_lineage.get("provider") == "manual_operator":
                values.pop(name)
                lineage.pop(name, None)
    if product is None and not {"title", "current_price"} <= values.keys():
        raise ValueError("New products require sourced title and price")
    metadata = {
        "source_type": result.source_type.value, "provider": result.provider,
        "offline_fixture": result.confidence == "synthetic",
        "source_reference": result.source_reference,
        "identity": {"store_id": identity.store_id, "ozon_product_id": identity.ozon_product_id,
                     "offer_id": identity.offer_id, "sku": identity.sku},
        "field_status": statuses, "observed_at": _timestamp(result.observed_at),
        "source_updated_at": _timestamp(result.source_updated_at),
    }
    raw_payload = dict(result.data.get("raw_payload") or {}) if result.source_type == DataSourceType.DEMO_MOCK else {}
    if product is not None and result.source_type == DataSourceType.OZON_SELLER:
        existing_inventory = (product.raw_payload or {}).get("_seller_inventory")
        if isinstance(existing_inventory, dict):
            # A later catalog/price sync must not erase a separately observed
            # warehouse-granular stock snapshot for this same store/product.
            raw_payload["_seller_inventory"] = existing_inventory
    if merchant_csv:
        previous_raw = product.raw_payload or {} if product is not None else {}
        merged_status = dict(previous_raw.get("_merchant_field_status") or {})
        for name in values:
            if name in statuses:
                merged_status[name] = statuses[name]
        raw_payload["_merchant_field_status"] = merged_status
        raw_payload["_merchant_import"] = {
            "source_type": DataSourceType.MERCHANT_INTERNAL.value,
            "last_file_sha256": result.source_reference.split(":")[1],
            "price_kind": "merchant_estimate",
            "currency": "RUB",
        }
    identities = dict((product.raw_payload or {}).get("_external_identities") or {}) if product is not None else {}
    if result.source_type in {DataSourceType.DEMO_MOCK, DataSourceType.OZON_SELLER}:
        identities[result.source_type.value] = {"external_product_id": key, **metadata["identity"]}
    raw_payload["_external_identities"] = identities
    raw_payload["_ingestion"] = metadata
    if product is not None:
        if not values:
            return product
        if merchant_csv and (product.raw_payload or {}).get("_merchant_import", {}).get("last_file_sha256") == raw_payload["_merchant_import"]["last_file_sha256"] and all(getattr(product, name) == value for name, value in values.items() if name in ProductPatch.model_fields):
            return product
        previous = (product.raw_payload or {}).get("_ingestion")
        comparable = {name: value for name, value in values.items() if name in ProductPatch.model_fields and name not in {"competitor_entries", "visible_lowest_competitor_price"}}
        same_raw = all((product.raw_payload or {}).get(raw_key) == values[name]
                       for name, raw_key in (("competitor_entries", "visibleCompetitors"), ("visible_lowest_competitor_price", "visibleLowestCompetitorPriceRub"))
                       if name in values)
        if previous == metadata and same_raw and all(getattr(product, name) == value for name, value in comparable.items()):
            return product
        patch_values = {name: value for name, value in values.items() if name in ProductPatch.model_fields}
        patch_values["raw_payload"] = raw_payload
        product = await repo.patch(product, ProductPatch.model_validate(patch_values), field_lineage=lineage, captured_at=result.observed_at, commit=commit)
    else:
        create_values = {**values, "external_product_id": key, "store_id": identity.store_id,
                         "field_lineage": lineage, "raw_payload": raw_payload, "captured_at": result.observed_at}
        product = await repo.upsert(ProductIn.model_validate(create_values), commit=commit)
    product.sync_status = ("demo_mock" if result.source_type == DataSourceType.DEMO_MOCK else
                           "offline_fixture" if result.confidence == "synthetic" else
                           "merchant_imported" if merchant_csv else "synced")
    product.last_sync_at = datetime.now(UTC).replace(tzinfo=None)
    if result.source_updated_at is not None:
        source_time = result.source_updated_at.astimezone(UTC).replace(tzinfo=None) if result.source_updated_at.tzinfo else result.source_updated_at
        product.source_updated_at = max(product.source_updated_at, source_time) if product.source_updated_at else source_time
    if commit:
        await repo.session.commit()
    else:
        await repo.session.flush()
    await repo.session.refresh(product)
    return product


async def ingest_seller_stock_observation(
    repo: ProductRepository, store_id: str, item: SellerStockItem, *, observed_at: datetime,
) -> Product:
    """Store an offline, warehouse-granular observation on an existing product.

    It does not change price, sales, costs, recommendation or compliance facts.
    The normal tenant-scoped repository patch creates the auditable snapshot.
    """
    if observed_at.tzinfo is None:
        raise ValueError("Stock observation time must carry a timezone")
    identity = ExternalProductIdentity(DataSourceType.OZON_SELLER, item.product_id,
                                       store_id=store_id, ozon_product_id=item.product_id)
    product = await repo.get_by_external_id(identity.repository_key())
    if product is None or product.store_id != store_id:
        raise ValueError("Seller stock target does not belong to this store and tenant")
    known = ((product.raw_payload or {}).get("_external_identities") or {}).get("OZON_SELLER") or {}
    if item.offer_id and known.get("offer_id") and item.offer_id != known["offer_id"]:
        raise ValueError("Seller stock offer ID conflicts with product identity")
    wire_stocks = [
        {"sku": int(unit.sku) if unit.sku is not None else None,
         "type": unit.fulfillment_type, "shipment_type": unit.shipment_type,
         "warehouse_ids": list(unit.warehouse_ids) if unit.warehouse_ids is not None else None,
         "present": unit.present, "reserved": unit.reserved}
        for unit in item.stocks
    ]
    # Revalidate even if a caller constructed SellerStockItem directly rather
    # than using the strict wire decoder. No partial or malformed record writes.
    decode_product_stocks({"items": [{"product_id": int(item.product_id),
                                      "offer_id": item.offer_id, "stocks": wire_stocks}], "cursor": ""})
    inventory = {
        "source_type": DataSourceType.OZON_SELLER.value,
        "provider": "mock_ozon_seller_contract", "is_mock": True,
        "observed_at": _timestamp(observed_at), "stock_units": wire_stocks,
        "quantity_note": "present and reserved are distinct source fields; availability is not inferred",
    }
    raw = dict(product.raw_payload or {})
    if raw.get("_seller_inventory") == inventory:
        return product
    raw["_seller_inventory"] = inventory
    return await repo.patch(product, ProductPatch.model_validate({"raw_payload": raw}),
                            captured_at=observed_at)
