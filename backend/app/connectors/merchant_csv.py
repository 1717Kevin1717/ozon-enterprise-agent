"""Bounded, offline merchant CSV adapter. CSV text is never a model instruction."""

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from app.connectors.base import AdapterResult, DataSourceType, ExternalProductIdentity, FieldStatus
from app.schemas.product_import import ImportIssue


MAX_CSV_BYTES = 1_000_000
MAX_CSV_ROWS = 500  # Current Agent portfolio tools read at most 500 products.
MONEY_COLUMNS = {
    "purchase_cost": "procurement_cost",
    "logistics_cost": "shipping_cost",
    "platform_fee_rub": "platform_fee",
    "advertising_cost_rub": "advertising_cost",
    "warehousing_cost_rub": "warehousing_cost",
    "tax_cost_rub": "tax_cost",
    "return_loss_reserve_rub": "return_loss_reserve",
    "other_cost": "other_cost",
}
COST_CURRENCY = {
    "purchase_cost": "purchase_currency",
    "logistics_cost": "logistics_currency",
    "other_cost": "other_cost_currency",
}
REQUIRED_COLUMNS = {"merchant_product_id", "name", "expected_price_rub"}
ALLOWED_COLUMNS = REQUIRED_COLUMNS | set(MONEY_COLUMNS) | set(COST_CURRENCY.values()) | {
    "category", "platform_commission_rate", "source_updated_at", "notes",
}
PROFIT_FIELDS = ("current_price", "procurement_cost", "shipping_cost", "platform_commission_rate",
                 "platform_fee", "advertising_cost", "warehousing_cost", "tax_cost",
                 "return_loss_reserve", "other_cost")
_HTML_TAG = re.compile(r"<\s*/?\s*[a-zA-Z][^>]*>")


class MerchantCsvError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass
class MerchantCsvRow:
    row_number: int
    merchant_product_id: str | None
    name: str | None
    values: dict[str, object] = field(default_factory=dict)
    statuses: dict[str, FieldStatus] = field(default_factory=dict)
    source_updated_at: datetime | None = None
    issues: list[ImportIssue] = field(default_factory=list)


@dataclass(frozen=True)
class ParsedMerchantCsv:
    sha256: str
    rows: tuple[MerchantCsvRow, ...]


def _text(value: str | None, *, field_name: str, maximum: int) -> str:
    text = (value or "").strip()
    if len(text) > maximum or any(ord(ch) < 32 and ch not in "\t\n" for ch in text):
        raise MerchantCsvError("INVALID_TEXT")
    if text.lstrip().startswith(("=", "+", "-", "@")) or _HTML_TAG.search(text):
        raise MerchantCsvError("UNSAFE_TEXT")
    return text


def _amount(value: str | None, *, positive: bool = False) -> Decimal | None:
    if value is None or not value.strip():
        return None
    try:
        number = Decimal(value.strip())
    except InvalidOperation:
        raise MerchantCsvError("INVALID_NUMBER") from None
    if not number.is_finite() or number < 0 or (positive and number == 0) or number > 1_000_000_000:
        raise MerchantCsvError("INVALID_NUMBER")
    if number.as_tuple().exponent < -4:
        raise MerchantCsvError("INVALID_PRECISION")
    return number


def parse_merchant_csv(content: bytes, *, now: datetime | None = None) -> ParsedMerchantCsv:
    if not content or len(content) > MAX_CSV_BYTES:
        raise MerchantCsvError("FILE_SIZE_INVALID")
    try:
        source = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise MerchantCsvError("CSV_UTF8_REQUIRED") from None
    try:
        reader = csv.DictReader(io.StringIO(source, newline=""), strict=True)
        headers = reader.fieldnames or []
        if len(headers) != len(set(headers)) or not REQUIRED_COLUMNS <= set(headers) or set(headers) - ALLOWED_COLUMNS:
            raise MerchantCsvError("CSV_COLUMNS_INVALID")
        rows: list[MerchantCsvRow] = []
        for number, raw in enumerate(reader, start=2):
            if number - 1 > MAX_CSV_ROWS:
                raise MerchantCsvError("CSV_ROW_LIMIT")
            row = MerchantCsvRow(number, None, None)
            if None in raw:
                row.issues.append(ImportIssue(code="EXTRA_COLUMNS", message="Row has more values than headers"))
            for name in headers:
                if raw.get(name) is None:
                    row.issues.append(ImportIssue(code="MISSING_CELL", field=name, message="Column value is missing"))
            try:
                row.merchant_product_id = _text(raw.get("merchant_product_id"), field_name="merchant_product_id", maximum=80)
                row.name = _text(raw.get("name"), field_name="name", maximum=500)
                if not row.merchant_product_id or not row.name:
                    raise MerchantCsvError("IDENTITY_REQUIRED")
                category = _text(raw.get("category"), field_name="category", maximum=1000)
                _text(raw.get("notes"), field_name="notes", maximum=2000)  # never sent to Agent or persisted
                if category:
                    row.values["category_path"] = category
                    row.statuses["category_path"] = FieldStatus.PRESENT
                row.values["title"] = row.name
                row.statuses["title"] = FieldStatus.PRESENT
                price = _amount(raw.get("expected_price_rub"), positive=True)
                if price is None:
                    raise MerchantCsvError("EXPECTED_PRICE_REQUIRED")
                row.values["current_price"] = float(price)
                row.statuses["current_price"] = FieldStatus.PRESENT
                for column, target in MONEY_COLUMNS.items():
                    amount = _amount(raw.get(column))
                    if amount is None:
                        row.statuses[target] = FieldStatus.MISSING
                        continue
                    currency_column = COST_CURRENCY.get(column)
                    if currency_column:
                        currency = (raw.get(currency_column) or "").strip().upper()
                        if currency != "RUB":
                            raise MerchantCsvError("TRUSTED_FX_REQUIRED" if currency in {"CNY", "USD", "EUR"} else "CURRENCY_INVALID")
                    row.values[target] = float(amount)
                    row.statuses[target] = FieldStatus.KNOWN_ZERO if amount == 0 else FieldStatus.PRESENT
                rate = _amount(raw.get("platform_commission_rate"))
                if rate is None:
                    row.statuses["platform_commission_rate"] = FieldStatus.MISSING
                elif rate > 1:
                    raise MerchantCsvError("COMMISSION_RATE_INVALID")
                else:
                    row.values["platform_commission_rate"] = float(rate)
                    row.statuses["platform_commission_rate"] = FieldStatus.KNOWN_ZERO if rate == 0 else FieldStatus.PRESENT
                updated = (raw.get("source_updated_at") or "").strip()
                if updated:
                    try:
                        parsed = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                    except ValueError:
                        raise MerchantCsvError("SOURCE_TIME_INVALID") from None
                    if parsed.tzinfo is None or parsed.astimezone(UTC) > (now or datetime.now(UTC)):
                        raise MerchantCsvError("SOURCE_TIME_INVALID")
                    row.source_updated_at = parsed.astimezone(UTC)
            except MerchantCsvError as exc:
                row.issues.append(ImportIssue(code=exc.code, message=exc.code))
            rows.append(row)
    except csv.Error:
        raise MerchantCsvError("CSV_SYNTAX_INVALID") from None
    if not rows:
        raise MerchantCsvError("CSV_EMPTY")
    duplicates: dict[str, list[MerchantCsvRow]] = {}
    for row in rows:
        if row.merchant_product_id:
            duplicates.setdefault(row.merchant_product_id, []).append(row)
    for group in duplicates.values():
        if len(group) > 1:
            for row in group:
                row.issues.append(ImportIssue(code="DUPLICATE_MERCHANT_ID", field="merchant_product_id",
                                              message="Merchant product ID occurs more than once in this file"))
    return ParsedMerchantCsv(hashlib.sha256(content).hexdigest(), tuple(rows))


def merchant_adapter(row: MerchantCsvRow, *, file_sha256: str, observed_at: datetime) -> AdapterResult:
    if row.issues or not row.merchant_product_id:
        raise ValueError("Invalid CSV row cannot become a product adapter")
    lineage = {name: {"declared_by": "merchant_csv", "value_kind": "estimated" if name == "current_price" else "merchant_reported",
                      "currency": "RUB" if name in PROFIT_FIELDS and name != "platform_commission_rate" else None}
               for name in row.values}
    return AdapterResult(
        data=row.values, provider="merchant_csv_import",
        source_reference=f"csv:{file_sha256}:row:{row.row_number}",
        observed_at=observed_at, source_updated_at=row.source_updated_at,
        confidence="merchant_reported", source_type=DataSourceType.MERCHANT_INTERNAL,
        identity=ExternalProductIdentity(DataSourceType.MERCHANT_INTERNAL, row.merchant_product_id),
        field_status=row.statuses, field_lineage=lineage,
        warnings=["Merchant-estimated inputs; not observed Ozon market facts."],
    )
