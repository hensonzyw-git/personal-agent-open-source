"""`DEV-035`: the library-side restore checks, pinned offline.

``scripts/restore_drill.sh`` orchestrates restic + a read-only service start;
this test pins the six checks that are pure library calls against a restored
fixture database, including the failure shapes: a snapshot at the wrong schema
revision, a broken operation reference, an AEAD sample that will not open
under the wrong key, and a manifest replay that must run after the sample.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from personal_agent.backup.deletion_manifest import (
    MANIFEST_COLUMN,
    MANIFEST_TABLE,
    export_manifest,
)
from personal_agent.backup.restore_verify import (
    check_aead_sample,
    check_audit_chain_intact,
    check_dal_reference_integrity,
    check_dal_schema_version,
    check_finance_reference_integrity,
    check_finance_schema_version,
    check_integrity,
    check_replay_deletion_manifest,
    check_schema_version,
    run_all,
)
from personal_agent.storage import db
from personal_agent.storage.engine import create_database_engine, session_factory
from personal_agent_core.crypto import KeyRing, generate_key
from personal_agent_core.timeutil import to_rfc3339
from personal_data_mcp.storage import db as finance_db


NOW = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)


def _seed(path: Path, keyring: KeyRing, *, conversations: list[str]) -> None:
    engine = create_database_engine(path)
    db.upgrade(engine, "head")
    with engine.begin() as conn:
        for cid in conversations:
            conn.execute(
                text(
                    "INSERT INTO conversations (conversation_id, created_at, "
                    "next_sequence) VALUES (:cid, :now, 2)"
                ),
                {"cid": cid, "now": to_rfc3339(NOW)},
            )
            conn.execute(
                text(
                    "INSERT INTO context_sessions (session_id, conversation_id, "
                    "status, relation_kind, opened_at) VALUES "
                    "(:sid, :cid, 'open', 'new_topic', :now)"
                ),
                {"sid": f"sess-{cid}", "cid": cid, "now": to_rfc3339(NOW)},
            )
            sealed = keyring.encrypt(
                json.dumps({"text": cid}).encode("utf-8"),
                table="conversation_events",
                column="encrypted_content",
                row_id=f"evt-{cid}",
            )
            conn.execute(
                text(
                    "INSERT INTO conversation_events (event_id, conversation_id, "
                    "session_id, turn_id, event_type, encrypted_content, "
                    "timeline_sequence, created_at) VALUES "
                    "(:eid, :cid, :sid, :turn, 'user_message', :content, 1, :now)"
                ),
                {
                    "eid": f"evt-{cid}",
                    "cid": cid,
                    "sid": f"sess-{cid}",
                    "turn": f"turn-{cid}",
                    "content": json.dumps(sealed, ensure_ascii=False, sort_keys=True),
                    "now": to_rfc3339(NOW),
                },
            )
    return engine


def _add_manifest(engine, keyring: KeyRing, *, entry_id: str, object_id: str) -> None:
    sealed = keyring.encrypt(
        object_id.encode("utf-8"),
        table=MANIFEST_TABLE,
        column=MANIFEST_COLUMN,
        row_id=entry_id,
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO deletion_manifest (entry_id, object_type, "
                "encrypted_object_id, deleted_at) VALUES "
                "(:eid, 'conversation', :sealed, :now)"
            ),
            {
                "eid": entry_id,
                "sealed": json.dumps(sealed, ensure_ascii=False, sort_keys=True),
                "now": to_rfc3339(NOW),
            },
        )


def _seed_finance(path: Path, *, with_receipt: bool = True) -> None:
    engine = create_database_engine(path)
    finance_db.upgrade(engine, "head")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tool_executions "
                "(idempotency_key, tool, request_fingerprint, state, "
                " state_version, client_token, created_at, updated_at, "
                " submitted_at, completed_at) VALUES "
                "('idem-1', 'finance.log_expense', 'fp', 'succeeded', "
                " 5, 'token-1', :now, :now, :now, :now)"
            ),
            {"now": to_rfc3339(NOW)},
        )
        if with_receipt:
            conn.execute(
                text(
                    "INSERT INTO external_receipts "
                    "(receipt_id, idempotency_key, source_system, table_kind, "
                    " record_id, created_at, verified_at) VALUES "
                    "('receipt-1', 'idem-1', 'feishu_bitable', 'expense', "
                    " 'record-1', :now, :now)"
                ),
                {"now": to_rfc3339(NOW)},
            )
    engine.dispose()


@pytest.fixture()
def keyring() -> KeyRing:
    return KeyRing(
        [generate_key("agent-data-2026", state="active")],
        service="personal-agent-api",
    )


def test_integrity_passes_on_a_fresh_restore(tmp_path: Path, keyring: KeyRing) -> None:
    path = tmp_path / "agent.sqlite"
    _seed(path, keyring, conversations=["c1"])
    result = check_integrity(path)
    assert result["ok"], result["detail"]


def test_schema_version_matches_head(tmp_path: Path, keyring: KeyRing) -> None:
    path = tmp_path / "agent.sqlite"
    _seed(path, keyring, conversations=["c1"])
    result = check_schema_version(path)
    assert result["ok"], result["detail"]


def test_schema_version_fails_on_a_stale_snapshot(tmp_path: Path, keyring: KeyRing) -> None:
    path = tmp_path / "agent.sqlite"
    engine = create_database_engine(path)
    db.upgrade(engine, "0002_notification_outbox_constraints")
    engine.dispose()
    result = check_schema_version(path)
    assert not result["ok"]
    assert "0002" in result["detail"] or "expected" in result["detail"]


def test_reference_integrity_passes(tmp_path: Path, keyring: KeyRing) -> None:
    path = tmp_path / "agent.sqlite"
    _seed(path, keyring, conversations=["c1"])
    assert check_audit_chain_intact(path)["ok"]


def test_reference_integrity_catches_an_orphan_operation(
    tmp_path: Path, keyring: KeyRing
) -> None:
    path = tmp_path / "agent.sqlite"
    _seed(path, keyring, conversations=["c1"])
    engine = create_database_engine(path)
    # Insert an operation whose request_id resolves to nothing -- the shape of
    # a restore that lost an api_requests row. The schema enforces the FK on
    # normal connections, so this uses a raw connection with foreign keys off,
    # modelling a DB file that was repaired or partially restored by a tool
    # that did not enforce constraints.
    import sqlite3

    raw = sqlite3.connect(str(path))
    try:
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute(
            "INSERT INTO operations (operation_id, request_id, trace_id, "
            "idempotency_key, state, state_version, cancel_requested, "
            "client_detached, created_at, updated_at) VALUES "
            "('op-orphan', 'req-missing', 'tr', 'key-orphan', 'succeeded', "
            "1, 0, 0, ?, ?)",
            (to_rfc3339(NOW), to_rfc3339(NOW)),
        )
        raw.commit()
    finally:
        raw.close()
    engine.dispose()
    result = check_audit_chain_intact(path)
    assert not result["ok"]
    assert "orphan_operations=1" in result["detail"]


def test_finance_schema_and_receipt_integrity_pass(tmp_path: Path) -> None:
    path = tmp_path / "finance.sqlite"
    _seed_finance(path)
    assert check_finance_schema_version(path)["ok"]
    assert check_finance_reference_integrity(path)["ok"]


def test_finance_reference_integrity_catches_success_without_receipt(
    tmp_path: Path,
) -> None:
    path = tmp_path / "finance.sqlite"
    _seed_finance(path, with_receipt=False)

    result = check_finance_reference_integrity(path)

    assert not result["ok"]
    assert "invalid_succeeded_receipts=1" in result["detail"]


def dal_write_connection(path: Path):
    """A raw sqlite3 connection with FKs off, for seeding broken shapes.

    The schema enforces its constraints on normal connections; these tests
    model a DB file that was partially restored or repaired by a tool that
    did not enforce them (the same technique the agent-side orphan test
    uses).
    """
    import sqlite3
    from contextlib import contextmanager

    @contextmanager
    def _conn():
        raw = sqlite3.connect(str(path))
        try:
            raw.execute("PRAGMA foreign_keys=OFF")
            yield raw
            raw.commit()
        finally:
            raw.close()

    return _conn()


def _seed_dal(path: Path) -> None:
    """A minimal but real DAL database: one feature, one effect, one receipt.

    Uses the DAL package's own engine/migrations so the check under test runs
    against the schema the deployed DAL service actually writes, and the
    ORM factories so the rows match what the machine writes (not a hand-
    guessed column list).
    """
    from personal_agent_core.timeutil import utc_now
    from personal_agent_dal.machine.engine import RECEIPT_SCHEMAS
    from personal_agent_dal.storage import db as dal_db
    from personal_agent_dal.storage.engine import (
        create_database_engine as dal_create_database_engine,
    )
    from personal_agent_dal.storage.engine import session_factory as dal_session_factory
    from personal_agent_dal.storage.machine_models import TransitionReceipt
    from tests.dal.factories import EMPTY_SHA256, external_effect_row, feature_row

    engine = dal_create_database_engine(path)
    dal_db.upgrade(engine, "head")
    with dal_session_factory(engine)() as session, session.begin():
        session.add(feature_row(feature_id="feature-dal-1", version=1))
        session.add(
            external_effect_row(
                effect_id="effect-dal-1", owner_id="feature-dal-1",
                version=1, state="confirmed_completed",
            )
        )
        session.add(
            TransitionReceipt(
                receipt_id="receipt-dal-1",
                idempotency_key="idem-dal-1",
                aggregate_type="external_effect",
                aggregate_id="effect-dal-1",
                aggregate_version=1,
                spec_id="EE-CONFIRM-COMPLETED",
                command_type="record_effect_confirmed_completed",
                from_state="dispatch_started",
                to_state="confirmed_completed",
                receipt_code="APPLIED",
                receipt_schema_version=RECEIPT_SCHEMAS["external_effect"],
                request_payload_sha256=EMPTY_SHA256,
                event_id=None,
                recorded_at=utc_now(),
            )
        )
    engine.dispose()


def test_dal_schema_and_reference_integrity_pass(tmp_path: Path) -> None:
    path = tmp_path / "dal.sqlite"
    _seed_dal(path)
    assert check_dal_schema_version(path)["ok"]
    assert check_dal_reference_integrity(path)["ok"]


def test_dal_reference_integrity_catches_a_receipt_without_its_effect(
    tmp_path: Path,
) -> None:
    """A receipt naming a missing external effect must fail the gate.

    ``transition_receipts.aggregate_id`` is a polymorphic reference with no
    DB-level foreign key, so a restore that lost effect rows while keeping
    receipts is otherwise invisible: ``integrity_check`` and the FK graph
    both pass. The receipt graph is the audit spine — this is the DAL
    analogue of the agent-side orphan-operation check.
    """
    path = tmp_path / "dal.sqlite"
    _seed_dal(path)
    with dal_write_connection(path) as conn:
        conn.execute(
            "INSERT INTO transition_receipts (receipt_id, idempotency_key, "
            "aggregate_type, aggregate_id, aggregate_version, spec_id, "
            "command_type, from_state, to_state, receipt_code, "
            "receipt_schema_version, request_payload_sha256, recorded_at) "
            "VALUES ('receipt-dal-2', 'idem-dal-2', 'external_effect', "
            "'effect-missing', 1, 'EE-CONFIRM-COMPLETED', "
            "'record_effect_confirmed_completed', 'dispatch_started', "
            "'confirmed_completed', 'APPLIED', "
            "(SELECT receipt_schema_version FROM transition_receipts "
            " WHERE receipt_id = 'receipt-dal-1'), "
            "(SELECT request_payload_sha256 FROM transition_receipts "
            " WHERE receipt_id = 'receipt-dal-1'), ?)",
            (to_rfc3339(NOW),),
        )

    result = check_dal_reference_integrity(path)

    assert not result["ok"]
    assert "orphan_effect_receipts=1" in result["detail"]


def test_dal_reference_integrity_catches_an_effect_without_its_owner(
    tmp_path: Path,
) -> None:
    """An effect row whose owner feature is gone must fail the gate.

    ``external_effects.owner_aggregate_id`` is likewise polymorphic: the
    reconciliation and resume compositions read the owner row to derive
    every guard fact, so a restore that keeps effects but loses features
    cannot be trusted to resume from.
    """
    path = tmp_path / "dal.sqlite"
    _seed_dal(path)
    with dal_write_connection(path) as conn:
        conn.execute("DELETE FROM features WHERE feature_id = 'feature-dal-1'")

    result = check_dal_reference_integrity(path)

    assert not result["ok"]
    assert "orphan_effect_owners=1" in result["detail"]


def test_aead_sample_opens_under_the_right_key(tmp_path: Path, keyring: KeyRing) -> None:
    path = tmp_path / "agent.sqlite"
    engine = _seed(path, keyring, conversations=["c1"])
    _add_manifest(engine, keyring, entry_id="drill-sample", object_id="c1")
    result = check_aead_sample(path, keyring, entry_id="drill-sample")
    assert result["ok"], result["detail"]


def test_aead_sample_fails_under_the_wrong_key(tmp_path: Path, keyring: KeyRing) -> None:
    path = tmp_path / "agent.sqlite"
    engine = _seed(path, keyring, conversations=["c1"])
    _add_manifest(engine, keyring, entry_id="drill-sample", object_id="c1")
    wrong = KeyRing(
        [generate_key("agent-data-2026", state="active")],
        service="personal-agent-api",
    )
    result = check_aead_sample(path, wrong, entry_id="drill-sample")
    assert not result["ok"]
    assert "decrypt failed" in result["detail"]


def test_replay_deletes_a_restored_conversation(tmp_path: Path, keyring: KeyRing) -> None:
    path = tmp_path / "agent.sqlite"
    engine = _seed(path, keyring, conversations=["c1", "c2"])
    _add_manifest(engine, keyring, entry_id="m1", object_id="c1")
    with session_factory(engine)() as session:
        manifest = export_manifest(session)

    result = check_replay_deletion_manifest(path, keyring, manifest)
    assert result["ok"], result["detail"]
    assert "applied=1" in result["detail"]


def test_run_all_orders_aead_before_replay(tmp_path: Path, keyring: KeyRing) -> None:
    # The AEAD sample and the replay both touch the same manifest entry. If
    # replay ran first it would delete the entry's target but the entry itself
    # stays, so the sample would still decrypt -- the ordering matters for the
    # *replay* not racing the *sample* against a half-applied state. Pin that
    # run_all returns a result for each, in order.
    path = tmp_path / "agent.sqlite"
    finance_path = tmp_path / "finance.sqlite"
    engine = _seed(path, keyring, conversations=["c1"])
    _seed_finance(finance_path)
    _add_manifest(engine, keyring, entry_id="drill-sample", object_id="c1")
    with session_factory(engine)() as session:
        manifest = export_manifest(session)

    results = run_all(
        path,
        keyring,
        finance_database=finance_path,
        manifest_entries=manifest,
        aead_sample_entry_id="drill-sample",
    )
    names = [r["name"] for r in results]
    assert "finance_reference_integrity" in names
    assert names.index("aead_sample") < names.index("deletion_manifest_replay")
    assert all(r["ok"] for r in results), [r for r in results if not r["ok"]]


def test_run_all_verifies_the_dal_database(tmp_path: Path, keyring: KeyRing) -> None:
    """run_all gains a dal_database gate; without it a DAL restore is unverified.

    R09-B added ``dal.latest.sqlite`` to the backup set; a drill that verifies
    the two original databases and silently ignores the third closes "可备份"
    without closing "可恢复". Passing the restored DAL path must produce the
    schema + reference checks in the results.
    """
    path = tmp_path / "agent.sqlite"
    finance_path = tmp_path / "finance.sqlite"
    dal_path = tmp_path / "dal.sqlite"
    engine = _seed(path, keyring, conversations=["c1"])
    _seed_finance(finance_path)
    _seed_dal(dal_path)
    _add_manifest(engine, keyring, entry_id="drill-sample", object_id="c1")
    with session_factory(engine)() as session:
        manifest = export_manifest(session)
    engine.dispose()

    results = run_all(
        path,
        keyring,
        finance_database=finance_path,
        dal_database=dal_path,
        manifest_entries=manifest,
        aead_sample_entry_id="drill-sample",
    )
    names = [r["name"] for r in results]
    assert "dal_schema_version" in names
    assert "dal_reference_integrity" in names
    assert all(r["ok"] for r in results), [r for r in results if not r["ok"]]


def test_restore_drill_requires_dal_snapshot() -> None:
    """The latest-snapshot drill cannot downgrade DAL to an optional check."""
    script = (
        Path(__file__).resolve().parents[2] / "scripts" / "restore_drill.sh"
    ).read_text(encoding="utf-8")
    assert 'for f in "$API_DB" "$MCP_DB" "$DAL_DB" "$MANIFEST"' in script
    assert '--dal-database "$DAL_DB"' in script
    assert "DAL_ARGS" not in script
    assert "DAL restore gates are SKIPPED" not in script
