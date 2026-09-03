from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol


@dataclass
class AdapterResult:
    data: dict[str, Any]
    provider: str
    source_reference: str
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    confidence: str = "unknown"
    field_lineage: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class ProductConnector(Protocol):
    async def get_product(self, external_product_id: str) -> AdapterResult: ...


class MarketConnector(Protocol):
    async def get_market_signals(self, query: dict[str, Any]) -> AdapterResult: ...


class SellerConnector(Protocol):
    async def get_seller_metrics(self, external_product_id: str) -> AdapterResult: ...


class ImageConnector(Protocol):
    async def get_image(self, category: str, product_name: str) -> AdapterResult: ...

