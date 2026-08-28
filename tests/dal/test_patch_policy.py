"""DAL-028 (R09-A1): the patch policy scanner — the model-patch boundary.

The scanner judges a model-produced patch against a closed six-stage rule
pipeline before the patch may become a candidate commit:

- ``leak_fingerprint`` — a configured credential canary appears verbatim in a
  file's content (the §5.2 ordering rule: leak checks precede fact checks);
- ``leak_structural`` — PEM private-key markers and provider key shapes with
  a long alphanumeric tail;
- ``protected_path`` — protected prefixes (``.git``/``deploy``/``config``/the
  key inventory) and dotenv basenames;
- ``out_of_bounds`` — a path outside every allowed path of the approved plan
  (component-boundary prefix match);
- ``binary`` — a non-reviewable payload;
- ``size`` — an over-policy file size.

Each rule test below is isolated: every other dimension of the fixture stays
inside policy so the failure can only be attributed to the rule under test.
Malformed shapes fail closed as ``INVALID_ARGUMENT``; the verdict never
carries matched content; and the pure module cannot acquire an unguarded I/O
dependency.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.machine import patch_policy


ALLOWED_PATHS = ["src/app", "tests/app"]
FINGERPRINTS = ["canary-3f9a2b7c"]
TARGET = {
    "entity_id": "feature-0001",
    "entity_type": "feature",
    "state": "coding",
    "version": 3,
}


def _file(
    path: str,
    *,
    change_type: str = "add",
    size_bytes: int | None = None,
    is_binary: bool = False,
    content: str = "plain text\n",
) -> dict:
    return {
        "path": path,
        "change_type": change_type,
        "size_bytes": len(content.encode()) if size_bytes is None else size_bytes,
        "is_binary": is_binary,
        "content": content,
    }


def _facts(files: list[dict]) -> dict:
    return {
        "schema_version": "dal.patch-scan-facts/1.0",
        "target": dict(TARGET),
        "allowed_paths": list(ALLOWED_PATHS),
        "secret_fingerprints": list(FINGERPRINTS),
        "files": files,
    }


# ---------------------------------------------------------------------------
# Clean patches: every stage passes.
# ---------------------------------------------------------------------------


def test_clean_patch_passes_all_stages() -> None:
    facts = _facts(
        [
            _file("src/app/service.py", change_type="modify", content="value = 1\n"),
            _file("tests/app/test_service.py"),
            _file("src/app/old.py", change_type="delete", content=""),
        ]
    )
    verdict = patch_policy.evaluate_patch_policy(facts)
    assert verdict == patch_policy.PatchPolicyEvaluation(conflict=False)


def test_boundary_path_inside_allowed_prefix_passes() -> None:
    """``src/app`` itself and ``src/app/x.py`` are inside; the match is a
    component-boundary match, not a raw string prefix."""
    verdict = patch_policy.evaluate_patch_policy(_facts([_file("src/app")]))
    assert verdict.conflict is False


def test_component_boundary_is_not_a_string_prefix_match() -> None:
    """``src/appx`` shares the string prefix with ``src/app`` but is outside
    every allowed path."""
    verdict = patch_policy.evaluate_patch_policy(
        _facts([_file("src/appx/service.py")])
    )
    assert (verdict.conflict, verdict.rule) == (True, "out_of_bounds")
    assert verdict.path == "src/appx/service.py"


# ---------------------------------------------------------------------------
# Leak stages precede fact stages (§5.2: leak checks precede correctness).
# ---------------------------------------------------------------------------


def test_fingerprint_hit_reports_leak_fingerprint_first() -> None:
    """A file that simultaneously leaks a canary, sits on a protected path and
    is oversized must report the leak stage — not the fact stage."""
    facts = _facts(
        [
            _file(
                "config/leak.env",
                content="TOKEN=canary-3f9a2b7c\n" + "x" * 600_000,
            )
        ]
    )
    verdict = patch_policy.evaluate_patch_policy(facts)
    assert (verdict.conflict, verdict.rule) == (True, "leak_fingerprint")
    assert verdict.path == "config/leak.env"


def test_structural_pem_marker_hits() -> None:
    verdict = patch_policy.evaluate_patch_policy(
        _facts([_file("src/app/keys.py", content="-----BEGIN PRIVATE KEY-----\n")])
    )
    assert (verdict.conflict, verdict.rule) == (True, "leak_structural")


def test_structural_provider_key_shape_hits() -> None:
    tail = "a" * 24
    verdict = patch_policy.evaluate_patch_policy(
        _facts([_file("src/app/client.py", content=f"gsk_{tail}\n")])
    )
    assert (verdict.conflict, verdict.rule) == (True, "leak_structural")


def test_structural_key_prefix_inside_word_does_not_hit() -> None:
    """``task-`` before ``sk-`` is alphanumeric-adjacent; no key shape."""
    verdict = patch_policy.evaluate_patch_policy(
        _facts([_file("src/app/names.py", content="task-sk-0001\n")])
    )
    assert verdict.conflict is False


def test_short_tail_after_key_prefix_does_not_hit() -> None:
    """A ``sk-`` with a short tail is not a key shape."""
    verdict = patch_policy.evaluate_patch_policy(
        _facts([_file("src/app/plan.py", content="sk-1234\n")])
    )
    assert verdict.conflict is False


# ---------------------------------------------------------------------------
# Protected paths.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        ".git/hooks/pre-commit",
        ".github/workflows/ci.yml",
        "deploy/backup.sh",
        "config/ledger.json",
        "docs/密钥清单_v0.1.md",
        ".env",
        ".env.local",
        "src/app/.env.production",
    ],
)
def test_protected_path_hits(path: str) -> None:
    verdict = patch_policy.evaluate_patch_policy(_facts([_file(path)]))
    assert (verdict.conflict, verdict.rule) == (True, "protected_path")
    assert verdict.path == path


def test_dotenv_like_basename_is_not_overbroad() -> None:
    """``env.py`` and ``dotenv`` are ordinary names, not protected."""
    verdict = patch_policy.evaluate_patch_policy(
        _facts([_file("src/app/env.py"), _file("src/app/dotfile")])
    )
    assert verdict.conflict is False


# ---------------------------------------------------------------------------
# Out of bounds.
# ---------------------------------------------------------------------------


def test_absolute_path_is_out_of_bounds() -> None:
    verdict = patch_policy.evaluate_patch_policy(_facts([_file("/etc/passwd")]))
    assert (verdict.conflict, verdict.rule) == (True, "out_of_bounds")


def test_dotdot_path_is_out_of_bounds() -> None:
    verdict = patch_policy.evaluate_patch_policy(
        _facts([_file("src/app/../../escape.py")])
    )
    assert (verdict.conflict, verdict.rule) == (True, "out_of_bounds")


# ---------------------------------------------------------------------------
# Binary and size (delete entries are exempt from both).
# ---------------------------------------------------------------------------


def test_binary_file_hits() -> None:
    verdict = patch_policy.evaluate_patch_policy(
        _facts([_file("src/app/blob.bin", is_binary=True)])
    )
    assert (verdict.conflict, verdict.rule) == (True, "binary")


def test_oversized_file_hits() -> None:
    big = "x" * 600_000
    verdict = patch_policy.evaluate_patch_policy(
        _facts([_file("src/app/big.txt", size_bytes=len(big.encode()), content=big)])
    )
    assert (verdict.conflict, verdict.rule) == (True, "size")


def test_delete_entry_is_exempt_from_binary_and_size() -> None:
    facts = _facts(
        [_file("src/app/old.bin", change_type="delete", content="", size_bytes=10**9)]
    )
    verdict = patch_policy.evaluate_patch_policy(facts)
    assert verdict.conflict is False


def test_pipeline_reports_earliest_stage_across_files() -> None:
    """The first stage in RULE_ORDER with any hit wins, regardless of file
    order in the patch."""
    facts = _facts(
        [
            _file("src/app/big.txt", size_bytes=600_000),
            _file("deploy/x.sh"),
        ]
    )
    verdict = patch_policy.evaluate_patch_policy(facts)
    assert verdict.rule == "protected_path"


# ---------------------------------------------------------------------------
# Fail closed: malformed shapes are stable DalError, never Python errors.
# ---------------------------------------------------------------------------


MALFORMED_CASES = [
    ("facts not an object", None),
    ("empty facts", {}),
    ("unknown fact field", {**_facts([]), "extra": 1}),
    ("missing fact field", {k: v for k, v in _facts([]).items() if k != "files"}),
    ("wrong schema version", {**_facts([]), "schema_version": "dal.patch-scan/2.0"}),
    (
        "wrong target state",
        {**_facts([]), "target": {**TARGET, "state": "intake"}},
    ),
    (
        "wrong entity type",
        {**_facts([]), "target": {**TARGET, "entity_type": "recovery_case"}},
    ),
    ("empty allowed paths", {**_facts([]), "allowed_paths": []}),
    (
        "absolute allowed path",
        {**_facts([]), "allowed_paths": ["/abs"]},
    ),
    (
        "trailing slash allowed path",
        {**_facts([]), "allowed_paths": ["src/"]},
    ),
    ("empty fingerprints", {**_facts([]), "secret_fingerprints": []}),
    (
        "empty file list",
        {**_facts([]), "files": []},
    ),
    (
        "unknown file field",
        {
            **_facts([]),
            "files": [{**_file("src/app/x.py"), "mode": "0755"}],
        },
    ),
    (
        "missing file field",
        {
            **_facts([]),
            "files": [
                {k: v for k, v in _file("src/app/x.py").items() if k != "is_binary"}
            ],
        },
    ),
    (
        "unknown change type",
        {**_facts([]), "files": [_file("src/app/x.py", change_type="rename")]},
    ),
    (
        "negative size",
        {**_facts([]), "files": [_file("src/app/x.py", size_bytes=-1)]},
    ),
    (
        "boolean size",
        {**_facts([]), "files": [_file("src/app/x.py", size_bytes=True)]},
    ),
    (
        "delete with content",
        {**_facts([]), "files": [_file("src/app/x.py", change_type="delete")]},
    ),
    (
        "duplicate file path",
        {
            **_facts([]),
            "files": [_file("src/app/x.py"), _file("src/app/x.py")],
        },
    ),
    (
        "empty path",
        {**_facts([]), "files": [_file("")]},
    ),
]


@pytest.mark.parametrize("label,malformed", MALFORMED_CASES)
def test_malformed_facts_fail_closed(label: str, malformed: object) -> None:
    with pytest.raises(DalError) as raised:
        patch_policy.evaluate_patch_policy(malformed)
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT, label


# ---------------------------------------------------------------------------
# Verdict hygiene and dependency surface.
# ---------------------------------------------------------------------------


def test_verdict_does_not_echo_content() -> None:
    """The deny verdict carries rule + path only — never the matched
    fingerprint, key material or patch body."""
    canary = "canary-3f9a2b7c"
    verdict = patch_policy.evaluate_patch_policy(
        _facts([_file("src/app/leak.py", content=f"TOKEN={canary}\n")])
    )
    assert verdict.conflict is True
    data = (verdict.rule or "", verdict.path or "")
    assert canary not in " ".join(data)
    assert "TOKEN" not in " ".join(data)


def test_block_reason_matches_frozen_policy_block() -> None:
    assert patch_policy.BLOCK_REASON == "POLICY_FAILURE"


def test_rule_order_keeps_leak_stages_first() -> None:
    assert patch_policy.RULE_ORDER[:2] == ("leak_fingerprint", "leak_structural")


def test_pure_module_dependency_surface_is_closed() -> None:
    """The scanner cannot acquire an unguarded I/O dependency."""
    expected_imports = {
        "__future__",
        "dataclasses",
        "typing",
        "personal_agent_dal.errors",
    }
    tree = ast.parse(
        Path(patch_policy.__file__).read_text(encoding="utf-8")
    )
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert imports == expected_imports


def test_module_declares_no_operation_spec_id() -> None:
    """The scanner is a stage-internal guard, not a dispatchable operation;
    it must not invent an unregistered spec id."""
    assert not hasattr(patch_policy, "OPERATION_SPEC_ID")
