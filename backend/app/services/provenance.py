"""Normalize legacy lineage and build evidence without DB/network/LLM side effects."""
import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from types import SimpleNamespace

from app.schemas.provenance import FieldEvidence, FreshnessResult, ProductDataTrust, TrustNotice


# Portfolio policy v1, centralized and deliberately simple (not platform guarantees).
FRESHNESS_MAX_AGE_DAYS = {
    "current_price": 7, "regular_price": 7, "sales_snapshot": 30,
    "latest_30d_sales": 30, "sales_growth_rate": 30, "review_count": 30, "rating": 30,
    "competitor_count": 30, "competitor_price_min": 7, "competitor_price_avg": 7,
    "price_competition_score": 30, "market_saturation": 30, "search_volume": 30, "trend_score": 30,
    "compliance_status": 90, "compliance_note": 90, "certificates": 90, "manual_risk_level": 30,
    "procurement_cost": 30, "shipping_cost": 30, "fulfillment_cost": 30, "platform_fee": 30,
    "advertising_cost": 30, "warehousing_cost": 30, "tax_cost": 30, "return_loss_reserve": 30,
    "other_cost": 30, "platform_commission_rate": 30, "target_margin_rate": 30,
}
RAW_FIELDS = tuple(sorted((FRESHNESS_MAX_AGE_DAYS.keys() - {"sales_snapshot"}) | {"title", "brand", "category_path", "main_image_url", "url", "sales_source", "sales_evidence", "competition_evidence"}))
ALIASES = {"current_price": "currentPriceRub", "regular_price": "regularPriceRub", "latest_30d_sales": "latest30dSales", "review_count": "reviewCount", "procurement_cost": "procurementCostRub", "fulfillment_cost": "fulfillmentCostRub", "platform_commission_rate": "platformCommissionRate", "compliance_status": "complianceStatus"}
SOURCE_TYPES = {"mock", "manual", "imported", "marketplace", "enterprise_internal", "unknown"}
LEGACY_SOURCES = {
    "manual_enterprise_form": ("manual", "manual_operator"),
    "enterprise_workbench": ("manual", "manual_operator"),
    "manual": ("manual", "manual_operator"), "manual_operator": ("manual", "manual_operator"),
    "csv_import": ("imported", "csv_import"), "excel_import": ("imported", "excel_import"),
    "visible_page": ("marketplace", "ozon"), "ozon_visible_page": ("marketplace", "ozon"),
    "seller_report": ("enterprise_internal", "seller_report"),
}
COST_FIELDS = ["procurement_cost", "shipping_cost", "fulfillment_cost", "platform_fee", "advertising_cost", "warehousing_cost", "tax_cost", "return_loss_reserve", "other_cost", "platform_commission_rate"]
DIMENSION_FIELDS = {
    "price": ["current_price"], "profit": ["net_profit", "net_margin"], "roi": ["roi"],
    "risk": ["risk_level"], "compliance": ["compliance_status", "compliance_note", "certificates"],
    "competition": ["competitor_count", "competitor_price_min", "competitor_price_avg", "market_saturation"],
    "demand": ["latest_30d_sales", "sales_growth_rate", "rating", "review_count", "search_volume", "trend_score"],
    "sales_snapshot": ["latest_30d_sales", "sales_growth_rate"], "data_sufficiency": ["latest_30d_sales", "sales_growth_rate"],
}


def utc(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def freshness(field: str, timestamp: Any, *, now: datetime | None = None) -> FreshnessResult:
    current, captured = utc(now) or datetime.now(UTC), utc(timestamp)
    maximum = FRESHNESS_MAX_AGE_DAYS.get(field)
    age = (current - captured).total_seconds() / 86400 if captured else None
    status = "UNKNOWN" if age is None or age < 0 or maximum is None else "STALE" if age > maximum else "FRESH"
    reason = "missing_or_future_observation_time" if age is None or age < 0 else "no_policy" if maximum is None else "age_exceeds_policy" if status == "STALE" else "within_policy"
    return FreshnessResult(status=status, age_days=round(age, 6) if age is not None else None, max_age_days=maximum, evaluated_at=current, reason=reason)


def _identity(product, field, value, metadata):
    content = json.dumps([product.company_id, product.id, field, value, metadata], sort_keys=True, default=str, ensure_ascii=False)
    return "ev_" + hashlib.sha256(content.encode()).hexdigest()[:24]


def _mock_provider(value: str) -> bool:
    return value.lower().startswith(("mock_", "demo_"))


def raw_evidence(product, field: str, *, now=None) -> FieldEvidence:
    lineage, raw = product.field_lineage or {}, product.raw_payload or {}
    entry = lineage.get(field, lineage.get(ALIASES.get(field, ""), {}))
    entry = {"source": entry} if isinstance(entry, str) else dict(entry) if isinstance(entry, dict) else {}
    provider = str(entry.get("provider") or entry.get("source") or "")
    if not provider and field in {"latest_30d_sales", "sales_growth_rate"}:
        provider = str(product.sales_source or "")
    is_mock = bool(raw.get("demo_data") or raw.get("is_mock") or entry.get("is_mock") or entry.get("source_type") == "mock" or _mock_provider(provider))
    kind = str(entry.get("source_type") or "unknown")
    if kind not in SOURCE_TYPES:
        kind = "unknown"
    if kind == "unknown" and provider in LEGACY_SOURCES:
        kind, provider = LEGACY_SOURCES[provider]
    if is_mock:
        kind = "mock"
        if not _mock_provider(provider):
            provider = "mock_enterprise_catalog"
    provider = provider or "unknown"
    observed = utc(entry.get("observed_at"))
    collected = utc(entry.get("collected_at") or entry.get("captured_at") or entry.get("capturedAt"))
    # Never infer capture time from Product.updated_at or the act of reading/re-analyzing.
    source_url = entry.get("source_url") or entry.get("url")
    source_url = source_url if isinstance(source_url, str) and source_url.startswith(("https://", "http://")) and not is_mock else None
    value = getattr(product, field, None)
    provided = raw.get("_provided_fields", [])
    presence = "MISSING" if value is None or value == "" else "PRESENT" if field in provided or entry or is_mock or value not in (0, [], "pending", "unknown") else "UNKNOWN"
    traceable = kind != "unknown" and provider not in {"unknown", "not_provided", "未提供", ""} and presence == "PRESENT"
    metadata = [kind, provider, observed, collected, source_url]
    return FieldEvidence(field=field, value=value, unit="RUB" if field in COST_FIELDS and field != "platform_commission_rate" or field in {"current_price", "regular_price", "competitor_price_min", "competitor_price_avg"} else "%" if field == "sales_growth_rate" else "",
        product_id=product.id, company_id=product.company_id, source_type=kind, provider=provider, source_url=source_url,
        collected_at=collected, observed_at=observed, updated_at=utc(entry.get("updated_at")), evidence_id=_identity(product, field, value, metadata),
        is_mock=is_mock, presence=presence, evidence_status="TRACEABLE" if traceable else "SOURCE_MISSING", freshness=freshness(field, observed or collected, now=now),
        notice="演示 / Mock 数据，不是实时 Ozon 数据。" if is_mock else "来源未确认。" if not traceable else "来源为录入方声明，未自动验证外部真实性。")


def derived_evidence(product, field, value, inputs, calculation, *, now=None):
    current = utc(now) or datetime.now(UTC)
    statuses = [item.freshness.status for item in inputs]
    status = "STALE" if "STALE" in statuses else "UNKNOWN" if "UNKNOWN" in statuses else "FRESH"
    is_mock = any(item.is_mock for item in inputs)
    return FieldEvidence(field=field, value=value, unit="RUB" if field in {"net_profit", "gross_profit"} else "ratio" if field in {"net_margin", "roi", "gross_margin"} else "",
        product_id=product.id, company_id=product.company_id, source_type="derived", provider="deterministic_evaluation_engine",
        evidence_id=_identity(product, field, value, [item.evidence_id for item in inputs]), is_mock=is_mock, is_derived=True,
        presence="PRESENT", evidence_status="TRACEABLE" if all(item.evidence_status == "TRACEABLE" for item in inputs) else "SOURCE_MISSING",
        freshness=FreshnessResult(status=status, evaluated_at=current, reason="inherited_from_inputs_not_calculation_time"),
        derived_from=[item.field for item in inputs], inputs=inputs, calculation=calculation,
        notice="基于 Mock 输入的确定性计算，不是真实市场结论。" if is_mock else "计算可复核不代表输入已获外部验证。")


def _refresh(item: FieldEvidence, now=None) -> FieldEvidence:
    if item.is_derived:
        inputs = [_refresh(value, now) for value in item.inputs]
        statuses = [value.freshness.status for value in inputs]
        status = "STALE" if "STALE" in statuses else "UNKNOWN" if "UNKNOWN" in statuses else "FRESH"
        return item.model_copy(update={"inputs": inputs, "freshness": FreshnessResult(status=status, evaluated_at=utc(now) or datetime.now(UTC), reason="inherited_from_inputs_not_calculation_time")})
    return item.model_copy(update={"freshness": freshness(item.field, item.observed_at or item.collected_at, now=now)})


def build_trust(product, analysis: dict | None = None, *, saved_fields: dict | None = None, legacy_analysis=False, now=None) -> ProductDataTrust:
    fields = {name: raw_evidence(product, name, now=now) for name in sorted(RAW_FIELDS)}
    raw_fields = list(fields.values())
    if analysis:
        profit_inputs = [fields["current_price"], *[fields[key] for key in COST_FIELDS]]
        # Shipping is preferred over fulfillment by the existing formula; both inputs are disclosed.
        calculations = {
            "gross_profit": ([fields["current_price"], fields["procurement_cost"]], "price - procurement_cost"),
            "gross_margin": ([fields["current_price"], fields["procurement_cost"]], "(price - procurement_cost) / price; zero price => 0"),
            "net_profit": (profit_inputs, "price - procurement - (shipping or fulfillment) - price*commission_rate - platform_fee - advertising - warehousing - tax - return_reserve - other; missing/unknown numeric inputs are normalized to 0 by evaluation_v2"),
            "net_margin": (profit_inputs, "net_profit / price; zero price => 0; missing/unknown numeric inputs are normalized to 0 by evaluation_v2"),
            "roi": (profit_inputs, "net_profit / total_cost; zero total_cost => 0; missing/unknown numeric inputs are normalized to 0 by evaluation_v2"),
            "risk_level": (raw_fields, "existing evaluation v2 risk score + evidence-readiness and hard gates; lineage presence also contributes to existing readiness; see analysis.score_explanations.risk"),
        }
        for name, (inputs, formula) in calculations.items():
            if name not in analysis:
                continue
            if saved_fields and name in saved_fields:
                saved = FieldEvidence.model_validate(saved_fields[name])
                if (saved.company_id, saved.product_id) != (product.company_id, product.id):
                    raise ValueError("Stored analysis evidence binding mismatch")
                fields[name] = _refresh(saved, now)
            else:
                if legacy_analysis:
                    # Old analyses did not store per-input provenance. Do not backfill a false chain.
                    inputs = [item.model_copy(update={"value": None, "provider": "legacy_analysis_input_unknown", "source_type": "mock" if item.is_mock else "unknown", "collected_at": None, "observed_at": None, "presence": "UNKNOWN", "evidence_status": "SOURCE_MISSING", "freshness": freshness(item.field, None, now=now), "evidence_id": _identity(product, item.field, None, "legacy_input_unknown")}) for item in inputs]
                fields[name] = derived_evidence(product, name, analysis[name], inputs, formula, now=now)
    present = sum(item.presence == "PRESENT" for item in raw_fields)
    mock = any(item.is_mock for item in fields.values())
    notices = evidence_notices(list(fields.values()))
    if legacy_analysis and not saved_fields:
        notices.append(TrustNotice(product_id=product.id, field="analysis", code="LEGACY_ANALYSIS", message="旧分析未保存字段级输入证据；其历史计算输入来源未知，需重新分析以建立新证据链。"))
    return ProductDataTrust(product_id=product.id, company_id=product.company_id, is_mock=mock,
        disclosure="演示 / Mock 数据，不代表真实 Ozon 市场；平台链接仅供参考。" if mock else "来源声明不等于外部真实性认证；缺失来源或采集时间保持未知。",
        completeness={"definition": "field_presence_v1", "present": present, "total": len(raw_fields), "percent": round(100*present/len(raw_fields)), "missing_fields": [item.field for item in raw_fields if item.presence == "MISSING"], "unknown_fields": [item.field for item in raw_fields if item.presence == "UNKNOWN"]}, fields=fields, notices=notices)


def evidence_notices(fields: list[FieldEvidence]) -> list[TrustNotice]:
    notices = []
    for item in fields:
        if item.is_mock:
            notices.append(TrustNotice(product_id=item.product_id, field=item.field, code="MOCK_DATA", message=f"{item.field}：演示 / Mock 数据，不是实时 Ozon 数据。"))
        if item.evidence_status == "SOURCE_MISSING":
            notices.append(TrustNotice(product_id=item.product_id, field=item.field, code="SOURCE_MISSING", message=f"{item.field}：来源或输入证据不足，不能视为已验证事实。"))
        if item.freshness.status != "FRESH":
            code = "STALE_DATA" if item.freshness.status == "STALE" else "FRESHNESS_UNKNOWN"
            message = "已过期，请更新证据；不能作为当前强结论的唯一依据。" if code == "STALE_DATA" else "采集/观察时间或输入时效未知，不能确认数据新鲜度。"
            notices.append(TrustNotice(product_id=item.product_id, field=item.field, code=code, message=f"{item.field}：{message}"))
    return notices


def scoped_fields(trust: dict, dimensions, *, decision=False, criteria=None) -> list[dict]:
    fields = trust.get("fields", {})
    names = list(fields) if decision or "detail" in dimensions else [name for dim in dimensions for name in DIMENSION_FIELDS.get(dim, [])]
    if "filters" in dimensions or criteria:
        names += ["current_price"]
        for key, value in (criteria or {}).items():
            if value is not None and value != "":
                names += {"min_margin_rate": ["net_margin"], "risk_level": ["risk_level"], "compliance_status": ["compliance_status"], "min_score": ["risk_level", "net_margin"]}.get(key, [])
    return [fields[name] for name in dict.fromkeys(names) if name in fields]


def capture_lineage(data, *, changed_fields=None) -> dict:
    """Record only supplied fields; partial edits must not refresh unrelated observations."""
    lineage = dict(getattr(data, "field_lineage", None) or {})
    timestamp = (utc(getattr(data, "captured_at", None)) or datetime.now(UTC)).isoformat()
    raw = data.raw_payload or {}
    entry = lineage.get("entry", {})
    manual = raw.get("source") == "enterprise_workbench" or isinstance(entry, dict) and entry.get("source") == "manual_enterprise_form"
    mock = bool(raw.get("demo_data") or raw.get("is_mock"))
    for name in changed_fields if changed_fields is not None else data.model_fields_set:
        if name not in RAW_FIELDS:
            continue
        old = lineage.get(name, lineage.get(ALIASES.get(name, "")))
        if manual:
            lineage[name] = {"source_type": "mock" if mock else "manual", "provider": "mock_manual_operator" if mock else "manual_operator", "is_mock": mock, "collected_at": timestamp, "observed_at": timestamp, "updated_at": timestamp}
        elif old:
            record = {"source": old} if isinstance(old, str) else dict(old) if isinstance(old, dict) else {}
            # A provided capture time is evidence; an implicit sync timestamp is not.
            if getattr(data, "captured_at", None) and not any(record.get(key) for key in ("collected_at", "captured_at", "capturedAt")):
                record["collected_at"] = timestamp
            lineage[name] = record
        elif mock:
            source = getattr(data, "sales_source", "") if name in {"latest_30d_sales", "sales_growth_rate"} else ""
            lineage[name] = {"source_type": "mock", "provider": source if _mock_provider(source) else "mock_enterprise_catalog", "is_mock": True, "collected_at": timestamp}
        elif changed_fields is not None:
            # A changed value without new evidence must not inherit an old source.
            lineage[name] = {"source_type": "unknown", "provider": "unknown"}
    return lineage


def snapshot_view(row) -> dict:
    raw = getattr(row, "raw_payload", None) or {}
    lineage = raw.get("_field_lineage", {})
    values = {"current_price": row.price, "latest_30d_sales": row.sales_30d, "review_count": row.review_count, "rating": row.rating, "competitor_count": row.competitor_count}
    base = {"captured_at": row.captured_at, "price": row.price, "sales_30d": row.sales_30d, "rating": row.rating, "review_count": row.review_count, "competitor_count": row.competitor_count}
    if not getattr(row, "product_id", None) or not getattr(row, "company_id", None):
        # Legacy numeric snapshot adapters remain usable, but cannot mint bound evidence.
        return {**base, "field_provenance": {}, "is_mock": bool(raw.get("demo_data") or raw.get("is_mock")), "capture_notice": "快照缺少企业/商品身份，不能生成字段证据；数据来源未知。"}
    product = SimpleNamespace(id=row.product_id, company_id=row.company_id, field_lineage=lineage, raw_payload=raw, sales_source=raw.get("salesSource", "not_provided"), **values)
    fields = {key: raw_evidence(product, key).model_dump(mode="json") for key in values}
    return {**base, "id": getattr(row, "id", None), "product_id": row.product_id, "field_provenance": fields, "is_mock": any(item["is_mock"] for item in fields.values()), "capture_notice": "快照保存时间不自动等于原始数据观察时间。"}
