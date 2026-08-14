"""Executes frozen hash-binding fixtures against the real binding validators.

`DAL-T-STATEHASH-001` and `DAL-T-ARTIFACTHASH-001` prove the server recomputes
the RFC 8785 JCS digest of a submitted state/artifact binding and compares it
to the protected digest an approval/decision was bound to. Every frozen G1
variant is a refusal: the submitted binding diverges from the protected one in
exactly one dimension, and the oracle expects `DECISION_STALE` (state) or
`APPROVAL_INVALID` (artifact) with zero writes.

The executor does not reimplement the hash comparison. It seeds the feature
row, dispatches to the real production validator by the fixture's business
command (`consume_decision` -> `validate_state_binding`,
`consume_artifact_approval` -> `validate_artifact_binding`), and records the
outcome faithfully. The comparator — not the executor — fails a wrong receipt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import text

from personal_agent_dal.machine.binding import (
    ArtifactReadback,
    validate_artifact_binding,
    validate_state_binding,
)
from personal_agent_dal.machine.engine import (
    RECEIPT_SCHEMAS,
    ReceiptCodes,
    TransitionRefused,
)
from personal_agent_dal.storage import db
from personal_agent_dal.storage.engine import create_database_engine, session_factory

from tests.dal.operation_executor import ExecutionTrace, ReceiptRecord
from tests.dal.side_effects import SideEffectProbe


class UnsupportedCommandError(RuntimeError):
    """A fixture command the binding harness has no validator for."""


#: A hash-binding refusal reports the command aggregate's transition receipt,
#: the same schema `apply_transition` uses for a refused feature transition.
RECEIPT_SCHEMA: str = RECEIPT_SCHEMAS["feature"]


def _seed_feature(engine: Any, fixture_body: dict[str, Any]) -> None:
    from tests.dal.factories import feature_row

    target = fixture_body["operation_sequence"][0]["input"]["target"]
    sessions = session_factory(engine)
    with sessions() as session, session.begin():
        session.add(
            feature_row(
                feature_id=target["entity_id"],
                version=target["version"],
                state=target["state"],
            )
        )


def execute_binding_fixture(
    fixture_body: dict[str, Any],
    *,
    database: Path,
    probe: SideEffectProbe | None = None,
) -> ExecutionTrace:
    """Seed, run the binding validator, and record the receipt and zero writes.

    All frozen G1 variants diverge, so the validator always refuses. A variant
    that unexpectedly matched would fall through to ``APPLIED`` and fail the
    oracle's ``DECISION_STALE``/``APPROVAL_INVALID`` expectation loudly, rather
    than being folded into the expected code to force a pass.
    """
    engine = create_database_engine(database)
    db.upgrade(engine)
    _seed_feature(engine, fixture_body)

    op = fixture_body["operation_sequence"][0]
    target = op["input"]["target"]
    facts = op["input"]["authoritative_facts"]
    action = op["input"]["action_sequence"][0]["command"]

    trace = ExecutionTrace(probe=probe)
    trace.state_trace.append(target["state"])
    trace.final_entity_type = target["entity_type"]

    try:
        if action == "consume_decision":
            validate_state_binding(
                current_binding=facts["current_binding"],
                protected_binding_sha256=facts["protected_binding_sha256"],
                observed_binding_sha256=facts["observed_binding_sha256"],
            )
            code = ReceiptCodes.APPLIED
        elif action == "consume_artifact_approval":
            validate_artifact_binding(
                current_artifact=ArtifactReadback(
                    binding=facts["current_artifact"],
                    canonical_body=op["input"]["injected_results"][0][
                        "canonical_body_utf8"
                    ].encode("utf-8"),
                ),
                protected_binding_sha256=facts["protected_binding_sha256"],
                observed_binding_sha256=facts["observed_binding_sha256"],
            )
            code = ReceiptCodes.APPLIED
        else:
            raise UnsupportedCommandError(
                f"no binding validator for command: {action!r}"
            )
    except TransitionRefused as refusal:
        code = refusal.code

    trace.receipts.append(
        ReceiptRecord(code=code, schema_version=RECEIPT_SCHEMA)
    )
    # A binding refusal leaves the feature exactly where it was, and writes
    # nothing at all — the validator is a pure comparison, not a transition.
    trace.final_state = target["state"]
    trace.state_trace.append(target["state"])

    engine.dispose()
    return trace


def binding_persisted_divergences(
    database: Path, fixture_body: dict[str, Any]
) -> list[str]:
    """A refusal must leave the database untouched: the seeded feature is the
    only row, unchanged, and no transition/decision/approval row appeared."""
    problems: list[str] = []
    engine = create_database_engine(database)
    try:
        target = fixture_body["operation_sequence"][0]["input"]["target"]
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT state, version FROM features WHERE feature_id = :id")
                .bindparams(id=target["entity_id"])
            ).first()
            if row is None:
                problems.append("feature row missing")
            elif row[0] != target["state"] or row[1] != target["version"]:
                problems.append(
                    f"feature changed on refusal: ({target['state']!r}, "
                    f"v{target['version']}) -> ({row[0]!r}, v{row[1]})"
                )
            for table in (
                "transition_receipts",
                "events",
                "decisions",
                "approvals",
                "audit_events",
                "outbox_events",
                "evidence_records",
                "impact_reports",
            ):
                count = conn.execute(
                    text(f"SELECT count(*) FROM {table}")  # noqa: S608
                ).scalar_one()
                if count != 0:
                    problems.append(
                        f"{table} has {count} rows after a zero-write refusal"
                    )
    finally:
        engine.dispose()
    return problems
