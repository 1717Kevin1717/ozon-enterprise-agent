from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol


class DataSourceType(StrEnum):
    DEMO_MOCK = "DEMO_MOCK"
    OZON_SELLER = "OZON_SELLER"
    MERCHANT_INTERNAL = "MERCHANT_INTERNAL"
    AUTHORIZED_MARKET_DATA = "AUTHORIZED_MARKET_DATA"


class FieldStatus(StrEnum):
    PRESENT = "PRESENT"
    KNOWN_ZERO = "KNOWN_ZERO"
    MISSING = "MISSING"
    UNKNOWN = "UNKNOWN"
    UNVERIFIED = "UNVERIFIED"


@dataclass(frozen=True)
class ExternalProductIdentity:
    """These identifiers occupy separate namespaces and are never interchangeable."""

    source_type: DataSourceType
    external_product_id: str
    store_id: str | None = None
    ozon_product_id: str | None = None
    offer_id: str | None = None
    sku: str | None = None
    internal_product_id: str | None = None

    def repository_key(self) -> str:
        if self.source_type == DataSourceType.DEMO_MOCK:
            if not self.external_product_id:
                raise ValueError("Mock external identity is required")
            return self.external_product_id
        if self.source_type == DataSourceType.OZON_SELLER:
            if not self.store_id or not self.ozon_product_id:
                raise ValueError("Ozon identity requires a store and Ozon product ID")
            return f"ozon:{self.store_id}:product:{self.ozon_product_id}"
        if not self.external_product_id:
            raise ValueError("External identity is required")
        return f"{self.source_type.value.lower()}:{self.external_product_id}"


@dataclass
class AdapterResult:
    data: dict[str, Any]
    provider: str
    source_reference: str
    observed_at: datetime | None = None
    confidence: str = "unknown"
    field_lineage: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    source_type: DataSourceType | None = None
    identity: ExternalProductIdentity | None = None
    field_status: dict[str, FieldStatus] = field(default_factory=dict)
    source_updated_at: datetime | None = None


class ProductConnector(Protocol):
    async def get_product(self, external_product_id: str) -> AdapterResult: ...


class MarketConnector(Protocol):
    async def get_market_signals(self, query: dict[str, Any]) -> AdapterResult: ...


class SellerConnector(Protocol):
    async def get_seller_metrics(self, external_product_id: str) -> AdapterResult: ...


class ImageConnector(Protocol):
    async def get_image(self, category: str, product_name: str) -> AdapterResult: ...

