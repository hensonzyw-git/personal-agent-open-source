"""`DEV-035`: verify a restored Agent database before reads open.

Design 10.5's restore drill verifies both restored databases: integrity,
schema version, foreign-key and idempotency/receipt reference integrity, a
fixed-sample AEAD decrypt, deletion-manifest replay, and (in the shell wrapper)
dedicated read-only service starts. This module owns the pure library calls so
they can run in a Mac restore drill without a deployed service and be tested
offline against fixture databases.

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
from personal_agent.storage.engine import (
    create_database_engine,
    create_read_only_database_engine,
    session_factory,
)


def check_media_bundle_media(database: Path, keyring, bundle: Path, *, materialized=False) -> dict[str, Any]:
    """Authenticate every restored ready/bound media ciphertext before reads open."""
    from personal_agent.media.container import ContainerError, SealRecord, read_container
    from personal_agent.media.lifecycle import ATTEMPT_TABLE, SEAL_RECORD_COLUMN

    engine = create_read_only_database_engine(database)
    checked = 0
    try:
        with session_factory(engine)() as session:
            rows = session.execute(
                text(
                    "SELECT m.media_id, m.current_attempt_number, a.attempt_id, "
                    "a.encrypted_seal_record FROM media_objects m "
                    "LEFT JOIN media_attempts a ON a.media_id = m.media_id "
                    "AND a.attempt_number = m.current_attempt_number "
                    "WHERE m.state IN ('ready', 'bound') ORDER BY m.media_id"
                )
            ).all()
        for media_id, attempt, attempt_id, envelope in rows:
            if not isinstance(media_id, str) or not isinstance(attempt, int) or envelope is None:
                return {"name": "media_bundle", "ok": False, "detail": "ready media row lacks sealed attempt"}
            try:
                raw = keyring.decrypt(
                    json.loads(envelope) if isinstance(envelope, str) else envelope,
                    table=ATTEMPT_TABLE, column=SEAL_RECORD_COLUMN, row_id=attempt_id,
                )
                seal = SealRecord.from_dict(json.loads(raw.decode("utf-8")))
                path = (Path(bundle) / "final" / media_id[:2] / f"{media_id}.bin"
                        if materialized else Path(bundle) / "media" / f"{media_id}.bin")
                # Fully consume the generator: authentication of a chunk is not
                # proof that later chunks exist or that the whole-stream hash matches.
                for _ in read_container(
                    path, seal, keyring=keyring, media_id=media_id,
                    attempt_number=attempt, role="chat_image",
                ):
                    pass
                checked += 1
            except Exception as exc:
                return {
                    "name": "media_bundle", "ok": False,
                    "detail": f"media ciphertext verification failed for {media_id}: {type(exc).__name__}",
                }
        return {"name": "media_bundle", "ok": True, "detail": f"authenticated_media={checked}"}
    finally:
        engine.dispose()


def check_integrity(
    database: Path, *, name: str = "integrity_check"
) -> dict[str, Any]:
    engine = create_read_only_database_engine(database)
    try:
        with engine.connect() as conn:
            result = conn.execute(text("PRAGMA integrity_check")).scalar_one()
    finally:
        engine.dispose()
    return {
        "name": name,
        "ok": result == "ok",
        "detail": f"integrity_check={result}",
    }


def _check_schema_version(
    database: Path,
    *,
    migrations_path: Path,
    name: str,
    expected: str = "head",
) -> dict[str, Any]:
    """The restored DB must be at the current schema revision.

    A snapshot taken before a migration restores at the old revision; running
    it against the current service would either fail or silently use the old
    shape. ``expected='head'`` resolves to the package's current head.
    """
    engine = create_read_only_database_engine(database)
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
            cfg.set_main_option("script_location", str(migrations_path))
            target = ScriptDirectory.from_config(cfg).get_current_head()
        return {
            "name": name,
            "ok": current_rev == target,
            "detail": f"restored={current_rev} expected={target}",
        }
    finally:
        engine.dispose()


def check_schema_version(database: Path, expected: str = "head") -> dict[str, Any]:
    return _check_schema_version(
        database,
        migrations_path=db.MIGRATIONS_PATH,
        name="schema_version",
        expected=expected,
    )


def check_finance_schema_version(
    database: Path, expected: str = "head"
) -> dict[str, Any]:
    from personal_data_mcp.storage import db as finance_db

    return _check_schema_version(
        database,
        migrations_path=finance_db.MIGRATIONS_PATH,
        name="finance_schema_version",
        expected=expected,
    )


def check_dal_schema_version(database: Path, expected: str = "head") -> dict[str, Any]:
    from personal_agent_dal.storage import db as dal_db

    return _check_schema_version(
        database,
        migrations_path=dal_db.MIGRATIONS_PATH,
        name="dal_schema_version",
        expected=expected,
    )


def check_agent_reference_integrity(database: Path) -> dict[str, Any]:
    """Verify the Agent-side request/operation and foreign-key lineage.

    SQLite's ``integrity_check`` does not validate foreign keys. A backup can be
    page-perfect while an operation points at a request that is no longer there,
    so the restore gate checks both the full FK graph and the explicit
    idempotency invariants the service depends on.
    """
    engine = create_read_only_database_engine(database)
    try:
        with engine.connect() as conn:
            foreign_key_violations = len(
                conn.execute(text("PRAGMA foreign_key_check")).all()
            )
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
        ok = foreign_key_violations == 0 and orphans == 0 and dupes == 0
        return {
            "name": "reference_integrity",
            "ok": ok,
            "detail": (
                f"foreign_key_violations={foreign_key_violations} "
                f"orphan_operations={orphans} duplicate_idempotency_keys={dupes}"
            ),
        }
    finally:
        engine.dispose()


# Historical public name retained for callers outside the drill package.
check_audit_chain_intact = check_agent_reference_integrity


def check_finance_reference_integrity(database: Path) -> dict[str, Any]:
    """Verify Finance idempotency rows and the receipts that justify success."""
    engine = create_read_only_database_engine(database)
    try:
        with engine.connect() as conn:
            foreign_key_violations = len(
                conn.execute(text("PRAGMA foreign_key_check")).all()
            )
            orphan_receipts = conn.execute(
                text(
                    "SELECT count(*) FROM external_receipts r "
                    "LEFT JOIN tool_executions e "
                    "ON e.idempotency_key = r.idempotency_key "
                    "WHERE e.idempotency_key IS NULL"
                )
            ).scalar_one()
            invalid_successes = conn.execute(
                text(
                    "SELECT count(*) FROM ("
                    " SELECT e.idempotency_key"
                    " FROM tool_executions e"
                    " LEFT JOIN external_receipts r"
                    "   ON r.idempotency_key = e.idempotency_key"
                    " WHERE e.state = 'succeeded'"
                    " GROUP BY e.idempotency_key, e.tool"
                    " HAVING count(r.receipt_id) <> 1"
                    "    OR sum(CASE"
                    "         WHEN r.verified_at IS NOT NULL"
                    "          AND length(trim(r.record_id)) > 0"
                    "          AND r.table_kind = CASE e.tool"
                    "            WHEN 'finance.log_expense' THEN 'expense'"
                    "            WHEN 'finance.log_income' THEN 'income'"
                    "            WHEN 'finance.update_family_fund' THEN 'family_fund'"
                    "            ELSE '__unsupported__' END"
                    "         THEN 1 ELSE 0 END) <> 1"
                    ")"
                )
            ).scalar_one()
        ok = (
            foreign_key_violations == 0
            and orphan_receipts == 0
            and invalid_successes == 0
        )
        return {
            "name": "finance_reference_integrity",
            "ok": ok,
            "detail": (
                f"foreign_key_violations={foreign_key_violations} "
                f"orphan_receipts={orphan_receipts} "
                f"invalid_succeeded_receipts={invalid_successes}"
            ),
        }
    finally:
        engine.dispose()


def check_dal_reference_integrity(database: Path) -> dict[str, Any]:
    """Verify the restored DAL machine tables' reference integrity (R09-B).

    The receipt graph is the DAL's audit spine: every state change is one
    ``transition_receipts`` row, and ``aggregate_id`` plus
    ``external_effects.owner_aggregate_id`` are polymorphic references the
    schema cannot enforce with a foreign key. A restore that loses an effect
    or feature row while keeping its receipts therefore passes
    ``integrity_check`` and the FK graph while being unable to resume or
    reconcile anything. The gate reads aggregate receipts against the
    aggregates themselves, effects against their owners, and the idempotency
    uniqueness the machine's replay fence depends on.
    """
    engine = create_read_only_database_engine(database)
    try:
        with engine.connect() as conn:
            foreign_key_violations = len(
                conn.execute(text("PRAGMA foreign_key_check")).all()
            )
            # Feature/recovery-case receipts whose aggregate row is gone.
            orphan_aggregate_receipts = conn.execute(
                text(
                    "SELECT count(*) FROM ("
                    " SELECT r.receipt_id FROM transition_receipts r"
                    " LEFT JOIN features f"
                    "   ON r.aggregate_type = 'feature'"
                    "  AND r.aggregate_id = f.feature_id"
                    " LEFT JOIN recovery_cases c"
                    "   ON r.aggregate_type = 'recovery_case'"
                    "  AND r.aggregate_id = c.recovery_case_id"
                    " WHERE r.aggregate_type IN ('feature', 'recovery_case')"
                    "   AND f.feature_id IS NULL AND c.recovery_case_id IS NULL"
                    ")"
                )
            ).scalar_one()
            # Effect receipts whose effect row is gone.
            orphan_effect_receipts = conn.execute(
                text(
                    "SELECT count(*) FROM transition_receipts r "
                    "LEFT JOIN external_effects e "
                    "ON r.aggregate_type = 'external_effect' "
                    "AND r.aggregate_id = e.effect_id "
                    "WHERE r.aggregate_type = 'external_effect' "
                    "AND e.effect_id IS NULL"
                )
            ).scalar_one()
            # Effects whose owner feature/recovery-case row is gone.
            orphan_effect_owners = conn.execute(
                text(
                    "SELECT count(*) FROM ("
                    " SELECT e.effect_id FROM external_effects e"
                    " LEFT JOIN features f"
                    "   ON e.owner_aggregate_type = 'feature'"
                    "  AND e.owner_aggregate_id = f.feature_id"
                    " LEFT JOIN recovery_cases c"
                    "   ON e.owner_aggregate_type = 'recovery_case'"
                    "  AND e.owner_aggregate_id = c.recovery_case_id"
                    " WHERE f.feature_id IS NULL AND c.recovery_case_id IS NULL"
                    ")"
                )
            ).scalar_one()
            # The machine's replay fence relies on unique idempotency keys.
            duplicate_idempotency_keys = conn.execute(
                text(
                    "SELECT count(*) FROM (SELECT idempotency_key "
                    "FROM transition_receipts "
                    "GROUP BY idempotency_key HAVING count(*) > 1)"
                )
            ).scalar_one()
        ok = (
            foreign_key_violations == 0
            and orphan_aggregate_receipts == 0
            and orphan_effect_receipts == 0
            and orphan_effect_owners == 0
            and duplicate_idempotency_keys == 0
        )
        return {
            "name": "dal_reference_integrity",
            "ok": ok,
            "detail": (
                f"foreign_key_violations={foreign_key_violations} "
                f"orphan_aggregate_receipts={orphan_aggregate_receipts} "
                f"orphan_effect_receipts={orphan_effect_receipts} "
                f"orphan_effect_owners={orphan_effect_owners} "
                f"duplicate_idempotency_keys={duplicate_idempotency_keys}"
            ),
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

    engine = create_read_only_database_engine(database)
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


def restore_media_files(database, keyring, bundle):
    """Materialize restored ciphertext, finish replayed deletions, then verify.

    Only the restored copy is changed; the source backup repository is untouched.
    """
    import os
    import stat
    from sqlalchemy import select
    from personal_agent.backup.media_bundle import _copy_ciphertext
    from personal_agent.media.store import MediaStore
    from personal_agent.media.locking import ensure_lock_files
    from personal_agent.media.deletion import mark_media_deleting, reap_media_object
    from personal_agent.storage.models import MediaObject
    from personal_agent_core.timeutil import utc_now
    root = Path(bundle).parent / ("restored-media-" + Path(bundle).name)
    engine = create_database_engine(database)
    try:
        for directory in (root, *(root / n for n in ("staging", "final", "quarantine", "locks"))):
            if directory.is_symlink():
                raise ValueError("restore media directory is a symlink")
            directory.mkdir(mode=0o700, exist_ok=True)
        ensure_lock_files(root)
        store = MediaStore(root, keyring)
        with session_factory(engine)() as session:
            rows = session.execute(select(MediaObject.media_id, MediaObject.state)).all()
            known = {media_id for media_id, _ in rows}
            source_dir = Path(bundle) / "media"
            if source_dir.exists():
                if source_dir.is_symlink():
                    raise ValueError("media input directory is a symlink")
                for source in source_dir.iterdir():
                    if source.suffix != ".bin" or source.stem not in known:
                        raise ValueError("unowned ciphertext in restored input")
            for media_id, state in rows:
                source = source_dir / f"{media_id}.bin"
                if state in ("ready", "bound") and (source.exists() or source.is_symlink()):
                    destination = store.final_path(media_id)
                    if not destination.exists():
                        _copy_ciphertext(source, destination)
                if state not in ("ready", "bound"):
                    if state == "deleted":
                        # Re-entry after the tombstone commit must also remove
                        # any leftover materialized copy.
                        from personal_agent.media.locking import media_locks
                        session.rollback()
                        with media_locks(root, [media_id], blocking=False):
                            store.discard_final(media_id)
                    mark_media_deleting(session, media_id=media_id, keyring=keyring, now=utc_now())
                    session.commit()
                    outcome = reap_media_object(session, store=store, media_id=media_id, now=utc_now())
                    if outcome.value == "deferred":
                        raise ValueError("restored deletion was deferred")
                    if source.exists() or source.is_symlink():
                        if not stat.S_ISREG(source.lstat().st_mode):
                            raise ValueError("foreign restored ciphertext")
                        source.unlink()
                        fd = os.open(source_dir, os.O_RDONLY)
                        try:
                            os.fsync(fd)
                        finally:
                            os.close(fd)
            session.commit()
        return check_media_bundle_media(database, keyring, root, materialized=True)
    except Exception as exc:
        return {"name": "media_restore", "ok": False,
                "detail": f"media restore refused: {type(exc).__name__}"}
    finally:
        engine.dispose()


def run_all(
    database: Path,
    keyring,
    *,
    finance_database: Path | None = None,
    dal_database: Path | None = None,
    manifest_entries: list[dict[str, Any]] | None = None,
    aead_sample_entry_id: str | None = None,
    media_bundle: Path | None = None,
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
        check_agent_reference_integrity(database),
    ]
    if finance_database is not None:
        results.extend(
            [
                check_integrity(
                    finance_database, name="finance_integrity_check"
                ),
                check_finance_schema_version(finance_database),
                check_finance_reference_integrity(finance_database),
            ]
        )
    if dal_database is not None:
        results.extend(
            [
                check_integrity(dal_database, name="dal_integrity_check"),
                check_dal_schema_version(dal_database),
                check_dal_reference_integrity(dal_database),
            ]
        )
    if aead_sample_entry_id is not None:
        results.append(check_aead_sample(database, keyring, entry_id=aead_sample_entry_id))
    if any(not result["ok"] for result in results):
        return results
    if media_bundle is None:
        # "Optional" is the text-only compatibility path, not permission to
        # skip ciphertext checks for a populated media database. Check before
        # replay so deletion cannot erase the very evidence requiring a bundle.
        engine = create_read_only_database_engine(database)
        try:
            with engine.connect() as connection:
                has_media = bool(connection.execute(text(
                    "SELECT EXISTS (SELECT 1 FROM media_objects)"
                )).scalar_one())
            if has_media:
                results.append({
                    "name": "media_restore", "ok": False,
                    "detail": "media bundle required for a database with media lifecycle records",
                })
                return results
        finally:
            engine.dispose()
    if manifest_entries is not None:
        results.append(
            check_replay_deletion_manifest(database, keyring, manifest_entries)
        )
    if any(not result["ok"] for result in results):
        return results
    if media_bundle is not None:
        if manifest_entries is None:
            results.append({"name": "media_restore", "ok": False,
                            "detail": "latest independent deletion manifest required"})
        else:
            results.append(restore_media_files(database, keyring, media_bundle))
    return results
