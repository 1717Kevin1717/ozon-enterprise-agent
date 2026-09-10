from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field, HttpUrl, field_validator

class ProductIn(BaseModel):
    external_product_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    brand: str = ""
    category_path: str = ""
    sku: str = ""
    url: str = ""
    main_image_url: str = ""
    image_urls: list[str] = Field(default_factory=list)
    seller_name: str = ""
    current_price: float = Field(ge=0)
    regular_price: float = Field(default=0, ge=0)
    competitor_price_min: float = Field(default=0, ge=0)
    competitor_price_avg: float = Field(default=0, ge=0)
    price_position: str = "unknown"
    rating: float = Field(default=0, ge=0, le=5)
    review_count: int = Field(default=0, ge=0)
    latest_30d_sales: float = Field(default=0, ge=0)
    sales_growth_rate: float = 0
    search_volume: float = Field(default=0, ge=0)
    trend_score: float = Field(default=0, ge=0, le=100)
    sales_source: str = "not_provided"
    sales_evidence: str = ""
    competitor_count: int = Field(default=0, ge=0)
    price_competition_score: float = Field(default=0, ge=0, le=100)
    market_saturation: float = Field(default=0, ge=0, le=100)
    competition_evidence: str = ""
    competitor_entries: list[dict] = Field(default_factory=list)
    visible_lowest_competitor_price: float = Field(default=0, ge=0)
    compliance_status: str = "pending"
    compliance_note: str = ""
    certificates: list[str] = Field(default_factory=list)
    manual_risk_level: str = "unknown"
    procurement_cost: float = Field(default=0, ge=0)
    fulfillment_cost: float = Field(default=0, ge=0)
    shipping_cost: float = Field(default=0, ge=0)
    platform_fee: float = Field(default=0, ge=0)
    advertising_cost: float = Field(default=0, ge=0)
    warehousing_cost: float = Field(default=0, ge=0)
    tax_cost: float = Field(default=0, ge=0)
    return_loss_reserve: float = Field(default=0, ge=0)
    other_cost: float = Field(default=0, ge=0)
    platform_commission_rate: float = Field(default=0, ge=0, le=1)
    target_margin_rate: float = Field(default=0.30, ge=0, lt=1)
    lifecycle_status: str = "candidate"
    tags: list[str] = Field(default_factory=list)
    field_lineage: dict = Field(default_factory=dict)
    raw_payload: dict = Field(default_factory=dict)
    captured_at: datetime | None = None

    @field_validator("brand", "category_path", "title")
    @classmethod
    def no_html_control(cls, value: str) -> str:
        return value.strip()

class ProductPatch(BaseModel):
    title: str | None = None
    brand: str | None = None
    category_path: str | None = None
    sku: str | None = None
    url: str | None = None
    main_image_url: str | None = None
    image_urls: list[str] | None = None
    seller_name: str | None = None
    current_price: float | None = Field(default=None, ge=0)
    regular_price: float | None = Field(default=None, ge=0)
    competitor_price_min: float | None = Field(default=None, ge=0)
    competitor_price_avg: float | None = Field(default=None, ge=0)
    price_position: str | None = None
    rating: float | None = Field(default=None, ge=0, le=5)
    review_count: int | None = Field(default=None, ge=0)
    latest_30d_sales: float | None = Field(default=None, ge=0)
    sales_growth_rate: float | None = None
    search_volume: float | None = Field(default=None, ge=0)
    trend_score: float | None = Field(default=None, ge=0, le=100)
    sales_source: str | None = None
    sales_evidence: str | None = None
    procurement_cost: float | None = Field(default=None, ge=0)
    fulfillment_cost: float | None = Field(default=None, ge=0)
    shipping_cost: float | None = Field(default=None, ge=0)
    platform_fee: float | None = Field(default=None, ge=0)
    advertising_cost: float | None = Field(default=None, ge=0)
    warehousing_cost: float | None = Field(default=None, ge=0)
    tax_cost: float | None = Field(default=None, ge=0)
    return_loss_reserve: float | None = Field(default=None, ge=0)
    other_cost: float | None = Field(default=None, ge=0)
    platform_commission_rate: float | None = Field(default=None, ge=0, le=1)
    target_margin_rate: float | None = Field(default=None, ge=0, lt=1)
    competitor_count: int | None = Field(default=None, ge=0)
    price_competition_score: float | None = Field(default=None, ge=0, le=100)
    market_saturation: float | None = Field(default=None, ge=0, le=100)
    competition_evidence: str | None = None
    competitor_entries: list[dict] | None = None
    visible_lowest_competitor_price: float | None = Field(default=None, ge=0)
    compliance_status: str | None = None
    compliance_note: str | None = None
    certificates: list[str] | None = None
    manual_risk_level: str | None = None
    lifecycle_status: str | None = None
    tags: list[str] | None = None
    raw_payload: dict | None = None

class CapturePacket(BaseModel):
    protocol: str = "zmt-ozon-product-master/v2"
    operation: str = "upsert_product_master"
    capturedAt: datetime | None = None
    snapshot: dict
    master: dict = Field(default_factory=dict)
    fieldLineage: dict = Field(default_factory=dict)

class CompareRequest(BaseModel):
    product_ids: list[str] = Field(min_length=2, max_length=10)

class AgentAsk(BaseModel):
    query: str = Field(min_length=2, max_length=2000)
    session_id: str | None = None
    selected_product_ids: list[str] = Field(default_factory=list, max_length=10)
    selection_revision: int = Field(default=0, ge=0)
    selection_bound_session_id: str | None = None
    provider_execution_policy: Literal["AUTO", "QWEN_ONLY", "PRIMARY_ONLY"] = "AUTO"


class DecisionCreate(BaseModel):
    action: Literal["approve", "reject", "needs_more_data"]
    reason: str = Field(min_length=2, max_length=1000)


class DemoSeedRequest(BaseModel):
    replace_demo: bool = False
