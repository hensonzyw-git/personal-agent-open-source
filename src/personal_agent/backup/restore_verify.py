"""`DEV-035`: verify a restored Agent database before reads open.

Design 10.5's restore drill runs six checks on a restored database:
integrity, schema version, idempotency/receipt reference integrity, a fixed
sample AEAD decrypt, deletion-manifest replay, and (in the shell wrapper) a
read-only service start. This module owns the ones that are pure library
calls against a database path, so they can run in a Mac restore drill
without the deployed service present and be unit-tested offline against a
fixture database.

Each check returns a result dict with ``name``, ``ok`` and ``detail``; the
drill CLI fails closed if any is not ``ok``. The detail never includes a
secret: it carries counts, version strings and pass/fail, the way the
observe CLI does.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import text

from personal_agent.backup.deletion_manifest import replay_manifest
from personal_agent.storage import db
from personal_agent.storage.engine import create_database_engine, session_factory


def check_integrity(database: Path) -> dict[str, Any]:
    engine = create_database_engine(database)
    try:
        with engine.connect() as conn:
            result = conn.execute(text("PRAGMA integrity_check")).scalar_one()
    finally:
        engine.dispose()
    return {
        "name": "integrity_check",
        "ok": result == "ok",
        "detail": f"integrity_check={result}",
    }


def check_schema_version(database: Path, expected: str = "head") -> dict[str, Any]:
    """The restored DB must be at the current schema revision.

    A snapshot taken before a migration restores at the old revision; running
    it against the current service would either fail or silently use the old
    shape. ``expected='head'`` resolves to the package's current head.
    """
    engine = create_database_engine(database)
    try:
        from alembic.runtime.migration import MigrationContext

        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            current_rev = ctx.get_current_revision()
        target = expected
        if expected == "head":
            # Resolve head through the same migrations path the upgrade CLI
            # uses, so the comparison is against what the deployed service
            # would run, not a hard-coded string.
            from alembic.config import Config
            from alembic.script import ScriptDirectory

            cfg = Config()
            cfg.set_main_option("script_location", str(db.MIGRATIONS_PATH))
            target = ScriptDirectory.from_config(cfg).get_current_head()
        return {
            "name": "schema_version",
            "ok": current_rev == target,
            "detail": f"restored={current_rev} expected={target}",
        }
    finally:
        engine.dispose()


def check_audit_chain_intact(database: Path) -> dict[str, Any]:
    """The Finance-side audit chain lives in the MCP database, not here.

    For the Agent database the reference-integrity check that matters is the
    operation/receipt lineage: every terminal operation that claims success
    must reference an api_request, and the idempotency keys must be unique.
    This runs a bounded query rather than importing the full recovery layer,
    so it works on a cold restored file with no service running.
    """
    engine = create_database_engine(database)
    try:
        with engine.connect() as conn:
            # Operations whose request_id does not resolve to an api_request
            # row: a restored DB with a broken foreign reference cannot be
            # trusted to report what happened.
            orphans = conn.execute(
                text(
                    "SELECT count(*) FROM operations o "
                    "LEFT JOIN api_requests r ON o.request_id = r.request_id "
                    "WHERE r.request_id IS NULL"
                )
            ).scalar_one()
            # Duplicate idempotency keys would let a replay collide.
            dupes = conn.execute(
                text(
                    "SELECT count(*) FROM (SELECT idempotency_key FROM operations "
                    "GROUP BY idempotency_key HAVING count(*) > 1)"
                )
            ).scalar_one()
        ok = orphans == 0 and dupes == 0
        return {
            "name": "reference_integrity",
            "ok": ok,
            "detail": f"orphan_operations={orphans} duplicate_idempotency_keys={dupes}",
        }
    finally:
        engine.dispose()


def check_aead_sample(
    database: Path, keyring, *, entry_id: str
) -> dict[str, Any]:
    """Decrypt one sealed deletion-manifest entry end to end.

    Proves the restored DB's sealed data is openable under the data key, with
    the AAD (service/table/column/row_id) binding intact. A restore that
    injected the wrong key, or a manifest copied from another service, fails
    closed here rather than at replay time.
    """
    from personal_agent.backup.deletion_manifest import MANIFEST_COLUMN, MANIFEST_TABLE

    engine = create_database_engine(database)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT encrypted_object_id FROM deletion_manifest "
                    "WHERE entry_id = :eid"
                ),
                {"eid": entry_id},
            ).fetchone()
        if row is None:
            return {
                "name": "aead_sample",
                "ok": False,
                "detail": f"no deletion_manifest entry with entry_id={entry_id}",
            }
        envelope = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        from personal_agent_core.crypto import DecryptionError

        try:
            keyring.decrypt(
                envelope,
                table=MANIFEST_TABLE,
                column=MANIFEST_COLUMN,
                row_id=entry_id,
            )
        except DecryptionError as exc:
            return {
                "name": "aead_sample",
                "ok": False,
                "detail": f"decrypt failed (AAD/key mismatch): {exc}",
            }
        return {
            "name": "aead_sample",
            "ok": True,
            "detail": f"entry_id={entry_id} opened under active key",
        }
    finally:
        engine.dispose()


def check_replay_deletion_manifest(
    database: Path, keyring, manifest_entries: list[dict[str, Any]]
) -> dict[str, Any]:
    """Replay the deletion manifest so deleted data does not come back.

    Runs against the restored DB in one transaction; any failure leaves the
    transaction for the caller to roll back, so a partial replay cannot half-
    open the database.
    """
    engine = create_database_engine(database)
    try:
        with session_factory(engine)() as session:
            try:
                result = replay_manifest(session, manifest_entries, keyring)
            except Exception as exc:
                session.rollback()
                return {
                    "name": "deletion_manifest_replay",
                    "ok": False,
                    "detail": f"replay failed: {exc}",
                }
        return {
            "name": "deletion_manifest_replay",
            "ok": True,
            "detail": (
                f"applied={result['applied']} already_absent={result['already_absent']}"
            ),
        }
    finally:
        engine.dispose()


def run_all(
    database: Path,
    keyring,
    *,
    manifest_entries: list[dict[str, Any]] | None = None,
    aead_sample_entry_id: str | None = None,
) -> list[dict[str, Any]]:
    """Run the library-side restore checks in order and return all results.

    Order matters for one pair: the AEAD sample decrypt must precede the
    replay, because replay *consumes* the manifest entries by deleting their
    targets -- running replay first would leave nothing to verify the AEAD
    binding against. Leak-before-correctness (§5.2) does not apply here
    because there is no secret-scan step.
    """
    results = [
        check_integrity(database),
        check_schema_version(database),
        check_audit_chain_intact(database),
    ]
    if aead_sample_entry_id is not None:
        results.append(check_aead_sample(database, keyring, entry_id=aead_sample_entry_id))
    if manifest_entries is not None:
        results.append(
            check_replay_deletion_manifest(database, keyring, manifest_entries)
        )
    return results
