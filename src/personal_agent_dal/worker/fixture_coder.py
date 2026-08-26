"""The deterministic no-model coder for the DAL-R07A fixture slice.

R07A proves the DWS -> worker -> worktree -> verification -> checkpoint ->
receipt vertical slice *without a provider*, so the "coder" is a pure function:
it writes one repo-declared tracked file with deterministic content and reports
the path it changed. It performs no I/O beyond that write, reads no environment,
runs no model, and emits no provider stream — deliberately, because a fixture
that pretended to be a provider would smuggle provider-shaped assumptions into a
slice whose point is the deterministic plumbing around it.

The path to write comes from the pinned toolchain manifest (`fixture_coder`),
never from a job field or a model, and it is re-validated here (not only at load
time) so a caller of this module cannot bypass the safety gate.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from personal_agent_dal.worker.toolchain import FixtureCoderSpec


def _validate_path(path: str) -> None:
    """Refuse a path that is not a safe repo-relative tracked-file path."""
    if not path or path != path.strip():
        raise ValueError("fixture_coder.path must be a non-empty, trimmed path")
    parts = PurePosixPath(path).parts
    if PurePosixPath(path).is_absolute() or ".." in parts or parts[:1] == (".git",):
        raise ValueError("fixture_coder.path is not a safe repo-relative path")


def apply_fixture_change(
    worktree_path: Path, spec: FixtureCoderSpec, feature_id: str
) -> tuple[str, ...]:
    """Write the fixture's deterministic change into the worktree.

    Returns the changed paths (a single-element tuple). The write overwrites the
    declared file with `spec.template` after substituting `{feature_id}`, so the
    same feature replays to the same bytes and the same changed path.
    """
    _validate_path(spec.path)
    target = worktree_path / spec.path
    if target.is_dir():
        raise ValueError("fixture_coder.path must name a file, not a directory")
    target.parent.mkdir(parents=True, exist_ok=True)
    content = spec.template.replace("{feature_id}", feature_id)
    target.write_text(content, encoding="utf-8")
    return (spec.path,)
