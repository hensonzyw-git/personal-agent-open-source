"""The five media endpoints' logic (design §4.2, §5.2, §5.4).

The HTTP layer is deliberately thin: it authenticates, parses the body under a
bound, and calls one of the functions here. Everything that decides -- what a
declaration may say, which transition a `PUT` is entitled to, what counts as a
mismatch, what the lock protects -- lives in this module so it can be tested
against a real database and a real store without a server in between.

Three readings this module had to fix, because the design leaves them open and
each one changes observable behaviour:

1. **The whole body is buffered, then written in one locked batch.** §4.2 says
   "网络接收在锁外使用有界缓冲，磁盘写在锁内" -- receive outside the lock under
   a bound, write inside it. This takes the simplest instance of that: the
   handler reads the body up to the configured ceiling, then one locked section
   re-reads the claim and writes. The alternative -- §4.2's per-batch re-read
   with :meth:`~personal_agent.media.store.MediaStore.staging_writer` -- buys
   nothing under option 1, where there is no decoder to overlap with and the
   bytes are the same bytes either way. What it costs is one image's worth of
   memory per in-flight upload, which §4.3's "并发上传数" bounds.

2. **A short body is an incomplete upload, not a mismatch.** §5.4 attaches
   "整对象拒绝" to "声明与实际不符", and a body that stops early does not match
   its declaration either. But a dropped connection is the ordinary reason a
   body stops early, and rejecting the *object* for it would mean a client on a
   bad network can never finish an upload it already has a target for. So the
   two are separated by direction: **more** bytes than declared is a
   declaration that is untrue and rejects the object; **fewer** leaves it
   `pending`, untouched, and the client retries the same target.

3. **A body that disagrees with its declared magic type rejects the object
   before any file is created.** §5.4's "探测失败一律拒绝且不派生文件", applied
   at the probe rather than after the write.

Every refusal here is fail-closed and none of them repairs, degrades or
re-encodes: §5.4's "失败拒绝：封口不完整、超限、探测不符一律拒绝，不降级、不修复、
不重试成另一个内容".
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from personal_agent.media.container import DEFAULT_CHUNK_BYTES
from personal_agent.media.deletion import mark_media_deleting
from personal_agent.media.lifecycle import (
    CHAT_IMAGE_PURPOSE,
    CompleteOutcome,
    MediaError,
    MediaLifecycleError,
    claim_upload,
    content_sha256,
    create_upload,
    declared_sha256,
    publish_upload,
    read_media_row,
    reject_upload,
    seal_record,
    seal_upload,
)
from personal_agent.media.locking import MediaLockError, media_locks
from personal_agent.media.probe import PROBE_BYTES, ProbeError, probe_header
from personal_agent.media.store import MediaStore
from personal_agent.storage.models import MediaObject
from personal_agent_core.crypto import KeyRing
from personal_agent_core.ids import new_id

#: Digest length for the declared sha256, in hex characters.
_SHA256_HEX: Final[int] = 64


class MediaBusyError(MediaError):
    """The stripe lock is held by someone else.

    A retryable refusal, and deliberately not a wait: §6 gives the reader that
    holds the stripe a bounded grace ("读者占锁超界只告警/延后"), and neither
    side of that is served by a `PUT` queueing behind a slow read. The client
    retries; the object is untouched.
    """


class MediaRejectedError(MediaError):
    """The object was terminally refused, and this call is what refused it."""


class MediaIncompleteUploadError(MediaError):
    """Fewer bytes arrived than the client declared. The object is untouched."""


class MediaNotFoundError(MediaError):
    """No such object *for this device*.

    Raised with the same message whether the id is unknown or simply belongs to
    somebody else, so the endpoint cannot be used to ask whether an id exists.
    """


class MediaNotReadyError(MediaError):
    """The object is real and not yet usable. Retryable, not terminal."""


class MediaGoneError(MediaError):
    """A tombstone. §5.2: "删除后返回墓碑，不复活"."""


@dataclass(frozen=True)
class MediaLimits:
    """The engineering ceilings of §4.3, as configuration rather than defaults.

    No field has a default here on purpose. §4.3 makes these versioned
    configuration, and a dataclass that invents a value for an unset one is a
    policy nobody chose -- the failure mode the design names for the budget
    coefficient in §8 applies to every one of these.
    """

    max_content_bytes: int
    max_dimension: int
    allowed_mimes: frozenset[str]
    #: How long a `pending` target may sit unconsumed before the reaper may
    #: expire it.
    target_ttl: timedelta
    #: How long one `PUT` owns the object. §5.2: the deadline bounds *that*
    #: attempt; past it a second writer takes over rather than waiting.
    claim_ttl: timedelta
    #: How long a `ready` image that no message bound may live.
    retention_ttl: timedelta
    #: §8's budget coefficient: how many declared pixels this deployment will
    #: concede to one token. A *larger* number prices images more cheaply, so
    #: the operator sets it to the ratio the pinned model's vision encoder is
    #: known to achieve and no more -- §8 asks for a "保守上界", and an
    #: optimistic coefficient is the one way this bound can fail open.
    #:
    #: Detail mode is not a field here because it is not a field on the wire in
    #: this version (§3.1's `image_ref` carries a `media_id` and nothing else):
    #: the mode is the deployment's own, so it is folded into this one number
    #: rather than modelled as a second axis nothing can vary.
    image_pixels_per_token: int

    def __post_init__(self) -> None:
        if self.image_pixels_per_token < 1:
            raise ValueError("image_pixels_per_token must be positive")

    def image_token_upper_bound(self, width: int, height: int) -> int:
        """§8's upper bound for one image, from the pixels the client declared.

        Rounded up, and never below one: a declared-but-tiny image that came
        out at zero tokens would be sent and charged nothing, which is the one
        answer a *bound* may not give.
        """
        if width < 1 or height < 1:
            raise ValueError("declared dimensions must be positive")
        return max(1, -(-width * height // self.image_pixels_per_token))


@dataclass(frozen=True)
class UploadDeclaration:
    """What a client says it is about to send (§5.2's create row).

    Every field is a claim, and the server treats all of them as claims: the
    digest is sealed and later compared against the *measured* one, and the
    dimensions are stored in columns named `declared_*` so nothing downstream
    can present them as measurements (§5.1).
    """

    mime: str
    size: int
    sha256: str
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class CreatedUpload:
    media_id: str
    state: str
    expires_at: datetime
    #: True when this call found an object the same key already created, and
    #: returned it instead of making a second one. A client recovering from a
    #: lost response cannot tell the two apart by the body -- which is the
    #: point -- but the field is in the response so the property is testable.
    replayed: bool


@dataclass(frozen=True)
class UploadReceipt:
    media_id: str
    state: str
    mime: str
    size: int


@dataclass(frozen=True)
class CompletedUpload:
    media_id: str
    state: str
    outcome: CompleteOutcome
    #: The server's measurement, never the client's declaration (§5.1).
    content_sha256: str | None
    mime: str | None
    size: int | None
    #: Declared, not measured. §5.1 requires them to be labelled that way
    #: wherever they travel, and the field names do the labelling.
    declared_width: int | None
    declared_height: int | None
    #: §5.2's "处理中返回可轮询状态与期限", and `None` for every other outcome.
    #: The deadline is the current claim's, so a client that polls until then
    #: is waiting exactly as long as the server has promised to hold the object
    #: for that writer -- past it §5.2 lets a new attempt take over, and a
    #: client that kept polling would be watching a writer that no longer
    #: exists.
    retry_at: datetime | None


@dataclass(frozen=True)
class FetchedMedia:
    media_id: str
    mime: str
    body: bytes


# --- create ---------------------------------------------------------------


def _require_sha256(value: object) -> str:
    if not isinstance(value, str) or len(value) != _SHA256_HEX:
        raise MediaError("sha256 must be 64 hex characters")
    if any(character not in "0123456789abcdefABCDEF" for character in value):
        raise MediaError("sha256 must be 64 hex characters")
    return value.lower()


def _validate_declaration(
    declaration: UploadDeclaration, limits: MediaLimits
) -> UploadDeclaration:
    """Refuse a declaration the server already knows it cannot honour.

    Checked here, before a row exists, because §4.3's "客户端声明值仍按上限校验
    （超出即拒绝）" is cheaper and more honest at the door than after an upload:
    a client told "no" now has not spent a megabyte finding out.
    """
    if declaration.mime not in limits.allowed_mimes:
        raise MediaError(f"unsupported media type {declaration.mime!r}")
    if not 0 < declaration.size <= limits.max_content_bytes:
        raise MediaError(
            f"size must be between 1 and {limits.max_content_bytes} bytes"
        )
    for dimension, label in (
        (declaration.width, "width"),
        (declaration.height, "height"),
    ):
        if dimension is None:
            continue
        if not 0 < dimension <= limits.max_dimension:
            raise MediaError(f"{label} must be between 1 and {limits.max_dimension}")
    return UploadDeclaration(
        mime=declaration.mime,
        size=declaration.size,
        sha256=_require_sha256(declaration.sha256),
        width=declaration.width,
        height=declaration.height,
    )


def _replayed_upload(
    session: Session,
    *,
    keyring: KeyRing,
    device_id: str,
    client_request_id: str,
    declaration: UploadDeclaration,
) -> CreatedUpload | None:
    """The object this key already created, if the declaration agrees.

    A key that names two different requests is refused rather than resolved.
    Silently returning the first object would answer a question the client did
    not ask -- it thinks it is describing a new image -- and the alternative of
    creating a second object under one key defeats the constraint the column
    exists to enforce.
    """
    row = session.execute(
        select(
            MediaObject.media_id,
            MediaObject.state,
            MediaObject.expires_at,
            MediaObject.declared_mime,
            MediaObject.declared_size,
            MediaObject.declared_width,
            MediaObject.declared_height,
        ).where(
            MediaObject.device_id == device_id,
            MediaObject.client_request_id == client_request_id,
        )
    ).one_or_none()
    if row is None:
        return None
    (
        media_id,
        state,
        expires_at,
        declared_mime,
        declared_size,
        declared_width,
        declared_height,
    ) = row
    if (
        declared_mime != declaration.mime
        or declared_size != declaration.size
        or declared_width != declaration.width
        or declared_height != declaration.height
        or declared_sha256(session, keyring=keyring, media_id=media_id)
        != declaration.sha256
    ):
        raise MediaError(
            "this idempotency key already named a different upload; "
            "a new upload needs a new key"
        )
    return CreatedUpload(
        media_id=media_id,
        state=state,
        expires_at=expires_at,
        replayed=True,
    )


def start_upload(
    session: Session,
    *,
    keyring: KeyRing,
    device_id: str,
    client_request_id: str | None,
    declaration: UploadDeclaration,
    limits: MediaLimits,
    now: datetime,
    media_id: str | None = None,
) -> CreatedUpload:
    """`POST /v1/media/uploads`: create a target, or return the one it made.

    The key is optional here and required by the route, which is the honest
    split: this function can create an object without one (a test, a restore),
    but a *request* cannot, because the alternative -- a client whose response
    was lost -- is a second `pending` object the user is never told about.
    """
    declaration = _validate_declaration(declaration, limits)
    if client_request_id is not None:
        replayed = _replayed_upload(
            session,
            keyring=keyring,
            device_id=device_id,
            client_request_id=client_request_id,
            declaration=declaration,
        )
        if replayed is not None:
            return replayed

    expires_at = now + limits.target_ttl
    created = create_upload(
        session,
        keyring=keyring,
        device_id=device_id,
        declared_mime=declaration.mime,
        declared_size=declaration.size,
        declared_sha256=declaration.sha256,
        now=now,
        expires_at=expires_at,
        declared_width=declaration.width,
        declared_height=declaration.height,
        media_id=media_id,
        client_request_id=client_request_id,
    )
    return CreatedUpload(
        media_id=created, state="pending", expires_at=expires_at, replayed=False
    )


# --- upload ---------------------------------------------------------------


def _chunks(body: bytes, size: int = DEFAULT_CHUNK_BYTES) -> Iterator[bytes]:
    """Split a buffered body into container-sized pieces.

    The container refuses a chunk above :data:`MAX_CHUNK_BYTES`, so a 30 MiB
    image cannot be sealed as one -- and it must not be, because the chunk is
    also the AAD unit the reader authenticates one index at a time.
    """
    for start in range(0, len(body), size):
        yield body[start : start + size]


def receive_upload(
    session: Session,
    *,
    store: MediaStore,
    keyring: KeyRing,
    media_id: str,
    device_id: str,
    body: bytes,
    limits: MediaLimits,
    now: datetime,
) -> UploadReceipt:
    """`PUT /v1/media/content/{id}`: claim the target and seal the bytes.

    Order, and each step is load-bearing:

    1. The body is already in memory under the handler's bound, so the length
       checks come first and cost nothing. **More** bytes than declared is
       §5.4's mismatch and rejects the object; **fewer** is an incomplete
       upload, and this returns without having touched the row at all.
    2. Any read transaction is ended *before* the lock, because §4.1 puts the
       lock ahead of the new transaction and §5.2 forbids a session that has
       read from upgrading its snapshot once another has committed.
    3. Under the stripe lock: claim (a CAS, so two concurrent `PUT`s cannot
       both proceed), probe the header against the declaration, seal, and
       commit.
    """
    row = read_media_row(session, media_id)
    if row is None or row.device_id != device_id:
        # The same message for both, so a caller cannot ask whether an id
        # exists without being entitled to it.
        raise MediaNotFoundError("no such media object")
    if row.state in ("deleted", "deleting", "reaping", "expired", "rejected"):
        raise MediaGoneError(f"this upload can no longer be completed ({row.state})")
    declared = _declaration_columns(session, media_id)
    if declared is None or declared[0] is None:
        raise MediaLifecycleError(f"{media_id} has no declaration to check against")
    declared_mime, declared_size = declared
    assert declared_size is not None

    if len(body) < declared_size:
        raise MediaIncompleteUploadError(
            f"received {len(body)} of {declared_size} declared bytes; "
            "the upload target is unchanged, so retry it"
        )
    if len(body) > declared_size:
        _reject(session, media_id=media_id, now=now)
        raise MediaRejectedError(
            f"received {len(body)} bytes but {declared_size} were declared"
        )

    session.rollback()
    owner_token = new_id()
    try:
        with media_locks(store.roots.root, [media_id], blocking=False):
            attempt = claim_upload(
                session,
                media_id=media_id,
                device_id=device_id,
                owner_token=owner_token,
                now=now,
                claim_deadline=now + limits.claim_ttl,
            )
            try:
                mime = probe_header(
                    body[:PROBE_BYTES],
                    declared_mime=declared_mime,
                    allowed_mimes=limits.allowed_mimes,
                )
            except ProbeError as exc:
                # §5.4: "探测失败一律拒绝且不派生文件". The probe runs before
                # `write_staging` so there is no file to clean up, and the
                # rejection is committed rather than rolled back -- a rollback
                # would hand the client back a `pending` target for bytes the
                # object has already been told are wrong.
                reject_upload(session, media_id=media_id, now=now)
                session.commit()
                raise MediaRejectedError(str(exc)) from exc
            seal = store.write_staging(
                media_id, attempt, _chunks(body), max_bytes=declared_size
            )
            seal_upload(
                session,
                keyring=keyring,
                media_id=media_id,
                attempt_number=attempt,
                owner_token=owner_token,
                seal=seal,
                actual_mime=mime,
                content_size=len(body),
                now=now,
            )
            session.commit()
    except MediaLockError as exc:
        session.rollback()
        raise MediaBusyError("the media store is busy; retry shortly") from exc
    except BaseException:
        session.rollback()
        raise
    return UploadReceipt(media_id=media_id, state="uploaded", mime=mime, size=len(body))


def _declaration_columns(
    session: Session, media_id: str
) -> tuple[str | None, int | None] | None:
    """The plaintext half of the declaration: the MIME and the size.

    Both stay in the clear (§5.1), which is what lets the upload path check
    them repeatedly without opening an envelope each time.
    """
    return session.execute(
        select(MediaObject.declared_mime, MediaObject.declared_size).where(
            MediaObject.media_id == media_id
        )
    ).one_or_none()


def _reject(session: Session, *, media_id: str, now: datetime) -> None:
    reject_upload(session, media_id=media_id, now=now)
    session.commit()


# --- complete -------------------------------------------------------------


def complete_upload(
    session: Session,
    *,
    store: MediaStore,
    keyring: KeyRing,
    media_id: str,
    device_id: str,
    limits: MediaLimits,
    now: datetime,
) -> CompletedUpload:
    """`POST /v1/media/uploads/{id}/complete`: verify, then publish.

    §5.2's "校验封口记录与声明一致后发布为持久图". The comparison is against the
    **sealed** record -- the server's measurement of what actually arrived --
    and not against the staged file, so a file tampered with after sealing
    fails the publish rather than publishing something the seal does not
    describe.

    The mismatch is terminal. A declared digest that the stream disagrees with
    is the one failure §5.1 keeps the sealed declaration for ("声明与实际不符即
    整对象拒绝"), and publishing anyway would produce a `ready` image whose
    declared metadata is false.
    """
    row = read_media_row(session, media_id)
    if row is None or row.device_id != device_id:
        raise MediaNotFoundError("no such media object")

    if row.state == "uploaded":
        attempt = row.current_attempt_number
        if attempt is None:
            raise MediaLifecycleError(f"{media_id} is uploaded with no attempt")
        seal = seal_record(
            session, keyring=keyring, media_id=media_id, attempt_number=attempt
        )
        if seal is None:
            raise MediaLifecycleError(f"{media_id} is uploaded without a seal record")
        declared = declared_sha256(session, keyring=keyring, media_id=media_id)
        declared_columns = _declaration_columns(session, media_id)
        declared_size = None if declared_columns is None else declared_columns[1]
        if seal.sha256 != declared or seal.total_bytes != declared_size:
            _reject(session, media_id=media_id, now=now)
            raise MediaRejectedError(
                "the uploaded bytes do not match the declaration "
                f"(declared {declared_size} bytes/{declared}, "
                f"sealed {seal.total_bytes} bytes/{seal.sha256})"
            )

    outcome = publish_upload(
        session,
        media_id=media_id,
        device_id=device_id,
        store=store,
        keyring=keyring,
        now=now,
    )
    session.commit()

    measured = content_sha256(session, keyring=keyring, media_id=media_id)
    actual = session.execute(
        select(MediaObject.actual_mime, MediaObject.content_size,
               MediaObject.declared_width, MediaObject.declared_height,
               MediaObject.state, MediaObject.claim_deadline).where(
            MediaObject.media_id == media_id
        )
    ).one()
    actual_mime, content_size, width, height, state, claim_deadline = actual
    return CompletedUpload(
        media_id=media_id,
        # Re-read rather than assumed: on `IN_PROGRESS`/`TOMBSTONED` the object
        # did not move, and reporting a state this call did not observe is how
        # a client ends up polling a value that was never true.
        state=state or row.state,
        outcome=outcome,
        content_sha256=measured,
        mime=actual_mime,
        size=content_size,
        declared_width=width,
        declared_height=height,
        # Only meaningful while an attempt is in flight; `None` on a published
        # or tombstoned object, where polling again is not what the client
        # should do next.
        retry_at=claim_deadline if outcome is CompleteOutcome.IN_PROGRESS else None,
    )


# --- fetch ----------------------------------------------------------------


def read_media(
    session: Session,
    *,
    store: MediaStore,
    keyring: KeyRing,
    media_id: str,
    device_id: str,
) -> FetchedMedia:
    """`GET /v1/media/{id}`: the persisted image, under §6's read protection.

    §6's order, with the half that applies outside a model turn: take the locks,
    check the row is still live, read the authenticated bytes **while holding
    the stripe**, then release. The read is inside the lock rather than after it
    so a concurrent reap cannot clear the file between the check and the read
    -- which is the whole reason §6 refuses to force-delete past a reader.

    What this does *not* do is §6's "登记本次使用": recording a use relation
    needs the operation and event the image is being attached to, and this
    endpoint has neither. That registration belongs to the anchor path, which
    runs inside a request's write transaction.
    """
    row = read_media_row(session, media_id)
    if row is None or row.device_id != device_id:
        raise MediaNotFoundError("no such media object")
    if row.state in ("deleted", "deleting", "reaping", "expired", "rejected"):
        # §5.2: a tombstone is not an absence. Saying so is what lets a client
        # tell "you deleted this" from "this never existed", which is the only
        # way it can stop retrying.
        raise MediaGoneError("this image was deleted")
    if row.state not in ("ready", "bound"):
        raise MediaNotReadyError(f"this image is not ready ({row.state})")
    attempt = row.current_attempt_number
    if attempt is None:
        raise MediaLifecycleError(f"{media_id} is {row.state} with no attempt")

    session.rollback()
    try:
        with media_locks(store.roots.root, [media_id], blocking=False):
            # Re-read inside the lock: between the check above and here another
            # writer may have decided a deletion, and §6's protection is worth
            # nothing if it protects a row that has already changed.
            current = read_media_row(session, media_id)
            if current is None:
                raise MediaNotFoundError("no such media object")
            if current.state not in ("ready", "bound"):
                raise MediaGoneError("this image was deleted")
            seal = seal_record(
                session, keyring=keyring, media_id=media_id, attempt_number=attempt
            )
            body = store.read_final(media_id, attempt, seal)
    except MediaLockError as exc:
        raise MediaBusyError("the media store is busy; retry shortly") from exc
    finally:
        session.rollback()

    mime = session.execute(
        select(MediaObject.actual_mime).where(MediaObject.media_id == media_id)
    ).scalar_one_or_none()
    return FetchedMedia(media_id=media_id, mime=mime or "application/octet-stream", body=body)


# --- delete ---------------------------------------------------------------


def delete_media(
    session: Session,
    *,
    keyring: KeyRing,
    media_id: str,
    device_id: str,
    now: datetime,
) -> bool:
    """`DELETE /v1/media/{id}`: decide the deletion, in one transaction.

    §5.2: "幂等删除标记与 manifest 同事务；返回删除已受理，物理完成另查". This
    returns whether *this* call decided it. Reporting `False` is a success for
    a retry and for a replay, so the route answers 200 either way and puts the
    distinction in the body -- a 409 on a second delete would be a lie about
    the state the user asked for.
    """
    row = read_media_row(session, media_id)
    if row is None or row.device_id != device_id:
        raise MediaNotFoundError("no such media object")
    decided = mark_media_deleting(session, media_id=media_id, keyring=keyring, now=now)
    session.commit()
    return decided


def state_of(session: Session, media_id: str) -> str | None:
    row = read_media_row(session, media_id)
    return None if row is None else row.state


#: Re-exported so the route layer names the same vocabulary this module does
#: rather than re-listing the MIME set or the purpose string.
__all__ = [
    "CHAT_IMAGE_PURPOSE",
    "CompleteOutcome",
    "CompletedUpload",
    "CreatedUpload",
    "FetchedMedia",
    "MediaBusyError",
    "MediaError",
    "MediaGoneError",
    "MediaIncompleteUploadError",
    "MediaLifecycleError",
    "MediaLimits",
    "MediaNotFoundError",
    "MediaNotReadyError",
    "MediaRejectedError",
    "UploadDeclaration",
    "UploadReceipt",
    "complete_upload",
    "delete_media",
    "read_media",
    "receive_upload",
    "start_upload",
    "state_of",
]
