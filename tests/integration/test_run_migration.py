"""The v2 tables exist in real migrations and cannot erase live state on rollback."""

import pytest
from sqlalchemy import insert, inspect, text

from personal_agent.storage import db
from personal_agent.storage.engine import create_database_engine
from personal_agent.storage.models import Base
from personal_agent.storage.run_schema_v2 import RUN_TABLES


def test_migration_adds_declared_columns_without_touching_legacy_rows(tmp_path):
    engine = create_database_engine(tmp_path / "migration.sqlite")
    try:
        db.upgrade(engine, "0012_calendar_media_merge")
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO conversations(conversation_id,created_at,next_sequence,is_canonical) VALUES ('synthetic','2026-09-14T00:00:00Z',1,0)"))
        db.upgrade(engine)
        inspector = inspect(engine)
        for name in RUN_TABLES:
            assert {c["name"] for c in inspector.get_columns(name)} == set(Base.metadata.tables[name].c.keys())
        with engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM conversations WHERE conversation_id='synthetic'")).scalar_one() == 1
            assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []
        db.downgrade(engine, "0012_calendar_media_merge")
        assert not (set(RUN_TABLES) & set(inspect(engine).get_table_names()))
    finally:
        engine.dispose()


def test_nonempty_budget_history_blocks_destructive_downgrade(tmp_path):
    engine = create_database_engine(tmp_path / "retained.sqlite")
    try:
        db.upgrade(engine)
        with engine.begin() as conn:
            conn.execute(insert(Base.metadata.tables["search_budget_days"]).values(
                provider="synthetic", utc_day="2026-09-14", reserved_count=1, limit_snapshot=100))
        with pytest.raises(RuntimeError, match="v2 state exists"):
            db.downgrade(engine, "0012_calendar_media_merge")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT reserved_count FROM search_budget_days")).scalar_one() == 1
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0015_dal_delivery_status"
    finally:
        engine.dispose()


def test_matching_create_all_tables_are_adopted_without_losing_data(tmp_path):
    engine = create_database_engine(tmp_path / 'created.sqlite')
    db.upgrade(engine, '0012_calendar_media_merge')
    # Reproduce the pre-bridge create_all schema this adoption gate covers.
    Base.metadata.create_all(engine, tables=[
        table for name, table in Base.metadata.tables.items()
        if not name.startswith("dal_resume_")
    ])
    with engine.begin() as conn:
        conn.execute(insert(Base.metadata.tables['search_budget_days']).values(
            provider='synthetic', utc_day='2026-09-14', reserved_count=2, limit_snapshot=100))
    db.upgrade(engine)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT reserved_count FROM search_budget_days')).scalar_one() == 2
        assert conn.execute(text('PRAGMA foreign_key_check')).all() == []
    engine.dispose()


@pytest.mark.parametrize('damage', ['partial', 'missing_index', 'wrong_column'])
def test_existing_v2_schema_must_match_before_adoption(tmp_path, damage):
    engine = create_database_engine(tmp_path / 'damaged.sqlite')
    db.upgrade(engine, '0012_calendar_media_merge')
    if damage == 'partial':
        Base.metadata.tables['search_budget_days'].create(engine)
    else:
        # Restrict the historical adoption fixture to pre-bridge tables.
        Base.metadata.create_all(engine, tables=[
            table for name, table in Base.metadata.tables.items()
            if not name.startswith("dal_resume_")
        ])
        with engine.begin() as conn:
            if damage == 'wrong_column':
                conn.execute(text('ALTER TABLE search_budget_days ADD COLUMN unexpected TEXT'))
            else:
                index = inspect(conn).get_indexes('agent_runs')[0]['name']
                conn.execute(text(f'DROP INDEX "{index}"'))
    with pytest.raises(RuntimeError, match='existing v2 schema'):
        db.upgrade(engine)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT version_num FROM alembic_version')).scalar_one() == '0012_calendar_media_merge'
    engine.dispose()
