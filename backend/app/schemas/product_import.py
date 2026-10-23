"""Public, row-specific contract for a merchant CSV preview and confirmation."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ImportIssue(BaseModel):
    code: str
    field: str | None = None
    message: str


class ImportPreviewRow(BaseModel):
    row_number: int
    merchant_product_id: str | None = None
    name: str | None = None
    action: Literal["create", "update", "unchanged", "reject"]
    profit_status: Literal["READY", "INSUFFICIENT_DATA"]
    missing_profit_fields: list[str] = Field(default_factory=list)
    issues: list[ImportIssue] = Field(default_factory=list)


class ImportPreviewResult(BaseModel):
    preview_id: str
    file_sha256: str
    expires_at: datetime
    total_rows: int
    create_count: int
    update_count: int
    unchanged_count: int
    reject_count: int
    field_mapping: dict[str, str]
    rows: list[ImportPreviewRow]


class ImportConfirmedRow(BaseModel):
    row_number: int = Field(ge=2)
    action: Literal["create", "update", "unchanged", "reject"]
    product_id: str | None = None


class ImportConfirmResult(BaseModel):
    preview_id: str
    created: int
    updated: int
    unchanged: int
    rejected: int
    product_ids: list[str]
    row_outcomes: list[ImportConfirmedRow] = Field(default_factory=list)
    replayed: bool = False
