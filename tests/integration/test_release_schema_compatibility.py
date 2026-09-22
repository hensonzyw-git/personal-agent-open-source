"""Release gates: real historical migrations, synthetic rows, no stamp/adoption."""
from datetime import datetime, timezone
import importlib
import json

from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, Table, inspect, insert, select, text

from personal_agent.storage import db
from personal_agent.storage.engine import create_database_engine
from personal_agent.storage.models import Base
from personal_agent.storage.run_schema_v2 import RUN_TABLES
from personal_agent.backup.restore_verify import check_schema_version, check_dal_schema_version

NOW = datetime(2026, 9, 16, tzinfo=timezone.utc)


def _rows(engine, names):
    with engine.connect() as conn:
        return {name: conn.execute(text(f'SELECT * FROM "{name}"')).all() for name in names}


def test_main_0013_rows_and_schema_survive_bridge_release(tmp_path):
    path = tmp_path / 'pa.sqlite'
    engine = create_database_engine(path)
    try:
        db.upgrade(engine, '0013_adk_model_led')
        with engine.begin() as conn:
            conn.execute(insert(Base.metadata.tables['devices']).values(
                device_id='synthetic-device', display_name='fixture', public_key='fixture',
                device_key_thumbprint='fixture', status='active', scopes='[]',
                allowed_tools_version='v1', created_at=NOW))
            conn.execute(insert(Base.metadata.tables['conversations']).values(
                conversation_id='synthetic-conversation', created_at=NOW,
                next_sequence=1, is_canonical=False))
            conn.execute(insert(Base.metadata.tables['search_budget_days']).values(
                provider='synthetic', utc_day='2026-09-16', reserved_count=7, limit_snapshot=100))
            conn.execute(insert(Base.metadata.tables['media_objects']).values(
                media_id='synthetic-media', device_id='synthetic-device',
                purpose='chat_image', retention_class='timeline_media', state='deleted',
                state_version=1, created_at=NOW, updated_at=NOW, deleted_at=NOW))
        names = set(inspect(engine).get_table_names()) - {'alembic_version'}
        before = _rows(engine, names)
        signature = importlib.import_module('personal_agent.storage.migrations.versions.0013_adk_model_led')._signature
        schemas = {name: signature(inspect(engine), name) for name in names}
        db.upgrade(engine)
        assert _rows(engine, names) == before
        assert all(signature(inspect(engine), name) == schemas[name] for name in names)
        assert set(RUN_TABLES) <= names
        graph = ScriptDirectory.from_config(db.alembic_config(engine))
        assert graph.get_heads() == ['0022_dal_command_retry']
        assert graph.get_revision('0015_dal_delivery_status').down_revision == '0014_dal_resume_decisions'
        assert graph.get_revision('0014_dal_resume_decisions').down_revision == '0013_adk_model_led'
        with engine.connect() as conn:
            assert conn.execute(text('PRAGMA foreign_key_check')).all() == []
        assert check_schema_version(path)['ok']
    finally:
        engine.dispose()


def test_empty_pa_head_matches_model_schema(tmp_path):
    migrated = create_database_engine(tmp_path / 'migrated.sqlite')
    modeled = create_database_engine(tmp_path / 'modeled.sqlite')
    try:
        db.upgrade(migrated)
        Base.metadata.create_all(modeled)
        signature = importlib.import_module('personal_agent.storage.migrations.versions.0013_adk_model_led')._signature
        assert set(inspect(migrated).get_table_names()) - {'alembic_version'} == set(Base.metadata.tables)
        for name in Base.metadata.tables:
            actual = json.loads(signature(inspect(migrated), name))
            expected = json.loads(signature(inspect(modeled), name))
            # ALTER TABLE appends columns; physical order is not a contract.
            actual['columns'].sort(key=lambda c: c['name'])
            expected['columns'].sort(key=lambda c: c['name'])
            assert actual == expected, name
    finally:
        migrated.dispose()
        modeled.dispose()


def test_dal_0010_historical_job_unknown_effect_and_receipt_survive_0018(tmp_path):
    from personal_agent_dal.storage import db as dal_db
    from personal_agent_dal.storage.engine import create_database_engine as dal_engine
    path = tmp_path / 'dal.sqlite'
    engine = dal_engine(path)
    try:
        dal_db.upgrade(engine, '0010')
        metadata = MetaData()
        jobs = Table('worker_jobs', metadata, autoload_with=engine)
        receipts = Table('worker_result_receipts', metadata, autoload_with=engine)
        effects = Table('external_effects', metadata, autoload_with=engine)
        features = Table('features', metadata, autoload_with=engine)
        with engine.begin() as conn:
            conn.execute(insert(features).values(feature_id='synthetic-feature',
                schema_version='dal.feature/1.0', version=1, state='intake',
                repository_id='synthetic-repo', base_sha='a'*40,
                decision_frontier_version=0, policy_version='synthetic', capability_epoch=0,
                external_effect_inventory_sha256='c'*64, trace_id='synthetic-trace',
                created_at=NOW.isoformat(), updated_at=NOW.isoformat()))
            conn.execute(insert(effects).values(effect_id='synthetic-effect', version=1,
                origin='dal_dispatched', owner_aggregate_type='feature',
                owner_aggregate_id='synthetic-feature', effect_scope_key='synthetic-scope',
                remote_idempotency_key='synthetic-key', target_fingerprint='synthetic-target',
                state='unknown', attempt=1, created_at=NOW.isoformat(), updated_at=NOW.isoformat()))
            conn.execute(insert(jobs).values(job_id='synthetic-job', feature_id='synthetic-feature',
                repository_id='synthetic-repo', base_sha='a'*40, branch_name='synthetic',
                toolchain_ref='synthetic', state='failed', attempt_count=1, lease_epoch=3,
                last_error='synthetic unknown outcome', created_at=NOW.isoformat(), updated_at=NOW.isoformat()))
            conn.execute(insert(receipts).values(receipt_id='synthetic-receipt', job_id='synthetic-job',
                result_sha256='b'*64, receipt_schema_version='dal.worker-result/1.0', recorded_at=NOW.isoformat()))
        with engine.connect() as conn:
            old_job = dict(conn.execute(select(jobs)).mappings().one())
        old_receipts = _rows(engine, ['worker_result_receipts', 'external_effects', 'features'])
        dal_db.upgrade(engine)
        with engine.connect() as conn:
            current = dict(conn.execute(text('SELECT * FROM worker_jobs')).mappings().one())
            assert {key: current[key] for key in old_job} == old_job
            assert current['execution_mode'] == 'legacy_unclassified'
            assert conn.execute(text('SELECT version_num FROM alembic_version')).scalar_one() == '0022'
            assert conn.execute(text('PRAGMA foreign_key_check')).all() == []
        assert _rows(engine, ['worker_result_receipts', 'external_effects', 'features']) == old_receipts
        assert check_dal_schema_version(path)['ok']
    finally:
        engine.dispose()
