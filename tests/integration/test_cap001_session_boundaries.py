"""CAP-001 slice D: the Session boundary decision order.

Covers failure set F-D1..F-D12 (`docs/CAP-001失败集_v0.1.md` §4). Transaction
and idle-input semantics live in `test_cap001_session_manager.py`; this file is
about *which* Session a message lands in.

The classifier is a non-deterministic boundary, so the fakes here are
deliberately hostile: free text, extra fields, unknown reasons, several answers,
exceptions. Every one must land on "continue the current Session" (design §6.1
step 9), because splitting a topic in half on a bad guess costs the user real
context while merging two topics costs only some irrelevance.

F-D14 -- whether a real model actually produces these shapes -- cannot be shown
here and belongs to the live evidence in CAP-001 H.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text as sql
from sqlalchemy.exc import IntegrityError

from personal_agent.api import events
from personal_agent.context.config import default_context_config
from personal_agent.context.session_manager import (
    CLASSIFIER_VERSION,
    ClassifierOutcome,
    CompactSessionState,
    SessionManager,
    detect_explicit_signal,
    parse_classifier_outcome,
)
from personal_agent.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_agent.storage.models import (
    ApiRequest,
    ContextSession,
    Conversation,
    Device,
    Operation,
)
from personal_agent_core.crypto import KeyRing, generate_key
from personal_agent_core.errors import AppError


NOW = datetime(2026, 7, 27, 3, 0, tzinfo=timezone.utc)
CANONICAL = "tl_canonical"
CONFIG = default_context_config()


@pytest.fixture()
def keyring() -> KeyRing:
    return KeyRing(
        [generate_key("agent-data-2026", state="active")],
        service="personal-agent-api",
    )


@pytest.fixture()
def engine(tmp_path: Path):
    engine = create_database_engine(tmp_path / "agent.sqlite")
    create_all(engine)
    with session_factory(engine)() as session:
        session.add(
            Device(
                device_id="dev-1",
                display_name="iPhone",
                public_key="K",
                device_key_thumbprint="THUMB",
                status="active",
                scopes="[]",
                allowed_tools_version="v1",
                created_at=NOW,
            )
        )
        session.add(
            Conversation(
                conversation_id=CANONICAL,
                created_at=NOW,
                next_sequence=1,
                is_canonical=True,
            )
        )
        session.commit()
    yield engine
    engine.dispose()


@pytest.fixture()
def db(engine):
    with session_factory(engine)() as session:
        yield session


class _Classifier:
    """Returns whatever it was told to, or raises."""

    def __init__(self, answer=None, *, raises: Exception | None = None) -> None:
        self.answer = answer
        self.raises = raises
        self.seen: list = []

    def classify(self, request):
        self.seen.append(request)
        if self.raises is not None:
            raise self.raises
        return self.answer


class _StateProvider:
    """Returns trusted, bounded semantic state rather than archive text."""

    def __init__(self, state: CompactSessionState | None = None) -> None:
        self.state = state or CompactSessionState(
            topic_summary="记录今天的个人支出",
            domain="finance",
            task_state="active",
        )
        self.seen: list[ContextSession] = []

    def compact_state(self, db, *, session: ContextSession) -> CompactSessionState:
        self.seen.append(session)
        return self.state


class _FailingStateProvider:
    def compact_state(self, db, *, session: ContextSession) -> CompactSessionState:
        raise RuntimeError("checkpoint unavailable")


def _manager(classifier=None) -> SessionManager:
    return SessionManager(
        CONFIG,
        classifier=classifier,
        state_provider=_StateProvider() if classifier is not None else None,
    )


def _open_session(db, session_id: str = "ses-1", **kwargs) -> ContextSession:
    row = ContextSession(
        session_id=session_id,
        conversation_id=CANONICAL,
        status="open",
        relation_kind="new_topic",
        opened_at=kwargs.pop("opened_at", NOW),
        last_event_at=kwargs.pop("last_event_at", NOW),
        **kwargs,
    )
    db.add(row)
    db.flush()
    return row


def _closed_session(db, session_id: str, *, closed_at: datetime) -> ContextSession:
    row = ContextSession(
        session_id=session_id,
        conversation_id=CANONICAL,
        status="closed",
        relation_kind="new_topic",
        opened_at=closed_at - timedelta(hours=1),
        closed_at=closed_at,
        last_event_at=closed_at,
    )
    db.add(row)
    db.flush()
    return row


def _waiting_operation(db, keyring: KeyRing, *, session_id: str, state: str) -> None:
    """A parked operation, with the Timeline event that anchored it."""
    db.add(
        ApiRequest(
            request_id="req-1",
            device_id="dev-1",
            client_request_id="client-1",
            request_fingerprint="fp",
            received_at=NOW,
        )
    )
    db.add(
        Operation(
            operation_id="op-1",
            request_id="req-1",
            trace_id="tr",
            idempotency_key="key-1",
            state=state,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    db.flush()
    events.append_event(
        db,
        keyring,
        conversation_id=CANONICAL,
        session_id=session_id,
        turn_id="trn-1",
        event_type=events.USER_MESSAGE,
        content={"text": "午饭 45"},
        operation_id="op-1",
        now=NOW,
    )
    db.flush()


def _completed_finance_operation(db, keyring: KeyRing, *, session_id: str) -> None:
    db.add(
        ApiRequest(
            request_id="req-finance",
            device_id="dev-1",
            client_request_id="client-finance",
            request_fingerprint="fp-finance",
            received_at=NOW,
        )
    )
    db.add(
        Operation(
            operation_id="op-finance",
            request_id="req-finance",
            trace_id="tr-finance",
            idempotency_key="key-finance",
            state="succeeded",
            tool="finance.log_expense",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    db.flush()
    events.append_event(
        db,
        keyring,
        conversation_id=CANONICAL,
        session_id=session_id,
        turn_id="trn-finance",
        event_type=events.OPERATION_RESULT,
        content={"status": "succeeded"},
        operation_id="op-finance",
        now=NOW,
    )
    db.flush()


# -- F-D1 / F-D2: every non-terminal operation pins its Session ------------


def test_eight_hours_always_opens_a_fresh_session(db) -> None:
    _open_session(db, last_event_at=NOW - timedelta(hours=8))

    decision = _manager().select_session(
        db,
        conversation_id=CANONICAL,
        user_text="继续昨天的旅行计划",
        now=NOW,
    )

    assert decision.opened is True
    assert decision.reason == "idle_timeout"


def test_completed_finance_tool_splits_a_plainly_unrelated_request(db, keyring) -> None:
    _open_session(db)
    _completed_finance_operation(db, keyring, session_id="ses-1")

    decision = _manager().select_session(
        db,
        conversation_id=CANONICAL,
        user_text="帮我规划周末爬山",
        now=NOW + timedelta(minutes=3),
    )

    assert decision.opened is True
    assert decision.reason == "completed_tool_unrelated"


def test_completed_finance_tool_keeps_a_finance_followup(db, keyring) -> None:
    _open_session(db)
    _completed_finance_operation(db, keyring, session_id="ses-1")

    decision = _manager().select_session(
        db,
        conversation_id=CANONICAL,
        user_text="把这笔改成 30",
        now=NOW + timedelta(minutes=3),
    )

    assert decision.opened is False
    assert decision.session_id == "ses-1"


@pytest.mark.parametrize(
    "state",
    [
        "accepted",
        "interpreting",
        "waiting_for_clarification",
        "waiting_for_duplicate_decision",
        "dispatching",
        "source_in_progress",
        "verifying",
    ],
)
def test_a_non_terminal_operation_pins_its_session(
    db, keyring: KeyRing, state: str
) -> None:
    _open_session(db, "ses-1")
    _waiting_operation(db, keyring, session_id="ses-1", state=state)
    # Even a classifier insisting on a new topic cannot move an in-flight
    # operation out of the Session where its request was anchored.
    decision = _manager(
        _Classifier(
            {
                "decision": "open_new_session",
                "reason": "task_boundary",
                "confidence_band": "high",
            }
        )
    ).select_session(
        db, conversation_id=CANONICAL, user_text="个人支出", now=NOW
    )
    assert decision.decision == "continue_session"
    assert decision.session_id == "ses-1"
    assert decision.opened is False


def test_an_explicit_reset_cannot_orphan_a_waiting_operation(
    db, keyring: KeyRing
) -> None:
    # The user's own words are outranked here, and only here: a parked
    # duplicate decision answered in a fresh Session loses its candidate set.
    _open_session(db, "ses-1")
    _waiting_operation(
        db, keyring, session_id="ses-1", state="waiting_for_duplicate_decision"
    )
    decision = _manager().select_session(
        db, conversation_id=CANONICAL, user_text="换个话题", now=NOW
    )
    assert decision.session_id == "ses-1"
    assert db.query(ContextSession).count() == 1


def test_a_session_holding_a_waiting_operation_cannot_be_closed(
    db, keyring: KeyRing
) -> None:
    session_row = _open_session(db, "ses-1")
    _waiting_operation(
        db, keyring, session_id="ses-1", state="waiting_for_clarification"
    )
    with pytest.raises(AppError):
        _manager().close_session(db, session_row, now=NOW)


@pytest.mark.parametrize(
    "state",
    ["accepted", "interpreting", "dispatching", "source_in_progress", "verifying"],
)
def test_a_session_holding_any_non_terminal_operation_cannot_be_closed(
    db, keyring: KeyRing, state: str
) -> None:
    session_row = _open_session(db, "ses-1")
    _waiting_operation(db, keyring, session_id="ses-1", state=state)
    with pytest.raises(AppError):
        _manager().close_session(db, session_row, now=NOW)


def test_an_explicitly_pinned_session_wins(db) -> None:
    _open_session(db, "ses-1")
    decision = _manager().select_session(
        db,
        conversation_id=CANONICAL,
        user_text="换个话题",
        now=NOW,
        pinned_session_id="ses-pinned",
    )
    assert decision.session_id == "ses-pinned"


# -- F-D3 / F-D4 / F-D5: every classifier deviation continues --------------


@pytest.mark.parametrize(
    "answer",
    [
        None,
        "open_new_session",
        {"decision": "open_new_session"},
        {"decision": "open_new_session", "reason": "task_boundary"},
        {
            "decision": "open_new_session",
            "reason": "task_boundary",
            "confidence_band": "high",
            "extra": "smuggled",
        },
        {
            "decision": "maybe",
            "reason": "task_boundary",
            "confidence_band": "high",
        },
        {
            "decision": "open_new_session",
            "reason": "because I felt like it",
            "confidence_band": "high",
        },
        # `explicit_*` is the user's to give and `previous_closed` the store's:
        # a classifier claiming either answers a question it was not asked.
        {
            "decision": "open_new_session",
            "reason": "explicit_reset",
            "confidence_band": "high",
        },
        {
            "decision": "open_new_session",
            "reason": "previous_closed",
            "confidence_band": "high",
        },
        {
            "decision": "open_new_session",
            "reason": "task_boundary",
            "confidence_band": "low",
        },
        # Several answers at once is a shape the contract has no room for.
        [
            {
                "decision": "open_new_session",
                "reason": "task_boundary",
                "confidence_band": "high",
            },
            {
                "decision": "continue_session",
                "reason": "task_boundary",
                "confidence_band": "high",
            },
        ],
    ],
)
def test_a_malformed_classifier_answer_continues_the_session(
    db, answer: object
) -> None:
    _open_session(db, "ses-1")
    decision = _manager(_Classifier(answer)).select_session(
        db, conversation_id=CANONICAL, user_text="然后呢", now=NOW
    )
    assert decision.decision == "continue_session"
    assert decision.session_id == "ses-1"
    assert db.query(ContextSession).count() == 1


@pytest.mark.parametrize(
    "failure", [TimeoutError("provider timed out"), RuntimeError("503")]
)
def test_a_classifier_failure_continues_the_session(db, failure: Exception) -> None:
    _open_session(db, "ses-1")
    decision = _manager(_Classifier(raises=failure)).select_session(
        db, conversation_id=CANONICAL, user_text="然后呢", now=NOW
    )
    assert decision.decision == "continue_session"


def test_no_classifier_at_all_continues_the_session(db) -> None:
    # The safe default shape: a deployment with no semantic classifier makes
    # every boundary decision deterministically and never fragments a topic.
    _open_session(db, "ses-1")
    decision = _manager().select_session(
        db, conversation_id=CANONICAL, user_text="然后呢", now=NOW
    )
    assert decision.decision == "continue_session"


def test_a_prepared_answer_is_rejected_after_a_same_timestamp_event(
    db, engine, keyring
) -> None:
    """The Timeline sequence, not a collision-prone clock, is the CAS version."""

    current = _open_session(db)
    db.commit()
    classifier = _Classifier(
        {
            "decision": "open_new_session",
            "reason": "task_boundary",
            "confidence_band": "high",
        }
    )
    manager = _manager(classifier)
    prepared = manager.prepare_classification(
        db,
        conversation_id=CANONICAL,
        user_text="开始另一件事",
        now=NOW,
    )
    db.commit()
    resolved = manager.resolve_classification(prepared)

    # Another request appends at the exact same injected clock instant. The
    # Session timestamp therefore cannot distinguish the snapshots.
    with session_factory(engine)() as concurrent:
        events.append_event(
            concurrent,
            keyring,
            conversation_id=CANONICAL,
            session_id=current.session_id,
            turn_id=events.new_turn_id(),
            event_type=events.USER_MESSAGE,
            content={"text": "并发消息"},
            operation_id=None,
            now=NOW,
        )
        concurrent.commit()

    decision = manager.select_session(
        db,
        conversation_id=CANONICAL,
        user_text="开始另一件事",
        now=NOW,
        resolved_classification=resolved,
    )
    assert decision.decision == "continue_session"
    assert decision.session_id == current.session_id


@pytest.mark.parametrize("reason", ["task_boundary", "idle_and_unrelated"])
def test_a_well_formed_new_topic_opens_a_session(db, reason: str) -> None:
    _open_session(db, "ses-1")
    decision = _manager(
        _Classifier(
            {
                "decision": "open_new_session",
                "reason": reason,
                "confidence_band": "high",
            }
        )
    ).select_session(
        db, conversation_id=CANONICAL, user_text="帮我查个别的", now=NOW
    )
    assert decision.decision == "open_new_session"
    assert decision.reason == reason
    assert decision.classifier_version == CLASSIFIER_VERSION
    assert decision.previous_session_id == "ses-1"
    # The predecessor closes in the same breath: a Timeline has at most one
    # open Session, and the partial unique index enforces it.
    assert db.get(ContextSession, "ses-1").status == "closed"


def test_parse_classifier_outcome_accepts_only_the_closed_schema() -> None:
    assert parse_classifier_outcome(
        {
            "decision": "continue_session",
            "reason": "task_boundary",
            "confidence_band": "medium",
        }
    ) == ClassifierOutcome("continue_session", "task_boundary", "medium")
    assert parse_classifier_outcome({"decision": "continue_session"}) is None


# -- F-D11: the classifier sees the minimum ---------------------------------


def test_the_classifier_sees_no_history_tools_or_credentials(db) -> None:
    _open_session(db, "ses-1", boundary_reason="task_boundary")
    classifier = _Classifier(None)
    state = CompactSessionState(
        topic_summary="核对七月旅行支出",
        domain="finance",
        task_state="active",
    )
    provider = _StateProvider(state)
    SessionManager(
        CONFIG, classifier=classifier, state_provider=provider
    ).select_session(
        db, conversation_id=CANONICAL, user_text="然后呢", now=NOW
    )
    request = classifier.seen[0]
    assert request.user_text == "然后呢"
    # A short trusted summary/domain/task state gives the classifier evidence
    # for semantic continuity without sending the archive or tool state.
    assert request.open_session_state == state
    assert set(vars(request)) == {
        "user_text",
        "open_session_state",
        "minutes_since_last_event",
    }
    assert provider.seen[0].session_id == "ses-1"


@pytest.mark.parametrize("provider", [None, _FailingStateProvider()])
def test_a_classifier_without_trusted_semantic_state_is_not_called(
    db, provider
) -> None:
    _open_session(db, "ses-1")
    classifier = _Classifier(
        {
            "decision": "open_new_session",
            "reason": "task_boundary",
            "confidence_band": "high",
        }
    )
    decision = SessionManager(
        CONFIG, classifier=classifier, state_provider=provider
    ).select_session(db, conversation_id=CANONICAL, user_text="然后呢", now=NOW)
    assert decision.decision == "continue_session"
    assert classifier.seen == []


# -- F-D6 / F-D7 / F-D8: explicit signals and lineage -----------------------


@pytest.mark.parametrize(
    ("text_value", "expected"),
    [
        ("这是同一个话题", "correction"),
        ("不是新话题，接着说", "correction"),
        ("继续上次的话题", "resume"),
        ("回到上一个话题", "resume"),
        ("换个话题", "reset"),
        ("我们重新开始吧", "reset"),
        ("午饭 45 个人支出", None),
        ("", None),
        (None, None),
    ],
)
def test_explicit_signals_are_a_closed_set(text_value, expected) -> None:
    assert detect_explicit_signal(text_value) == expected


def test_an_explicit_reset_outranks_a_classifier_that_says_continue(db) -> None:
    _open_session(db, "ses-1")
    decision = _manager(
        _Classifier(
            {
                "decision": "continue_session",
                "reason": "task_boundary",
                "confidence_band": "high",
            }
        )
    ).select_session(db, conversation_id=CANONICAL, user_text="换个话题", now=NOW)
    assert decision.decision == "open_new_session"
    assert decision.reason == "explicit_reset"
    # The user decided, so no classifier version is attributed to it.
    assert decision.classifier_version is None


def test_a_correction_links_back_without_rewriting_history(
    db, keyring: KeyRing
) -> None:
    closed = _closed_session(db, "ses-old", closed_at=NOW - timedelta(minutes=5))
    events.append_event(
        db,
        keyring,
        conversation_id=CANONICAL,
        session_id="ses-old",
        turn_id="trn-old",
        event_type=events.USER_MESSAGE,
        content={"text": "原来的消息"},
        operation_id=None,
        now=NOW - timedelta(minutes=10),
    )
    _open_session(db, "ses-new", opened_at=NOW - timedelta(minutes=4))
    db.commit()

    decision = _manager().select_session(
        db, conversation_id=CANONICAL, user_text="这是同一个话题", now=NOW
    )
    assert decision.decision == "open_new_session"
    assert decision.relation_kind == "corrects_boundary"
    assert decision.parent_session_id == closed.session_id
    assert decision.reason == "explicit_correction"
    # The historical event keeps its Session, its order and its content: a
    # correction adds a relation, it does not rewrite the archive.
    stored = events.list_timeline(db, keyring, conversation_id=CANONICAL)
    assert stored[0].session_id == "ses-old"
    assert stored[0].timeline_sequence == 1
    assert stored[0].content == {"text": "原来的消息"}


def test_a_bare_resume_names_the_most_recent_topic(db) -> None:
    # Every resume marker names *the last* topic, so the target is singular by
    # construction and asking about it would re-ask what the user just said.
    _closed_session(db, "ses-older", closed_at=NOW - timedelta(hours=3))
    recent = _closed_session(db, "ses-recent", closed_at=NOW - timedelta(minutes=5))
    db.commit()
    decision = _manager().select_session(
        db, conversation_id=CANONICAL, user_text="继续上次的话题", now=NOW
    )
    assert decision.relation_kind == "resumes"
    assert decision.parent_session_id == recent.session_id
    assert decision.reason == "explicit_resume"


def test_a_resume_with_nothing_to_resume_just_continues(db) -> None:
    _open_session(db, "ses-1")
    decision = _manager().select_session(
        db, conversation_id=CANONICAL, user_text="继续上次的话题", now=NOW
    )
    assert decision.decision == "continue_session"
    assert decision.session_id == "ses-1"


def test_a_lineage_target_in_another_timeline_is_refused(db) -> None:
    db.add(
        Conversation(
            conversation_id="tl_other",
            created_at=NOW,
            next_sequence=1,
            is_canonical=False,
        )
    )
    db.add(
        ContextSession(
            session_id="ses-foreign",
            conversation_id="tl_other",
            status="closed",
            relation_kind="new_topic",
            opened_at=NOW,
            closed_at=NOW,
        )
    )
    db.commit()
    with pytest.raises(AppError):
        _manager()._require_valid_parent(
            db, conversation_id=CANONICAL, parent_id="ses-foreign"
        )


def test_an_unknown_lineage_target_is_refused(db) -> None:
    with pytest.raises(AppError):
        _manager()._require_valid_parent(
            db, conversation_id=CANONICAL, parent_id="ses-nope"
        )


def test_a_lineage_cycle_is_refused(db) -> None:
    for session_id in ("ses-a", "ses-b"):
        db.add(
            ContextSession(
                session_id=session_id,
                conversation_id=CANONICAL,
                status="closed",
                relation_kind="new_topic",
                opened_at=NOW,
                closed_at=NOW,
            )
        )
    db.flush()
    # Force a cycle past the declarative guards, exactly as a corrupted or
    # hand-edited pair of rows would look. The walk must terminate, not hang.
    db.execute(
        sql(
            "UPDATE context_sessions SET parent_session_id = 'ses-b', "
            "relation_kind = 'resumes' WHERE session_id = 'ses-a'"
        )
    )
    db.execute(
        sql(
            "UPDATE context_sessions SET parent_session_id = 'ses-a', "
            "relation_kind = 'resumes' WHERE session_id = 'ses-b'"
        )
    )
    db.commit()
    db.expire_all()
    with pytest.raises(AppError):
        _manager()._require_valid_parent(
            db, conversation_id=CANONICAL, parent_id="ses-a"
        )


def test_a_self_parent_is_refused_by_the_database(db) -> None:
    db.add(
        ContextSession(
            session_id="ses-self",
            conversation_id=CANONICAL,
            status="open",
            relation_kind="resumes",
            parent_session_id="ses-self",
            opened_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


def test_a_lineage_relation_without_a_parent_is_refused_by_the_database(
    db,
) -> None:
    db.add(
        ContextSession(
            session_id="ses-dangling",
            conversation_id=CANONICAL,
            status="open",
            relation_kind="resumes",
            parent_session_id=None,
            opened_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


# -- F-D10: at most one open Session per Timeline ---------------------------


def test_only_one_open_session_can_exist(db) -> None:
    _open_session(db, "ses-1")
    db.add(
        ContextSession(
            session_id="ses-2",
            conversation_id=CANONICAL,
            status="open",
            relation_kind="new_topic",
            opened_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


# -- the divider rule -------------------------------------------------------


def test_the_first_session_of_a_timeline_records_no_boundary_reason(db) -> None:
    decision = _manager().select_session(
        db, conversation_id=CANONICAL, user_text="第一句", now=NOW
    )
    assert decision.decision == "open_new_session"
    # Not the outcome of a boundary decision, so it invents no reason for one.
    assert decision.reason is None
    # And it divides nothing, so no divider is drawn above the first message.
    assert decision.is_boundary is False


def test_a_session_after_a_closed_one_records_previous_closed(db) -> None:
    _closed_session(db, "ses-old", closed_at=NOW - timedelta(minutes=5))
    db.commit()
    decision = _manager().select_session(
        db, conversation_id=CANONICAL, user_text="新的一句", now=NOW
    )
    assert decision.reason == "previous_closed"
    assert decision.is_boundary is True


def test_the_audit_record_is_enumerations_only(db) -> None:
    _open_session(db, "ses-1")
    decision = _manager().select_session(
        db, conversation_id=CANONICAL, user_text="换个话题", now=NOW
    )
    record = decision.audit_record()
    assert set(record) == {
        "decision",
        "reason",
        "relation_kind",
        "classifier_version",
        "confidence_band",
    }
    # §6.2: enumerations, versions and fingerprints the caller supplies. No
    # user text, no identifier.
    assert "换个话题" not in str(record)
    assert decision.session_id not in str(record)
