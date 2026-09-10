#!/usr/bin/env python3
"""Refuse a product-code PR whose branch has no filled gate record.

CLAUDE.md §5 requires a round-scoped PRD and technical design, approved by
Henson, before implementation begins. Until 2026-09-10 that rule was prose
only, and prose lost: a round was implemented and committed before its PRD
existed, partly because the already-frozen higher-level CAP-003/CAP-006
contracts made it look as though the gate had been passed. The rule now says
the gate is per round and that no artifact above it counts; this script is the
mechanical half of it.

What it can do is deliberately narrow. It cannot judge whether a design is any
good, or whether the recorded scope really covers the diff. It can refuse a
branch that has no gate record at all, or one whose approvals are still
placeholders -- which is the exact shape the 2026-09-10 miss took. The judgment
half stays with Henson, at review.

Product code means `src/**` or `ios/**`, excluding test directories. Everything
else -- docs, tests, scripts, CI -- is exempt under §5's "purely documentary or
test-only" clause, which is why this script does not gate its own change.

Exit codes: 0 pass, 1 refused, 2 usage error.
"""

from __future__ import annotations

import argparse
import datetime
import subprocess
import sys
from pathlib import Path

GATE_DIR = Path("docs/gates")
PLACEHOLDERS = {"", "pending", "todo", "tbd", "none", "n/a", "待填", "未批准"}
PRODUCT_PREFIXES = ("src/", "ios/")
TEST_MARKERS = ("/Tests/", "/tests/", "/test/")


class GateError(Exception):
    """A refusal that should be reported to the user, not a crash."""


def changed_files(base: str, head: str, root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{head}"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GateError(
            f"`git diff {base}...{head}` failed, so the diff cannot be judged:\n"
            f"{result.stderr.strip()}"
        )
    return [line for line in result.stdout.splitlines() if line.strip()]


def is_product_code(path: str) -> bool:
    if not path.startswith(PRODUCT_PREFIXES):
        return False
    return not any(marker in path for marker in TEST_MARKERS)


def parse_frontmatter(text: str) -> dict[str, str]:
    """Read the leading `---` block as flat `key: value` pairs.

    Deliberately not YAML: the record format is a handful of scalar fields, and
    a stdlib-only parse keeps this script runnable in CI without uv.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def gate_records(root: Path) -> list[tuple[Path, dict[str, str]]]:
    directory = root / GATE_DIR
    if not directory.is_dir():
        return []
    records = []
    for path in sorted(directory.glob("*.md")):
        if path.name == "README.md":
            continue
        records.append((path, parse_frontmatter(path.read_text(encoding="utf-8"))))
    return records


def _is_approved(value: str) -> bool:
    """True only for a real ISO date.

    A shape-only `\\d{4}-\\d{2}-\\d{2}` regex accepts `2026-13-99`, which is the
    wrong kind of pass for a gate whose whole job is to distinguish "filled in"
    from "looks filled in". Parsing rejects the impossible values too.
    """
    stripped = value.strip()
    if stripped.lower() in PLACEHOLDERS:
        return False
    try:
        datetime.date.fromisoformat(stripped)
    except ValueError:
        return False
    return True


def judge(head: str, files: list[str], records: list[tuple[Path, dict[str, str]]]) -> list[str]:
    """Return the refusal reasons; empty means the gate is satisfied."""
    product = sorted(path for path in files if is_product_code(path))
    if not product:
        return []

    matching = [(path, data) for path, data in records if data.get("branch") == head]
    if not matching:
        seen = ", ".join(str(path) for path, _ in records) or "none"
        return [
            f"`{head}` changes product code but has no gate record in `{GATE_DIR}/`.",
            f"  Gate records present: {seen}",
            f"  Product-code files in this PR: {len(product)} "
            f"(e.g. {', '.join(product[:3])})",
            "  A frozen higher-level PRD or technical design does NOT count "
            "(CLAUDE.md §5).",
            "  Add a record for this round whose `branch:` matches, and fill "
            "`prd_approved` and `design_approved` with real dates.",
        ]

    if len(matching) > 1:
        names = ", ".join(str(path) for path, _ in matching)
        return [f"More than one gate record claims branch `{head}`: {names}"]

    path, data = matching[0]
    reasons = []
    if not _is_approved(data.get("prd_approved", "")):
        reasons.append("  `prd_approved` is not a date")
    if not _is_approved(data.get("design_approved", "")):
        reasons.append("  `design_approved` is not a date")
    if not reasons:
        return []
    return [
        f"`{path}` matches branch `{head}` but is not filled in:",
        *reasons,
        "  Henson's explicit approval of each artifact is required before "
        "product code is merged (CLAUDE.md §5).",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="ref to diff against, e.g. origin/main")
    parser.add_argument("--head", required=True, help="branch under test, e.g. feat/x")
    parser.add_argument("--root", default=".", help="repository root (default: cwd)")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    try:
        files = changed_files(args.base, args.head, root)
        reasons = judge(args.head, files, gate_records(root))
    except GateError as error:
        print(f"gate-check: {error}", file=sys.stderr)
        return 2

    if reasons:
        print(f"gate-check: REFUSED — branch `{args.head}`\n", file=sys.stderr)
        print("\n".join(reasons), file=sys.stderr)
        return 1

    print(f"gate-check: PASS — branch `{args.head}` has no ungated product-code change")
    return 0


if __name__ == "__main__":
    sys.exit(main())
