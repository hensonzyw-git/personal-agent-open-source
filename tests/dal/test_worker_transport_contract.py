"""DAL-R04 Worker Transport v1 — contract-freeze tests.

These tests do **not** run a transport server (that is DAL-R05). They pin the
contract the server must honour: the five closed request/response shapes and the
fail-closed outcomes for the eight required adversarial shapes. A fake server
would only re-state these rules; the fixtures are the adversary here.

The closed field sets below mirror ``docs/dal/openapi/worker-transport-v1.yaml``
``components.schemas``. Keep them in lockstep; the OpenAPI remains the contract,
this module is the executable check that a payload cannot silently gain a field.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_PATH = REPO_ROOT / "docs" / "dal" / "manifests" / "worker-transport-fixtures_v1.0.json"
OPENAPI_PATH = REPO_ROOT / "docs" / "dal" / "openapi" / "worker-transport-v1.yaml"

SCHEMA_VERSION = "dal.worker-transport/1.0"
CAPABILITIES = frozenset({"coding", "verification", "checkpoint"})
SENSITIVITY = frozenset({"checkpoint", "diff", "log"})
RESULT_STATES = frozenset({"succeeded", "failed"})
ARTIFACT_MAX_BYTES = 104857600
CHANGED_FILES_MAX = 10000
SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA64 = re.compile(r"^[0-9a-f]{64}$")

#: outcome -> canonical HTTP status (mirrors the OpenAPI response mapping).
HTTP_BY_OUTCOME = {
    "accepted": 200,
    "replay": 200,
    "unknown_capability": 403,
    "invalid": 400,
    "stale": 409,
    "conflict": 409,
    "cancelled": 409,
    "oversized": 413,
}

#: schema_name -> (closed field set, required subset, which key carries the payload).
CLOSED: dict[str, tuple[frozenset[str], frozenset[str], str]] = {
    "EnrollRequest": (
        frozenset(
            {"schema_version", "request_id", "worker_id", "machine_id", "capabilities"}
        ),
        frozenset(
            {"schema_version", "request_id", "worker_id", "machine_id", "capabilities"}
        ),
        "request",
    ),
    "ClaimResponse": (
        frozenset(
            {
                "schema_version",
                "job_id",
                "repository_id",
                "base_sha",
                "branch_name",
                "toolchain_ref",
                "lease_epoch",
                "attempt",
                "deadline",
            }
        ),
        frozenset(
            {
                "schema_version",
                "job_id",
                "repository_id",
                "base_sha",
                "branch_name",
                "toolchain_ref",
                "lease_epoch",
                "attempt",
                "deadline",
            }
        ),
        "response",
    ),
    "HeartbeatRequest": (
        frozenset(
            {"schema_version", "request_id", "job_id", "worker_id", "lease_epoch"}
        ),
        frozenset(
            {"schema_version", "request_id", "job_id", "worker_id", "lease_epoch"}
        ),
        "request",
    ),
    "CheckpointRequest": (
        frozenset(
            {
                "schema_version",
                "request_id",
                "job_id",
                "worker_id",
                "lease_epoch",
                "sequence",
                "artifact_sha256",
                "artifact_size_bytes",
                "changed_files",
                "sensitivity",
            }
        ),
        frozenset(
            {
                "schema_version",
                "request_id",
                "job_id",
                "worker_id",
                "lease_epoch",
                "sequence",
                "artifact_sha256",
                "artifact_size_bytes",
                "changed_files",
                "sensitivity",
            }
        ),
        "request",
    ),
    "ResultRequest": (
        frozenset(
            {
                "schema_version",
                "request_id",
                "job_id",
                "worker_id",
                "lease_epoch",
                "result_sha256",
                "state",
                "last_error",
            }
        ),
        frozenset(
            {
                "schema_version",
                "request_id",
                "job_id",
                "worker_id",
                "lease_epoch",
                "result_sha256",
                "state",
            }
        ),
        "request",
    ),
    "ResultResponse": (
        frozenset(
            {"schema_version", "job_id", "result_sha256", "receipt_id", "replay"}
        ),
        frozenset(
            {"schema_version", "job_id", "result_sha256", "receipt_id", "replay"}
        ),
        "response",
    ),
}


def _load_fixtures() -> dict[str, Any]:
    return json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def fixtures() -> dict[str, Any]:
    return _load_fixtures()


def _nonneg_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and value != ""


def _validate_enroll(p: dict[str, Any]) -> None:
    assert p["schema_version"] == SCHEMA_VERSION
    assert _nonempty_str(p["request_id"])
    assert _nonempty_str(p["worker_id"])
    assert _nonempty_str(p["machine_id"])
    caps = p["capabilities"]
    assert isinstance(caps, list) and len(caps) >= 1
    assert len(caps) == len(set(caps)), "capabilities must be unique"
    # Enum membership is a semantic fail-closed rule (unknown_capability), not a
    # shape rule: the adversarial `enroll_unknown_capability` fixture must pass
    # closed-shape validation so the outcome test can reject it.
    assert all(_nonempty_str(c) for c in caps)


def _validate_claim(p: dict[str, Any]) -> None:
    assert p["schema_version"] == SCHEMA_VERSION
    for key in ("job_id", "repository_id", "branch_name", "toolchain_ref"):
        assert _nonempty_str(p[key]), key
    assert SHA40.match(p["base_sha"])
    assert _nonneg_int(p["lease_epoch"])
    assert _nonneg_int(p["attempt"])
    assert _nonempty_str(p["deadline"])


def _validate_heartbeat(p: dict[str, Any]) -> None:
    assert p["schema_version"] == SCHEMA_VERSION
    assert _nonempty_str(p["request_id"])
    assert _nonempty_str(p["job_id"])
    assert _nonempty_str(p["worker_id"])
    assert _nonneg_int(p["lease_epoch"])


def _validate_checkpoint(p: dict[str, Any]) -> None:
    assert p["schema_version"] == SCHEMA_VERSION
    assert _nonempty_str(p["request_id"])
    assert _nonempty_str(p["job_id"])
    assert _nonempty_str(p["worker_id"])
    assert _nonneg_int(p["lease_epoch"])
    assert _nonneg_int(p["sequence"])
    assert SHA64.match(p["artifact_sha256"])
    assert isinstance(p["artifact_size_bytes"], int) and not isinstance(
        p["artifact_size_bytes"], bool
    )
    assert isinstance(p["changed_files"], list)
    assert len(p["changed_files"]) <= CHANGED_FILES_MAX
    assert all(_nonempty_str(f) for f in p["changed_files"])
    assert p["sensitivity"] in SENSITIVITY


def _validate_result(p: dict[str, Any]) -> None:
    assert p["schema_version"] == SCHEMA_VERSION
    assert _nonempty_str(p["request_id"])
    assert _nonempty_str(p["job_id"])
    assert _nonempty_str(p["worker_id"])
    assert _nonneg_int(p["lease_epoch"])
    assert SHA64.match(p["result_sha256"])
    assert p["state"] in RESULT_STATES
    # last_error is optional; it must be a non-empty string when present, and the
    # `state=failed` path carries it (asserted in the outcome test).
    assert p.get("last_error") is None or _nonempty_str(p["last_error"])


def _validate_result_response(p: dict[str, Any]) -> None:
    assert p["schema_version"] == SCHEMA_VERSION
    assert _nonempty_str(p["job_id"])
    assert SHA64.match(p["result_sha256"])
    assert _nonempty_str(p["receipt_id"])
    assert isinstance(p["replay"], bool)


_VALIDATORS = {
    "EnrollRequest": _validate_enroll,
    "ClaimResponse": _validate_claim,
    "HeartbeatRequest": _validate_heartbeat,
    "CheckpointRequest": _validate_checkpoint,
    "ResultRequest": _validate_result,
    "ResultResponse": _validate_result_response,
}


def _derive_outcome(fixture: dict[str, Any]) -> str:
    """The fail-closed outcome the fixture's own facts imply.

    lease fencing is exact: an active job's `lease_epoch` must equal the ECS
    current epoch (`!=` → stale, so a forged future epoch fails closed too).
    A terminal job's result is judged on digest first (idempotent replay or
    conflict), because a finished job has no live lease to fence.
    """
    facts = fixture.get("authoritative_facts") or {}
    schema = fixture["schema_name"]
    if schema == "EnrollRequest":
        caps = fixture["request"]["capabilities"]
        if any(c not in CAPABILITIES for c in caps):
            return "unknown_capability"
        return "accepted"
    if schema == "HeartbeatRequest":
        if fixture["request"]["lease_epoch"] != facts["current_lease_epoch"]:
            return "stale"
        return "accepted"
    if schema == "CheckpointRequest":
        if fixture["request"]["artifact_size_bytes"] > ARTIFACT_MAX_BYTES:
            return "oversized"
        expected = facts.get("expected_artifact_sha256")
        if expected is not None and fixture["request"]["artifact_sha256"] != expected:
            return "invalid"
        if fixture["request"]["lease_epoch"] != facts["current_lease_epoch"]:
            return "stale"
        return "accepted"
    if schema == "ResultRequest":
        if facts.get("current_job_state") == "cancelled":
            return "cancelled"
        existing = facts.get("existing_result_sha256")
        if existing is None:
            if fixture["request"]["lease_epoch"] != facts["current_lease_epoch"]:
                return "stale"
            return "accepted"
        return "replay" if existing == fixture["request"]["result_sha256"] else "conflict"
    if schema == "ResultResponse":
        return "replay" if fixture["response"].get("replay") else "accepted"
    return "accepted"


def test_every_fixture_is_closed_shape(fixtures: dict[str, Any]) -> None:
    for key, fixture in fixtures.items():
        if not isinstance(fixture, dict) or "schema_name" not in fixture:
            continue  # the $comment top-level key
        schema = fixture["schema_name"]
        fields, required, payload_key = CLOSED[schema]
        payload = fixture[payload_key]
        assert isinstance(payload, dict), key
        assert set(payload) <= fields, f"{key}: unknown field(s) {set(payload) - fields}"
        assert required <= set(payload), f"{key}: missing {required - set(payload)}"
        _VALIDATORS[schema](payload)


def test_fixture_outcome_matches_fail_closed_rule(fixtures: dict[str, Any]) -> None:
    for key, fixture in fixtures.items():
        if not isinstance(fixture, dict) or "schema_name" not in fixture:
            continue
        if fixture["schema_name"] == "ClaimResponse":
            assert fixture["expected"]["outcome"] == "accepted", key
            continue
        assert fixture["expected"]["outcome"] == _derive_outcome(fixture), key


def test_fixture_http_status_matches_outcome(fixtures: dict[str, Any]) -> None:
    for key, fixture in fixtures.items():
        if not isinstance(fixture, dict) or "schema_name" not in fixture:
            continue
        outcome = fixture["expected"]["outcome"]
        assert fixture["expected"]["http"] == HTTP_BY_OUTCOME[outcome], key


def test_replay_receipt_id_is_stable(fixtures: dict[str, Any]) -> None:
    replay = [
        f
        for f in fixtures.values()
        if isinstance(f, dict) and f.get("schema_name") == "ResultResponse"
    ]
    assert replay, "no ResultResponse replay fixture"
    for fixture in replay:
        assert fixture["response"]["replay"] is True
        assert fixture["response"]["receipt_id"] == fixture["authoritative_facts"][
            "existing_receipt_id"
        ]


def test_eight_adversarial_shapes_are_present(fixtures: dict[str, Any]) -> None:
    variants = {
        f["variant_id"] for f in fixtures.values() if isinstance(f, dict) and "variant_id" in f
    }
    for expected in {
        "enroll_unknown_capability",
        "heartbeat_expired_epoch",
        "checkpoint_tamper",
        "checkpoint_oversized",
        "result_duplicate_same",
        "result_duplicate_conflicting",
        "result_response_loss_replay",
        "result_cancel_race",
    }:
        assert expected in variants, f"missing adversarial shape {expected}"


def test_openapi_schemas_are_closed() -> None:
    text = OPENAPI_PATH.read_text(encoding="utf-8")
    for name in CLOSED:
        assert re.search(rf"^    {name}:\n", text, re.MULTILINE), f"{name} schema missing"
    # Every one of the five transport schemas closes against extra properties; the
    # inline health/heartbeat/error responses add more, so a lower bound is fine.
    assert text.count("additionalProperties: false") >= len(CLOSED)
