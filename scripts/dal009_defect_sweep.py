"""DAL-009 defect-injection sweep.

Each entry below disables exactly one safety property of the state machine,
runs the DAL replay suite in a subprocess against the mutated source, and
requires that *some test goes red*. A property no test can see is a property
the suite does not actually hold -- this is the adversarial half of DAL-009,
the mirror of DAL-008's sweep in
`docs/evidence/DAL008_数据库合同实现_2026-08-10.md` §4.

Usage:
    .venv/bin/python scripts/dal009_defect_sweep.py            # all injections
    .venv/bin/python scripts/dal009_defect_sweep.py NAME ...   # a subset

Every injection runs against a pristine copy of the file restored afterwards;
a `finally` guarantees the restore even when a run is interrupted. The engine
is never mutated in place on the real tree.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "src/personal_agent_dal/machine/engine.py"
OPERATIONS = ROOT / "src/personal_agent_dal/storage/operations.py"
SUITE = "tests/dal/test_state_machine.py"


@dataclass(frozen=True)
class Injection:
    """One property switched off, and why a green suite would be a lie."""

    name: str
    file: Path
    old: str
    new: str
    rationale: str

    def render(self) -> str:
        return f"{self.name}: {self.rationale}"


INJECTIONS: tuple[Injection, ...] = (
    Injection(
        name="cas_without_version_guard",
        file=ENGINE,
        old=(
            "        .where(pk == ctx.aggregate_id)\n"
            "        .where(table.c.version == ctx.command.expected_version)"
        ),
        new=(
            "        .where(pk == ctx.aggregate_id)"
        ),
        rationale=(
            "remove the compare-and-swap version guard: a stale writer "
            "overwrites a newer row and no test notices"
        ),
    ),
    Injection(
        name="terminal_guard_removed",
        file=ENGINE,
        old=(
            "            if from_state in TERMINAL_STATES.get(command.aggregate_type, frozenset()):\n"
            "                raise TransitionRefused(\n"
            "                    ReceiptCodes.TERMINAL_STATE,\n"
            "                    f\"{command.aggregate_type} is {from_state}\",\n"
            "                )"
        ),
        new="",
        rationale=(
            "remove the terminal-state guard: cancelled/completed features "
            "accept new transitions"
        ),
    ),
    Injection(
        name="replay_returns_current_state",
        file=ENGINE,
        old=(
            "                raise _Replay(\n"
            "                    from_state=replayed[1],\n"
            "                    to_state=replayed[2],\n"
            "                    spec_id=replayed[3],\n"
            "                    receipt_schema=replayed[4],\n"
            "                )"
        ),
        new=(
            "                raise _Replay(\n"
            "                    from_state=from_state,\n"
            "                    to_state=from_state,\n"
            "                    spec_id=replayed[3],\n"
            "                    receipt_schema=replayed[4],\n"
            "                )"
        ),
        rationale=(
            "a replay answers with the aggregate's *current* state instead of "
            "the original receipt's -- §2.6's lie that erases what the command did"
        ),
    ),
    # `actor_allowlist_widened` is deliberately absent. Removing the first
    # actor check is undetectable *by construction*, not by a coverage gap:
    # across all 276 frozen specs `allowed_actor_types` is exactly the set of
    # actor types the evidence bindings name, so any command the removed check
    # would refuse is refused identically by the binding layer below it. The
    # layering test `test_actor_allowlist_is_checked_before_the_binding` pins
    # the only direction that would make the first check load-bearing
    # (a binding admitting an actor the allowlist omits), which is the defect
    # worth detecting; the symmetric removal is provably a no-op and asserting
    # redness for it would be theatre.
    Injection(
        name="guard_bypassed",
        file=ENGINE,
        old=(
            "def _check_guard(spec: dict[str, Any], facts: GuardFacts) -> None:\n"
            "    guard_id = spec[\"guard_id\"]\n"
            "    if guard_id is None:\n"
            "        return"
        ),
        new=(
            "def _check_guard(spec: dict[str, Any], facts: GuardFacts) -> None:\n"
            "    return"
        ),
        rationale=(
            "make every guard a no-op: guarded transitions apply without "
            "their facts, and the guard-deny variants must catch it"
        ),
    ),
    Injection(
        name="receipt_from_state_is_post_state",
        file=ENGINE,
        old=(
            "            from_state=ctx.scratch.get(\"effect_from_state\", effect.state),\n"
            "            to_state=effect.state,"
        ),
        new=(
            "            from_state=effect.state,\n"
            "            to_state=effect.state,"
        ),
        rationale=(
            "the external-effect receipt records the post-transition state as "
            "its from_state -- the persisted-content assertions must catch the lie"
        ),
    ),
    Injection(
        name="companion_event_uses_root_id",
        file=ENGINE,
        old=(
            "        if companion[\"aggregate_type\"] == \"external_effect\":\n"
            "            companion_aggregate_id = ctx.scratch.get(\"effect_id\")"
        ),
        new=(
            "        if companion[\"aggregate_type\"] == \"external_effect\":\n"
            "            companion_aggregate_id = ctx.aggregate_id"
        ),
        rationale=(
            "a companion event names the root aggregate instead of its own -- "
            "the persisted event rows must disagree with the receipts"
        ),
    ),
    Injection(
        name="effect_inventory_root_only",
        file=ENGINE,
        old=(
            "    owner_ids: list[str] = [feature_id]\n"
            "    if feature_id != ctx.aggregate_id:\n"
            "        owner_ids.append(ctx.aggregate_id)\n"
            "    case_table = RecoveryCase.__table__\n"
            "    owner_ids.extend(\n"
            "        ctx.session.execute(\n"
            "            select(case_table.c.recovery_case_id).where(\n"
            "                case_table.c.feature_id == feature_id\n"
            "            )\n"
            "        ).scalars()\n"
            "    )"
        ),
        new="    owner_ids: list[str] = [feature_id]",
        rationale=(
            "the inventory query drops recovery-case-owned effects: a "
            "cancel/block decides on an incomplete picture of the feature's "
            "external facts"
        ),
    ),
    Injection(
        name="business_event_not_persisted",
        file=ENGINE,
        old=(
            "def _w_business_event(ctx: ApplyContext) -> None:\n"
            "    payload = {\"spec_id\": ctx.spec[\"spec_id\"], \"to_state\": ctx.to_state}"
        ),
        new=(
            "def _w_business_event(ctx: ApplyContext) -> None:\n"
            "    return\n"
            "    payload = {\"spec_id\": ctx.spec[\"spec_id\"], \"to_state\": ctx.to_state}"
        ),
        rationale=(
            "the engine claims a business_event write it never performs -- the "
            "observed-vs-declared check must catch the false claim"
        ),
    ),
    Injection(
        name="double_effect_mutation",
        file=ENGINE,
        old=(
            "    if (\n"
            "        ctx.spec[\"aggregate_type\"] != \"external_effect\"\n"
            "        and ctx.scratch.get(\"effect_id\") == effect.effect_id\n"
            "    ):"
        ),
        new="    if False:",
        rationale=(
            "let the outcome writer re-raise an effect the companion already "
            "closed: the receipt records one version, the row another"
        ),
    ),
)


def _run_suite() -> tuple[bool, str]:
    """Run the DAL replay suite; return (any_test_failed, tail)."""
    result = subprocess.run(
        [str(ROOT / ".venv/bin/python"), "-m", "pytest", SUITE, "-q", "-x",
         "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"},
        timeout=600,
    )
    tail = "\n".join(result.stdout.splitlines()[-4:])
    return result.returncode != 0, tail


def main() -> int:
    selected = set(sys.argv[1:])
    injections = [
        i for i in INJECTIONS if not selected or i.name in selected
    ]
    missing = selected - {i.name for i in INJECTIONS}
    if missing:
        print(f"unknown injection(s): {sorted(missing)}", file=sys.stderr)
        return 2

    work = Path(tempfile.mkdtemp(prefix="dal009-sweep-"))
    failures: list[str] = []
    try:
        for injection in injections:
            original = injection.file.read_text(encoding="utf-8")
            if injection.old not in original:
                failures.append(
                    f"{injection.name}: anchor text not found "
                    "(the code moved; the sweep is stale)"
                )
                continue
            backup = work / injection.file.name
            shutil.copy2(injection.file, backup)
            try:
                injection.file.write_text(
                    original.replace(injection.old, injection.new, 1),
                    encoding="utf-8",
                )
                caught, tail = _run_suite()
                status = "caught" if caught else "NOT CAUGHT"
                print(f"[{status:>10}] {injection.name}")
                if not caught:
                    failures.append(
                        f"{injection.name}: suite stayed green -- "
                        f"{injection.rationale}"
                    )
            finally:
                shutil.copy2(backup, injection.file)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print()
    if failures:
        print("sweep failures:")
        for line in failures:
            print(f"  - {line}")
        return 1
    print(f"all {len(injections)} injections were caught by the suite")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
