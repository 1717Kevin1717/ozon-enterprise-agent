"""One source of truth for merchant-import financial input sufficiency.

Legacy/Mock products retain their existing evaluation contract. A CSV-created
candidate must prove each input used by the existing net-profit formula.
"""

from dataclasses import dataclass


REQUIRED_FINANCIAL_FIELDS = (
    "current_price", "procurement_cost", "shipping_cost",
    "platform_commission_rate", "platform_fee", "advertising_cost",
    "warehousing_cost", "tax_cost", "return_loss_reserve", "other_cost",
)


@dataclass(frozen=True)
class FinancialReadiness:
    status: str
    missing_fields: tuple[str, ...] = ()
    source: str = "legacy_policy"

    @property
    def ready(self) -> bool:
        return self.status == "SUFFICIENT"


def financial_readiness(product: object) -> FinancialReadiness:
    raw = getattr(product, "raw_payload", None) or {}
    merchant = raw.get("_merchant_import") if isinstance(raw, dict) else None
    merchant_identity = str(getattr(product, "external_product_id", "") or "").startswith("merchant_internal:")
    merchant_source = isinstance(merchant, dict) and merchant.get("source_type") == "MERCHANT_INTERNAL"
    if not (merchant_identity or merchant_source or getattr(product, "sync_status", None) == "merchant_imported"):
        return FinancialReadiness("SUFFICIENT")
    statuses = raw.get("_merchant_field_status") or {}
    lineage = getattr(product, "field_lineage", None) or {}
    missing = []
    for field in REQUIRED_FINANCIAL_FIELDS:
        entry = lineage.get(field) or {}
        manually_provided = isinstance(entry, dict) and entry.get("source_type") == "manual" and entry.get("provider") == "manual_operator"
        csv_provided = (statuses.get(field) in {"PRESENT", "KNOWN_ZERO"}
                        and isinstance(entry, dict) and entry.get("source_type") == "enterprise_internal"
                        and entry.get("provider") == "merchant_csv_import"
                        and (field == "platform_commission_rate" or entry.get("currency") == "RUB"))
        known = csv_provided or manually_provided
        value = getattr(product, field, None)
        if not known or value is None or isinstance(value, bool) or value < 0:
            missing.append(field)
    # The CSV importer fixes both price and costs in RUB; reject later currency drift.
    if merchant.get("currency") != "RUB" or getattr(product, "currency", None) != "RUB":
        missing.append("trusted_fx_to_rub")
    return FinancialReadiness("SUFFICIENT" if not missing else "INSUFFICIENT_DATA",
                              tuple(missing), "merchant_internal")
