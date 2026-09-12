"""The chat wire format's `parts`, and every way one is refused.

Multimodal design §3.1. A chat message arrives either as the pre-media `text`
field or as a `parts` list, and the rules are deliberately narrow: only `text`
and `image_ref` exist in this version, at most one of each, `text` first when
both are present, and anything else is refused as a whole request rather than
partially understood.

Two of those rules are worth stating as properties rather than as checks:

- **Order is checked, never repaired.** A reversed pair is refused instead of
  being silently sorted into the shape the server prefers, because "the server
  quietly reordered what I sent" is indistinguishable from a bug at the client
  (FR-COMMON-03).
- **Unknown fields are refused, not ignored.** A field the server does not know
  is how inline base64 would arrive, and §3.1 forbids images travelling that
  way -- so an `image_ref` carrying extra bytes is refused by the same rule that
  refuses a typo, rather than by a separate scan for `data` keys.

An illegal part has no event and no operation behind it (§3.1), so everything
here fails before the caller touches the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from personal_agent_core.errors import AppError, ErrorCode

#: The sealed shape's version. §3.2 seals it alongside the original structure so
#: a later version can be read back without guessing which rules produced it.
PARTS_SCHEMA_VERSION = 1

_TEXT_TYPE = "text"
_IMAGE_TYPE = "image_ref"

_TEXT_FIELDS = frozenset({"type", "text"})
_IMAGE_FIELDS = frozenset({"type", "media_id"})


@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class ImageRefPart:
    """A reference to an uploaded image, never the image itself.

    `content_sha256` is the server's measured digest and is absent on anything
    that came off the wire: the client submits only `media_id` and never
    declares an authoritative hash or computes the fingerprint (§3.2). It is
    filled in when the part is resolved against the media table, which is why
    it is a defaulted field on the *same* type rather than a second type --
    a resolved part and an unresolved one are the same thing at different
    points in the request's life.
    """

    media_id: str
    content_sha256: str | None = None


ChatPart = TextPart | ImageRefPart
Parts = tuple[ChatPart, ...]


def _invalid(detail: str) -> AppError:
    return AppError(ErrorCode.INVALID_ARGUMENT, internal_detail=detail)


def parse_chat_parts(raw: object) -> Parts:
    """Validate a wire `parts` list, or refuse the whole request.

    The legal lists are exactly three: `[text]`, `[image_ref]` and
    `[text, image_ref]`. Everything else -- an empty list, an unknown type, a
    duplicated type, an unexpected field, a reversed pair -- is an error.
    """
    if not isinstance(raw, list):
        raise _invalid("parts must be a list")
    if not raw:
        raise _invalid("parts must not be empty")

    parts: list[ChatPart] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise _invalid(f"parts[{index}] must be an object")
        kind = entry.get("type")
        if not isinstance(kind, str):
            raise _invalid(f"parts[{index}] needs a string type")
        if kind in seen:
            raise _invalid(f"parts carries more than one {kind!r} part")
        seen.add(kind)

        if kind == _TEXT_TYPE:
            parts.append(TextPart(_text_part(entry, index)))
        elif kind == _IMAGE_TYPE:
            parts.append(ImageRefPart(_image_ref_part(entry, index)))
        else:
            # Including the types a later version will add: an old server must
            # refuse a new part rather than drop it and answer a shorter
            # question than the user asked.
            raise _invalid(f"parts[{index}] has unknown type {kind!r}")

    if len(parts) == 2 and isinstance(parts[1], TextPart):
        raise _invalid("a text part must come before an image_ref part")
    return tuple(parts)


def _text_part(entry: dict[str, Any], index: int) -> str:
    if set(entry) != _TEXT_FIELDS:
        raise _invalid(f"parts[{index}] has fields a text part may not carry")
    text = entry["text"]
    if not isinstance(text, str):
        raise _invalid(f"parts[{index}].text must be a string")
    if text and not text.strip():
        # The pre-media rule for a whitespace-only message, unchanged: it is
        # not content. An *empty* string is not this case and is allowed -- see
        # the implementation note in the design.
        raise _invalid(f"parts[{index}].text is blank")
    return text


def _image_ref_part(entry: dict[str, Any], index: int) -> str:
    if set(entry) != _IMAGE_FIELDS:
        raise _invalid(f"parts[{index}] has fields an image_ref part may not carry")
    media_id = entry["media_id"]
    if not isinstance(media_id, str) or not media_id:
        raise _invalid(f"parts[{index}].media_id must be a non-empty string")
    # Whether this id names a real, ready, owned image is not a wire question:
    # it is answered against the media table when the request is anchored.
    return media_id


def seal_chat_parts(parts: Parts) -> list[dict[str, Any]]:
    """The parts as the original structure, for the sealed request payload.

    §3.2 seals the original structure, so this is the wire shape again -- the
    measured digest belongs to the fingerprint and to the media table, not
    here. Asserting the resolved field is absent keeps a resolved part from
    quietly changing what a later replay is compared against.
    """
    sealed: list[dict[str, Any]] = []
    for part in parts:
        if isinstance(part, TextPart):
            sealed.append({"type": _TEXT_TYPE, "text": part.text})
        elif isinstance(part, ImageRefPart):
            if part.content_sha256 is not None:
                raise _invalid("a sealed part carries no measured digest")
            sealed.append({"type": _IMAGE_TYPE, "media_id": part.media_id})
        else:  # pragma: no cover - unreachable while ChatPart is a closed union
            raise _invalid(f"unknown part {part!r}")
    return sealed


def open_chat_parts(raw: object, *, schema_version: object) -> Parts:
    """Read parts back out of a sealed payload, or fail closed.

    The version is checked before the structure, so a payload written by a
    later version is refused as such rather than reported as a malformed part.
    """
    if schema_version != PARTS_SCHEMA_VERSION:
        raise _invalid(
            f"sealed parts schema version {schema_version!r} is not readable"
        )
    return parse_chat_parts(raw)


def same_parts(left: Parts, right: Parts) -> bool:
    """Whether two `parts` lists name the same request, order included.

    §3.2's replay rule compares an arriving request against a stored one. It
    cannot recompute
    :func:`~personal_agent.api.operation_store.chat_request_fingerprint`,
    because that input carries the measured digest and reading a digest back is
    a read of live media -- which the same sentence forbids ("不访问活媒体"). The
    comparison costs nothing to lose: a media object's digest is written once
    when the object is published and never changes, so the ordered `media_id`s
    already determine the ordered digests. What it keeps is everything the text
    alone loses, including the difference between an absent text part and an
    empty one.

    Comparison is on `(type, value)` and nothing else. A sealed part never
    carries a digest (see :func:`seal_chat_parts`) and a part off the wire
    cannot supply one, so there is no third field for the two sides to disagree
    about.
    """

    def identity(part: ChatPart) -> tuple[str, str]:
        if isinstance(part, TextPart):
            return (_TEXT_TYPE, part.text)
        return (_IMAGE_TYPE, part.media_id)

    if len(left) != len(right):
        return False
    return [identity(part) for part in left] == [identity(part) for part in right]


def parts_text(parts: Parts) -> str:
    """The effective user text: the text part's text, or "" when there is none.

    A request with no text part and one with an empty text part both give "",
    and they are still different requests -- the distinction lives in the parts
    and survives sealing, as §3.1 requires.
    """
    for part in parts:
        if isinstance(part, TextPart):
            return part.text
    return ""


def has_text_part(parts: Parts) -> bool:
    return any(isinstance(part, TextPart) for part in parts)


def image_refs(parts: Parts) -> tuple[ImageRefPart, ...]:
    return tuple(part for part in parts if isinstance(part, ImageRefPart))
