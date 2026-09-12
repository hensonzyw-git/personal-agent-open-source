"""Both released-candidate lineages must converge without dropping tables."""

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from personal_agent.storage import db
from personal_agent.storage.engine import create_database_engine


@pytest.mark.parametrize("previous", [
    "0011_action_plan", "0010_media_upload_idempotency",
])
def test_calendar_and_media_heads_converge(tmp_path, previous):
    engine = create_database_engine(tmp_path / "merged.sqlite")
    try:
        scripts = ScriptDirectory.from_config(db.alembic_config(engine))
        assert scripts.get_heads() == ["0012_calendar_media_merge"]
        db.upgrade(engine, previous)
        previous_tables = set(inspect(engine).get_table_names())
        db.upgrade(engine)
        expected_tables = set(inspect(engine).get_table_names())
        assert {"media_objects", "media_attempts", "operations"} <= expected_tables
        columns = {column["name"] for column in inspect(engine).get_columns("operations")}
        assert {"plan_key", "plan_index"} <= columns
        with engine.connect() as connection:
            assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all() == [
                "0012_calendar_media_merge"
            ]
        # A branched history needs an explicit rollback target, not "-1".
        # These databases are synthetic and contain no user media/actions.
        db.downgrade(engine, previous)
        assert previous_tables <= set(inspect(engine).get_table_names())
        db.upgrade(engine)
    finally:
        engine.dispose()
