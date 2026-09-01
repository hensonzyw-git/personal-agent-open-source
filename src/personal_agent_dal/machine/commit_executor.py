"""`DAL-031`: the deterministic git executor (R09-A3).

The only component that turns an issued commit capability into a candidate
commit (技术方案 §9.3). It is deliberately narrow: **git mechanics, no
policy**. Every policy judgement — allowed paths, trailer fidelity, base
and result binding — belongs to the pure consume gate
(``personal_agent_dal.machine.commit_capability``) judging the controller's
presentation of *actual* git state. This module refuses on mechanics and
reports actuals; it never decides whether a commit is allowed.

Determinism. No model, clock, inherited environment or repository execution
configuration influences the result: same base tree + same declared file
contents ⇒ same result tree SHA
(commit SHAs embed timestamps; trees do not). The helper
:func:`result_tree_for` computes the binding's ``result_sha`` the same way
git will: base tree + blobs through plumbing on a throwaway index — no ref,
no worktree mutation.

Hygiene (CLAUDE.md §5.1, the tampered-environment failure shape). Every
subprocess uses the fixed ``/usr/bin/git`` binary, an allowlisted env and
``GIT_CONFIG_NOSYSTEM=1`` / ``GIT_CONFIG_GLOBAL=/dev/null``. Every command
also disables hooks and filesystem monitors. The executor deliberately avoids
``git add``: local clean filters and attributes are executable configuration,
not candidate-commit input. It reads only ``user.name`` and ``user.email``
from local config, validates them, and supplies them explicitly to
``commit-tree``.

Every refusal is a typed fixed phrase — git stderr is never echoed into
refusals or receipts (leak discipline; the same rule the patch policy
applies to provider output).

The binding commits **tree SHAs**: ``base_sha`` is the tree the candidate
must sit on, ``result_sha`` is the tree the declared change produces. Files
are represented as regular ``100644`` blobs; symlinks, non-UTF-8 content and
any declared set that does not exactly equal the candidate tree diff refuse
before HEAD moves.
"""

from __future__ import annotations

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
_GIT_BINARY: Final[str] = "/usr/bin/git"
_SAFE_GIT_CONFIG: Final[tuple[str, ...]] = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "core.useBuiltinFSMonitor=true",
    "-c", "commit.gpgSign=false",
)


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
    """The executor's allowlisted subprocess environment.

    Do not inherit ``PATH`` (binary replacement), arbitrary loader variables,
    or any host-provided ``GIT_*`` setting. Repository-local config is still
    read for the two explicit identity values, but every git command runs with
    hooks and optional filesystem monitors disabled; the commit path itself
    never calls ``git add`` and therefore cannot run attributes filters.
    """
    return {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    }


def _git(
    repo: Path,
    *args: str,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_GIT_BINARY, "-C", str(repo), *_SAFE_GIT_CONFIG, *args],
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


def _local_identity(repo: Path) -> tuple[str, str] | None:
    """Read only the two local identity values needed for ``commit-tree``."""
    values: list[str] = []
    for key in ("user.name", "user.email"):
        result = _git(repo, "config", "--local", "--get", key)
        value = result.stdout.rstrip("\n")
        if result.returncode != 0 or not value or "\n" in value or "\r" in value:
            return None
        values.append(value)
    return values[0], values[1]


def run_candidate_commit(
    repo_path: Path,
    *,
    binding: dict,
    declared_paths: list[str],
    message: str,
) -> CommitExecutorResult:
    """Form exactly one candidate commit inside the issued binding.

    The declared paths are read as regular files and overlaid on a throwaway
    index. This intentionally does not invoke ``git add``: repository
    attributes filters and index hooks are executable configuration, not
    candidate-commit input. The fully validated commit object is created
    before a compare-and-swap ``update-ref`` advances HEAD.
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
    if type(declared_paths) is not list:
        return _refuse("declared paths must be a native list")
    for path in declared_paths:
        if type(path) is not str or not _is_normalized_declared_path(path):
            return _refuse("declared path is not a normalized repo-relative path")
        if any(component == ".git" for component in path.split("/")):
            return _refuse("declared path names a .git subtree")
    if not declared_paths:
        return _refuse("declared paths must be non-empty")
    if len(set(declared_paths)) != len(declared_paths):
        return _refuse("declared paths must not repeat a path")
    if any(
        line.partition(": ")[0] in TRAILER_ORDER
        for line in message.splitlines()
        if ": " in line
    ):
        return _refuse("commit message must not predeclare frozen trailers")

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

    existing_staged = _git(repo_path, "diff", "--cached", "--name-only")
    if existing_staged.returncode != 0:
        return _refuse("existing staged set could not be read")
    if any(line for line in existing_staged.stdout.splitlines() if line):
        return _refuse("repository has pre-existing staged changes")

    files: dict[str, str] = {}
    for path in declared_paths:
        candidate = repo_path / path
        if candidate.is_symlink() or not candidate.is_file():
            return _refuse("declared path is not a regular file")
        try:
            files[path] = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return _refuse("declared file could not be read as UTF-8")
    try:
        commit_tree = result_tree_for(repo_path, head_tree, files=files)
    except RuntimeError:
        return _refuse("candidate tree could not be built")

    changed = _git(
        repo_path, "diff-tree", "--no-commit-id", "--name-only", "-r",
        head_tree, commit_tree,
    )
    if changed.returncode != 0:
        return _refuse("candidate paths could not be read")
    committed_paths = tuple(line for line in changed.stdout.splitlines() if line)
    if tuple(sorted(committed_paths)) != tuple(sorted(declared_paths)):
        return _refuse("candidate set diverged from the declared paths")

    identity = _local_identity(repo_path)
    if identity is None:
        return _refuse("repository local author identity is unavailable")
    commit_message = message.rstrip() + "\n\n" + "\n".join(
        f"{key}: {trailers[key]}" for key in TRAILER_ORDER
    ) + "\n"
    commit_env = _git_env()
    commit_env.update(
        {
            "GIT_AUTHOR_NAME": identity[0],
            "GIT_AUTHOR_EMAIL": identity[1],
            "GIT_COMMITTER_NAME": identity[0],
            "GIT_COMMITTER_EMAIL": identity[1],
        }
    )
    committed = _git(
        repo_path, "commit-tree", commit_tree, "-p", head_commit,
        env=commit_env, input_text=commit_message,
    )
    if committed.returncode != 0:
        return _refuse("candidate commit object could not be created")
    commit_sha = committed.stdout.strip()
    if _rev(repo_path, f"{commit_sha}^{{tree}}") != commit_tree or (
        _rev(repo_path, f"{commit_sha}^") != head_commit
    ):
        return _refuse("candidate commit object failed ancestry readback")

    shown = _git(repo_path, "show", "-s", "--format=%B", commit_sha)
    if shown.returncode != 0:
        return _refuse("commit message could not be read back")
    trailers_readback = _trailers_from_message(repo_path, shown.stdout)
    if trailers_readback != {key: trailers[key] for key in TRAILER_ORDER}:
        return _refuse("trailer readback did not reproduce the binding trailers")

    updated = _git(repo_path, "update-ref", "HEAD", commit_sha, head_commit)
    if updated.returncode != 0:
        return _refuse("head moved while the candidate commit formed")
    synchronized = _git(repo_path, "read-tree", commit_tree)
    if synchronized.returncode != 0:
        raise RuntimeError("candidate ref advanced but index synchronization failed")

    return CommitExecutorResult(
        commit_sha=commit_sha,
        tree_sha=commit_tree,
        parent_commit_sha=head_commit,
        parent_tree_sha=head_tree,
        touched_paths=committed_paths,
        trailers_readback=dict(trailers_readback),
        refusal=None,
    )
