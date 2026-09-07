"""The durable GitHub dispatch executor (whole-track review finding 5, F5).

Henson's frozen scope (2026-09-04): the executor lives **inside the DWS
composition**, and the operator can only *approve or wake* an already-existing,
state/version-bound effect — never submit a branch, SHA, body or idempotency
key. Everything the outward write needs is derived here from persistence:

    effect row (external_effects)  -> owner, remote idempotency key,
                                      target fingerprint, state, version
    target record (effect_dispatch_targets) -> action + closed payload
    the engine's own CAS edges      -> claim and dispatch, each in its own
                                       committed transaction (§5.2)

The request therefore carries only ``(effect_id, expected_state,
expected_version)``. Any further field is refused, not ignored — a request
that names a payload it also carries is exactly the drifted-target shape the
intent row exists to prevent, and silently dropping the extra fields would
teach callers to send them.

**Crash windows** (frozen: ``dispatch_started`` 后崩溃必须转
unknown/reconciliation，禁止重发):

- crash after the claim CAS, before the dispatch CAS: the row sits in
  ``claimed``. A re-wake with the same key re-enters ``dispatch_github_write``,
  which re-runs the remaining edges — the F3-v2 fence semantics. Nothing has
  been sent, so this is safe.
- crash after the dispatch CAS, before the adapter returns: the row is in
  ``dispatch_started``. Every later entry is refused by
  ``dispatch_github_write``'s parked-state guard — the composition **never
  re-fires** a write that may have landed. Closing that window is
  reconciliation's job, driven by the same sweep (``run_unknown_sweep``),
  never by a re-dispatch.

**Reconciliation in the production composition** (frozen: 对账由持久化任务
驱动): ``run_unknown_sweep`` walks every effect in ``unknown`` and runs the
read-only reconciliation pass for it; ``unknown``→``reconciling``→ (judged |
STILL-UNKNOWN→``unknown``) are the frozen edges, so repeated sweeps converge
without ever issuing a write.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy import Engine

from personal_agent_core.ids import new_id
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_core.timeutil import utc_now
from personal_agent_dal.github.adapter_controller import (
    ACTIONS,
    CONTROLLER_SOURCE,
    ControllerRefusal,
    GithubWriteOutcome,
    _apply_step,
    _stop_feature_for_unknown,
    dispatch_github_write,
    fingerprint_for,
    INTENT_STATE,
    CLAIMED_STATE,
    PARKED_COMPLETED,
)
from personal_agent_dal.github.reconciliation import (
    RECONCILING_STATE,
    RECONCILER_ID,
    UNKNOWN_STATE,
    ReconciliationOutcome,
    ReconciliationRefusal,
    reconcile_github_write,
)
from personal_agent_dal.github.reconciliation import COUNTERPARTY_SOURCE
from personal_agent_dal.storage.machine_models import (
    EffectConfirmReceipt,
    EffectDispatchTarget,
    ExternalEffect,
)
from personal_agent_dal.storage.models import Feature

#: The states a wake may operate on. ``intent_recorded`` and ``claimed`` are
#: composable; a ``dispatch_started`` effect is parked (may have landed) and is
#: never re-fired; terminal/unknown/reconciling belong to their own closers.
WAKEABLE_STATES: frozenset[str] = frozenset({INTENT_STATE, CLAIMED_STATE})


class ExecutorRefusal(RuntimeError):
    """The wake refused before any lifecycle step; nothing was written."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class WakeOutcome:
    """What one wake did, at the level the engine proved.

    ``reconciled`` carries the reconciliation outcome when the wake drove a
    read-only reconciliation pass instead of (or after) a dispatch attempt —
    the composition's answer for an unknown it was asked to wake.
    """

    effect_id: str
    effect_state: str
    dispatch: GithubWriteOutcome | None = None
    reconciled: ReconciliationOutcome | None = None
    refusal: ExecutorRefusal | None = None


def record_effect_target(
    session: Any,
    *,
    effect_id: str,
    action: str,
    payload: dict[str, Any],
    now: Any,
) -> None:
    """Persist the target body in the caller's intent transaction.

    Called by :func:`record_github_write_intent` inside the same transaction
    that creates the intent, so target and binding are atomic: a row in
    ``external_effects`` without a target record is a refuse-at-wake defect
    this module's derivation probe reports, never a request-field fallback.
    The fingerprint stored here must equal the intent row's stamped
    fingerprint; a disagreement is a bug at the intent site, refused here.
    """
    from personal_agent_core.manifest import canonical_json
    if action not in ACTIONS:
        raise ExecutorRefusal("INVALID_ARGUMENT", f"action must be one of {ACTIONS}")
    _validate_target_payload(action, payload, effect_id=effect_id)
    fingerprint = fingerprint_for(action, payload)
    effect = session.get(ExternalEffect, effect_id)
    if effect is None:
        raise ExecutorRefusal("NOT_FOUND", f"no external effect {effect_id}")
    if effect.target_fingerprint != fingerprint:
        raise ExecutorRefusal(
            "FINGERPRINT_MISMATCH",
            f"effect {effect_id} intent fingerprint {effect.target_fingerprint} "
            f"!= target record {fingerprint}",
        )
    payload_json = canonical_json(payload)
    existing = session.get(EffectDispatchTarget, effect_id)
    if existing is not None:
        if (
            existing.action != action
            or existing.payload_json != payload_json
            or existing.target_fingerprint != fingerprint
        ):
            raise ExecutorRefusal(
                "IDEMPOTENCY_CONFLICT",
                f"effect {effect_id} already has a different dispatch target",
            )
        return
    row = EffectDispatchTarget(
        effect_id=effect_id,
        action=action,
        payload_json=payload_json,
        target_fingerprint=fingerprint,
        recorded_at=now or utc_now(),
    )
    session.merge(row)


def record_github_write_intent(
    engine: Engine,
    *,
    owner_feature_id: str,
    action: str,
    payload: dict[str, Any],
    remote_idempotency_key: str,
    capability_id: str | None = None,
    now: Any = None,
) -> str:
    """Atomically persist a GitHub intent and its immutable dispatch target.

    This is the trusted controller's production producer boundary.  It is not
    an operator endpoint: the later operator request may only name the returned
    effect id plus state/version.  Replays resolve through the database unique
    key and verify the complete stored pair instead of creating a second row.
    """
    if not isinstance(owner_feature_id, str) or not owner_feature_id:
        raise ExecutorRefusal(
            "INVALID_ARGUMENT", "owner_feature_id must be a non-empty string"
        )
    if not isinstance(remote_idempotency_key, str) or not remote_idempotency_key:
        raise ExecutorRefusal(
            "INVALID_ARGUMENT", "remote_idempotency_key must be a non-empty string"
        )
    if capability_id is not None and (
        not isinstance(capability_id, str) or not capability_id
    ):
        raise ExecutorRefusal(
            "INVALID_ARGUMENT", "capability_id must be a non-empty string or null"
        )
    if action not in ACTIONS:
        raise ExecutorRefusal("INVALID_ARGUMENT", f"action must be one of {ACTIONS}")
    _validate_target_payload(action, payload, effect_id="new effect")
    fingerprint = fingerprint_for(action, payload)
    recorded_at = now or utc_now()
    scope = f"github:{owner_feature_id}:{action}"

    from personal_agent_dal.storage.engine import session_factory

    def _body(session: Any) -> str:
        if session.get(Feature, owner_feature_id) is None:
            raise ExecutorRefusal(
                "NOT_FOUND", f"no feature {owner_feature_id} for GitHub intent"
            )
        from sqlalchemy import select

        existing = session.scalars(
            select(ExternalEffect).where(
                ExternalEffect.effect_scope_key == scope,
                ExternalEffect.remote_idempotency_key == remote_idempotency_key,
            )
        ).one_or_none()
        if existing is not None:
            if (
                existing.owner_aggregate_type != "feature"
                or existing.owner_aggregate_id != owner_feature_id
                or existing.target_fingerprint != fingerprint
            ):
                raise ExecutorRefusal(
                    "IDEMPOTENCY_CONFLICT",
                    "GitHub intent key was already used for different content",
                )
            target = session.get(EffectDispatchTarget, existing.effect_id)
            if (
                target is None
                or target.action != action
                or target.payload_json != _canonical_json(payload)
            ):
                raise ExecutorRefusal(
                    "IDEMPOTENCY_CONFLICT",
                    "GitHub intent replay does not match its target record",
                )
            return existing.effect_id

        effect_id = new_id()
        session.add(
            ExternalEffect(
                effect_id=effect_id,
                version=1,
                origin="dal_dispatched",
                owner_aggregate_type="feature",
                owner_aggregate_id=owner_feature_id,
                effect_scope_key=scope,
                remote_idempotency_key=remote_idempotency_key,
                target_fingerprint=fingerprint,
                state=INTENT_STATE,
                attempt=1,
                executor_id="workflow-service",
                executor_epoch=1,
                claim_expires_at=None,
                capability_id=capability_id,
                capability_epoch=None,
                receipt_refs_sha256=None,
                post_read_refs_sha256=None,
                impact_sha256=None,
                created_at=recorded_at,
                updated_at=recorded_at,
            )
        )
        session.flush()
        record_effect_target(
            session,
            effect_id=effect_id,
            action=action,
            payload=payload,
            now=recorded_at,
        )
        from personal_agent_dal.storage.audit import append_audit_event

        append_audit_event(
            session,
            event_id=new_id(),
            trace_id=effect_id,
            event_type="github.effect_intent",
            redacted_summary=f"GitHub {action} intent and target recorded atomically",
            now=recorded_at,
        )
        return effect_id

    with session_factory(engine)() as session:
        return run_write_transaction(session, lambda: _body(session))


def _canonical_json(payload: dict[str, Any]) -> str:
    from personal_agent_core.manifest import canonical_json

    return canonical_json(payload)


def _validate_target_payload(
    action: str, payload: Any, *, effect_id: str
) -> None:
    from personal_agent_dal.github.adapter import _valid_branch_name, _valid_sha

    if not isinstance(payload, dict):
        raise ExecutorRefusal(
            "TARGET_INVALID", f"{effect_id} target payload is not an object"
        )
    allowed_keys = {
        "push_branch": frozenset({"branch", "head_sha"}),
        "create_pull_request": frozenset(
            {"branch", "base_branch", "title", "body"}
        ),
        "write_check_run": frozenset(
            {
                "branch_head_sha",
                "check_name",
                "external_id",
                "conclusion",
                "details_url",
            }
        ),
    }[action]
    required = allowed_keys - {"details_url"}
    if set(payload) - allowed_keys or required - set(payload):
        raise ExecutorRefusal(
            "TARGET_INVALID", f"{effect_id} target payload shape is not closed"
        )
    for key in required:
        if not isinstance(payload[key], str) or not payload[key]:
            raise ExecutorRefusal(
                "TARGET_INVALID",
                f"{effect_id} target field {key} must be a non-empty string",
            )
    if (
        "details_url" in payload
        and payload["details_url"] is not None
        and not isinstance(payload["details_url"], str)
    ):
        raise ExecutorRefusal(
            "TARGET_INVALID",
            f"{effect_id} target field details_url must be a string or null",
        )
    branch_fields = (
        ("branch",) if action == "push_branch" else
        ("branch", "base_branch") if action == "create_pull_request" else ()
    )
    if any(not _valid_branch_name(payload[field]) for field in branch_fields):
        raise ExecutorRefusal(
            "TARGET_INVALID", f"{effect_id} target carries an invalid branch name"
        )
    sha_field = (
        "head_sha" if action == "push_branch" else
        "branch_head_sha" if action == "write_check_run" else None
    )
    if sha_field is not None and not _valid_sha(payload[sha_field]):
        raise ExecutorRefusal(
            "TARGET_INVALID", f"{effect_id} target carries an invalid commit SHA"
        )
    if action == "write_check_run" and payload["conclusion"] not in {
        "action_required",
        "cancelled",
        "failure",
        "neutral",
        "skipped",
        "stale",
        "success",
        "timed_out",
    }:
        raise ExecutorRefusal(
            "TARGET_INVALID", f"{effect_id} target carries an invalid conclusion"
        )


def _load_target(engine: Engine, effect_id: str) -> EffectDispatchTarget:
    """The persisted target record; its absence refuses the wake."""
    from personal_agent_dal.storage.engine import session_factory

    with session_factory(engine)() as session:
        target = session.get(EffectDispatchTarget, effect_id)
    if target is None:
        raise ExecutorRefusal(
            "TARGET_MISSING",
            f"effect {effect_id} has no persisted dispatch target; the "
            "executor derives every field from persistence and will not "
            "accept them from a request",
        )
    return target


def _validated_target(
    engine: Engine, effect_id: str, facts: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Load and validate the complete persisted target before any adapter call.

    Dispatch and reconciliation share this exact boundary.  A target row that
    is malformed, carries a fourth action, or no longer hashes to both stored
    fingerprints is refused before a write *or* a read-back.  In particular,
    reconciliation may not trust a row merely because it has a foreign key.
    """
    target = _load_target(engine, effect_id)
    if target.action not in ACTIONS:
        raise ExecutorRefusal(
            "TARGET_INVALID", f"effect {effect_id} carries unknown action"
        )
    try:
        payload = json.loads(target.payload_json)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ExecutorRefusal(
            "TARGET_INVALID", f"effect {effect_id} target payload is not valid JSON"
        ) from error
    _validate_target_payload(target.action, payload, effect_id=f"effect {effect_id}")
    fingerprint = fingerprint_for(target.action, payload)
    if (
        target.target_fingerprint != fingerprint
        or facts["target_fingerprint"] != fingerprint
    ):
        raise ExecutorRefusal(
            "FINGERPRINT_MISMATCH",
            f"effect {effect_id} target payload does not match its persisted binding",
        )
    return target.action, payload


def _effect_facts(engine: Engine, effect_id: str) -> dict[str, Any]:
    """The effect row's binding facts, read fresh (§5.2: no identity map)."""
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT state, version, owner_aggregate_type, owner_aggregate_id, "
                "remote_idempotency_key, target_fingerprint FROM external_effects "
                "WHERE effect_id = :eid"
            ).bindparams(eid=effect_id)
        ).first()
    if row is None:
        raise ExecutorRefusal("NOT_FOUND", f"no external effect {effect_id}")
    facts = {
        "state": row[0],
        "version": row[1],
        "owner_aggregate_type": row[2],
        "owner_aggregate_id": row[3],
        "remote_idempotency_key": row[4],
        "target_fingerprint": row[5],
    }
    if facts["owner_aggregate_type"] != "feature":
        raise ExecutorRefusal(
            "TARGET_INVALID",
            f"effect {effect_id} is not owned by a feature",
        )
    return facts


def wake_effect(
    engine: Engine,
    adapter: Any,
    *,
    effect_id: str,
    expected_state: str,
    expected_version: int,
    stop_feature_on_unknown: bool = True,
    now_epoch: int | None = None,
) -> WakeOutcome:
    """Approve/wake one persisted effect: derive, then dispatch.

    The operator's contribution is exactly the state/version binding — a
    stale view refuses (``STATE_MISMATCH`` / ``VERSION_MISMATCH``) before any
    lifecycle step, mirroring the operator plane's CAS contract. Everything
    else is read from the rows; a request that could steer the target would
    defeat the intent row.

    On an ``unknown``/``reconciling`` effect the wake runs the read-only
    reconciliation pass instead of a dispatch — waking a parked unknown is a
    legitimate operator action, and re-dispatching it is the one thing this
    executor must never do.
    """
    if type(effect_id) is not str or not effect_id:
        raise ExecutorRefusal("INVALID_ARGUMENT", "effect_id must be a non-empty string")
    if type(expected_state) is not str or not expected_state:
        raise ExecutorRefusal("INVALID_ARGUMENT", "expected_state must be a non-empty string")
    if type(expected_version) is not int or expected_version < 1:
        raise ExecutorRefusal("INVALID_ARGUMENT", "expected_version must be a positive int")

    facts = _effect_facts(engine, effect_id)
    if facts["state"] != expected_state:
        raise ExecutorRefusal(
            "STATE_MISMATCH",
            f"effect {effect_id} is {facts['state']}, request bound {expected_state}",
        )
    if facts["version"] != expected_version:
        raise ExecutorRefusal(
            "VERSION_MISMATCH",
            f"effect {effect_id} is at version {facts['version']}, "
            f"request bound {expected_version}",
        )

    if facts["state"] in (UNKNOWN_STATE, RECONCILING_STATE):
        action, payload = _validated_target(engine, effect_id, facts)
        try:
            reconciled = reconcile_github_write(
                engine,
                adapter,
                effect_id=effect_id,
                action=action,
                idempotency_key=_reconciliation_key(
                    effect_id, facts["remote_idempotency_key"], facts["version"]
                ),
                payload=_read_payload_for(action, payload),
                feature_id=facts["owner_aggregate_id"],
            )
        except ReconciliationRefusal as error:
            return WakeOutcome(
                effect_id=effect_id,
                effect_state=_effect_facts(engine, effect_id)["state"],
                refusal=ExecutorRefusal(error.code, error.detail),
            )
        return WakeOutcome(
            effect_id=effect_id,
            effect_state=_effect_facts(engine, effect_id)["state"],
            reconciled=reconciled,
        )

    if facts["state"] not in WAKEABLE_STATES:
        raise ExecutorRefusal(
            "ILLEGAL_STATE",
            f"effect {effect_id} is {facts['state']}: a parked "
            "(dispatch_started) effect is never re-fired — it closes via its "
            "owner root or reconciliation; terminal states are closed",
        )

    action, payload = _validated_target(engine, effect_id, facts)
    try:
        dispatch = dispatch_github_write(
            engine,
            adapter,
            effect_id=effect_id,
            action=action,
            idempotency_key=facts["remote_idempotency_key"],
            payload=payload,
            feature_id=facts["owner_aggregate_id"],
            stop_feature_on_unknown=stop_feature_on_unknown,
            now_epoch=now_epoch,
        )
    except ControllerRefusal as error:
        return WakeOutcome(
            effect_id=effect_id,
            effect_state=_effect_facts(engine, effect_id)["state"],
            refusal=ExecutorRefusal(error.code, error.detail),
        )
    return WakeOutcome(
        effect_id=effect_id,
        effect_state=dispatch.effect_state,
        dispatch=dispatch,
    )


def record_effect_confirm_receipt(
    session: Any,
    *,
    effect_id: str,
    action: str,
    target_fingerprint: str,
    composition_key: str,
    now: Any = None,
) -> int:
    """Persist the executor's confirm receipt for one confirmed park (R3-1).

    The dispatch composition calls this after a closed success read-back, in
    its own transaction: the effect stays parked in ``dispatch_started``
    awaiting its owner root, and this row is what later distinguishes that
    confirmed park from a crash window whose fate is unproven. Idempotent by
    primary key — a replayed composition rewrites the identical row — and a
    different receipt for the same effect is a conflict, not a repair.

    Concurrency (R09-B round-4 finding R4-1): the state read here is the
    caller's *transaction's* fresh read, not a stale snapshot, so a park the
    expiry sweep already moved to ``unknown`` is refused and no receipt is
    written; the unit runs under ``run_write_transaction``, so a snapshot
    that interleaves with a committing sweep re-runs from a fresh read and
    reaches the same refusal. The write also takes the effect's concurrency
    token — ``version`` moves to ``version + 1`` — so a sweep whose
    discriminator read is being interleaved *forward* by this commit finds
    its ``record_effect_unknown`` compare-and-swap refused on the stale
    version. Receipt discrimination and recovery CAS therefore form one
    mutually-exclusive outcome per effect: a committed matching receipt is
    never swept; a state that has left ``dispatch_started`` never gains a
    receipt. Returns the row's new version.
    """
    if action not in ACTIONS:
        raise ExecutorRefusal(
            "INVALID_ARGUMENT", f"action must be one of {ACTIONS}"
        )
    effect = session.execute(
        text(
            "SELECT state, version, target_fingerprint FROM external_effects "
            "WHERE effect_id = :eid"
        ).bindparams(eid=effect_id)
    ).first()
    if effect is None:
        raise ExecutorRefusal("NOT_FOUND", f"no external effect {effect_id}")
    state, version, binding = effect
    if state != PARKED_COMPLETED:
        raise ExecutorRefusal(
            "ILLEGAL_STATE",
            f"effect {effect_id} is {state}; a confirm receipt may only be "
            f"recorded for a parked ({PARKED_COMPLETED}) effect",
        )
    if binding != target_fingerprint:
        raise ExecutorRefusal(
            "FINGERPRINT_MISMATCH",
            f"effect {effect_id} binding {binding} != receipt "
            f"{target_fingerprint}",
        )
    existing = session.get(EffectConfirmReceipt, effect_id)
    if existing is not None:
        if (
            existing.action != action
            or existing.target_fingerprint != target_fingerprint
            or existing.composition_key != composition_key
        ):
            raise ExecutorRefusal(
                "IDEMPOTENCY_CONFLICT",
                f"effect {effect_id} already has a different confirm receipt",
            )
        return version
    stamp = now or utc_now()
    session.merge(
        EffectConfirmReceipt(
            effect_id=effect_id,
            action=action,
            target_fingerprint=target_fingerprint,
            composition_key=composition_key,
            confirmed_at=stamp,
        )
    )
    # The concurrency token (R4-1): one transaction writes the receipt and
    # moves the version, so any in-flight recovery compare-and-swap keyed on
    # the pre-receipt version is refused. Inside this transaction the state
    # cannot have changed since the read above (SQLite serialises writers),
    # so no state predicate belongs in this UPDATE — a missed update must
    # never be silent.
    session.execute(
        text(
            "UPDATE external_effects SET version = version + 1, "
            "updated_at = :stamp WHERE effect_id = :eid"
        ).bindparams(stamp=stamp, eid=effect_id)
    )
    session.flush()
    return version + 1


def record_composition_confirm_receipt(
    engine: Engine,
    *,
    effect_id: str,
    action: str,
    composition_key: str,
) -> None:
    """The dispatch composition's confirm-receipt boundary (R09-B R3-1).

    Called after a confirmed read-back, the receipt's fingerprint is the
    effect row's own binding — the composition verified that exact
    fingerprint against the dispatched payload before the outward write, so
    the receipt is backed by the same derivation chain the dispatch was,
    never by the caller's word. A missing row refuses up front.

    The parked-state check lives *inside* the write transaction (round-4
    finding R4-1), not on a plain connection: a park the expiry sweep has
    already moved to ``unknown`` refuses without writing, and an interleave
    mid-transaction surfaces as a snapshot conflict that
    ``run_write_transaction`` re-runs from a fresh read — reaching the same
    refusal. The receipt and its concurrency-token version bump commit
    atomically, so discrimination and recovery CAS cannot both win.
    """
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT target_fingerprint FROM external_effects "
                "WHERE effect_id = :eid"
            ).bindparams(eid=effect_id)
        ).first()
    if row is None:
        raise ExecutorRefusal("NOT_FOUND", f"no external effect {effect_id}")
    target_fingerprint = row[0]
    from personal_agent_dal.storage.engine import session_factory

    with session_factory(engine)() as session:
        run_write_transaction(
            session,
            lambda: record_effect_confirm_receipt(
                session,
                effect_id=effect_id,
                action=action,
                target_fingerprint=target_fingerprint,
                composition_key=composition_key,
            ),
        )


def _reconciliation_key(
    effect_id: str, remote_idempotency_key: str, effect_version: int
) -> str:
    """The reconciliation episode's idempotency key, derived from persistence.

    Each STILL-UNKNOWN round-trip bumps the effect's version, so keying the
    episode on the effect's *current* version gives every claim a fresh,
    reproducible key: a retried episode (same version) replays its own
    receipt instead of re-writing, and a new episode after STILL-UNKNOWN
    never collides with the last one. The remote key anchors it to this
    effect's intent — the key is derived, never caller-supplied.
    """
    return f"reconcile:{effect_id}:{remote_idempotency_key}:v{effect_version}"


def _recover_expired_dispatches(
    engine: Engine, *, limit: int, now_epoch: int
) -> None:
    """Move expired ``dispatch_started`` crash windows to ``unknown``.

    The marker was committed before the network call, so expiry never licenses
    a resend.  It only licenses the frozen EE-DISPATCH-UNKNOWN edge, after
    which the normal read-only reconciliation sweep owns the effect.

    Expiry alone cannot distinguish a crash window from a *confirmed* park:
    both sit in ``dispatch_started`` with a stamped ``claim_expires_at``
    (round-3 finding R3-1). The discriminator is the executor's confirm
    receipt — a park whose effect row still carries a receipt matching its
    own target fingerprint was confirmed by the adapter and is never swept;
    no receipt, or a stale one, fails closed to the recovery edge. The join
    on ``effect_dispatch_targets`` is deliberately gone (round-3 finding
    R3-2): an expired park without a target record must enter ``unknown``
    and surface as a visible ``TARGET_MISSING`` refusal in the sweep, not
    silently rot in ``dispatch_started`` forever.

    The discriminator read and the recovery CAS are one atomic decision
    (round-4 finding R4-1): the receipt writer commits its row and moves the
    effect's ``version`` in a single transaction, so the version this query
    read is only current while no matching receipt exists — a receipt
    committing after this read poisons the in-flight ``record_effect_unknown``
    compare-and-swap, which arrives with a stale version and is refused. A
    committed matching receipt therefore can never be swept, and the sweep
    never holds a transaction across the model-free lifecycle step.

    Every expired park enters this sweep regardless of owner (round-4
    finding R4-2 — the old ``owner_aggregate_type = 'feature'`` filter made
    the non-feature skip below unreachable and let a
    ``recovery_case``-owned effect rot): the effect-level edge does not
    depend on the owner; only the feature stop does.
    """
    now = datetime.fromtimestamp(now_epoch, tz=timezone.utc)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT e.effect_id, e.version, e.owner_aggregate_id, "
                "       e.owner_aggregate_type "
                "FROM external_effects e "
                "WHERE e.state = 'dispatch_started' "
                "AND e.claim_expires_at IS NOT NULL AND e.claim_expires_at <= :now "
                "AND NOT EXISTS ("
                "    SELECT 1 FROM effect_confirm_receipts r "
                "    WHERE r.effect_id = e.effect_id "
                "    AND r.target_fingerprint = e.target_fingerprint) "
                "ORDER BY e.updated_at ASC LIMIT :lim"
            ).bindparams(now=now, lim=limit)
        ).all()
    for effect_id, version, owner_id, owner_type in rows:
        try:
            _apply_step(
                engine,
                command_type="record_effect_unknown",
                evidence_source=CONTROLLER_SOURCE,
                effect_id=effect_id,
                expected_version=version,
                idempotency_key=f"recover-dispatch:{effect_id}:v{version}:unknown",
                facts={"executor.failure_shape": "response_lost"},
            )
        except ControllerRefusal:
            # Another process won the CAS: either it moved the effect first
            # (its fresh state will enter a later pass or is owned by that
            # process) or the confirm receipt committed mid-flight and took
            # the version token (R4-1) — this park is confirmed, not ours.
            continue
        if owner_type != "feature":
            # No feature root to park; the effect's unknown state is its own
            # escalation. Stopping by `owner_id` here would read a *feature
            # id* from a non-feature aggregate and abort the whole sweep on
            # the missing row (round-3 note R3-3's crash shape).
            continue
        try:
            _stop_feature_for_unknown(engine, feature_id=owner_id, now_epoch=now_epoch)
        except ControllerRefusal as error:
            if error.code != "ILLEGAL_TRANSITION":
                raise


def _read_payload_for(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Trim a dispatch payload to the reconciliation read keys for its action.

    The dispatch payload and the read payload differ for ``create_pull_request``
    (no title/body in a listing read) and ``write_check_run`` (no
    conclusion/details). Branch reads use branch only.
    """
    if action == "push_branch":
        return {"branch": payload["branch"], "head_sha": payload["head_sha"]}
    if action == "create_pull_request":
        return {"branch": payload["branch"], "base_branch": payload["base_branch"]}
    if action == "write_check_run":
        return {
            "branch_head_sha": payload["branch_head_sha"],
            "check_name": payload["check_name"],
            "external_id": payload["external_id"],
        }
    raise ExecutorRefusal("INVALID_ARGUMENT", f"unknown action {action}")


def unknown_effects(engine: Engine, *, limit: int = 20) -> list[dict[str, Any]]:
    """The effects a reconciliation sweep owns, oldest first.

    Only ``unknown`` rows enter the sweep — a ``reconciling`` row is already
    claimed by a live reconciliation pass (EE-RECONCILE-START's
    SINGLE_RECONCILER_CLAIM guard), and re-entering it from a second driver
    would race the claim rather than respect it. An *expired* claim is not a
    live pass: ``_recover_expired_reconciling`` returns it to ``unknown``
    first, after which this listing owns it again.
    """
    if type(limit) is not int or limit < 1:
        raise ExecutorRefusal("INVALID_ARGUMENT", "limit must be a positive int")
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT effect_id, version, owner_aggregate_id, "
                "remote_idempotency_key FROM external_effects "
                "WHERE state = :state "
                "ORDER BY updated_at ASC LIMIT :lim"
            ).bindparams(state=UNKNOWN_STATE, lim=limit)
        ).all()
    return [
        {
            "effect_id": r[0],
            "version": r[1],
            "owner_aggregate_id": r[2],
            "remote_idempotency_key": r[3],
        }
        for r in rows
    ]


def reconciling_effects(engine: Engine, *, limit: int = 20) -> list[dict[str, Any]]:
    """The effects currently holding a reconciler claim, oldest first.

    F2 (2026-09-07 review): a crashed reconciler used to be invisible — the
    operator listing only showed ``unknown`` rows. This listing surfaces every
    ``reconciling`` row with its claim expiry so a stuck claim is visible at a
    glance, whether it is a live pass or one awaiting the expiry reclaim.
    """
    if type(limit) is not int or limit < 1:
        raise ExecutorRefusal("INVALID_ARGUMENT", "limit must be a positive int")
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT effect_id, version, owner_aggregate_id, "
                "remote_idempotency_key, claim_expires_at "
                "FROM external_effects "
                "WHERE state = :state AND executor_id = :executor "
                "ORDER BY updated_at ASC LIMIT :lim"
            ).bindparams(
                state=RECONCILING_STATE, executor=RECONCILER_ID, lim=limit
            )
        ).all()
    return [
        {
            "effect_id": r[0],
            "version": r[1],
            "owner_aggregate_id": r[2],
            "remote_idempotency_key": r[3],
            "claim_expires_at": (
                r[4].isoformat() if r[4] is not None else None
            ),
        }
        for r in rows
    ]


def _recover_expired_reconciling(
    engine: Engine, *, limit: int, now_epoch: int
) -> None:
    """Return expired reconciler claims to ``unknown`` (F2, 2026-09-07 review).

    A process that dies after committing ``unknown -> reconciling`` left the
    effect invisible to every automatic path: the sweep selected only
    ``unknown``, the expired-dispatch recovery only ``dispatch_started``, and
    the operator listing only ``unknown``. The claim now stamps
    ``claim_expires_at`` (``_w_reconciler_claim``); this pass drives the same
    frozen EE-RECONCILE-STILL-UNKNOWN edge the inconclusive read-back uses,
    returning the effect to ``unknown`` so the normal sweep re-reconciles it
    read-only. The idempotency key derives from the pre-read version, so a
    replay after the state moved is an idempotent replay, and a CAS lost to
    another process is silently its claim.

    The edge's write set has no ``reconciler_claim`` member, so it does not
    clear ``executor_id``/``claim_expires_at`` — that is fine: every query
    that matters filters on ``state``, and the next EE-RECONCILE-START
    restamps both. The feature is not re-parked: it was already parked by the
    REC-UNKNOWN that produced the unknown effect.

    Known trade-off, stated rather than hidden: an effect whose read-back was
    conclusive but still awaits the human RECONCILE-* resume sits in
    ``reconciling`` too, and this pass will cycle it
    ``reconciling -> unknown -> reconciling`` at the claim-expiry period (each
    cycle: one read-only GET plus the frozen edge's own decision/notification
    writes) until the human acts. Persisting the judgment itself (the never-
    populated ``ReconciliationOutcome.receipt_id``) is heavier contract work
    and is deliberately out of scope here; ``reconciling_effects`` makes the
    state visible in the meantime.
    """
    now = datetime.fromtimestamp(now_epoch, tz=timezone.utc)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT effect_id, version FROM external_effects "
                "WHERE state = :state AND executor_id = :executor "
                "AND claim_expires_at IS NOT NULL AND claim_expires_at <= :now "
                "ORDER BY updated_at ASC LIMIT :lim"
            ).bindparams(
                state=RECONCILING_STATE, executor=RECONCILER_ID, now=now, lim=limit
            )
        ).all()
    for effect_id, version in rows:
        try:
            _apply_step(
                engine,
                command_type="record_reconciliation_unknown",
                evidence_source=COUNTERPARTY_SOURCE,
                effect_id=effect_id,
                expected_version=version,
                idempotency_key=f"recover-reconcile:{effect_id}:v{version}:unknown",
                facts={"evidence.authoritative_result": "unknown"},
            )
        except ControllerRefusal:
            # Another process won the CAS (its claim-restamp or its own
            # STILL-UNKNOWN bumped the version) — the effect is not ours.
            continue


def run_unknown_sweep(
    engine: Engine,
    adapter: Any,
    *,
    limit: int = 20,
    max_effective: int | None = None,
    now_epoch: int | None = None,
) -> list[WakeOutcome]:
    """One persistent-task pass over unknown effects: reconcile, never re-fire.

    The production driver calls this on a timer; it issues only the frozen
    reconciliation edges (read-backs via the adapter's ``read_*`` methods —
    the structural duplicate-write guard) and STILL-UNKNOWN returns. A
    conclusive read-back leaves the effect in ``reconciling`` for the human
    RECONCILE-* root; an inconclusive one returns it to ``unknown`` so a later
    sweep can retry. Effects without a target record are reported as refusals
    in the outcome list rather than aborting the pass.
    """
    if max_effective is not None and max_effective < 0:
        raise ExecutorRefusal("INVALID_ARGUMENT", "max_effective must be non-negative")
    clock = (
        int(datetime.now(tz=timezone.utc).timestamp())
        if now_epoch is None
        else now_epoch
    )
    _recover_expired_dispatches(engine, limit=limit, now_epoch=clock)
    # F2 (2026-09-07 review): expired reconciler claims re-enter this pass as
    # `unknown` before the listing below runs, so a crashed reconciler no
    # longer parks an effect outside every automatic recovery path.
    _recover_expired_reconciling(engine, limit=limit, now_epoch=clock)
    outcomes: list[WakeOutcome] = []
    effective = 0
    for facts in unknown_effects(engine, limit=limit):
        if max_effective is not None and effective >= max_effective:
            break
        try:
            current = _effect_facts(engine, facts["effect_id"])
            action, payload = _validated_target(engine, facts["effect_id"], current)
        except ExecutorRefusal as error:
            outcomes.append(
                WakeOutcome(
                    effect_id=facts["effect_id"],
                    effect_state=UNKNOWN_STATE,
                    refusal=error,
                )
            )
            continue
        effective += 1
        try:
            reconciled = reconcile_github_write(
                engine,
                adapter,
                effect_id=facts["effect_id"],
                action=action,
                idempotency_key=_reconciliation_key(
                    facts["effect_id"],
                    facts["remote_idempotency_key"],
                    facts["version"],
                ),
                payload=_read_payload_for(action, payload),
                feature_id=facts["owner_aggregate_id"],
            )
            outcomes.append(
                WakeOutcome(
                    effect_id=facts["effect_id"],
                    effect_state=_effect_facts(engine, facts["effect_id"])["state"],
                    reconciled=reconciled,
                )
            )
        except ReconciliationRefusal as error:
            outcomes.append(
                WakeOutcome(
                    effect_id=facts["effect_id"],
                    effect_state=_effect_facts(engine, facts["effect_id"])["state"],
                    refusal=ExecutorRefusal(error.code, error.detail),
                )
            )
    return outcomes
