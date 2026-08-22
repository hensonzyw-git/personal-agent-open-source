"""`OP-VERIFICATION-CONTRACT-001`: deterministic verification boundary (DAL-029, G3).

Freeze package `docs/dal/DAL029_确定性验证流水线_合同冻结包_v0.1.md` fixes the
verification consumption rule: a verification run is the five registered
commands (`diff` capture + `format`/`lint`/`build`/`test`), each declared in the
repository's registry and each reporting an exit code. The verifier's own
failure vocabulary is exactly `succeeded` / `task_failure` (the two new words it
adds), reusing `contract_failure` / `policy_failure` for the two fail-closed
integrity checks. `task_failure` maps to `TEST_BLOCKED` -> `blocked_test`, which
is the single reason code the coder adapter deliberately omits.

`consume_verification` is the pure decision. It classifies the untrusted run
report into one outcome of the frozen vocabulary — `succeeded`, `blocked`
(`task_failure`), or `failed` (`contract_failure` / `policy_failure`). The
trusted half (command envelope, target, the registry commands, base SHA, the
`injected_results` container shape) validates as closed shapes and raises
`DalError(INVALID_ARGUMENT)` on drift. The untrusted values — the diff, its
claimed hash and base, and the per-stage observed command and exit code — never
raise: drift is classified fail-closed, never a crash.

The decision precedence (§6 of the freeze package) is: registry integrity
(a swapped command is the positive half of `injection.py`'s refusal, so it runs
before any correctness check — §5.2 leak-before-correctness), then diff
integrity, then stage exit codes. The report — `commands`, `exit_codes`,
`report_hash = sha256(JCS(report body))` and an `APPLIED` check receipt — is
produced unconditionally as a faithful record of what ran; the verdict is
separate from the record.

The function performs no I/O: the actual `git diff` capture, the
`execute_toolchain` run and persistence belong to `worker/verification.py`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Final

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.receipt import OperationReceipt, ReceiptCode

OPERATION_SPEC_ID: Final[str] = "OP-VERIFICATION-CONTRACT-001"
COMMAND_TYPE: Final[str] = "consume_verification"
SERVICE_ACTOR: Final[str] = "service"
EVIDENCE_SOURCE: Final[str] = "verification-adapter"
CONTRACT_VERSION: Final[str] = "dal.verification-report/1.0"

FEATURE_TRANSITION_RECEIPT_SCHEMA: Final[str] = "dal.transition-receipt/1.0"

#: Frozen block write set (§5 of the freeze package) — a classified failure is
#: the operation succeeding at blocking, not a refusal.
BLOCK_WRITE_SET: Final[tuple[str, ...]] = (
    "aggregate",
    "business_event",
    "transition_receipt",
    "audit",
    "decision_create",
    "decision_projection",
    "notification_outbox",
)
BLOCK_EVENT: Final[str] = "feature.blocked"

#: The write set for a *successful* verification: the feature moves
#: `verifying -> verified`, writing the aggregate (`state` + `last_verified_sha`
#: = base_sha) and the transition receipt. No block event, no checkpoint.
VERIFIED_WRITE_SET: Final[tuple[str, ...]] = ("aggregate", "transition_receipt")

#: The five registered stages, in the worker's fixed order. `diff` is the patch
#: capture; the other four are the toolchain checks whose failure is a
#: `task_failure`.
REGISTRY_STAGES: Final[tuple[str, ...]] = ("diff", "format", "lint", "build", "test")
CHECK_STAGES: Final[tuple[str, ...]] = ("format", "lint", "build", "test")

#: A single stage result's frozen shape: the command argv and its exit code.
STAGE_RESULT_FIELDS: Final[frozenset[str]] = frozenset({"command", "exit_code"})

#: `failure_class` -> `reason_code` (storage FEATURE_REASON_CODES) -> feature
#: state. `task_failure` is the verifier's own new word; `contract_failure` /
#: `policy_failure` are the reused fail-closed integrity classes.
FAILURE_TO_REASON: Final[dict[str, str]] = {
    "task_failure": "TEST_BLOCKED",
    "contract_failure": "PROVIDER_CONTRACT_FAILURE",
    "policy_failure": "POLICY_FAILURE",
}
REASON_TO_STATE: Final[dict[str, str]] = {
    "TEST_BLOCKED": "blocked_test",
    "PROVIDER_CONTRACT_FAILURE": "needs_human",
    "POLICY_FAILURE": "needs_human",
}

REQUIRED_INPUT_SECTIONS: Final[frozenset[str]] = frozenset(
    {"target", "action_sequence", "authoritative_facts", "injected_results"}
)
COMMAND_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "actor_type",
        "evidence_source_type",
        "idempotency_key",
        "input",
        "operation_id",
        "operation_spec_id",
        "schema_version",
    }
)
INPUT_FIELDS: Final[frozenset[str]] = REQUIRED_INPUT_SECTIONS | {"schema_version"}
ACTION_FIELDS: Final[frozenset[str]] = frozenset({"command", "contract_version"})
TARGET_FIELDS: Final[frozenset[str]] = frozenset(
    {"entity_id", "entity_type", "state", "version"}
)

#: The authoritative (trusted) facts of a verification run (§3): the base SHA
#: the diff was taken against, the registered commands, and the last SHA that
#: was verified before this run (preserved on failure).
FACT_FIELDS: Final[frozenset[str]] = frozenset(
    {"base_sha", "registry_commands", "prior_last_verified_sha"}
)

#: The untrusted `injected_results` container (§4). The container shape is
#: trusted (drift raises); the values it carries are the worker's report of the
#: run and are classified, never trusted.
INJECTED_FIELDS: Final[frozenset[str]] = frozenset(
    {"diff", "diff_sha", "diff_base_sha", "stage_results"}
)

_HEX: Final[str] = "0123456789abcdef"


@dataclass(frozen=True)
class VerificationReport:
    """The deterministic verification report (§7): commands, exit codes, their
    JCS-bound `report_hash`, and the `APPLIED` check receipt.

    `commands` and `exit_codes` are the clean, string-keyed view of the observed
    stages, with `None` for a missing or malformed stage. `report_hash` binds
    `base_sha` + `diff_sha` + the *raw* observed `stage_results` (nothing
    dropped — non-string keys and non-dict values are recorded faithfully), so
    two different observations can never share a hash.
    """

    commands: dict[str, Any]
    exit_codes: dict[str, Any]
    report_hash: str
    check_receipt: OperationReceipt


@dataclass(frozen=True)
class VerificationEvaluation:
    """The complete observable result of the pure verification decision.

    `result_status` / `failure_class` are the verification-specific outcome pair
    the executor carries alongside the generic trace; the oracle freezes them as
    `expected_result_status` / `expected_failure_class`. `last_verified_sha`
    advances to `base_sha` on success and stays at the prior value otherwise;
    the oracle freezes it as `expected_last_verified_sha`. `report` carries the
    report whose `report_hash` the oracle freezes as `expected_report_hash`.
    """

    receipt: OperationReceipt
    state_trace: tuple[str, ...]
    final_state: str
    final_entity_type: str
    result_status: str
    failure_class: str | None
    last_verified_sha: str | None
    report: VerificationReport
    final_reason_code: str | None = None
    final_reason_owner: str | None = None
    declared_write_set: tuple[str, ...] = ()
    event_trace: tuple[str, ...] = ()
    external_effect_trace: tuple[str, ...] = ()
    contract_failure_reasons: tuple[str, ...] = ()


def _invalid(detail: str) -> DalError:
    return DalError(DalErrorCode.INVALID_ARGUMENT, internal_detail=detail)


def _is_sha40_hex(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 40 and all(c in _HEX for c in value)


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_argv(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(token, str) and token for token in value)
    )


def _canonical_json(value: Any) -> str:
    """Stable serialisation for the report hash. RFC 8785-compatible for the
    ASCII-only report body (no floats, no non-ASCII), matching `dal_jcs.py`."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validate_command(command: dict[str, Any]) -> None:
    """Validate the trusted envelope; raise `DalError` on any drift."""
    if not isinstance(command, dict):
        raise _invalid("command must be an object")
    if frozenset(command) != COMMAND_FIELDS:
        raise _invalid("command shape is not closed")
    if command.get("schema_version") != "dal.test-operation-command/1.0":
        raise _invalid("wrong command schema")
    if not isinstance(command.get("operation_id"), str) or not command["operation_id"]:
        raise _invalid("operation_id must be a non-empty string")
    if not isinstance(command.get("idempotency_key"), str) or not command["idempotency_key"]:
        raise _invalid("idempotency_key must be a non-empty string")
    if command.get("operation_spec_id") != OPERATION_SPEC_ID:
        raise _invalid("wrong verification-contract spec")
    if command.get("actor_type") != SERVICE_ACTOR:
        raise DalError(DalErrorCode.ACTOR_NOT_ALLOWED)
    if command.get("evidence_source_type") != EVIDENCE_SOURCE:
        raise DalError(DalErrorCode.SCOPE_DENIED)

    payload = command.get("input")
    if not isinstance(payload, dict):
        raise _invalid("input must be an object")
    if frozenset(payload) != INPUT_FIELDS:
        raise _invalid("input shape is not closed")
    if payload.get("schema_version") != "dal.operation-input/1.0":
        raise _invalid("wrong input schema")

    target = payload.get("target")
    if not isinstance(target, dict) or frozenset(target) != TARGET_FIELDS:
        raise _invalid("target shape is not closed")
    if (
        not isinstance(target.get("entity_id"), str)
        or not target["entity_id"]
        or target.get("entity_type") != "feature"
        or target.get("state") != "verifying"
        or not _is_non_negative_int(target.get("version"))
    ):
        raise _invalid("verification-contract target must be a feature in verifying")

    action_sequence = payload.get("action_sequence")
    if not isinstance(action_sequence, list) or len(action_sequence) != 1:
        raise _invalid("action sequence must contain exactly one step")
    step = action_sequence[0]
    if not isinstance(step, dict) or frozenset(step) != ACTION_FIELDS:
        raise _invalid("unexpected action sequence shape")
    if step.get("command") != COMMAND_TYPE or step.get("contract_version") != CONTRACT_VERSION:
        raise _invalid("unexpected action sequence")

    facts = payload.get("authoritative_facts")
    if not isinstance(facts, dict) or frozenset(facts) != FACT_FIELDS:
        raise _invalid("authoritative facts shape is not closed")
    if not _is_sha40_hex(facts.get("base_sha")):
        raise _invalid("base_sha must be 40 hex chars")

    registry = facts.get("registry_commands")
    if not isinstance(registry, dict) or frozenset(registry) != frozenset(REGISTRY_STAGES):
        raise _invalid("registry_commands must declare exactly the five registered stages")
    for stage in REGISTRY_STAGES:
        if not _is_argv(registry.get(stage)):
            raise _invalid(f"registry_commands[{stage}] must be a non-empty argv")

    prior = facts.get("prior_last_verified_sha")
    if prior is not None and not _is_sha40_hex(prior):
        raise _invalid("prior_last_verified_sha must be 40 hex chars or null")

    injected = payload.get("injected_results")
    if not isinstance(injected, dict) or frozenset(injected) != INJECTED_FIELDS:
        raise _invalid("injected_results shape is not closed")
    if not isinstance(injected.get("diff"), str):
        raise _invalid("injected_results.diff must be a string")
    if not isinstance(injected.get("diff_sha"), str):
        raise _invalid("injected_results.diff_sha must be a string")
    if not isinstance(injected.get("diff_base_sha"), str):
        raise _invalid("injected_results.diff_base_sha must be a string")
    if not isinstance(injected.get("stage_results"), dict):
        raise _invalid("injected_results.stage_results must be an object")


def _jsonable(value: Any) -> Any:
    """Reduce an untrusted value to a JSON-serialisable, deterministic form.

    JSON primitives and containers pass through; dict keys are normalised to
    strings with a type prefix, so mixed-type keys neither crash
    `sort_keys=True` nor collide with string-keyed entries; any other object
    becomes a stable `repr` string. This is the report's faithfulness guarantee
    (§5.1): nothing is silently dropped, and nothing can crash the hash.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {
            (
                key if isinstance(key, str) else f"<{type(key).__name__}:{key!r}>"
            ): _jsonable(val)
            for key, val in value.items()
        }
    return f"<{type(value).__name__}:{value!r}>"


def _observed_commands_exit_codes(
    injected: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The clean, string-keyed view of the observed stage results (§7). Non-string
    stage names are excluded here — they are recorded faithfully in the hashed
    report body instead — and a missing or malformed stage records `None`."""
    stage_results = injected["stage_results"]
    commands: dict[str, Any] = {}
    exit_codes: dict[str, Any] = {}
    for stage in sorted(key for key in stage_results if isinstance(key, str)):
        observed = stage_results[stage]
        if isinstance(observed, dict):
            commands[stage] = observed.get("command")
            exit_codes[stage] = observed.get("exit_code")
        else:
            commands[stage] = None
            exit_codes[stage] = None
    return commands, exit_codes


def _report_hash(facts: dict[str, Any], injected: dict[str, Any]) -> str:
    """`sha256(JCS(report body))`, where the body binds `base_sha` + `diff_sha` +
    the raw observed `stage_results` (via `_jsonable`, so nothing is dropped)."""
    body = {
        "schema_version": CONTRACT_VERSION,
        "base_sha": facts["base_sha"],
        "diff_sha": injected["diff_sha"],
        "stage_results": _jsonable(injected["stage_results"]),
    }
    return hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()


def _registry_contract_reasons(
    facts: dict[str, Any], injected: dict[str, Any]
) -> tuple[list[str], bool]:
    """Collect registry-integrity reasons and whether any command was swapped.

    A swapped command (`observed != declared`) is a `policy_failure` — the
    positive half of `injection.py`'s refusal of model-stitched commands. A
    missing, extra or malformed stage result is a `contract_failure`. Never
    raises: the stage results are untrusted.
    """
    registry = facts["registry_commands"]
    stage_results = injected["stage_results"]
    reasons: list[str] = []
    swapped = False

    for stage in REGISTRY_STAGES:
        declared = registry[stage]
        observed = stage_results.get(stage)
        if observed is None:
            reasons.append(f"missing stage result: {stage}")
            continue
        if not isinstance(observed, dict) or frozenset(observed) != STAGE_RESULT_FIELDS:
            reasons.append(f"malformed stage result: {stage}")
            continue
        observed_command = observed.get("command")
        if not _is_argv(observed_command):
            reasons.append(f"malformed command for stage: {stage}")
            continue
        if list(observed_command) != list(declared):
            swapped = True
        exit_code = observed.get("exit_code")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            reasons.append(f"malformed exit code for stage: {stage}")

    for stage in stage_results:
        if not isinstance(stage, str) or stage not in registry:
            reasons.append(f"undeclared stage: {stage!r}")

    return reasons, swapped


def _diff_reasons(facts: dict[str, Any], injected: dict[str, Any]) -> list[str]:
    """The diff-integrity contract failures, checked in order: a non-zero diff
    exit code (the diff command itself failed, so `diff` is an error message
    rather than a patch), an empty diff, a diff not bound to the requested base,
    or a drifted `diff_sha`. Never raises: the stage results are untrusted."""
    reasons: list[str] = []
    diff_stage = injected["stage_results"].get("diff")
    if isinstance(diff_stage, dict):
        diff_code = diff_stage.get("exit_code")
        if isinstance(diff_code, int) and not isinstance(diff_code, bool) and diff_code != 0:
            reasons.append("diff command failed")
    if not injected["diff"]:
        reasons.append("empty diff")
    if injected["diff_base_sha"] != facts["base_sha"]:
        reasons.append("diff not bound to base_sha")
    if injected["diff_sha"] != _sha256_text(injected["diff"]):
        reasons.append("diff_sha drift")
    return reasons


def _classify(
    facts: dict[str, Any], injected: dict[str, Any]
) -> tuple[str, str | None, str | None, list[str]]:
    """The §6 precedence tree: `(result_status, failure_class, reason_code, reasons)`.

    Registry integrity runs first: a swapped command is a `policy_failure`, and
    it precedes every correctness check (§5.2 leak-before-correctness). Diff
    integrity and stage exit codes follow. `task_failure` is `blocked` (the
    tests genuinely failed); `contract_failure` / `policy_failure` are `failed`
    (the feature needs a human).
    """
    reasons, swapped = _registry_contract_reasons(facts, injected)
    if swapped:
        return ("failed", "policy_failure", "POLICY_FAILURE", ())
    if reasons:
        return ("failed", "contract_failure", "PROVIDER_CONTRACT_FAILURE", reasons)

    diff_reasons = _diff_reasons(facts, injected)
    if diff_reasons:
        return ("failed", "contract_failure", "PROVIDER_CONTRACT_FAILURE", diff_reasons)

    stage_results = injected["stage_results"]
    for stage in CHECK_STAGES:
        if stage_results[stage]["exit_code"] != 0:
            return ("blocked", "task_failure", "TEST_BLOCKED", ())

    return ("succeeded", None, None, ())


def consume_verification(command: dict[str, Any]) -> VerificationEvaluation:
    """Consume one verification report and decide the contract boundary.

    A conforming run whose four check stages all exit 0 reports `succeeded`,
    moves the feature `verifying -> verified`, and advances `last_verified_sha`
    to `base_sha`. Any failed check stage reports `blocked` (`task_failure`) and
    moves the feature to `blocked_test`, preserving `last_verified_sha`. A
    swapped command reports `failed` (`policy_failure`); a drifted diff or a
    malformed stage set reports `failed` (`contract_failure`). The report and
    its `report_hash` are produced regardless of the verdict.
    """
    _validate_command(command)
    payload = command["input"]
    target = payload["target"]
    facts = payload["authoritative_facts"]
    injected = payload["injected_results"]

    result_status, failure_class, reason_code, reasons = _classify(facts, injected)

    commands, exit_codes = _observed_commands_exit_codes(injected)
    report = VerificationReport(
        commands=commands,
        exit_codes=exit_codes,
        report_hash=_report_hash(facts, injected),
        check_receipt=OperationReceipt(ReceiptCode.APPLIED, schema_version=CONTRACT_VERSION),
    )

    receipt = OperationReceipt(
        ReceiptCode.APPLIED, schema_version=FEATURE_TRANSITION_RECEIPT_SCHEMA
    )

    if result_status == "succeeded":
        return VerificationEvaluation(
            receipt=receipt,
            state_trace=(target["state"], "verified"),
            final_state="verified",
            final_entity_type=target["entity_type"],
            result_status=result_status,
            failure_class=None,
            last_verified_sha=facts["base_sha"],
            report=report,
            declared_write_set=VERIFIED_WRITE_SET,
        )

    final_state = REASON_TO_STATE[reason_code]
    return VerificationEvaluation(
        receipt=receipt,
        state_trace=(target["state"], final_state),
        final_state=final_state,
        final_entity_type=target["entity_type"],
        result_status=result_status,
        failure_class=failure_class,
        last_verified_sha=facts["prior_last_verified_sha"],
        report=report,
        final_reason_code=reason_code,
        final_reason_owner="feature",
        declared_write_set=BLOCK_WRITE_SET,
        event_trace=(BLOCK_EVENT,),
        contract_failure_reasons=tuple(reasons),
    )
