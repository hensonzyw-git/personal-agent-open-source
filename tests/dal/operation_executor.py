"""Executes frozen DAL operation fixtures against real component entry points.

This is the harness half of "tests-first against the frozen contracts": it does
not reimplement the component's policy. It reads a fixture's `dal.operation-
input/1.0` payload, drives the real production entry point (for DAL-007, the
`ConfigLoader`/`ConfigPolicy`), and reduces whatever happened into an
`ExecutionTrace` the oracle comparator can judge.

The separation matters. The production code decides whether the load is in
policy; the executor only records the outcome faithfully. If the component
silently loaded a config it should have refused, the trace records `loaded`
plus an `APPLIED` receipt, and the comparator -- not the executor -- fails it
against the oracle's `POLICY_DENIED` expectation. The executor never turns a
real outcome into the expected one.

Test-only module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from personal_agent_dal.config import ConfigLoader, ConfigPolicy
from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.receipt import (
    OPERATION_RECEIPT_SCHEMA,
    OperationReceipt,
    ReceiptCode,
)

from tests.dal.side_effects import SideEffectProbe


@dataclass(frozen=True)
class ReceiptRecord:
    """One emitted receipt, in the shape the oracle asserts against.

    `duplicate` and `receipt_id` are only populated by the state-machine
    executors, where an idempotent replay returns the *original* receipt
    (§2.6): the oracle freezes `duplicate_flags` and `unique_receipt_ids`, and
    the comparator needs to see them to assert them.
    """

    code: str
    schema_version: str
    duplicate: bool = False
    receipt_id: str | None = None


@dataclass
class ExecutionTrace:
    """What actually happened when a fixture's operation sequence ran.

    Every field defaults to the empty/no-op outcome, so a refused operation
    leaves a trace that can only match an oracle expecting a refusal.
    """

    state_trace: list[str] = field(default_factory=list)
    receipts: list[ReceiptRecord] = field(default_factory=list)
    #: What the database was **observed** to have changed. For anything with a
    #: real database this is measured before and after, never taken from the
    #: code under test — a trace assembled from the implementation's own
    #: account of itself can only ever confirm it.
    write_set: list[str] = field(default_factory=list)
    #: What the implementation *said* it wrote. Compared against `write_set` by
    #: the caller, so a handler that claims a write it never made is caught.
    declared_write_set: list[str] = field(default_factory=list)
    event_trace: list[str] = field(default_factory=list)
    external_effect_trace: list[str] = field(default_factory=list)
    #: Scenario metrics the frozen `scenario_assertions` clauses are judged
    #: against (e.g. `business_event_count`, `aggregate_version_increment`).
    metrics: dict[str, Any] = field(default_factory=dict)
    final_state: str = ""
    #: The command aggregate's persisted stop reason and its owner, as written
    #: by the transition. The oracle freezes both in `expected_final_snapshot`
    #: and both must be judged, not only the state.
    final_reason_code: str | None = None
    final_reason_owner: str | None = None
    #: The command aggregate's type, for the oracle's `entity_type` check.
    final_entity_type: str = ""
    #: The companion transition identities observed in the sequence (§2.3.1);
    #: the oracle freezes them as `expected_atomic_companion_transitions`.
    companion_ids: list[str] = field(default_factory=list)
    probe: SideEffectProbe | None = None


class UnsupportedCommandError(RuntimeError):
    """A fixture command the harness has no executor for. Never a silent pass."""


def _receipt_for_error(error: DalError) -> ReceiptRecord:
    code = error.code
    if code is DalErrorCode.CONFIG_POLICY_DENIED:
        return ReceiptRecord(code=ReceiptCode.POLICY_DENIED.value,
                             schema_version=OPERATION_RECEIPT_SCHEMA)
    # Any other refusal is surfaced as its own code so the comparator can
    # distinguish "denied by policy" from "unavailable"; it is never folded
    # into the expected code to force a pass.
    return ReceiptRecord(code=code.value, schema_version=OPERATION_RECEIPT_SCHEMA)


def execute_config_load(
    command: dict[str, Any],
    *,
    loader: ConfigLoader,
    probe: SideEffectProbe,
) -> ExecutionTrace:
    """Run one `load_declared_service_config` operation against the real loader.

    The fixture's `authoritative_facts` are the declared inputs a real config
    load would carry: which module the config belongs to, which names were
    requested, and (for a secret file) its path and OS mode. The executor maps
    each onto the corresponding policy check in the same order a real load
    would apply them, then records the resulting state and receipt.
    """
    facts = command["input"]["authoritative_facts"]
    target = command["input"]["target"]
    trace = ExecutionTrace(probe=probe)

    # The pre-state is the entity's starting point, recorded so the state
    # trace always begins where the fixture said the entity was.
    current_state = target["state"]
    trace.state_trace.append(current_state)

    policy: ConfigPolicy = loader.policy
    try:
        # 1. Module namespace (finance_import lives here).
        policy.check_module(facts["module"])

        # 2. Requested secret names (production_credential lives here).
        for secret_name in facts.get("requested_secret_names", []):
            policy.check_secret_name(secret_name)

        # 3. Requested config names (unknown_config lives here).
        for config_name in facts.get("requested_config_names", []):
            policy.check_config_name(config_name)

        # 4. Secret file mode (insecure_secret_file lives here). The fixture
        #    supplies the mode as metadata; the policy normalises and judges it.
        if "secret_file" in facts:
            policy.check_secret_file_mode(
                facts["secret_file"], facts.get("secret_file_mode")
            )

    except DalError as error:
        # A refused load leaves the entity where it was and writes nothing.
        trace.receipts.append(_receipt_for_error(error))
        trace.final_state = current_state
        trace.state_trace.append(current_state)
        return trace

    # Reaching here means the load was in policy. For the frozen DAL-007
    # variants this path is never expected; if it is reached the trace records
    # an APPLIED receipt and the comparator fails it against POLICY_DENIED.
    trace.receipts.append(
        ReceiptRecord(code=ReceiptCode.APPLIED.value,
                      schema_version=OPERATION_RECEIPT_SCHEMA)
    )
    trace.final_state = "loaded"
    trace.state_trace.append("loaded")
    return trace


#: The commands the DAL-007–013 harness knows how to execute. An operation
#: whose command is absent is a harness gap, surfaced as UnsupportedCommandError
#: rather than silently scored.
def _seed_database(
    database: Path, command: dict[str, Any]
) -> "tuple[Any, Any]":
    """Build the fixture's pre-state in a real database, from its facts alone.

    The seeding reads `pre_state` and `authoritative_facts` only. It never
    branches on `variant_id`: a harness that recognised the variant it is
    replaying could arrange exactly the conditions that variant needs and would
    stop being an independent check. Each fact key maps to one piece of
    pre-state, so an unfamiliar fact combination seeds what it describes.
    """
    from personal_agent_core.crypto import KeyRing, generate_key
    from personal_agent_core.timeutil import parse_rfc3339
    from personal_agent_dal.storage import db
    from personal_agent_dal.storage.engine import (
        create_database_engine,
        session_factory,
    )
    from tests.dal.factories import event_row, feature_row, operation_receipt_row

    target = command["input"]["target"]
    facts = command["input"]["authoritative_facts"]
    engine = create_database_engine(database)

    # `empty` means the schema has never been applied; the migration under test
    # is what creates it. Anything else starts from a fully migrated database.
    if target["state"] != "empty":
        db.upgrade(engine)

    keyring = None
    if "encryption_key_ref" in facts:
        # An ephemeral key, generated per run and never written to disk. The
        # real `keyref://dal/data/v1` is deployment-time work and deliberately
        # outside this slice; a test that needed the production key would be
        # reaching for a credential this authorisation excludes.
        keyring = KeyRing(
            [generate_key(facts["encryption_key_ref"])], service="dal"
        )

    if target["state"] == "empty":
        return engine, keyring

    sessions = session_factory(engine)
    with sessions() as session, session.begin():
        if "current_version" in facts:
            session.add(
                feature_row(
                    feature_id=target["entity_id"],
                    version=facts["current_version"],
                )
            )
        if "existing_payload_sha256" in facts:
            for step in command["input"]["action_sequence"]:
                if "idempotency_key" not in step:
                    continue
                session.add(
                    operation_receipt_row(
                        idempotency_key=step["idempotency_key"],
                        request_payload_sha256=facts["existing_payload_sha256"],
                    )
                )
        for record in facts.get("records", []):
            session.add(
                event_row(
                    event_id=record["id"],
                    occurred_at=parse_rfc3339(record["created_at"]),
                )
            )
    return engine, keyring


#: Row-count growth in these tables maps straight onto one write class.
_COUNTED_TABLE_LABELS: dict[str, str] = {
    "migration_receipts": "migration_receipt",
    "operation_receipts": "operation_receipt",
    "audit_events": "audit",
    "retention_tombstones": "retention_tombstone",
}


def _snapshot(engine: Any) -> dict[str, Any]:
    """Everything about the database that a write could change."""
    from sqlalchemy import inspect, text

    tables = set(inspect(engine).get_table_names())
    counts: dict[str, int] = {}
    sealed = 0
    revision = None
    features: dict[str, int] = {}
    with engine.connect() as connection:
        for table in tables:
            counts[table] = connection.execute(
                text(f"SELECT count(*) FROM {table}")  # noqa: S608 - names from the schema
            ).scalar_one()
        if "events" in tables:
            sealed = connection.execute(
                text("SELECT count(*) FROM events WHERE encrypted_payload IS NOT NULL")
            ).scalar_one()
        if "alembic_version" in tables:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()
        if "features" in tables:
            features = {
                row[0]: row[1]
                for row in connection.execute(
                    text("SELECT feature_id, version FROM features")
                )
            }
    return {
        "tables": tables,
        "counts": counts,
        "sealed": sealed,
        "revision": revision,
        "features": features,
    }


def _observed_writes(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Derive the oracle's write-class labels from what actually changed.

    The labels are write *classes*, not table names (design draft §3), so one
    class can span more than one physical change: `retention_tombstone` covers
    the deletion together with the tombstone that records it. A deletion that
    arrives without its tombstone therefore does not quietly reduce to the
    allowed label — it produces `unrecorded_deletion`, which appears in no
    oracle and fails.
    """
    labels: list[str] = []

    if after["revision"] != before["revision"] or after["tables"] != before["tables"]:
        labels.append("schema")

    for table, label in _COUNTED_TABLE_LABELS.items():
        if after["counts"].get(table, 0) > before["counts"].get(table, 0):
            labels.append(label)

    if after["sealed"] > before["sealed"]:
        labels.append("encrypted_record")

    deleted = before["counts"].get("events", 0) - after["counts"].get("events", 0)
    if deleted > 0:
        tombstoned = after["counts"].get("retention_tombstones", 0) - before[
            "counts"
        ].get("retention_tombstones", 0)
        if tombstoned != deleted:
            labels.append("unrecorded_deletion")

    changed_features = [
        feature_id
        for feature_id, version in after["features"].items()
        if before["features"].get(feature_id) != version
    ]
    if changed_features:
        labels.append("feature_state")

    return labels


def execute_db_contract(
    command: dict[str, Any],
    *,
    database: Path,
    probe: SideEffectProbe,
) -> ExecutionTrace:
    """Run one `apply_database_contract` command against a real database.

    The real production entry point decides everything. The fixture's
    `injected_results` are deliberately **not** used to force an outcome: a
    real SQLite CAS miss, a real unique-constraint conflict and a real AEAD
    round-trip are stronger evidence than a stub reporting the status the
    fixture predicted.

    The write set is measured from the database on either side of the call.
    Recording the outcome's own `writes` instead would make the fixture replay
    a fake with the same assumptions as the code: an injected defect that
    deleted rows and skipped the tombstone left every oracle green, because the
    handler still declared the write it had stopped making.
    """
    from personal_agent_dal.storage.operations import apply_database_contract

    engine, keyring = _seed_database(database, command)
    target = command["input"]["target"]
    trace = ExecutionTrace(probe=probe)
    trace.state_trace.append(target["state"])

    before = _snapshot(engine)
    outcome = apply_database_contract(engine, command, keyring=keyring)
    after = _snapshot(engine)

    trace.receipts.append(
        ReceiptRecord(
            code=outcome.receipt.code.value,
            schema_version=outcome.receipt.schema_version,
        )
    )
    trace.write_set.extend(_observed_writes(before, after))
    trace.declared_write_set.extend(outcome.writes)
    trace.event_trace.extend(outcome.events)
    trace.final_state = outcome.entity_state
    trace.state_trace.append(outcome.entity_state)
    return trace


#: Actions belonging to `OP-DB-CONTRACT-001` (DAL-008). One command carries the
#: whole action sequence and produces exactly one receipt, so these are
#: dispatched as a group rather than one executor per action.
_DB_CONTRACT_ACTIONS: frozenset[str] = frozenset(
    {
        "apply_migration",
        "update_aggregate",
        "insert_operation_receipt",
        "apply_retention",
        "write_encrypted_record",
        "read_encrypted_record",
    }
)


def execute_operation_command(
    command: dict[str, Any],
    *,
    probe: SideEffectProbe,
    loader: ConfigLoader | None = None,
    database: "Path | None" = None,
) -> ExecutionTrace:
    """Dispatch one `dal.test-operation-command/1.0` to its executor."""
    action_sequence = command["input"]["action_sequence"]
    if not action_sequence:
        raise UnsupportedCommandError("command carries no action")
    actions = {step["command"] for step in action_sequence}

    if actions <= _DB_CONTRACT_ACTIONS:
        if database is None:
            raise UnsupportedCommandError(
                "database contract commands need a database path"
            )
        return execute_db_contract(command, database=database, probe=probe)

    if len(action_sequence) != 1:
        raise UnsupportedCommandError(
            f"expected exactly one action, got {len(action_sequence)}"
        )
    action = action_sequence[0]["command"]

    if action == "load_declared_service_config":
        if loader is None:
            loader = ConfigLoader()
        return execute_config_load(command, loader=loader, probe=probe)

    raise UnsupportedCommandError(f"no executor for command: {action!r}")


def execute_fixture(
    fixture_body: dict[str, Any],
    *,
    probe: SideEffectProbe,
    loader: ConfigLoader | None = None,
    database: "Path | None" = None,
) -> ExecutionTrace:
    """Execute a fixture's full operation sequence and merge the traces.

    The DAL-007 variants each carry exactly one operation command; the loop is
    written for the general sequence shape so later waves with multi-command
    sequences reuse it. The merged trace accumulates states, receipts and
    write sets in order; the final state is the last operation's.
    """
    merged = ExecutionTrace(probe=probe)
    for command in fixture_body["operation_sequence"]:
        trace = execute_operation_command(
            command, probe=probe, loader=loader, database=database
        )
        merged.state_trace.extend(trace.state_trace)
        merged.receipts.extend(trace.receipts)
        merged.write_set.extend(trace.write_set)
        merged.declared_write_set.extend(trace.declared_write_set)
        merged.event_trace.extend(trace.event_trace)
        merged.external_effect_trace.extend(trace.external_effect_trace)
        merged.final_state = trace.final_state
    return merged
