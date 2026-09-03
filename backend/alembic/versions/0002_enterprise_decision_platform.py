"""Enterprise decision platform fields and audit entities.

Revision ID: 0002_enterprise_decision_platform
Revises: 0001_initial

The original 0001 migration builds the then-current SQLAlchemy metadata.  This
migration therefore checks existing columns/tables so it works for both an
already-running database and a fresh local demonstration database.
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_enterprise_decision_platform"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def _add_missing_columns(table_name: str, definitions: list[tuple[str, sa.Column]]) -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {item["name"] for item in inspector.get_columns(table_name)}
    for name, column in definitions:
        if name not in existing:
            op.add_column(table_name, column)
            existing.add(name)


def upgrade():
    _add_missing_columns("products", [
        ("image_urls", sa.Column("image_urls", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))),
        ("seller_name", sa.Column("seller_name", sa.String(255), nullable=False, server_default="")),
        ("competitor_price_min", sa.Column("competitor_price_min", sa.Float(), nullable=False, server_default="0")),
        ("competitor_price_avg", sa.Column("competitor_price_avg", sa.Float(), nullable=False, server_default="0")),
        ("price_position", sa.Column("price_position", sa.String(32), nullable=False, server_default="unknown")),
        ("search_volume", sa.Column("search_volume", sa.Float(), nullable=False, server_default="0")),
        ("trend_score", sa.Column("trend_score", sa.Float(), nullable=False, server_default="0")),
        ("price_competition_score", sa.Column("price_competition_score", sa.Float(), nullable=False, server_default="0")),
        ("market_saturation", sa.Column("market_saturation", sa.Float(), nullable=False, server_default="0")),
        ("certificates", sa.Column("certificates", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))),
        ("manual_risk_level", sa.Column("manual_risk_level", sa.String(32), nullable=False, server_default="unknown")),
        ("shipping_cost", sa.Column("shipping_cost", sa.Float(), nullable=False, server_default="0")),
        ("platform_fee", sa.Column("platform_fee", sa.Float(), nullable=False, server_default="0")),
        ("warehousing_cost", sa.Column("warehousing_cost", sa.Float(), nullable=False, server_default="0")),
        ("tax_cost", sa.Column("tax_cost", sa.Float(), nullable=False, server_default="0")),
        ("return_loss_reserve", sa.Column("return_loss_reserve", sa.Float(), nullable=False, server_default="0")),
        ("other_cost", sa.Column("other_cost", sa.Float(), nullable=False, server_default="0")),
        ("sync_status", sa.Column("sync_status", sa.String(32), nullable=False, server_default="not_synced")),
        ("last_sync_at", sa.Column("last_sync_at", sa.DateTime(), nullable=True)),
        ("source_updated_at", sa.Column("source_updated_at", sa.DateTime(), nullable=True)),
    ])
    _add_missing_columns("product_analyses", [
        ("risk_score", sa.Column("risk_score", sa.Float(), nullable=False, server_default="0")),
        ("recommendation_grade", sa.Column("recommendation_grade", sa.String(8), nullable=False, server_default="C")),
        ("gross_profit", sa.Column("gross_profit", sa.Float(), nullable=False, server_default="0")),
        ("gross_margin", sa.Column("gross_margin", sa.Float(), nullable=False, server_default="0")),
        ("net_profit", sa.Column("net_profit", sa.Float(), nullable=False, server_default="0")),
        ("net_margin", sa.Column("net_margin", sa.Float(), nullable=False, server_default="0")),
        ("roi", sa.Column("roi", sa.Float(), nullable=False, server_default="0")),
        ("data_completeness", sa.Column("data_completeness", sa.Float(), nullable=False, server_default="0")),
        ("missing_fields", sa.Column("missing_fields", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))),
        ("score_explanations", sa.Column("score_explanations", sa.JSON(), nullable=False, server_default=sa.text("'{}'"))),
    ])
    _add_missing_columns("conversation_sessions", [
        ("goal_summary", sa.Column("goal_summary", sa.Text(), nullable=False, server_default="")),
        ("state_json", sa.Column("state_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'"))),
        ("last_product_ids", sa.Column("last_product_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))),
        ("conclusion_summary", sa.Column("conclusion_summary", sa.Text(), nullable=False, server_default="")),
        ("memory_version", sa.Column("memory_version", sa.String(32), nullable=False, server_default="v1")),
    ])
    _add_missing_columns("agent_runs", [
        ("active_provider", sa.Column("active_provider", sa.String(64), nullable=False, server_default="rule_engine")),
        ("response_mode", sa.Column("response_mode", sa.String(64), nullable=False, server_default="rule_engine")),
        ("fallback_reason", sa.Column("fallback_reason", sa.String(128), nullable=False, server_default="")),
        ("intent", sa.Column("intent", sa.String(64), nullable=False, server_default="")),
        ("prompt_version", sa.Column("prompt_version", sa.String(32), nullable=False, server_default="v1")),
        ("tool_registry_version", sa.Column("tool_registry_version", sa.String(32), nullable=False, server_default="v1")),
    ])

    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "knowledge_documents" not in tables:
        op.create_table(
            "knowledge_documents",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("company_id", sa.String(64), sa.ForeignKey("companies.id"), nullable=False),
            sa.Column("filename", sa.String(500), nullable=False),
            sa.Column("content_type", sa.String(128), nullable=False, server_default="application/octet-stream"),
            sa.Column("source_type", sa.String(64), nullable=False, server_default="user_upload"),
            sa.Column("storage_path", sa.Text(), nullable=False, server_default=""),
            sa.Column("checksum", sa.String(128), nullable=False, server_default=""),
            sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
            sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error_code", sa.String(128), nullable=False, server_default=""),
            sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("company_id", "checksum", name="uq_knowledge_document_company_checksum"),
        )
        op.create_index("ix_knowledge_documents_company_id", "knowledge_documents", ["company_id"])
    if "knowledge_chunks" not in tables:
        op.create_table(
            "knowledge_chunks",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("company_id", sa.String(64), sa.ForeignKey("companies.id"), nullable=False),
            sa.Column("document_id", sa.String(64), sa.ForeignKey("knowledge_documents.id"), nullable=False),
            sa.Column("chunk_index", sa.Integer(), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("embedding", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("document_id", "chunk_index", name="uq_knowledge_chunk_document_index"),
        )
        op.create_index("ix_knowledge_chunks_company_id", "knowledge_chunks", ["company_id"])
        op.create_index("ix_knowledge_chunks_document_id", "knowledge_chunks", ["document_id"])
    if "connector_runs" not in tables:
        op.create_table(
            "connector_runs",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("company_id", sa.String(64), sa.ForeignKey("companies.id"), nullable=False),
            sa.Column("connector_type", sa.String(64), nullable=False),
            sa.Column("provider", sa.String(64), nullable=False),
            sa.Column("operation", sa.String(128), nullable=False),
            sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
            sa.Column("records_requested", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("records_succeeded", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("records_failed", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error_code", sa.String(128), nullable=False, server_default=""),
            sa.Column("summary_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_connector_runs_company_id", "connector_runs", ["company_id"])
    if "agent_evaluations" not in tables:
        op.create_table(
            "agent_evaluations",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("company_id", sa.String(64), sa.ForeignKey("companies.id"), nullable=False),
            sa.Column("agent_run_id", sa.String(64), sa.ForeignKey("agent_runs.id"), nullable=True),
            sa.Column("task_name", sa.String(255), nullable=False),
            sa.Column("task_input", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("expected_output", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("actual_output", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("task_success_rate", sa.Float(), nullable=False, server_default="0"),
            sa.Column("answer_accuracy", sa.Float(), nullable=False, server_default="0"),
            sa.Column("tool_calling_accuracy", sa.Float(), nullable=False, server_default="0"),
            sa.Column("data_completeness", sa.Float(), nullable=False, server_default="0"),
            sa.Column("judge_mode", sa.String(32), nullable=False, server_default="deterministic"),
            sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
            sa.Column("details_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_agent_evaluations_company_id", "agent_evaluations", ["company_id"])
        op.create_index("ix_agent_evaluations_agent_run_id", "agent_evaluations", ["agent_run_id"])
    if "prompt_templates" not in tables:
        op.create_table(
            "prompt_templates",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("company_id", sa.String(64), sa.ForeignKey("companies.id"), nullable=False),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("version", sa.String(32), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
            sa.Column("schema_version", sa.String(32), nullable=False, server_default="v1"),
            sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("company_id", "name", "version", name="uq_prompt_company_name_version"),
        )
        op.create_index("ix_prompt_templates_company_id", "prompt_templates", ["company_id"])


def downgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    for table_name in ["prompt_templates", "agent_evaluations", "connector_runs", "knowledge_chunks", "knowledge_documents"]:
        if table_name in tables:
            op.drop_table(table_name)
    removals = {
        "agent_runs": ["active_provider", "response_mode", "fallback_reason", "intent", "prompt_version", "tool_registry_version"],
        "conversation_sessions": ["goal_summary", "state_json", "last_product_ids", "conclusion_summary", "memory_version"],
        "product_analyses": ["risk_score", "recommendation_grade", "gross_profit", "gross_margin", "net_profit", "net_margin", "roi", "data_completeness", "missing_fields", "score_explanations"],
        "products": ["image_urls", "seller_name", "competitor_price_min", "competitor_price_avg", "price_position", "search_volume", "trend_score", "price_competition_score", "market_saturation", "certificates", "manual_risk_level", "shipping_cost", "platform_fee", "warehousing_cost", "tax_cost", "return_loss_reserve", "other_cost", "sync_status", "last_sync_at", "source_updated_at"],
    }
    for table_name, columns in removals.items():
        existing = {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table_name)}
        with op.batch_alter_table(table_name) as batch:
            for column in columns:
                if column in existing:
                    batch.drop_column(column)
