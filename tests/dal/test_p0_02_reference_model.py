"""P0-02 pure action/attempt protocol oracle.

This is deliberately not a test of a production driver.  It pins the recovery
semantics independently, so production composition cannot "pass" merely by
repeating the assumptions that created it.  No test here opens a database,
process, network connection, provider CLI or credential.
"""

from __future__ import annotations

from tests.dal.p0_02_reference_model import ActionWorld


def _world() -> ActionWorld:
    return ActionWorld.new(action_id="a-1", policy_lease_epoch=4, job_lease_epoch=9)


def test_i2_only_one_driver_can_cross_dispatch_boundary() -> None:
    world = _world()

    winner = world.claim_dispatch(owner_id="driver-a", send=True)
    loser = world.claim_dispatch(owner_id="driver-b", send=True)

    assert winner.code == "DISPATCH_GRANTED"
    assert loser.code == "ATTEMPT_OWNERSHIP_LOST"
    assert world.outbound_call_count == 1
    assert world.attempt.owner_id == "driver-a"
    assert world.attempt.fence == 1
    assert world.attempt.state == "dispatching"


def test_i2_crash_after_dispatch_commit_does_not_reissue_call() -> None:
    world = _world()

    assert world.claim_dispatch(owner_id="driver-a", send=False).code == "DISPATCH_GRANTED"
    assert world.outbound_call_count == 0
    assert world.recover().code == "ATTEMPT_UNKNOWN"
    assert world.attempt.state == "unknown"
    assert world.claim_dispatch(owner_id="driver-b", send=True).code == "ATTEMPT_UNKNOWN"
    assert world.outbound_call_count == 0


def test_i3_reclaimed_queue_lease_never_replays_unknown_provider_attempt() -> None:
    world = _world()
    assert world.claim_dispatch(owner_id="driver-a", send=True).code == "DISPATCH_GRANTED"

    world = world.with_job_lease_epoch(10)
    assert world.recover().code == "ATTEMPT_UNKNOWN"
    assert world.claim_dispatch(owner_id="driver-b", send=True).code == "ATTEMPT_UNKNOWN"
    assert world.outbound_call_count == 1


def test_i4_persisted_result_consumes_once_without_second_call() -> None:
    world = _world()
    assert world.claim_dispatch(owner_id="driver-a", send=True).code == "DISPATCH_GRANTED"
    assert (
        world.record_result(
            owner_id="driver-a", fence=1, digest="result-1", job_lease_epoch=9,
            policy_lease_epoch=4,
        ).code
        == "RESULT_RECORDED"
    )

    first = world.consume_result()
    replay = world.consume_result()

    assert first.code == "APPLIED"
    assert replay.code == "APPLIED_REPLAY"
    assert world.feature_advanced is True
    assert world.outbound_call_count == 1
    assert len([e for e in world.evidence if e.kind == "result_consumed"]) == 1


def test_i6_old_fence_or_lease_cannot_advance_but_is_audited() -> None:
    world = _world()
    assert world.claim_dispatch(owner_id="driver-a", send=True).code == "DISPATCH_GRANTED"
    world = world.with_job_lease_epoch(10)

    refused = world.record_result(
        owner_id="driver-a", fence=1, digest="late", job_lease_epoch=9,
        policy_lease_epoch=4,
    )

    assert refused.code == "JOB_LEASE_STALE"
    assert world.feature_advanced is False
    assert world.attempt.state == "dispatching"
    assert world.evidence[-1].kind == "result_refused"


def test_i6_conflicting_digest_is_immutable_conflict_not_overwrite() -> None:
    world = _world()
    assert world.claim_dispatch(owner_id="driver-a", send=True).code == "DISPATCH_GRANTED"
    assert world.record_result(
        owner_id="driver-a", fence=1, digest="first", job_lease_epoch=9,
        policy_lease_epoch=4,
    ).code == "RESULT_RECORDED"

    conflict = world.record_result(
        owner_id="driver-a", fence=1, digest="other", job_lease_epoch=9,
        policy_lease_epoch=4,
    )

    assert conflict.code == "ATTEMPT_RESULT_CONFLICT"
    assert world.attempt.result_digest == "first"
    assert world.evidence[-1].kind == "result_conflict"


def test_i7_cancel_stops_prepared_and_dispatching_attempts_without_erasure() -> None:
    prepared = _world()
    assert prepared.cancel(expected_gate_version=1).code == "CANCELLED"
    assert prepared.claim_dispatch(owner_id="driver-a", send=True).code == "EXECUTION_GATE_CLOSED"
    assert prepared.outbound_call_count == 0

    dispatched = _world()
    assert dispatched.claim_dispatch(owner_id="driver-a", send=True).code == "DISPATCH_GRANTED"
    assert dispatched.cancel(expected_gate_version=1).code == "CANCELLED"
    assert dispatched.recover().code == "ATTEMPT_UNKNOWN"
    assert dispatched.outbound_call_count == 1


def test_i7_resume_requires_new_approval_and_never_revives_old_attempt() -> None:
    world = _world()
    assert world.claim_dispatch(owner_id="driver-a", send=False).code == "DISPATCH_GRANTED"
    assert world.cancel(expected_gate_version=1).code == "CANCELLED"

    assert world.resume(approval_epoch=1).code == "EXECUTION_AUTHORIZATION_STALE"
    assert world.resume(approval_epoch=2).code == "RESUMED"
    assert world.claim_dispatch(owner_id="driver-b", send=True).code == "EXECUTION_AUTHORIZATION_STALE"
    assert world.recover().code == "ATTEMPT_UNKNOWN"
    assert world.create_replacement(accepted_duplicate_cost=True).code == "REPLACEMENT_CREATED"
    assert world.claim_dispatch(owner_id="driver-b", send=True).code == "DISPATCH_GRANTED"
    assert world.outbound_call_count == 1
