"""Independent adversarial vectors from the 4494fe8b review (F1-F9).

These facts are constructed here, not copied from the implementation's field
constants. No provider, executor, persistent consumption or CAS is simulated.
"""

from copy import deepcopy
import json
from pathlib import Path
import sys
from types import ModuleType

import pytest

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.machine import commit_capability as gate
from personal_agent_dal.receipt import ReceiptCode
from tests.dal.test_commit_capability import _assert_pure_source


class NativeLookingStr(str):
    pass


class UnhashableStr(str):
    __hash__ = None


class ExplodingStr(str):
    def __eq__(self, other):
        raise AssertionError("must not compare an untrusted subclass")

    def __ne__(self, other):
        raise AssertionError("must not compare an untrusted subclass")

    __hash__ = str.__hash__


class DirectorySpoof(str):
    def __hash__(self):
        return hash("directory")

    def __eq__(self, other):
        return other == "directory"


class HostileDict(dict):
    def get(self, *args):
        raise AssertionError("must not dereference a non-native object")


def facts():
    return {
        "schema_version": "dal.commit-capability-consume-facts/1.0",
        "target": {"entity_id": "feature-A", "entity_type": "feature", "state": "verified", "version": 5},
        "capability": {
            "capability_id": "cap-A", "approval_id": "approval-A",
            "base_sha": "a" * 40, "result_sha": "b" * 40,
            "allowed_paths": [{"path": "src/item", "path_type": "file"}],
            "trailers": {"Feature-Id": "feature-A", "Task-Id": "task-A", "Plan-Hash": "c" * 64, "Review-Id": "review-A"},
            "idempotency_key": "key-A", "expires_at": 200, "max_uses": 1,
            "capability_epoch": 3, "lease_epoch": 7,
            "uses_consumed": 0, "consumed_by": None, "revoked_at": None,
        },
        "presented": {
            "capability_id": "cap-A", "approval_id": "approval-A",
            "base_sha": "a" * 40, "result_sha": "b" * 40,
            "idempotency_key": "key-A", "touched_paths": ["src/item"],
            "trailers": {"Feature-Id": "feature-A", "Task-Id": "task-A", "Plan-Hash": "c" * 64, "Review-Id": "review-A"},
        },
        "now": 100, "current_epoch": 3, "current_lease_epoch": 7,
    }


def issue_facts():
    f = facts()
    binding = f["capability"]
    for field in ("uses_consumed", "consumed_by", "revoked_at"):
        del binding[field]
    return {"schema_version": "dal.commit-capability-issue-facts/1.0", "target": f["target"], "binding": binding, "now": 100}


def assert_invalid(fn, value):
    with pytest.raises(DalError) as raised:
        fn(value)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


def assert_stale(result):
    assert result == gate.CommitCapabilityEvaluation(
        receipt=gate.OperationReceipt(ReceiptCode.CAPABILITY_STALE, schema_version="dal.transition-receipt/1.0"),
        state_trace=("verified",), final_state="verified", final_entity_type="feature",
    )


def assert_block(result):
    assert result.receipt.code is ReceiptCode.APPLIED
    assert result.receipt.schema_version == "dal.transition-receipt/1.0"
    assert result.state_trace == ("verified", "needs_human")
    assert result.final_state == "needs_human"
    assert result.final_entity_type == "feature"
    assert result.final_reason_code == "POLICY_FAILURE"
    assert result.final_reason_owner == "feature"
    assert result.declared_write_set == (
        "aggregate", "business_event", "transition_receipt", "audit",
        "decision_create", "decision_projection", "notification_outbox",
    )
    assert result.event_trace == ("feature.blocked",)
    assert result.violations


def make_dead(f, dead):
    if dead == "consumed":
        f["capability"].update(uses_consumed=1, consumed_by="command-A")
    elif dead == "revoked":
        f["capability"]["revoked_at"] = 99
    elif dead == "expired":
        f["now"] = 201
    elif dead == "capability_epoch":
        f["current_epoch"] += 1
    else:
        f["current_lease_epoch"] += 1


def test_valid_bound_facts_are_eligible_without_mutation():
    f = facts()
    before = deepcopy(f)
    assert gate.issue_commit_capability(issue_facts()).violations == ()
    for _ in range(2):
        result = gate.consume_commit_capability(f)
        assert result.receipt.code is ReceiptCode.APPLIED
        assert result.final_state == "verified"
        assert result.violations == ()
    assert f == before  # Pure eligibility is deliberately not a consume-once CAS.


def test_capability_cannot_cross_feature_even_when_presentation_agrees():
    f = facts()
    f["target"]["entity_id"] = "feature-B"
    f["presented"]["trailers"]["Feature-Id"] = "feature-B"
    assert_invalid(gate.consume_commit_capability, f)


@pytest.mark.parametrize("key", ["Feature-Id", "Task-Id", "Plan-Hash", "Review-Id"])
def test_each_frozen_trailer_value_is_bound(key):
    f = facts()
    f["presented"]["trailers"][key] = "d" * 64 if key == "Plan-Hash" else "other"
    result = gate.consume_commit_capability(f)
    assert_block(result)
    assert any(key in label for label in result.violations)


def test_approval_identity_is_bound():
    f = facts()
    f["presented"]["approval_id"] = "approval-B"
    assert_block(gate.consume_commit_capability(f))


@pytest.mark.parametrize("location", ["facts", "target", "capability", "trailers", "path_entry"])
@pytest.mark.parametrize("mutation", ["missing", "extra", "subclass"])
def test_trusted_nested_shapes_are_closed_before_liveness(location, mutation):
    f = facts()
    make_dead(f, "consumed")
    container = {
        "facts": f, "target": f["target"], "capability": f["capability"],
        "trailers": f["capability"]["trailers"],
        "path_entry": f["capability"]["allowed_paths"][0],
    }[location]
    if mutation == "missing":
        del container[next(iter(container))]
    elif mutation == "extra":
        container["extra"] = None
    elif location == "facts":
        f = HostileDict(f)
    elif location in {"target", "capability"}:
        f[location] = HostileDict(container)
    elif location == "trailers":
        f["capability"]["trailers"] = HostileDict(container)
    else:
        f["capability"]["allowed_paths"][0] = HostileDict(container)
    assert_invalid(gate.consume_commit_capability, f)


@pytest.mark.parametrize("field", ["approval_id", "lease_epoch"])
def test_required_issue_binding_fields_cannot_be_omitted(field):
    f = issue_facts()
    del f["binding"][field]
    assert_invalid(gate.issue_commit_capability, f)


@pytest.mark.parametrize("field,value", [("consumed_by", ""), ("consumed_by", 3), ("consumed_by", UnhashableStr("command-A")), ("uses_consumed", 2)])
def test_malformed_consumption_record_is_trusted_drift(field, value):
    f = facts()
    f["capability"][field] = value
    assert_invalid(gate.consume_commit_capability, f)


@pytest.mark.parametrize("uses,consumer", [(0, "command-A"), (1, None)])
def test_consumption_count_and_identity_cannot_disagree(uses, consumer):
    f = facts()
    f["capability"].update(uses_consumed=uses, consumed_by=consumer)
    assert_invalid(gate.consume_commit_capability, f)


@pytest.mark.parametrize("current", ["current_epoch", "current_lease_epoch"])
def test_each_epoch_independently_revokes(current):
    f = facts()
    f[current] += 1
    assert_stale(gate.consume_commit_capability(f))
    f[current] -= 2
    assert_invalid(gate.consume_commit_capability, f)


@pytest.mark.parametrize("value", [[], {}, UnhashableStr("file"), NativeLookingStr("file"), DirectorySpoof("file")])
@pytest.mark.parametrize("entrypoint", ["issue", "consume"])
def test_path_type_checked_before_hash_or_comparison(value, entrypoint):
    f = issue_facts() if entrypoint == "issue" else facts()
    binding = f["binding"] if entrypoint == "issue" else f["capability"]
    binding["allowed_paths"][0]["path_type"] = value
    if entrypoint == "consume":
        f["presented"]["touched_paths"] = ["src/item/escape"]
    assert_invalid(gate.issue_commit_capability if entrypoint == "issue" else gate.consume_commit_capability, f)


@pytest.mark.parametrize("location", ["schema", "state", "entity_type", "trailer_key", "fact_key", "path_key", "capability_key", "target_key"])
@pytest.mark.parametrize("entrypoint", ["issue", "consume"])
def test_all_trusted_strings_and_keys_are_native(location, entrypoint):
    f = issue_facts() if entrypoint == "issue" else facts()
    binding = f.get("binding", f.get("capability"))
    if location == "schema":
        f["schema_version"] = ExplodingStr(f["schema_version"])
    elif location in {"state", "entity_type"}:
        f["target"][location] = ExplodingStr(f["target"][location])
    else:
        container, key = {
            "trailer_key": (binding["trailers"], "Task-Id"), "fact_key": (f, "now"),
            "path_key": (binding["allowed_paths"][0], "path"),
            "capability_key": (binding, "capability_id"), "target_key": (f["target"], "state"),
        }[location]
        value = container.pop(key)
        container[NativeLookingStr(key)] = value
    assert_invalid(gate.issue_commit_capability if entrypoint == "issue" else gate.consume_commit_capability, f)


@pytest.mark.parametrize("value", [None, 7, [["Review-Id"]], ["Feature-Id", "Task-Id", "Plan-Hash", "Review-Id"], HostileDict()])
def test_malformed_live_trailers_block_without_dereferencing(value):
    f = facts()
    f["presented"]["trailers"] = value
    assert_block(gate.consume_commit_capability(f))


@pytest.mark.parametrize("field", ["capability_id", "approval_id", "base_sha", "result_sha", "idempotency_key"])
def test_untrusted_scalar_comparisons_never_call_subclass_methods(field):
    f = facts()
    f["presented"][field] = ExplodingStr(f["presented"][field])
    assert_block(gate.consume_commit_capability(f))


@pytest.mark.parametrize("location", ["presented", "trailer_key", "trailer_value", "path"])
def test_nested_untrusted_native_guards(location):
    f = facts()
    p = f["presented"]
    if location == "presented":
        f["presented"] = HostileDict(p)
    elif location == "path":
        p["touched_paths"] = [UnhashableStr("src/item")]
    elif location == "trailer_key":
        value = p["trailers"].pop("Task-Id")
        p["trailers"][ExplodingStr("Task-Id")] = value
    else:
        p["trailers"]["Task-Id"] = ExplodingStr("task-A")
    assert_block(gate.consume_commit_capability(f))


@pytest.mark.parametrize("dead", ["consumed", "revoked", "expired", "capability_epoch", "lease_epoch"])
@pytest.mark.parametrize("bad", ["none", "missing", "empty", "dotgit", "subclass", "trailers", "object"])
def test_dead_capability_ignores_every_untrusted_presentation(dead, bad):
    f = facts()
    make_dead(f, dead)
    if bad == "none":
        f["presented"] = None
    elif bad == "missing":
        del f["presented"]["capability_id"]
    elif bad == "empty":
        f["presented"]["touched_paths"] = []
    elif bad == "dotgit":
        f["presented"]["touched_paths"] = ["src/.git/config"]
    elif bad == "subclass":
        f["presented"]["touched_paths"] = [ExplodingStr("src/item")]
    elif bad == "object":
        f["presented"] = HostileDict()
    else:
        f["presented"]["trailers"] = None
    assert_stale(gate.consume_commit_capability(f))


def test_trusted_validation_still_precedes_dead_refusal():
    f = facts()
    make_dead(f, "consumed")
    f["capability"]["lease_epoch"] = True
    f["presented"] = None
    assert_invalid(gate.consume_commit_capability, f)


@pytest.mark.parametrize("path", ["src/item/child", "src/items", ".git/config", "src/.git/config", "src/./item", "src/../item", "src//item", "/src/item", "src/item/", "", "src/item\0"])
def test_file_or_malformed_path_cannot_escape(path):
    f = facts()
    f["presented"]["touched_paths"] = [path]
    assert_block(gate.consume_commit_capability(f))


def test_directory_contains_self_and_children_not_siblings():
    f = facts()
    f["capability"]["allowed_paths"][0]["path_type"] = "directory"
    for path in ("src/item", "src/item/child"):
        f["presented"]["touched_paths"] = [path]
        assert gate.consume_commit_capability(f).violations == ()
    f["presented"]["touched_paths"] = ["src/items"]
    assert_block(gate.consume_commit_capability(f))


def test_trailer_set_error_does_not_hide_independent_value_errors():
    f = facts()
    f["presented"]["trailers"].update(Extra="x", **{"Review-Id": "review-B", "Task-Id": "task-B"})
    result = gate.consume_commit_capability(f)
    assert_block(result)
    assert any("trailer set" in label for label in result.violations)
    for field in ("Review-Id", "Task-Id"):
        assert any(field in label for label in result.violations)


def test_unsafe_sibling_does_not_hide_other_safe_violations():
    f = facts()
    f["presented"]["trailers"] = None
    f["presented"]["result_sha"] = "d" * 40
    f["presented"]["touched_paths"] = [[], "outside", "outside"]
    result = gate.consume_commit_capability(f)
    assert_block(result)
    for label in ("trailers", "result_sha", "index 0", "outside", "repeats"):
        assert any(label in violation for violation in result.violations)


def test_block_matches_frozen_registry_without_local_expected_constants():
    repo = Path(__file__).resolve().parents[2]
    registry = json.loads((repo / "docs/dal/manifests/transition-spec-registry_v1.0.json").read_text())
    def find(node):
        if isinstance(node, dict):
            if node.get("spec_id") == "BLK-POLICY--verified":
                return node
            nodes = node.values()
        elif isinstance(node, list):
            nodes = node
        else:
            return None
        return next((found for child in nodes if (found := find(child)) is not None), None)
    spec = find(registry)
    assert spec is not None
    f = facts()
    f["presented"]["result_sha"] = "d" * 40
    r = gate.consume_commit_capability(f)
    assert r.state_trace == (spec["from_state"], spec["to_state"])
    assert r.final_state == spec["to_state"]
    assert r.final_entity_type == spec["aggregate_type"]
    assert r.final_reason_code == spec["result_reason_code"]
    assert r.final_reason_owner == spec["result_reason_owner"]
    assert r.declared_write_set == tuple(spec["atomic_write_set"])
    assert r.event_trace == (spec["event_type"],)
    assert r.receipt.code.value == spec["success_receipt_code"]
    assert r.receipt.schema_version == spec["success_receipt_schema"]


@pytest.mark.parametrize("addition", [
    "import os", "from os import environ", "from typing import IO",
    "def bad():\n    return open('not-executed')",
    "def bad():\n    return __import__('os')",
    "def bad():\n    return globals()['open']('not-executed')",
    "def bad():\n    return getattr(object, '__subclasses__')()",
    "def bad():\n    return os.environ",
    "reader = open\ndef bad():\n    return reader('not-executed')",
    "tuple = open\ndef bad():\n    return tuple('not-executed')",
])
def test_hygiene_guard_rejects_io_mutations_without_executing_them(addition):
    source = Path(gate.__file__).read_text()
    _assert_pure_source(source)
    with pytest.raises(AssertionError):
        _assert_pure_source(source + "\n" + addition + "\n")


@pytest.mark.parametrize("mutation", [
    "target_binding", "feature_trailer", "task_plan_trailer",
    "lease_revocation", "stale_trace", "block_receipt",
])
def test_review_regressions_kill_in_memory_mutants(monkeypatch, mutation):
    """Prove the assertions detect six concrete regressions; never edit disk."""
    source = Path(gate.__file__).read_text()
    if mutation == "target_binding":
        old = 'if capability["trailers"]["Feature-Id"] != facts["target"]["entity_id"]:'
        changed = source.replace(old, "if False:")
        check = test_capability_cannot_cross_feature_even_when_presentation_agrees
    elif mutation in {"feature_trailer", "task_plan_trailer"}:
        old = 'for key in ("Feature-Id", "Task-Id", "Plan-Hash", "Review-Id"):'
        changed = source.replace(old, 'for key in ("Review-Id",):')
        key = "Feature-Id" if mutation == "feature_trailer" else "Task-Id"
        check = lambda: test_each_frozen_trailer_value_is_bound(key)
    elif mutation == "lease_revocation":
        old = 'if capability["lease_epoch"] != facts["current_lease_epoch"]:'
        changed = source.replace(old, "if False:")
        check = lambda: test_each_epoch_independently_revokes("current_lease_epoch")
    elif mutation == "stale_trace":
        head, tail = source.split("def _stale_refusal", 1)
        changed = head + "def _stale_refusal" + tail.replace('state_trace=(VERIFIED_STATE,),', 'state_trace=("coding",),', 1)
        check = lambda: test_each_epoch_independently_revokes("current_epoch")
    else:
        head, tail = source.split("def _policy_block", 1)
        changed = head + "def _policy_block" + tail.replace('schema_version=FEATURE_TRANSITION_RECEIPT_SCHEMA,', 'schema_version="wrong-schema",', 1)
        check = test_block_matches_frozen_registry_without_local_expected_constants
    assert changed != source, "mutation anchor drifted"
    check()  # A mutant cannot 'fail' merely because the original check is broken.
    mutant = ModuleType("commit_capability_review_mutant")
    monkeypatch.setitem(sys.modules, mutant.__name__, mutant)
    exec(compile(changed, gate.__file__, "exec"), mutant.__dict__)
    monkeypatch.setattr(sys.modules[__name__], "gate", mutant)
    with pytest.raises((AssertionError, pytest.fail.Exception)):
        check()
