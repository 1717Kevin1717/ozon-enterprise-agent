"""Tenant-bound merchant CSV preview confirmations.

Revision ID: 0003_merchant_import_preview
Revises: 0002_enterprise_decision_platform
"""

from alembic import op
import sqlalchemy as sa

revision = "0003_merchant_import_preview"
down_revision = "0002_enterprise_decision_platform"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "merchant_import_previews" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "merchant_import_previews",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("company_id", sa.String(64), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("versions_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=False),
    )
    op.create_index("ix_merchant_import_previews_company_id", "merchant_import_previews", ["company_id"])


def downgrade() -> None:
    op.drop_index("ix_merchant_import_previews_company_id", "merchant_import_previews")
    op.drop_table("merchant_import_previews")
