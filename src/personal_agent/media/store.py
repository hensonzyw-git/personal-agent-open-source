"""Where media bytes live, and the publish protocol that moves them for good.

Multimodal design §5.3 and §4.2. Two storage layers and no third one:

- **staging** (`staging/<media_id>/<attempt>.part`) is one upload attempt's
  bytes while they are being written and after they are sealed but before they
  are published. It is not a persistent object; §5.3 says a published staging
  file is cleaned up and nothing else ever refers to it.
- **final** (`final/<xx>/<media_id>.bin`) is the persisted image -- the single
  durable object, the one FR-PHOTO-06 keeps with the Timeline.

The option-1 revision deleted the middle "candidate" layer along with the
normalizer, so publishing is not a conversion: it is an install of bytes that
were already final in content, plus the database compare-and-swap that makes
them authoritative. That is also why §5.3's crash matrix is asymmetric -- a
published file with no committed row is recoverable by *verifying* it against
the attempt's seal record, because there is no second derivation to compare
against and no normalizer version that could have moved.

**Publishing never overwrites.** `os.link` fails with `FileExistsError` when the
target exists, which is the same guarantee Linux's `renameat2(RENAME_NOREPLACE)`
gives, and unlike `os.rename` it cannot silently replace a persisted image with
a different one. The staging name is then unlinked, leaving one name for the
inode. A crash between the two leaves two names for one inode, which is
harmless and which staging cleanup removes. The link is what is atomic; nothing
here depends on the two steps being one.

**An attempt number is not decoration.** Every chunk's AAD binds the media id,
the attempt number and the chunk index, so a persisted image can only be read
back by naming the attempt that produced it. `read_final` therefore takes the
attempt number rather than assuming the first one: a store that assumed would
work for every first-try upload and fail authentication for every retry.

**Nothing here trusts a path.** Every path is built from a validated UUIDv4 and
a positive attempt number, never from a client-supplied string, and every open
is `O_NOFOLLOW` with the result checked to be a regular file.

Production wiring must call :meth:`MediaStore.verify_installation` during
startup, before the service accepts an upload. It is deliberately not called by
the constructor, so that a test can build a store over a scratch directory
without fabricating a whole installed lock set.
"""

from __future__ import annotations

import enum
import os
import shutil
import stat
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from personal_agent.media.container import (
    DEFAULT_CHUNK_BYTES,
    DEFAULT_MAX_CONTENT_BYTES,
    DEFAULT_MEDIA_ROLE,
    ContainerError,
    SealRecord,
    StagingWriter,
    read_container,
    write_container,
)
from personal_agent.media.locking import verify_lock_installation
from personal_agent_core.crypto import KeyRing
from personal_agent_core.ids import InvalidIdentifierError, require_uuid4


STAGING_DIRNAME = "staging"
FINAL_DIRNAME = "final"
QUARANTINE_DIRNAME = "quarantine"


class MediaStoreError(RuntimeError):
    """The store cannot be used safely, or was asked to break a storage rule."""


class MediaIntegrityError(RuntimeError):
    """A `ready` object's persisted image is missing or does not authenticate.

    §5.3's row for that state is "integrity failure, refuse and alert". It is a
    separate type from :class:`MediaStoreError` because it is an operational
    incident about data that used to be good, not a rejected request.
    """


class FinalOutcome(enum.Enum):
    """What recovery found at the final path."""

    ABSENT = "absent"
    ADOPTED = "adopted"
    QUARANTINED = "quarantined"


@dataclass(frozen=True)
class MediaRoots:
    """The three controlled directories, resolved from one root."""

    root: Path

    @property
    def staging(self) -> Path:
        return self.root / STAGING_DIRNAME

    @property
    def final(self) -> Path:
        return self.root / FINAL_DIRNAME

    @property
    def quarantine(self) -> Path:
        return self.root / QUARANTINE_DIRNAME


def _require_media_id(media_id: object) -> str:
    try:
        return require_uuid4(media_id)
    except InvalidIdentifierError as exc:
        raise MediaStoreError(f"media id is not a UUIDv4: {exc}") from exc


def _require_attempt(attempt_number: object) -> int:
    if isinstance(attempt_number, bool) or not isinstance(attempt_number, int):
        raise MediaStoreError("attempt number must be an integer")
    if attempt_number < 1:
        raise MediaStoreError("attempt number must be >= 1")
    return attempt_number


def _fsync_directory(path: Path) -> None:
    """Make a link or unlink in `path` durable.

    The design scopes this storage to a local filesystem and says explicitly
    not to put it on NFS or behind a cross-host worker, where a directory fsync
    would not mean what it says here.
    """
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def probe_non_overwriting_publish(root: Path) -> None:
    """Prove in *this* environment that publishing cannot clobber a file.

    §5.3 requires the non-overwriting install to be environment-verified rather
    than assumed, because the mechanism differs by platform and filesystem. The
    probe performs a real link against a real existing target in a scratch
    directory and requires the refusal, so a host whose `os.link` overwrites is
    rejected at startup instead of silently replacing a user's image later.

    The scratch directory is under the staging area rather than directly under
    the storage root, and that placement is load-bearing rather than tidy. The
    lock set lives at ``root/locks``, so replacing it is a rename of a directory
    entry in ``root`` -- which means ``root`` has to be unwritable by the
    service for the lock check to pass. A probe that created its scratch
    directory in ``root`` needed exactly the opposite, and the two checks could
    never both be satisfied. Staging is the area the service already writes, and
    it is on the same filesystem as the published files, so `os.link` behaves
    here exactly as it does at the publish destination.
    """
    staging = Path(root) / STAGING_DIRNAME
    if not staging.is_dir():
        raise MediaStoreError(
            f"cannot run the publish probe: {staging} does not exist, so the "
            "installation is incomplete"
        )
    probe_root = staging / ".publish-probe"
    try:
        probe_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # A root this identity cannot write is a finding, not a crash. The
        # caller is a verifier: it reports every reason the host is unusable,
        # so an unanticipated OSError here must become one of those reasons
        # rather than propagating out of the check that was meant to state it.
        raise MediaStoreError(
            f"cannot run the publish probe: {probe_root} is not writable by "
            f"this identity ({exc})"
        ) from exc
    try:
        with tempfile.TemporaryDirectory(dir=probe_root) as scratch:
            source = Path(scratch) / "source"
            target = Path(scratch) / "target"
            source.write_bytes(b"source")
            target.write_bytes(b"target")
            try:
                os.link(source, target)
            except FileExistsError:
                return
            except OSError as exc:
                raise MediaStoreError(
                    f"this filesystem cannot publish without overwriting: {exc}"
                ) from exc
            raise MediaStoreError(
                "os.link replaced an existing file; publishing could clobber a "
                "persisted image"
            )
    finally:
        shutil.rmtree(probe_root, ignore_errors=True)


def verify_media_installation(root: Path, *, boundary: Path | None = None) -> list[str]:
    """Every reason this host cannot store media safely. Empty means usable.

    Both halves are checks against the filesystem rather than against
    configuration, because both properties *are* filesystem properties: the
    lock set must not be replaceable, and publishing must not be able to
    overwrite. `boundary` is passed through to the lock check, which walks the
    lock directory's ancestors up to it.
    """
    problems = list(verify_lock_installation(root, boundary=boundary))
    try:
        probe_non_overwriting_publish(root)
    except MediaStoreError as exc:
        problems.append(str(exc))
    return problems


class MediaStore:
    """Encrypted staging, publishing and cleanup for one deployment."""

    def __init__(
        self,
        root: Path,
        keyring: KeyRing,
        *,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
        max_content_bytes: int = DEFAULT_MAX_CONTENT_BYTES,
        role: str = DEFAULT_MEDIA_ROLE,
    ) -> None:
        self._root = Path(root)
        self._keyring = keyring
        self._chunk_bytes = chunk_bytes
        self._max_content_bytes = max_content_bytes
        # Bound into every chunk's AAD. One store holds one role's objects, so
        # a store cannot be pointed at another role's files and read them.
        self._role = role

    @property
    def roots(self) -> MediaRoots:
        return MediaRoots(root=self._root)

    def verify_installation(self) -> None:
        """Fail closed if this host cannot store media safely. Call at startup."""
        problems = verify_media_installation(self._root)
        if problems:
            raise MediaStoreError(
                "media storage is not usable here: " + "; ".join(problems)
            )

    def prepare_directories(self) -> None:
        """Create the three storage directories if absent. Install-time only.

        Separate from construction for the same reason the lock files are: a
        service that creates a storage directory at runtime can also recreate
        one an operator deliberately removed.
        """
        for directory in (
            self.roots.staging,
            self.roots.final,
            self.roots.quarantine,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    # --- paths ------------------------------------------------------------

    def staging_path(self, media_id: str, attempt_number: int) -> Path:
        media_id = _require_media_id(media_id)
        attempt = _require_attempt(attempt_number)
        return self.roots.staging / media_id / f"{attempt}.part"

    def final_path(self, media_id: str) -> Path:
        media_id = _require_media_id(media_id)
        # Sharded by the first two hex characters so one deployment's images do
        # not all land in a single directory.
        return self.roots.final / media_id[:2] / f"{media_id}.bin"

    def quarantine_path(self, media_id: str) -> Path:
        media_id = _require_media_id(media_id)
        return self.roots.quarantine / f"{media_id}.bin"

    # --- writing ----------------------------------------------------------

    def write_staging(
        self,
        media_id: str,
        attempt_number: int,
        chunks: Iterable[bytes],
        *,
        max_bytes: int | None = None,
    ) -> SealRecord:
        """Seal one upload attempt's bytes into its staging file.

        `max_bytes` is counted against what actually arrives rather than against
        what the client declared (§4.2: "上限按实际流式累计计数"), and going over
        it removes the staging file rather than leaving a truncated one.
        """
        media_id = _require_media_id(media_id)
        attempt = _require_attempt(attempt_number)
        path = self.staging_path(media_id, attempt)
        # Whether we own the directory decides how far a failure may clean up:
        # an existing per-attempt directory may hold another attempt's file,
        # and removing it would destroy work this call never wrote.
        directory_was_ours = not path.parent.exists()
        path.parent.mkdir(parents=True, exist_ok=True)

        ceiling = self._max_content_bytes if max_bytes is None else max_bytes
        try:
            return write_container(
                path,
                _bounded(chunks, ceiling),
                keyring=self._keyring,
                media_id=media_id,
                attempt_number=attempt,
                role=self._role,
                chunk_bytes=self._chunk_bytes,
            )
        except ContainerError:
            # `write_container` already removes its own file, so only a
            # directory this call created can safely go.
            if directory_was_ours:
                shutil.rmtree(path.parent, ignore_errors=True)
            raise

    def staging_writer(self, media_id: str, attempt_number: int) -> StagingWriter:
        """An unopened incremental writer for one attempt's staging file.

        Returned unopened on purpose. §4.2 requires the check and the file
        operation to be inseparable -- "检查与文件写入不分离" -- so the caller
        opens (or appends) only after re-reading state, owner, attempt and
        deadline inside the lock. Opening here, where there is no lock, would
        hand back a file handle that was created without anyone having checked
        whether this attempt is still allowed to write.
        """
        media_id = _require_media_id(media_id)
        attempt = _require_attempt(attempt_number)
        path = self.staging_path(media_id, attempt)
        path.parent.mkdir(parents=True, exist_ok=True)
        return StagingWriter(
            path,
            keyring=self._keyring,
            media_id=media_id,
            attempt_number=attempt,
            role=self._role,
            chunk_bytes=self._chunk_bytes,
        )

    def read_staging(
        self,
        media_id: str,
        attempt_number: int,
        seal: SealRecord | dict[str, Any] | None,
    ) -> bytes:
        return read_container(
            self.staging_path(media_id, attempt_number),
            seal,
            keyring=self._keyring,
            media_id=media_id,
            attempt_number=_require_attempt(attempt_number),
            role=self._role,
            chunk_bytes=self._chunk_bytes,
            max_content_bytes=self._max_content_bytes,
        )

    def read_final(
        self,
        media_id: str,
        attempt_number: int,
        seal: SealRecord | dict[str, Any] | None,
    ) -> bytes:
        """The persisted image's plaintext, authenticated against its attempt.

        `attempt_number` is the attempt that published this file -- the one
        `media_objects.current_attempt_number` names -- not necessarily 1.
        """
        return read_container(
            self.final_path(media_id),
            seal,
            keyring=self._keyring,
            media_id=media_id,
            attempt_number=_require_attempt(attempt_number),
            role=self._role,
            chunk_bytes=self._chunk_bytes,
            max_content_bytes=self._max_content_bytes,
        )

    def verify_ready_final(
        self,
        media_id: str,
        attempt_number: int,
        seal: SealRecord | dict[str, Any] | None,
    ) -> None:
        """Check a `ready` object's file, or raise :class:`MediaIntegrityError`.

        §5.3's "ready, file missing or does not authenticate" row: refuse and
        alert, and specifically do not treat it as ordinary recovery by
        generating another content.
        """
        try:
            self.read_final(media_id, attempt_number, seal)
        except ContainerError as exc:
            raise MediaIntegrityError(
                f"persisted image for {media_id} failed verification: {exc}"
            ) from exc

    # --- publishing -------------------------------------------------------

    def publish(self, media_id: str, attempt_number: int) -> None:
        """Install a sealed staging file as this object's persisted image.

        Non-overwriting by construction: the link fails if the target exists,
        and that failure is propagated rather than retried against a different
        name. §5.3 is explicit that a disk error must never be reported as
        `ready`, so nothing here swallows one.
        """
        media_id = _require_media_id(media_id)
        attempt = _require_attempt(attempt_number)
        staging = self.staging_path(media_id, attempt)
        final = self.final_path(media_id)
        self._require_regular_file(staging, "staging")
        final.parent.mkdir(parents=True, exist_ok=True)

        try:
            os.link(staging, final)
        except FileExistsError as exc:
            raise MediaStoreError(
                f"refusing to overwrite an existing persisted image at {final}"
            ) from exc
        except OSError as exc:
            raise MediaStoreError(f"cannot publish to {final}: {exc}") from exc

        os.unlink(staging)
        # The link is what durability depends on, so the directory entry is
        # synced before the caller may record the object as ready.
        _fsync_directory(final.parent)
        shutil.rmtree(staging.parent, ignore_errors=True)

    def final_exists(self, media_id: str) -> bool:
        try:
            info = os.lstat(self.final_path(media_id))
        except FileNotFoundError:
            return False
        return stat.S_ISREG(info.st_mode)

    def recover_final(
        self,
        media_id: str,
        attempt_number: int,
        seal: SealRecord | dict[str, Any] | None,
    ) -> FinalOutcome:
        """Decide what an already-persisted file is, without replacing it.

        §5.3's "final persisted, database not committed" row: verify the
        existing bytes against the attempt's seal record and adopt them;
        anything that does not verify is quarantined and alerted, and is never
        overwritten or regenerated.
        """
        media_id = _require_media_id(media_id)
        attempt = _require_attempt(attempt_number)
        if not self.final_exists(media_id):
            return FinalOutcome.ABSENT
        try:
            self.read_final(media_id, attempt, seal)
        except ContainerError:
            self.quarantine_final(media_id)
            return FinalOutcome.QUARANTINED
        return FinalOutcome.ADOPTED

    def quarantine_final(self, media_id: str) -> Path:
        """Move an unverifiable persisted image aside, keeping it for review.

        Quarantine rather than delete because these bytes are the only copy of
        something that was nearly published, and §5.3 wants them available to
        whoever decides what happened.
        """
        media_id = _require_media_id(media_id)
        final = self.final_path(media_id)
        target = self.quarantine_path(media_id)
        self.roots.quarantine.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise MediaStoreError(
                f"quarantine already holds an entry for {media_id}; a human must "
                "resolve the earlier one first"
            )
        try:
            os.link(final, target)
        except OSError as exc:
            raise MediaStoreError(f"cannot quarantine {final}: {exc}") from exc
        os.unlink(final)
        _fsync_directory(target.parent)
        return target

    # --- cleanup ----------------------------------------------------------

    def discard_staging(self, media_id: str, attempt_number: int) -> bool:
        """Remove one attempt's staging bytes. Idempotent.

        Returns whether anything was removed, so a caller can tell "cleaned"
        from "already gone" without treating the second as a failure -- a
        replayed cleanup and a crash midway through one both land here.
        """
        media_id = _require_media_id(media_id)
        attempt = _require_attempt(attempt_number)
        staging = self.staging_path(media_id, attempt)
        removed = False
        try:
            os.unlink(staging)
            removed = True
        except FileNotFoundError:
            removed = False
        except OSError as exc:
            raise MediaStoreError(f"cannot remove staging {staging}: {exc}") from exc
        # The per-attempt directory holds only this attempt's file, so an empty
        # one is ours to drop; a non-empty one refuses and is left alone.
        try:
            os.rmdir(staging.parent)
        except OSError:
            pass
        return removed

    def discard_final(self, media_id: str) -> bool:
        """Remove the persisted image. Idempotent; returns what it removed.

        Reached only under the §4.1 lock and after the durable deletion marker
        is committed -- this method is the physical half, not the decision, and
        it deliberately refuses a symlink or any other foreign file type rather
        than unlinking whatever is at the path.
        """
        media_id = _require_media_id(media_id)
        final = self.final_path(media_id)
        try:
            info = os.lstat(final)
        except FileNotFoundError:
            # §5.3's reaping-interrupted row: a missing file counts as done.
            return False
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise MediaStoreError(f"final at {final} is not a regular file")
        try:
            os.unlink(final)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise MediaStoreError(f"cannot remove final {final}: {exc}") from exc
        _fsync_directory(final.parent)
        return True

    def _require_regular_file(self, path: Path, what: str) -> None:
        try:
            info = os.lstat(path)
        except FileNotFoundError as exc:
            raise MediaStoreError(f"no {what} at {path}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise MediaStoreError(f"{what} at {path} is a symlink, refusing it")
        if not stat.S_ISREG(info.st_mode):
            raise MediaStoreError(f"{what} at {path} is not a regular file")


def _bounded(chunks: Iterable[bytes], ceiling: int) -> Iterator[bytes]:
    """Pass chunks through while their running total stays under `ceiling`."""
    total = 0
    for chunk in chunks:
        total += len(chunk)
        if total > ceiling:
            raise ContainerError(
                f"upload passed its {ceiling}-byte ceiling at {total} bytes"
            )
        yield chunk
