"""Checkpoint/handoff bundle (DAL-020 runnable layer).

A checkpoint is the resume point for a crashed or reclaimed job: the versioned
bundle plus the full `git diff` patch, written atomically (temp file +
`os.replace`) so a crash mid-write never leaves a half-file that a restart would
mistake for a completed checkpoint.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

from personal_agent_core.manifest import canonical_json, sha256_of

CHECKPOINT_SCHEMA: Final[str] = "dal.checkpoint-bundle/1.1"


@dataclass(frozen=True)
class CheckpointBundle:
    """The versioned handoff a worker can resume from."""

    schema_version: str
    feature_id: str
    repository_id: str
    base_sha: str
    head_sha: str
    changed_files: tuple[str, ...]
    acceptance_progress: tuple[str, ...]
    test_results: dict[str, int]
    toolchain_ref: str
    toolchain_manifest_sha256: str
    patch: str


def checkpoint_path(root: Path, feature_id: str) -> Path:
    return root / feature_id / "checkpoint.json"


def checkpoint_sha256(bundle: CheckpointBundle) -> str:
    """Return the canonical digest that result receipts bind to."""
    return sha256_of(asdict(bundle))


def write_checkpoint(root: Path, bundle: CheckpointBundle) -> Path:
    """Atomically write `bundle`; returns the path written."""
    path = checkpoint_path(root, bundle.feature_id)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    body = asdict(bundle)
    if body.get("schema_version") != CHECKPOINT_SCHEMA:
        raise ValueError(f"unexpected checkpoint schema: {body.get('schema_version')!r}")
    data = canonical_json(body).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=".checkpoint-", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if tmp.exists():
            tmp.unlink()
    return path


def load_checkpoint(root: Path, feature_id: str) -> CheckpointBundle | None:
    """Read the most recent checkpoint for a feature, or None if none exists."""
    path = checkpoint_path(root, feature_id)
    if not path.is_file():
        return None
    body = json.loads(path.read_text("utf-8"))
    if body.get("schema_version") != CHECKPOINT_SCHEMA:
        raise ValueError(f"unexpected checkpoint schema: {body.get('schema_version')!r}")
    body["changed_files"] = tuple(body.get("changed_files") or ())
    body["acceptance_progress"] = tuple(body.get("acceptance_progress") or ())
    return CheckpointBundle(**body)


def checkpoint_exists(root: Path, feature_id: str) -> bool:
    return checkpoint_path(root, feature_id).is_file()
