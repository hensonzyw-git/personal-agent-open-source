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
    """Seal `chunks` into `path` and return the record that commits to them.

    Writes with `O_CREAT | O_EXCL`: the staging path for an attempt is created
    once and never reopened by a later writer, which is how §4.2's "an expired
    writer cannot reopen or append its own staging" is enforced at the file
    layer rather than only in the state machine.

    `fsync` happens here because this is the seal point §5.3 step 1 names: once
    this returns, the bytes are complete and durable, and the caller's job is
    the directory sync and the database compare-and-swap that publish them.
    """
    media_id = _require_media_id(media_id)
    role = _require_role(role)
    if attempt_number < 1:
        raise ContainerError("attempt number must be >= 1")
    if not 0 < chunk_bytes <= MAX_CHUNK_BYTES:
        raise ContainerError(
            f"chunk size {chunk_bytes} outside 1..{MAX_CHUNK_BYTES}"
        )

    hasher = ChunkHasher()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise ContainerError(f"staging already exists at {path}") from exc
    except OSError as exc:
        raise ContainerError(f"cannot create staging at {path}: {exc}") from exc

    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(CONTAINER_MAGIC)
            handle.write(bytes([CONTAINER_VERSION]))
            for index, chunk in enumerate(chunks):
                if len(chunk) > chunk_bytes:
                    raise ContainerError(
                        f"chunk {index} is {len(chunk)} bytes, over the "
                        f"configured {chunk_bytes}"
                    )
                handle.write(
                    _seal_chunk(
                        chunk,
                        keyring=keyring,
                        media_id=media_id,
                        role=role,
                        attempt_number=attempt_number,
                        index=index,
                    )
                )
                hasher.update(chunk)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        # A half-written staging file is not left behind for anything to adopt;
        # the caller records the attempt as abandoned, and the reaper would
        # remove this anyway, but failing closed means removing it now.
        path.unlink(missing_ok=True)
        raise

    if hasher.chunk_count == 0:
        # `chunks` was empty. The container is still well defined: one sealed
        # empty chunk, so "empty upload" has a shape a reader can reject on its
        # own terms rather than as a missing file.
        os.unlink(path)
        return write_container(
            path,
            [b""],
            keyring=keyring,
            media_id=media_id,
            attempt_number=attempt_number,
            role=role,
            chunk_bytes=chunk_bytes,
        )

    return SealRecord(
        chunk_count=hasher.chunk_count,
        total_bytes=hasher.total_bytes,
        sha256=hasher.hexdigest(),
    )


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
        # asked for and the ceiling already forbids.
        raise ContainerError(
            f"container is {info.st_size} bytes, past the {max_content_bytes} "
            "ceiling for its content"
        )

    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ContainerError(f"cannot open container at {path}: {exc}") from exc

    hasher = ChunkHasher()
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
