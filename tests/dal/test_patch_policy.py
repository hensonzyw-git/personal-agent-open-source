"""DAL-028 (R09-A1): the patch policy scanner — the model-patch boundary.

The scanner judges a model-produced patch against a closed eight-stage rule
pipeline before the patch may become a candidate commit:

- ``leak_fingerprint`` — a configured credential canary appears verbatim in a
  file's path **or** content (leak checks precede fact checks, §5.2);
- ``leak_structural`` — PEM markers (encrypted form included) and provider
  key shapes, with delimiter-bearing tails (``sk-proj-…``) under their own
  prefix entries;
- ``protected_path`` — component-boundary trees plus deliberate wide matches
  (``.git*`` components, ``.env`` basenames);
- ``out_of_bounds`` — outside the frozen allowed-paths semantics
  (DAL021-024: ``path_type=file`` matches only the exact path, ``directory``
  covers the component subtree);
- ``binary`` / ``size`` / ``size_aggregate`` / ``file_count`` — the
  reviewability and volume policy.

Each rule test below isolates the rule under test: every other dimension of
the fixture stays inside policy so the failure can only be attributed to that
rule. Malformed shapes fail closed as ``INVALID_ARGUMENT``; leak verdicts
never echo the (possibly secret-bearing) path; and the pure module cannot
acquire an unguarded I/O dependency.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.machine import patch_policy


ALLOWED_PATHS = [
    {"path": "src/app", "path_type": "directory"},
    {"path": "tests/app/test_service.py", "path_type": "file"},
]
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
    if change_type == "delete" or is_binary:
        content = ""  # delete and binary entries carry no inspectable content
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
        "allowed_paths": [dict(entry) for entry in ALLOWED_PATHS],
        "secret_fingerprints": list(FINGERPRINTS),
        "files": files,
    }


# ---------------------------------------------------------------------------
# Clean patches: every stage passes.
# ---------------------------------------------------------------------------


def test_clean_patch_passes_all_stages() -> None:
    facts = _facts(
        [
            _file("src/app/service.py", change_type="modify"),
            _file("tests/app/test_service.py"),
            _file("src/app/old.py", change_type="delete"),
        ]
    )
    verdict = patch_policy.evaluate_patch_policy(facts)
    assert verdict == patch_policy.PatchPolicyEvaluation(conflict=False)


def test_boundary_path_inside_allowed_directory_passes() -> None:
    """``src/app`` itself and ``src/app/x.py`` are inside the directory
    authorisation; the match is a component-boundary match."""
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


def test_file_type_authorisation_is_exact_only() -> None:
    """A ``path_type=file`` authorisation covers only that exact path — a
    child file is out of bounds (review blocker 1)."""
    verdict = patch_policy.evaluate_patch_policy(
        _facts([_file("tests/app/test_service.py/child.py")])
    )
    assert (verdict.conflict, verdict.rule) == (True, "out_of_bounds")


def test_sibling_of_file_authorisation_is_out_of_bounds() -> None:
    verdict = patch_policy.evaluate_patch_policy(
        _facts([_file("tests/app/conftest.py")])
    )
    assert (verdict.conflict, verdict.rule) == (True, "out_of_bounds")


# ---------------------------------------------------------------------------
# Leak stages precede fact stages (§5.2: leak checks precede correctness).
# ---------------------------------------------------------------------------


def test_fingerprint_in_content_reports_leak_first() -> None:
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


def test_fingerprint_in_filename_alone_hits() -> None:
    """Clean content, secret-bearing path — still a leak (review blocker 2)."""
    verdict = patch_policy.evaluate_patch_policy(
        _facts([_file("src/app/canary-3f9a2b7c.txt")])
    )
    assert (verdict.conflict, verdict.rule) == (True, "leak_fingerprint")
    assert verdict.path is None  # leak verdict never echoes the path


def test_leak_verdict_does_not_echo_secret_bearing_path() -> None:
    """When the key shape sits in the filename, verdict.path must be unset;
    the deny verdict must not become a new leak channel (review blocker 3)."""
    secret = "gsk_" + "a1b2c3d4e5f6g7h8i9j0"
    verdict = patch_policy.evaluate_patch_policy(
        _facts([_file(f"src/app/{secret}.txt", content="clean\n")])
    )
    assert (verdict.conflict, verdict.rule) == (True, "leak_structural")
    assert verdict.path is None
    assert verdict.file_index == 0
    assert secret not in (verdict.rule or "")


def test_non_leak_verdicts_still_carry_path() -> None:
    """Fact-stage verdicts keep the path for debuggability — the path is not
    sensitive in those stages."""
    verdict = patch_policy.evaluate_patch_policy(_facts([_file("deploy/x.sh")]))
    assert (verdict.conflict, verdict.rule) == (True, "protected_path")
    assert verdict.path == "deploy/x.sh"


def test_structural_pem_markers_hit() -> None:
    for marker in ("BEGIN PRIVATE KEY", "BEGIN ENCRYPTED PRIVATE KEY"):
        verdict = patch_policy.evaluate_patch_policy(
            _facts(
                [_file("src/app/keys.py", content=f"-----{marker}-----\n")]
            )
        )
        assert (verdict.conflict, verdict.rule) == (True, "leak_structural"), marker


def test_structural_key_with_delimiter_tail_hits() -> None:
    """``sk-proj-`` tails carry separators; the specific prefix entry covers
    them (review blocker 4)."""
    tail = "AA" + "a1_-" * 6
    verdict = patch_policy.evaluate_patch_policy(
        _facts([_file("src/app/client.py", content=f"sk-proj-{tail}\n")])
    )
    assert (verdict.conflict, verdict.rule) == (True, "leak_structural")


def test_structural_generic_key_shape_hits() -> None:
    verdict = patch_policy.evaluate_patch_policy(
        _facts([_file("src/app/client.py", content="gsk_" + "a1b2c3d4e5f6g7h8i9j0" + "\n")])
    )
    assert (verdict.conflict, verdict.rule) == (True, "leak_structural")


def test_alphanumeric_left_boundary_does_not_hit() -> None:
    """Only the left character varies between the two payloads — a delimiter
    left boundary arms the shape, an alphanumeric one makes it an ordinary
    word. The tail satisfies every tail gate in both cases, so the test
    proves the boundary check itself, not a shared length or digit
    assumption (review should-fix 3)."""
    tail = "a1b2c3d4e5f6a7b8c9d0"
    assert len(tail) >= 16 and any(c.isdigit() for c in tail)
    hit = patch_policy.evaluate_patch_policy(
        _facts([_file("src/app/a.py", content=f"KEY=sk-{tail}\n")])
    )
    miss = patch_policy.evaluate_patch_policy(
        _facts([_file("src/app/a.py", content=f"KEYXsk-{tail}\n")])
    )
    assert (hit.conflict, hit.rule) == (True, "leak_structural")
    assert miss.conflict is False


def test_prefixed_leak_still_hits() -> None:
    """``leaked_sk-…`` — an ``_``/``-`` left of the prefix is a value
    delimiter, not a word joiner: the shape stays armed (review blocker 1).
    The fail direction is deny even though ``task-`` style compounds are
    swept up as accepted false positives."""
    verdict = patch_policy.evaluate_patch_policy(
        _facts([_file("src/app/a.py", content="leaked_sk-" + "a1b2c3d4e5f6a7b8c9d0\n")])
    )
    assert (verdict.conflict, verdict.rule) == (True, "leak_structural")


def test_prose_like_tail_is_an_accepted_false_positive() -> None:
    """A long all-letter hyphenated tail hits under the high-recall grammar.
    This is deliberate: no unfrozen probability assumption (\"real keys have
    digits\") may open a deny-side hole; a prose look-alike blocks and routes
    to a human."""
    verdict = patch_policy.evaluate_patch_policy(
        _facts(
            [
                _file(
                    "src/app/plan.py",
                    content="sk-this-is-a-very-long-english-sentence\n",
                )
            ]
        )
    )
    assert (verdict.conflict, verdict.rule) == (True, "leak_structural")


def test_delimiter_left_boundary_still_hits() -> None:
    """``=sk-…`` — the left boundary is a delimiter, the right side a full
    digit-bearing key tail: a hit."""
    verdict = patch_policy.evaluate_patch_policy(
        _facts([_file("src/app/client.py", content="KEY=sk-a1b2c3d4e5f6a7b8c9d0\n")])
    )
    assert (verdict.conflict, verdict.rule) == (True, "leak_structural")


# ---------------------------------------------------------------------------
# Protected paths: trees at component boundary; wide matches deliberate.
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
def test_protected_path_hits_inside_allowlist(path: str) -> None:
    """Every sample also sits inside an allowed-path entry, so a hit proves
    the protected rule, not the out-of-bounds rule (review nit). ``deploy``/
    ``config`` trees and the ``.env``/``.github`` names are legal in a plan
    (plan may express, policy denies). One caveat, spelled out rather than
    glossed: the ``.git/hooks/pre-commit`` sample cannot be *fully* isolated
    this way — a ``.git`` patch path is normalized and would also be
    out-of-bounds unless the plan allowed it, and allowing the exact ``.git``
    component is itself a malformed fact. So for that one sample the
    protected-stage attribution is asserted by a dedicated probe below
    (``test_exact_git_allowed_path_is_rejected``), which pins the
    shape-layer rejection; the sample here rides along for coverage."""
    facts = _facts([_file(path)])
    facts["allowed_paths"].append({"path": "deploy", "path_type": "directory"})
    facts["allowed_paths"].append({"path": "config", "path_type": "directory"})
    facts["allowed_paths"].append({"path": "docs", "path_type": "directory"})
    facts["allowed_paths"].append({"path": ".github/workflows/ci.yml", "path_type": "file"})
    facts["allowed_paths"].append({"path": ".env", "path_type": "file"})
    facts["allowed_paths"].append({"path": ".env.local", "path_type": "file"})
    verdict = patch_policy.evaluate_patch_policy(facts)
    assert (verdict.conflict, verdict.rule) == (True, "protected_path"), path


def test_exact_git_allowed_path_is_rejected() -> None:
    """The frozen plan schema forbids only the exact ``.git`` component in
    allowed_paths — that, and only that, is a malformed fact; the patch-path
    samples above are normalized paths that merely trip two rules at once."""
    with pytest.raises(DalError) as raised:
        patch_policy.evaluate_patch_policy(
            _facts([_file("src/app/x.py")]) | {
                "allowed_paths": [{"path": ".git/hooks", "path_type": "directory"}]
            }
        )
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


@pytest.mark.parametrize(
    "path",
    [".github/workflows/deploy.yml", ".gitignore", ".github/dependabot.yml"],
)
def test_git_star_names_are_expressible_in_plan(path: str) -> None:
    """``.github``/``.gitignore`` are legal allowed-path entries under the
    frozen plan schema (only the exact ``.git`` component is forbidden) and
    are then rejected by the protected stage — never re-classified as
    malformed (review should-fix 2 / nit 4)."""
    facts = _facts([_file(path)])
    facts["allowed_paths"].append({"path": path, "path_type": "file"})
    verdict = patch_policy.evaluate_patch_policy(facts)
    assert (verdict.conflict, verdict.rule) == (True, "protected_path")
    assert verdict.path == path


@pytest.mark.parametrize(
    "path",
    ["configuration.py", "configx/notes.md", "deprecation.md", "src/app/environment.py"],
)
def test_prefix_adjacent_names_are_not_protected(path: str) -> None:
    """String-prefix neighbours of protected trees are ordinary files
    (review should-fix 6)."""
    facts = _facts([_file(path)])
    # Make them in-bounds so the only possible hit would be protected_path.
    facts["allowed_paths"].append({"path": "src/app", "path_type": "directory"})
    facts["allowed_paths"].append({"path": path, "path_type": "file"})
    verdict = patch_policy.evaluate_patch_policy(facts)
    assert verdict.conflict is False


# ---------------------------------------------------------------------------
# Out of bounds.
# ---------------------------------------------------------------------------


def test_absolute_path_is_rejected_as_malformed() -> None:
    """A path outside the repo namespace is a facts-shape error, not a rule
    verdict — Git never produces it (review should-fix 7)."""
    with pytest.raises(DalError) as raised:
        patch_policy.evaluate_patch_policy(_facts([_file("/etc/passwd")]))
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


def test_dotdot_path_is_rejected_as_malformed() -> None:
    with pytest.raises(DalError) as raised:
        patch_policy.evaluate_patch_policy(_facts([_file("src/app/../../escape.py")]))
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


def test_dot_segment_path_is_rejected_as_malformed() -> None:
    with pytest.raises(DalError) as raised:
        patch_policy.evaluate_patch_policy(_facts([_file("src/app/./x.py")]))
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


def test_empty_segment_path_is_rejected_as_malformed() -> None:
    with pytest.raises(DalError) as raised:
        patch_policy.evaluate_patch_policy(_facts([_file("src/app//x.py")]))
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


# ---------------------------------------------------------------------------
# Binary and size (delete entries are exempt from binary/size/aggregate).
# ---------------------------------------------------------------------------


def test_binary_file_hits() -> None:
    verdict = patch_policy.evaluate_patch_policy(
        _facts([_file("src/app/blob.bin", is_binary=True)])
    )
    assert (verdict.conflict, verdict.rule) == (True, "binary")


def test_oversized_file_hits() -> None:
    big = "x" * 600_000
    verdict = patch_policy.evaluate_patch_policy(
        _facts([_file("src/app/big.txt", content=big)])
    )
    assert (verdict.conflict, verdict.rule) == (True, "size")


def test_size_content_mismatch_is_rejected_as_malformed() -> None:
    """A declared size below the actual content length is a malformed fact
    (review blocker 5): the controller derives size from Git, and a provider
    cannot shrink a file by declaration."""
    with pytest.raises(DalError) as raised:
        patch_policy.evaluate_patch_policy(
            _facts([_file("src/app/x.py", size_bytes=4, content="abcdefg\n")])
        )
    assert raised.value.code is DalErrorCode.INVALID_ARGUMENT


def test_each_file_under_limit_but_aggregate_over_hits() -> None:
    """3 × 400 KB passes the per-file gate and must hit the aggregate gate
    (review blocker 5)."""
    big = "x" * 400_000
    verdict = patch_policy.evaluate_patch_policy(
        _facts(
            [
                _file("src/app/a.txt", content=big),
                _file("src/app/b.txt", content=big),
                _file("src/app/c.txt", content=big),
            ]
        )
    )
    assert (verdict.conflict, verdict.rule) == (True, "size_aggregate")


def test_too_many_files_hits_file_count() -> None:
    files = [_file(f"src/app/f{i:03}.py") for i in range(patch_policy.MAX_PATCH_FILE_COUNT + 1)]
    verdict = patch_policy.evaluate_patch_policy(_facts(files))
    assert (verdict.conflict, verdict.rule) == (True, "file_count")
    assert verdict.file_index is None  # patch-level violation


def test_delete_entry_is_exempt_from_binary_size_and_aggregate() -> None:
    facts = _facts(
        [_file("src/app/old.bin", change_type="delete", size_bytes=10**9)]
    )
    verdict = patch_policy.evaluate_patch_policy(facts)
    assert verdict.conflict is False


def test_pipeline_reports_earliest_stage_across_files() -> None:
    """The first stage in RULE_ORDER with any hit wins, regardless of file
    order in the patch."""
    facts = _facts(
        [
            _file("src/app/big.txt", content="x" * 600_000),
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
    ("wrong target state", {**_facts([]), "target": {**TARGET, "state": "intake"}}),
    (
        "wrong entity type",
        {**_facts([]), "target": {**TARGET, "entity_type": "recovery_case"}},
    ),
    ("empty allowed paths", {**_facts([]), "allowed_paths": []}),
    (
        "legacy allowed path string",
        {**_facts([]), "allowed_paths": ["src/app"]},
    ),
    (
        "unknown path_type",
        {**_facts([]), "allowed_paths": [{"path": "src", "path_type": "glob"}]},
    ),
    (
        "absolute allowed path",
        {**_facts([]), "allowed_paths": [{"path": "/abs", "path_type": "file"}]},
    ),
    (
        "dotdot allowed path",
        {**_facts([]), "allowed_paths": [{"path": "a/../b", "path_type": "file"}]},
    ),
    (
        "exact git component allowed path",
        {**_facts([]), "allowed_paths": [{"path": ".git/x", "path_type": "file"}]},
    ),
    ("empty fingerprints", {**_facts([]), "secret_fingerprints": []}),
    ("empty file list", {**_facts([]), "files": []}),
    (
        "unknown file field",
        {**_facts([]), "files": [{**_file("src/app/x.py"), "mode": "0755"}]},
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
    ("negative size", {**_facts([]), "files": [_file("src/app/x.py", size_bytes=-1)]}),
    (
        "boolean size",
        {**_facts([]), "files": [_file("src/app/x.py", size_bytes=True)]},
    ),
    (
        "delete with content",
        {
            **_facts([]),
            "files": [
                {
                    "path": "src/app/x.py",
                    "change_type": "delete",
                    "size_bytes": 5,
                    "is_binary": False,
                    "content": "data\n",  # constructed raw: the _file helper
                }  # would blank it for deletes
            ],
        },
    ),
    (
        "duplicate file path",
        {**_facts([]), "files": [_file("src/app/x.py"), _file("src/app/x.py")]},
    ),
    ("empty path", {**_facts([]), "files": [_file("")]}),
    (
        "binary entry with content",
        {
            **_facts([]),
            "files": [
                {
                    "path": "src/app/x.bin",
                    "change_type": "add",
                    "size_bytes": 4,
                    "is_binary": True,
                    "content": "text",  # constructed raw: binary carries no
                }  # inspectable content
            ],
        },
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
    """The deny verdict carries rule + safe locator only — never the matched
    fingerprint, key material or patch body."""
    canary = "canary-3f9a2b7c"
    verdict = patch_policy.evaluate_patch_policy(
        _facts([_file("src/app/leak.py", content=f"TOKEN={canary}\n")])
    )
    assert verdict.conflict is True
    data = " ".join(
        part for part in (verdict.rule, verdict.file_index and str(verdict.file_index)) if part
    )
    assert canary not in data
    assert "TOKEN" not in data


def test_block_reason_matches_frozen_policy_block() -> None:
    assert patch_policy.BLOCK_REASON == "POLICY_FAILURE"


def test_rule_order_keeps_leak_stages_first() -> None:
    assert patch_policy.RULE_ORDER[:2] == ("leak_fingerprint", "leak_structural")


def test_provisional_size_constants_are_named() -> None:
    """The size policy numbers are provisional pending Henson's freeze; they
    must stay named constants so the freeze is a one-line change."""
    assert patch_policy.MAX_PATCH_FILE_SIZE_BYTES > 0
    assert patch_policy.MAX_PATCH_TOTAL_SIZE_BYTES >= patch_policy.MAX_PATCH_FILE_SIZE_BYTES
    assert patch_policy.MAX_PATCH_FILE_COUNT > 0


def test_pure_module_dependency_surface_is_closed() -> None:
    """The scanner cannot acquire an unguarded I/O dependency."""
    expected_imports = {
        "__future__",
        "dataclasses",
        "typing",
        "personal_agent_dal.errors",
    }
    tree = ast.parse(Path(patch_policy.__file__).read_text(encoding="utf-8"))
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
