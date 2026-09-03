from app.db.models import Product
from app.services.decision_engine import analyze

def complete_product() -> Product:
    return Product(
        id="p1", company_id="c1", external_product_id="ozon-1", title="便携收纳盒", brand="Brand",
        category_path="家居收纳 > 桌面收纳", url="https://www.ozon.ru/product/ozon-1",
        main_image_url="https://images.example.com/ozon-1.jpg", current_price=1000, regular_price=1290,
        competitor_price_min=850, competitor_price_avg=1050, review_count=100, rating=4.5,
        latest_30d_sales=50, sales_growth_rate=8, search_volume=12000, trend_score=68,
        sales_source="enterprise_verified", sales_evidence="authorized report 2026-09-01",
        competitor_count=8, price_competition_score=35, market_saturation=42,
        competition_evidence="eight manually confirmed comparable products", compliance_status="approved",
        compliance_note="internal review", certificates=["material-declaration"], procurement_cost=300,
        shipping_cost=80, platform_fee=15, advertising_cost=70, warehousing_cost=20, tax_cost=30,
        return_loss_reserve=15, platform_commission_rate=.18, target_margin_rate=.30,
        field_lineage={"currentPriceRub": "visible_page", "search_volume": "authorized_market_report"},
    )

def test_complete_product_has_explainable_scores():
    result=analyze(complete_product())
    assert result.total_score is not None
    assert result.evidence["demand"]["fields"][0]["source"] == "enterprise_verified"
    assert result.evidence_completeness["percent"] == 100
    assert result.recommendation_score == result.base_score
    assert result.recommendation in {"recommended","review_required","rejected"}
    assert result.recommendation_grade in {"S", "A", "B", "C"}
    assert result.net_profit < result.gross_profit
    assert result.score_explanations["profit"]["drivers"]

def test_compliance_rejection_blocks_recommendation():
    product=complete_product(); product.compliance_status="rejected"
    result=analyze(product)
    assert result.recommendation == "rejected"
    assert any(item["code"] == "COMPLIANCE_BLOCK" for item in result.risks)

def test_missing_sales_evidence_prevents_total_score():
    product=complete_product(); product.sales_evidence=""
    result=analyze(product)
    assert result.total_score is None
    assert result.evidence_completeness["percent"] < 100
    assert any(item["dimension"] == "demand" for item in result.missing_data)
    assert any(item["code"] == "DEMAND_EVIDENCE_MISSING" for item in result.risks)

def test_historical_failure_factor_reduces_dynamic_recommendation():
    product=complete_product()
    base=analyze(product)
    risked=analyze(product,historical_risk_factor=.75)
    assert risked.recommendation_score < base.recommendation_score
    assert any(item["code"] == "HISTORICAL_FAILURE_SIMILARITY" for item in risked.risks)


def test_profit_score_changes_with_real_cost_inputs():
    efficient = complete_product()
    expensive = complete_product()
    expensive.procurement_cost = 570
    expensive.advertising_cost = 160
    assert analyze(efficient).profit_score > analyze(expensive).profit_score
    assert analyze(efficient).net_profit > analyze(expensive).net_profit


def test_competition_score_changes_with_market_pressure():
    low_pressure = complete_product()
    high_pressure = complete_product()
    high_pressure.competitor_count = 40
    high_pressure.price_competition_score = 85
    high_pressure.market_saturation = 90
    assert analyze(low_pressure).competition_score > analyze(high_pressure).competition_score


def test_data_completeness_never_claims_100_with_missing_required_fields():
    product = complete_product()
    product.main_image_url = ""
    result = analyze(product)
    assert result.data_completeness < 100
    assert "main_image_url" in result.missing_fields
    assert result.total_score is None
