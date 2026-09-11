"""§6's authorized read: the lock, the authorization, and the bytes.

Design §6, in one sentence: "授权与读取必须在同一段锁保护下衔接，关闭 v0.5「已
授权但尚未读，文件先被删」的窗口：取得存储共享锁＋条带独占锁 → DB 校验未删除、
来源/operation/attempt 有效，登记本次使用并 commit → 保持条带锁，认证解密并读
完整图片到有界内存 → 释放锁 → 进入该次模型调用。失败释放锁且不发送".

Three parts, and this module is only the third of them:

- **登记本次使用 is the anchor's, and it has already happened.** §5.1's use
  relation *is* the `media_bindings` row plus the `ready -> bound` move, and
  `_anchor_chat` commits both under these same locks before the model is ever
  consulted. §5 says the rest plainly -- "不需要把实际读锁复制成第二套 DB 租约
  系统" -- so this step re-validates that registration rather than writing a
  second one. That is also what "不能仅凭永久 authorization 行绕过删除" asks
  for: the row that authorizes is read live, under the lock, on every attempt,
  and a deleted source fails here no matter what was granted earlier.
- **校验未删除、来源/operation/attempt 有效 is the re-read below**, and it is the
  whole reason the stripe is held while the bytes come off the disk. A deletion
  decided between the anchor and this call must win, and it can only be seen by
  reading state *after* the lock is taken.
- **认证解密并读完整图片到有界内存 is `store.read_final`**, which authenticates
  the container against its sealed attempt and refuses past
  `max_content_bytes` rather than truncating to fit.

**Every failure here is a `MediaError`, including the ones the store reports as
something else.** `read_final` raises `ContainerError` -- §5.3's "ready, file
missing or does not authenticate" row, the condition `MediaIntegrityError` is
the other name for -- and that is a `RuntimeError`, deliberately not a media
refusal, because a store that has lost a file is an incident rather than a
request that was wrong. It is translated below so that this module keeps one
failure vocabulary: the caller classifies refusals, and an unclassified
`RuntimeError` escaping here would reach the transport as a crash with no
reason recorded against the operation. The translation is only of *type*: the
outward code it reaches is `INTERNAL_ERROR`, which is what §5.3 asks for and
what an incident should be.

Two things this module does not do, each because something else already does:

**It does not write.** Not a lease, not a marker, not a "read at" column. §5
rules that out by name, and a second bookkeeping system beside the state machine
is what the design spent a paragraph refusing.

**It does not read the whole request's images in one lock and hold them.** The
caller wraps this around exactly the turn it belongs to, so the lock is held for
one read and released before the model call (see `_context_factory` in `app.py`).

Two implementation notes that are load-bearing rather than hygiene:

- **The `rollback` before the lock is correctness.** A plain `SELECT` opens a
  read transaction, and every later read inside it sees the snapshot it started
  from. Without ending it, the in-lock re-read would report the state that was
  true *before* the lock was taken -- the exact window §6 exists to close. §4.1
  forbids holding a transaction across a file lock anyway; this is both rules at
  once, and `read_media` uses the same shape for the same reason.
- **The lock is taken non-blocking.** §4.1's "每次等待有界" is satisfied by not
  waiting at all, which is also what every other media path in this build does
  (`read_media`, `_anchor_locks`). A contended stripe is a `MEDIA_BUSY` the
  caller can classify, and §6's answer to a failure is "释放锁且不发送" rather
  than "wait longer".
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from personal_agent.media.container import ContainerError
from personal_agent.media.lifecycle import (
    CHAT_IMAGE_PURPOSE,
    DELETION_STATES,
    USABLE_STATES,
    MediaLifecycleError,
    content_sha256,
    read_media_row,
    seal_record,
)
from personal_agent.media.locking import MediaLockError, media_locks
from personal_agent.media.store import MediaIntegrityError, MediaStore
from personal_agent.media.uploads import (
    MediaBusyError,
    MediaGoneError,
    MediaLimits,
    MediaNotFoundError,
    MediaNotReadyError,
)
from personal_agent.runtime.model_input import ImageInputPart
from personal_agent.storage.models import (
    MEDIA_OBJECT_TERMINAL_STATES,
    MediaBinding,
    MediaObject,
)
from personal_agent_core.crypto import KeyRing


def read_authorized_images(
    session: Session,
    *,
    store: MediaStore,
    keyring: KeyRing,
    limits: MediaLimits,
    event_id: str,
    operation_id: str,
    media_ids: Sequence[str],
) -> tuple[ImageInputPart, ...]:
    """§6's read, for every image one message names, or a refusal.

    `event_id` and `operation_id` are the message's own -- the ones the
    anchoring transaction bound these images to. They are what makes "来源/
    operation 有效" a question with an answer: a use relation this message does
    not have is not one it may read through.

    Returns the parts in `media_ids` order, which is the order the request's
    `parts` array established and the order the gateway will send them in (§8
    makes the input an ordered structure, not a set).

    Every failure is a `MediaError` and every failure releases the lock without
    sending anything (§6: "失败释放锁且不发送"). Which failures the *caller*
    may retry is a separate question, and the answer is in `app.py` -- it is not
    this module's to decide, and it is not the same for all of them.
    """
    if not media_ids:
        # A text-only turn never reaches the media store, which is what §5.4's
        # "缺配置不启用图片" needs: a deployment without a media surface still
        # serves every text turn.
        return ()

    # End the assembly's read transaction before taking a file lock. See the
    # module docstring: without this the re-read below would answer with a
    # snapshot taken before the lock existed.
    session.rollback()
    try:
        with media_locks(store.roots.root, media_ids, blocking=False):
            return tuple(
                _read_one(
                    session,
                    keyring=keyring,
                    store=store,
                    limits=limits,
                    media_id=media_id,
                    event_id=event_id,
                    operation_id=operation_id,
                )
                for media_id in media_ids
            )
    except MediaLockError as exc:
        raise MediaBusyError("the media store is busy; retry shortly") from exc
    finally:
        # The reads below opened a transaction of their own, and the caller is
        # about to hand the session to the context builder -- which must see
        # committed state, not this call's snapshot.
        session.rollback()


def _read_one(
    session: Session,
    *,
    keyring: KeyRing,
    store: MediaStore,
    limits: MediaLimits,
    media_id: str,
    event_id: str,
    operation_id: str,
) -> ImageInputPart:
    """One image, inside the lock: re-validate, then read.

    The order is §6's, and the first check is deliberately the same one
    `_resolve_one` makes. That is not redundancy for its own sake: the two run
    in different transactions on either side of a window in which a deletion
    can commit, and re-asking is the only thing that closes it.
    """
    row = read_media_row(session, media_id)
    if row is None or row.purpose != CHAT_IMAGE_PURPOSE:
        # Same single answer as the anchor gives for "no such id" and "not a
        # chat image": §5.2's existence-hiding rule, kept identical here so a
        # media id cannot be probed by the shape of the refusal either.
        raise MediaNotFoundError("no such media object")
    if row.state in DELETION_STATES or row.state in MEDIA_OBJECT_TERMINAL_STATES:
        raise MediaGoneError("this image was deleted")
    if row.state not in USABLE_STATES:
        raise MediaNotReadyError(f"this image is not ready ({row.state})")
    attempt = row.current_attempt_number
    if attempt is None:
        # `ready`/`bound` with no attempt is a state and a content that
        # disagree. Unreachable while the state machine is correct, which is
        # why it refuses rather than guessing an attempt number.
        raise MediaLifecycleError(f"{media_id} is {row.state} with no attempt")

    _require_binding(
        session, media_id=media_id, event_id=event_id, operation_id=operation_id
    )

    measured = session.execute(
        select(
            MediaObject.declared_width,
            MediaObject.declared_height,
            MediaObject.actual_mime,
        ).where(MediaObject.media_id == media_id)
    ).one_or_none()
    if measured is None:
        raise MediaNotFoundError("no such media object")
    width, height, mime = measured
    if mime is None or mime not in limits.allowed_mimes:
        # Re-asked under the lock for the same reason as the state: the
        # deployment's allow-list is what §5.1 makes it, and an object whose
        # measured type is no longer permitted must not become a part.
        raise MediaLifecycleError(f"media type {mime!r} is not allowed")

    seal = seal_record(
        session, keyring=keyring, media_id=media_id, attempt_number=attempt
    )
    if seal is None:
        # Nothing established that these bytes are complete, and
        # `read_container` refuses an unsealed container for exactly that
        # reason. Refused here so the failure names the state rather than the
        # reader's internals.
        raise MediaNotReadyError(f"{media_id} has no seal record for its attempt")
    try:
        body = store.read_final(media_id, attempt, seal)
    except (ContainerError, MediaIntegrityError) as exc:
        # §5.3's row for a `bound` object whose persisted image is gone or does
        # not authenticate: refuse and alert, and never treat it as ordinary
        # recovery by generating another content. Both names are caught because
        # they are the same condition -- `verify_ready_final` wraps one in the
        # other -- and a read that let a `RuntimeError` out would be refusing
        # without telling the operation what happened.
        raise MediaLifecycleError(
            f"persisted image for {media_id} is missing or does not authenticate: {exc}"
        ) from exc

    digest = content_sha256(session, keyring=keyring, media_id=media_id)
    if digest is None:
        raise MediaLifecycleError(f"{media_id} published without a digest")

    return ImageInputPart(
        mime_type=mime,
        data=body,
        content_sha256=digest,
        token_upper_bound=_token_upper_bound(limits, width, height),
    )


def _require_binding(
    session: Session, *, media_id: str, event_id: str, operation_id: str
) -> None:
    """§6's "来源/operation 有效", as a question about recorded rows.

    A plain ``SELECT``, like every other decision read in this path: the
    identity map's view of a binding is not evidence, and this must see the
    database's current state. All three columns are checked, because each is a
    different claim -- that this image is used by *this* message, under *this*
    operation. A row matching only some of them would be a use relation that
    belongs to somebody else's turn.
    """
    found = session.execute(
        select(MediaBinding.binding_id).where(
            MediaBinding.media_id == media_id,
            MediaBinding.event_id == event_id,
            MediaBinding.operation_id == operation_id,
        )
    ).first()
    if found is None:
        # Refused rather than looked up further: §3.2's anchors "全部" of a
        # request's images at once, so an image with no binding for this message
        # is not part of this message, and reading it anyway would send a photo
        # the turn has no record of using.
        raise MediaNotFoundError("this image is not bound to this message")


def _token_upper_bound(limits: MediaLimits, width: int | None, height: int | None) -> int:
    """§8's conservative ceiling for one image, from what the server has.

    A declared rectangle is priced as declared. An undeclared one is priced at
    the deployment's own `max_dimension`, which is not a guess: `create` refuses
    any larger declaration (§4.3's "客户端声明值仍按上限校验"), so it is the
    largest rectangle an object reaching this point could have. §8 asks for a
    保守上界 and this is the only figure that is true of every object that could
    be here.

    The alternative for an undeclared image -- pricing it at one token -- is the
    one answer a *bound* may not give: the image would be sent and charged
    nothing, so a turn that fits the budget on paper is over it in fact. §10's
    "预算缺失时保持关闭" is about the *deployment's* configuration, and that
    case is already closed by §8's media term; it is not a licence to invent a
    price for a request that omitted a field.
    """
    if width is None or height is None:
        width = height = limits.max_dimension
    return limits.image_token_upper_bound(width, height)


__all__ = ["read_authorized_images"]
