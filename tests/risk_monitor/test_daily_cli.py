"""Risk-card seal tests: the daily_cli half that writes the Timeline event."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timezone

from personal_agent.api import events
from personal_agent.context.config import default_context_config
from personal_agent.context.session_manager import SessionManager
from personal_agent.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_agent_core.crypto import KeyRing, generate_key

from risk_monitor import daily_cli


def test_card_content_maps_report_to_contract():
    """``_card_content`` projects a ``build_report`` dict onto the frozen iOS
    contract (as_of/state mandatory, scores may be None, action optional)."""
    report = {
        "as_of": "2026-08-22",
        "state": "NORMAL",
        "scores": {"mbs": 35.0, "css": 40.0, "afrs": None},
        "action": "持仓观察",
    }
    assert daily_cli._card_content(report) == {
        "as_of": "2026-08-22",
        "state": "NORMAL",
        "mbs": 35.0,
        "css": 40.0,
        "afrs": None,
        "action": "持仓观察",
        "quality_status": "ok",
        "stale_days": 0,
        "anomalous": False,
        "components": None,
    }


def test_card_content_passes_components_through():
    """The per-indicator breakdown is carried verbatim into the sealed content."""
    components = {
        "mbs": [{"label": "VIX", "value": "16.0", "band": "green"}],
        "css": [{"label": "AI 篮子", "value": "警戒", "band": "orange"}],
    }
    report = {
        "as_of": "2026-08-22",
        "state": "NORMAL",
        "scores": {"mbs": 0.0, "css": 16.25, "afrs": 21.0},
        "action": "持有（无需操作）",
        "components": components,
    }
    assert daily_cli._card_content(report)["components"] == components


def test_card_content_passes_quality_status_through():
    """A degraded day's flag is sealed onto the card, not dropped."""
    report = {
        "as_of": "2026-08-22",
        "state": "NORMAL",
        "scores": {"mbs": 0.0, "css": 16.25, "afrs": 21.0},
        "action": "持有（无需操作）",
        "quality_status": "data_quality_warning",
    }
    assert daily_cli._card_content(report)["quality_status"] == "data_quality_warning"


def test_card_content_missing_action_is_none():
    report = {
        "as_of": "2026-08-22",
        "state": "DELEVERAGING",
        "scores": {"mbs": 65.0, "css": 40.0, "afrs": 35.0},
    }
    content = daily_cli._card_content(report)
    assert content["action"] is None
    assert content["state"] == "DELEVERAGING"


def test_seal_risk_event_appends_risk_report(monkeypatch):
    """``seal_risk_event`` resolves the canonical Timeline, appends one
    ``risk_report`` event, and returns its id — with the content frozen at
    seal time."""
    captured = {}

    class _FakeEvents:
        RISK_REPORT = "risk_report"

        @staticmethod
        def canonical_timeline_id(session, *, now):
            return "conv-1"

        @staticmethod
        def new_turn_id():
            return "turn-1"

        @staticmethod
        def event_exists_with(session, keyring, *, event_type, content_key, content_value):
            return False

        @staticmethod
        def append_event(
            session, keyring, *, conversation_id, session_id, turn_id,
            event_type, content, operation_id, now,
        ):
            captured.update(
                conversation_id=conversation_id,
                session_id=session_id,
                turn_id=turn_id,
                event_type=event_type,
                content=content,
                operation_id=operation_id,
            )
            return "evt-1"

    class _FakeSessionManager:
        def system_event_session(self, session, *, conversation_id, now):
            return f"sess-{conversation_id}"

    monkeypatch.setattr(daily_cli, "events", _FakeEvents())
    # ``run_write_transaction(session, work)`` calls ``work()`` with no args.
    monkeypatch.setattr(
        daily_cli, "run_write_transaction", lambda session, fn: fn()
    )

    report = {
        "as_of": "2026-08-22",
        "state": "NORMAL",
        "scores": {"mbs": 35.0, "css": 40.0, "afrs": 60.0},
        "action": "持仓观察",
    }
    event_id = daily_cli.seal_risk_event(
        lambda: nullcontext(object()),
        keyring=object(),
        session_manager=_FakeSessionManager(),
        report=report,
        now=datetime(2026, 8, 31, 2, 0, tzinfo=timezone.utc),  # 10:00 Shanghai
    )

    assert event_id == "evt-1"
    assert captured["event_type"] == "risk_report"
    assert captured["conversation_id"] == "conv-1"
    assert captured["session_id"] == "sess-conv-1"
    assert captured["turn_id"] == "turn-1"
    assert captured["operation_id"] is None
    assert captured["content"] == {
        "as_of": "2026-08-22",
        "state": "NORMAL",
        "mbs": 35.0,
        "css": 40.0,
        "afrs": 60.0,
        "action": "持仓观察",
        "quality_status": "ok",
        "stale_days": 0,
        "anomalous": False,
        "components": None,
        "sealed_on": "2026-08-31",
    }


def test_seal_risk_event_writes_a_real_timeline_event(tmp_path):
    """B1 remediation: the seal is exercised against a real SQLite Agent database
    and the real AEAD keyring + append path — not fakes that could only confirm
    the code's own assumptions (§5.1). The event must land, decrypt back, and
    carry the frozen content including the per-indicator components."""
    engine = create_database_engine(tmp_path / "agent.sqlite")
    create_all(engine)
    sessions = session_factory(engine)
    keyring = KeyRing([generate_key("risk-seal-fixture")], service="personal-agent-api")
    session_manager = SessionManager(default_context_config())

    report = {
        "as_of": "2026-08-22",
        "state": "NORMAL",
        "scores": {"mbs": 0.0, "css": 16.25, "afrs": 21.0},
        "action": "持有（无需操作）",
        "components": {
            "mbs": [{"label": "VIX", "value": "16.0", "band": "green"}],
            "css": [{"label": "AI 篮子", "value": "警戒", "band": "orange"}],
        },
    }

    event_id = daily_cli.seal_risk_event(sessions, keyring, session_manager, report)

    with sessions() as session:
        timeline_id = events.canonical_timeline_id(
            session, now=datetime.now(timezone.utc)
        )
        entries = events.list_timeline(session, keyring, conversation_id=timeline_id)
    engine.dispose()

    assert [entry.event_type for entry in entries] == ["risk_report"]
    entry = entries[0]
    assert entry.event_id == event_id
    assert entry.content["as_of"] == "2026-08-22"
    assert entry.content["state"] == "NORMAL"
    assert entry.content["mbs"] == 0.0
    assert entry.content["components"]["mbs"][0] == {
        "label": "VIX",
        "value": "16.0",
        "band": "green",
    }
    assert entry.content["components"]["css"][0]["band"] == "orange"


def test_seal_risk_event_is_idempotent_within_a_day(tmp_path):
    """Sealing twice on the same Shanghai day adds nothing: the second call
    finds the existing card and returns None, so a manual rerun never stacks a
    duplicate."""
    engine = create_database_engine(tmp_path / "agent.sqlite")
    create_all(engine)
    sessions = session_factory(engine)
    keyring = KeyRing([generate_key("risk-seal-fixture")], service="personal-agent-api")
    session_manager = SessionManager(default_context_config())

    report = {
        "as_of": "2026-08-22",
        "state": "NORMAL",
        "scores": {"mbs": 0.0, "css": 16.25, "afrs": 21.0},
        "action": "持有（无需操作）",
        "components": {"mbs": [], "css": []},
    }
    morning = datetime(2026, 8, 31, 2, 0, tzinfo=timezone.utc)  # 10:00 Shanghai

    first = daily_cli.seal_risk_event(
        sessions, keyring, session_manager, report, now=morning
    )
    second = daily_cli.seal_risk_event(
        sessions, keyring, session_manager, report, now=morning
    )

    assert first is not None
    assert second is None

    with sessions() as session:
        timeline_id = events.canonical_timeline_id(
            session, now=datetime.now(timezone.utc)
        )
        entries = events.list_timeline(session, keyring, conversation_id=timeline_id)
    engine.dispose()

    assert [entry.event_type for entry in entries] == ["risk_report"]
    assert len(entries) == 1


def test_seal_risk_event_seals_a_fresh_card_on_a_new_day(tmp_path):
    """The live defect fixed 2026-08-31: keying idempotency on ``as_of`` meant a
    weekend (whose market data, and therefore whose as_of, had not moved)
    suppressed every card after the first morning — Monday woke to no card even
    though the push had gone out. Idempotency is on the Shanghai seal day: the
    same as_of re-sealed on a later morning lands a new card."""
    engine = create_database_engine(tmp_path / "agent.sqlite")
    create_all(engine)
    sessions = session_factory(engine)
    keyring = KeyRing([generate_key("risk-seal-fixture")], service="personal-agent-api")
    session_manager = SessionManager(default_context_config())

    saturday_report = {
        "as_of": "2026-08-28",
        "state": "NORMAL",
        "scores": {"mbs": 0.0, "css": 8.75, "afrs": 27.9697},
        "action": "持有（无需操作）",
    }
    monday_report = {
        "as_of": "2026-08-28",  # same market data, three days later
        "state": "NORMAL",
        "scores": {"mbs": 0.0, "css": 21.875, "afrs": 27.9697},  # drifted recompute
        "action": "持有（无需操作）",
    }
    saturday = datetime(2026, 8, 29, 2, 0, tzinfo=timezone.utc)
    monday = datetime(2026, 8, 31, 2, 0, tzinfo=timezone.utc)

    saturday_id = daily_cli.seal_risk_event(
        sessions, keyring, session_manager, saturday_report, now=saturday
    )
    monday_id = daily_cli.seal_risk_event(
        sessions, keyring, session_manager, monday_report, now=monday
    )

    assert saturday_id is not None
    assert monday_id is not None
    assert saturday_id != monday_id

    with sessions() as session:
        timeline_id = events.canonical_timeline_id(
            session, now=datetime.now(timezone.utc)
        )
        entries = events.list_timeline(session, keyring, conversation_id=timeline_id)
    engine.dispose()

    assert [entry.event_type for entry in entries] == [
        "risk_report",
        "risk_report",
    ]
    assert entries[0].content["as_of"] == "2026-08-28"
    assert entries[0].content["sealed_on"] == "2026-08-29"
    assert entries[0].content["css"] == 8.75
    assert entries[1].content["as_of"] == "2026-08-28"
    assert entries[1].content["sealed_on"] == "2026-08-31"
    assert entries[1].content["css"] == 21.875
