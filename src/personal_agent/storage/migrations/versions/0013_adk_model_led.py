"""Add v2 Task/run/attempt/evidence/budget storage; leave legacy rows untouched."""

import sqlalchemy as sa
from alembic import op

from personal_agent.storage.run_schema_v2 import RUN_TABLES, register_run_tables
from personal_agent_core.sqlite import NAMING_CONVENTION

revision = "0013_adk_model_led"
down_revision = "0012_calendar_media_merge"
branch_labels = None
depends_on = None


def _tables():
    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)
    # Existing parents resolve FKs; never recreate or alter these tables here.
    sa.Table("operations", metadata, sa.Column("operation_id", sa.Text, primary_key=True))
    sa.Table("conversations", metadata, sa.Column("conversation_id", sa.Text, primary_key=True))
    return register_run_tables(metadata)


def upgrade():
    tables = _tables()
    for name in RUN_TABLES:
        tables[name].create(op.get_bind())


def downgrade():
    tables = _tables()
    bind = op.get_bind()
    if any(bind.execute(sa.select(sa.func.count()).select_from(tables[name])).scalar_one() for name in RUN_TABLES):
        raise RuntimeError("v2 state exists; rollback uses a compatible binary, not destructive downgrade")
    for name in reversed(RUN_TABLES):
        tables[name].drop(bind)
