"""Structured model input, threaded from the resolved request to the provider.

Design §8: "结构化 InputPart 从请求封存→ContextEnvelope→gateway 消息构造→ADK
Part→LiteLlm→HTTP 全链传递." This is the resolved form of that chain: a text
input is the user's words, an image input is bytes that were authorized and read
from the media store.

The distinction that matters is between this type and
:class:`~personal_agent.api.chat_parts.ImageRefPart`. A `ChatPart` is what the
client sent -- a media id and nothing else, because "客户端只提交 media_id，不声明
权威 hash" (§3.2). An :class:`ImageInputPart` is what the server resolved that id
into: the decrypted bytes, their MIME type, and the digest the *server* measured
(§5.1's `content_sha256`). The A2 witness compares the bytes that actually leave
against that digest, so the comparison is against a value the client never had
the opportunity to choose.

`content_sha256` is required rather than optional, and re-derived on
construction. A part without it could still be sent, and the witness would then
have no independent basis for its check -- it would be verifying the bytes
against themselves, which is the shape §8.1's N1 rule exists to forbid.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from personal_agent_core.errors import AppError, ErrorCode


@dataclass(frozen=True)
class TextInputPart:
    """The user's words, as one part of an ordered input."""

    text: str


@dataclass(frozen=True)
class ImageInputPart:
    """Authorized image bytes, with the digest the server measured for them.

    `data` is plaintext, and exists only for the length of one turn. It is
    never written to a transcript, a log or an evidence record: §8 requires
    hash, count, target and attempt to be recorded, and forbids "raw base64、
    凭据、完整载荷" in ordinary logs. :func:`recorded_parts` is the only form
    that may be recorded.
    """

    mime_type: str
    data: bytes
    content_sha256: str
    #: §8's upper bound on what this image may cost the model input, computed
    #: when the part is built from the client's declared pixels and the
    #: deployment's coefficient. It travels with the part because the budgeter
    #: has no other way to price a photo, and because a value derived at the
    #: point of construction cannot drift from the declaration it came from.
    #:
    #: It is a bound, not a measurement: the server never decodes (§5.4), so
    #: the declared dimensions are the only figure that exists, and §8 asks for
    #: "保守上界" precisely because it may overstate.
    token_upper_bound: int

    def __post_init__(self) -> None:
        if not self.mime_type:
            raise _invalid("an image input part must declare a MIME type")
        if not self.data:
            raise _invalid("an image input part must carry bytes")
        if not isinstance(self.token_upper_bound, int) or isinstance(
            self.token_upper_bound, bool
        ):
            raise _invalid("an image input part must carry an integer token bound")
        if self.token_upper_bound < 1:
            # Zero is the dangerous value: the image is sent and charged
            # nothing, so a turn that fits the budget on paper is over it in
            # fact. A missing bound is refused rather than defaulted for the
            # same reason a missing media configuration keeps images off.
            raise _invalid("an image input part must cost at least one token")
        # The digest is the server's own measurement and the witness's only
        # independent basis for its check. A part whose declared digest does not
        # describe its own bytes is a defect in the caller, and repairing it here
        # would hand the witness a value derived from the very bytes it is meant
        # to be checking.
        if hashlib.sha256(self.data).hexdigest() != self.content_sha256:
            raise _invalid(
                "an image input part's content_sha256 must describe its own bytes"
            )


InputPart = TextInputPart | ImageInputPart


def image_parts(parts: tuple[InputPart, ...]) -> tuple[ImageInputPart, ...]:
    """The image parts, in order. §8 checks count and content against this."""
    return tuple(part for part in parts if isinstance(part, ImageInputPart))


def recorded_parts(parts: tuple[InputPart, ...]) -> list[dict[str, Any]]:
    """What a transcript may keep about these parts.

    Hashes, sizes and types -- never the bytes themselves and never base64. A
    transcript is written on every turn and read by people; an image encoded
    into it would be a copy of the user's photo in a place §6's deletion
    fan-out does not know about.
    """
    recorded: list[dict[str, Any]] = []
    for part in parts:
        if isinstance(part, TextInputPart):
            recorded.append({"type": "text", "chars": len(part.text)})
        else:
            recorded.append(
                {
                    "type": "image",
                    "mime_type": part.mime_type,
                    "bytes": len(part.data),
                    "content_sha256": part.content_sha256,
                }
            )
    return recorded


def _invalid(detail: str) -> AppError:
    return AppError(ErrorCode.INVALID_ARGUMENT, internal_detail=detail)
