from dataclasses import asdict, dataclass
from math import log1p
from typing import Any

from app.db.models import Product
from app.services.provenance import build_trust

ALGORITHM_VERSION = "v2.0.0-enterprise-evaluation"
WEIGHTS = {"profit": 0.28, "demand": 0.24, "competition": 0.18, "compliance": 0.18, "risk": 0.12}


@dataclass
class AnalysisResult:
    demand_score: float
    profit_score: float
    competition_score: float
    compliance_score: float
    risk_score: float
    total_score: float | None
    base_score: float
    recommendation_score: float
    recommendation_grade: str
    data_confidence: float
    data_completeness: float
    evidence_completeness: dict[str, Any]
    missing_data: list[dict[str, str]]
    missing_fields: list[str]
    risk_level: str
    risks: list[dict[str, Any]]
    evidence: dict[str, Any]
    score_explanations: dict[str, Any]
    recommendation: str
    gross_profit: float
    gross_margin: float
    net_profit: float
    net_margin: float
    roi: float
    current_margin_rate: float
    expected_profit: float
    break_even_price: float
    target_price: float
    price_position: str
    strategy_factor: float
    historical_risk_factor: float


def _number(value: Any) -> float:
    return float(value or 0)


def _clamp(value: float, low: float = 0, high: float = 100) -> float:
    return max(low, min(high, value))


def _log_score(value: float, reference: float) -> float:
    return _clamp(log1p(max(value, 0)) / log1p(reference) * 100 if reference > 0 else 0)


def _price_position(product: Product) -> str:
    price = _number(product.current_price)
    average = _number(product.competitor_price_avg)
    if not price or not average:
        return "unknown"
    ratio = price / average
    if ratio < 0.90:
        return "below_market"
    if ratio > 1.10:
        return "premium"
    return "market_aligned"


def evidence_completeness(product: Product) -> tuple[dict[str, Any], list[dict[str, str]]]:
    lineage = product.field_lineage or {}
    sales_ready = bool(
        _number(product.latest_30d_sales) > 0
        and product.sales_source not in {"", "not_provided", "未提供"}
        and product.sales_evidence
        and _number(product.rating) > 0
        and _number(product.review_count) > 0
        and _number(product.search_volume) > 0
    )
    profit_ready = bool(
        _number(product.current_price) > 0
        and _number(product.procurement_cost) > 0
        and (_number(product.shipping_cost) > 0 or _number(product.fulfillment_cost) > 0)
        and _number(product.platform_commission_rate) > 0
    )
    competition_ready = bool(
        _number(product.competitor_count) > 0
        and product.competition_evidence
        and _number(product.competitor_price_min) > 0
        and _number(product.competitor_price_avg) > 0
        and _number(product.market_saturation) > 0
    )
    checks = [
        ("identity", 10, bool(product.title and product.brand and product.category_path and product.main_image_url and product.url), ["title", "brand", "category_path", "main_image_url", "url"], "补齐商品身份、主图和来源链接。"),
        ("demand", 22, sales_ready, ["monthly_sales", "sales_source", "sales_evidence", "rating", "review_count", "search_volume"], "补充授权销量、搜索量、评价及证据来源。"),
        ("profit", 24, profit_ready, ["sale_price", "cost_price", "shipping_cost", "platform_commission_rate"], "补齐售价、采购、物流和平台佣金。"),
        ("competition", 20, competition_ready, ["competitor_count", "competitor_price_min", "competitor_price_avg", "market_saturation", "competition_evidence"], "建立同口径竞品集合、价格带和市场饱和度。"),
        ("compliance", 14, product.compliance_status in {"approved", "通过"} and bool(product.compliance_note or product.certificates), ["compliance_status", "certificates", "compliance_evidence"], "完成人工合规核验并上传证据或证书清单。"),
        ("lineage", 10, bool(lineage), ["field_lineage"], "记录关键字段来自页面、授权 API 或人工录入。"),
    ]
    groups: dict[str, Any] = {}
    missing: list[dict[str, str]] = []
    passed_weight = 0
    for name, weight, ready, fields, action in checks:
        groups[name] = {"ready": ready, "weight": weight, "required_fields": fields}
        if ready:
            passed_weight += weight
        else:
            missing.append({"dimension": name, "missing": " / ".join(fields), "action": action})
    percent = round(passed_weight)
    grade = "complete" if percent == 100 else "partial" if percent >= 40 else "insufficient"
    return {
        "ratio": round(percent / 100, 2),
        "percent": percent,
        "passed_weight": passed_weight,
        "required_weight": 100,
        "groups": groups,
        "grade": grade,
        "decision_ready": percent == 100,
        "definition": "v2_weighted_required_evidence",
    }, missing


def analyze(product: Product, strategy_factor: float = 1.0, historical_risk_factor: float = 1.0) -> AnalysisResult:
    price = _number(product.current_price)
    purchase = _number(product.procurement_cost)
    logistics = _number(product.shipping_cost) or _number(product.fulfillment_cost)
    advertising = _number(product.advertising_cost)
    commission_rate = _number(product.platform_commission_rate)
    commission_cost = price * commission_rate
    fixed_operating_costs = sum(_number(value) for value in [product.platform_fee, product.warehousing_cost, product.tax_cost, product.return_loss_reserve, product.other_cost])
    total_cost = purchase + logistics + advertising + commission_cost + fixed_operating_costs
    target_margin = _number(product.target_margin_rate or 0.30)
    gross_profit = price - purchase
    gross_margin = gross_profit / price if price else 0
    net_profit = price - total_cost
    net_margin = net_profit / price if price else 0
    roi = net_profit / total_cost if total_cost else 0
    break_even_price = (purchase + logistics + advertising + fixed_operating_costs) / max(0.01, 1 - commission_rate)
    target_price = (purchase + logistics + advertising + fixed_operating_costs) / max(0.01, 1 - commission_rate - target_margin)
    price_position = _price_position(product)
    completeness, missing = evidence_completeness(product)
    missing_fields = sorted({field for item in missing for field in item["missing"].split(" / ")})

    sales_score = _log_score(_number(product.latest_30d_sales), 1000)
    growth_score = _clamp(50 + _number(product.sales_growth_rate) * 2.5)
    rating_score = _clamp(_number(product.rating) / 5 * 100)
    review_score = _log_score(_number(product.review_count), 5000)
    search_score = _log_score(_number(product.search_volume), 100_000)
    trend_score = _clamp(_number(product.trend_score))
    demand = sales_score * 0.30 + growth_score * 0.18 + rating_score * 0.14 + review_score * 0.14 + search_score * 0.14 + trend_score * 0.10

    margin_score = _clamp(net_margin / max(target_margin, 0.01) * 100)
    roi_score = _clamp(roi / 0.80 * 100)
    profit_buffer_score = _clamp(net_profit / max(price * 0.35, 1) * 100) if price else 0
    profit = margin_score * 0.45 + roi_score * 0.35 + profit_buffer_score * 0.20

    competitor_count_score = _clamp(100 - _number(product.competitor_count) * 2)
    price_pressure = _number(product.price_competition_score)
    if not price_pressure and _number(product.competitor_price_avg) > 0:
        price_pressure = _clamp((price / _number(product.competitor_price_avg) - 0.85) / 0.50 * 100)
    saturation = _clamp(_number(product.market_saturation))
    position_score = {"below_market": 90, "market_aligned": 75, "premium": 45, "unknown": 50}[price_position]
    competition = competitor_count_score * 0.40 + (100 - price_pressure) * 0.25 + (100 - saturation) * 0.25 + position_score * 0.10

    if product.compliance_status in {"approved", "通过"}:
        compliance = 100 if product.compliance_note or product.certificates else 80
    elif product.compliance_status in {"rejected", "未通过"}:
        compliance = 0
    else:
        compliance = 55 if product.compliance_note or product.certificates else 30

    risk_deduction = 0.0
    risk_deduction += 80 if product.compliance_status in {"rejected", "未通过"} else 25 if compliance < 80 else 0
    risk_deduction += _clamp((target_margin - net_margin) / max(target_margin, 0.01) * 25, 0, 25)
    risk_deduction += _clamp(-_number(product.sales_growth_rate) * 1.5, 0, 15)
    risk_deduction += saturation * 0.12 + price_pressure * 0.08
    risk_deduction += (1 - completeness["ratio"]) * 25
    risk_deduction += {"high": 25, "medium": 12, "low": 0}.get(str(product.manual_risk_level or "unknown").lower(), 0)
    risk_score = _clamp(100 - risk_deduction)

    scores = {"profit": round(profit, 1), "demand": round(demand, 1), "competition": round(competition, 1), "compliance": round(compliance, 1), "risk": round(risk_score, 1)}
    base_score = round(sum(scores[name] * weight for name, weight in WEIGHTS.items()), 1)
    confidence_multiplier = 0.55 + completeness["ratio"] * 0.45
    recommendation_score = round(base_score * confidence_multiplier * _clamp(strategy_factor, 0.3, 1.2) * _clamp(historical_risk_factor, 0.2, 1.0), 1)
    total_score = recommendation_score if completeness["decision_ready"] else None
    grade = "S" if recommendation_score >= 85 else "A" if recommendation_score >= 75 else "B" if recommendation_score >= 60 else "C"

    risks: list[dict[str, Any]] = []

    def risk(code: str, level: str, message: str, action: str) -> None:
        risks.append({"code": code, "level": level, "message": message, "action": action})

    missing_codes = {"identity": "IDENTITY_INCOMPLETE", "demand": "DEMAND_EVIDENCE_MISSING", "profit": "COST_MODEL_INCOMPLETE", "competition": "COMPETITION_EVIDENCE_MISSING", "compliance": "COMPLIANCE_PENDING", "lineage": "LINEAGE_MISSING"}
    for item in missing:
        risk(missing_codes[item["dimension"]], "high" if item["dimension"] in {"profit", "compliance"} else "medium", f"{item['dimension']} 证据不完整：{item['missing']}。", item["action"])
    if product.compliance_status in {"rejected", "未通过"}:
        risk("COMPLIANCE_BLOCK", "high", "合规未通过，选品已阻断。", "补充材料或由人工驳回候选。")
    if price and net_margin < target_margin:
        risk("MARGIN_BELOW_TARGET", "medium", f"净利率 {net_margin:.1%} 低于目标 {target_margin:.1%}。", "复核成本、广告或调整价格。")
    if _number(product.sales_growth_rate) < -5:
        risk(
            "STATIC_SALES_GROWTH_DECLINE",
            "medium",
            "商品主档记录的外部销量增速为负；这不是多快照计算出的趋势。",
            "核对该字段来源；至少积累 2 个有效销量快照后再判断快照趋势。",
        )
    if saturation >= 75:
        risk("MARKET_SATURATION_HIGH", "medium", f"市场饱和度 {saturation:.0f}/100。", "验证差异化卖点和获客成本。")
    if historical_risk_factor < 1:
        risk("HISTORICAL_FAILURE_SIMILARITY", "medium", "存在与企业历史失败商品相似的确定性风险特征。", "查看历史失败原因并人工复核。")
    risk_level = "high" if risk_score < 45 or any(item["level"] == "high" for item in risks) else "medium" if risk_score < 75 or risks else "low"

    if product.compliance_status in {"rejected", "未通过"}:
        recommendation = "rejected"
    elif not completeness["decision_ready"]:
        recommendation = "review_required"
    elif grade in {"S", "A"}:
        recommendation = "recommended"
    elif grade == "B":
        recommendation = "review_required"
    else:
        recommendation = "rejected"

    score_explanations = {
        "profit": {"score": scores["profit"], "drivers": [f"净利率 {net_margin:.1%}（目标 {target_margin:.1%}）", f"单件净利润 {net_profit:.2f} {product.currency}", f"ROI {roi:.1%}"], "formula": "45%净利率达成 + 35% ROI + 20%利润缓冲"},
        "demand": {"score": scores["demand"], "drivers": [f"近30天销量 {_number(product.latest_30d_sales):.0f}", f"主档外部销量增速 {_number(product.sales_growth_rate):.1f}%（不等同于快照趋势）", f"评分 {_number(product.rating):.1f} / 评论 {_number(product.review_count):.0f}", f"搜索量 {_number(product.search_volume):.0f}"], "formula": "销量30% + 外部增速18% + 评分14% + 评论14% + 搜索14% + 主档趋势分10%"},
        "competition": {"score": scores["competition"], "drivers": [f"竞品 {_number(product.competitor_count):.0f} 个", f"市场饱和度 {saturation:.0f}/100", f"价格位置 {price_position}"], "formula": "竞品数量40% + 价格压力25% + 饱和度25% + 价格位置10%（高分代表竞争更友好）"},
        "compliance": {"score": scores["compliance"], "drivers": [f"合规状态 {product.compliance_status}", f"证书 {len(product.certificates or [])} 项"], "formula": "人工合规状态与证据闸门"},
        "risk": {"score": scores["risk"], "drivers": [f"安全余量 {risk_score:.1f}/100", f"风险项 {len(risks)} 个"], "formula": "100 - 合规/利润/趋势/饱和度/缺失数据扣分（高分代表更安全）"},
        "recommendation": {"score": recommendation_score, "grade": grade, "weights": WEIGHTS, "data_confidence": completeness["ratio"]},
    }
    evidence = {
        "demand": {"score": scores["demand"], "fields": [{"field": "latest_30d_sales", "value": product.latest_30d_sales, "source": product.sales_source, "evidence": product.sales_evidence}, {"field": "search_volume", "value": product.search_volume, "source": (product.field_lineage or {}).get("search_volume", "not_confirmed")}]},
        "profit": {"score": scores["profit"], "fields": [{"field": "current_price", "value": price, "source": (product.field_lineage or {}).get("currentPriceRub", "not_confirmed")}, {"field": "total_cost", "value": round(total_cost, 2), "source": "enterprise_operating_data"}, {"field": "net_margin", "value": net_margin, "source": "deterministic_evaluation_v2"}]},
        "competition": {"score": scores["competition"], "fields": [{"field": "competitor_count", "value": product.competitor_count, "source": "enterprise_verified", "evidence": product.competition_evidence}, {"field": "competitor_price_avg", "value": product.competitor_price_avg, "source": (product.field_lineage or {}).get("competitor_price_avg", "not_confirmed")}]},
        "compliance": {"score": scores["compliance"], "fields": [{"field": "compliance_status", "value": product.compliance_status, "source": "human_compliance_review", "evidence": product.compliance_note}]},
        "risk": {"score": scores["risk"], "fields": [{"field": "manual_risk_level", "value": product.manual_risk_level, "source": "human_or_rule"}]},
        "dynamic": {"base_score": base_score, "data_confidence": completeness["ratio"], "strategy_factor": strategy_factor, "historical_risk_factor": historical_risk_factor, "recommendation_score": recommendation_score},
    }
    result = AnalysisResult(
        demand_score=scores["demand"], profit_score=scores["profit"], competition_score=scores["competition"], compliance_score=scores["compliance"], risk_score=scores["risk"], total_score=total_score,
        base_score=base_score, recommendation_score=recommendation_score, recommendation_grade=grade, data_confidence=completeness["ratio"], data_completeness=completeness["percent"], evidence_completeness=completeness,
        missing_data=missing, missing_fields=missing_fields, risk_level=risk_level, risks=risks, evidence=evidence, score_explanations=score_explanations, recommendation=recommendation,
        gross_profit=round(gross_profit, 2), gross_margin=round(gross_margin, 4), net_profit=round(net_profit, 2), net_margin=round(net_margin, 4), roi=round(roi, 4),
        current_margin_rate=net_margin, expected_profit=net_profit, break_even_price=round(break_even_price, 2), target_price=round(target_price, 2), price_position=price_position,
        strategy_factor=strategy_factor, historical_risk_factor=historical_risk_factor,
    )
    if product.id and product.company_id:
        trust = build_trust(product, {key: getattr(result, key) for key in ("gross_profit", "gross_margin", "net_profit", "net_margin", "roi", "risk_level")})
        evidence["field_provenance"] = {key: value.model_dump(mode="json") for key, value in trust.fields.items()}
        evidence["disclosure"] = trust.disclosure
        for dimension in ("demand", "profit", "competition", "compliance", "risk"):
            for item in evidence[dimension]["fields"]:
                source = trust.fields.get(item["field"])
                item["source"] = source.provider if source else "unverified_input"
                item["is_mock"] = source.is_mock if source else trust.is_mock
                item["evidence_id"] = source.evidence_id if source else None
    return result


def recommendation_gate_status(
    *,
    compliance_status: str,
    decision_ready: bool,
    net_margin: float,
    target_margin: float,
    risk_level: str,
    recommendation: str,
) -> str:
    """Return the decision gate status independently from score and relative rank."""

    if compliance_status.casefold() in {"rejected", "未通过"}:
        return "BLOCKED"
    if not decision_ready:
        return "INSUFFICIENT_DATA"
    if net_margin < target_margin:
        return "NOT_RECOMMENDED"
    if risk_level.casefold() == "high":
        return "HUMAN_REVIEW_REQUIRED"
    if recommendation == "recommended":
        return "RECOMMENDED"
    return "REVIEW_REQUIRED"


def simulate_price(product: Product, proposed_price: float) -> dict[str, Any]:
    original = product.current_price
    product.current_price = proposed_price
    result = analyze(product)
    product.current_price = original
    return {"proposed_price": proposed_price, "gross_profit": result.gross_profit, "net_profit": result.net_profit, "roi": result.roi, "expected_profit": round(result.expected_profit, 2), "margin_rate": round(result.current_margin_rate, 4), "break_even_price": result.break_even_price, "target_price": result.target_price, "price_position": result.price_position, "base_score": result.base_score, "recommendation_score": result.recommendation_score, "recommendation_grade": result.recommendation_grade, "data_confidence": result.data_confidence, "risks": result.risks, "notice": "情景模拟只改变售价，不假设销量、转化率、广告成本或竞品价格会自动变化。"}


def to_dict(result: AnalysisResult) -> dict[str, Any]:
    return asdict(result)
