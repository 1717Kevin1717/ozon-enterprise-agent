"""Offline adapter for the existing enterprise demo catalog."""

from app.connectors.base import (
    AdapterResult,
    DataSourceType,
    ExternalProductIdentity,
    FieldStatus,
)
from app.schemas.products import ProductIn
from app.services.demo_dataset import enterprise_demo_candidates


class MockProductConnector:
    def __init__(self, products: list[ProductIn] | None = None):
        self._products = products if products is not None else enterprise_demo_candidates()

    def list_products(self) -> list[AdapterResult]:
        return [self._adapt(product) for product in self._products]

    async def get_product(self, external_product_id: str) -> AdapterResult:
        for product in self._products:
            if product.external_product_id == external_product_id:
                return self._adapt(product)
        raise LookupError("Mock product not found")

    @staticmethod
    def _adapt(product: ProductIn) -> AdapterResult:
        values = product.model_dump(exclude_unset=True, exclude={"captured_at"})
        statuses = {
            name: FieldStatus.KNOWN_ZERO if isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0 else FieldStatus.PRESENT
            for name, value in values.items()
            if name not in {"field_lineage", "raw_payload", "external_product_id"}
        }
        return AdapterResult(
            data=values,
            provider="mock_enterprise_catalog",
            source_reference=product.external_product_id,
            observed_at=None,  # Generating a fixture is not a marketplace observation.
            source_type=DataSourceType.DEMO_MOCK,
            identity=ExternalProductIdentity(
                source_type=DataSourceType.DEMO_MOCK,
                external_product_id=product.external_product_id,
                sku=product.sku or None,
            ),
            confidence="synthetic",
            field_lineage=product.field_lineage,
            field_status=statuses,
            warnings=["Mock fixture: not verified Ozon market data."],
        )
