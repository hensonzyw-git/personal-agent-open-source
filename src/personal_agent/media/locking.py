"""The fixed lock set of multimodal design §4.1.

Every operation that touches media state -- authorization, deletion marking,
GC, crash recovery, publish -- takes the same two-level lock in the same order:
a shared global storage lock, then exclusive locks on the stripes the objects
belong to, then a fresh database transaction. Release is the reverse. Nothing
in here is advisory politeness: the design's whole recovery story assumes that
"who may write this object" is decided under this lock, so a path that skips it
is a correctness bug even when it happens to work.

Three properties are load-bearing and each is easy to get wrong:

- **A lock is a file descriptor, not a name.** `flock` binds to the open file
  description. Two requests handled by one process must therefore each open
  their own descriptor; sharing one would let a single process exclude itself
  from itself and call that mutual exclusion. `_FileLock` opens per acquisition
  and never caches, forks or duplicates inside the critical section.

- **The descriptors are not ours to create.** The lock files and their
  directory are made at install time with permissions that let the service open
  and lock them but not unlink or replace them. If the service could replace a
  lock file it could hand two writers different inodes for the same name and
  the two would both believe they held the lock. So this module never creates
  a lock file at runtime, and :func:`verify_lock_installation` refuses to hand
  back a working lock set when that property does not hold.

- **Stripes are ordered.** A request touching several objects takes its stripes
  in sorted, de-duplicated order so two requests that share objects cannot
  deadlock by taking them in opposite orders. Media reuse is what makes this
  reachable.

`O_CLOEXEC` on every descriptor only prevents inheritance across `exec`; it
does nothing about `fork`. Nothing here forks, and the design says the critical
section must not start to.
"""

from __future__ import annotations

import fcntl
import os
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

#: The design fixes this at 64 as a single-user engineering parameter, and says
#: changing it requires stopping the service and draining every process before
#: migrating. It is part of the storage configuration version for that reason.
STRIPE_COUNT = 64

LOCKS_DIRNAME = "locks"
STORAGE_LOCK_NAME = "storage.lock"


class MediaLockError(RuntimeError):
    """The lock set is not usable, so no media operation may proceed."""


def stripe_of(media_id: str) -> int:
    """Which of the fixed stripes an object belongs to.

    The same expression the design specifies, so a stripe number computed here
    and one computed by an operator's script agree.
    """
    digest = sha256(media_id.encode("utf-8")).digest()
    return int.from_bytes(digest, "big") % STRIPE_COUNT


def stripe_lock_name(stripe: int) -> str:
    if not 0 <= stripe < STRIPE_COUNT:
        raise MediaLockError(f"stripe {stripe} outside 0..{STRIPE_COUNT - 1}")
    return f"stripe-{stripe:02d}.lock"


def locks_directory(root: Path) -> Path:
    return Path(root) / LOCKS_DIRNAME


def ensure_lock_files(root: Path) -> list[Path]:
    """Create any missing lock file and the lock directory. Install-time only.

    Deliberately separate from the runtime path: a service that can create a
    lock file at runtime can also replace one, and a replaced lock file is a
    different inode that silently stops excluding anybody. An operator runs
    this once, then removes the service's write permission on the directory.
    """
    directory = locks_directory(root)
    directory.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    for name in [STORAGE_LOCK_NAME] + [
        stripe_lock_name(stripe) for stripe in range(STRIPE_COUNT)
    ]:
        path = directory / name
        if not path.exists():
            # 0o444: the service opens and locks it, and can neither write it
            # nor -- given a non-writable directory -- unlink it.
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o444)
            os.close(fd)
            created.append(path)
    return created


def _swap_problems(directory: Path, boundary: Path | None) -> list[str]:
    """Every directory from `directory` up to `boundary` that this identity could
    rename the lock set out of.

    Replacing a lock is not only a permission on the lock's own directory. The
    directory itself can be renamed away and a fresh one put in its place, which
    swaps every lock file at once for inodes nothing else holds; and that is
    possible exactly when the *parent* is writable. So the whole chain matters,
    one hop per directory, and each hop is checked.

    Two independent reasons a directory is not safe to rely on:

    - the running identity can write it, so it can rename the entry below it;
    - the running identity *owns* it, because an owner can chmod a read-only
      directory back to writable and then do the same thing. A mode check alone
      therefore proves nothing about a directory the service owns.

    `boundary` is the topmost directory the deployment declares it protects;
    the walk stops before it, because above that point the check would be
    asserting something about the host rather than about this installation.
    `None` walks to the filesystem root.
    """
    problems: list[str] = []
    running_uid = os.geteuid()
    current = directory
    while True:
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            problems.append(f"{current} does not exist")
            return problems
        if stat.S_ISLNK(info.st_mode):
            # A symlinked ancestor means the path being checked is not
            # necessarily the path being used, so the rest of the chain says
            # nothing. Stop rather than report on the wrong tree.
            problems.append(f"{current} is a symlink, so the lock path is not fixed")
            return problems

        if info.st_uid == running_uid:
            problems.append(
                f"{current} is owned by the running identity, which can chmod it "
                "writable and rename the lock set out from under a held lock"
            )
        elif os.access(current, os.W_OK) and not info.st_mode & stat.S_ISVTX:
            problems.append(
                f"{current} is writable by this process, so everything inside it "
                "-- including the lock directory -- can be renamed and replaced"
            )

        parent = current.parent
        if parent == current or (boundary is not None and parent == boundary):
            return problems
        current = parent


def verify_lock_installation(root: Path, *, boundary: Path | None = None) -> list[str]:
    """Report every reason the lock set cannot be trusted. Empty means usable.

    This is the check that makes "the service cannot replace a lock file" a
    tested property rather than an assumption about how the box was set up. It
    looks at the filesystem, not at configuration, because the things that
    actually stop a replacement are the directory's write bit and its owner.
    """
    problems: list[str] = []
    directory = locks_directory(root)
    if not directory.is_dir():
        return [f"lock directory {directory} does not exist"]

    # The lock directory and every ancestor above it: a read-only lock
    # directory inside a writable parent is the same hole one level up.
    problems.extend(_swap_problems(directory, boundary))

    for name in [STORAGE_LOCK_NAME] + [
        stripe_lock_name(stripe) for stripe in range(STRIPE_COUNT)
    ]:
        path = directory / name
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            problems.append(f"lock file {path} is missing")
            continue
        if stat.S_ISLNK(info.st_mode):
            problems.append(f"lock file {path} is a symlink, not a regular file")
            continue
        if not stat.S_ISREG(info.st_mode):
            problems.append(f"lock file {path} is not a regular file")
            continue
        if info.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
            problems.append(f"lock file {path} is writable, so it can be truncated")
    return problems


class _FileLock:
    """One held `flock`, owning exactly one descriptor.

    Opening is `O_NOFOLLOW | O_CLOEXEC` and the result is checked to be a
    regular file, so a symlink planted at a lock path is a refusal rather than
    a lock taken on whatever it points at.
    """

    def __init__(self, path: Path, mode: int, *, blocking: bool) -> None:
        self._path = path
        self._mode = mode
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            self._fd: int | None = os.open(path, flags)
        except OSError as exc:
            raise MediaLockError(f"cannot open lock file {path}: {exc}") from exc
        info = os.fstat(self._fd)
        if not stat.S_ISREG(info.st_mode):
            os.close(self._fd)
            self._fd = None
            raise MediaLockError(f"lock path {path} is not a regular file")
        operation = mode | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(self._fd, operation)
        except OSError as exc:
            os.close(self._fd)
            self._fd = None
            raise MediaLockError(f"cannot lock {path}: {exc}") from exc

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


@contextmanager
def media_locks(
    root: Path,
    media_ids: Iterable[str] = (),
    *,
    exclusive_storage: bool = False,
    blocking: bool = True,
) -> Iterator[None]:
    """Hold the design's lock set for the given objects.

    Acquires the global storage lock, then every stripe the objects fall in, in
    sorted order and once each, and releases them in reverse. `exclusive_storage`
    is what backup takes before a snapshot; everything else takes it shared.

    Waiting is bounded by the caller, not here: the design forbids holding a
    database transaction while waiting on a file lock, so this must be entered
    *before* the transaction opens, and a caller that cannot afford to block
    passes `blocking=False` and handles :class:`MediaLockError`.
    """
    directory = locks_directory(root)
    held: list[_FileLock] = []
    storage_mode = fcntl.LOCK_EX if exclusive_storage else fcntl.LOCK_SH
    try:
        held.append(
            _FileLock(
                directory / STORAGE_LOCK_NAME, storage_mode, blocking=blocking
            )
        )
        stripes = sorted({stripe_of(media_id) for media_id in media_ids})
        for stripe in stripes:
            held.append(
                _FileLock(
                    directory / stripe_lock_name(stripe),
                    fcntl.LOCK_EX,
                    blocking=blocking,
                )
            )
        yield
    finally:
        for lock in reversed(held):
            lock.release()
