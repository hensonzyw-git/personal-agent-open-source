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


def _signature(inspector, name):
    # Compare reflected schemas on the same dialect, including constraints and
    # indexes. checkfirst alone would silently bless a partial/older dev schema.
    import json
    columns = [{k: (str(v) if k == "type" else v) for k, v in column.items()}
               for column in inspector.get_columns(name)]
    return json.dumps({
        "columns": columns,
        "pk": inspector.get_pk_constraint(name),
        "fk": sorted(inspector.get_foreign_keys(name), key=lambda c: str(c)),
        "unique": sorted(inspector.get_unique_constraints(name), key=lambda c: str(c)),
        "checks": sorted(inspector.get_check_constraints(name), key=lambda c: str(c)),
        "indexes": sorted(inspector.get_indexes(name), key=lambda c: str(c)),
    }, sort_keys=True, default=str)


def upgrade():
    tables = _tables()
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names()) & set(RUN_TABLES)
    if existing:
        if existing != set(RUN_TABLES):
            raise RuntimeError("existing v2 schema is partial; explicit repair required")
        reference = sa.create_engine("sqlite:///:memory:")
        try:
            tables[RUN_TABLES[0]].metadata.create_all(reference)
            expected = sa.inspect(reference)
            if any(_signature(inspector, name) != _signature(expected, name) for name in RUN_TABLES):
                raise RuntimeError("existing v2 schema differs; explicit repair required")
        finally:
            reference.dispose()
        return
    for name in RUN_TABLES:
        tables[name].create(bind)


def downgrade():
    tables = _tables()
    bind = op.get_bind()
    if any(bind.execute(sa.select(sa.func.count()).select_from(tables[name])).scalar_one() for name in RUN_TABLES):
        raise RuntimeError("v2 state exists; rollback uses a compatible binary, not destructive downgrade")
    for name in reversed(RUN_TABLES):
        tables[name].drop(bind)
