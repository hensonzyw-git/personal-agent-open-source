"""Tests for the PRD/technical-design gate check.

The interesting cases are the ones that must be *refused*. A checker that only
proves it can pass is the same "green suite proves nothing" shape that
CLAUDE.md §5.1 warns about, so the refusals are enumerated first.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_gate.py"
_spec = importlib.util.spec_from_file_location("check_gate", _SCRIPT)
assert _spec and _spec.loader
check_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_gate)


def _record(branch="feat/x", prd="2026-09-10", design="2026-09-10"):
    return Path("docs/gates/x.md"), {
        "branch": branch,
        "prd_approved": prd,
        "design_approved": design,
    }


# --- refusal cases -------------------------------------------------------


def test_product_code_with_no_gate_record_is_refused():
    reasons = check_gate.judge("feat/x", ["src/personal_agent/api/app.py"], [])
    assert reasons
    assert "no gate record" in reasons[0]


def test_docs_only_change_needs_no_record():
    assert check_gate.judge("feat/x", ["docs/a.md", "README.md"], []) == []


def test_pending_design_approval_is_refused():
    reasons = check_gate.judge("feat/x", ["ios/PersonalAgent/ChatView.swift"], [_record(design="pending")])
    assert reasons
    assert any("design_approved" in reason for reason in reasons)


def test_pending_prd_approval_is_refused():
    reasons = check_gate.judge("feat/x", ["src/a.py"], [_record(prd="待填")])
    assert reasons
    assert any("prd_approved" in reason for reason in reasons)


def test_record_for_a_different_branch_does_not_satisfy_the_gate():
    reasons = check_gate.judge("feat/other", ["src/a.py"], [_record(branch="feat/x")])
    assert reasons
    assert "no gate record" in reasons[0]


def test_two_records_claiming_one_branch_are_refused():
    records = [_record(), (Path("docs/gates/y.md"), dict(_record()[1]))]
    reasons = check_gate.judge("feat/x", ["src/a.py"], records)
    assert reasons
    assert "More than one" in reasons[0]


@pytest.mark.parametrize("value", ["", "PENDING", "TBD", "2026-13-99", "yesterday"])
def test_non_date_approvals_are_refused(value):
    reasons = check_gate.judge("feat/x", ["src/a.py"], [_record(design=value)])
    assert reasons


# --- acceptance cases ----------------------------------------------------


def test_filled_record_accepts_product_code():
    assert check_gate.judge("feat/x", ["src/a.py", "ios/PersonalAgent/App.swift"], [_record()]) == []


@pytest.mark.parametrize(
    "path",
    [
        "ios/PersonalAgentKit/Tests/PersonalAgentKitTests/ChatTimelineTests.swift",
        "src/risk_monitor/tests/test_x.py",
    ],
)
def test_test_paths_are_not_product_code(path):
    assert not check_gate.is_product_code(path)


@pytest.mark.parametrize(
    "path",
    [
        "docs/多模态输入PRD_v0.1.md",
        "scripts/check_gate.py",
        ".github/workflows/gate-check.yml",
        "PROJECT_STATUS.md",
        "tests/unit/test_check_gate.py",
    ],
)
def test_process_and_doc_paths_are_not_product_code(path):
    assert not check_gate.is_product_code(path)


def test_the_p0_commit_shape_would_have_been_refused():
    """The actual 2026-09-10 miss, as a regression case.

    `bc3194ae` changed Package.swift, the Xcode project and Info.plist before
    any PRD existed. Had this gate existed, it would have refused.
    """
    files = [
        "ios/PersonalAgentKit/Package.swift",
        "ios/PersonalAgent.xcodeproj/project.pbxproj",
        "ios/PersonalAgent-Info.plist",
    ]
    assert check_gate.judge("feat/multimodal-input", files, []) != []


# --- frontmatter ---------------------------------------------------------


def test_frontmatter_parses_scalars_and_ignores_body():
    text = "---\nbranch: feat/x\nprd_approved: 2026-09-10\n---\n\n# body\nbranch: nope\n"
    assert check_gate.parse_frontmatter(text) == {
        "branch": "feat/x",
        "prd_approved": "2026-09-10",
    }


def test_text_without_frontmatter_yields_nothing():
    assert check_gate.parse_frontmatter("# just a doc\n") == {}
