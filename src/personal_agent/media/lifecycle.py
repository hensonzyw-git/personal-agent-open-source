"""The media object state machine: create, claim, seal, publish (design §5.2).

Under option 1 this machine has no processing step. The bytes a `PUT` seals
*are* the persisted image, so `complete` recomputes nothing: it verifies the
seal record and publishes. Every state below names upload progress and nothing
else.

    pending -> uploading -> uploaded -> ready -> bound
                    |            |
                    |            +-- complete publishes and CASes to ready
                    +-- an expired claim is taken over by a new attempt

Each step is a compare-and-swap against the state it read, for the reason
§5.2 gives: two writers must not both believe they own the object, and a lost
`PUT` response must not turn into a second published image. The real mutual
exclusion is still §4.1's file lock; these CASes are what make the *database*
agree with it rather than a second lease system beside it.

Sealed columns. Three fields on `media_objects` carry personal data and are
stored as envelopes: the client's declared digest, the server's measured one,
and the storage reference. Their AAD binds the row they belong to
(``media_objects``/``<column>``/``media_id``), following the convention every
other sealed column in this schema uses, so an envelope moved to another row
or another table refuses to open rather than reporting someone else's digest.
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from personal_agent.media.container import SealRecord
from personal_agent.media.store import MediaStore
from personal_agent.storage.models import MediaAttempt, MediaObject
from personal_agent_core.crypto import KeyRing
from personal_agent_core.ids import new_id, require_uuid4
from personal_agent_core.manifest import canonical_json

#: AAD for the object's sealed columns. The row id is the media id, which is
#: the table's primary key -- an envelope can only be opened from the row that
#: sealed it.
MEDIA_TABLE = "media_objects"
CONTENT_SHA_COLUMN = "encrypted_content_sha256"
DECLARED_SHA_COLUMN = "encrypted_declared_sha256"
STORAGE_REF_COLUMN = "encrypted_storage_ref"

#: AAD for an attempt's seal record, bound to the attempt row.
ATTEMPT_TABLE = "media_attempts"
SEAL_RECORD_COLUMN = "encrypted_seal_record"

#: The only purpose this round produces (§5.1). A second one must parameterise
#: the AAD rather than reuse these values, or chunks would be readable across
#: object kinds.
CHAT_IMAGE_PURPOSE = "chat_image"


class MediaError(RuntimeError):
    """The base of every refusal the media domain raises at a client.

    Defined here rather than in the endpoint layer because the state machine is
    where most refusals originate, and a route layer that had to catch one type
    per module would eventually miss one -- catching the base and mapping it to
    a status is the whole point. Subclasses in
    :mod:`personal_agent.media.uploads` carry the request-level distinctions
    (busy, rejected, incomplete); this class carries only "the request was
    refused, and the message is safe to show the user".
    """


class MediaLifecycleError(MediaError):
    """A transition was refused. The object is unchanged and the caller may retry."""


class StaleMediaObjectError(MediaLifecycleError):
    """The object moved between this call's read and its write.

    Raised rather than retried here: the caller is normally inside
    :func:`personal_agent_core.sqlite.run_write_transaction`, which re-runs the
    whole unit against fresh state.
    """


class MediaClaimConflictError(MediaLifecycleError):
    """Another writer holds a live claim on this object.

    §5.2: the deadline bounds *that attempt*, and an expired one may be taken
    over -- but a live one is a refusal. Handing the object to a second writer
    while the first is still streaming would give one media id two staging
    files and no rule about which one wins.
    """


@dataclass(frozen=True)
class MediaRow:
    """The plaintext part of an object's state, as read for a decision."""

    media_id: str
    device_id: str
    purpose: str
    state: str
    state_version: int
    current_attempt_number: int | None
    owner_token: str | None
    expires_at: datetime | None
    claim_deadline: datetime | None


def read_media_row(session: Session, media_id: str) -> MediaRow | None:
    """Read state with a plain ``SELECT`` (CLAUDE.md §5.2).

    Not the ORM identity map: every one of these reads is followed by a
    compare-and-swap that must see the database's current value, and the
    identity map would hand back a version another session has already
    superseded.
    """
    row = session.execute(
        select(
            MediaObject.media_id,
            MediaObject.device_id,
            MediaObject.purpose,
            MediaObject.state,
            MediaObject.state_version,
            MediaObject.current_attempt_number,
            MediaObject.owner_token,
            MediaObject.expires_at,
            MediaObject.claim_deadline,
        ).where(MediaObject.media_id == media_id)
    ).one_or_none()
    if row is None:
        return None
    return MediaRow(*row)


def _cas_state(
    session: Session,
    *,
    media_id: str,
    from_states: tuple[str, ...],
    version: int,
    to_state: str,
    now: datetime,
    **values,
) -> None:
    """Move one object out of ``from_states`` at exactly ``version``, or refuse.

    ``from_states`` is part of the WHERE clause rather than a branch above, so
    a state this code has not been taught to move fails closed the same way a
    lost race does. The column's CHECK constraint enumerates the same
    vocabulary, which makes the guard unreachable while the two agree -- and
    that is the point: a later migration adding a state must not silently make
    it traversable.
    """
    result = session.execute(
        update(MediaObject)
        .where(
            MediaObject.media_id == media_id,
            MediaObject.state_version == version,
            MediaObject.state.in_(from_states),
        )
        .values(state=to_state, state_version=version + 1, updated_at=now, **values)
    )
    if result.rowcount != 1:
        raise StaleMediaObjectError(
            f"{media_id} is no longer {from_states}/v{version}; another writer moved it"
        )


# --- create ---------------------------------------------------------------


def create_upload(
    session: Session,
    *,
    keyring: KeyRing,
    device_id: str,
    declared_mime: str,
    declared_size: int,
    declared_sha256: str | None,
    now: datetime,
    expires_at: datetime,
    declared_width: int | None = None,
    declared_height: int | None = None,
    media_id: str | None = None,
    purpose: str = CHAT_IMAGE_PURPOSE,
    client_request_id: str | None = None,
) -> str:
    """Create a ``pending`` object and return its media id.

    The declared digest is sealed, not stored in the clear: a photo's digest is
    a fingerprint of that photo, and this table outlives the file it names. The
    declared size and MIME stay plaintext -- they are what the upload path
    checks cheaply and repeatedly, and neither is personal data (§5.1).

    ``media_id`` is accepted for a test or a caller that must control the id,
    but a real request lets it default: §5.4's argument for random ids is that
    a guessed path must not be a way to reach someone else's file.
    """
    if purpose != CHAT_IMAGE_PURPOSE:
        # The AAD and the chunk role are both fixed to this purpose; a second
        # one has to change them together or chunks would be readable across
        # object kinds.
        raise MediaLifecycleError(f"unsupported media purpose {purpose!r}")
    media_id = require_uuid4(media_id) if media_id is not None else new_id()
    session.execute(
        insert(MediaObject).values(
            media_id=media_id,
            device_id=device_id,
            client_request_id=client_request_id,
            purpose=purpose,
            retention_class="timeline_media",
            state="pending",
            state_version=1,
            expires_at=expires_at,
            declared_mime=declared_mime,
            declared_size=declared_size,
            declared_width=declared_width,
            declared_height=declared_height,
            encrypted_declared_sha256=(
                _seal(keyring, declared_sha256, media_id, DECLARED_SHA_COLUMN)
                if declared_sha256 is not None
                else None
            ),
            created_at=now,
            updated_at=now,
        )
    )
    return media_id


def _seal(
    keyring: KeyRing, value: str, media_id: str, column: str
) -> dict[str, object]:
    return keyring.encrypt(
        value.encode("utf-8"),
        table=MEDIA_TABLE,
        column=column,
        row_id=media_id,
    )


def _open(keyring: KeyRing, envelope: object, media_id: str, column: str) -> str:
    plaintext = keyring.decrypt(
        envelope, table=MEDIA_TABLE, column=column, row_id=media_id
    )
    return plaintext.decode("utf-8")


# --- claim ----------------------------------------------------------------


def claim_upload(
    session: Session,
    *,
    media_id: str,
    device_id: str,
    owner_token: str,
    now: datetime,
    claim_deadline: datetime,
) -> int:
    """Consume the one-shot target and return the attempt number.

    §5.2: a `PUT` consumes the target by CAS-ing ``pending`` to ``uploading``
    with an owner and an attempt number. A second `PUT` against a *live* claim
    is refused; against an *expired* one it takes over with a fresh attempt
    number, because the response to the first `PUT` may simply have been lost
    and §5.2's answer to that is "过期/不完整明确失败并新建上传", not a retry of
    the same attempt.
    """
    row = read_media_row(session, media_id)
    if row is None:
        raise MediaLifecycleError(f"no media object {media_id}")
    if row.device_id != device_id:
        # Same message as a missing object: whether an id exists is not
        # something an unauthorised caller should be able to ask.
        raise MediaLifecycleError(f"no media object {media_id}")

    if row.state == "pending":
        attempt_number = 1
    elif row.state == "uploading":
        # A missing deadline is treated as live, not as expired: the claim row
        # is written by `claim_upload` in the same transaction as the state, so
        # an absent one means something a caller did not write -- and taking
        # over on the strength of an absent deadline would hand the object to a
        # second writer precisely when nothing is known about the first.
        if row.claim_deadline is None or row.claim_deadline > now:
            raise MediaClaimConflictError(
                f"{media_id} already has a live upload claim"
            )
        attempt_number = (row.current_attempt_number or 0) + 1
    else:
        raise MediaLifecycleError(f"{media_id} is {row.state}; it cannot be uploaded")

    attempt_id = new_id()
    session.execute(
        insert(MediaAttempt).values(
            attempt_id=attempt_id,
            media_id=media_id,
            attempt_number=attempt_number,
            state="claimed",
            state_version=1,
            owner_token=owner_token,
            claim_deadline=claim_deadline,
            created_at=now,
            updated_at=now,
        )
    )
    _cas_state(
        session,
        media_id=media_id,
        from_states=(row.state,),
        version=row.state_version,
        to_state="uploading",
        now=now,
        owner_token=owner_token,
        current_attempt_number=attempt_number,
        claim_deadline=claim_deadline,
    )
    return attempt_number


# --- seal -----------------------------------------------------------------


def seal_upload(
    session: Session,
    *,
    keyring: KeyRing,
    media_id: str,
    attempt_number: int,
    owner_token: str,
    seal: SealRecord,
    actual_mime: str,
    content_size: int,
    now: datetime,
) -> None:
    """Record the seal record and move ``uploading`` to ``uploaded``.

    The seal record is what a later recovery compares a published file against,
    so it is written before the state that claims the bytes are complete: an
    ``uploaded`` object whose attempt has no record would look adoptable and be
    unverifiable, which is the one combination the attempt table's CHECK
    constraint also refuses.

    The record is **sealed**, because its whole-stream hash is a fingerprint of
    the image: §5.1 keeps hashes and storage references in envelopes, and this
    column is one of them. The AAD binds the attempt row, so a record copied to
    another attempt refuses to open rather than adopting someone else's bytes.
    """
    row = read_media_row(session, media_id)
    if row is None:
        raise MediaLifecycleError(f"no media object {media_id}")
    if row.state != "uploading":
        raise MediaLifecycleError(f"{media_id} is {row.state}; it has no upload to seal")
    if row.owner_token != owner_token:
        raise MediaClaimConflictError(f"{media_id} is claimed by another writer")

    attempt = session.execute(
        select(MediaAttempt.attempt_id, MediaAttempt.state).where(
            MediaAttempt.media_id == media_id,
            MediaAttempt.attempt_number == attempt_number,
        )
    ).one_or_none()
    if attempt is None:
        raise MediaLifecycleError(f"{media_id} has no attempt {attempt_number}")
    attempt_id, attempt_state = attempt
    if attempt_state not in ("claimed",):
        raise MediaLifecycleError(
            f"attempt {attempt_number} of {media_id} is {attempt_state}"
        )

    result = session.execute(
        update(MediaAttempt)
        .where(
            MediaAttempt.attempt_id == attempt_id,
            MediaAttempt.state == "claimed",
        )
        .values(
            state="sealed",
            state_version=MediaAttempt.state_version + 1,
            encrypted_seal_record=keyring.encrypt(
                canonical_json(seal.to_dict()).encode("utf-8"),
                table=ATTEMPT_TABLE,
                column=SEAL_RECORD_COLUMN,
                row_id=attempt_id,
            ),
            updated_at=now,
            sealed_at=now,
        )
    )
    if result.rowcount != 1:
        raise StaleMediaObjectError(f"attempt {attempt_number} of {media_id} moved")

    _cas_state(
        session,
        media_id=media_id,
        from_states=("uploading",),
        version=row.state_version,
        to_state="uploaded",
        now=now,
        actual_mime=actual_mime,
        content_size=content_size,
        uploaded_at=now,
    )


# --- publish --------------------------------------------------------------


class CompleteOutcome(enum.Enum):
    """What ``complete`` found, in the shapes a client must tell apart."""

    PUBLISHED = "published"
    #: The object is already ``ready``/``bound``. §5.2: "complete 在 ready/bound
    #: 返回同一完成结果" -- a lost response must be recoverable by re-asking.
    ALREADY_READY = "already_ready"
    #: A deletion was decided. §5.2: "删除后返回墓碑，不复活".
    TOMBSTONED = "tombstoned"
    #: ``uploading``: this attempt has not sealed. §5.2: report it as in
    #: progress rather than as a bad image, and let the client poll.
    IN_PROGRESS = "in_progress"


def publish_upload(
    session: Session,
    *,
    media_id: str,
    device_id: str,
    store: MediaStore,
    keyring: KeyRing,
    now: datetime,
    owner_token: str | None = None,
) -> CompleteOutcome:
    """Publish the sealed staging file, CAS ``uploaded`` to ``ready``.

    §5.3's order is file first, database second: the final file is installed
    without overwriting, and only then does the row claim to be ``ready``. A
    crash in between leaves an unreferenced final file, which the recovery path
    adopts by re-verifying it against the seal record -- the reverse order
    would leave a ``ready`` row with nothing behind it, and every reader would
    find out one at a time.

    **This function does not commit.** The caller owns the transaction, because
    §5.1 requires the publish and the CAS to be one unit: the file is already
    on disk by then, so committing the row is the only thing left that can
    fail, and it must not be deferred past the caller's own commit.
    """
    row = read_media_row(session, media_id)
    if row is None:
        raise MediaLifecycleError(f"no media object {media_id}")
    if row.device_id != device_id:
        raise MediaLifecycleError(f"no media object {media_id}")
    if row.state in ("ready", "bound"):
        return CompleteOutcome.ALREADY_READY
    if row.state in ("deleting", "reaping"):
        return CompleteOutcome.TOMBSTONED
    if row.state == "deleted" or row.state in ("expired", "rejected"):
        return CompleteOutcome.TOMBSTONED
    if row.state != "uploaded":
        # `pending` and `uploading`: no sealed bytes exist yet.
        return CompleteOutcome.IN_PROGRESS
    if owner_token is not None and row.owner_token != owner_token:
        raise MediaClaimConflictError(f"{media_id} is claimed by another writer")
    if row.current_attempt_number is None:
        raise MediaLifecycleError(f"{media_id} is uploaded with no attempt")

    attempt_id, seal_record, staging_ref = session.execute(
        select(
            MediaAttempt.attempt_id,
            MediaAttempt.encrypted_seal_record,
            MediaAttempt.encrypted_staging_ref,
        ).where(
            MediaAttempt.media_id == media_id,
            MediaAttempt.attempt_number == row.current_attempt_number,
        )
    ).one()
    if seal_record is None:
        raise MediaLifecycleError(f"{media_id} is uploaded without a seal record")
    seal = SealRecord.from_dict(
        json.loads(
            keyring.decrypt(
                seal_record,
                table=ATTEMPT_TABLE,
                column=SEAL_RECORD_COLUMN,
                row_id=attempt_id,
            ).decode("utf-8")
        )
    )

    # §5.3's order: the file first, the row second. `publish` is
    # non-overwriting by construction, so a final file that already exists --
    # evidence of an earlier attempt that crashed after installing it -- stops
    # here instead of being replaced.
    store.publish(media_id, row.current_attempt_number)

    session.execute(
        update(MediaAttempt)
        .where(MediaAttempt.attempt_id == attempt_id)
        .values(state="published", updated_at=now)
    )
    _cas_state(
        session,
        media_id=media_id,
        from_states=("uploaded",),
        version=row.state_version,
        to_state="ready",
        now=now,
        ready_at=now,
        content_size=seal.total_bytes,
        encrypted_content_sha256=_seal(keyring, seal.sha256, media_id, CONTENT_SHA_COLUMN),
        encrypted_storage_ref=_seal(
            keyring, str(store.final_path(media_id)), media_id, STORAGE_REF_COLUMN
        ),
        owner_token=None,
        claim_deadline=None,
    )
    return CompleteOutcome.PUBLISHED


class MediaDeclarationMismatchError(MediaLifecycleError):
    """What arrived is not what the client declared.

    §5.4: "声明 MIME/magic、size/hash、Content-Length 与实测不符，整对象拒绝".
    The refusal is terminal rather than a retryable failure on purpose -- a
    client that declared a 4 MiB JPEG and sent 3 MiB of something else has not
    had a bad network, it has told the server something untrue, and the object
    it named must not later become a `ready` image whose metadata still carries
    the untrue declaration.
    """


def reject_upload(session: Session, *, media_id: str, now: datetime) -> bool:
    """Terminally refuse one object. Returns whether this call refused it.

    ``rejected`` is a tombstone like ``deleted``: the id is never reused and the
    row outlives whatever bytes were staged. Reached from every state an upload
    can be interrupted in, because the mismatch may be found at the header probe
    (a declared MIME the bytes disagree with) or at `complete` (a declared hash
    the sealed stream disagrees with), and neither is discoverable before the
    other.

    A no-op on an object that is already terminal. Unlike
    :func:`~personal_agent.media.deletion.mark_media_deleting` it writes no
    manifest entry: nothing was ever published for a restore to bring back, so
    an entry here would only add a row to the one list that has to replay
    correctly.
    """
    row = read_media_row(session, media_id)
    if row is None:
        return False
    if row.state in ("rejected", "deleted", "deleting", "reaping", "expired"):
        return False
    _cas_state(
        session,
        media_id=media_id,
        from_states=(row.state,),
        version=row.state_version,
        to_state="rejected",
        now=now,
        # The claim is released with the state, so a later `PUT` is refused by
        # the *state* rather than by a live claim that would expire into a
        # takeover of a terminal object.
        owner_token=None,
        claim_deadline=None,
    )
    return True


def seal_record(
    session: Session,
    *,
    keyring: KeyRing,
    media_id: str,
    attempt_number: int,
) -> SealRecord | None:
    """The sealed record of one attempt, or ``None`` if it has not sealed.

    The record a `GET` authenticates the persisted file against, and the one
    `complete` compares the declaration to. Both callers need the same value, so
    it is read here once rather than twice with two chances to pick a different
    attempt.
    """
    row = session.execute(
        select(MediaAttempt.attempt_id, MediaAttempt.encrypted_seal_record).where(
            MediaAttempt.media_id == media_id,
            MediaAttempt.attempt_number == attempt_number,
        )
    ).one_or_none()
    if row is None:
        return None
    attempt_id, envelope = row
    if envelope is None:
        return None
    return SealRecord.from_dict(
        json.loads(
            keyring.decrypt(
                envelope,
                table=ATTEMPT_TABLE,
                column=SEAL_RECORD_COLUMN,
                row_id=attempt_id,
            ).decode("utf-8")
        )
    )


def declared_sha256(session: Session, *, keyring: KeyRing, media_id: str) -> str | None:
    """The client's declared digest, or ``None`` if it declared none.

    The value every "声明与实际不符即整对象拒绝" check compares against, and the
    reason it is stored sealed: it is a fingerprint of the image, and this row
    outlives the file.
    """
    envelope = session.execute(
        select(MediaObject.encrypted_declared_sha256).where(
            MediaObject.media_id == media_id
        )
    ).scalar_one_or_none()
    if envelope is None:
        return None
    return _open(keyring, envelope, media_id, DECLARED_SHA_COLUMN)


def content_sha256(session: Session, *, keyring: KeyRing, media_id: str) -> str | None:
    """The server-measured digest, or ``None`` if this object never published.

    Sealed, so this is the only way to read it -- and the only value the request
    fingerprint may use (§5.1: the digest is the server's measurement, never the
    client's declaration).
    """
    envelope = session.execute(
        select(MediaObject.encrypted_content_sha256).where(
            MediaObject.media_id == media_id
        )
    ).scalar_one_or_none()
    if envelope is None:
        return None
    return _open(keyring, envelope, media_id, CONTENT_SHA_COLUMN)
