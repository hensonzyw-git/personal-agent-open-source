"""Worker configuration (DAL-017/018 runnable layer).

The repo allowlist, worktree root and run policy live in a small JSON file the
launchd plist points at via `--config`. It is Henson-authored and machine-local;
the loader still validates a closed shape and fails closed on any unknown field,
so a typo cannot silently widen the allowlist.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final

CONFIG_SCHEMA: Final[str] = "dal.worker-config/1.0"

#: Known keys; anything else is an error, not ignored.
_KNOWN_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "worker_id",
        "database_path",
        "worktree_root",
        "checkpoint_root",
        "kill_switch_path",
        "lease_ttl_seconds",
        "max_attempts",
        "repos",
    }
)
_REPO_KEYS: Final[frozenset[str]] = frozenset({"local_path"})


@dataclass(frozen=True)
class RepoAllowlistEntry:
    """One allowlisted repository the worker may touch."""

    repository_id: str
    local_path: str


@dataclass(frozen=True)
class WorkerConfig:
    """The validated, closed worker configuration."""

    worker_id: str
    database_path: Path
    worktree_root: Path
    checkpoint_root: Path
    kill_switch_path: Path
    lease_ttl_seconds: int
    max_attempts: int
    repos: dict[str, RepoAllowlistEntry]


def load_worker_config(path: Path) -> WorkerConfig:
    """Load and validate the worker config; raises on any unknown field."""
    body = json.loads(path.read_text("utf-8"))
    if not isinstance(body, dict):
        raise ValueError("worker config must be a JSON object")
    unknown = set(body) - _KNOWN_KEYS
    if unknown:
        raise ValueError(f"unknown worker config keys: {sorted(unknown)!r}")
    if body.get("schema_version") != CONFIG_SCHEMA:
        raise ValueError(f"unsupported worker config schema: {body.get('schema_version')!r}")

    worker_id = body.get("worker_id")
    if not isinstance(worker_id, str) or not worker_id:
        raise ValueError("worker_id must be a non-empty string")

    database_path = _path_field(body, "database_path")
    worktree_root = _path_field(body, "worktree_root")
    checkpoint_root = _path_field(body, "checkpoint_root")
    kill_switch_path = _path_field(body, "kill_switch_path")

    lease_ttl_seconds = body.get("lease_ttl_seconds")
    if (
        not isinstance(lease_ttl_seconds, int)
        or isinstance(lease_ttl_seconds, bool)
        or lease_ttl_seconds <= 0
    ):
        raise ValueError("lease_ttl_seconds must be a positive integer")
    max_attempts = body.get("max_attempts")
    if (
        not isinstance(max_attempts, int)
        or isinstance(max_attempts, bool)
        or max_attempts < 1
    ):
        raise ValueError("max_attempts must be a positive integer")

    raw_repos = body.get("repos")
    if not isinstance(raw_repos, dict) or not raw_repos:
        raise ValueError("repos must be a non-empty object")
    repos: dict[str, RepoAllowlistEntry] = {}
    for repository_id, entry in raw_repos.items():
        if not isinstance(entry, dict):
            raise ValueError(f"repo {repository_id!r} must be an object")
        unknown_repo = set(entry) - _REPO_KEYS
        if unknown_repo:
            raise ValueError(
                f"unknown repo {repository_id!r} keys: {sorted(unknown_repo)!r}"
            )
        local_path = entry.get("local_path")
        if not isinstance(local_path, str) or not local_path:
            raise ValueError(f"repo {repository_id!r} local_path must be a non-empty string")
        repos[repository_id] = RepoAllowlistEntry(
            repository_id=repository_id, local_path=str(Path(local_path).resolve())
        )

    return WorkerConfig(
        worker_id=worker_id,
        database_path=database_path,
        worktree_root=worktree_root,
        checkpoint_root=checkpoint_root,
        kill_switch_path=kill_switch_path,
        lease_ttl_seconds=lease_ttl_seconds,
        max_attempts=max_attempts,
        repos=repos,
    )


def _path_field(body: dict, key: str) -> Path:
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    # Resolve to an absolute, canonical path so downstream git/subprocess calls
    # never reinterpret a relative path against a `-C` working directory.
    return Path(value).resolve()
