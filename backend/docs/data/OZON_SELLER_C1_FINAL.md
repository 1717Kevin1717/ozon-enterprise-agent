# Ozon Seller Stage C1 Final — offline extract contract

Checked **2026-10-21**. The user supplied
`ozon_seller_4_endpoints_contract_extract.json` outside this repository.
It is a 94,665-byte OpenAPI 3.0.0 / info version 2.1 extract with SHA-256
`259289b24a64e2a17636ad306927b03b5355806caf1bfdfbe0e36ba8f05fba5a`.
Its embedded provenance says `USER_PROVIDED_CLAIMED_OFFICIAL_UNVERIFIED_CURRENT`
and identifies a 4,322,954-byte parent file by SHA-256
`709cb90d8e09c95b140695ce99f62fde0f7f450023b8e56b1a31f94c6f555a3d`.
The parent file and current Ozon online API were **not independently verified**.
All 58 internal `$ref` targets resolve within this extract. This document and
the implementation describe an **offline contract match**, not live data.

| Read operation | Extract path | Request | 200 response | Pagination |
| --- | --- | --- | --- | --- |
| Product list | `POST /v3/product/list` | `filter`, `last_id`, `limit` (description: 1–1000) | `result.items[]`, `result.last_id`; item `product_id` (integer), `offer_id` (string), `sku` (integer) | Request `last_id` / response `result.last_id` |
| Product info | `POST /v3/product/info/list` | `offer_id[]`, `product_id[]`, or `sku[]` (strings); operation description limits a request to 1000 products | `items[]`; `id` (integer), `offer_id`, `sku`, `name`, `updated_at`, other fields | No cursor declared; batch by ID |
| Product prices | `POST /v5/product/info/prices` | required `filter`, `limit` (1–1000), optional `cursor` | `items[]`, `cursor`; item `product_id`, `offer_id`, nested `price.price` (number), `price.currency_code` | Top-level `cursor` |
| Product stocks | `POST /v4/product/info/stocks` | required `filter`, `limit` (description: 1–1000), optional `cursor` | `items[]`, `cursor`; stock granules carry `sku`, `type`, `shipment_type`, `warehouse_ids[]`, `present`, `reserved` | Top-level `cursor` |

All four operations declare required `Client-Id` and `Api-Key` headers and an
application/json request body. The extract does not mark them deprecated. Its
per-field `required` declarations are sparse; decoders additionally require
the IDs, arrays and cursors needed for safe local processing. Missing/null
numeric facts stay unknown; zero remains distinct; malformed numeric strings
are rejected. The price endpoint supplies no price-observation timestamp, so
product-info `updated_at` is not used as a price timestamp.

The list/info/price operations declare `200, 400, 403, 404, 409, 500` response
schemas; stocks declares `200` and `default`. This extract does not establish
current 429 response shape, `Retry-After` format, per-method limits or online
cursor termination semantics. Offline pagination uses an empty cursor as a
conservative terminal signal, rejects repeats/conflicting duplicates, and has
a page bound. A page failure yields no partial collected result.

`ozon_seller_contract.py` implements typed request builders and separate
decoders. `ozon_seller_offline.py` can call them only through injected
`httpx.MockTransport`; no live client is exposed. Price and detail can be
joined only on matching Ozon product IDs (and nonconflicting offer IDs), then
passed through the existing `AdapterResult` / Product Ingestion boundary.
The resulting offline sample is labelled synthetic/mock in field provenance
and `sync_status=offline_fixture`. Internal cost, human compliance, sales and
competition are not inferred. Stock granules can be saved on an existing
tenant/store-owned product under namespaced `_seller_inventory` in its JSON
payload and auditable snapshot. This preserves SKU, fulfillment type,
warehouse IDs, `present`, `reserved`, null versus zero, mock provenance and
observation time. It does **not** collapse them into a single inventory number
or populate a dedicated inventory table, which the current model lacks.

## Before real integration

- Obtain and hash a current official contract by an authorized ordinary
  channel; compare endpoint versions, request/response fields, descriptions,
  pagination and deprecation notices with this extract.
- Resolve actual cursor termination, per-method rate limits and error headers.
- Install a real server-verified identity provider, tenant/store-bound secret
  storage and a cross-worker rate limiter. Production currently fails closed.
- Decide whether durable warehouse-granular inventory needs a separate table
  before any live sync; current namespaced JSON is an offline compatibility
  boundary, not a full inventory ledger. Stock is not sales or market demand.
- Only then consider a separate, approved live Seller API test. No seller
  account, key or live request was used in Stage C1 Final.
