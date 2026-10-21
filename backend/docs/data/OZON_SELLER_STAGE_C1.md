# Ozon Seller Stage C1 — offline contract boundary

This records the initial C1 investigation. For the later user-supplied
four-operation extract and offline decoders, see `OZON_SELLER_C1_FINAL.md`.

Checked 2026-10-21. This is **not** a live Seller integration and contains no
merchant credentials or real merchant data.

## Official sources checked

- [Ozon Seller API documentation](https://docs.ozon.ru/api/seller/) and its
  `swagger.json`: public entry point found, but the interactive document was
  blocked by Ozon's anti-bot challenge in this environment. Current wire
  request/response schemas could **not** be inspected.
- [Ozon's read-only role/method mapping](https://dev.ozon.ru/start/300-Mapping-rolei-i-metodov-Seller-API):
  confirms read-only product-list, product-info, price and stock method
  families, including `/v2/product/list`, `/v2/product/info/list`,
  `/v4/product/info/prices`, and `/v3/product/info/stocks` in that published
  version. This role list is dated 2024 and explicitly cautions that methods
  can become obsolete; it is **not** confirmation of current request schemas.
- [Ozon rate-limit announcement](https://dev.ozon.ru/news/584-Vnedriaem-novye-limity-v-Seller-API-s-19-maia/):
  describes a 50 RPS per-client-ID cabinet-wide limit. Per-method limits and
  current response headers remain unverified.
- [Ozon API-key error announcement](https://dev.ozon.ru/news/505-Izmenenie-otdavaemykh-oshibok-sviazannykh-c-API-kliuchami-Seller-API/):
  documents that an invalid key can produce 404 and deactivated, wrong-role or
  IP-restricted keys can produce 403. The offline client never exposes upstream
  error bodies because they could contain sensitive details.

## Implemented and verified offline

`OzonSellerOfflineClient` accepts only an injected `httpx.MockTransport`. It
checks tenant-owned active Store and matching server-side credential-provider
binding before dispatch, and allowlists the published read-only endpoint
families. It has connection timeout, bounded retries, 429/5xx handling and a
clamped numeric `Retry-After`; no credentials or raw error bodies are logged.

`collect_cursor_pages` provides bounded cursor/de-duplication orchestration
independent of an unverified Ozon response schema. `SellerProductObservation`
is an **internal normalized fixture contract**, not a claimed wire schema.
Its adapter preserves the store/Ozon-product/offer/SKU namespaces, timestamps,
field status and source type. Unknown currency, absent price and unverified
values do not become product price facts. Existing Product Ingestion rejects
cross-tenant Store and protects internal costs and human compliance fields.

## Not verified; required before any live connection

- Which current product-list/info/price/stock endpoint versions supersede the
  published role table, and their exact request and response JSON fields.
- Seller list pagination cursor location, page-size limit and termination
  semantics. The offline cursor helper does **not** parse Seller responses.
- Product status, price currency, SKU, stock warehouse scope, update timestamp
  and null/zero semantics for current responses. No raw Seller-response mapper
  or inventory write to the Product model is enabled.
- Whether `Retry-After` is emitted as seconds, HTTP-date or not at all on each
  endpoint; per-method limits and rate-limit response shape.
- Account-specific permissions and live behavior. No real account or Seller API
  endpoint was called. An end-to-end catalog sync must remain disabled.

Stage C2 should begin by obtaining an accessible official current OpenAPI
contract and implementing typed endpoint decoders against it, then add a
server-bound credential provider and a rate limiter shared across workers.
