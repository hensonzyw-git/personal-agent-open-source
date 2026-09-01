"""`DAL-031`: the deterministic git executor (R09-A3).

The only component that turns an issued commit capability into a candidate
commit (技术方案 §9.3). It is deliberately narrow: **git mechanics, no
policy**. Every policy judgement — allowed paths, trailer fidelity, base
and result binding — belongs to the pure consume gate
(``personal_agent_dal.machine.commit_capability``) judging the controller's
presentation of *actual* git state. This module refuses on mechanics and
reports actuals; it never decides whether a commit is allowed.

Determinism. No model, no clock, no environment read influences the result:
same base tree + same declared file contents ⇒ same result tree SHA
(commit SHAs embed timestamps; trees do not). The helper
:func:`result_tree_for` computes the binding's ``result_sha`` the same way
git will: base tree + blobs through plumbing on a throwaway index — no ref,
no worktree mutation.

Hygiene (CLAUDE.md §5.1, the tampered-environment failure shape). Every
subprocess runs with an env that drops **every** ``GIT_*`` variable and
pins ``GIT_CONFIG_NOSYSTEM=1`` / ``GIT_CONFIG_GLOBAL=/dev/null`` — a
hostile ``GIT_DIR``, ``GIT_INDEX_FILE``, ``GIT_AUTHOR_*`` or
``GIT_CONFIG_*`` cannot redirect the commit. Hooks are neutralized twice:
an empty ``core.hooksPath`` *and* ``--no-verify``; GPG signing is disabled
by ``-c commit.gpgSign=false`` and ``--no-gpg-sign`` so repo config cannot
demand it. Identity comes from the repository's own local config.

Every refusal is a typed fixed phrase — git stderr is never echoed into
refusals or receipts (leak discipline; the same rule the patch policy
applies to provider output).

The binding commits **tree SHAs**: ``base_sha`` is the tree the candidate
must sit on, ``result_sha`` is the tree the declared change produces. A
declared file with the executable bit set would commit as mode ``100755``
and diverge from the helper's ``100644`` — that divergence is not judged
here; it surfaces as a result-tree mismatch in the consume gate and lands
the block. Fail closed at the gate, not repaired here.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from personal_agent_dal.machine.commit_capability import TRAILER_KEYS

#: Candidate commits embed these four trailers (技术方案 §9.3); the executor
#: hands git exactly the binding's values and verifies the commit object
#: carries them back.
TRAILER_ORDER: Final[tuple[str, ...]] = (
    "Feature-Id",
    "Task-Id",
    "Plan-Hash",
    "Review-Id",
)

_GIT_TIMEOUT_SECONDS: Final[int] = 60


@dataclass(frozen=True)
class CommitExecutorRefusal:
    """A typed no-commit outcome. ``reason`` is a fixed phrase; ``detail``
    names only our own enumerated facts, never git output."""

    reason: str
    detail: str


@dataclass(frozen=True)
class CommitExecutorResult:
    """The actuals of one executor run, read back from the commit object."""

    commit_sha: str | None
    tree_sha: str | None
    parent_commit_sha: str | None
    parent_tree_sha: str | None
    touched_paths: tuple[str, ...]
    trailers_readback: dict[str, str]
    refusal: CommitExecutorRefusal | None


def _refuse(reason: str, detail: str = "") -> CommitExecutorResult:
    return CommitExecutorResult(
        commit_sha=None,
        tree_sha=None,
        parent_commit_sha=None,
        parent_tree_sha=None,
        touched_paths=(),
        trailers_readback={},
        refusal=CommitExecutorRefusal(reason=reason, detail=detail),
    )


def _git_env() -> dict[str, str]:
    """A clean env: every ``GIT_*`` variable dropped, config sources pinned.

    A credential must only ever travel to a pinned endpoint (§5.1); the same
    pinning discipline applies to the commit: it can only land in the
    repository we point ``-C`` at, through the index we build, with no
    environment-supplied redirection.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    return env


def _git(
    repo: Path,
    *args: str,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=_GIT_TIMEOUT_SECONDS,
        env=env if env is not None else _git_env(),
        input=input_text,
    )


def _is_normalized_declared_path(value: object) -> bool:
    """Mechanics-level path check: native str, repo-relative, no traversal.

    The consume gate re-judges every touched path against the issued
    allowed set; this check only keeps git itself out of trouble (no
    absolute paths, no traversal). ``.git`` gets its own labelled refusal
    at the call site.
    """
    if type(value) is not str or not value:
        return False
    if value.startswith("/") or "\0" in value:
        return False
    components = value.split("/")
    return all(component not in ("", ".", "..") for component in components)


def result_tree_for(
    repo_path: Path,
    base_tree_sha: str,
    *,
    files: dict[str, str],
) -> str:
    """The tree SHA the declared file contents produce on top of the base tree.

    Plumbing only — a throwaway index, blob writes into the object database,
    no ref move, no worktree change. Deterministic: git trees carry no
    timestamps, so the same inputs always yield the same SHA. Modes are
    pinned to ``100644``: the executor commits regular files.
    """
    env = _git_env()
    with tempfile.TemporaryDirectory(prefix="dal-result-tree-") as scratch:
        env = dict(env)
        env["GIT_INDEX_FILE"] = str(Path(scratch) / "index")
        read = _git(repo_path, "read-tree", base_tree_sha, env=env)
        if read.returncode != 0:
            raise RuntimeError("result tree: base tree could not be read")
        for path in sorted(files):
            blob = _git(repo_path, "hash-object", "-w", "--stdin",
                        env=env, input_text=files[path])
            if blob.returncode != 0:
                raise RuntimeError("result tree: blob could not be written")
            update = _git(
                repo_path,
                "update-index",
                "--add",
                "--cacheinfo",
                f"100644,{blob.stdout.strip()},{path}",
                env=env,
            )
            if update.returncode != 0:
                raise RuntimeError("result tree: index entry could not be written")
        write = _git(repo_path, "write-tree", env=env)
        if write.returncode != 0:
            raise RuntimeError("result tree: tree could not be written")
        return write.stdout.strip()


def _rev(repo: Path, spec: str) -> str | None:
    result = _git(repo, "rev-parse", "-q", "--verify", spec)
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _trailers_from_message(repo: Path, message: str) -> dict[str, str] | None:
    """The commit's trailers, parsed by git from the real message bytes."""
    parsed = _git(repo, "interpret-trailers", "--parse", input_text=message)
    if parsed.returncode != 0:
        return None
    trailers: dict[str, str] = {}
    for line in parsed.stdout.splitlines():
        line = line.rstrip("\n")
        if not line:
            continue
        key, separator, value = line.partition(": ")
        if not separator or key in trailers:
            return None
        trailers[key] = value
    return trailers


def run_candidate_commit(
    repo_path: Path,
    *,
    binding: dict,
    declared_paths: list[str],
    message: str,
) -> CommitExecutorResult:
    """Form exactly one candidate commit inside the issued binding.

    The declared paths (whose new contents are already in the worktree) are
    staged alone — unrelated worktree noise is never committed — and the
    commit carries the binding's four trailers. Everything the caller
    receives about the commit is read back from the commit object, not
    from the arguments.

    Refusals leave the repository's refs exactly as they were.
    """
    if not repo_path.is_dir() or repo_path.is_symlink():
        return _refuse("repository path is not a real directory")

    if type(message) is not str or not message.strip():
        return _refuse("commit message must be a non-empty string")

    trailers = binding.get("trailers")
    if (
        type(trailers) is not dict
        or frozenset(trailers) != set(TRAILER_KEYS)
        or any(type(value) is not str or not value for value in trailers.values())
    ):
        return _refuse("binding trailers must carry the four frozen values")
    if any(
        "\n" in value or "\r" in value
        for value in trailers.values()
    ):
        return _refuse("trailer values cannot carry line breaks")
    for path in declared_paths:
        if type(path) is not str or not _is_normalized_declared_path(path):
            return _refuse("declared path is not a normalized repo-relative path")
        if any(component == ".git" for component in path.split("/")):
            return _refuse("declared path names a .git subtree")
    if not declared_paths:
        return _refuse("declared paths must be non-empty")

    head_commit = _rev(repo_path, "HEAD")
    if head_commit is None:
        return _refuse("head commit could not be resolved")
    head_tree = _rev(repo_path, "HEAD^{tree}")
    if head_tree is None:
        return _refuse("head tree could not be resolved")

    base_sha = binding.get("base_sha")
    if head_tree != base_sha:
        return _refuse(
            "base tree drift: head tree no longer matches the issued base"
        )

    staged = _git(repo_path, "add", "--", *sorted(declared_paths))
    if staged.returncode != 0:
        return _refuse("declared path could not be staged")

    staged_names = _git(repo_path, "diff", "--cached", "--name-only")
    if staged_names.returncode != 0:
        return _refuse("staged set could not be read")
    touched = tuple(
        line for line in staged_names.stdout.splitlines() if line
    )
    if not touched:
        return _refuse("nothing to commit for the declared paths")
    if any(path not in set(declared_paths) for path in touched):
        return _refuse("staged set diverged from the declared paths")

    with tempfile.TemporaryDirectory(prefix="dal-hooks-") as hooks_dir:
        committed = _git(
            repo_path,
            "-c",
            f"core.hooksPath={hooks_dir}",
            "-c",
            "commit.gpgSign=false",
            "commit",
            "--no-verify",
            "--no-gpg-sign",
            "-q",
            "-m",
            message,
            *[
                f"--trailer={key}={trailers[key]}"
                for key in TRAILER_ORDER
            ],
        )
    if committed.returncode != 0:
        return _refuse("git commit failed")

    commit_sha = _rev(repo_path, "HEAD")
    if commit_sha is None:
        return _refuse("new head could not be resolved after commit")
    commit_tree = _rev(repo_path, f"{commit_sha}^{{tree}}")
    parent_commit = _rev(repo_path, f"{commit_sha}^")
    if commit_tree is None or parent_commit is None:
        return _refuse("commit ancestry could not be read after commit")
    if parent_commit != head_commit:
        return _refuse("head moved while the candidate commit formed")
    parent_tree = _rev(repo_path, f"{parent_commit}^{{tree}}")
    if parent_tree is None:
        return _refuse("parent tree could not be read after commit")

    shown = _git(repo_path, "show", "-s", "--format=%B", commit_sha)
    if shown.returncode != 0:
        return _refuse("commit message could not be read back")
    trailers_readback = _trailers_from_message(repo_path, shown.stdout)
    if trailers_readback is None or any(
        key not in trailers_readback or trailers_readback[key] != trailers[key]
        for key in TRAILER_ORDER
    ):
        return _refuse("trailer readback did not reproduce the binding trailers")

    committed_names = _git(
        repo_path, "diff-tree", "--no-commit-id", "--name-only", "-r", commit_sha
    )
    if committed_names.returncode != 0:
        return _refuse("committed paths could not be read back")
    committed_paths = tuple(
        line for line in committed_names.stdout.splitlines() if line
    )
    if not committed_paths:
        return _refuse("committed set could not be read back")

    return CommitExecutorResult(
        commit_sha=commit_sha,
        tree_sha=commit_tree,
        parent_commit_sha=parent_commit,
        parent_tree_sha=parent_tree,
        touched_paths=committed_paths,
        trailers_readback=dict(trailers_readback),
        refusal=None,
    )
