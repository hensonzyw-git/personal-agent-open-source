"""`OP-GH-ADAPTER-001`: the ECS GitHub write composition (DAL-032, R09-B).

One composition = one external write, riding the frozen external-effect
lifecycle with **no invented edges**. The frozen registry (transition-spec
registry v1.0) offers the controller exactly these service-actor edges on
``external_effect``, and the frozen `DAL-T-GIT-ACK-001` oracle binds the
same sequence for a push/PR whose response is lost:

    intent_recorded --EE-CLAIM--> claimed
      --EE-DISPATCH--> dispatch_started          (claim facts; pre-call CAS)
      --(the adapter's single write, outside any transaction)-->
        read-back confirmed exact  -> EE-CONFIRM-NOT-EXECUTED is NOT usable;
                                      the effect stays dispatch_started and
                                      closes only via the owner root
                                      (SM-MERGED-DISPATCHED at G5) or the
                                      human RECONCILE-* roots. A confirmed
                                      push/PR therefore parks the effect in
                                      dispatch_started and reports the
                                      read-back to the caller, which owns the
                                      owner-root command.
        read-back proves not-executed -> EE-CONFIRM-NOT-EXECUTED
                                       (counterparty-adapter evidence).
        outcome unknowable / 5xx / refused-shape -> EE-DISPATCH-UNKNOWN
                                       (external-effect-controller evidence),
                                       then REC-UNKNOWN require_reconciliation
                                       on the feature.

§3.6 is explicit that no standalone ``EE-CONFIRM-COMPLETED`` exists; inventing
one would fabricate a successful external fact the registry cannot represent.
The adapter therefore never claims "completed" on its own authority — a
confirmed write parks in ``dispatch_started`` for the owner root to close,
which is exactly how the G5 managed-merge composition is specified.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy import Engine

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.github.adapter import (
    CheckRunOutcome,
    GithubAdapter,
    PullRequestOutcome,
    PushOutcome,
)
from personal_agent_dal.machine.engine import TransitionCommand, apply_transition
from personal_agent_dal.machine.guards import GuardFacts

#: The operation spec this controller's compositions belong to (R09-B).
OPERATION_SPEC_ID: Final[str] = "OP-GH-ADAPTER-001"

#: evidence source per edge, exactly as each frozen spec's binding names it.
CONTROLLER_SOURCE: Final[str] = "external-effect-controller"
EXECUTOR_SOURCE: Final[str] = "effect-executor"
COUNTERPARTY_SOURCE: Final[str] = "counterparty-adapter"

#: Closed action set; anything else is refused before the claim.
ACTIONS: Final[tuple[str, ...]] = (
    "push_branch",
    "create_pull_request",
    "write_check_run",
)

PARKED_COMPLETED: Final[str] = "dispatch_started"
UNKNOWN_STATE: Final[str] = "unknown"
NOT_EXECUTED_STATE: Final[str] = "confirmed_not_executed"
CLAIMED_STATE: Final[str] = "claimed"
INTENT_STATE: Final[str] = "intent_recorded"

#: The lease window the engine stamps on claim/dispatch (§3.6 rule 4: a claim
#: is time-bounded). The controller re-checks liveness against this on every
#: fresh read, so an expired claim is refused, never silently re-blessed.
EXECUTOR_ID: Final[str] = "home-mac-worker"

#: Server-side 5xx statuses whose refusal text means "the write MAY have
#: landed": only post-read reconciliation may close these, never this
#: controller's not-executed edge.
_HTTP_PERSISTENT_ERROR_CLASSES: Final[frozenset[int]] = frozenset({500, 502, 503, 504})


class ControllerRefusal(RuntimeError):
    """A closed-shape refusal: nothing was claimed, nothing was written."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class GithubWriteOutcome:
    """What one composition did, at the level the engine proved.

    ``effect_state`` is read back from the row after the composition, so the
    caller sees the lifecycle state the engine persisted — never a claim
    derived from the adapter response alone. ``adapter_outcome`` carries the
    closed read-back (or the refusal/unknown marker) for the evidence bundle
    and for the owner-root command that closes a confirmed write.
    """

    effect_id: str
    effect_state: str
    adapter_outcome: PushOutcome | PullRequestOutcome | CheckRunOutcome | None
    refusal: ControllerRefusal | None = None


def _invalid(detail: str) -> DalError:
    return DalError(DalErrorCode.INVALID_ARGUMENT, internal_detail=detail)


def _epoch_of(value: Any) -> float | None:
    """The POSIX seconds of a timestamp cell, whatever the read returned.

    Raw SQL (Core) returns the stored RFC 3339 text — the TypeDecorator's
    parse runs only on ORM result rows — so both shapes arrive here.
    """
    if value is None:
        return None
    if isinstance(value, str):
        from personal_agent_core.timeutil import parse_rfc3339

        return parse_rfc3339(value).timestamp()
    return float(value.timestamp())


def _composition_was_applied(
    engine: Engine, effect_id: str, idempotency_key: str
) -> bool:
    """Whether *this effect's* lifecycle receipts already carry this key.

    The engine's replay fence matches ``transition_receipts.idempotency_key``
    globally, so the same key re-submitting any of this composition's steps
    returns the original receipt instead of a refusal. The controller checks
    the fence *before* the parked-state guard, because a key whose dispatch
    already applied will find the effect in ``dispatch_started`` (parked) —
    refusing there would make a delivered-but-acked-late composition
    un-answerable and invite a second write.

    The query binds the effect's aggregate id and escapes the two LIKE
    wildcards (whole-track review finding 3, 2026-09-04): an unescaped
    ``%``/``_`` in a key would widen the match to receipts this composition
    never wrote, and an unbound prefix could answer from another effect's
    receipts — both fabricate a replay that never happened.
    """
    escaped = idempotency_key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT receipt_id FROM transition_receipts "
                "WHERE idempotency_key LIKE :prefix ESCAPE '\\' "
                "AND aggregate_id = :eid LIMIT 1"
            ).bindparams(prefix=f"{escaped}:%", eid=effect_id)
        ).first()
    return row is not None


def _effect_row(engine: Engine, effect_id: str) -> tuple[str, int]:
    """The effect's (state, version), read per call — never held across calls."""
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT state, version FROM external_effects "
                "WHERE effect_id = :eid"
            ).bindparams(eid=effect_id)
        ).first()
    if row is None:
        raise _invalid(f"no external effect {effect_id}")
    return row[0], row[1]


def _intent_binding(engine: Engine, effect_id: str) -> dict[str, Any]:
    """The intent row's frozen binding: owner, target fingerprint, remote key.

    Read per call, before any state moves — these three fields are what the
    composition is allowed to send, and every closing edge's ``target_*``
    guard fact is judged against this comparison, not asserted.
    """
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT owner_aggregate_id, target_fingerprint, "
                "remote_idempotency_key FROM external_effects "
                "WHERE effect_id = :eid"
            ).bindparams(eid=effect_id)
        ).first()
    if row is None:
        raise _invalid(f"no external effect {effect_id}")
    owner_id, fingerprint, remote_key = row
    return {
        "owner_aggregate_id": owner_id,
        "target_fingerprint": fingerprint,
        "remote_idempotency_key": remote_key,
    }


def _claim_guard_facts(engine: Engine, effect_id: str, now_epoch: int) -> dict[str, Any]:
    """Derive `capability.epoch_current` / `lease.epoch_current` from the rows.

    Server-derived facts (§2.3.1), not controller opinions. The effect row's
    `capability_epoch` is the epoch the *feature* carried when the intent was
    recorded; `None` means no binding was stamped (the engine's `_new_effect`
    never sets one, and the frozen harness treats that shape as current). A
    stamped epoch must equal the feature's current epoch — the kill switch
    bumps the feature's epoch, so a claim computed against a pre-bump
    snapshot refuses here. A capability row bound to the effect must also be
    live (unrevoked, unexhausted, unexpired) for the epoch fact to hold.

    The lease fact: the executor claim must be live or not yet stamped — a
    pre-claim effect has nothing to be stale, and the EE-DISPATCH recheck
    (OP-KILL-001's frozen third step) re-judges it before the outward call.
    """
    with engine.connect() as connection:
        effect = connection.execute(
            text(
                "SELECT owner_aggregate_id, capability_id, capability_epoch, "
                "claim_expires_at FROM external_effects WHERE effect_id = :eid"
            ).bindparams(eid=effect_id)
        ).first()
        if effect is None:
            raise _invalid(f"no external effect {effect_id}")
        owner_id, capability_id, capability_epoch, claim_expires_at = effect

        fact: dict[str, Any] = {
            "capability.epoch_current": False,
            "lease.epoch_current": False,
        }

        feature = connection.execute(
            text("SELECT capability_epoch FROM features WHERE feature_id = :fid")
            .bindparams(fid=owner_id)
        ).first()
        if feature is None:
            raise _invalid(f"no owner feature {owner_id} for effect {effect_id}")
        current_epoch = feature[0]

        if capability_epoch is not None and capability_epoch != current_epoch:
            return fact

        if capability_id is not None:
            cap = connection.execute(
                text(
                    "SELECT expires_at, max_uses, uses_consumed, revoked_at "
                    "FROM capabilities WHERE capability_id = :cid"
                ).bindparams(cid=capability_id)
            ).first()
            if cap is None:
                return fact
            expires_at, max_uses, uses_consumed, revoked_at = cap
            if (
                revoked_at is not None
                or uses_consumed >= max_uses
                or expires_at is None
                or (cap_expiry := _epoch_of(expires_at)) is None
                or cap_expiry <= now_epoch
            ):
                return fact

        fact["capability.epoch_current"] = True

        # The lease fact: the claim window is live, or nothing was claimed yet.
        claim_expiry = _epoch_of(claim_expires_at)
        if claim_expiry is None or claim_expiry > now_epoch:
            fact["lease.epoch_current"] = True
        return fact


def _dispatch_guard_facts(engine: Engine, effect_id: str, now_epoch: int) -> dict[str, Any]:
    """Derive `executor.claim_matches` / `executor.claim_expired` from the row.

    The dispatch edge's guard is the recheck step of OP-KILL-001's frozen
    action sequence: between the claim CAS and the outward write, the claim
    must still name this executor and must not have expired. A lapsed window
    makes the fact False, so EE-DISPATCH refuses before the HTTP call.
    """
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT executor_id, claim_expires_at FROM external_effects "
                "WHERE effect_id = :eid"
            ).bindparams(eid=effect_id)
        ).first()
    if row is None:
        raise _invalid(f"no external effect {effect_id}")
    executor_id, claim_expires_at = row
    expiry = _epoch_of(claim_expires_at)
    expired = expiry is not None and expiry <= now_epoch
    return {
        "executor.claim_matches": executor_id == EXECUTOR_ID,
        "executor.claim_expired": expired,
    }


def _stop_guard_facts(engine: Engine, feature_id: str) -> dict[str, Any]:
    """Count the feature's effects that are unknown or reconciling (§3.2.1).

    The UNKNOWN_EXTERNAL_EFFECT guard needs `unknown_or_reconciling_count > 0`.
    The inventory is the feature's full effect set — including effects owned
    through recovery cases (the engine's `_w_external_effect_inventory`
    enumerates owner ids the same way) — so this counts rows directly rather
    than asserting a constant the row state may contradict.
    """
    from personal_agent_dal.storage.machine_models import RecoveryCase

    with engine.connect() as connection:
        feature = connection.execute(
            text("SELECT feature_id FROM features WHERE feature_id = :fid")
            .bindparams(fid=feature_id)
        ).first()
        if feature is None:
            raise _invalid(f"no feature {feature_id}")
        owner_ids = [feature_id]
        owner_ids.extend(
            connection.execute(
                text("SELECT recovery_case_id FROM recovery_cases WHERE feature_id = :fid")
                .bindparams(fid=feature_id)
            ).scalars()
        )
        binds = {f"o{i}": oid for i, oid in enumerate(owner_ids)}
        placeholders = ", ".join(f":{name}" for name in binds)
        rows = connection.execute(
            text(
                "SELECT state FROM external_effects "
                f"WHERE owner_aggregate_id IN ({placeholders}) "  # noqa: S608 — bound ids only
                "AND state IN ('unknown', 'reconciling')"
            ).bindparams(**binds)
        ).all()
    return {"effect_inventory.unknown_or_reconciling_count": len(rows)}


def _apply_step(
    engine: Engine,
    *,
    command_type: str,
    evidence_source: str,
    effect_id: str,
    expected_version: int,
    idempotency_key: str,
    facts: dict[str, Any],
) -> Any:
    """One frozen lifecycle edge, resolved by exact (from_state, command_type)."""
    from personal_agent_dal.machine.registry import transition_registry

    registry = transition_registry()
    state, _ = _effect_row(engine, effect_id)
    matches = []
    for spec_id in registry.spec_ids:
        spec = registry.by_id(spec_id)
        if (
            spec["aggregate_type"] == "external_effect"
            and spec["from_state"] == state
            and spec["command_type"] == command_type
        ):
            matches.append(spec)
    if len(matches) != 1:
        raise ControllerRefusal(
            "ILLEGAL_TRANSITION",
            f"lifecycle step {command_type} not unique from {state}: "
            f"{[m['spec_id'] for m in matches]}",
        )
    spec = matches[0]
    binding = spec["actor_evidence_bindings"][0]
    command = TransitionCommand(
        aggregate_type="external_effect",
        aggregate_id=effect_id,
        command_type=spec["command_type"],
        command_parameters=dict(spec["command_parameters"] or {}),
        actor_type=binding["actor_type"],
        evidence_source_types=(evidence_source,),
        evidence_schema_versions=tuple(spec["required_evidence_schema_versions"]),
        decision_action=spec["requires_decision_action"],
        reason_code=(
            spec["allowed_reason_codes"][0] if spec["allowed_reason_codes"] else None
        ),
        expected_version=expected_version,
        idempotency_key=idempotency_key,
    )
    outcome = apply_transition(engine, command, facts=GuardFacts(facts))
    if outcome.receipt_code not in ("APPLIED", "IDEMPOTENT_REPLAY"):
        raise ControllerRefusal(
            outcome.receipt_code,
            f"{command_type} refused: {outcome.reason_code or outcome.receipt_code}",
        )
    return outcome


def _canonical_fingerprint(payload: dict[str, Any]) -> str:
    """The stable target fingerprint: sha256 over the action's closed payload."""
    from personal_agent_core.manifest import canonical_json

    return hashlib.sha256(
        canonical_json(
            {"op": OPERATION_SPEC_ID, "payload": payload}
        ).encode("utf-8")
    ).hexdigest()


def dispatch_github_write(
    engine: Engine,
    adapter: GithubAdapter,
    *,
    effect_id: str,
    action: str,
    idempotency_key: str,
    payload: dict[str, Any],
    feature_id: str | None,
    stop_feature_on_unknown: bool = True,
    now_epoch: int | None = None,
) -> GithubWriteOutcome:
    """Claim → dispatch → one adapter write → confirm / not-executed / unknown.

    ``payload`` is the closed per-action object the adapter layer validates:
    ``push_branch`` needs ``branch``/``head_sha``; ``create_pull_request``
    needs ``branch``/``base_branch``/``title``/``body``; ``write_check_run``
    needs ``branch_head_sha``/``check_name``/``external_id``/``conclusion``
    plus an optional ``details_url``. Any other key is refused before the
    claim.

    Composition order per §5.2: the claim and dispatch CAS steps each run in
    their own committed transaction; the adapter call happens with no
    transaction held; the confirming edge runs after, against fresh state.

    The guard facts for claim and dispatch are derived from the capability /
    lease / effect rows at the moment each CAS applies (the frozen
    OP-KILL-001 sequence: read epoch → claim → recheck before dispatch), so
    a kill-switch revocation that lands between the steps refuses the next
    edge instead of riding a pre-asserted True. An effect already past
    ``dispatch_started`` — parked, unknown, or confirmed — is refused
    without any adapter call: one composition owns one write, and a second
    composition must never re-fire it.
    """
    if action not in ACTIONS:
        raise _invalid(f"action must be one of {ACTIONS}")
    if type(idempotency_key) is not str or not idempotency_key:
        raise _invalid("idempotency_key must be a non-empty native string")
    if feature_id is not None and (type(feature_id) is not str or not feature_id):
        raise _invalid("feature_id must be a non-empty native string or None")
    if not isinstance(payload, dict) or not payload:
        raise _invalid(f"action {action} needs its payload object")
    allowed_keys = {
        "push_branch": frozenset({"branch", "head_sha"}),
        "create_pull_request": frozenset({"branch", "base_branch", "title", "body"}),
        "write_check_run": frozenset(
            {"branch_head_sha", "check_name", "external_id", "conclusion", "details_url"}
        ),
    }[action]
    extra = set(payload) - allowed_keys
    if extra:
        raise _invalid(f"action {action} payload carries unknown keys: {sorted(extra)}")
    missing = allowed_keys - {"details_url"} - set(payload)
    if missing:
        raise _invalid(f"action {action} payload is missing {sorted(missing)}")
    clock = int(time.time()) if now_epoch is None else now_epoch

    state, version = _effect_row(engine, effect_id)

    # --- intent binding (whole-track review finding 1, 2026-09-04) -------
    # The intent row froze exactly one target (fingerprint over action +
    # closed payload) and one remote dedupe key. Dispatching a drifted
    # target or under another key would fire a write the recorded intent
    # never named; the refusal is pre-write, so not-executed is provable.
    # The binding comparison also feeds the closing edges' guard facts —
    # ``target_matches`` stops being an asserted constant.
    binding = _intent_binding(engine, effect_id)
    expected_fingerprint = _canonical_fingerprint({"action": action, **payload})
    if binding["target_fingerprint"] != expected_fingerprint:
        raise ControllerRefusal(
            "POLICY_DENIED",
            "effect {eid} target fingerprint does not match the dispatch "
            "intent (action={action}): the effect row names another target; "
            "refusing zero-write".format(eid=effect_id, action=action),
        )
    if binding["remote_idempotency_key"] != idempotency_key:
        raise ControllerRefusal(
            "POLICY_DENIED",
            "effect {eid} remote idempotency key does not match the "
            "dispatch key; refusing zero-write".format(eid=effect_id),
        )

    # --- owner derivation (whole-track review finding 2, 2026-09-04) -----
    # The owner is whatever the effect row says, never what the caller
    # supplied: a caller-supplied feature_id that disagrees with the row
    # would park the wrong feature (or none) on unknown while the true
    # owner stayed awaiting_merge. A supplied id must agree; None derives.
    if feature_id is None:
        feature_id = binding["owner_aggregate_id"]
    elif feature_id != binding["owner_aggregate_id"]:
        raise ControllerRefusal(
            "POLICY_DENIED",
            "effect {eid} owner is {owner}, not the supplied feature "
            "{feature}: refusing zero-write".format(
                eid=effect_id, owner=binding["owner_aggregate_id"],
                feature=feature_id,
            ),
        )

    # --- 0. the replay fence (§2.6): this exact key already composed -----
    # A replayed composition must answer from its receipts, never re-fire the
    # outward write and never refuse as a second write. Any lifecycle step
    # with this composition's key having been applied is a completed replay.
    if _composition_was_applied(engine, effect_id, idempotency_key):
        state, _ = _effect_row(engine, effect_id)
        return GithubWriteOutcome(
            effect_id=effect_id,
            effect_state=state,
            adapter_outcome=None,
            refusal=None,
        )

    # --- 1. claim (intent_recorded -> claimed), then fresh read ---------
    if state == INTENT_STATE:
        _apply_step(
            engine,
            command_type="claim_external_effect",
            evidence_source=CONTROLLER_SOURCE,
            effect_id=effect_id,
            expected_version=version,
            idempotency_key=f"{idempotency_key}:claim",
            facts=_claim_guard_facts(engine, effect_id, clock),
        )
        state, version = _effect_row(engine, effect_id)
    if state != CLAIMED_STATE:
        raise ControllerRefusal(
            "ILLEGAL_TRANSITION",
            f"effect {effect_id} is {state}; only {INTENT_STATE} or "
            f"{CLAIMED_STATE} compose here — a {PARKED_COMPLETED} effect is "
            "already written and closes via its owner root, a terminal or "
            "unknown one via reconciliation",
        )

    # --- 2. dispatch CAS (claimed -> dispatch_started) -------------------
    if state == CLAIMED_STATE:
        _apply_step(
            engine,
            command_type="record_effect_dispatch",
            evidence_source=EXECUTOR_SOURCE,
            effect_id=effect_id,
            expected_version=version,
            idempotency_key=f"{idempotency_key}:dispatch",
            facts=_dispatch_guard_facts(engine, effect_id, clock),
        )
        state, version = _effect_row(engine, effect_id)
    if state != PARKED_COMPLETED:
        raise ControllerRefusal(
            "ILLEGAL_TRANSITION",
            f"effect {effect_id} is {state}, not {PARKED_COMPLETED}",
        )

    # --- 3. the one outward write; no transaction held across it --------
    try:
        if action == "push_branch":
            outcome: PushOutcome | PullRequestOutcome | CheckRunOutcome = (
                adapter.push_feature_branch(
                    branch=payload["branch"],
                    head_sha=payload["head_sha"],
                    idempotency_key=idempotency_key,
                )
            )
        elif action == "create_pull_request":
            outcome = adapter.create_pull_request(
                branch=payload["branch"],
                base_branch=payload["base_branch"],
                title=payload["title"],
                body=payload["body"],
                idempotency_key=idempotency_key,
            )
        else:
            outcome = adapter.write_check_run(
                branch_head_sha=payload["branch_head_sha"],
                check_name=payload["check_name"],
                external_id=payload["external_id"],
                conclusion=payload["conclusion"],
                details_url=payload.get("details_url"),
                idempotency_key=idempotency_key,
            )
    except Exception as error:  # noqa: BLE001 - fail closed to unknown
        outcome = _unknown_outcome(action, idempotency_key, f"adapter raised: {error}")

    judged = _judge(outcome)

    # --- 4. the closing edge the registry actually offers ----------------
    if judged == "not_executed":
        # The target/key comparison ran against the rows before the write;
        # the guard facts report that comparison, not a constant.
        _apply_step(
            engine,
            command_type="record_effect_not_executed",
            evidence_source=COUNTERPARTY_SOURCE,
            effect_id=effect_id,
            expected_version=version,
            idempotency_key=f"{idempotency_key}:not-executed",
            facts={
                "evidence.scope_key_matches": True,
                "evidence.target_matches": (
                    binding["target_fingerprint"]
                    == _canonical_fingerprint({"action": action, **payload})
                ),
                "evidence.not_executed_readback_valid": True,
            },
        )
    elif judged == "unknown":
        _apply_step(
            engine,
            command_type="record_effect_unknown",
            evidence_source=CONTROLLER_SOURCE,
            effect_id=effect_id,
            expected_version=version,
            idempotency_key=f"{idempotency_key}:unknown",
            facts={"executor.failure_shape": "response_lost"},
        )
        state, _ = _effect_row(engine, effect_id)
        if stop_feature_on_unknown and state == UNKNOWN_STATE:
            _stop_feature_for_unknown(engine, feature_id=feature_id, now_epoch=clock)

    state, _ = _effect_row(engine, effect_id)
    return GithubWriteOutcome(
        effect_id=effect_id,
        effect_state=state,
        adapter_outcome=outcome,
    )


def _unknown_outcome(
    action: str, idempotency_key: str, detail: str
) -> PushOutcome | PullRequestOutcome | CheckRunOutcome:
    marker = type(
        "UnknownMarker", (), {
            "unknown": True,
            "refusal": None,
            "repository_id": None,
            "branch": None,
            "head_sha": None,
            "pull_request_number": None,
            "check_name": None,
            "check_run_id": None,
            "external_id": None,
            "conclusion": None,
            "idempotency_key": idempotency_key,
            "__repr__": lambda self: f"<unknown {action}: {detail}>",
        },
    )()
    return marker  # type: ignore[return-value]


def _judge(outcome: Any) -> str:
    """Closed adapter outcome → closing edge, judged by refusal *stage*.

    ``confirmed`` parks the effect in ``dispatch_started``: §3.6 freezes no
    standalone completed edge, and claiming success on the adapter's word
    alone would fabricate an external fact.

    The refusal's stage — set by the adapter where the refusal happened, not
    parsed from prose here — decides provability:

    - ``pre_write`` (nothing sent) and ``write`` (the server refused the
      write itself) prove not-executed;
    - a 5xx under stage ``write`` is still ``unknown``: a 5xx says the
      server *may* have applied the write before failing, and only
      DAL-034's post-read reconciliation may decide;
    - ``post_write`` (the default stage, including read-back drift after a
      201) cannot prove absence — the write may have landed with a shape we
      cannot verify — so it is ``unknown``.
    """
    if getattr(outcome, "unknown", False):
        return "unknown"
    refusal = getattr(outcome, "refusal", None)
    if refusal is not None:
        stage = getattr(refusal, "stage", "post_write")
        if stage in ("pre_write", "write"):
            reason = refusal.reason
            for status in _HTTP_PERSISTENT_ERROR_CLASSES:
                if f"HTTP {status}" in reason:
                    return "unknown"
            return "not_executed"
        return "unknown"
    if isinstance(outcome, (PushOutcome, PullRequestOutcome, CheckRunOutcome)):
        return "confirmed"
    return "unknown"


def _stop_feature_for_unknown(engine: Engine, *, feature_id: str, now_epoch: int) -> None:
    """REC-UNKNOWN: park the owner feature for reconciliation (service act)."""
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT state, version FROM features WHERE feature_id = :fid"
            ).bindparams(fid=feature_id)
        ).first()
    if row is None:
        raise _invalid(f"no feature {feature_id}")
    state, version = row
    from personal_agent_dal.machine.registry import transition_registry

    registry = transition_registry()
    matches = [
        registry.by_id(spec_id)
        for spec_id in registry.spec_ids
        if (
            registry.by_id(spec_id)["aggregate_type"] == "feature"
            and registry.by_id(spec_id)["from_state"] == state
            and registry.by_id(spec_id)["command_type"] == "require_reconciliation"
        )
    ]
    if len(matches) != 1:
        raise ControllerRefusal(
            "ILLEGAL_TRANSITION",
            f"require_reconciliation not unique from {state}: "
            f"{[m['spec_id'] for m in matches]}",
        )
    spec = matches[0]
    binding = spec["actor_evidence_bindings"][0]
    command = TransitionCommand(
        aggregate_type="feature",
        aggregate_id=feature_id,
        command_type="require_reconciliation",
        command_parameters=dict(spec["command_parameters"] or {}),
        actor_type=binding["actor_type"],
        evidence_source_types=(CONTROLLER_SOURCE,),
        evidence_schema_versions=tuple(spec["required_evidence_schema_versions"]),
        decision_action=spec["requires_decision_action"],
        reason_code="EXTERNAL_RESULT_UNKNOWN",
        expected_version=version,
        idempotency_key=f"reconcile:{feature_id}:{version}",
    )
    outcome = apply_transition(
        engine,
        command,
        facts=GuardFacts(_stop_guard_facts(engine, feature_id)),
    )
    if outcome.receipt_code not in ("APPLIED", "IDEMPOTENT_REPLAY"):
        raise ControllerRefusal(
            outcome.receipt_code,
            f"require_reconciliation refused: {outcome.reason_code or outcome.receipt_code}",
        )


def fingerprint_for(action: str, payload: dict[str, Any]) -> str:
    """The target fingerprint a caller stamps onto the intent row."""
    return _canonical_fingerprint({"action": action, **payload})
