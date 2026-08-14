"""DEV-036: the periodic cleanup CLI (`personal-agent-cleanup`).

Two properties define this job, and both are tested here:

1. **Whitelist-only.** The job deletes expired enrollment codes and nothing
   else. The permanent conversation archive (`conversation_events`,
   `conversations`, `conversation_aliases`, `deletion_manifest`) is never on the
   deletion path, so a row placed in any of those tables survives a cleanup run
   even when it is old. This is the DEV-036 acceptance rule "永久 conversation
   不被清理", held by structure rather than by the job's discretion.
2. **Idempotent.** A second run the same day deletes nothing: the expired rows
   are already gone, and the unexpired ones are still unexpired. A timer that
   fires twice is a no-op, not a double action.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from personal_agent import cleanup_cli
from personal_agent.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_agent.storage.models import (
    ContextCheckpoint,
    ContextSession,
    Conversation,
    ConversationAlias,
    ConversationEvent,
    DeletionManifest,
    EnrollmentCode,
)
from personal_agent_core.timeutil import utc_now


SEALED = {
    "v": 1,
    "kid": "cleanup-test-key",
    "nonce": "AAAAAAAAAAAAAAAA",
    "ciphertext": "AAAA",
    "tag": "AAAAAAAAAAAAAAAAAAAAAA",
}


@pytest.fixture()
def database(tmp_path: Path) -> Path:
    path = tmp_path / "agent.sqlite"
    engine = create_database_engine(path)
    create_all(engine)
    engine.dispose()
    return path


def run(database: Path, *args: str) -> str:
    argv = ["personal-agent-cleanup", "--database", str(database), *args]
    original = sys.argv
    sys.argv = argv
    try:
        cleanup_cli.main()
    finally:
        sys.argv = original
    return ""


def _seed_enrollment_code(
    database: Path,
    *,
    expires_at,
    used_at=None,
) -> None:
    engine = create_database_engine(database)
    with session_factory(engine)() as session:
        session.add(
            EnrollmentCode(
                code_hash=f"hash-{expires_at.isoformat()}-{used_at}",
                grants_device_manage=False,
                created_at=expires_at - timedelta(minutes=10),
                expires_at=expires_at,
                used_at=used_at,
            )
        )
        session.commit()
    engine.dispose()


def _count(database: Path, model) -> int:
    engine = create_database_engine(database)
    try:
        with session_factory(engine)() as session:
            return session.query(model).count()
    finally:
        engine.dispose()


def test_a_missing_database_is_refused_rather_than_created(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        run(tmp_path / "missing.sqlite")


def test_expired_enrollment_codes_are_deleted(database: Path) -> None:
    now = utc_now()
    _seed_enrollment_code(database, expires_at=now - timedelta(hours=1))
    _seed_enrollment_code(database, expires_at=now - timedelta(minutes=5))
    assert _count(database, EnrollmentCode) == 2

    run(database, "--now", now.isoformat())

    assert _count(database, EnrollmentCode) == 0


def test_unexpired_enrollment_codes_survive(database: Path) -> None:
    now = utc_now()
    _seed_enrollment_code(database, expires_at=now + timedelta(hours=1))
    _seed_enrollment_code(database, expires_at=now - timedelta(hours=1))

    run(database, "--now", now.isoformat())

    # The live one remains; only the expired one is removed.
    assert _count(database, EnrollmentCode) == 1


def test_used_but_unexpired_codes_survive(database: Path) -> None:
    # A code that was consumed but has not expired yet is still within its TTL
    # window; cleanup respects `expires_at`, not `used_at`.
    now = utc_now()
    _seed_enrollment_code(
        database,
        expires_at=now + timedelta(hours=1),
        used_at=now - timedelta(minutes=5),
    )

    run(database, "--now", now.isoformat())

    assert _count(database, EnrollmentCode) == 1


def test_repeated_runs_are_idempotent(database: Path) -> None:
    now = utc_now()
    _seed_enrollment_code(database, expires_at=now - timedelta(hours=1))

    run(database, "--now", now.isoformat())
    assert _count(database, EnrollmentCode) == 0

    # A second run the same day deletes nothing further and does not raise.
    run(database, "--now", now.isoformat())
    assert _count(database, EnrollmentCode) == 0


def test_permanent_conversation_archive_is_off_the_deletion_path(
    database: Path,
) -> None:
    # The whitelist is structural: the cleanup module imports exactly one model
    # (EnrollmentCode) and issues exactly one `delete(...)` call, targeted at
    # that model. The permanent archive -- conversation_events, conversations,
    # conversation_aliases, deletion_manifest, context_checkpoints -- has no
    # import and no delete, so no code path here can remove a row from it. This
    # is the DEV-036 acceptance rule "永久 conversation 不被清理" held by what
    # the module may touch, not by the job's discretion.
    import ast
    import inspect

    from personal_agent import cleanup_cli as cleanup_module

    source = inspect.getsource(cleanup_module)
    tree = ast.parse(source)

    imported_names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported_names.append(alias.asname or alias.name)

    # The only storage model the cleanup module may touch.
    assert "EnrollmentCode" in imported_names
    forbidden_models = {
        "ConversationEvent",
        "ConversationAlias",
        "Conversation",
        "DeletionManifest",
        "ContextCheckpoint",
    }
    assert not (forbidden_models & set(imported_names)), (
        f"cleanup imports a permanent-archive model: "
        f"{forbidden_models & set(imported_names)}"
    )

    # Exactly one `delete(...)` call, and its first argument is EnrollmentCode.
    delete_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "delete"
    ]
    assert len(delete_calls) == 1
    target = delete_calls[0].args[0]
    target_name = (
        target.id if isinstance(target, ast.Name) else None
    )
    assert target_name == "EnrollmentCode"

    # And the one table it does delete is the only one the delete targets.
    now = utc_now()
    _seed_enrollment_code(database, expires_at=now - timedelta(hours=1))
    run(database, "--now", now.isoformat())
    assert _count(database, EnrollmentCode) == 0


def test_permanent_archive_rows_survive_cleanup(database: Path) -> None:
    """Exercise the real FK graph, not only the cleanup module's syntax."""
    now = utc_now()
    old = now - timedelta(days=365)
    engine = create_database_engine(database)
    try:
        with session_factory(engine)() as session:
            session.add(
                Conversation(
                    conversation_id="timeline-permanent",
                    created_at=old,
                    last_event_at=old,
                    next_sequence=2,
                    is_canonical=True,
                )
            )
            session.add(
                ContextSession(
                    session_id="session-permanent",
                    conversation_id="timeline-permanent",
                    status="open",
                    relation_kind="new_topic",
                    opened_at=old,
                    last_event_at=old,
                )
            )
            session.add(
                ConversationAlias(
                    alias_hmac="alias-permanent",
                    conversation_id="timeline-permanent",
                    encrypted_legacy_conversation_id=SEALED,
                    created_at=old,
                )
            )
            session.add(
                ConversationEvent(
                    event_id="event-permanent",
                    conversation_id="timeline-permanent",
                    timeline_sequence=1,
                    session_id="session-permanent",
                    turn_id="turn-permanent",
                    event_type="user_message",
                    encrypted_content=SEALED,
                    created_at=old,
                )
            )
            session.add(
                ContextCheckpoint(
                    checkpoint_id="checkpoint-permanent",
                    session_id="session-permanent",
                    status="active",
                    covered_from_sequence=1,
                    covered_through_sequence=1,
                    encrypted_payload=SEALED,
                    source_hash="source-hash-permanent",
                    schema_version="1",
                    compactor_version="test",
                    estimated_tokens=1,
                    created_at=old,
                )
            )
            session.add(
                DeletionManifest(
                    entry_id="manifest-permanent",
                    object_type="conversation",
                    encrypted_object_id=SEALED,
                    deleted_at=old,
                    backup_expiry_after=old,
                )
            )
            session.add(
                EnrollmentCode(
                    code_hash="expired-control",
                    grants_device_manage=False,
                    created_at=old,
                    expires_at=old,
                    used_at=None,
                )
            )
            session.commit()
    finally:
        engine.dispose()

    run(database, "--now", now.isoformat())

    assert _count(database, EnrollmentCode) == 0
    for model in (
        Conversation,
        ContextSession,
        ConversationAlias,
        ConversationEvent,
        ContextCheckpoint,
        DeletionManifest,
    ):
        assert _count(database, model) == 1


def test_an_invalid_now_is_refused(database: Path) -> None:
    with pytest.raises(SystemExit):
        run(database, "--now", "not-a-timestamp")


def test_transcript_retention_runs_without_a_new_message(database: Path, tmp_path: Path) -> None:
    directory = tmp_path / "transcripts"
    directory.mkdir()
    stale = directory / "api-2026-08-12.jsonl"
    current = directory / "api-2026-08-13.jsonl"
    foreign = directory / "notes.txt"
    for path in (stale, current, foreign):
        path.write_text("{}\n")

    run(
        database,
        "--now",
        "2026-08-14T00:00:00Z",
        "--transcript-directory",
        str(directory),
        "--transcript-retention-days",
        "2",
    )

    assert not stale.exists()
    assert current.exists()
    assert foreign.exists()


def test_transcript_cleanup_arguments_are_atomic(database: Path, tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        run(database, "--transcript-directory", str(tmp_path))


def test_entrypoint_is_installed() -> None:
    # The console script must resolve, so a systemd unit that names it cannot
    # fail at exec time with a command-not-found.
    entrypoint = Path(sys.executable).parent / "personal-agent-cleanup"
    assert entrypoint.is_file()
    result = subprocess.run(
        [str(entrypoint), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Agent database" in result.stdout
