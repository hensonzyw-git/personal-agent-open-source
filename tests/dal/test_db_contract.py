"""DAL-008: `DAL-T-DB-CONTRACT-001`, replayed from the frozen contracts.

Five variants, each the minimal proof of one database obligation
(docs/dal/DAL008_数据库Schema设计草案_v0.1.md §1):

- `migration_up`: an empty database reaches revision `0001`, and the migration
  leaves a receipt and an audit row rather than only a schema;
- `cas_conflict`: a compare-and-swap against a stale version writes nothing and
  returns `VERSION_CONFLICT`;
- `idempotency_unique`: the same idempotency key with different content returns
  `IDEMPOTENCY_CONFLICT` and does not overwrite the existing receipt;
- `sensitive_field_encryption`: a sealed column round-trips, and the plaintext
  canary never reaches the database file;
- `retention_delete`: a cutoff deletes only what precedes it and leaves a
  tombstone.

Every variant is loaded from `docs/dal/manifests/`, bound by its
manifest-declared content hash, executed against a **real SQLite database**
through the real `apply_database_contract` entry point, and judged by its
frozen oracle. The fixtures' `injected_results` are not used to manufacture an
outcome; the database produces it.

The three tests after the replay cover what a single-session fixture replay
structurally cannot see: a real cross-session race, the raw bytes of the
database file, and the audit chain's own witness.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.dal.contract_loader import FrozenContracts
from tests.dal.operation_executor import execute_fixture
from tests.dal.oracle_comparator import compare
from tests.dal.side_effects import fresh_probe


TEST_ID = "DAL-T-DB-CONTRACT-001"
EXPECTED_VARIANTS = {
    "cas_conflict",
    "idempotency_unique",
    "migration_up",
    "retention_delete",
    "sensitive_field_encryption",
}
CANARY = "DAL_CANARY_REDACTED"


@pytest.fixture(scope="module")
def contracts() -> FrozenContracts:
    return FrozenContracts()


def test_manifest_covers_exactly_the_five_expected_variants(
    contracts: FrozenContracts,
) -> None:
    """The frozen set is exactly the five expected variants, all owned by DAL-008."""
    variants = contracts.variants(TEST_ID)
    assert {v.variant_id for v in variants} == EXPECTED_VARIANTS
    for variant in variants:
        assert variant.run_gate == "G1"
        assert variant.owner_tasks == ("DAL-008",)


@pytest.mark.parametrize("variant_id", sorted(EXPECTED_VARIANTS))
def test_db_contract_variant(
    contracts: FrozenContracts, variant_id: str, tmp_path: Path
) -> None:
    """Replay one frozen variant against a real database and judge by its oracle."""
    variant = next(
        v for v in contracts.variants(TEST_ID) if v.variant_id == variant_id
    )
    probe = fresh_probe()
    trace = execute_fixture(
        variant.fixture.body, probe=probe, database=tmp_path / "dal.db"
    )
    result = compare(trace, variant.oracle.body)
    assert result.passed, (
        f"{variant_id} diverged from its frozen oracle:\n"
        + "\n".join(f"  - {m}" for m in result.mismatches)
    )
    # The oracle is judged against writes observed in the database. This
    # additionally holds the implementation to its own account of them, so a
    # handler cannot claim a write it stopped performing.
    assert set(trace.declared_write_set) == set(trace.write_set), (
        f"{variant_id}: declared {sorted(set(trace.declared_write_set))} "
        f"but the database shows {sorted(set(trace.write_set))}"
    )


def test_failed_sealed_roundtrip_is_never_reported_as_success(
    tmp_path: Path,
) -> None:
    """A round trip that did not round-trip must fail closed, writing nothing.

    On a healthy path the comparison always succeeds, so removing it changes
    nothing observable — a defect injection that deleted the check left the
    suite green. The only way to hold the guard is to make the seam lie: this
    key ring seals correctly and opens to the wrong value, exactly as a wrong
    key, a drifted AAD or a corrupted envelope would. `APPLIED` here would mean
    the service certifying encryption that does not work.
    """
    from sqlalchemy import select

    from personal_agent_core.crypto import KeyRing, generate_key
    from personal_agent_dal.receipt import ReceiptCode
    from personal_agent_dal.storage import db
    from personal_agent_dal.storage.engine import (
        create_database_engine,
        session_factory,
    )
    from personal_agent_dal.storage.models import Event
    from personal_agent_dal.storage.operations import apply_database_contract

    class LyingKeyRing:
        """Seals for real; opens to something else."""

        def __init__(self, inner: KeyRing) -> None:
            self._inner = inner

        def encrypt(self, plaintext: bytes, **kwargs: object) -> dict:
            return self._inner.encrypt(plaintext, **kwargs)  # type: ignore[arg-type]

        def decrypt(self, envelope: dict, **kwargs: object) -> bytes:
            self._inner.decrypt(envelope, **kwargs)  # type: ignore[arg-type]
            return b"not what was sealed"

    database = tmp_path / "dal.db"
    engine = create_database_engine(database)
    db.upgrade(engine)
    keyring = LyingKeyRing(KeyRing([generate_key("keyref://dal/data/v1")],
                                   service="dal"))

    outcome = apply_database_contract(
        engine,
        {
            "operation_id": "op-seal-liar",
            "operation_spec_id": "OP-DB-CONTRACT-001",
            "idempotency_key": "idem-seal-liar",
            "actor_type": "service",
            "evidence_source_type": "migration-runner",
            "input": {
                "schema_version": "dal.operation-input/1.0",
                "target": {
                    "entity_id": "fixture-entity",
                    "entity_type": "database",
                    "state": "migrated",
                    "version": 7,
                },
                "action_sequence": [
                    {"command": "write_encrypted_record",
                     "plaintext_canary": CANARY},
                    {"command": "read_encrypted_record"},
                ],
                "authoritative_facts": {},
                "injected_results": [],
            },
        },
        keyring=keyring,  # type: ignore[arg-type]
    )

    assert outcome.receipt.code is not ReceiptCode.APPLIED
    assert outcome.writes == ()
    with sessions_of(engine) as session:
        assert list(session.scalars(select(Event.event_id))) == []


def sessions_of(engine):  # noqa: ANN001, ANN201 - test helper
    from personal_agent_dal.storage.engine import session_factory

    return session_factory(engine)()


def test_retention_removes_the_row_and_keeps_the_rest(tmp_path: Path) -> None:
    """Retention must delete the data, not merely record that it did.

    The frozen oracle checks the write classes, and a tombstone is one of
    them — so an implementation that writes the tombstone and skips the delete
    satisfies it completely. A defect injection that removed the `DELETE`
    left every oracle green. Retention's whole purpose is that the row is
    gone, so that is asserted here directly, row by row.
    """
    from sqlalchemy import select

    from personal_agent_dal.receipt import ReceiptCode
    from personal_agent_dal.storage import db
    from personal_agent_dal.storage.engine import (
        create_database_engine,
        session_factory,
    )
    from personal_agent_dal.storage.models import Event, RetentionTombstone
    from personal_agent_dal.storage.operations import apply_database_contract
    from personal_agent_core.timeutil import parse_rfc3339
    from tests.dal.factories import event_row

    database = tmp_path / "dal.db"
    engine = create_database_engine(database)
    db.upgrade(engine)
    sessions = session_factory(engine)
    with sessions() as seed, seed.begin():
        seed.add(event_row(event_id="old",
                           occurred_at=parse_rfc3339("2026-07-01T00:00:00Z"),
                           aggregate_version=1))
        seed.add(event_row(event_id="new",
                           occurred_at=parse_rfc3339("2026-08-01T00:00:00Z"),
                           aggregate_version=2))

    outcome = apply_database_contract(
        engine,
        {
            "operation_id": "op-retention-rowcheck",
            "operation_spec_id": "OP-DB-CONTRACT-001",
            "idempotency_key": "idem-retention-rowcheck",
            "actor_type": "service",
            "evidence_source_type": "migration-runner",
            "input": {
                "schema_version": "dal.operation-input/1.0",
                "target": {
                    "entity_id": "fixture-entity",
                    "entity_type": "database",
                    "state": "migrated",
                    "version": 7,
                },
                "action_sequence": [
                    {"command": "apply_retention", "cutoff": "2026-07-10T00:00:00Z"}
                ],
                "authoritative_facts": {},
                "injected_results": [],
            },
        },
    )
    assert outcome.receipt.code is ReceiptCode.APPLIED

    with sessions() as session:
        remaining = set(session.scalars(select(Event.event_id)))
        tombstones = list(session.scalars(select(RetentionTombstone.sealed_id)))
    assert remaining == {"new"}, "the row past the cutoff must be gone"
    assert len(tombstones) == 1, "exactly one deletion, exactly one tombstone"


def test_cas_conflict_across_two_real_sessions(tmp_path: Path) -> None:
    """A losing CAS must lose against another session's *committed* write.

    The frozen `cas_conflict` fixture supplies a stale expected version
    directly, which a single session can detect on its own. That is not the
    failure this obligation exists to prevent. Here session B commits first and
    session A -- which read the row before that commit -- must still be refused:
    `Session.get()` would hand A its own cached copy and let the update through,
    so the pre-CAS read has to reach the database's current state
    (CLAUDE.md §5.2).
    """
    from personal_agent_dal.receipt import ReceiptCode
    from personal_agent_dal.storage import db
    from personal_agent_dal.storage.engine import (
        create_database_engine,
        session_factory,
    )
    from personal_agent_dal.storage.models import Feature
    from personal_agent_dal.storage.operations import update_feature_state
    from tests.dal.factories import feature_row

    database = tmp_path / "dal.db"
    engine = create_database_engine(database)
    db.upgrade(engine)
    sessions = session_factory(engine)
    with sessions() as seed, seed.begin():
        seed.add(feature_row(feature_id="feat-race", version=1))

    # Session A observes version 1 and holds that observation.
    with sessions() as reader:
        observed = reader.scalars(
            Feature.__table__.select().where(
                Feature.__table__.c.feature_id == "feat-race"
            )
        ).first()
        assert observed is not None

    # Session B commits first, moving the row to version 2.
    winner = update_feature_state(
        engine, feature_id="feat-race", expected_version=1, new_state="planning"
    )
    assert winner.receipt.code is ReceiptCode.APPLIED

    # Session A now writes against the version it observed. It must lose.
    loser = update_feature_state(
        engine, feature_id="feat-race", expected_version=1, new_state="coding"
    )
    assert loser.receipt.code is ReceiptCode.VERSION_CONFLICT
    assert loser.writes == ()

    with sessions() as check:
        row = check.get(Feature, "feat-race")
        assert row is not None
        assert row.version == 2
        assert row.state == "planning"


def test_canary_never_appears_in_the_database_file(tmp_path: Path) -> None:
    """The sealed column's plaintext must not exist anywhere in the file.

    Asserting only that `decrypt(encrypt(x)) == x` would pass just as happily
    if the column also kept a plaintext copy beside the envelope, or if a
    stray index or WAL page retained it. The check that actually proves
    `stored_plaintext_visible: false` is a byte scan of what landed on disk.
    """
    from personal_agent_core.crypto import KeyRing, generate_key
    from personal_agent_dal.storage import db
    from personal_agent_dal.storage.engine import create_database_engine
    from personal_agent_dal.storage.operations import seal_and_reread_event

    database = tmp_path / "dal.db"
    engine = create_database_engine(database)
    db.upgrade(engine)
    keyring = KeyRing([generate_key("keyref://dal/data/v1")], service="dal")

    outcome = seal_and_reread_event(engine, keyring=keyring, plaintext=CANARY)
    assert outcome.roundtrip_matches

    engine.dispose()
    written = b"".join(
        path.read_bytes()
        for path in sorted(tmp_path.iterdir())
        if path.is_file()
    )
    assert CANARY.encode("utf-8") not in written
    assert written != b""


def test_sealed_column_refuses_a_tampered_row(tmp_path: Path) -> None:
    """AAD binding: an envelope moved to another row must not open.

    Without the row binding, a sealed value could be copied from one event to
    another and would still decrypt, which turns encryption at rest into a
    value that travels. This is the test that makes `build_aad`'s `row_id`
    component load-bearing rather than decorative.
    """
    from sqlalchemy import select

    from personal_agent_core.crypto import DecryptionError, KeyRing, generate_key
    from personal_agent_dal.storage import db
    from personal_agent_dal.storage.engine import (
        create_database_engine,
        session_factory,
    )
    from personal_agent_dal.storage.models import Event
    from personal_agent_dal.storage.operations import (
        open_event_payload,
        seal_and_reread_event,
    )
    from tests.dal.factories import event_row

    database = tmp_path / "dal.db"
    engine = create_database_engine(database)
    db.upgrade(engine)
    keyring = KeyRing([generate_key("keyref://dal/data/v1")], service="dal")

    outcome = seal_and_reread_event(engine, keyring=keyring, plaintext=CANARY)
    sessions = session_factory(engine)
    with sessions() as session, session.begin():
        original = session.scalars(
            select(Event).where(Event.event_id == outcome.event_id)
        ).one()
        stolen = event_row(event_id="evt-thief")
        stolen.encrypted_payload = original.encrypted_payload
        session.add(stolen)

    with sessions() as session:
        thief = session.scalars(
            select(Event).where(Event.event_id == "evt-thief")
        ).one()
        with pytest.raises(DecryptionError):
            open_event_payload(thief, keyring=keyring)


def test_audit_rows_cannot_be_edited_or_deleted(tmp_path: Path) -> None:
    """Append-only is enforced by the database, not by the code's good manners.

    Every write path in this service is supposed to leave an audit row. That
    guarantee is worth nothing if a later bug -- or a stray `DELETE` in a
    maintenance script -- can quietly remove one, so the table refuses both.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import DatabaseError

    from personal_agent_dal.storage import db
    from personal_agent_dal.storage.engine import create_database_engine
    from personal_agent_dal.storage.operations import update_feature_state
    from personal_agent_dal.storage.engine import session_factory
    from tests.dal.factories import feature_row

    database = tmp_path / "dal.db"
    engine = create_database_engine(database)
    db.upgrade(engine)
    sessions = session_factory(engine)
    with sessions() as seed, seed.begin():
        seed.add(feature_row(feature_id="feat-audit", version=1))
    update_feature_state(
        engine, feature_id="feat-audit", expected_version=1, new_state="planning"
    )

    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM audit_events")
        ).scalar_one()
        assert count >= 1
        with pytest.raises(DatabaseError):
            connection.execute(
                text("UPDATE audit_events SET event_type = 'forged'")
            )
        with pytest.raises(DatabaseError):
            connection.execute(text("DELETE FROM audit_events"))


def test_terminal_and_irreversible_states_are_refused_by_the_database(
    tmp_path: Path,
) -> None:
    """§2.2's two irreversible invariants, enforced as triggers (decision D3).

    `merged` and `deployed` are external facts that already happened, and
    `completed`/`cancelled` are terminal. A service-layer guard is the right
    first line, but it is one refactor away from being bypassed, and the cost
    of being wrong here is a database that claims an external fact was undone.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import DatabaseError

    from personal_agent_dal.storage import db
    from personal_agent_dal.storage.engine import (
        create_database_engine,
        session_factory,
    )
    from tests.dal.factories import feature_row

    database = tmp_path / "dal.db"
    engine = create_database_engine(database)
    db.upgrade(engine)
    sessions = session_factory(engine)
    with sessions() as seed, seed.begin():
        seed.add(feature_row(feature_id="feat-merged", version=1, state="merged"))
        seed.add(feature_row(feature_id="feat-done", version=1, state="completed"))

    with engine.connect() as connection, connection.begin():
        with pytest.raises(DatabaseError):
            connection.execute(
                text(
                    "UPDATE features SET state = 'cancelled', version = 2 "
                    "WHERE feature_id = 'feat-merged'"
                )
            )
    with engine.connect() as connection, connection.begin():
        with pytest.raises(DatabaseError):
            connection.execute(
                text(
                    "UPDATE features SET state = 'planning', version = 2 "
                    "WHERE feature_id = 'feat-done'"
                )
            )


def test_downgrade_returns_the_database_to_base_and_upgrade_replays(
    tmp_path: Path,
) -> None:
    """`0001` is reversible, and reversing it is not a one-way door.

    `db.py` states that every revision must have a working downgrade before any
    forward change ships. A downgrade that is never exercised is a claim, not a
    recovery path, and the moment it is needed is the worst moment to discover
    it drops the wrong tables.
    """
    from sqlalchemy import inspect

    from personal_agent_dal.storage import db
    from personal_agent_dal.storage.engine import create_database_engine

    engine = create_database_engine(tmp_path / "dal.db")
    db.upgrade(engine)
    migrated = sorted(inspect(engine).get_table_names())
    assert "features" in migrated and "audit_events" in migrated

    db.downgrade(engine, "base")
    assert sorted(inspect(engine).get_table_names()) == ["alembic_version"]

    db.upgrade(engine)
    assert sorted(inspect(engine).get_table_names()) == migrated


def test_0003_downgrade_handles_existing_decision_event(tmp_path: Path) -> None:
    """A used Dock database can return to 0002 without CHECK failure."""

    from sqlalchemy import inspect, text

    from personal_agent_dal.storage import db
    from personal_agent_dal.storage.engine import create_database_engine

    engine = create_database_engine(tmp_path / "dal-used-dock.db")
    db.upgrade(engine)
    with engine.connect() as connection, connection.begin():
        connection.execute(
            text(
                "INSERT INTO operation_events "
                "(operation_event_id, event_type, operation_id, occurred_at, detail) "
                "VALUES ('dock-event', 'decision.created', 'dock-op', "
                "'2026-08-14T00:00:00.000000Z', '{}')"
            )
        )

    db.downgrade(engine, "0002")
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM operation_events WHERE event_type = 'decision.created'")
        ).scalar_one() == 0
    assert "root_id" not in {
        column["name"] for column in inspect(engine).get_columns("decisions")
    }

    db.upgrade(engine)
    assert "root_id" in {
        column["name"] for column in inspect(engine).get_columns("decisions")
    }


def test_migrated_and_metadata_built_schemas_agree(tmp_path: Path) -> None:
    """`create_all` and the migration must produce the same database.

    Tests and fixtures build the schema from metadata; deployment builds it
    from the migration. If those two drift, every test runs against a database
    that does not exist in production — including the triggers, which Alembic
    autogenerate cannot see and which are therefore the most likely thing to be
    present in one and missing from the other. CHECK constraints are compared
    too (round-4 finding R4-3): a CHECK present in only one build is an
    invisible contract difference.
    """
    import re

    from sqlalchemy import inspect, text

    from personal_agent_dal.storage import db
    from personal_agent_dal.storage.engine import create_all, create_database_engine

    #: Historical tables whose migration-era CHECK names predate a strict
    #: name parity rule (double `ck_` prefixes, missing outer parentheses).
    #: Their SQL semantics agree; renaming their constraints is a separate
    #: decision, not this test's. New tables get exact CHECK parity.
    _CHECK_PARITY_EXEMPT = frozenset(
        {
            "commit_capabilities",
            "notification_batches",
            "notification_deliveries",
            "operation_events",
            "worker_checkpoints",
            "worker_enrollments",
            "worker_jobs",
            "worker_result_receipts",
        }
    )

    def _sql(text_value: str) -> str:
        return re.sub(r"\s+", "", text_value).lower()

    def shape(engine) -> dict[str, object]:  # noqa: ANN001
        inspector = inspect(engine)
        with engine.connect() as connection:
            triggers = sorted(
                row[0]
                for row in connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
                )
            )
        tables = [
            t for t in inspector.get_table_names() if t != "alembic_version"
        ]
        checks = {
            table: {
                (c["name"], _sql(c["sqltext"]))
                for c in inspector.get_check_constraints(table)
            }
            for table in tables
            if table not in _CHECK_PARITY_EXEMPT
        }
        return {
            "tables": sorted(tables),
            "columns": {
                table: sorted(c["name"] for c in inspector.get_columns(table))
                for table in sorted(tables)
            },
            "checks": checks,
            "triggers": triggers,
        }

    migrated = create_database_engine(tmp_path / "migrated.db")
    db.upgrade(migrated)

    built = create_database_engine(tmp_path / "built.db")
    create_all(built)

    assert shape(migrated) == shape(built)
