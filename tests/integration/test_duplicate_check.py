"""DEV-020: an exact duplicate stops the write, and only Henson can release it.

Two properties carry the whole task. Zero writes when a candidate exists, and an
override that a model cannot produce: it has to name a check row this server
created, for this exact entry, still unexpired, whose candidate set has not
moved. Each of those is tested by trying to break it.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from personal_agent_core.crypto import KeyRing, generate_key
from personal_data_mcp.finance.duplicate_check import (
    Candidate,
    OVERRIDE_TTL,
    OverrideRefused,
    authorise_override,
    candidate_set_hash,
    dismiss,
    expire_stale,
    find_exact_duplicates,
    intent_fingerprint,
    raise_check,
)
from personal_data_mcp.finance.expense_record import ExpenseEntry
from personal_data_mcp.finance.ledger_reader import LedgerExpense
from personal_data_mcp.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_data_mcp.storage.models import DuplicateCheck


NOW = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
DAY = date(2026, 7, 23)

LUNCH = ExpenseEntry(
    name="午饭",
    amount_cny=Decimal("20.00"),
    occurred_on=DAY,
    is_family_expense=False,
    category="餐饮",
)


@pytest.fixture()
def sessions(tmp_path: Path):
    engine = create_database_engine(tmp_path / "finance.sqlite")
    create_all(engine)
    yield session_factory(engine)
    engine.dispose()


@pytest.fixture()
def keyring():
    return KeyRing([generate_key("dup-2026-01")], service="personal_data_mcp")


def ledger_row(
    *,
    name: str = "午饭",
    amount: str = "20.00",
    when: date = DAY,
    category: str = "餐饮",
    record_id: str = "rec1",
    is_family_expense: bool | None = False,
) -> LedgerExpense:
    return LedgerExpense(
        record_id=record_id,
        name=name,
        amount_cny=Decimal(amount),
        occurred_on=when,
        category=category,
        is_family_expense=is_family_expense,
    )


# --- the match is exact, on final stored values ------------------------------


def test_an_identical_row_is_a_candidate() -> None:
    assert len(find_exact_duplicates(LUNCH, [ledger_row()])) == 1


def test_scope_does_not_decide_whether_to_prompt() -> None:
    """A row differing only by family scope still matches (design 7.7).

    Scope is shown for judgement, not used as part of the key, so two entries
    that differ only there must still pause.
    """
    family_lunch = ExpenseEntry(
        name="午饭",
        amount_cny=Decimal("20.00"),
        occurred_on=DAY,
        is_family_expense=True,
        category="餐饮",
    )
    candidates = find_exact_duplicates(
        family_lunch, [ledger_row(is_family_expense=False)]
    )
    assert len(candidates) == 1
    assert candidates[0].is_family_expense is False


def test_candidate_card_carries_the_existing_family_scope() -> None:
    candidates = find_exact_duplicates(
        LUNCH, [ledger_row(is_family_expense=True)]
    )
    assert candidates[0].card()["is_family_expense"] is True


@pytest.mark.parametrize(
    "difference",
    [
        {"amount": "20.01"},
        {"name": "午饭 "},
        {"category": "购物"},
        {"when": date(2026, 7, 22)},
    ],
)
def test_any_difference_in_the_key_is_not_a_duplicate(difference) -> None:
    # No normalisation, no similarity, no amount ranges: a trailing space or a
    # cent is a different entry, and guessing otherwise would suppress a real
    # write.
    assert find_exact_duplicates(LUNCH, [ledger_row(**difference)]) == ()


def test_a_manually_created_row_counts_the_same() -> None:
    # The scan is of the whole ledger, not of what the Agent wrote, so a row
    # Henson typed into Feishu himself is a candidate like any other.
    rows = [ledger_row(record_id="manual-1")]
    candidates = find_exact_duplicates(LUNCH, rows)
    assert [c.record_id for c in candidates] == ["manual-1"]


def test_several_candidates_are_all_reported() -> None:
    rows = [ledger_row(record_id="rec1"), ledger_row(record_id="rec2")]
    assert len(find_exact_duplicates(LUNCH, rows)) == 2


def test_the_candidate_set_hash_ignores_order() -> None:
    a = Candidate("rec1", "午饭", Decimal("20"), "餐饮")
    b = Candidate("rec2", "午饭", Decimal("20"), "餐饮")
    assert candidate_set_hash((a, b)) == candidate_set_hash((b, a))


# --- releasing requires a real, current, matching decision -------------------


def raise_for(
    sessions, keyring, entry=LUNCH, rows=None, now=NOW, idempotency_key="idem-1"
):
    rows = rows if rows is not None else [ledger_row()]
    candidates = find_exact_duplicates(entry, rows)
    with sessions() as session:
        finding = raise_check(
            session,
            entry=entry,
            candidates=candidates,
            keyring=keyring,
            now=now,
            idempotency_key=idempotency_key,
        )
        session.commit()
    return finding, candidates


def test_a_raised_check_is_pending_and_seals_its_candidates(
    sessions, keyring
) -> None:
    finding, _ = raise_for(sessions, keyring)
    with sessions() as session:
        check = session.get(DuplicateCheck, finding.check_id)
        assert check.status == "awaiting_decision"
        assert check.decided_at is None
        # The record ids point at real ledger rows, so they are never stored in
        # the clear.
        assert "rec1" not in str(check.encrypted_candidate_record_ids)


def test_the_matching_decision_releases_the_write(sessions, keyring) -> None:
    finding, candidates = raise_for(sessions, keyring)
    with sessions() as session:
        authorise_override(
            session,
            check_id=finding.check_id,
            entry=LUNCH,
            current_candidates=candidates,
            keyring=keyring,
            now=NOW,
        )
        session.commit()
    with sessions() as session:
        assert session.get(DuplicateCheck, finding.check_id).status == "write_anyway"


def test_an_invented_check_id_is_refused(sessions, keyring) -> None:
    # The closest a model could get to forging one: assert an id.
    _, candidates = raise_for(sessions, keyring)
    with sessions() as session:
        with pytest.raises(OverrideRefused, match="no such"):
            authorise_override(
                session,
                check_id="00000000-0000-0000-0000-000000000000",
                entry=LUNCH,
                current_candidates=candidates,
                keyring=keyring,
                now=NOW,
            )


def test_an_override_cannot_be_carried_to_a_different_entry(
    sessions, keyring
) -> None:
    finding, candidates = raise_for(sessions, keyring)
    other = ExpenseEntry(
        name="午饭",
        amount_cny=Decimal("200.00"),
        occurred_on=DAY,
        is_family_expense=False,
        category="餐饮",
    )
    with sessions() as session:
        with pytest.raises(OverrideRefused, match="different entry"):
            authorise_override(
                session,
                check_id=finding.check_id,
                entry=other,
                current_candidates=candidates,
                keyring=keyring,
                now=NOW,
            )


def test_restating_the_scope_also_invalidates_the_override(
    sessions, keyring
) -> None:
    # Scope is not part of the match key but *is* part of the intent: agreeing
    # to record a personal lunch is not agreement to record a family one.
    finding, candidates = raise_for(sessions, keyring)
    family = ExpenseEntry(
        name="午饭",
        amount_cny=Decimal("20.00"),
        occurred_on=DAY,
        is_family_expense=True,
        category="餐饮",
    )
    assert intent_fingerprint(family) != intent_fingerprint(LUNCH)
    with sessions() as session:
        with pytest.raises(OverrideRefused):
            authorise_override(
                session,
                check_id=finding.check_id,
                entry=family,
                current_candidates=candidates,
                keyring=keyring,
                now=NOW,
            )


def test_a_changed_candidate_set_invalidates_the_override(
    sessions, keyring
) -> None:
    finding, _ = raise_for(sessions, keyring)
    # Someone added another identical row in Feishu between the question and
    # the answer: Henson agreed about a different world.
    moved = find_exact_duplicates(
        LUNCH, [ledger_row(record_id="rec1"), ledger_row(record_id="rec2")]
    )
    with sessions() as session:
        with pytest.raises(OverrideRefused, match="candidate set changed"):
            authorise_override(
                session,
                check_id=finding.check_id,
                entry=LUNCH,
                current_candidates=moved,
                keyring=keyring,
                now=NOW,
            )


def test_an_expired_check_is_refused(sessions, keyring) -> None:
    finding, candidates = raise_for(sessions, keyring)
    with sessions() as session:
        with pytest.raises(OverrideRefused, match="expired"):
            authorise_override(
                session,
                check_id=finding.check_id,
                entry=LUNCH,
                current_candidates=candidates,
                keyring=keyring,
                now=NOW + OVERRIDE_TTL + timedelta(seconds=1),
            )


def test_a_decision_cannot_be_spent_twice(sessions, keyring) -> None:
    finding, candidates = raise_for(sessions, keyring)
    with sessions() as session:
        authorise_override(
            session,
            check_id=finding.check_id,
            entry=LUNCH,
            current_candidates=candidates,
            keyring=keyring,
            now=NOW,
        )
        session.commit()
    with sessions() as session:
        with pytest.raises(OverrideRefused, match="already"):
            authorise_override(
                session,
                check_id=finding.check_id,
                entry=LUNCH,
                current_candidates=candidates,
                keyring=keyring,
                now=NOW,
            )


def test_a_stale_session_cannot_authorise_an_already_spent_decision(
    sessions, keyring
) -> None:
    finding, candidates = raise_for(sessions, keyring)
    stale_session = sessions()
    winning_session = sessions()
    try:
        # Both workers observed the same pending decision. The winner commits;
        # the stale identity map must not be able to overwrite that decision.
        stale = stale_session.get(DuplicateCheck, finding.check_id)
        assert stale.status == "awaiting_decision"
        authorise_override(
            winning_session,
            check_id=finding.check_id,
            entry=LUNCH,
            current_candidates=candidates,
            keyring=keyring,
            now=NOW,
        )
        winning_session.commit()

        with pytest.raises(OverrideRefused, match="concurrently"):
            authorise_override(
                stale_session,
                check_id=finding.check_id,
                entry=LUNCH,
                current_candidates=candidates,
                keyring=keyring,
                now=NOW,
            )
    finally:
        stale_session.close()
        winning_session.close()


def test_dismissing_ends_the_operation_without_side_effects(
    sessions, keyring
) -> None:
    finding, candidates = raise_for(sessions, keyring)
    with sessions() as session:
        dismiss(session, check_id=finding.check_id, now=NOW)
        session.commit()
    with sessions() as session:
        assert session.get(DuplicateCheck, finding.check_id).status == "dismissed"
        with pytest.raises(OverrideRefused):
            authorise_override(
                session,
                check_id=finding.check_id,
                entry=LUNCH,
                current_candidates=candidates,
                keyring=keyring,
                now=NOW,
            )


def test_stale_checks_expire_rather_than_linger(sessions, keyring) -> None:
    finding, _ = raise_for(sessions, keyring)
    with sessions() as session:
        assert expire_stale(session, now=NOW + OVERRIDE_TTL + timedelta(1)) == 1
        session.commit()
    with sessions() as session:
        assert session.get(DuplicateCheck, finding.check_id).status == "expired"
