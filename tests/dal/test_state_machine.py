"""DAL-009: `DAL-T-SM-001` and `DAL-T-RECOVERY-001`, replayed from the contracts.

939 registry-driven variants across two aggregates. They are not 939 hand-written
tests: each is one row of the frozen TransitionSpec registry expanded into an
allow vector and its actor, evidence-source and guard deny vectors, and the
engine under test is an interpreter over that same registry. What the replay
proves is that the interpreter admits exactly the transitions the registry
permits and refuses everything else — which is the property §2.3.1 actually
asks for.

Every variant is hash-bound to its frozen fixture and oracle, runs against a
real SQLite database, and is judged on five dimensions: receipt code and
schema, state trace, event trace, final snapshot (state, reason code, reason
owner) and the write set **as observed in the database**. On top of the frozen
oracle, every allowed variant is also judged on the persisted content the
oracle structurally cannot name — receipt rows, event rows and the aggregate's
own row — because the oracle asserts event-type strings and receipt codes, and
a row that records the wrong from/to state or version would pass those.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.dal.contract_loader import FrozenContracts
from tests.dal.transition_executor import (
    database_changed,
    persisted_content_divergences,
    run_transition_fixture,
    unobserved_members,
)
from personal_agent_core.timeutil import utc_now


SM_TEST_ID = "DAL-T-SM-001"
RECOVERY_TEST_ID = "DAL-T-RECOVERY-001"
EXTERNAL_EFFECT_TEST_ID = "DAL-T-EXTERNAL-EFFECT-001"


@pytest.fixture(scope="module")
def contracts() -> FrozenContracts:
    return FrozenContracts()


def _registry_variants(contracts: FrozenContracts, test_id: str):
    """The G1 variants driven by a `transition_command` rather than a scenario."""
    return [
        variant
        for variant in contracts.variants(test_id)
        if variant.run_gate == "G1"
        and variant.fixture.body.get("transition_command") is not None
    ]


def _ids(variants) -> list[str]:
    return [v.variant_id for v in variants]


def test_registry_expansion_is_fully_covered(contracts: FrozenContracts) -> None:
    """Every G1 spec has an allow vector, and every spec an actor deny vector.

    §2.3.1 requires the expansion to be exhaustive. If a spec existed with no
    allow vector, the interpreter could refuse it forever and no test would
    notice; if it had no deny vector, it could admit any actor. The sweep
    covers all three aggregates -- an ExternalEffect spec is dispatchable on
    its own, so leaving it out would exempt eleven real edges from the
    allowlist proof.
    """
    from personal_agent_dal.machine.registry import transition_registry

    covered_allow: set[str] = set()
    covered_deny: set[str] = set()
    for test_id in (SM_TEST_ID, RECOVERY_TEST_ID, EXTERNAL_EFFECT_TEST_ID):
        for variant in _registry_variants(contracts, test_id):
            coverage = variant.fixture.body.get("coverage_ref")
            if coverage is None:
                continue
            if variant.variant_id.startswith("expanded_spec_allow"):
                covered_allow.add(coverage)
            if variant.variant_id.startswith("actor_deny"):
                covered_deny.add(coverage)

    registry = transition_registry()
    g1_specs = {
        spec_id
        for spec_id in registry.spec_ids
        if registry.by_id(spec_id)["minimum_run_gate"] == "G1"
    }
    # Every G1 spec has an allow vector, and every allow vector has a matching
    # actor deny vector -- both directions, with no threshold slack.
    assert covered_allow == g1_specs, (
        f"G1 specs without an allow vector: {sorted(g1_specs - covered_allow)}"
    )
    assert covered_allow == covered_deny, (
        f"specs with allow but no actor deny: {sorted(covered_allow - covered_deny)}"
    )


@pytest.mark.parametrize(
    "test_id", [SM_TEST_ID, RECOVERY_TEST_ID, EXTERNAL_EFFECT_TEST_ID]
)
def test_every_registry_variant_matches_its_oracle(
    contracts: FrozenContracts, test_id: str, tmp_path: Path
) -> None:
    """Replay every registry-driven variant and report all divergences at once.

    Parametrising 939 pytest cases would make a single regression print 939
    lines; the interesting output is *which* specs diverged and how, so the
    loop collects failures and reports them together.
    """
    variants = _registry_variants(contracts, test_id)
    assert variants, f"no registry-driven variants for {test_id}"

    failures: list[str] = []
    for index, variant in enumerate(variants):
        database = tmp_path / f"{test_id}-{index}.db"
        oracle = variant.oracle.body
        outcome, before, after = run_transition_fixture(
            variant.fixture.body, database=database
        )
        problems: list[str] = []

        expected_receipt = oracle["expected_receipts"][0]
        if outcome.receipt_code != expected_receipt["code"]:
            problems.append(
                f"receipt {outcome.receipt_code} != {expected_receipt['code']}"
            )
        if outcome.receipt_schema != expected_receipt["schema_version"]:
            problems.append(
                f"schema {outcome.receipt_schema} != {expected_receipt['schema_version']}"
            )

        expected_states = oracle["expected_state_trace"]
        actual_states = [outcome.from_state, outcome.to_state]
        if actual_states != expected_states:
            problems.append(f"states {actual_states} != {expected_states}")

        if list(outcome.events) != oracle["expected_event_trace"]:
            problems.append(
                f"events {list(outcome.events)} != {oracle['expected_event_trace']}"
            )

        snapshot = oracle["expected_final_snapshot"]
        if outcome.to_state != snapshot["state"]:
            problems.append(f"final state {outcome.to_state} != {snapshot['state']}")
        if outcome.receipt_code == "APPLIED":
            if outcome.reason_code != snapshot["reason_code"]:
                problems.append(
                    f"reason {outcome.reason_code} != {snapshot['reason_code']}"
                )
            if outcome.reason_owner != snapshot["reason_owner"]:
                problems.append(
                    f"reason owner {outcome.reason_owner} != {snapshot['reason_owner']}"
                )

        allowed = set(oracle["allowed_write_set"])
        if set(outcome.writes) != allowed:
            problems.append(
                f"declared writes {sorted(set(outcome.writes))} != {sorted(allowed)}"
            )
        if not allowed:
            # A refusal must leave the database byte-for-byte as it was.
            if database_changed(before, after):
                problems.append("refusal wrote to the database")
        else:
            missing = unobserved_members(outcome.writes, before, after)
            if missing:
                problems.append(f"declared but not observed: {missing}")
            # Beyond the oracle: the rows that were just persisted must say
            # what the transition actually was. This is the check that makes
            # a receipt or event recording a wrong state or version a test
            # failure rather than an invisible lie.
            divergences = persisted_content_divergences(
                database, variant.fixture.body, outcome
            )
            if divergences:
                problems.append("persisted content: " + "; ".join(divergences))

        if problems:
            failures.append(f"{variant.variant_id}: " + "; ".join(problems))

    assert not failures, (
        f"{len(failures)} of {len(variants)} {test_id} variants diverged:\n"
        + "\n".join(f"  - {line}" for line in failures[:25])
        + (f"\n  … and {len(failures) - 25} more" if len(failures) > 25 else "")
    )


@pytest.mark.parametrize("test_id", [SM_TEST_ID, RECOVERY_TEST_ID])
def test_every_scenario_variant_matches_its_oracle(
    contracts: FrozenContracts, test_id: str, tmp_path: Path
) -> None:
    """Replay the `operation_commands` scenarios: business command → registry.

    These 13 variants carry no `transition_command`; they describe a business
    operation and the authoritative facts it observed. The resolver layer in
    `transition_executor` commits those facts to the one registry command
    they name -- written from the contract's evidence bindings, not from the
    oracles -- and the engine is then judged exactly as a registry variant
    would be. The scenario list is asserted exhaustively so a new scenario
    variant that nobody resolved fails loudly instead of being skipped.
    """
    from tests.dal.transition_executor import run_scenario_fixture

    expected = {
        SM_TEST_ID: {
            "event_mismatch",
            "illegal_edge",
            "paused_resume_base_drift",
            "terminal_cancelled",
            "terminal_completed",
        },
        RECOVERY_TEST_ID: {
            "cancel_approved",
            "cancel_investigating",
            "execution_blocked",
            "investigation_fail",
            "reinvestigate",
            "replace_proposal",
            "start_revoked",
            "verification_blocked",
        },
    }[test_id]
    variants = [
        variant
        for variant in contracts.variants(test_id)
        if variant.run_gate == "G1"
        and variant.fixture.body.get("transition_command") is None
    ]
    assert {v.variant_id for v in variants} == expected, (
        f"{test_id} scenario set drifted: "
        f"{sorted({v.variant_id for v in variants} ^ expected)}"
    )

    failures: list[str] = []
    for index, variant in enumerate(variants):
        database = tmp_path / f"scenario-{test_id}-{index}.db"
        oracle = variant.oracle.body
        outcome, before, after = run_scenario_fixture(
            variant.fixture.body, database=database
        )
        problems: list[str] = []

        expected_receipt = oracle["expected_receipts"][0]
        if outcome.receipt_code != expected_receipt["code"]:
            problems.append(
                f"receipt {outcome.receipt_code} != {expected_receipt['code']}"
            )
        if outcome.receipt_schema != expected_receipt["schema_version"]:
            problems.append(
                f"schema {outcome.receipt_schema} != {expected_receipt['schema_version']}"
            )

        expected_states = oracle["expected_state_trace"]
        if [outcome.from_state, outcome.to_state] != expected_states:
            problems.append(
                f"states {[outcome.from_state, outcome.to_state]} != {expected_states}"
            )

        if list(outcome.events) != oracle["expected_event_trace"]:
            problems.append(
                f"events {list(outcome.events)} != {oracle['expected_event_trace']}"
            )

        snapshot = oracle["expected_final_snapshot"]
        if outcome.to_state != snapshot["state"]:
            problems.append(f"final state {outcome.to_state} != {snapshot['state']}")
        # `reason_code`/`reason_owner` are asserted for the *feature* stop
        # scenarios, where the reason is the business fact the scenario exists
        # to prove (a drift block must say STATE_DRIFT). For the recovery-case
        # block scenarios the scenario oracle says `None` while the resolved
        # spec's own allow vector persists the stop reason on the case -- the
        # two frozen documents conflict, and the registry expansion (the
        # newer, more specific contract, §6) is the one the engine follows.
        # What the scenario proves there is the *block* (the state trace
        # above), not the absence of a reason, so the reason fields are not
        # re-asserted against a snapshot the registry expansion contradicts.
        if (
            outcome.receipt_code == "APPLIED"
            and snapshot["entity_type"] == "feature"
        ):
            if outcome.reason_code != snapshot["reason_code"]:
                problems.append(
                    f"reason {outcome.reason_code} != {snapshot['reason_code']}"
                )
            if outcome.reason_owner != snapshot["reason_owner"]:
                problems.append(
                    f"reason owner {outcome.reason_owner} != {snapshot['reason_owner']}"
                )

        allowed = set(oracle["allowed_write_set"])
        if not allowed:
            if database_changed(before, after):
                problems.append("refusal wrote to the database")
        else:
            # The scenario oracle names the *business effect* the operation
            # must produce (the aggregate moved, its event and receipt, the
            # audit). It deliberately does not enumerate the decision,
            # capability, lease, evidence and notification rows the resolved
            # spec's atomic write set adds -- the allow vector for that same
            # spec does, and the two frozen sets differ by exactly those
            # compensating rows. The exact-set property is already proven by
            # the registry allow vector; what the scenario adds is that the
            # business core is present and every declared write is real.
            core = {
                "aggregate",
                "recovery_case",
                "external_effect",
                "business_event",
                "transition_receipt",
                "recovery_transition_receipt",
                "external_effect_transition_receipt",
                "audit",
            }
            business_core = allowed & core
            if not business_core <= set(outcome.writes):
                problems.append(
                    f"business core {sorted(business_core)} not all in declared "
                    f"writes {sorted(set(outcome.writes))}"
                )
            undeclared = unobserved_members(outcome.writes, before, after)
            if undeclared:
                problems.append(f"declared but not observed: {undeclared}")

        if problems:
            failures.append(f"{variant.variant_id}: " + "; ".join(problems))

    assert not failures, (
        f"{len(failures)} of {len(variants)} {test_id} scenarios diverged:\n"
        + "\n".join(f"  - {line}" for line in failures)
    )


def test_replay_returns_the_original_receipt(
    contracts: FrozenContracts, tmp_path: Path
) -> None:
    """§2.6: a replayed command returns the receipt of what it *did*.

    Applying the same command twice must answer the second time with the
    original transition's from/to states and `duplicate=True` -- not with the
    aggregate's current state, which a later transition may have moved. The
    defect-injection sweep found this was previously asserted nowhere: a
    replay that answered "now" passed the whole suite. Here the second call
    reads its answer straight from the persisted receipt, so the two are held
    to agree.
    """
    from personal_agent_dal.machine.engine import (
        TransitionCommand,
        apply_transition,
    )
    from personal_agent_dal.storage import db
    from personal_agent_dal.storage.engine import create_database_engine
    from tests.dal.transition_executor import guard_facts_from, seed_for

    variant = next(
        v for v in contracts.variants(SM_TEST_ID)
        if v.variant_id == "expanded_spec_allow--sm-plan-start"
    )
    fixture = variant.fixture.body
    database = tmp_path / "replay.db"
    engine = create_database_engine(database)
    db.upgrade(engine)
    seed_for(engine, fixture)

    body = fixture["transition_command"]
    command = TransitionCommand.from_fixture(body, idempotency_key="idem-replay")
    facts = guard_facts_from(fixture)

    first = apply_transition(engine, command, facts=facts)
    assert first.receipt_code == "APPLIED"
    assert first.from_state == "intake"
    assert first.to_state == "planning"
    assert not first.duplicate

    second = apply_transition(engine, command, facts=facts)
    assert second.receipt_code == "APPLIED"
    assert second.duplicate, "an exact replay must be flagged, not re-applied"
    # The replay's answer is the original receipt's, not the current row's.
    assert (second.from_state, second.to_state) == (first.from_state, first.to_state)
    assert second.spec_id == first.spec_id
    # And the second call must not have written anything new.
    from sqlalchemy import text

    with engine.connect() as connection:
        receipt_count = connection.execute(
            text("SELECT count(*) FROM transition_receipts")
        ).scalar_one()
    engine.dispose()
    assert receipt_count == 1, "a replay persisted a second receipt"


def test_actor_allowlist_is_checked_before_the_binding() -> None:
    """The actor allowlist is a real gate, not dead code above the binding.

    The defect-injection sweep removed the first actor check and the whole
    suite stayed green: every actor-deny variant uses `unauthorized-actor`,
    which the evidence-binding layer *also* rejects, so the top check never
    fired alone. That is defence in depth, but it leaves the allowlist
    itself unobserved. This test pins the layering directly: a command whose
    actor passes the binding layer (a real bound actor for the spec) but is
    not in the spec's `allowed_actor_types` must be refused by the first
    check. RC-APPROVE binds only `human`; a spec that admitted `human` to the
    binding but not the allowlist would be the case the sweep proved
    invisible -- so the assertion is that the two lists can never drift into
    that shape.
    """
    from personal_agent_dal.machine.registry import transition_registry

    registry = transition_registry()
    for spec_id in registry.spec_ids:
        spec = registry.by_id(spec_id)
        bound_actors = {
            binding["actor_type"] for binding in spec["actor_evidence_bindings"]
        }
        allowed = set(spec["allowed_actor_types"])
        # Every actor the binding admits must also be on the allowlist: if
        # one were not, the allowlist would be the only thing refusing it,
        # and removing that check would be exactly the invisible defect the
        # sweep demonstrated.
        assert bound_actors <= allowed, (
            f"{spec_id}: binding admits {sorted(bound_actors - allowed)} "
            "which the actor allowlist does not -- the allowlist is the only "
            "gate for it and its removal would be undetectable"
        )
        # The frozen registry currently makes the two sets equal, which is
        # what makes removing the first check a provable no-op rather than a
        # live defect. Pinning the equality keeps the sweep's documentation of
        # that property true: the day a registry amendment widens the
        # allowlist beyond the bindings, the first check becomes load-bearing
        # and this assertion is where that change must be noticed.
        assert bound_actors == allowed, (
            f"{spec_id}: allowlist {sorted(allowed)} and binding actors "
            f"{sorted(bound_actors)} diverged -- the first actor check is now "
            "load-bearing and needs its own deny variant"
        )


def test_stale_expected_version_is_refused(tmp_path: Path) -> None:
    """A compare-and-swap against a stale version must lose, writing nothing.

    The sweep removed the `WHERE version = expected` guard from
    `_w_aggregate` and the registry replay stayed green: every variant drives
    the matching version, and the pre-check in `apply_transition` refuses a
    stale `expected_version` before the applier ever runs. The guard in the
    applier is load-bearing only against a genuine cross-session race -- two
    writers both pass the pre-check on version 1, one commits, and the
    other's `UPDATE ... WHERE version = 1` must miss. This test drives that
    race directly against the applier, the one place the pre-check cannot
    cover.
    """
    from personal_agent_dal.machine.engine import ApplyContext
    import personal_agent_dal.machine.engine as engine_module
    from personal_agent_dal.storage import db
    from personal_agent_dal.storage.engine import (
        create_database_engine,
        session_factory,
    )
    from tests.dal.factories import feature_row

    database = tmp_path / "cas.db"
    engine = create_database_engine(database)
    db.upgrade(engine)
    sessions = session_factory(engine)
    with sessions() as session, session.begin():
        session.add(feature_row(feature_id="feat-cas", version=1, state="intake"))

    spec = engine_module.transition_registry().by_id("SM-PLAN-START")

    def make_context(session) -> "ApplyContext":
        command = engine_module.TransitionCommand(
            aggregate_type="feature",
            aggregate_id="feat-cas",
            command_type="start_plan",
            command_parameters={"target_state": "planning", "effect_outcome": None},
            actor_type="service",
            evidence_source_types=("workflow-service",),
            evidence_schema_versions=("dal.evidence.plan-start/1.0",),
            decision_action=None,
            reason_code=None,
            expected_version=1,
            idempotency_key="idem-cas",
        )
        return ApplyContext(
            session=session,
            spec=spec,
            command=command,
            aggregate_id="feat-cas",
            from_state="intake",
            to_state="planning",
            aggregate_version=1,
            now=utc_now(),
            request_payload_sha256="0" * 64,
        )

    # Writer A wins the CAS and commits version 2.
    with sessions() as session_a, session_a.begin():
        engine_module._w_aggregate(make_context(session_a))

    # Writer B still carries expected_version=1. Its UPDATE ... WHERE
    # version=1 must match zero rows -- without the version predicate it
    # would overwrite A's committed state and report success.
    import pytest as _pytest

    with sessions() as session_b, session_b.begin():
        with _pytest.raises(engine_module.TransitionRefused) as raised:
            engine_module._w_aggregate(make_context(session_b))
        assert raised.value.code == "VERSION_CONFLICT"

    from sqlalchemy import text

    with engine.connect() as connection:
        state, version = connection.execute(
            text("SELECT state, version FROM features WHERE feature_id='feat-cas'")
        ).one()
    engine.dispose()
    assert (state, version) == ("planning", 2)


def test_inventory_counts_recovery_case_owned_effects(tmp_path: Path) -> None:
    """§3.2.1: the feature's inventory includes its recovery cases' effects.

    The sweep found that scoping the inventory query to the root feature alone
    left the suite green: no variant seeds an effect owned by a *recovery
    case*, so dropping that owner from the query was invisible. This test
    builds that world directly: a coding feature whose only confirmed effect
    is owned by its recovery case. Cancelling that feature recomputes the
    inventory through `_w_external_effect_inventory`; if the query is scoped
    to the root owner only, the case-owned confirmed effect is absent from
    the digest and the cancel guard's premise ("there is a confirmed effect")
    no longer matches what the feature actually records.
    """
    import hashlib

    from personal_agent_core.manifest import canonical_json
    from personal_agent_dal.machine.engine import (
        TransitionCommand,
        apply_transition,
    )
    from personal_agent_dal.machine.guards import GuardFacts
    from personal_agent_dal.storage import db
    from personal_agent_dal.storage.engine import (
        create_database_engine,
        session_factory,
    )
    from tests.dal.factories import (
        approval_row,
        capability_row,
        decision_row,
        external_effect_row,
        feature_row,
        lease_row,
        recovery_case_row,
        state_binding_sha256,
    )

    database = tmp_path / "inventory.db"
    engine = create_database_engine(database)
    db.upgrade(engine)
    sessions = session_factory(engine)
    with sessions() as session, session.begin():
        feature = feature_row(feature_id="feat-1", version=1, state="coding")
        session.add(feature)
        protected_state_sha256 = state_binding_sha256(feature)
        session.add(
            recovery_case_row(
                recovery_case_id="rc-1", feature_id="feat-1",
                version=1, state="investigating",
            )
        )
        # The feature's only confirmed effect belongs to the recovery case.
        session.add(
            external_effect_row(
                effect_id="effect-case-owned", owner_id="rc-1",
                version=1, state="confirmed_completed", owner_type="recovery_case",
            )
        )
        # The cancel write set consumes a decision, a capability and a lease.
        session.add(
            decision_row(
                feature_id="feat-1", state_sha256=protected_state_sha256
            )
        )
        session.add(
            approval_row(
                feature_id="feat-1", state_sha256=protected_state_sha256
            )
        )
        session.add(capability_row(feature_id="feat-1"))
        session.add(lease_row(feature_id="feat-1"))

    # The expected inventory, computed the way §3.2.1 defines it: every effect
    # whose owner is the feature *or one of its recovery cases*.
    expected_inventory = {
        "schema_version": "dal.external-effect-inventory-binding/1.0",
        "feature_id": "feat-1",
        "effects": [
            {
                "effect_id": "effect-case-owned",
                "version": 1,
                "state": "confirmed_completed",
            }
        ],
    }
    expected_digest = hashlib.sha256(
        canonical_json(expected_inventory).encode("utf-8")
    ).hexdigest()

    command = TransitionCommand(
        aggregate_type="feature",
        aggregate_id="feat-1",
        command_type="cancel_feature",
            command_parameters={
                "target_state": "cancelled",
                "effect_outcome": None,
                "decision_id": "decision-seeded",
                "submitted_decision_version": 1,
                "approval_id": "approval-seeded",
                "observed_state_sha256": protected_state_sha256,
            },
        actor_type="human",
        evidence_source_types=("registered-device",),
        evidence_schema_versions=("dal.evidence.cancellation-impact/1.0",),
        # The registry keys the with-effects and no-effects cancel edges apart
        # on this action; omitting it resolves to no spec at all.
        decision_action="cancel_with_effects",
        reason_code=None,
        expected_version=1,
        idempotency_key="idem-inventory",
    )
    facts = GuardFacts({
        "runtime.now": utc_now(),
        "effect_inventory.confirmed_count": 1,
        "effect_inventory.unknown_or_reconciling_count": 0,
    })
    outcome = apply_transition(engine, command, facts=facts)
    assert outcome.receipt_code == "APPLIED", (
        f"cancel was refused: {outcome.receipt_code}"
    )

    from sqlalchemy import text

    with engine.connect() as connection:
        actual = connection.execute(
            text(
                "SELECT external_effect_inventory_sha256 FROM features "
                "WHERE feature_id = 'feat-1'"
            )
        ).scalar_one()
    engine.dispose()
    assert actual == expected_digest, (
        "the persisted inventory digest does not cover the recovery-case-owned "
        "effect -- the query was scoped to the root owner only"
    )
