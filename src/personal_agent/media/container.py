"""The sealed chunk container: the only format the media store persists.

Multimodal design §5.1. A media file is a version header followed by a sequence
of independently sealed chunks, plus a **seal record** that authenticates the
whole stream -- chunk count, total plaintext bytes, and the SHA-256 of the
plaintext in order. Every property the design asks a reader to check lives in
one of those two places, and each check exists because of a specific way the
file could otherwise lie:

- **Reordering and cross-object splicing** are refused by the AAD, not by a
  comparison. Each chunk's row id is `media_id:attempt:index`, so a chunk moved
  to a different position, or lifted from another object or another attempt of
  the same object, fails authentication rather than decrypting into the wrong
  place. A reader that merely concatenated decrypted chunks would return a
  plausible image built from the wrong bytes.
- **Truncation and missing chunks** are refused by the sealed chunk count, and
  independently by the sealed total byte count and the whole-stream hash. Three
  independent checks on one property is deliberate: a partially written upload
  must never be readable as a complete image.
- **Extra and duplicated chunks** are refused by the same count, because a
  reader that stopped at "sealed chunk count reached" would accept trailing
  bytes and call them part of the image.
- **An unsealed container** is refused outright. A file that was never sealed
  is an upload still in flight, and reading it would publish bytes whose
  completeness nothing has established.

Two bounds keep the reader from being talked into work by its input (§5.4:
"不按声明长度分配内存"):

- a chunk's declared record length is refused before anything is read if it
  exceeds :data:`MAX_CHUNK_BYTES`, and
- the whole stream is refused on its *size* if it exceeds
  :data:`DEFAULT_MAX_CONTENT_BYTES`, before its contents are examined.

Everything fails closed. There is no "best effort" path, no partial result and
no repair: §5.1 says the reader must not truncate, drop or repair, and this is
the one place in the design that reads bytes it did not produce.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from personal_agent_core.crypto import CryptoError, KeyRing
from personal_agent_core.ids import InvalidIdentifierError, require_uuid4

#: Container magic and version. The version is stored so a future format change
#: is a refusal on an old file rather than a misread of it.
CONTAINER_MAGIC: Final[bytes] = b"PAMEDIA1"
CONTAINER_VERSION: Final[int] = 1

#: Hard ceiling on one chunk's record, checked against the length prefix before
#: any read. The upload path never produces a chunk near this; the bound exists
#: so a hand-crafted length cannot ask the reader to allocate gigabytes.
MAX_CHUNK_BYTES: Final[int] = 4 * 1024 * 1024

#: The streaming chunk size. Every write is bounded by this, which is what makes
#: "network receive uses a bounded buffer" true rather than aspirational.
DEFAULT_CHUNK_BYTES: Final[int] = 1024 * 1024

#: Ceiling on the plaintext of one media object. The engine's configured upload
#: limit is the real value; this is the reader's own guard so that a file grown
#: behind the service's back is refused on its size.
DEFAULT_MAX_CONTENT_BYTES: Final[int] = 32 * 1024 * 1024

#: The AAD binding for a chunk. §5.1 requires it to bind "media_id、文件角色、
#: attempt/格式版本"; all four travel in the row id below, so a chunk is only
#: ever readable at the exact position it was sealed for.
#:
#: "文件角色" is read here as the object's **purpose** (`chat_image`), not as
#: the storage layer. It cannot mean the storage layer under option 1: the
#: persisted image *is* the sealed staging bytes, so a role that changed name
#: on publish would make every published file unreadable. The reading is
#: recorded as an interpretation of an ambiguous normative line, not as a
#: settled fact -- see the note accompanying this change.
_AAD_TABLE: Final[str] = "media_attempts"
_AAD_COLUMN: Final[str] = "encrypted_chunk"

#: The only role this round produces. §5.1 fixes `purpose=chat_image` for chat
#: images; a second purpose must parameterise this rather than reuse the value,
#: or chunks would be readable across object kinds.
DEFAULT_MEDIA_ROLE: Final[str] = "chat_image"

_HEADER_BYTES: Final[int] = len(CONTAINER_MAGIC) + 1
_LENGTH_BYTES: Final[int] = 4


class ContainerError(RuntimeError):
    """The container is malformed, tampered with, or not what its seal says."""


class ChunkHasher:
    """Running plaintext totals for a stream being written.

    Kept separate from the writer so the upload path can report progress and
    enforce its byte ceiling without holding the whole body in memory.
    """

    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self._total_bytes = 0
        self._chunk_count = 0

    def update(self, chunk: bytes) -> None:
        self._digest.update(chunk)
        self._total_bytes += len(chunk)
        self._chunk_count += 1

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    @property
    def chunk_count(self) -> int:
        return self._chunk_count

    def hexdigest(self) -> str:
        return self._digest.hexdigest()


@dataclass(frozen=True)
class SealRecord:
    """What a sealed stream commits to, and what a reader checks it against.

    Serialised into `media_attempts.encrypted_seal_record`, so recovery can
    compare a published file against the attempt that produced it instead of
    trusting that the write finished.
    """

    chunk_count: int
    total_bytes: int
    sha256: str
    format_version: int = CONTAINER_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "chunk_count": self.chunk_count,
            "total_bytes": self.total_bytes,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "SealRecord":
        if not isinstance(data, dict):
            raise ContainerError("seal record must be an object")
        try:
            format_version = int(data["format_version"])
            chunk_count = int(data["chunk_count"])
            total_bytes = int(data["total_bytes"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ContainerError(f"seal record is incomplete: {exc}") from exc
        digest = data.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ContainerError("seal record has no usable sha256")
        if chunk_count < 1 or total_bytes < 0:
            raise ContainerError("seal record describes an impossible stream")
        return cls(
            chunk_count=chunk_count,
            total_bytes=total_bytes,
            sha256=digest,
            format_version=format_version,
        )


def _require_media_id(media_id: str) -> str:
    try:
        return require_uuid4(media_id)
    except InvalidIdentifierError as exc:
        raise ContainerError(f"media id is not a UUIDv4: {exc}") from exc


def _require_role(role: str) -> str:
    if not isinstance(role, str) or not role:
        raise ContainerError("media role must be a non-empty string")
    if ":" in role:
        raise ContainerError(f"media role {role!r} may not contain ':'")
    return role


def _row_id(
    media_id: str, role: str, attempt_number: int, index: int, format_version: int
) -> str:
    """The AAD row id: every property a chunk must not be moveable across.

    `media_id` is a UUIDv4 and `role` is a closed vocabulary with no separator
    in it, so the colon-joined form parses unambiguously.
    """
    return f"{media_id}:{role}:{format_version}:{attempt_number}:{index}"


def _seal_chunk(
    chunk: bytes,
    *,
    keyring: KeyRing,
    media_id: str,
    role: str,
    attempt_number: int,
    index: int,
) -> bytes:
    envelope = keyring.encrypt(
        chunk,
        table=_AAD_TABLE,
        column=_AAD_COLUMN,
        row_id=_row_id(media_id, role, attempt_number, index, CONTAINER_VERSION),
    )
    payload = json.dumps(
        envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return len(payload).to_bytes(_LENGTH_BYTES, "big") + payload


class StagingWriter:
    """Writes one attempt's staging file, one sealed chunk at a time.

    §4.2 requires the upload's *write batches* to be individually lock-scoped:
    "PUT 每次写入批次都在锁内重读 state、owner、attempt、期限，才打开/追加自己的
    staging". A single-pass writer cannot honour that -- the caller would have
    to hold the storage lock across the whole network receive, which is the one
    thing §4.2's separation of receive (outside) from write (inside) exists to
    prevent. So the file is opened once here and each chunk is appended by a
    separate call, letting the caller re-take the lock and re-check the attempt
    between calls without ever reopening the file.

    The file handle deliberately survives across lock releases. What the design
    forbids is an *expired* writer reopening and appending, and that is enforced
    by the caller's per-append check: a writer whose attempt has been taken over
    is refused before the next append, and :meth:`abort` then removes the file.
    Keeping the descriptor open is safe because nothing else writes this path --
    it was created `O_EXCL` and belongs to this attempt alone.
    """

    def __init__(
        self,
        path: Path,
        *,
        keyring: KeyRing,
        media_id: str,
        attempt_number: int,
        role: str = DEFAULT_MEDIA_ROLE,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    ) -> None:
        self._path = path
        self._keyring = keyring
        self._media_id = _require_media_id(media_id)
        self._role = _require_role(role)
        if attempt_number < 1:
            raise ContainerError("attempt number must be >= 1")
        if not 0 < chunk_bytes <= MAX_CHUNK_BYTES:
            raise ContainerError(
                f"chunk size {chunk_bytes} outside 1..{MAX_CHUNK_BYTES}"
            )
        self._attempt_number = attempt_number
        self._chunk_bytes = chunk_bytes
        self._hasher = ChunkHasher()
        self._handle: Any | None = None
        self._sealed = False
        #: Whether *this* writer created the file. `open()` loses to `O_EXCL`
        #: when another attempt already owns the path, and the loser's
        #: `abort()` must not delete the winner's file: a caller cleans up on
        #: its error path, so an unowned abort would destroy a sealed upload
        #: that nothing had asked to remove.
        self._owns_file = False

    @property
    def chunk_bytes(self) -> int:
        """The largest batch :meth:`append` accepts.

        The caller sizes its receive batches from this so that one received
        batch is exactly one sealed chunk.
        """
        return self._chunk_bytes

    @property
    def chunk_count(self) -> int:
        return self._hasher.chunk_count

    @property
    def total_bytes(self) -> int:
        return self._hasher.total_bytes

    def open(self) -> None:
        """Create the staging file. Refuses if it already exists.

        `O_CREAT | O_EXCL` is what makes "an attempt owns exactly one staging
        file" a filesystem fact rather than a convention, and it is why a
        replayed or racing PUT cannot adopt another writer's bytes.
        """
        if self._handle is not None:
            raise ContainerError("staging writer is already open")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            fd = os.open(self._path, flags, 0o600)
        except FileExistsError as exc:
            raise ContainerError(f"staging already exists at {self._path}") from exc
        except OSError as exc:
            raise ContainerError(
                f"cannot create staging at {self._path}: {exc}"
            ) from exc
        # `O_EXCL` succeeded, so this call created the file and is the only
        # thing entitled to remove it.
        self._owns_file = True
        handle = os.fdopen(fd, "wb", closefd=True)
        try:
            handle.write(CONTAINER_MAGIC)
            handle.write(bytes([CONTAINER_VERSION]))
        except BaseException:
            handle.close()
            self.abort()
            raise
        self._handle = handle

    def append(self, batch: bytes) -> None:
        """Seal one batch as the next chunk and append it.

        One batch is one chunk: the AAD binds the chunk index, so chunk
        boundaries are part of the authenticated content and the caller must
        not vary them between a write and its replay.
        """
        if self._sealed:
            raise ContainerError("staging writer is already sealed")
        if self._handle is None:
            raise ContainerError("staging writer is not open")
        if len(batch) > self._chunk_bytes:
            # A batch over the configured chunk size is a caller contract
            # violation: one batch is one chunk, and the chunk boundaries are
            # part of the authenticated content. The writer cannot continue
            # meaningfully, so it fails closed like every other refusal here
            # rather than leaving a partial file for a caller to reason about.
            self.abort()
            raise ContainerError(
                f"batch is {len(batch)} bytes, over the configured "
                f"{self._chunk_bytes}"
            )
        try:
            self._handle.write(
                _seal_chunk(
                    batch,
                    keyring=self._keyring,
                    media_id=self._media_id,
                    role=self._role,
                    attempt_number=self._attempt_number,
                    index=self._hasher.chunk_count,
                )
            )
        except BaseException:
            self.abort()
            raise
        self._hasher.update(batch)

    def seal(self) -> SealRecord:
        """Flush, sync and close, returning the record that commits to it all.

        `fsync` is here because §5.3 step 1 names seal as the durability point:
        once this returns the bytes are complete and durable, and the caller's
        remaining work is the directory sync and the database compare-and-swap.
        """
        if self._sealed:
            raise ContainerError("staging writer is already sealed")
        if self._handle is None:
            raise ContainerError("staging writer is not open")
        if self._hasher.chunk_count == 0:
            # An empty upload still needs a defined container shape, so that a
            # reader can refuse it on its own terms rather than as a missing
            # file, and so "empty" and "never written" stay distinguishable.
            self.append(b"")
        try:
            self._handle.flush()
            os.fsync(self._handle.fileno())
        except BaseException:
            self.abort()
            raise
        self._handle.close()
        self._handle = None
        self._sealed = True
        return SealRecord(
            chunk_count=self._hasher.chunk_count,
            total_bytes=self._hasher.total_bytes,
            sha256=self._hasher.hexdigest(),
        )

    def abort(self) -> None:
        """Close and remove the file. Safe to call at any point, repeatedly.

        A half-written staging file is never left for anything to adopt; the
        caller separately records the attempt as abandoned, and the reaper
        would remove this anyway, but failing closed means removing it now.

        After a successful :meth:`seal` this is a no-op rather than a deletion.
        Those bytes are complete and committed to by a seal record the caller
        already holds; removing them here would turn a caller's error-path
        cleanup into the destruction of a finished upload. Removing *sealed*
        staging is :meth:`MediaStore.discard_staging`'s job, which the caller
        reaches deliberately.
        """
        if self._sealed:
            return
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        if not self._owns_file:
            # This writer never created anything -- `open()` lost to `O_EXCL`,
            # or was never called. Removing the path would delete another
            # attempt's file, which is the opposite of cleaning up after
            # oneself.
            return
        self._path.unlink(missing_ok=True)


def write_container(
    path: Path,
    chunks: Iterable[bytes],
    *,
    keyring: KeyRing,
    media_id: str,
    attempt_number: int,
    role: str = DEFAULT_MEDIA_ROLE,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
) -> SealRecord:
    """Seal `chunks` into `path` in one pass, returning the record.

    The one-shot form of :class:`StagingWriter`, for callers that already hold
    every chunk -- the non-streaming paths and the tests. The upload endpoint
    uses the incremental writer instead, because §4.2 requires the lock to be
    re-taken between write batches.
    """
    writer = StagingWriter(
        path,
        keyring=keyring,
        media_id=media_id,
        attempt_number=attempt_number,
        role=role,
        chunk_bytes=chunk_bytes,
    )
    writer.open()
    try:
        for chunk in chunks:
            writer.append(chunk)
        return writer.seal()
    except BaseException:
        writer.abort()
        raise


def open_container(
    path: Path,
    seal: SealRecord | dict[str, Any] | None,
    *,
    keyring: KeyRing,
    media_id: str,
    attempt_number: int,
    role: str = DEFAULT_MEDIA_ROLE,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    max_content_bytes: int = DEFAULT_MAX_CONTENT_BYTES,
) -> Iterator[bytes]:
    """Yield the plaintext chunks of a sealed container, verifying as it goes.

    A generator, so a caller that only needs to authenticate the file (the
    recovery path checking a published image against its seal) can drive it to
    completion without ever holding the image, and a caller that needs the
    bytes can join it.
    """
    yield from _read_verified(
        path,
        seal,
        keyring=keyring,
        media_id=media_id,
        attempt_number=attempt_number,
        role=role,
        chunk_bytes=chunk_bytes,
        max_content_bytes=max_content_bytes,
    )


def read_container(
    path: Path,
    seal: SealRecord | dict[str, Any] | None,
    *,
    keyring: KeyRing,
    media_id: str,
    attempt_number: int,
    role: str = DEFAULT_MEDIA_ROLE,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    max_content_bytes: int = DEFAULT_MAX_CONTENT_BYTES,
) -> bytes:
    """The verified plaintext of a sealed container.

    Bounded by `max_content_bytes`, which is the design's "read the whole image
    into bounded memory" (§6). The bound is not a suggestion: past it the file
    is refused, never truncated to fit.
    """
    return b"".join(
        open_container(
            path,
            seal,
            keyring=keyring,
            media_id=media_id,
            attempt_number=attempt_number,
            role=role,
            chunk_bytes=chunk_bytes,
            max_content_bytes=max_content_bytes,
        )
    )


def _read_verified(
    path: Path,
    seal: SealRecord | dict[str, Any] | None,
    *,
    keyring: KeyRing,
    media_id: str,
    attempt_number: int,
    role: str,
    chunk_bytes: int,
    max_content_bytes: int,
) -> Iterator[bytes]:
    media_id = _require_media_id(media_id)
    role = _require_role(role)
    if attempt_number < 1:
        raise ContainerError("attempt number must be >= 1")
    if seal is None:
        raise ContainerError(
            "refusing to read an unsealed container: nothing has established "
            "that these bytes are complete"
        )
    record = seal if isinstance(seal, SealRecord) else SealRecord.from_dict(seal)
    if record.format_version != CONTAINER_VERSION:
        raise ContainerError(
            f"container format version {record.format_version} is not "
            f"{CONTAINER_VERSION}"
        )

    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise ContainerError(f"no container at {path}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise ContainerError(f"{path} is a symlink, refusing to follow it")
    if not stat.S_ISREG(info.st_mode):
        raise ContainerError(f"{path} is not a regular file")
    if info.st_size > max_content_bytes + _overhead_bound(record.chunk_count):
        # Refuse on the size alone: reading it first would be work the input
        # asked for and the ceiling already forbids. This is only a cheap early
        # refusal -- the bound it uses is padded for ciphertext overhead, so a
        # container of small chunks can sit well under it and still carry far
        # more plaintext than the ceiling allows. The authoritative checks are
        # the declared total below and the running total per chunk.
        raise ContainerError(
            f"container is {info.st_size} bytes, past the {max_content_bytes} "
            "ceiling for its content"
        )
    if record.total_bytes > max_content_bytes:
        # The seal states the plaintext total, so the ceiling is decidable
        # before a byte is read and without trusting the file's size.
        raise ContainerError(
            f"container holds {record.total_bytes} bytes of content, past the "
            f"{max_content_bytes} ceiling"
        )

    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ContainerError(f"cannot open container at {path}: {exc}") from exc

    hasher = ChunkHasher()
    yielded_bytes = 0
    with os.fdopen(fd, "rb", closefd=True) as handle:
        header = _read_exactly(handle, _HEADER_BYTES, "header")
        if header[: len(CONTAINER_MAGIC)] != CONTAINER_MAGIC:
            raise ContainerError("container magic does not match")
        if header[len(CONTAINER_MAGIC)] != CONTAINER_VERSION:
            raise ContainerError(
                f"container declares format version "
                f"{header[len(CONTAINER_MAGIC)]}, not {CONTAINER_VERSION}"
            )

        for index in range(record.chunk_count):
            length_prefix = _read_exactly(handle, _LENGTH_BYTES, f"chunk {index} length")
            length = int.from_bytes(length_prefix, "big")
            if length <= 0:
                raise ContainerError(f"chunk {index} declares length {length}")
            if length > MAX_CHUNK_BYTES:
                # Checked against the declaration, before any read of it.
                raise ContainerError(
                    f"chunk {index} declares {length} bytes, past the "
                    f"{MAX_CHUNK_BYTES} bound"
                )
            payload = _read_exactly(handle, length, f"chunk {index} envelope")
            try:
                envelope = json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ContainerError(f"chunk {index} envelope is not JSON") from exc
            if not isinstance(envelope, dict):
                raise ContainerError(f"chunk {index} envelope is not an object")
            try:
                chunk = keyring.decrypt(
                    envelope,
                    table=_AAD_TABLE,
                    column=_AAD_COLUMN,
                    row_id=_row_id(
                        media_id, role, attempt_number, index, CONTAINER_VERSION
                    ),
                )
            except CryptoError as exc:
                # Wrong key, tampered tag, or a chunk that belongs to another
                # position, object or attempt. All are refusals, never a
                # partially-trusted chunk.
                raise ContainerError(f"chunk {index} did not authenticate: {exc}") from exc
            if len(chunk) > chunk_bytes:
                raise ContainerError(
                    f"chunk {index} decrypts to {len(chunk)} bytes, over the "
                    f"configured {chunk_bytes}"
                )
            yielded_bytes += len(chunk)
            if yielded_bytes > max_content_bytes:
                # The declared total was checked before the read, but a seal is
                # metadata and the bytes are the fact. A caller streaming into
                # memory has to be stopped on the running total, because the
                # whole-stream hash that would also catch a lying seal only
                # runs once everything has already been handed out.
                raise ContainerError(
                    f"container has passed the {max_content_bytes} ceiling "
                    f"after chunk {index}"
                )
            hasher.update(chunk)
            yield chunk

        trailing = handle.read(1)
        if trailing:
            raise ContainerError(
                "container has bytes past its sealed chunk count"
            )

    if hasher.chunk_count != record.chunk_count:
        raise ContainerError(
            f"read {hasher.chunk_count} chunks, seal declares {record.chunk_count}"
        )
    if hasher.total_bytes != record.total_bytes:
        raise ContainerError(
            f"read {hasher.total_bytes} bytes, seal declares {record.total_bytes}"
        )
    if hasher.hexdigest() != record.sha256:
        raise ContainerError("whole-stream hash does not match the seal record")


def _overhead_bound(chunk_count: int) -> int:
    """An upper bound on a container's non-plaintext bytes.

    Only used to refuse an obviously oversized file early; the authoritative
    checks are the per-chunk ones. Envelope JSON with a 4 MiB chunk costs a few
    kilobytes more than its chunk, and the header is fixed.
    """
    return _HEADER_BYTES + chunk_count * (_LENGTH_BYTES + MAX_CHUNK_BYTES + 4096)


def _read_exactly(handle: Any, count: int, what: str) -> bytes:
    data = handle.read(count)
    if len(data) != count:
        raise ContainerError(
            f"container ends inside {what}: wanted {count} bytes, got {len(data)}"
        )
    return data
