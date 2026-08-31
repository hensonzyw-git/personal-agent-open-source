"""Adversarial tests for the one-time commit capability (DAL-031, R09-A3).

The capability is the boundary between "the feature is verified" and "a
candidate commit may be created". The pure module in the ``patch_policy`` /
``review_fix_loop`` family issues and consumes a capability that binds the
base/result SHAs, the approved plan's allowed paths, the four frozen commit
trailers (``Feature-Id``/``Task-Id``/``Plan-Hash``/``Review-Id``), an expiry,
the issuing idempotency key and ``max_uses=1``.

Test groups follow the Roadmap coverage line (单次消费、过期、SHA/path 篡改与
撤权竞态):

- (A) trusted shape drift on both gates → ``INVALID_ARGUMENT``;
- (B) the issue gate: a verified feature yields a go verdict; any drift in
  the binding (state, expiry, trailers, paths, max_uses) raises;
- (C) the consume gate lifecycle facts: an unused live row is eligible; a
  previously consumed row, expiry, revocation and an epoch bump each refuse
  ``CAPABILITY_STALE`` with zero writes (the lease-family refusal);
- (D) binding tampering on a live capability: any divergence between the
  issued binding and the presented commit intent — SHAs, id, idempotency
  key, trailers, touched paths outside the allowed set — lands the frozen
  ``BLK-POLICY--verified`` row (verified → needs_human, seven-write block
  set, APPLIED) with every violation labelled, never a crash and never a
  silent pass;
- (E) classification order: a dead capability refuses before its binding is
  judged (the caller may ask; the capability is already bad);
- (F) module hygiene: the pure judge cannot acquire an I/O dependency and
  does not invent an operation spec id.

Native-str-only guards (round-7 review F5 family): string validators accept
only native JSON strings; str subclasses are trusted-side drift.
"""

from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path

import pytest

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.machine import commit_capability
from personal_agent_dal.machine.commit_capability import (
    consume_commit_capability,
    issue_commit_capability,
)
from personal_agent_dal.receipt import ReceiptCode

# --- deterministic identity material ----------------------------------------


def _sha256_hex(namespace: str, label: str) -> str:
    import hashlib

    return hashlib.sha256(f"{namespace}/{label}".encode("utf-8")).hexdigest()


def _git_sha(label: str) -> str:
    return _sha256_hex("tree", label)[:40]


BASE = _git_sha("verified-base-tree")
RESULT = _git_sha("verified-result-tree")
PLAN_HASH = _sha256_hex("plan", "approved-plan")
NOW = 1_800_000_000
LATER = NOW + 900
EPOCH = 3

ALLOWED_PATHS = [
    {"path": "src/app", "path_type": "directory"},
    {"path": "tests/app/test_service.py", "path_type": "file"},
]
LEGAL_TOUCHED = ["src/app/core.py", "tests/app/test_service.py"]

TRAILERS = {
    "Feature-Id": "feature-0001",
    "Task-Id": "task-01",
    "Plan-Hash": PLAN_HASH,
    "Review-Id": "review-0001",
}

BASE_WRITE_SET = ("aggregate", "business_event", "transition_receipt", "audit")
BLOCK_WRITE_SET = BASE_WRITE_SET + (
    "decision_create",
    "decision_projection",
    "notification_outbox",
)


def _target(state: str = "verified") -> dict:
    return {
        "entity_id": "feature-0001",
        "entity_type": "feature",
        "state": state,
        "version": 5,
    }


def _binding() -> dict:
    return {
        "capability_id": "cap-0001",
        "approval_id": "approval-0001",
        "lease_epoch": 7,
        "base_sha": BASE,
        "result_sha": RESULT,
        "allowed_paths": deepcopy(ALLOWED_PATHS),
        "trailers": dict(TRAILERS),
        "idempotency_key": "issue-key-0001",
        "expires_at": LATER,
        "max_uses": 1,
        "capability_epoch": EPOCH,
    }


def _issue_facts(
    *,
    state: str = "verified",
    entity_id: str = "feature-0001",
    binding: dict | None = None,
    now: int = NOW,
) -> dict:
    return {
        "schema_version": "dal.commit-capability-issue-facts/1.0",
        "target": _target(state),
        "binding": dict(binding) if binding is not None else _binding(),
        "now": now,
    }


def _capability_row(
    *,
    uses_consumed: int = 0,
    revoked_at: int | None = None,
) -> dict:
    return {
        "capability_id": "cap-0001",
        "approval_id": "approval-0001",
        "lease_epoch": 7,
        "base_sha": BASE,
        "result_sha": RESULT,
        "allowed_paths": deepcopy(ALLOWED_PATHS),
        "trailers": dict(TRAILERS),
        "idempotency_key": "issue-key-0001",
        "expires_at": LATER,
        "max_uses": 1,
        "uses_consumed": uses_consumed,
        "consumed_by": "commit-command-0001" if uses_consumed else None,
        "revoked_at": revoked_at,
        "capability_epoch": EPOCH,
    }


def _presented() -> dict:
    return {
        "capability_id": "cap-0001",
        "approval_id": "approval-0001",
        "base_sha": BASE,
        "result_sha": RESULT,
        "touched_paths": list(LEGAL_TOUCHED),
        "trailers": dict(TRAILERS),
        "idempotency_key": "issue-key-0001",
    }


def _consume_facts(
    *,
    state: str = "verified",
    capability: dict | None = None,
    presented: dict | None = None,
    now: int = NOW,
    current_epoch: int = EPOCH,
) -> dict:
    return {
        "schema_version": "dal.commit-capability-consume-facts/1.0",
        "target": _target(state),
        "capability": dict(capability) if capability is not None else _capability_row(),
        "presented": dict(presented) if presented is not None else _presented(),
        "now": now,
        "current_epoch": current_epoch,
        "current_lease_epoch": 7,
    }


def _shape(result) -> tuple:  # type: ignore[no-untyped-def]
    """Every observable field of one evaluation, for equality assertions."""
    return (
        result.receipt.code,
        result.receipt.schema_version,
        result.state_trace,
        result.final_state,
        result.final_entity_type,
        result.final_reason_code,
        result.final_reason_owner,
        result.declared_write_set,
        result.event_trace,
        result.violations,
    )


def _go_shape(state: str = "verified"):  # type: ignore[no-untyped-def]
    return (
        ReceiptCode.APPLIED,
        "dal.transition-receipt/1.0",
        (state,),
        state,
        "feature",
        None,
        None,
        (),
        (),
        (),
    )


class _UnhashableStr(str):
    __hash__ = None


# ---------------------------------------------------------------------------
# (A) trusted shape drift: closed sets, value classes, native strings.
# ---------------------------------------------------------------------------


def test_issue_facts_shape_is_closed() -> None:
    facts = _issue_facts()
    facts["extra"] = 1
    with pytest.raises(DalError) as raised:
        issue_commit_capability(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT

    facts = _issue_facts()
    del facts["binding"]
    with pytest.raises(DalError) as raised:
        issue_commit_capability(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT

    with pytest.raises(DalError) as raised:
        issue_commit_capability(None)  # type: ignore[arg-type]
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


def test_consume_facts_shape_is_closed() -> None:
    facts = _consume_facts()
    facts["extra"] = 1
    with pytest.raises(DalError) as raised:
        consume_commit_capability(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT

    facts = _consume_facts()
    del facts["presented"]
    with pytest.raises(DalError) as raised:
        consume_commit_capability(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT

    with pytest.raises(DalError) as raised:
        consume_commit_capability(None)  # type: ignore[arg-type]
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


def test_wrong_issue_facts_schema_is_rejected() -> None:
    facts = _issue_facts()
    facts["schema_version"] = "dal.commit-capability-consume-facts/1.0"
    with pytest.raises(DalError) as raised:
        issue_commit_capability(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


@pytest.mark.parametrize(
    "field, value",
    [
        ("base_sha", "zz"),
        ("base_sha", _sha256_hex("x", "y")),  # 64-hex: wrong sha class
        ("result_sha", "zz"),
        ("capability_id", ""),
        ("capability_id", None),
        ("capability_id", _UnhashableStr("cap-0001")),
        ("idempotency_key", ""),
        ("idempotency_key", _UnhashableStr("issue-key-0001")),
        ("expires_at", True),
        ("expires_at", 1.5),
        ("expires_at", -1),
        ("max_uses", 2),
        ("max_uses", 0),
        ("max_uses", True),
        ("capability_epoch", True),
        ("capability_epoch", -1),
    ],
)
def test_issue_binding_value_drift_raises(field: str, value: object) -> None:
    binding = _binding()
    binding[field] = value
    with pytest.raises(DalError) as raised:
        issue_commit_capability(_issue_facts(binding=binding))
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


@pytest.mark.parametrize("field", ["capability_id", "idempotency_key", "expires_at", "max_uses"])
def test_issue_binding_missing_field_raises(field: str) -> None:
    binding = _binding()
    del binding[field]
    with pytest.raises(DalError) as raised:
        issue_commit_capability(_issue_facts(binding=binding))
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


def test_issue_trailers_must_carry_the_frozen_four() -> None:
    for removed in ("Feature-Id", "Task-Id", "Plan-Hash", "Review-Id"):
        trailers = dict(TRAILERS)
        del trailers[removed]
        with pytest.raises(DalError) as raised:
            issue_commit_capability(_issue_facts(binding={**_binding(), "trailers": trailers}))
        assert raised.value.code is DalErrorCode.INVALID_ARGUMENT

    with pytest.raises(DalError) as raised:
        issue_commit_capability(
            _issue_facts(binding={**_binding(), "trailers": {**TRAILERS, "Extra": "x"}})
        )
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT

    for value in ("", None, 7, _UnhashableStr("task-01")):
        trailers = {**TRAILERS, "Task-Id": value}
        with pytest.raises(DalError) as raised:
            issue_commit_capability(_issue_facts(binding={**_binding(), "trailers": trailers}))
        assert raised.value.code is DalErrorCode.INVALID_ARGUMENT

    trailers = {**TRAILERS, "Plan-Hash": "zz"}
    with pytest.raises(DalError) as raised:
        issue_commit_capability(_issue_facts(binding={**_binding(), "trailers": trailers}))
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


def test_issue_feature_trailer_must_name_the_target() -> None:
    trailers = {**TRAILERS, "Feature-Id": "feature-9999"}
    with pytest.raises(DalError) as raised:
        issue_commit_capability(_issue_facts(binding={**_binding(), "trailers": trailers}))
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


def test_issue_allowed_paths_follow_the_frozen_plan_semantics() -> None:
    with pytest.raises(DalError) as raised:
        issue_commit_capability(_issue_facts(binding={**_binding(), "allowed_paths": []}))
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT

    for path in ("/abs/src", "src/../src", "src//x", ".git/config", ""):
        allowed = [{"path": path, "path_type": "file"}]
        with pytest.raises(DalError) as raised:
            issue_commit_capability(_issue_facts(binding={**_binding(), "allowed_paths": allowed}))
        assert raised.value.code is DalErrorCode.INVALID_ARGUMENT

    allowed = [{"path": "src/app", "path_type": "symlink"}]
    with pytest.raises(DalError) as raised:
        issue_commit_capability(_issue_facts(binding={**_binding(), "allowed_paths": allowed}))
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


@pytest.mark.parametrize("state", ["coding", "reviewing", "verifying", "awaiting_merge"])
def test_issue_requires_the_verified_state(state: str) -> None:
    with pytest.raises(DalError) as raised:
        issue_commit_capability(_issue_facts(state=state))
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


def test_issue_expiry_must_be_in_the_future() -> None:
    binding = _binding()
    binding["expires_at"] = NOW
    with pytest.raises(DalError) as raised:
        issue_commit_capability(_issue_facts(binding=binding))
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT

    binding = _binding()
    binding["expires_at"] = NOW - 1
    with pytest.raises(DalError) as raised:
        issue_commit_capability(_issue_facts(binding=binding))
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


def test_issue_now_must_be_epoch_seconds() -> None:
    for now in (True, 1.5, "later", None):
        with pytest.raises(DalError) as raised:
            issue_commit_capability(_issue_facts(now=now))  # type: ignore[arg-type]
        assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


def test_issue_target_drift_raises() -> None:
    facts = _issue_facts()
    facts["target"]["entity_type"] = "task"
    with pytest.raises(DalError) as raised:
        issue_commit_capability(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT

    facts = _issue_facts()
    facts["target"]["version"] = True
    with pytest.raises(DalError) as raised:
        issue_commit_capability(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT

    facts = _issue_facts()
    facts["target"]["state"] = "coding"
    with pytest.raises(DalError) as raised:
        issue_commit_capability(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


# ---------------------------------------------------------------------------
# (B) consume-gate trusted shape drift.
# ---------------------------------------------------------------------------


def test_consume_capability_row_shape_is_closed() -> None:
    for removed in ("uses_consumed", "revoked_at", "capability_epoch"):
        row = _capability_row()
        del row[removed]
        with pytest.raises(DalError) as raised:
            consume_commit_capability(_consume_facts(capability=row))
        assert raised.value.code is DalErrorCode.INVALID_ARGUMENT

    row = _capability_row()
    row["extra"] = 1
    with pytest.raises(DalError) as raised:
        consume_commit_capability(_consume_facts(capability=row))
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


def test_consume_presented_shape_is_closed() -> None:
    presented = _presented()
    presented["extra"] = 1
    _assert_policy_block(consume_commit_capability(_consume_facts(presented=presented)))

    presented = _presented()
    del presented["touched_paths"]
    _assert_policy_block(consume_commit_capability(_consume_facts(presented=presented)))


def test_consume_time_fields_are_epoch_seconds() -> None:
    for now in (True, 1.5, "later", None):
        with pytest.raises(DalError) as raised:
            consume_commit_capability(_consume_facts(now=now))  # type: ignore[arg-type]
        assert raised.value.code is DalErrorCode.INVALID_ARGUMENT

    row = _capability_row()
    row["expires_at"] = True
    with pytest.raises(DalError) as raised:
        consume_commit_capability(_consume_facts(capability=row))
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT

    row = _capability_row()
    row["revoked_at"] = 1.5
    with pytest.raises(DalError) as raised:
        consume_commit_capability(_consume_facts(capability=row))
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT

    row = _capability_row()
    row["uses_consumed"] = True
    with pytest.raises(DalError) as raised:
        consume_commit_capability(_consume_facts(capability=row))
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


def test_consume_requires_the_verified_state() -> None:
    with pytest.raises(DalError) as raised:
        consume_commit_capability(_consume_facts(state="coding"))
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


def test_consume_bound_epoch_ahead_of_current_is_forged() -> None:
    """The bound epoch leads the feature's current epoch: the controller
    could not have issued this capability — forged history, not staleness."""
    facts = _consume_facts(current_epoch=EPOCH - 1)
    with pytest.raises(DalError) as raised:
        consume_commit_capability(facts)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


def test_consume_presented_capability_shape_is_not_capability_drift() -> None:
    """The presented commit intent is executor output, not trusted state: a
    non-native-str value there must land the tamper block, not raise. The
    *capability row* (controller state) raising for the same shape is the
    classification boundary this pair pins."""
    row = _capability_row()
    row["capability_id"] = _UnhashableStr("cap-0001")
    with pytest.raises(DalError) as raised:
        consume_commit_capability(_consume_facts(capability=row))
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


def test_consume_touched_paths_must_be_normalized() -> None:
    for path in ("/abs/x.py", "src/../x.py", "src//x.py", ""):
        presented = _presented()
        presented["touched_paths"] = list(LEGAL_TOUCHED) + [path]
        _assert_policy_block(consume_commit_capability(_consume_facts(presented=presented)))

    presented = _presented()
    presented["touched_paths"] = "src/app/core.py"  # not a list
    _assert_policy_block(consume_commit_capability(_consume_facts(presented=presented)))

    presented = _presented()
    presented["touched_paths"] = [_UnhashableStr("src/app/core.py")]
    _assert_policy_block(consume_commit_capability(_consume_facts(presented=presented)))


# ---------------------------------------------------------------------------
# (C) the issue go verdict and the consume lifecycle.
# ---------------------------------------------------------------------------


def test_issue_on_a_verified_feature_is_a_go() -> None:
    result = issue_commit_capability(_issue_facts())
    assert _shape(result) == _go_shape()


def test_consume_a_live_capability_is_a_go() -> None:
    result = consume_commit_capability(_consume_facts())
    assert _shape(result) == _go_shape()


def test_consume_at_exact_expiry_is_still_live() -> None:
    result = consume_commit_capability(_consume_facts(now=LATER))
    assert _shape(result) == _go_shape()


def test_consume_after_expiry_refuses_stale() -> None:
    result = consume_commit_capability(_consume_facts(now=LATER + 1))
    assert result.receipt.code is ReceiptCode.CAPABILITY_STALE
    assert result.final_state == "verified"
    assert result.declared_write_set == ()
    assert result.event_trace == ()


def test_consume_double_tap_refuses_stale() -> None:
    row = _capability_row(uses_consumed=1)
    result = consume_commit_capability(_consume_facts(capability=row))
    assert result.receipt.code is ReceiptCode.CAPABILITY_STALE
    assert result.declared_write_set == ()


def test_consume_when_max_uses_is_zero_raises_trusted_drift() -> None:
    """``max_uses`` is frozen to 1 (技术方案 §6.2): a zero-use row could never
    have been legally issued, so its presence in controller state is trusted
    drift and raises. The exhaustion path for a legal row is the double-tap
    test above (uses_consumed >= max_uses with max_uses=1)."""
    row = _capability_row()
    row["max_uses"] = 0
    with pytest.raises(DalError) as raised:
        consume_commit_capability(_consume_facts(capability=row))
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


def test_consume_revoked_capability_refuses_stale() -> None:
    row = _capability_row(revoked_at=NOW - 10)
    result = consume_commit_capability(_consume_facts(capability=row))
    assert result.receipt.code is ReceiptCode.CAPABILITY_STALE
    assert result.declared_write_set == ()


def test_consume_epoch_bump_refuses_stale() -> None:
    """A revoke-by-epoch (kill switch, policy upgrade) leaves the bound epoch
    behind the current one — the DAL-016 stale rule at the commit boundary."""
    result = consume_commit_capability(_consume_facts(current_epoch=EPOCH + 1))
    assert result.receipt.code is ReceiptCode.CAPABILITY_STALE
    assert result.declared_write_set == ()


# ---------------------------------------------------------------------------
# (D) binding tampering on a live capability lands BLK-POLICY--verified.
# ---------------------------------------------------------------------------


def _tampered(presented_mutate=None, capability_mutate=None):  # type: ignore[no-untyped-def]
    presented = _presented()
    if presented_mutate is not None:
        presented_mutate(presented)
    row = _capability_row()
    if capability_mutate is not None:
        row = _capability_row()
        capability_mutate(row)
    return _consume_facts(capability=row, presented=presented)


def _assert_policy_block(result) -> None:  # type: ignore[no-untyped-def]
    assert result.receipt.code is ReceiptCode.APPLIED
    assert result.receipt.schema_version == "dal.transition-receipt/1.0"
    assert result.final_entity_type == "feature"
    assert result.state_trace == ("verified", "needs_human")
    assert result.final_state == "needs_human"
    assert result.final_reason_code == "POLICY_FAILURE"
    assert result.final_reason_owner == "feature"
    assert result.declared_write_set == BLOCK_WRITE_SET
    assert result.event_trace == ("feature.blocked",)
    assert result.violations


@pytest.mark.parametrize(
    "label, mutate",
    [
        ("capability_id", lambda p: p.__setitem__("capability_id", "cap-9999")),
        ("base_sha", lambda p: p.__setitem__("base_sha", _git_sha("other-base"))),
        ("result_sha", lambda p: p.__setitem__("result_sha", _git_sha("other-result"))),
        ("idempotency_key", lambda p: p.__setitem__("idempotency_key", "issue-key-9999")),
    ],
)
def test_presented_identity_and_sha_drift_block(label: str, mutate) -> None:
    result = consume_commit_capability(_tampered(presented_mutate=mutate))
    _assert_policy_block(result)
    assert any(label in violation for violation in result.violations)


def test_presented_trailer_drift_block() -> None:
    result = consume_commit_capability(
        _tampered(presented_mutate=lambda p: p["trailers"].__setitem__("Review-Id", "review-9999"))
    )
    _assert_policy_block(result)
    assert any("trailer" in violation for violation in result.violations)

    result = consume_commit_capability(
        _tampered(presented_mutate=lambda p: p["trailers"].__setitem__("Extra", "x"))
    )
    _assert_policy_block(result)

    result = consume_commit_capability(
        _tampered(presented_mutate=lambda p: p["trailers"].pop("Task-Id"))
    )
    _assert_policy_block(result)


def test_presented_path_outside_the_allowed_set_blocks() -> None:
    # A file authorisation covers only the exact path: its neighbour is out.
    result = consume_commit_capability(
        _tampered(
            presented_mutate=lambda p: p.__setitem__(
                "touched_paths", ["tests/app/test_other.py"]
            )
        )
    )
    _assert_policy_block(result)
    assert any("outside" in violation for violation in result.violations)

    # A directory authorisation does not cover a sibling tree.
    result = consume_commit_capability(
        _tampered(presented_mutate=lambda p: p.__setitem__("touched_paths", ["src/appx/core.py"]))
    )
    _assert_policy_block(result)


def test_touched_git_component_inside_an_allowed_directory_blocks() -> None:
    """The exact-``.git``-component ban is an independent gate, not a
    consequence of "outside the allowed set": the allowed set here is the
    ``src/app`` directory, so the path is *inside* it — only the ``.git``
    rule rejects ``src/app/.git/config``. Killing the ``.git`` line alone
    must flip this test (review M8 survivor)."""
    result = consume_commit_capability(
        _tampered(
            presented_mutate=lambda p: p.__setitem__(
                "touched_paths", ["src/app/.git/config"]
            )
        )
    )
    _assert_policy_block(result)
    assert any(".git" in violation for violation in result.violations)


def test_consume_tampering_labels_are_collected_not_short_circuited() -> None:
    """Every divergence is labelled: an audit trail names all of them."""
    presented = _presented()
    presented["capability_id"] = "cap-9999"
    presented["base_sha"] = _git_sha("other-base")
    presented["result_sha"] = _git_sha("other-result")
    presented["touched_paths"] = ["deploy/secret.sh"]
    result = consume_commit_capability(_consume_facts(presented=presented))
    _assert_policy_block(result)
    assert len(result.violations) >= 4


def test_consume_presentation_string_subclasses_block_not_crash() -> None:
    """Provider-adjacent presentation values are untrusted: a hashable str
    subclass there lands the tamper block, never a TypeError, never a pass —
    while the same shape in the capability row is trusted drift and raises
    (see the paired test above)."""
    presented = _presented()
    presented["capability_id"] = _UnhashableStr("cap-0001")
    result = consume_commit_capability(_consume_facts(presented=presented))
    _assert_policy_block(result)


# ---------------------------------------------------------------------------
# (E) classification order: a dead capability refuses before tamper judging.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dead",
    ["consumed", "revoked", "expired", "epoch_bumped"],
)
def test_a_dead_capability_refuses_before_judging_tampering(dead: str) -> None:
    presented = _presented()
    presented["result_sha"] = _git_sha("other-result")
    row = _capability_row()
    now = NOW
    if dead == "consumed":
        row = _capability_row(uses_consumed=1)
    elif dead == "revoked":
        row = _capability_row(revoked_at=NOW - 10)
    elif dead == "expired":
        row = _capability_row()
        row["expires_at"] = NOW - 1
    else:  # epoch_bumped: the feature's epoch moved past the binding's
        row = _capability_row()
    facts = _consume_facts(capability=row, presented=presented, now=now)
    if dead == "epoch_bumped":
        facts["current_epoch"] = EPOCH + 1
    result = consume_commit_capability(facts)
    assert result.receipt.code is ReceiptCode.CAPABILITY_STALE
    assert result.declared_write_set == ()
    assert result.violations == ()


# ---------------------------------------------------------------------------
# (F) module hygiene.
# ---------------------------------------------------------------------------


def _assert_pure_source(source: str) -> None:
    """A bounded regression guard, not a proof of process isolation."""
    tree = ast.parse(source)
    nodes = list(ast.walk(tree))
    assert not any(isinstance(n, ast.Import) for n in nodes)
    imports = set()
    for node in nodes:
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0
            for alias in node.names:
                assert alias.asname is None
                imports.add((node.module, alias.name))
    assert imports == {
        ("__future__", "annotations"), ("dataclasses", "dataclass"),
        ("typing", "Any"), ("typing", "Final"),
        ("personal_agent_dal.errors", "DalError"),
        ("personal_agent_dal.errors", "DalErrorCode"),
        ("personal_agent_dal.receipt", "OperationReceipt"),
        ("personal_agent_dal.receipt", "ReceiptCode"),
    }
    local_functions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    allowed_calls = local_functions | {
        "dataclass", "DalError", "OperationReceipt", "CommitCapabilityEvaluation",
        "all", "any", "bool", "frozenset", "isinstance", "len", "set", "tuple", "type",
        "enumerate",
    }
    for node in nodes:
        if isinstance(node, ast.Name):
            assert node.id not in {
                "open", "__import__", "eval", "exec", "compile", "getattr",
                "setattr", "delattr", "globals", "locals", "vars", "__builtins__",
                "input", "print", "breakpoint",
            }
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                assert node.func.id in allowed_calls
            else:
                assert isinstance(node.func, ast.Attribute)
                assert node.func.attr in {"append", "add", "get", "items", "values", "split", "startswith"}
        if isinstance(node, ast.Attribute):
            assert not node.attr.startswith("__")
            assert node.attr not in {"environ", "stdin", "stdout", "stderr"}


def test_pure_module_dependency_surface_is_closed() -> None:
    _assert_pure_source(Path(commit_capability.__file__).read_text(encoding="utf-8"))


def test_module_declares_no_operation_spec_id() -> None:
    """A stage-internal guard is not a dispatchable operation."""
    assert not hasattr(commit_capability, "OPERATION_SPEC_ID")
