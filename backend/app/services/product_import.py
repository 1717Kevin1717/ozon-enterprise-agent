"""Tenant-bound, two-step merchant CSV import with an atomic confirmation."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import DataSourceType, ExternalProductIdentity
from app.connectors.merchant_csv import MONEY_COLUMNS, ParsedMerchantCsv, merchant_adapter, parse_merchant_csv
from app.db.models import MerchantImportPreview
from app.repositories.products import ProductRepository
from app.schemas.product_import import ImportConfirmResult, ImportIssue, ImportPreviewResult, ImportPreviewRow
from app.services.product_ingestion import ingest_product_result


async def latest_confirmed_batch(session: AsyncSession, company_id: str, user_id: str,
                                 *, previous: bool = False, batch_id: str | None = None) -> tuple[str, list[str]] | None:
    """Resolve a server-owned import batch; request-supplied product IDs are never trusted."""
    records = (await session.scalars(
        select(MerchantImportPreview).where(
            MerchantImportPreview.company_id == company_id,
            MerchantImportPreview.user_id == user_id,
            MerchantImportPreview.confirmed_at.is_not(None),
        ).where(*([MerchantImportPreview.id == batch_id] if batch_id else [])
        ).order_by(MerchantImportPreview.confirmed_at.desc(), MerchantImportPreview.id.desc()).limit(2)
    )).all()
    index = 1 if previous else 0
    if len(records) <= index:
        return None
    record = records[index]
    return record.id, list(dict.fromkeys((record.result_json or {}).get("product_ids") or []))


class ImportConflict(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _key(merchant_product_id: str) -> str:
    return ExternalProductIdentity(DataSourceType.MERCHANT_INTERNAL, merchant_product_id).repository_key()


async def _row_status(repo: ProductRepository, parsed: ParsedMerchantCsv):
    rows: list[ImportPreviewRow] = []
    versions: dict[str, dict | None] = {}
    for row in parsed.rows:
        existing = await repo.get_by_external_id(_key(row.merchant_product_id)) if row.merchant_product_id else None
        if row.merchant_product_id:
            versions[row.merchant_product_id] = (
                {"id": existing.id, "updated_at": existing.updated_at.isoformat()} if existing else None
            )
        issues = list(row.issues)
        if existing and (existing.raw_payload or {}).get("_merchant_import", {}).get("source_type") != "MERCHANT_INTERNAL":
            issues.append(ImportIssue(code="IDENTITY_CONFLICT", message="Identifier belongs to a different source"))
        if existing and row.source_updated_at and existing.source_updated_at:
            old = existing.source_updated_at.replace(tzinfo=UTC) if existing.source_updated_at.tzinfo is None else existing.source_updated_at
            if row.source_updated_at < old:
                issues.append(ImportIssue(code="OLDER_SOURCE_UPDATE", message="Source timestamp is older than stored data"))
        known_statuses = dict((existing.raw_payload or {}).get("_merchant_field_status") or {}) if existing else {}
        known_statuses.update({field: status for field, status in row.statuses.items() if status in {"PRESENT", "KNOWN_ZERO"}})
        missing = [field for field in ("current_price", "procurement_cost", "shipping_cost",
                   "platform_commission_rate", "platform_fee", "advertising_cost", "warehousing_cost",
                   "tax_cost", "return_loss_reserve", "other_cost")
                   if known_statuses.get(field) not in {"PRESENT", "KNOWN_ZERO"}]
        same_file = existing and (existing.raw_payload or {}).get("_merchant_import", {}).get("last_file_sha256") == parsed.sha256
        same_values = existing and all(getattr(existing, field) == value for field, value in row.values.items())
        if existing and existing.source_updated_at and row.source_updated_at is None and not (same_file and same_values):
            issues.append(ImportIssue(code="SOURCE_TIME_REQUIRED_FOR_UPDATE",
                                      message="Existing dated source requires a timestamp on an update"))
        action = "reject" if issues else "unchanged" if same_file and same_values else "update" if existing else "create"
        rows.append(ImportPreviewRow(row_number=row.row_number, merchant_product_id=row.merchant_product_id,
                                     name=row.name, action=action, profit_status="INSUFFICIENT_DATA" if missing else "READY",
                                     missing_profit_fields=missing, issues=issues))
    return rows, versions


async def preview_import(session: AsyncSession, company_id: str, user_id: str, content: bytes) -> ImportPreviewResult:
    parsed = parse_merchant_csv(content)
    rows, versions = await _row_status(ProductRepository(session, company_id), parsed)
    expires = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=15)
    record = MerchantImportPreview(company_id=company_id, user_id=user_id, file_sha256=parsed.sha256,
                                   versions_json=versions, expires_at=expires)
    session.add(record)
    await session.commit()
    return ImportPreviewResult(preview_id=record.id, file_sha256=parsed.sha256, expires_at=expires,
                               total_rows=len(rows), create_count=sum(r.action == "create" for r in rows),
                               update_count=sum(r.action == "update" for r in rows),
                               unchanged_count=sum(r.action == "unchanged" for r in rows),
                               reject_count=sum(r.action == "reject" for r in rows),
                               field_mapping={"merchant_product_id": "merchant_internal external identity",
                                              "name": "title", "expected_price_rub": "current_price (merchant estimate)",
                                              **MONEY_COLUMNS}, rows=rows)


async def confirm_import(session: AsyncSession, company_id: str, user_id: str,
                         preview_id: str, content: bytes) -> ImportConfirmResult:
    parsed = parse_merchant_csv(content)
    # SQLite ignores SELECT .. FOR UPDATE. Acquire its write reservation before
    # reading the preview so concurrent confirmations observe the committed result.
    # Other databases retain their normal row-locking transaction semantics.
    if session.get_bind().dialect.name == "sqlite" and not session.in_transaction():
        connection = await session.connection()
        await connection.exec_driver_sql("BEGIN IMMEDIATE")
    record = await session.scalar(select(MerchantImportPreview).where(
        MerchantImportPreview.id == preview_id, MerchantImportPreview.company_id == company_id,
        MerchantImportPreview.user_id == user_id).with_for_update())
    if record is None:
        raise ImportConflict("PREVIEW_NOT_FOUND")
    if parsed.sha256 != record.file_sha256:
        raise ImportConflict("FILE_DIGEST_MISMATCH")
    if record.confirmed_at is not None:
        return ImportConfirmResult.model_validate({**record.result_json, "replayed": True})
    if record.expires_at < datetime.now(UTC).replace(tzinfo=None):
        raise ImportConflict("PREVIEW_EXPIRED")
    repo = ProductRepository(session, company_id)
    rows, versions = await _row_status(repo, parsed)
    if versions != record.versions_json:
        raise ImportConflict("PREVIEW_STALE")
    created = updated = unchanged = rejected = 0
    product_ids: list[str] = []
    row_outcomes: list[dict] = []
    try:
        for row, preview in zip(parsed.rows, rows, strict=True):
            if preview.action == "reject":
                rejected += 1
                row_outcomes.append({"row_number": row.row_number, "action": "reject", "product_id": None})
                continue
            if preview.action == "unchanged":
                unchanged += 1
                product_id = versions[row.merchant_product_id]["id"]
                product_ids.append(product_id)
                row_outcomes.append({"row_number": row.row_number, "action": "unchanged", "product_id": product_id})
                continue
            existing = await repo.get_by_external_id(_key(row.merchant_product_id))
            previous_updated_at = existing.updated_at if existing else None
            result = await ingest_product_result(repo, merchant_adapter(row, file_sha256=parsed.sha256,
                                                observed_at=datetime.now(UTC)), commit=False)
            product_ids.append(result.id)
            if existing is None:
                created += 1
                action = "create"
            elif result.updated_at == previous_updated_at:
                unchanged += 1
                action = "unchanged"
            else:
                updated += 1
                action = "update"
            row_outcomes.append({"row_number": row.row_number, "action": action, "product_id": result.id})
        outcome = ImportConfirmResult(preview_id=preview_id, created=created, updated=updated,
                                      unchanged=unchanged, rejected=rejected, product_ids=product_ids,
                                      row_outcomes=row_outcomes)
        record.confirmed_at = datetime.now(UTC).replace(tzinfo=None)
        record.result_json = outcome.model_dump()
        await session.commit()
        return outcome
    except Exception:
        await session.rollback()
        raise
