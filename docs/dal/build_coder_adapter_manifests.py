#!/usr/bin/env python3
"""Build the Claude Code coder adapter machine contract (freeze pack §8).

Machine counterpart of `DAL026_ClaudeCode_coder_adapter_合同冻结包_v0.1.md`.  It
emits the closed `dal.coder-adapter-request/1.0` and `dal.coder-adapter-response/1.0`
JSON Schemas, the event-vocabulary + classification-decision registry, and the
sixteen `DAL-T-CODER-CONTRACT-001` adversarial oracles plus their fixtures and
manifest rows.  It is documentation tooling only: it does not import or execute
DAL runtime, Worker, provider, or Personal Agent code, and it never touches
credentials.

The static authority that freezes the semantics of these files lives in
`manifests/coder-adapter-authority_v1.0.json` and is verified by
`verify_coder_adapter_authority.py`; `refreeze_coder_adapter.py` re-derives each
variant independently and splices a targeted change, following the
`refreeze_controller_dispatch.py` (Task #15) pattern.

Unlike `build_controller_dispatch_manifests.py` this builder does not depend on
the transition-spec registry: the coder classifier is a *forward-ref* handler
that has not yet been wired into the dispatch graph, so its oracles freeze only
the classifier's own `(result_status, failure_class, reason_code)` outcome, the
feature transition it implies, and the coverage label.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from dal_jcs import canonical_bytes

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "manifests"

OPERATION_SPEC_ID = "OP-CODER-CONTRACT-001"
COMMAND_TYPE = "consume_coder_stream"
SERVICE_ACTOR = "service"
EVIDENCE_SOURCE = "coder-adapter"
CONTRACT_VERSION = "dal.coder-response/1.0"
TEST_ID = "DAL-T-CODER-CONTRACT-001"

FEATURE_TRANSITION_RECEIPT_SCHEMA = "dal.transition-receipt/1.0"

# Frozen classifier anchors (docs/DAL026 §2–§6).  These are the single source of
# truth the builder, the machine classifier and the verify tooling must agree on.
BASE_SHA = "0123456789abcdef0123456789abcdef01234567"  # 40 hex
CONTEXT_SHA = "a" * 64
CLASSIFIER_DIGEST = "b" * 64
PINNED_ENDPOINT = "127.0.0.1:3456"
ALLOWED_TOOLS = ["read", "edit", "bash"]
ALLOWED_PATHS = ["src/", "tests/"]

BLOCK_WRITE_SET = [
    "aggregate", "business_event", "transition_receipt", "audit",
    "decision_create", "decision_projection", "notification_outbox",
]
BLOCK_EVENT = "feature.blocked"
APPLIED_RECEIPT = [
    {"code": "APPLIED", "count": 1, "schema_version": "dal.transition-receipt/1.0"}
]
FORBIDDEN_BASE = [
    "provider_call", "network_call", "filesystem_write", "process_spawn",
    "production_access",
]

#: §2 event vocabulary: per type, the mandatory and optional fields.  A coder
#: stream is multi-turn and multi-tool, so `tool_call` / free prose are legal;
#: the final payload is a multi-file patch bound to base_sha + context envelope.
EVENT_REQUIRED_FIELDS = {
    "final": frozenset({"type", "content", "base_sha", "context_envelope_sha256", "changed_files"}),
    "text": frozenset({"type", "content"}),
    "stream_delta": frozenset({"type", "content"}),
    "tool_call": frozenset({"type", "name"}),
    "transport_error": frozenset({"type", "code"}),
    "cancelled": frozenset({"type"}),
}
EVENT_OPTIONAL_FIELDS = {
    "final": frozenset({"turn"}),
    "text": frozenset(),
    "stream_delta": frozenset(),
    "tool_call": frozenset({"turn", "arguments", "arguments_json"}),
    "transport_error": frozenset(),
    "cancelled": frozenset(),
}

FACT_FIELDS = frozenset({
    "allowed_tools", "max_turns", "max_wall_seconds", "max_patch_bytes",
    "base_sha", "requested_context_envelope_sha256", "requested_classifier_digest",
    "pinned_endpoint", "allowed_paths",
})
INJECTED_FIELDS = frozenset({
    "stream", "exit_code", "observed_endpoint", "classifier_digest_post",
    "redaction_scan", "endpoint_policy", "sandbox_violation", "canary_observed",
    "transport", "budget",
})
TRANSPORT_FIELDS = frozenset({
    "http_status", "account_scoped_429", "provider_error_code",
    "timed_out", "disconnected",
})
BUDGET_FIELDS = frozenset({
    "turns_exhausted", "wall_seconds_exhausted", "patch_bytes_exhausted",
})

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
#: `failure_class` has NO `task_failure` — deterministic verification (running
#: tests) is DAL-029's separate stage; the coder only produces a patch.
FAILURE_CLASS_ENUM = ["usage_limit", "transient", "auth", "contract_failure",
                      "policy_failure", "budget_limit"]

_ERROR_CODE_AUTH = frozenset({"authentication_error", "invalid_api_key", "permission_denied"})
_ERROR_CODE_USAGE = frozenset({"rate_limit_exceeded", "quota_exceeded", "insufficient_quota"})
_ERROR_CODE_TRANSIENT = frozenset({"server_error", "overloaded", "connection_error", "timeout"})

_HTTPS_TRANSIENT = frozenset({502, 503, 504})

_HEX = "0123456789abcdef"


def digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _is_sha256_hex(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in _HEX for c in value)


def _is_sha40_hex(value: object) -> bool:
    return isinstance(value, str) and len(value) == 40 and all(c in _HEX for c in value)


def _path_allowed(path: object, allowed_paths: list[str]) -> bool:
    if not isinstance(path, str) or not path:
        return False
    for prefix in allowed_paths:
        stripped = prefix.rstrip("/")
        if path == stripped or path.startswith(stripped + "/"):
            return True
    return False


# ---------------------------------------------------------------------------
# §6 classification (the generator's own encoding of the frozen rules).
# ---------------------------------------------------------------------------
def _classify_transport(transport: dict, exit_code: int) -> tuple | None:
    """Return (result_status, failure_class, reason_code) or None if clean."""
    http = transport.get("http_status")
    account = transport.get("account_scoped_429")
    errcode = transport.get("provider_error_code")
    if http is not None:
        if http in (401, 403):
            return ("blocked", "auth", "AUTH_REQUIRED")
        if http == 429:
            if account is True:
                return ("blocked", "usage_limit", "USAGE_LIMIT")
            if account is False:
                return ("failed", "transient", "TRANSIENT_RETRY_EXHAUSTED")
            return ("failed", "contract_failure", "PROVIDER_CONTRACT_FAILURE")
        if http in _HTTPS_TRANSIENT:
            return ("failed", "transient", "TRANSIENT_RETRY_EXHAUSTED")
        return ("failed", "contract_failure", "PROVIDER_CONTRACT_FAILURE")
    if errcode is not None:
        if errcode in _ERROR_CODE_AUTH:
            return ("blocked", "auth", "AUTH_REQUIRED")
        if errcode in _ERROR_CODE_USAGE:
            return ("blocked", "usage_limit", "USAGE_LIMIT")
        if errcode in _ERROR_CODE_TRANSIENT:
            return ("failed", "transient", "TRANSIENT_RETRY_EXHAUSTED")
        return ("failed", "contract_failure", "PROVIDER_CONTRACT_FAILURE")
    if transport.get("timed_out") or transport.get("disconnected"):
        return ("failed", "transient", "TRANSIENT_RETRY_EXHAUSTED")
    if exit_code != 0:
        return ("failed", "contract_failure", "PROVIDER_CONTRACT_FAILURE")
    return None


def _out_of_scope(stream: list, facts: dict) -> bool:
    """§6 step 5: a tool or changed path outside the frozen allowlist."""
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


def _has_conforming_final(stream: list, facts: dict) -> bool:
    for event in stream:
        if not isinstance(event, dict) or event.get("type") != "final":
            continue
        if (
            _is_sha40_hex(event.get("base_sha"))
            and event.get("base_sha") == facts["base_sha"]
            and _is_sha256_hex(event.get("context_envelope_sha256"))
            and event.get("context_envelope_sha256") == facts["requested_context_envelope_sha256"]
            and isinstance(event.get("changed_files"), list)
            and event["changed_files"]
        ):
            return True
    return False


def _classify_stream(stream: list, facts: dict) -> list[str]:
    """§6 step 9: collect contract-failure reasons; [] means conforming shape."""
    reasons: list[str] = []
    finals = 0
    for event in stream:
        if not isinstance(event, dict):
            reasons.append("stream event is not an object")
            continue
        etype = event.get("type")
        if not isinstance(etype, str) or etype not in EVENT_REQUIRED_FIELDS:
            reasons.append("stream event has an unknown type")
            continue
        if EVENT_REQUIRED_FIELDS[etype] - frozenset(event):
            reasons.append(f"{etype} event is missing a mandatory field")
            continue
        if frozenset(event) - (EVENT_REQUIRED_FIELDS[etype] | EVENT_OPTIONAL_FIELDS[etype]):
            reasons.append(f"{etype} event carries an unknown field")
            continue

        if etype == "cancelled":
            if len(stream) != 1:
                reasons.append("cancelled event is not the whole stream")
        elif etype == "tool_call":
            has_arguments = "arguments" in event
            has_arguments_json = "arguments_json" in event
            if has_arguments == has_arguments_json:
                reasons.append("tool call carries neither or both argument forms")
            elif has_arguments and not isinstance(event["arguments"], dict):
                reasons.append("tool call arguments are not an object")
            elif has_arguments_json:
                if not isinstance(event["arguments_json"], str):
                    reasons.append("tool call arguments_json is not a string")
                else:
                    try:
                        parsed = json.loads(event["arguments_json"])
                    except ValueError:
                        reasons.append("tool call arguments_json is not valid JSON")
                    else:
                        if not isinstance(parsed, dict):
                            reasons.append("tool call arguments_json is not an object")
            turn = event.get("turn")
            if turn is not None and (not isinstance(turn, int) or turn < 1):
                reasons.append("tool call turn is not a positive integer")
        elif etype in ("text", "stream_delta"):
            if not isinstance(event.get("content"), str):
                reasons.append(f"{etype} content is not a string")
        elif etype == "transport_error":
            if not isinstance(event.get("code"), str) or not event["code"]:
                reasons.append("transport error code is not a non-empty string")
            reasons.append("stream terminated by a transport error")
        elif etype == "final":
            finals += 1
            if not isinstance(event.get("content"), str) or not event["content"]:
                reasons.append("final content is not a non-empty string")
            if not _is_sha40_hex(event.get("base_sha")):
                reasons.append("final base_sha is malformed")
            elif event["base_sha"] != facts["base_sha"]:
                reasons.append("final base_sha drifted from the request")
            if not _is_sha256_hex(event.get("context_envelope_sha256")):
                reasons.append("final context envelope digest is malformed")
            elif event["context_envelope_sha256"] != facts["requested_context_envelope_sha256"]:
                reasons.append("final context envelope drifted from the request")
            if not isinstance(event.get("changed_files"), list):
                reasons.append("final changed_files is not a list")
            elif not event["changed_files"]:
                reasons.append("final declares an empty diff")

    if finals > 1:
        reasons.append("stream carries more than one final event")
    if finals == 0 and stream != [{"type": "cancelled"}]:
        reasons.append("stream carries no final event")
    return reasons


def classify(injected: dict, facts: dict) -> tuple:
    """Return (result_status, failure_class, reason_code) per §6 precedence."""
    stream = injected["stream"]
    # 1. classifier digest drift
    if injected["classifier_digest_post"] != facts["requested_classifier_digest"]:
        return ("failed", "policy_failure", "POLICY_FAILURE")
    # 2. host tamper
    if injected["observed_endpoint"] != facts["pinned_endpoint"]:
        return ("failed", "policy_failure", "POLICY_FAILURE")
    # 3. redaction / endpoint policy
    if injected["redaction_scan"] == "failed" or injected["endpoint_policy"] == "failed":
        return ("failed", "policy_failure", "POLICY_FAILURE")
    # 4. sandbox / canary
    if injected["sandbox_violation"] or injected["canary_observed"]:
        return ("failed", "policy_failure", "POLICY_FAILURE")
    # 5. patch / tool out of scope
    if _out_of_scope(stream, facts):
        return ("failed", "policy_failure", "POLICY_FAILURE")
    # 6. cancel
    if stream == [{"type": "cancelled"}]:
        return ("cancelled", None, None)
    # 7. budget exhausted with no conforming final
    if any(injected["budget"].values()) and not _has_conforming_final(stream, facts):
        return ("blocked", "budget_limit", "BUDGET_LIMIT")
    # 8. transport / auth / usage
    transport = _classify_transport(injected["transport"], injected["exit_code"])
    if transport is not None:
        return transport
    # 9. stream correctness
    if _classify_stream(stream, facts):
        return ("failed", "contract_failure", "PROVIDER_CONTRACT_FAILURE")
    # 10. conforming
    return ("succeeded", None, None)


def transition_outcome(result_status: str, failure_class: str | None) -> dict:
    """Map a classification to the feature transition it implies."""
    if result_status in ("succeeded", "cancelled"):
        return {
            "state_trace": ["coding"], "event_trace": [], "receipts": APPLIED_RECEIPT,
            "writes": [], "reason": None, "final_state": "coding",
        }
    reason = FAILURE_TO_REASON[failure_class]
    final_state = REASON_TO_STATE[reason]
    return {
        "state_trace": ["coding", final_state], "event_trace": [BLOCK_EVENT],
        "receipts": APPLIED_RECEIPT, "writes": sorted(BLOCK_WRITE_SET),
        "reason": reason, "final_state": final_state,
    }


# ---------------------------------------------------------------------------
# The 16 frozen variants (inputs only — outcomes are derived).
# ---------------------------------------------------------------------------
def _facts() -> dict:
    return {
        "allowed_tools": list(ALLOWED_TOOLS),
        "max_turns": 8,
        "max_wall_seconds": 900,
        "max_patch_bytes": 1048576,
        "base_sha": BASE_SHA,
        "requested_context_envelope_sha256": CONTEXT_SHA,
        "requested_classifier_digest": CLASSIFIER_DIGEST,
        "pinned_endpoint": PINNED_ENDPOINT,
        "allowed_paths": list(ALLOWED_PATHS),
    }


def _injected(**overrides: object) -> dict:
    base: dict = {
        "stream": [],
        "exit_code": 0,
        "observed_endpoint": PINNED_ENDPOINT,
        "classifier_digest_post": CLASSIFIER_DIGEST,
        "redaction_scan": "passed",
        "endpoint_policy": "passed",
        "sandbox_violation": False,
        "canary_observed": False,
        "transport": {
            "http_status": None, "account_scoped_429": False,
            "provider_error_code": None, "timed_out": False, "disconnected": False,
        },
        "budget": {
            "turns_exhausted": False, "wall_seconds_exhausted": False,
            "patch_bytes_exhausted": False,
        },
    }
    base.update(overrides)
    return base


def _happy_stream() -> list:
    return [
        {"type": "text", "content": "I'll implement the change."},
        {"type": "tool_call", "name": "read", "turn": 1,
         "arguments": {"path": "src/x.py"}},
        {"type": "tool_call", "name": "edit", "turn": 2,
         "arguments": {"path": "src/x.py"}},
        {"type": "final", "content": "dal.patch-artifact/1.0:ref",
         "base_sha": BASE_SHA, "context_envelope_sha256": CONTEXT_SHA,
         "changed_files": ["src/x.py"], "turn": 3},
    ]


def _transport(**overrides: object) -> dict:
    base: dict = {
        "http_status": None, "account_scoped_429": False,
        "provider_error_code": None, "timed_out": False, "disconnected": False,
    }
    base.update(overrides)
    return base


def _budget(**overrides: object) -> dict:
    base: dict = {
        "turns_exhausted": False, "wall_seconds_exhausted": False,
        "patch_bytes_exhausted": False,
    }
    base.update(overrides)
    return base


def _final(**overrides: object) -> dict:
    base: dict = {
        "type": "final", "content": "dal.patch-artifact/1.0:ref",
        "base_sha": BASE_SHA, "context_envelope_sha256": CONTEXT_SHA,
        "changed_files": ["src/x.py"], "turn": 1,
    }
    base.update(overrides)
    return base


VARIANTS = [
    # (name, stream, injected_overrides, coverage_ref)
    ("happy_multi_tool", _happy_stream(), {}, "CODER-SUCCEED--coding"),
    ("empty", [], {}, "BLK-CONTRACT--coding"),
    ("malformed_args",
     [{"type": "tool_call", "name": "read", "turn": 1,
       "arguments": {"path": "src/x.py"}, "arguments_json": '{"path": "src/x.py"}'}],
     {}, "BLK-CONTRACT--coding"),
    ("half_stream",
     [{"type": "tool_call", "name": "read", "turn": 1, "arguments": {"path": "src/x.py"}},
      {"type": "transport_error", "code": "econnreset"}],
     {}, "BLK-CONTRACT--coding"),
    ("multi_final", [_final(changed_files=["src/a.py"]), _final(changed_files=["src/b.py"])],
     {}, "BLK-CONTRACT--coding"),
    ("turn_ceiling",
     [{"type": "tool_call", "name": "read", "turn": 9, "arguments": {"path": "src/x.py"}}],
     {"budget": _budget(turns_exhausted=True)}, "BLK-BUDGET--coding"),
    ("context_drift",
     [_final(base_sha="0" * 40)], {}, "BLK-CONTRACT--coding"),
    ("cancel", [{"type": "cancelled"}], {}, "SM-CANCEL--coding"),
    ("quota", [],
     {"transport": _transport(http_status=429, account_scoped_429=True)},
     "BLK-USAGE--coding"),
    ("transient", [],
     {"transport": _transport(http_status=503)}, "BLK-TRANSIENT-5XX--coding"),
    ("recovery", [],
     {"transport": _transport(http_status=429, account_scoped_429=False)},
     "BLK-TRANSIENT-429--coding"),
    ("real_auth", [],
     {"transport": _transport(http_status=401)}, "BLK-AUTH--coding"),
    ("tampered_host", _happy_stream(),
     {"observed_endpoint": "evil.example.com:443"}, "BLK-POLICY-TAMPER--coding"),
    ("classifier_drift", _happy_stream(),
     {"classifier_digest_post": "c" * 64}, "BLK-POLICY-CLASSIFIER--coding"),
    ("out_of_scope_patch",
     [_final(changed_files=["/etc/passwd"])], {}, "BLK-POLICY-SCOPE--coding"),
    ("done_diff_empty", [_final(changed_files=[])], {}, "BLK-CONTRACT--coding"),
]


def _command(name: str, facts: dict, injected: dict) -> dict:
    return {
        "operation_spec_id": OPERATION_SPEC_ID,
        "schema_version": "dal.test-operation-command/1.0",
        "operation_id": name,
        "idempotency_key": name,
        "actor_type": SERVICE_ACTOR,
        "evidence_source_type": EVIDENCE_SOURCE,
        "input": {
            "schema_version": "dal.operation-input/1.0",
            "target": {"entity_id": "fixture-entity", "entity_type": "feature",
                       "state": "coding", "version": 7},
            "action_sequence": [{"command": COMMAND_TYPE, "contract_version": CONTRACT_VERSION}],
            "authoritative_facts": facts,
            "injected_results": injected,
        },
    }


def _variant_fixture(name: str, stream: list, injected_overrides: dict) -> dict:
    injected = _injected(stream=stream, **injected_overrides)
    return {
        "schema_version": "dal.coder-adapter-fixture/1.0",
        "test_id": TEST_ID,
        "variant_id": name,
        "run_gate": "G3",
        "pre_state": {"entity_id": "fixture-entity", "entity_type": "feature",
                      "state": "coding", "version": 7},
        "operation_sequence": [_command(name, _facts(), injected)],
    }


def _variant_oracle(name: str, stream: list, injected_overrides: dict,
                    coverage_ref: str) -> dict:
    injected = _injected(stream=stream, **injected_overrides)
    facts = _facts()
    result_status, failure_class, _reason = classify(injected, facts)
    tx = transition_outcome(result_status, failure_class)
    return {
        "schema_version": "dal.coder-adapter-oracle/1.0",
        "test_id": TEST_ID,
        "variant_id": name,
        "run_gate": "G3",
        "pre_state": {"entity_type": "feature", "state": "coding", "version": 7},
        "expected_result_status": result_status,
        "expected_failure_class": failure_class,
        "expected_state_trace": tx["state_trace"],
        "expected_event_trace": tx["event_trace"],
        "expected_receipts": tx["receipts"],
        "expected_external_effect_trace": [],
        "expected_final_snapshot": {
            "entity_type": "feature",
            "reason_code": tx["reason"],
            "reason_owner": "feature" if tx["reason"] else None,
            "state": tx["final_state"],
        },
        "allowed_write_set": tx["writes"],
        "forbidden_side_effects": sorted(FORBIDDEN_BASE),
        "coverage_ref": coverage_ref,
    }


# ---------------------------------------------------------------------------
# Schemas + registry.
# ---------------------------------------------------------------------------
def _build_request_schema() -> dict:
    facts_props = {
        "allowed_tools": {
            "type": "array", "minItems": 1, "uniqueItems": True,
            "items": {"type": "string", "minLength": 1},
        },
        "max_turns": {"type": "integer", "minimum": 0},
        "max_wall_seconds": {"type": "integer", "minimum": 0},
        "max_patch_bytes": {"type": "integer", "minimum": 0},
        "base_sha": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
        "requested_context_envelope_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "requested_classifier_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "pinned_endpoint": {"type": "string", "minLength": 1},
        "allowed_paths": {
            "type": "array", "minItems": 1, "uniqueItems": True,
            "items": {"type": "string", "minLength": 1},
        },
    }
    return {
        "$id": "dal.coder-adapter-request/1.0",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$comment": "The trusted (protected) half of the coder contract. base_sha / requested_*_sha256 are RFC8785-JCS digests or git SHA filled by the controller, never provider-asserted. allowed_tools is non-empty and closed; allowed_paths are write-scope prefixes.",
        "schema_version": "dal.coder-adapter-request/1.0",
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "operation_spec_id", "contract_version",
                     "authoritative_facts"],
        "properties": {
            "schema_version": {"const": "dal.coder-adapter-request/1.0"},
            "operation_spec_id": {"const": OPERATION_SPEC_ID},
            "contract_version": {"const": CONTRACT_VERSION},
            "authoritative_facts": {
                "type": "object", "additionalProperties": False,
                "required": sorted(FACT_FIELDS),
                "properties": facts_props,
            },
        },
    }


def _build_response_schema() -> dict:
    reason_enum = sorted(FAILURE_TO_REASON.values())
    return {
        "$id": "dal.coder-adapter-response/1.0",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$comment": "The coder classifier result. failure_class has NO task_failure: deterministic verification is DAL-029's stage, the coder only produces a patch. result_status=failed carries contract/policy/transient failure; blocked carries usage/auth/budget limit.",
        "schema_version": "dal.coder-adapter-response/1.0",
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "result_status", "failure_class",
                     "reason_code", "final_state", "patch_artifact_ref",
                     "patch_artifact_sha256", "changed_files", "exit_code",
                     "redaction_scan", "endpoint_policy", "quarantined"],
        "properties": {
            "schema_version": {"const": "dal.coder-adapter-response/1.0"},
            "result_status": {"enum": ["succeeded", "cancelled", "blocked", "failed"]},
            "failure_class": {"enum": FAILURE_CLASS_ENUM + [None],
                              "type": ["string", "null"]},
            "reason_code": {"enum": reason_enum + [None], "type": ["string", "null"]},
            "final_state": {"type": "string", "minLength": 1},
            "patch_artifact_ref": {"type": ["string", "null"]},
            "patch_artifact_sha256": {"type": ["string", "null"],
                                      "pattern": "^[0-9a-f]{64}$"},
            "changed_files": {"type": "array", "items": {"type": "string", "minLength": 1}},
            "exit_code": {"type": "integer"},
            "redaction_scan": {"enum": ["passed", "failed"]},
            "endpoint_policy": {"enum": ["passed", "failed"]},
            "quarantined": {"type": "boolean"},
        },
    }


def _build_registry() -> dict:
    value = {
        "schema_version": "dal.coder-adapter-registry/1.0",
        "operation_spec_id": OPERATION_SPEC_ID,
        "command_type": COMMAND_TYPE,
        "contract_version": CONTRACT_VERSION,
        "service_actor": SERVICE_ACTOR,
        "evidence_source": EVIDENCE_SOURCE,
        "feature_transition_receipt_schema": FEATURE_TRANSITION_RECEIPT_SCHEMA,
        "event_vocabulary": {
            t: {"required": sorted(EVENT_REQUIRED_FIELDS[t]),
                "optional": sorted(EVENT_OPTIONAL_FIELDS[t])}
            for t in sorted(EVENT_REQUIRED_FIELDS)
        },
        "fact_fields": sorted(FACT_FIELDS),
        "injected_fields": sorted(INJECTED_FIELDS),
        "transport_fields": sorted(TRANSPORT_FIELDS),
        "budget_fields": sorted(BUDGET_FIELDS),
        "failure_class_enum": FAILURE_CLASS_ENUM,
        "failure_to_reason": FAILURE_TO_REASON,
        "reason_to_state": REASON_TO_STATE,
        "classification_precedence": [
            "classifier_digest_drift", "host_tamper", "redaction_or_endpoint_policy",
            "sandbox_or_canary", "patch_or_tool_out_of_scope", "cancel",
            "budget_exhausted", "transport_auth_usage", "stream_correctness", "conforming",
        ],
        "block_event": BLOCK_EVENT,
        "block_write_set": sorted(BLOCK_WRITE_SET),
    }
    value["registry_sha256"] = digest(
        {k: v for k, v in value.items() if k != "registry_sha256"}
    )
    return value


def _write(name: str, value: dict) -> None:
    (OUT / name).write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    _write("coder-adapter-request_schema_v1.0.json", _build_request_schema())
    _write("coder-adapter-response_schema_v1.0.json", _build_response_schema())

    registry = _build_registry()
    _write("coder-adapter-registry_v1.0.json", registry)

    fixtures = {}
    oracles = {}
    manifest_rows = []
    for (name, stream, injected_overrides, coverage_ref) in VARIANTS:
        fixtures[f"dal.coder-adapter.fixture/{TEST_ID}/{name}/G3/1.0"] = \
            _variant_fixture(name, stream, injected_overrides)
        oracles[f"dal.coder-adapter.oracle/{TEST_ID}/{name}/G3/1.0"] = \
            _variant_oracle(name, stream, injected_overrides, coverage_ref)
        manifest_rows.append({
            "test_id": TEST_ID,
            "variant_id": name,
            "run_gate": "G3",
            "owner_tasks": ["DAL-026"],
            "fixture_ref": f"dal.coder-adapter.fixture/{TEST_ID}/{name}/G3/1.0",
            "oracle_id": f"dal.coder-adapter.oracle/{TEST_ID}/{name}/G3/1.0",
        })

    fixture_catalog = {
        "schema_version": "dal.coder-adapter-fixture-catalog/1.0",
        "registry_sha256": registry["registry_sha256"],
        "fixtures": fixtures,
    }
    fixture_catalog["catalog_sha256"] = digest(
        {k: v for k, v in fixture_catalog.items() if k != "catalog_sha256"}
    )
    _write("coder-adapter-fixtures_v1.0.json", fixture_catalog)

    oracle_catalog = {
        "schema_version": "dal.coder-adapter-oracle-catalog/1.0",
        "registry_sha256": registry["registry_sha256"],
        "oracles": oracles,
    }
    oracle_catalog["catalog_sha256"] = digest(
        {k: v for k, v in oracle_catalog.items() if k != "catalog_sha256"}
    )
    _write("coder-adapter-oracles_v1.0.json", oracle_catalog)

    manifest = {
        "schema_version": "dal.coder-adapter-manifest/1.0",
        "manifest_version": "1.0",
        "registry_sha256": registry["registry_sha256"],
        "fixture_catalog_sha256": fixture_catalog["catalog_sha256"],
        "oracle_catalog_sha256": oracle_catalog["catalog_sha256"],
        "test_variants": manifest_rows,
    }
    manifest["manifest_sha256"] = digest(
        {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    )
    _write("coder-adapter-manifest_v1.0.json", manifest)

    print(json.dumps({
        "coder_variants": len(VARIANTS),
        "failure_class_enum": FAILURE_CLASS_ENUM,
        "registry_sha256": registry["registry_sha256"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
