"""DEV-026 B: idempotent operation bookkeeping over the Agent database.

Offline: a real SQLite database from the Agent schema, no network and no model.
The focus is the two things that make the write path safe -- one operation per
client request key, and a cancel that never rewrites an accounting outcome.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from personal_agent.api.chat_parts import ImageRefPart, TextPart
from personal_agent.api.operation_state import StaleOperationVersionError
from personal_agent_core.sqlite import run_write_transaction
from personal_agent.api.operation_store import (
    chat_request_fingerprint,
    get_operation,
    mark_detached,
    open_operation,
    request_cancel,
    transition_operation,
)
from personal_agent.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_agent.storage.models import ApiRequest, Device, Operation
from personal_agent_core.errors import AppError, ErrorCode


NOW = datetime(2026, 7, 24, 7, 0, tzinfo=timezone.utc)


@pytest.fixture()
def session(tmp_path: Path):
    engine = create_database_engine(tmp_path / "agent.sqlite")
    create_all(engine)
    with session_factory(engine)() as session:
        session.add(_device())
        session.flush()
        yield session
    engine.dispose()


def _device(device_id: str = "dev-1") -> Device:
    return Device(
        device_id=device_id,
        display_name="iPhone",
        public_key="BASE64URL",
        device_key_thumbprint="THUMB",
        status="active",
        scopes="[]",
        allowed_tools_version="v1",
        created_at=NOW,
    )


FP = chat_request_fingerprint(conversation_id="conv-1", text="午饭 45，个人支出")


def _open(session, key: str = "req-uuid-1", fingerprint: str = FP):
    return open_operation(
        session,
        device_id="dev-1",
        client_request_id=key,
        request_fingerprint=fingerprint,
        now=NOW,
    )


# --- the fingerprint ---------------------------------------------------------


def test_fingerprint_depends_only_on_meaning() -> None:
    a = chat_request_fingerprint(conversation_id="c1", text="午饭 45")
    b = chat_request_fingerprint(conversation_id="c1", text="午饭 45")
    c = chat_request_fingerprint(conversation_id="c1", text="午饭 46")
    d = chat_request_fingerprint(conversation_id="c2", text="午饭 45")
    assert a == b
    assert a != c
    assert a != d


def test_the_pre_media_fingerprint_is_frozen() -> None:
    # §3.2: "无媒体旧 text 请求完整沿用升级前 canonical payload（不加空媒体键）；
    # 以冻结 hash 验证." These literals were produced by the implementation as it
    # stood before media existed, and every text-only request sealed since then
    # is compared against them on replay. A media field -- even an empty one --
    # would change every one of these and turn every existing sealed request
    # into an idempotency conflict.
    assert (
        chat_request_fingerprint(conversation_id="conv_example", text="这张账单记一下")
        == "0a961406f12fdedc624ce21eb01508b62ee9f370346a5884a10911acea8f9775"
    )
    assert (
        chat_request_fingerprint(
            conversation_id="conv_example", text="午餐", clarification_of="op_abc"
        )
        == "8520bed88f7aae23f293c752a9d45d9c4da8ce67429f55e479cf7019169cb93a"
    )
    assert (
        chat_request_fingerprint(
            conversation_id="conv_example", text="hi", start_new_session=True
        )
        == "fb02ec50363222cf9d4497b3fbfe298d6effd587e765acb2bd1a67831a9b6c47"
    )
    # The "no text part" versus "empty string" distinction §3.1 keeps: the
    # empty-text request has its own frozen value.
    assert (
        chat_request_fingerprint(conversation_id="conv_example", text="")
        == "d3af959242f4f8a757db121b1742ad3681acdfe6da560eff756256cefab9e05d"
    )


SHA_A = "a" * 64
SHA_B = "b" * 64


def _image(media_id: str, digest: str) -> ImageRefPart:
    return ImageRefPart(media_id, content_sha256=digest)


def test_a_parts_request_is_identified_by_its_ordered_media() -> None:
    # §3.2: the fingerprint input gains the ordered (media_id, content_sha256)
    # pairs, with the hash the server measured.
    base = dict(conversation_id="c1", text="这张账单记一下")
    legacy = chat_request_fingerprint(**base)

    # The same text, sent as `parts` instead of the legacy field, is a
    # different request: §3.1 requires the two forms stay distinguishable.
    as_parts = chat_request_fingerprint(**base, parts=(TextPart("这张账单记一下"),))
    assert legacy != as_parts

    one = chat_request_fingerprint(**base, parts=(_image("media_1", SHA_A),))
    two = chat_request_fingerprint(
        **base, parts=(_image("media_1", SHA_A), _image("media_2", SHA_B))
    )
    assert legacy != one != two

    # A different image under the same id is a different request.
    assert one != chat_request_fingerprint(**base, parts=(_image("media_1", SHA_B),))


def test_two_identical_uploads_do_not_replay_each_other() -> None:
    # Two photos with the same bytes are two different media ids, and swapping
    # one for the other is a different request even though the digest matches.
    base = dict(conversation_id="c1", text="这两张")
    left = chat_request_fingerprint(**base, parts=(_image("media_1", SHA_A),))
    right = chat_request_fingerprint(**base, parts=(_image("media_2", SHA_A),))
    assert left != right


def test_part_order_is_part_of_the_meaning() -> None:
    base = dict(conversation_id="c1", text="这张账单记一下")
    forward = chat_request_fingerprint(
        **base,
        parts=(TextPart("这张账单记一下"), _image("media_1", SHA_A)),
    )
    # The wire format refuses a reversed pair, so the reversal that reaches the
    # fingerprint is the one where the *text differs* -- and the fingerprint
    # still has to tell the two apart.
    backward = chat_request_fingerprint(
        **base, parts=(_image("media_1", SHA_A), TextPart("这张账单记一下"))
    )
    assert forward != backward


def test_a_part_without_the_servers_digest_cannot_be_fingerprinted() -> None:
    # §3.2: the client submits only a media id and never declares an
    # authoritative hash. A part that has not been resolved against the media
    # table has no digest, and fingerprinting it would produce an identity
    # derived from nothing.
    with pytest.raises(AppError) as excinfo:
        chat_request_fingerprint(
            conversation_id="c1", text="x", parts=(ImageRefPart("media_1"),)
        )
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT


# --- idempotent open ---------------------------------------------------------


def test_open_creates_a_single_accepted_operation(session) -> None:
    opened = _open(session)
    assert opened.created is True
    assert opened.operation.state == "accepted"
    assert opened.operation.state_version == 1
    # The client UUID is reused as the downstream idempotency key.
    assert opened.operation.idempotency_key == "req-uuid-1"
    version, trace_id, span_id, flags = opened.operation.trace_id.split("-")
    assert version == "00"
    assert len(trace_id) == 32
    assert len(span_id) == 16
    assert flags == "01"
    assert session.query(Operation).count() == 1
    assert session.query(ApiRequest).count() == 1


def test_replaying_the_same_request_returns_the_same_operation(session) -> None:
    first = _open(session)
    session.flush()
    second = _open(session)
    assert second.created is False
    assert second.operation.operation_id == first.operation.operation_id
    assert session.query(Operation).count() == 1


def test_same_key_different_request_is_a_conflict(session) -> None:
    _open(session)
    session.flush()
    other = chat_request_fingerprint(conversation_id="conv-1", text="打车 30")
    with pytest.raises(AppError) as exc:
        _open(session, key="req-uuid-1", fingerprint=other)
    assert exc.value.code is ErrorCode.IDEMPOTENCY_CONFLICT


def test_a_key_reserved_by_a_non_chat_request_cannot_open_chat(session) -> None:
    session.add(
        ApiRequest(
            request_id="req-decision",
            device_id="dev-1",
            client_request_id="decision-key",
            request_fingerprint="decision-fingerprint",
            encrypted_request_payload=None,
            received_at=NOW,
        )
    )
    session.flush()
    with pytest.raises(AppError) as exc:
        _open(session, key="decision-key")
    assert exc.value.code is ErrorCode.IDEMPOTENCY_CONFLICT


def test_different_keys_are_different_operations(session) -> None:
    a = _open(session, key="req-uuid-1")
    session.flush()
    b = _open(session, key="req-uuid-2")
    assert a.operation.operation_id != b.operation.operation_id
    assert session.query(Operation).count() == 2


# --- transitions -------------------------------------------------------------


def test_a_legal_transition_bumps_the_version(session) -> None:
    op = _open(session).operation
    session.flush()
    new_version = transition_operation(
        session,
        operation_id=op.operation_id,
        current_state="accepted",
        current_version=1,
        target_state="interpreting",
        now=NOW,
    )
    assert new_version == 2
    session.refresh(op)
    assert op.state == "interpreting"
    assert op.state_version == 2


def test_a_stale_version_cannot_transition(session) -> None:
    op = _open(session).operation
    session.flush()
    transition_operation(
        session,
        operation_id=op.operation_id,
        current_state="accepted",
        current_version=1,
        target_state="interpreting",
        now=NOW,
    )
    # A worker that still believes it is at v1 loses the compare-and-swap. The
    # target is legal from `accepted`, so it clears the safety table and fails
    # only on the stale (state, version) guard.
    with pytest.raises(StaleOperationVersionError):
        transition_operation(
            session,
            operation_id=op.operation_id,
            current_state="accepted",
            current_version=1,
            target_state="failed_safe",
            now=NOW,
        )


def test_a_terminal_transition_records_its_reason_and_result(session) -> None:
    op = _open(session).operation
    session.flush()
    _advance(session, op, ["interpreting", "dispatching", "source_in_progress",
                           "verifying"])
    transition_operation(
        session,
        operation_id=op.operation_id,
        current_state="verifying",
        current_version=op.state_version,
        target_state="succeeded",
        now=NOW,
        tool="finance.log_expense",
        safe_result="recXYZ",
    )
    session.refresh(op)
    assert op.state == "succeeded"
    assert op.tool == "finance.log_expense"
    assert op.safe_result == "recXYZ"


# --- cancellation that does not lie ------------------------------------------


def test_cancel_before_submit_produces_a_clean_cancellation(session) -> None:
    op = _open(session).operation
    session.flush()
    _advance(session, op, ["interpreting", "dispatching"])
    outcome = request_cancel(session, operation_id=op.operation_id, now=NOW)
    assert outcome.cancelled is True
    assert outcome.state == "cancelled_pre_submit"
    session.refresh(op)
    assert op.state == "cancelled_pre_submit"
    assert op.cancel_requested is True


def test_cancel_after_a_possible_submit_only_flags_and_does_not_lie(session) -> None:
    op = _open(session).operation
    session.flush()
    _advance(session, op, ["interpreting", "dispatching", "source_in_progress"])
    outcome = request_cancel(session, operation_id=op.operation_id, now=NOW)
    # The write may be in flight; the state is untouched and the flag records the
    # request, so the client is never told a possible write was rolled back.
    assert outcome.cancelled is False
    assert outcome.state == "source_in_progress"
    session.refresh(op)
    assert op.state == "source_in_progress"
    assert op.cancel_requested is True


def test_cancel_on_a_terminal_operation_is_a_truthful_no_op(session) -> None:
    op = _open(session).operation
    session.flush()
    _advance(session, op, ["interpreting", "dispatching", "source_in_progress",
                           "verifying", "succeeded"])
    outcome = request_cancel(session, operation_id=op.operation_id, now=NOW)
    assert outcome.cancelled is False
    assert outcome.state == "succeeded"
    session.refresh(op)
    assert op.cancel_requested is True


def test_cancel_racing_submit_keeps_the_flag_and_reports_the_new_state(
    tmp_path: Path,
) -> None:
    engine = create_database_engine(tmp_path / "cancel-race.sqlite")
    create_all(engine)
    factory = session_factory(engine)
    with factory() as setup:
        setup.add(_device())
        setup.commit()
        op = _open(setup, key="cancel-race").operation
        _advance(setup, op, ["interpreting", "dispatching"])
        setup.commit()
        operation_id = op.operation_id

    stale_session = factory()
    stale = stale_session.get(Operation, operation_id)
    assert stale.state == "dispatching"
    with factory() as worker:
        current = worker.get(Operation, operation_id)
        transition_operation(
            worker,
            operation_id=operation_id,
            current_state=current.state,
            current_version=current.state_version,
            target_state="source_in_progress",
            now=NOW,
        )
        worker.commit()

    # `DELETE /v1/operations/{id}` runs this through the retrying helper: the
    # stale session read before the worker committed, so its first write is
    # refused by SQLite and the retry re-reads the operation as
    # `source_in_progress`.
    outcome = run_write_transaction(
        stale_session,
        lambda: request_cancel(
            stale_session, operation_id=operation_id, now=NOW
        ),
    )
    stale_session.close()

    assert outcome.cancelled is False
    assert outcome.state == "source_in_progress"
    with factory() as check:
        persisted = check.get(Operation, operation_id)
        assert persisted.state == "source_in_progress"
        assert persisted.cancel_requested is True
    engine.dispose()


def test_mark_detached_sets_the_flag_without_changing_state(session) -> None:
    op = _open(session).operation
    session.flush()
    _advance(session, op, ["interpreting", "dispatching", "source_in_progress"])
    mark_detached(session, operation_id=op.operation_id, now=NOW)
    session.refresh(op)
    assert op.client_detached is True
    assert op.state == "source_in_progress"


def test_cancel_on_an_unknown_operation_is_rejected(session) -> None:
    with pytest.raises(AppError) as exc:
        request_cancel(session, operation_id="op_nope", now=NOW)
    assert exc.value.code is ErrorCode.INVALID_ARGUMENT
    assert get_operation(session, "op_nope") is None


def _advance(session, op: Operation, states: list[str]) -> None:
    """Walk an operation through a legal sequence of states, for test setup."""
    for target in states:
        session.refresh(op)
        transition_operation(
            session,
            operation_id=op.operation_id,
            current_state=op.state,
            current_version=op.state_version,
            target_state=target,
            now=NOW,
        )
    session.refresh(op)


# --- the 2026-08-03 cross-device replay, live evidence in --------------------
# docs/evidence/DEV038_线上半_2026-08-03.md §2.3


def test_another_devices_key_is_a_conflict_not_an_unhandled_constraint(
    session,
) -> None:
    """Two constraints can fire in `open_operation` and they mean opposites.

    `idempotency_key` is globally unique, so a second device presenting a key
    the first already anchored is not a race to re-read -- it is a client error.
    Before the fix the re-read was scoped to (device, key), found nothing, and
    re-raised, which reached production as HTTP 500 with a `null` body.
    """
    first = _open(session, key="shared-key")
    session.commit()
    session.add(_device("dev-2"))
    session.flush()

    with pytest.raises(AppError) as excinfo:
        open_operation(
            session,
            device_id="dev-2",
            client_request_id="shared-key",
            request_fingerprint=FP,
            now=NOW,
        )

    assert excinfo.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    # And the first device's operation is untouched: the refusal creates nothing.
    session.rollback()
    assert (
        session.query(Operation)
        .filter(Operation.idempotency_key == "shared-key")
        .count()
        == 1
    )
    assert (
        session.query(Operation).one().operation_id == first.operation.operation_id
    )


def test_the_owning_device_still_replays_the_same_key(session) -> None:
    """The fix must not turn a legitimate same-device retry into a conflict."""
    first = _open(session, key="mine")
    session.commit()
    again = _open(session, key="mine")
    assert again.created is False
    assert again.operation.operation_id == first.operation.operation_id
