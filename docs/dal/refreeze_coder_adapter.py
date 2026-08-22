#!/usr/bin/env python3
"""Targeted authority re-freeze for the coder adapter.

Follows `refreeze_controller_dispatch.py` (Task #15): it re-derives every
`DAL-T-CODER-CONTRACT-001` variant's classifier outcome from the fixture facts
and injected evidence, using a *separate* encoding of the frozen §6 precedence
tree — not `build_coder_adapter_manifests.py`'s `classify()`.  It then proves
that no unrelated authority row differs from the generated expectation, splices
only the coder-oracle entries, and recomputes the authority self-hash.

It deliberately cannot rebuild an entire authority.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MANIFESTS = ROOT / "manifests"
sys.path.insert(0, str(ROOT))

from dal_jcs import canonical_bytes  # noqa: E402
from verify_coder_adapter_authority import (  # noqa: E402
    expected_oracle_entries,
    semantic_counts,
)


TARGET_TEST_ID = "DAL-T-CODER-CONTRACT-001"

BLOCK_WRITES = [
    "aggregate", "business_event", "transition_receipt", "audit",
    "decision_create", "decision_projection", "notification_outbox",
]
APPLIED_RECEIPT = [
    {"code": "APPLIED", "count": 1, "schema_version": "dal.transition-receipt/1.0"}
]

FAILURE_TO_REASON = {
    "usage_limit": "USAGE_LIMIT",
    "transient": "TRANSIENT_RETRY_EXHAUSTED",
    "auth": "AUTH_REQUIRED",
    "contract_failure": "PROVIDER_CONTRACT_FAILURE",
    "policy_failure": "POLICY_FAILURE",
    "budget_limit": "BUDGET_LIMIT",
}
REASON_TO_STATE = {
    "USAGE_LIMIT": "blocked_usage",
    "AUTH_REQUIRED": "blocked_auth",
    "TRANSIENT_RETRY_EXHAUSTED": "needs_human",
    "PROVIDER_CONTRACT_FAILURE": "needs_human",
    "POLICY_FAILURE": "needs_human",
    "BUDGET_LIMIT": "needs_human",
}

_ERROR_CODE_AUTH = frozenset({"authentication_error", "invalid_api_key", "permission_denied"})
_ERROR_CODE_USAGE = frozenset({"rate_limit_exceeded", "quota_exceeded", "insufficient_quota"})
_ERROR_CODE_TRANSIENT = frozenset({"server_error", "overloaded", "connection_error", "timeout"})
_HTTPS_TRANSIENT = frozenset({502, 503, 504})

_HEX = "0123456789abcdef"


def digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def rehash(value: dict, field: str) -> str:
    return digest({k: v for k, v in value.items() if k != field})


# ---------------------------------------------------------------------------
# Independent re-derivation of the sixteen variants.  The classifier is a pure
# function of (facts, injected) whose outcome is a `(result_status,
# failure_class)` pair; the transition, write set, event and coverage label all
# follow from that pair plus the injection signature.
# ---------------------------------------------------------------------------
def _is_sha40(value: object) -> bool:
    return isinstance(value, str) and len(value) == 40 and all(c in _HEX for c in value)


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in _HEX for c in value)


def _path_allowed(path: object, allowed_paths: list[str]) -> bool:
    if not isinstance(path, str) or not path:
        return False
    for prefix in allowed_paths:
        stripped = prefix.rstrip("/")
        if path == stripped or path.startswith(stripped + "/"):
            return True
    return False


def _out_of_scope(stream: list, facts: dict) -> bool:
    for event in stream:
        if not isinstance(event, dict):
            continue
        if event.get("type") == "tool_call":
            name = event.get("name")
            if isinstance(name, str) and name not in facts["allowed_tools"]:
                return True
        if event.get("type") == "final":
            for path in event.get("changed_files", []):
                if not _path_allowed(path, facts["allowed_paths"]):
                    return True
    return False


def _has_bound_final(stream: list, facts: dict) -> bool:
    for event in stream:
        if not isinstance(event, dict) or event.get("type") != "final":
            continue
        if (
            event.get("base_sha") == facts["base_sha"]
            and event.get("context_envelope_sha256") == facts["requested_context_envelope_sha256"]
            and isinstance(event.get("changed_files"), list)
            and event["changed_files"]
        ):
            return True
    return False


def _transport_outcome(transport: dict, exit_code: int) -> tuple | None:
    http = transport.get("http_status")
    errcode = transport.get("provider_error_code")
    if http is not None:
        if http in (401, 403):
            return ("blocked", "auth")
        if http == 429:
            if transport.get("account_scoped_429") is True:
                return ("blocked", "usage_limit")
            if transport.get("account_scoped_429") is False:
                return ("failed", "transient")
            return ("failed", "contract_failure")
        if http in _HTTPS_TRANSIENT:
            return ("failed", "transient")
        return ("failed", "contract_failure")
    if errcode is not None:
        if errcode in _ERROR_CODE_AUTH:
            return ("blocked", "auth")
        if errcode in _ERROR_CODE_USAGE:
            return ("blocked", "usage_limit")
        if errcode in _ERROR_CODE_TRANSIENT:
            return ("failed", "transient")
        return ("failed", "contract_failure")
    if transport.get("timed_out") or transport.get("disconnected"):
        return ("failed", "transient")
    if exit_code != 0:
        return ("failed", "contract_failure")
    return None


def _stream_conforming(stream: list, facts: dict) -> bool:
    """True iff the stream shape is clean: exactly one valid final bound to the
    requested base_sha and a non-empty diff, no malformed event, no transport
    error, no cancelled marker mixed in.  Re-encoded independently of the
    generator's `_classify_stream`."""

    finals = 0
    for event in stream:
        if not isinstance(event, dict):
            return False
        etype = event.get("type")
        if etype not in ("final", "text", "stream_delta", "tool_call",
                         "transport_error", "cancelled"):
            return False
        if etype == "transport_error":
            return False  # 断流: the stream died mid-flight
        if etype == "cancelled":
            if len(stream) != 1:
                return False
        elif etype == "tool_call":
            if event.get("name") not in facts["allowed_tools"]:
                return False
            has_arguments = "arguments" in event
            has_arguments_json = "arguments_json" in event
            if has_arguments == has_arguments_json:
                return False
            if has_arguments and not isinstance(event["arguments"], dict):
                return False
            if has_arguments_json:
                if not isinstance(event["arguments_json"], str):
                    return False
                try:
                    parsed = json.loads(event["arguments_json"])
                except ValueError:
                    return False
                if not isinstance(parsed, dict):
                    return False
            turn = event.get("turn")
            if turn is not None and (not isinstance(turn, int) or turn < 1):
                return False
        elif etype in ("text", "stream_delta"):
            if not isinstance(event.get("content"), str):
                return False
        elif etype == "final":
            finals += 1
            if not event.get("content"):
                return False
            if event.get("base_sha") != facts["base_sha"]:
                return False
            if event.get("context_envelope_sha256") != facts["requested_context_envelope_sha256"]:
                return False
            if not event.get("changed_files"):
                return False
    return finals == 1


def _classify(facts: dict, injected: dict) -> tuple:
    stream = injected["stream"]
    # §6 policy/integrity checks first (leak before correctness).
    if injected["classifier_digest_post"] != facts["requested_classifier_digest"]:
        return ("failed", "policy_failure")
    if injected["observed_endpoint"] != facts["pinned_endpoint"]:
        return ("failed", "policy_failure")
    if injected["redaction_scan"] == "failed" or injected["endpoint_policy"] == "failed":
        return ("failed", "policy_failure")
    if injected["sandbox_violation"] or injected["canary_observed"]:
        return ("failed", "policy_failure")
    if _out_of_scope(stream, facts):
        return ("failed", "policy_failure")
    if stream == [{"type": "cancelled"}]:
        return ("cancelled", None)
    if any(injected["budget"].values()) and not _has_bound_final(stream, facts):
        return ("blocked", "budget_limit")
    transport = _transport_outcome(injected["transport"], injected["exit_code"])
    if transport is not None:
        return transport
    if not _stream_conforming(stream, facts):
        return ("failed", "contract_failure")
    return ("succeeded", None)


def _coverage(injected: dict, facts: dict, result_status: str, failure_class: str | None) -> str:
    """The coverage label, derived from the outcome plus the injection signature."""
    if result_status == "succeeded":
        return "CODER-SUCCEED--coding"
    if result_status == "cancelled":
        return "SM-CANCEL--coding"
    if failure_class == "budget_limit":
        return "BLK-BUDGET--coding"
    if failure_class == "usage_limit":
        return "BLK-USAGE--coding"
    if failure_class == "auth":
        return "BLK-AUTH--coding"
    if failure_class == "transient":
        return ("BLK-TRANSIENT-429--coding"
                if injected["transport"]["http_status"] == 429
                else "BLK-TRANSIENT-5XX--coding")
    if failure_class == "policy_failure":
        if injected["classifier_digest_post"] != facts["requested_classifier_digest"]:
            return "BLK-POLICY-CLASSIFIER--coding"
        if injected["observed_endpoint"] != facts["pinned_endpoint"]:
            return "BLK-POLICY-TAMPER--coding"
        return "BLK-POLICY-SCOPE--coding"
    return "BLK-CONTRACT--coding"


def derive(fixture: dict) -> dict:
    command = fixture["operation_sequence"][0]
    facts = command["input"]["authoritative_facts"]
    injected = command["input"]["injected_results"]

    result_status, failure_class = _classify(facts, injected)
    reason = None if failure_class is None else FAILURE_TO_REASON[failure_class]
    final_state = "coding" if failure_class is None else REASON_TO_STATE[reason]
    state_trace = ["coding"] if failure_class is None else ["coding", final_state]
    event_trace = [] if failure_class is None else ["feature.blocked"]
    writes = [] if failure_class is None else sorted(BLOCK_WRITES)

    return {
        "result_status": result_status,
        "failure_class": failure_class,
        "state_trace": state_trace,
        "event_trace": event_trace,
        "receipts": APPLIED_RECEIPT,
        "writes": writes,
        "final_state": final_state,
        "reason_code": reason,
        "coverage": _coverage(injected, facts, result_status, failure_class),
    }


def refreeze_targeted() -> None:
    registry = load(MANIFESTS / "coder-adapter-registry_v1.0.json")
    fixtures = load(MANIFESTS / "coder-adapter-fixtures_v1.0.json")
    oracles = load(MANIFESTS / "coder-adapter-oracles_v1.0.json")
    manifest = load(MANIFESTS / "coder-adapter-manifest_v1.0.json")

    # Independent re-derivation against the frozen oracle.
    for oracle_id, oracle in sorted(oracles["oracles"].items()):
        variant = oracle["variant_id"]
        fixture = fixtures["fixtures"][f"dal.coder-adapter.fixture/{TARGET_TEST_ID}/{variant}/G3/1.0"]
        derived = derive(fixture)
        if oracle["expected_result_status"] != derived["result_status"]:
            raise SystemExit(f"{variant}: result status drift")
        if oracle["expected_failure_class"] != derived["failure_class"]:
            raise SystemExit(f"{variant}: failure class drift")
        if oracle["expected_state_trace"] != derived["state_trace"]:
            raise SystemExit(f"{variant}: state trace drift")
        if oracle["expected_event_trace"] != derived["event_trace"]:
            raise SystemExit(f"{variant}: event trace drift")
        if oracle["expected_receipts"] != derived["receipts"]:
            raise SystemExit(f"{variant}: receipt drift")
        if oracle["allowed_write_set"] != derived["writes"]:
            raise SystemExit(f"{variant}: write set drift")
        if oracle["expected_final_snapshot"]["state"] != derived["final_state"]:
            raise SystemExit(f"{variant}: final state drift")
        if oracle["expected_final_snapshot"]["reason_code"] != derived["reason_code"]:
            raise SystemExit(f"{variant}: reason drift")
        if oracle["coverage_ref"] != derived["coverage"]:
            raise SystemExit(f"{variant}: coverage drift")

    # No unrelated drift: recompute the derived authority entries and splice
    # only the coder-oracle entries (all of them, since this is the whole family).
    authority_path = MANIFESTS / "coder-adapter-authority_v1.0.json"
    authority = load(authority_path)
    generated_entries = expected_oracle_entries(manifest, fixtures, oracles)

    def target(key: str) -> bool:
        return f"/{TARGET_TEST_ID}/" in key

    unexpected_entries = {
        key for key, value in generated_entries.items()
        if authority["oracle_entries"].get(key) != value and not target(key)
    }
    if unexpected_entries:
        raise SystemExit(f"unrelated oracle entry drift: {sorted(unexpected_entries)}")

    for key, value in generated_entries.items():
        if target(key):
            authority["oracle_entries"][key] = value
    authority["semantic_counts"] = semantic_counts(registry, generated_entries)
    authority["authority_sha256"] = rehash(authority, "authority_sha256")
    write(authority_path, authority)


def main() -> None:
    refreeze_targeted()
    print(json.dumps({"targeted_refreeze": [TARGET_TEST_ID]}, sort_keys=True))


if __name__ == "__main__":
    main()
