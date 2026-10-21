"""Mock-transport-only Seller API boundary.

The current official Swagger is not machine-readable from this environment.
Endpoint payload and response schemas therefore remain UNVERIFIED; this module
does not expose a live-network constructor or silently ingest response data.
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Awaitable, Callable, Mapping, Protocol

import httpx
from sqlalchemy import select

from app.connectors.base import AdapterResult, DataSourceType, ExternalProductIdentity, FieldStatus
from app.db.models import Store
from app.repositories.products import ProductRepository


class ReadOnlyEndpoint(StrEnum):
    # Ozon's published read-only role mapping names these versions. Their
    # current request/response schemas must be rechecked before live use.
    PRODUCT_LIST = "/v2/product/list"
    PRODUCT_INFO = "/v2/product/info/list"
    PRODUCT_PRICES = "/v4/product/info/prices"
    PRODUCT_STOCKS = "/v3/product/info/stocks"


@dataclass(frozen=True, repr=False)
class SellerCredentials:
    company_id: str
    store_id: str
    client_id: str
    api_key: str


class SellerCredentialProvider(Protocol):
    async def for_store(self, company_id: str, store_id: str) -> SellerCredentials | None: ...


@dataclass(frozen=True)
class SellerClientError(Exception):
    code: str
    http_status: int | None = None
    attempts: int = 0

    def __str__(self) -> str:
        # Never include response bodies, request headers, URLs or credentials.
        return f"Seller API {self.code} (status={self.http_status}, attempts={self.attempts})"


def _retry_after(value: str | None, *, ceiling: float) -> float:
    if value is None:
        return 0.0
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return 0.0  # HTTP-date support is unverified for this API.
    return max(0.0, min(seconds, ceiling))


class OzonSellerOfflineClient:
    """Read-only endpoint allowlist with an injected MockTransport only."""

    def __init__(
        self, repo: ProductRepository, store_id: str, credentials: SellerCredentialProvider,
        *, transport: httpx.MockTransport, max_attempts: int = 3,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        if not isinstance(transport, httpx.MockTransport):
            raise ValueError("Stage C1 requires an offline MockTransport")
        if not 1 <= max_attempts <= 3:
            raise ValueError("Retry budget must be between 1 and 3")
        self.repo = repo
        self.store_id = store_id
        self.credentials = credentials
        self.transport = transport
        self.max_attempts = max_attempts
        self.sleep = sleep

    async def post(self, endpoint: ReadOnlyEndpoint, payload: Mapping[str, object]) -> dict:
        if not isinstance(endpoint, ReadOnlyEndpoint):
            raise ValueError("Only allowlisted read-only Seller endpoints are accepted")
        store = await self.repo.session.scalar(select(Store).where(
            Store.id == self.store_id, Store.company_id == self.repo.company_id, Store.status == "active",
        ))
        if store is None:
            raise SellerClientError("STORE_FORBIDDEN")
        credential = await self.credentials.for_store(self.repo.company_id, self.store_id)
        if credential is None or credential.company_id != self.repo.company_id or credential.store_id != self.store_id:
            raise SellerClientError("CREDENTIAL_UNAVAILABLE")
        if not credential.client_id or not credential.api_key:
            raise SellerClientError("CREDENTIAL_UNAVAILABLE")
        headers = {"Client-Id": credential.client_id, "Api-Key": credential.api_key,
                   "Content-Type": "application/json"}
        async with httpx.AsyncClient(base_url="https://api-seller.ozon.ru", transport=self.transport,
                                     timeout=httpx.Timeout(10.0, connect=3.0)) as client:
            for attempt in range(1, self.max_attempts + 1):
                try:
                    response = await client.post(endpoint.value, json=dict(payload), headers=headers)
                except (httpx.ConnectError, httpx.TimeoutException):
                    if attempt == self.max_attempts:
                        raise SellerClientError("TRANSPORT_FAILURE", attempts=attempt) from None
                    await self.sleep(min(float(attempt), 3.0))
                    continue
                if response.status_code in {401, 403, 404}:
                    code = {401: "AUTHENTICATION_FAILED", 403: "PERMISSION_DENIED",
                            404: "API_OR_CREDENTIAL_NOT_FOUND"}[response.status_code]
                    raise SellerClientError(code, response.status_code, attempt)
                if response.status_code == 429 or 500 <= response.status_code <= 599:
                    if attempt == self.max_attempts:
                        raise SellerClientError("RATE_LIMITED" if response.status_code == 429 else "UPSTREAM_FAILURE",
                                                response.status_code, attempt)
                    delay = _retry_after(response.headers.get("Retry-After"), ceiling=30.0)
                    await self.sleep(delay if delay else min(float(attempt), 3.0))
                    continue
                if response.status_code != 200:
                    raise SellerClientError("UPSTREAM_RESPONSE", response.status_code, attempt)
                try:
                    body = response.json()
                except ValueError:
                    raise SellerClientError("INVALID_RESPONSE", response.status_code, attempt) from None
                if not isinstance(body, dict):
                    raise SellerClientError("INVALID_RESPONSE", response.status_code, attempt)
                return body
        raise AssertionError("bounded retry loop exhausted unexpectedly")


@dataclass(frozen=True)
class SellerProductObservation:
    """Internal normalized observation, not a claimed official wire schema."""

    ozon_product_id: str
    offer_id: str | None
    sku: str | None
    title: str | None
    price: float | None
    currency: str | None
    observed_at: datetime
    source_updated_at: datetime | None = None
    price_status: FieldStatus = FieldStatus.UNVERIFIED
    title_status: FieldStatus = FieldStatus.UNVERIFIED


def adapt_seller_observation(store_id: str, observation: SellerProductObservation) -> AdapterResult:
    """Preserve source/identity/unknowns; never invent costs, sales or competition."""
    if not store_id or not observation.ozon_product_id:
        raise ValueError("Store and Ozon product ID are required")
    if observation.observed_at.tzinfo is None:
        raise ValueError("Observation timestamp must carry a timezone")
    if observation.price_status == FieldStatus.KNOWN_ZERO and observation.price != 0:
        raise ValueError("Known-zero price requires zero")
    if observation.price_status == FieldStatus.PRESENT and (observation.price is None or observation.price < 0):
        raise ValueError("Present price must be nonnegative")
    values = {"title": observation.title, "current_price": observation.price}
    statuses = {"title": observation.title_status, "current_price": observation.price_status}
    if observation.sku:
        values["sku"] = observation.sku
        statuses["sku"] = FieldStatus.PRESENT
    if observation.currency != "RUB":
        # Existing ProductIn stores RUB price; no implicit currency conversion.
        statuses["current_price"] = FieldStatus.UNVERIFIED
    return AdapterResult(
        data=values, provider="ozon_seller_offline", source_reference=f"store:{store_id}:product:{observation.ozon_product_id}",
        observed_at=observation.observed_at.astimezone(UTC), source_updated_at=observation.source_updated_at,
        source_type=DataSourceType.OZON_SELLER,
        identity=ExternalProductIdentity(DataSourceType.OZON_SELLER, observation.ozon_product_id,
            store_id=store_id, ozon_product_id=observation.ozon_product_id,
            offer_id=observation.offer_id, sku=observation.sku),
        field_status=statuses,
        warnings=["Wire schema and field trust remain unverified; offline fixture only."],
    )


async def collect_cursor_pages(
    fetch_page: Callable[[str, int], Awaitable[tuple[list[SellerProductObservation], str]]],
    *, limit: int = 100, max_pages: int = 1000,
) -> list[SellerProductObservation]:
    """Offline cursor orchestration; endpoint-specific cursor schema is unverified."""
    if not 1 <= limit <= 1000 or not 1 <= max_pages <= 1000:
        raise ValueError("Invalid pagination bounds")
    cursor = ""
    visited = {cursor}
    found: dict[str, SellerProductObservation] = {}
    for _ in range(max_pages):
        items, next_cursor = await fetch_page(cursor, limit)
        for item in items:
            if not item.ozon_product_id:
                raise ValueError("Product page contains an empty Ozon product ID")
            found.setdefault(item.ozon_product_id, item)
        if not next_cursor:
            return list(found.values())
        if next_cursor in visited:
            raise SellerClientError("PAGINATION_CYCLE")
        visited.add(next_cursor)
        cursor = next_cursor
    raise SellerClientError("PAGINATION_LIMIT")
