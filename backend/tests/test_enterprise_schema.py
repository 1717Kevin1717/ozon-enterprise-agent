from app.db.models import (
    AgentEvaluation,
    AgentRun,
    ConnectorRun,
    ConversationSession,
    KnowledgeChunk,
    KnowledgeDocument,
    Product,
    ProductAnalysis,
    PromptTemplate,
)


def test_product_fact_layer_contains_enterprise_inputs():
    columns = set(Product.__table__.columns.keys())
    assert {
        "competitor_price_min",
        "competitor_price_avg",
        "price_position",
        "platform_fee",
        "shipping_cost",
        "search_volume",
        "trend_score",
        "price_competition_score",
        "market_saturation",
        "certificates",
        "manual_risk_level",
        "image_urls",
    } <= columns


def test_analysis_layer_separates_derived_metrics_from_product_facts():
    columns = set(ProductAnalysis.__table__.columns.keys())
    assert {
        "gross_profit",
        "gross_margin",
        "net_profit",
        "net_margin",
        "roi",
        "risk_score",
        "recommendation_grade",
        "data_completeness",
        "missing_fields",
        "score_explanations",
        "algorithm_version",
        "input_snapshot",
    } <= columns


def test_session_and_agent_run_have_auditable_state():
    assert {"goal_summary", "state_json", "last_product_ids", "conclusion_summary"} <= set(
        ConversationSession.__table__.columns.keys()
    )
    assert {"active_provider", "response_mode", "fallback_reason", "intent", "prompt_version"} <= set(
        AgentRun.__table__.columns.keys()
    )


def test_enterprise_extension_tables_are_tenant_scoped():
    for model in [KnowledgeDocument, KnowledgeChunk, ConnectorRun, AgentEvaluation, PromptTemplate]:
        assert "company_id" in model.__table__.columns
