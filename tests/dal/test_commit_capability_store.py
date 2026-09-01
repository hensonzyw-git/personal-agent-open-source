"""Adversarial tests for the persistent commit capability (R09-A3, CAS).

The pure gates are reviewed-closed; this suite attacks the durable half:

- (A) issue persistence: a go verdict writes the row + intent + audit +
  outbox in one transaction; every DAL-004 §5 field lands; a malformed
  binding or a missing/malformed ``repository_id`` writes nothing;
- (B) issue replay: the same facts return the original receipt and do not
  duplicate any row; the same key with different content raises
  ``IDEMPOTENCY_CONFLICT``;
- (C) consume CAS: a clean consume moves ``issued -> consumed`` and lands
  the consuming identity and instant atomically with the intent/audit/
  outbox; a stale row refuses with zero writes; a tampered presentation on
  a live row lands the block evidence rows and the labelled violations;
- (D) consume replay: the winner's identity replayed returns the original
  receipt without new effect rows; a different identity is stale, not a
  replay;
- (E) the race: two concurrent consumes of one live capability produce
  exactly one winner and one stale loser, and never two intents;
- (F) atomicity: a loser leaves no partial write behind;
- (G) the persisted row re-judges through the pure gate unchanged: the row
  is the binding, byte for byte (no CAS field loosened a gate binding).

The presentation's ``idempotency_key`` is the *issue* key (the pure gate
binds it); the consume's replay fence is therefore the ``consumed_by``
effect/command identity, not that key.
"""

from __future__ import annotations

import json
import threading

import pytest
from sqlalchemy import select, text

from personal_agent_core.manifest import canonical_json
from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.machine import commit_capability_store as store
from personal_agent_dal.machine.commit_capability import (
    issue_commit_capability,
)
from personal_agent_dal.receipt import ReceiptCode
from personal_agent_dal.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_agent_dal.storage.machine_models import (
    CommitCapability,
    ExternalEffect,
)
from personal_agent_dal.storage.models import AuditEvent, OutboxEvent
from tests.dal.test_commit_capability import (
    BASE,
    EPOCH,
    LATER,
    NOW,
    PLAN_HASH,
    RESULT,
    TRAILERS,
    _binding,
    _consume_facts,
    _issue_facts,
)

REPO = "dal-pilot-sandbox"
WORKER = "git-executor:op-0001"
WORKER_2 = "git-executor:op-0002"

CONSUME_INTENT_KEY = f"{WORKER}:intent"


@pytest.fixture()
def engine(tmp_path):
    engine = create_database_engine(tmp_path / "dal.sqlite")
    create_all(engine)
    return engine


def _issued(engine) -> None:
    store.issue_commit_capability_row(engine, _issue_facts(), repository_id=REPO)


def _row(engine) -> CommitCapability:
    sessions = session_factory(engine)
    with sessions() as session:
        return session.scalars(
            select(CommitCapability).where(
                CommitCapability.capability_id == "cap-0001"
            )
        ).one()


def _count(engine, model) -> int:
    sessions = session_factory(engine)
    with sessions() as session:
        return len(session.scalars(select(model)).all())


def _intents(engine, key: str) -> list:
    sessions = session_factory(engine)
    with sessions() as session:
        return session.scalars(
            select(ExternalEffect).where(
                ExternalEffect.remote_idempotency_key == key
            )
        ).all()


# --- (A) issue persistence ---------------------------------------------------


def test_issue_persists_row_with_all_dal004_fields(engine):
    outcome = store.issue_commit_capability_row(
        engine, _issue_facts(), repository_id=REPO
    )
    assert outcome.replayed is False
    assert outcome.capability_id == "cap-0001"
    row = _row(engine)
    assert row.schema_version == store.SCHEMA_VERSION
    assert row.state == "issued"
    assert row.action == "commit_candidate"
    assert row.repository_id == REPO
    assert row.task_id == TRAILERS["Task-Id"]
    assert row.base_sha == BASE
    assert row.result_sha == RESULT
    assert row.artifact_or_diff_sha256 == PLAN_HASH
    assert row.policy_version == store.POLICY_VERSION
    assert row.lease_epoch == 7
    assert row.capability_epoch == EPOCH
    assert row.expires_at == LATER
    assert row.max_uses == 1
    assert row.uses_consumed == 0
    assert row.consumed_by is None
    assert row.consumed_at is None
    assert row.revoked_at is None
    assert row.allowed_paths_json == canonical_json(_binding()["allowed_paths"])
    assert row.trailers_json == canonical_json(_binding()["trailers"])
    # The atomic companions exist in the same commit.
    assert _intents(engine, "issue-key-0001:intent") != []
    assert _count(engine, AuditEvent) >= 1
    assert _count(engine, OutboxEvent) >= 1


def test_issue_with_malformed_binding_writes_nothing(engine):
    binding = _binding()
    binding["max_uses"] = 2  # trusted drift: max_uses is frozen to 1
    with pytest.raises(DalError):
        store.issue_commit_capability_row(
            engine, _issue_facts(binding=binding), repository_id=REPO
        )
    assert _count(engine, CommitCapability) == 0
    assert _count(engine, ExternalEffect) == 0
    assert _count(engine, AuditEvent) == 0
    assert _count(engine, OutboxEvent) == 0


def test_issue_with_empty_repository_id_writes_nothing(engine):
    with pytest.raises(DalError):
        store.issue_commit_capability_row(engine, _issue_facts(), repository_id="")
    assert _count(engine, CommitCapability) == 0


def test_issue_with_non_native_repository_id_writes_nothing(engine):
    class RepoId(str):
        pass

    with pytest.raises(DalError):
        store.issue_commit_capability_row(
            engine, _issue_facts(), repository_id=RepoId(REPO)
        )
    assert _count(engine, CommitCapability) == 0


# --- (B) issue replay ---------------------------------------------------------


def test_issue_replay_returns_original_without_new_rows(engine):
    facts = _issue_facts()
    first = store.issue_commit_capability_row(engine, facts, repository_id=REPO)
    before = (
        _count(engine, CommitCapability),
        _count(engine, ExternalEffect),
        _count(engine, AuditEvent),
        _count(engine, OutboxEvent),
    )
    second = store.issue_commit_capability_row(engine, facts, repository_id=REPO)
    assert second.replayed is True
    assert second.capability_id == first.capability_id
    assert second.receipt.code == ReceiptCode.APPLIED
    after = (
        _count(engine, CommitCapability),
        _count(engine, ExternalEffect),
        _count(engine, AuditEvent),
        _count(engine, OutboxEvent),
    )
    assert before == after


def test_issue_key_reuse_with_different_content_conflicts(engine):
    store.issue_commit_capability_row(engine, _issue_facts(), repository_id=REPO)
    drifted = _issue_facts()
    drifted["binding"]["base_sha"] = "0" * 40
    with pytest.raises(DalError) as excinfo:
        store.issue_commit_capability_row(engine, drifted, repository_id=REPO)
    assert excinfo.value.code == DalErrorCode.IDEMPOTENCY_CONFLICT


# --- (C) consume CAS ----------------------------------------------------------


def test_clean_consume_moves_row_to_consumed_with_identity(engine):
    _issued(engine)
    outcome = store.consume_commit_capability_row(
        engine, _consume_facts(), consumed_by=WORKER
    )
    assert outcome.verdict == "go"
    assert outcome.replayed is False
    row = _row(engine)
    assert row.state == "consumed"
    assert row.state_version == 2
    assert row.uses_consumed == 1
    assert row.consumed_by == WORKER
    assert row.consumed_at == NOW
    assert len(_intents(engine, CONSUME_INTENT_KEY)) == 1


def test_stale_consume_writes_nothing(engine):
    _issued(engine)
    facts = _consume_facts(now=LATER + 10)  # past expiry
    outcome = store.consume_commit_capability_row(engine, facts, consumed_by=WORKER)
    assert outcome.verdict == "stale"
    assert outcome.receipt.code == ReceiptCode.CAPABILITY_STALE
    row = _row(engine)
    assert row.state == "issued"
    assert row.uses_consumed == 0
    assert _count(engine, ExternalEffect) == 1  # the issue intent only


def test_tampered_presentation_lands_block_evidence(engine):
    _issued(engine)
    facts = _consume_facts()
    facts["presented"]["base_sha"] = "1" * 40
    outcome = store.consume_commit_capability_row(engine, facts, consumed_by=WORKER)
    assert outcome.verdict == "blocked"
    assert any("base_sha" in v for v in outcome.violations)
    row = _row(engine)
    # The block verdict does not consume the capability: the engine's block
    # transition is the controller's separate step.
    assert row.state == "issued"
    assert len(_intents(engine, f"{WORKER}:block")) == 1


def test_stale_consume_after_epoch_bump(engine):
    _issued(engine)
    facts = _consume_facts(current_epoch=EPOCH + 1)
    outcome = store.consume_commit_capability_row(engine, facts, consumed_by=WORKER)
    assert outcome.verdict == "stale"


def test_consume_of_unknown_capability_row_is_invalid(engine):
    with pytest.raises(DalError):
        store.consume_commit_capability_row(
            engine, _consume_facts(), consumed_by=WORKER
        )


def test_consume_with_empty_identity_is_invalid(engine):
    _issued(engine)
    with pytest.raises(DalError):
        store.consume_commit_capability_row(
            engine, _consume_facts(), consumed_by=""
        )


# --- (D) consume replay --------------------------------------------------------


def test_consume_replay_returns_original_without_new_rows(engine):
    _issued(engine)
    facts = _consume_facts()
    first = store.consume_commit_capability_row(engine, facts, consumed_by=WORKER)
    assert first.verdict == "go"
    before = (
        _count(engine, ExternalEffect),
        _count(engine, AuditEvent),
        _count(engine, OutboxEvent),
    )
    second = store.consume_commit_capability_row(engine, facts, consumed_by=WORKER)
    assert second.replayed is True
    assert second.verdict == "go"
    assert second.receipt.code == ReceiptCode.APPLIED
    after = (
        _count(engine, ExternalEffect),
        _count(engine, AuditEvent),
        _count(engine, OutboxEvent),
    )
    assert before == after


def test_second_identity_on_consumed_row_is_stale_not_replay(engine):
    _issued(engine)
    facts = _consume_facts()
    store.consume_commit_capability_row(engine, facts, consumed_by=WORKER)
    outcome = store.consume_commit_capability_row(engine, facts, consumed_by=WORKER_2)
    assert outcome.replayed is False
    assert outcome.verdict == "stale"


# --- (E) the race ---------------------------------------------------------------


def test_concurrent_consumes_produce_one_effect_and_no_double_intent(engine):
    """The CAS admits exactly one consume per capability. Two concurrent
    calls with the *same* effect/command identity are one command racing
    with its own retry: the loser re-reads the winner's committed row and
    returns the original receipt as a replay. Distinct identities (below)
    get the stale refusal instead. Either way the row is consumed once,
    one intent exists, and `uses_consumed` never exceeds 1."""
    _issued(engine)
    facts = _consume_facts()
    outcomes: list = []
    barrier = threading.Barrier(2)

    def _run():
        barrier.wait()
        outcomes.append(
            store.consume_commit_capability_row(engine, facts, consumed_by=WORKER)
        )

    threads = [threading.Thread(target=_run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    fresh = [o for o in outcomes if not o.replayed]
    replays = [o for o in outcomes if o.replayed]
    assert len(fresh) == 1
    assert fresh[0].verdict == "go"
    assert len(replays) == 1
    assert replays[0].verdict == "go"
    row = _row(engine)
    assert row.state == "consumed"
    assert row.state_version == 2
    assert row.uses_consumed == 1
    assert len(_intents(engine, CONSUME_INTENT_KEY)) == 1


def test_concurrent_distinct_identities_produce_one_winner_one_stale(engine):
    """Two different commands racing for one capability: exactly one go,
    one stale, one intent row."""
    _issued(engine)
    facts = _consume_facts()
    outcomes: list = []
    barrier = threading.Barrier(2)

    def _run(identity: str):
        barrier.wait()
        outcomes.append(
            store.consume_commit_capability_row(engine, facts, consumed_by=identity)
        )

    threads = [
        threading.Thread(target=_run, args=(identity,))
        for identity in (WORKER, WORKER_2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    verdicts = sorted(o.verdict for o in outcomes)
    assert verdicts == ["go", "stale"]
    row = _row(engine)
    assert row.state == "consumed"
    assert row.uses_consumed == 1
    # Exactly one intent, and it belongs to the winner (the row's recorded
    # identity), not deterministically to the thread that started first.
    assert _intents(engine, f"{row.consumed_by}:intent") != []
    loser_identity = WORKER_2 if row.consumed_by == WORKER else WORKER
    assert _intents(engine, f"{loser_identity}:intent") == []


# --- (F) atomicity ---------------------------------------------------------------


def test_loser_leaves_no_partial_write(engine):
    _issued(engine)
    facts = _consume_facts()
    assert (
        store.consume_commit_capability_row(engine, facts, consumed_by=WORKER).verdict
        == "go"
    )
    assert (
        store.consume_commit_capability_row(
            engine, facts, consumed_by=WORKER_2
        ).verdict
        == "stale"
    )
    assert _intents(engine, f"{WORKER_2}:intent") == []
    assert _intents(engine, f"{WORKER_2}:block") == []


# --- (H) the migration path -------------------------------------------------------


def test_migration_round_trip(tmp_path):
    """Every revision must have a working downgrade (storage/db.py docstring)."""
    from pathlib import Path

    from personal_agent_dal.storage import db

    database = tmp_path / "dal.sqlite"
    engine = create_database_engine(database)
    db.upgrade(engine)
    sessions = session_factory(engine)
    with sessions() as session:
        # The table the migration creates is the table the model declares.
        columns = {
            row[1]
            for row in session.execute(text("PRAGMA table_info(commit_capabilities)"))
        }
    assert "state_version" in columns
    assert "artifact_or_diff_sha256" in columns
    assert "policy_version" in columns
    db.downgrade(engine, "0006")
    with sessions() as session:
        names = {
            row[0]
            for row in session.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
    assert "commit_capabilities" not in names
    db.upgrade(engine)
    with sessions() as session:
        names = {
            row[0]
            for row in session.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
    assert "commit_capabilities" in names
    Path(database).unlink(missing_ok=True)


# --- (G) the row re-judges through the pure gate unchanged -----------------------


def test_row_binding_rejudges_as_go_through_the_pure_gate(engine):
    """The persisted row, re-read into the pure gate's facts, classifies the
    same way the issue binding did: the row is the binding, byte for byte."""
    _issued(engine)
    row = _row(engine)
    binding = {
        "capability_id": row.capability_id,
        "approval_id": row.approval_id,
        "lease_epoch": row.lease_epoch,
        "base_sha": row.base_sha,
        "result_sha": row.result_sha,
        "allowed_paths": json.loads(row.allowed_paths_json),
        "trailers": json.loads(row.trailers_json),
        "idempotency_key": row.issue_idempotency_key,
        "expires_at": row.expires_at,
        "max_uses": row.max_uses,
        "capability_epoch": row.capability_epoch,
    }
    # Re-judging the persisted binding through the pure gate must still be
    # a go verdict — the persistence layer did not drift from the gate.
    issue_commit_capability(_issue_facts(binding=binding))
