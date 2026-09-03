"""Initial enterprise product intelligence schema.

Revision ID: 0001_initial
"""
from alembic import op
from app.db.models import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)

def downgrade():
    Base.metadata.drop_all(bind=op.get_bind())
