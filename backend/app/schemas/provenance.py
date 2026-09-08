"""Additive, backend-owned evidence contract; unknown is not marketplace data."""
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


SourceType = Literal["mock", "manual", "imported", "marketplace", "enterprise_internal", "derived", "unknown"]


class FreshnessResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["FRESH", "STALE", "UNKNOWN"]
    age_days: float | None = None
    max_age_days: int | None = None
    evaluated_at: datetime
    reason: str


class FieldEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    value: Any
    unit: str = ""
    product_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    source_type: SourceType
    provider: str
    source_url: str | None = None
    collected_at: datetime | None = None
    observed_at: datetime | None = None
    updated_at: datetime | None = None
    evidence_id: str = Field(min_length=1)
    is_mock: bool = False
    is_derived: bool = False
    presence: Literal["PRESENT", "MISSING", "UNKNOWN"] = "UNKNOWN"
    evidence_status: Literal["TRACEABLE", "SOURCE_MISSING"] = "SOURCE_MISSING"
    freshness: FreshnessResult
    derived_from: list[str] = Field(default_factory=list)
    inputs: list["FieldEvidence"] = Field(default_factory=list)
    calculation: str = ""
    notice: str = ""

    @model_validator(mode="after")
    def consistent_chain(self):
        if self.is_derived != (self.source_type == "derived"):
            raise ValueError("Derived flag and source type disagree")
        if self.source_type == "mock" and not self.is_mock:
            raise ValueError("Mock data must disclose is_mock")
        if self.is_mock and self.source_type not in {"mock", "derived"}:
            raise ValueError("Mock cannot be represented as real marketplace/manual data")
        if self.is_derived:
            if not self.inputs or not self.calculation or self.derived_from != [item.field for item in self.inputs]:
                raise ValueError("Derived evidence requires ordered input evidence and calculation")
            if any((item.company_id, item.product_id) != (self.company_id, self.product_id) for item in self.inputs):
                raise ValueError("Input evidence must belong to the same tenant and product")
            if any(item.is_mock for item in self.inputs) and not self.is_mock:
                raise ValueError("Mock input must propagate to derived output")
        elif self.inputs or self.derived_from or self.calculation:
            raise ValueError("Raw facts cannot carry derived inputs")
        return self


class TrustNotice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: str
    field: str
    code: Literal["STALE_DATA", "FRESHNESS_UNKNOWN", "SOURCE_MISSING", "MOCK_DATA", "LEGACY_ANALYSIS"]
    message: str


class ProductDataTrust(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: str
    company_id: str
    is_mock: bool
    disclosure: str
    # Presence only. Existing weighted readiness remains in analysis.evidence_completeness.
    completeness: dict[str, Any]
    fields: dict[str, FieldEvidence]
    notices: list[TrustNotice] = Field(default_factory=list)

    @model_validator(mode="after")
    def bound_fields(self):
        for key, item in self.fields.items():
            if key != item.field or (item.company_id, item.product_id) != (self.company_id, self.product_id):
                raise ValueError("Evidence does not belong to this product")
        return self
