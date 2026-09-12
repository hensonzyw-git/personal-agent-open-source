"""§3.2's first anchoring: the one place an `image_ref` becomes a use of an image.

A chat request that names an image is anchored in two steps, and the split is
the design's rather than this module's:

- **Resolve** reads each referenced object and decides whether this device may
  use it at all: it exists, it belongs to the requesting device, it was uploaded
  for chat, it is not terminal or already on its way to deletion, it is `ready`
  or `bound`, its *measured* type is one this deployment allows, and it carries
  the server's measured digest. Resolving happens before the request
  fingerprint exists, because §3.2's fingerprint input is the ordered
  `(media_id, content_sha256)` and the digest is a server measurement -- the
  client never supplies one.
- **Bind** writes what resolve proved: `ready -> bound`, and one
  `media_bindings` row per image. It runs after `append_event` has returned the
  event id, because §5.1's binding is a *use relation* and therefore names the
  message; the read and the write cannot happen at the same moment.

Three readings this module had to fix, because §3.2 lists what to verify
without saying what each check does with an object that fails it:

1. **A `bound` object is neither free nor gone.** §5.1 says "origin 唯一；合法
   澄清链/受控安全重试可登记 reuse", so a second use is legal only along a
   lineage the server already recorded. The reuse source travels in as an
   argument from the caller that knows it rather than being inferred from the
   request's shape, and a `bound` object with no such source refuses the whole
   request: never silently rebound, never a second origin.
2. **"未过期" is checked where an expiry exists.** `expires_at` is armed by
   `create` for the unbound upload phase and is *not* re-armed by
   `publish_upload`, so on a `ready` or `bound` object it holds a value that
   stopped meaning anything the moment the object published. The ready-phase
   bound is §4.3's `retention_ttl`, which the reaper applies. Enforcing the
   leftover value here would expire images that are still perfectly usable --
   see the round's flagged readings.
3. **A refused image refuses the request.** §3.2 anchors "全部" of a request's
   images or none. A message naming an image it may not use is not
   half-answerable: the model would answer a question the user did not ask.

Nothing here touches the blob. State, type and digest live in plaintext or
sealed *columns*, so anchoring needs no file store -- and, more to the point,
§3.2's replay rule can settle a retry without reading live media at all.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from personal_agent.api.chat_parts import ImageRefPart, Parts
from personal_agent.media.lifecycle import (
    CHAT_IMAGE_PURPOSE,
    DELETION_STATES,
    USABLE_STATES,
    MediaError,
    bind_upload,
    content_sha256,
    read_media_row,
)
from personal_agent.media.uploads import (
    MediaGoneError,
    MediaLimits,
    MediaNotFoundError,
    MediaNotReadyError,
)
from personal_agent.storage.models import (
    MEDIA_OBJECT_TERMINAL_STATES,
    MediaBinding,
    MediaObject,
)
from personal_agent_core.crypto import KeyRing
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.ids import new_id

# The state vocabulary is the state machine's, not this module's: §6's read
# re-checks the same two sets on the same object seconds later, and two copies
# of "which states may be used" would be two answers. See `media.lifecycle`.


@dataclass(frozen=True)
class ResolvedImage:
    """One `image_ref`, proven usable, carrying what the write step needs.

    `ordinal` is the part's position in the ordered `parts` array -- §5.1 makes
    it part of the binding, and the wire shape fixes it at 0 or 1 because text
    comes first.
    """

    media_id: str
    content_sha256: str
    #: The state read while resolving. It decides the role: a `ready` object has
    #: never been used, so this message is its origin, while a `bound` one can
    #: only be a reuse of a use the server already recorded.
    state: str
    #: The version observed at resolution, which the origin's compare-and-swap
    #: requires unchanged -- §3.2's "锁内行版本必须仍与预校验一致".
    state_version: int
    ordinal: int


def resolve_chat_images(
    session: Session,
    *,
    keyring: KeyRing,
    device_id: str,
    parts: Parts,
    limits: MediaLimits,
) -> tuple[ResolvedImage, ...]:
    """Prove every `image_ref` in `parts` usable, or refuse the request.

    Returns them in the parts' own order, which is the order their bindings are
    written in.
    """
    resolved: list[ResolvedImage] = []
    for ordinal, part in enumerate(parts):
        if not isinstance(part, ImageRefPart):
            continue
        resolved.append(
            _resolve_one(
                session,
                keyring=keyring,
                device_id=device_id,
                media_id=part.media_id,
                limits=limits,
                ordinal=ordinal,
            )
        )
    return tuple(resolved)


def resolved_parts(parts: Parts, images: Sequence[ResolvedImage]) -> Parts:
    """`parts` with the server's measured digests filled in.

    This is §3.2's fingerprint input. The sealed payload keeps the *unresolved*
    parts (`seal_chat_parts` refuses a digest), so the resolved copy is a
    separate value rather than a mutation -- a request's wire shape and its
    fingerprint input are two different things and stay two.
    """
    digest_by_ordinal = {image.ordinal: image.content_sha256 for image in images}
    return tuple(
        ImageRefPart(part.media_id, digest_by_ordinal[ordinal])
        if isinstance(part, ImageRefPart)
        else part
        for ordinal, part in enumerate(parts)
    )


def bind_chat_images(
    session: Session,
    *,
    images: Sequence[ResolvedImage],
    event_id: str,
    operation_id: str,
    reuse_lineage: Sequence[str],
    now: datetime,
) -> None:
    """Record each image's use by this message: binding row, then state move.

    `reuse_lineage` names the operations whose use of an image this message may
    re-establish -- the clarification source, or a controlled safety retry.
    Each is verified against the recorded bindings rather than trusted: §3.2
    makes the server check the lineage, this device and the actual authorization
    before registering a reuse.
    """
    for image in images:
        if image.state == "bound":
            # A declared lineage is not evidence; a recorded use is. Verified
            # against `media_bindings`, and against the *object* as well, so a
            # source that used some other image justifies nothing here.
            if not _reuse_is_recorded(session, image.media_id, reuse_lineage):
                raise MediaError(
                    "this image belongs to an earlier message and may only be "
                    "reused through the exchange it was sent in"
                )
            role = "reuse"
            source_operation_id = _reuse_source(session, image.media_id, reuse_lineage)
        else:
            role = "origin"
            source_operation_id = None
            bind_upload(
                session,
                media_id=image.media_id,
                version=image.state_version,
                now=now,
            )
        session.add(
            MediaBinding(
                binding_id=new_id(),
                media_id=image.media_id,
                event_id=event_id,
                operation_id=operation_id,
                role=role,
                ordinal=image.ordinal,
                source_operation_id=source_operation_id,
                created_at=now,
            )
        )


def _reuse_is_recorded(
    session: Session, media_id: str, reuse_lineage: Sequence[str]
) -> bool:
    return _reuse_source(session, media_id, reuse_lineage) is not None


def _reuse_source(
    session: Session, media_id: str, reuse_lineage: Sequence[str]
) -> str | None:
    """The first candidate operation that is on record as having used this image.

    A plain ``SELECT``, like every other decision read here: the identity map's
    view of a binding another worker just wrote is not evidence of anything.
    """
    if not reuse_lineage:
        return None
    return session.execute(
        select(MediaBinding.operation_id)
        .where(
            MediaBinding.media_id == media_id,
            MediaBinding.operation_id.in_(tuple(reuse_lineage)),
        )
        .order_by(MediaBinding.created_at)
    ).scalars().first()


def _resolve_one(
    session: Session,
    *,
    keyring: KeyRing,
    device_id: str,
    media_id: str,
    limits: MediaLimits,
    ordinal: int,
) -> ResolvedImage:
    row = read_media_row(session, media_id)
    # One answer for "no such id", "somebody else's id" and "an id uploaded for
    # something other than chat": §5.2's existence-hiding rule, applied to the
    # message path so a media id cannot be probed by chatting with it.
    if row is None or row.device_id != device_id or row.purpose != CHAT_IMAGE_PURPOSE:
        raise MediaNotFoundError("no such media object")
    if row.state in MEDIA_OBJECT_TERMINAL_STATES or row.state in DELETION_STATES:
        raise MediaGoneError("this image was deleted")
    if row.state not in USABLE_STATES:
        raise MediaNotReadyError(f"this image is not ready ({row.state})")

    actual_mime = session.execute(
        select(MediaObject.actual_mime).where(MediaObject.media_id == media_id)
    ).scalar_one_or_none()
    if actual_mime is None or actual_mime not in limits.allowed_mimes:
        # §5.1/§5.4: the format registry identifies, the deployment's allow-list
        # permits. The measured type is what is checked -- a declaration can say
        # anything, and the probe already rejected the objects whose bytes
        # disagreed with theirs.
        raise MediaError(f"media type {actual_mime!r} is not allowed")

    digest = content_sha256(session, keyring=keyring, media_id=media_id)
    if digest is None:
        # `ready`/`bound` without a sealed digest is a state and a content that
        # disagree. Nothing a client can do about it, and nothing a retry fixes.
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            internal_detail=f"media object {media_id} published without a digest",
        )
    return ResolvedImage(
        media_id=media_id,
        content_sha256=digest,
        state=row.state,
        state_version=row.state_version,
        ordinal=ordinal,
    )
