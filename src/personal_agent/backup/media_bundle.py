"""Prepare and verify immutable media backup bundles.

The API service owns the live media root and database, while the backup account
may only read ``/var/backups/personal-agent``.  This module preserves that
boundary: the producer takes the media storage fence, creates an Online Backup
API snapshot, copies only the ciphertext of ``ready``/``bound`` objects into a
new run directory, and atomically publishes one complete run.  The consumer
does no live-data I/O; it verifies the published manifest before passing that
single directory to restic.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
import fcntl
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import select

from personal_agent.backup.deletion_manifest import export_manifest
from personal_agent.media.locking import media_locks
from personal_agent.storage.engine import create_database_engine, session_factory
from personal_agent.storage.models import MediaObject
from personal_agent_core.sqlite import STAGED_SNAPSHOT_MODE, online_backup


BUNDLE_FORMAT = 1
RUNS_DIRNAME = "media-runs"
LATEST_NAME = "media-latest.json"
BUNDLE_LOCK_NAME = "media-bundle.lock"
_RUN_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


class MediaBundleError(RuntimeError):
    """A bundle cannot be prepared or verified without weakening recovery."""


@dataclass(frozen=True)
class PreparedMediaBundle:
    run_id: str
    path: Path


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    fd = _open_regular_readonly(path, label="bundle file")
    with os.fdopen(fd, "rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


def _regular(path: Path, *, label: str) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise MediaBundleError(f"{label} is missing: {path}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise MediaBundleError(f"{label} is not a regular file: {path}")


def _open_regular_readonly(path: Path, *, label: str) -> int:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise MediaBundleError(f"cannot open {label}: {path}") from exc
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise MediaBundleError(f"{label} is not a regular file: {path}")
    return fd


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy_ciphertext(source: Path, destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Copy from a descriptor opened with O_NOFOLLOW so a filesystem entry cannot
    # turn into a link between validation and read.  The destination is created
    # exclusively and is owner-only until the whole bundle is published.
    source_fd = _open_regular_readonly(source, label="live media ciphertext")
    try:
        target_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
        )
        try:
            digest = hashlib.sha256()
            total = 0
            while chunk := os.read(source_fd, 1024 * 1024):
                digest.update(chunk)
                total += len(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(target_fd, view)
                    view = view[written:]
            os.fsync(target_fd)
        finally:
            os.close(target_fd)
    finally:
        os.close(source_fd)
    os.chmod(destination, STAGED_SNAPSHOT_MODE)
    return {"sha256": digest.hexdigest(), "size": total}


def _atomic_json(path: Path, body: dict[str, Any]) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, STAGED_SNAPSHOT_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(body, output, ensure_ascii=False, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
        os.chmod(path, STAGED_SNAPSHOT_MODE)
        _fsync_directory(path.parent)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


@contextmanager
def _bundle_lock(stage_root: Path, mode: int) -> Iterator[None]:
    import fcntl

    lock = stage_root / BUNDLE_LOCK_NAME
    fd = _open_regular_readonly(lock, label="bundle lock")
    try:
        fcntl.flock(fd, mode)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def prepare_media_bundle(
    *, database: Path, media_root: Path, stage_root: Path
) -> PreparedMediaBundle:
    """Create and atomically publish one complete backup bundle.

    A published run contains only the snapshot and ciphertext referenced by
    *that* snapshot.  ``pending``/``uploading`` objects are intentionally absent:
    after recovery their deadlines/reaper path decide them; pretending their
    partial bytes were resumable would make an unprovable upload look complete.
    """
    stage_root = Path(stage_root)
    runs = stage_root / RUNS_DIRNAME
    if not runs.is_dir():
        raise MediaBundleError(f"bundle runs directory is missing: {runs}")
    run_id = str(uuid.uuid4())
    temporary = runs / f".incomplete-{run_id}"
    final = runs / run_id
    if temporary.exists() or final.exists():
        raise MediaBundleError(f"refusing to reuse bundle id {run_id}")

    with _bundle_lock(stage_root, fcntl.LOCK_EX):
        temporary.mkdir(mode=0o700)
        try:
            with media_locks(Path(media_root), exclusive_storage=True):
                snapshot = temporary / "agent.sqlite"
                online_backup(database, snapshot, mode=STAGED_SNAPSHOT_MODE)
                engine = create_database_engine(snapshot)
                try:
                    with session_factory(engine)() as session:
                        manifest = export_manifest(session)
                        rows = session.execute(
                            select(MediaObject.media_id).where(
                                MediaObject.state.in_(("ready", "bound"))
                            ).order_by(MediaObject.media_id)
                        ).scalars().all()
                finally:
                    engine.dispose()
                _atomic_json(temporary / "deletion-manifest.json", {"entries": manifest})

                media_entries: list[dict[str, Any]] = []
                for media_id in rows:
                    source = Path(media_root) / "final" / media_id[:2] / f"{media_id}.bin"
                    relative = Path("media") / f"{media_id}.bin"
                    metadata = _copy_ciphertext(source, temporary / relative)
                    media_entries.append({"path": relative.as_posix(), "media_id": media_id, **metadata})

            files = []
            for relative in (Path("agent.sqlite"), Path("deletion-manifest.json")):
                digest, size = _sha256(temporary / relative)
                files.append({"path": relative.as_posix(), "sha256": digest, "size": size})
            files.extend({key: entry[key] for key in ("path", "sha256", "size")} for entry in media_entries)
            bundle = {"format": BUNDLE_FORMAT, "run_id": run_id, "files": files, "media": media_entries}
            _atomic_json(temporary / "manifest.json", bundle)
            os.chmod(temporary, 0o750)
            os.rename(temporary, final)
            _fsync_directory(runs)
            _atomic_json(stage_root / LATEST_NAME, {"format": BUNDLE_FORMAT, "run_id": run_id})
            return PreparedMediaBundle(run_id=run_id, path=final)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise


def verify_published_media_bundle(stage_root: Path) -> PreparedMediaBundle:
    """Return the one complete published run, or refuse before restic sees it."""
    stage_root = Path(stage_root)
    with _bundle_lock(stage_root, fcntl.LOCK_SH):
        try:
            latest_path = stage_root / LATEST_NAME
            _regular(latest_path, label="published bundle pointer")
            latest = json.loads(latest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MediaBundleError("published bundle pointer is unreadable") from exc
        run_id = latest.get("run_id") if isinstance(latest, dict) else None
        if (
            not isinstance(latest, dict)
            or latest.get("format") != BUNDLE_FORMAT
            or not isinstance(run_id, str)
            or not _RUN_ID.fullmatch(run_id)
        ):
            raise MediaBundleError("published bundle pointer format or run id is invalid")
        return verify_media_bundle_run(stage_root / RUNS_DIRNAME / run_id, run_id=run_id)


def verify_media_bundle_run(run: Path, *, run_id: str | None = None) -> PreparedMediaBundle:
    """Verify one restored or published run without consulting a latest pointer."""
    run = Path(run)
    expected_id = run_id or run.name
    if not _RUN_ID.fullmatch(expected_id) or run.name != expected_id:
        raise MediaBundleError("bundle run id is invalid")
    if not run.is_dir() or run.is_symlink():
        raise MediaBundleError("bundle run is missing or unsafe")
    try:
        manifest_path = run / "manifest.json"
        _regular(manifest_path, label="bundle manifest")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MediaBundleError("bundle manifest is unreadable") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != BUNDLE_FORMAT or manifest.get("run_id") != expected_id:
        raise MediaBundleError("bundle manifest format or run id is invalid")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise MediaBundleError("bundle manifest has no files")
    seen: set[str] = set()
    file_metadata: dict[str, tuple[str, int]] = {}
    for entry in files:
        if not isinstance(entry, dict):
            raise MediaBundleError("bundle manifest has a non-object file entry")
        relative = entry.get("path")
        expected = entry.get("sha256")
        expected_size = entry.get("size")
        if not isinstance(relative, str) or not isinstance(expected, str) or not isinstance(expected_size, int):
            raise MediaBundleError("bundle manifest file entry is malformed")
        parsed = Path(relative)
        if parsed.is_absolute() or ".." in parsed.parts or relative in seen:
            raise MediaBundleError("bundle manifest path is unsafe or duplicated")
        seen.add(relative)
        file = run / parsed
        _regular(file, label="bundle file")
        actual, size = _sha256(file)
        if actual != expected or size != expected_size:
            raise MediaBundleError(f"bundle file does not match manifest: {relative}")
        file_metadata[relative] = (expected, expected_size)
    media = manifest.get("media")
    if not isinstance(media, list):
        raise MediaBundleError("bundle manifest media entries are missing")
    media_paths: set[str] = set()
    media_ids: set[str] = set()
    for entry in media:
        if not isinstance(entry, dict):
            raise MediaBundleError("bundle manifest has a non-object media entry")
        media_id, relative = entry.get("media_id"), entry.get("path")
        digest, size = entry.get("sha256"), entry.get("size")
        if (
            not isinstance(media_id, str)
            or not isinstance(relative, str)
            or not isinstance(digest, str)
            or not isinstance(size, int)
            or relative != f"media/{media_id}.bin"
            or relative not in seen
            or file_metadata[relative] != (digest, size)
            or media_id in media_ids
            or relative in media_paths
        ):
            raise MediaBundleError("bundle manifest media entry is malformed or duplicated")
        media_ids.add(media_id)
        media_paths.add(relative)
    if media_paths != {path for path in seen if path.startswith("media/")}:
        raise MediaBundleError("bundle manifest media files and entries disagree")
    # A manifest is an allow-list, not merely a list of things we happened to
    # check. An extra file could otherwise ride inside the same restic snapshot
    # without ever being attributed to the producer's snapshot.
    allowed = seen | {"manifest.json"}
    for path in run.rglob("*"):
        relative = path.relative_to(run).as_posix()
        if path.is_symlink():
            raise MediaBundleError(f"bundle contains a symbolic link: {relative}")
        if path.is_file() and relative not in allowed:
            raise MediaBundleError(f"bundle contains an unlisted file: {relative}")
    return PreparedMediaBundle(run_id=expected_id, path=run)
