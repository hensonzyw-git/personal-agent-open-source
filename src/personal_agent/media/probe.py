"""The bounded header probe: the only place that looks at untrusted bytes.

Multimodal design §4.2 and §12.2. Under option 1 the server does exactly two
things with an upload it does not trust -- it streams the bytes into a sealed
container while counting them, and it reads magic bytes at a fixed offset to
compare them against the client's declaration. The second job is this module.
It does not decode, does not re-encode, and does not interpret anything: the
design's whole claim is that the server never learns what is in an image, so a
probe that "checked a bit more" would be the beginning of a decoder.

Five constraints, all of them from the design text:

- **Fixed offset.** Only offset 0 and one landmark offset are read. Bytes
  elsewhere in the buffer cannot change the verdict, so no amount of crafted
  trailing content can steer identification.
- **Bounded.** The caller passes at most :data:`PROBE_BYTES`; more is refused
  rather than truncated, because a silent truncation here would mean the probe
  and the caller disagree about what was examined.
- **No allocation from a declared length.** There is no declared-length
  parameter to allocate from. A JPEG segment header claiming 65535 bytes is
  accepted as a JPEG and its claim is never honoured.
- **No derived file, no subprocess.** The module imports nothing that can open
  a file, start a process, or decode an image. A regression here would
  reintroduce exactly the sandbox the option-1 revision deleted.
- **Refusal is the only failure mode.** Every path either returns a media type
  or raises; nothing is repaired, coerced or defaulted (§5.1).

**The registry is identification, not permission.** :data:`_FORMATS` lists the
formats the probe can *name*, which is why ``image/png`` appears in it: the
design requires a failure shape for "declared type does not match the bytes",
and that shape cannot exist with a single-format registry. Permission is the
``allowed_mimes`` argument, and per §4.3 the limits are versioned configuration
("按实际 ECS 资源盘点形成版本化配置；缺配置不启用图片") -- so the probe takes the
set and holds no default. This round the client produces JPEG and only JPEG, so
the configured set contains ``image/jpeg`` alone. Adding a format is a
registry entry under review, never a loosened comparison.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Final

#: The most bytes the probe will look at. Large enough for the longest magic
#: plus its landmark, small enough that the caller's read buffer is trivial.
PROBE_BYTES: Final[int] = 32

REASON_EMPTY: Final[str] = "empty"
REASON_TRUNCATED: Final[str] = "truncated"
REASON_INCOMPLETE: Final[str] = "incomplete"
REASON_MALFORMED: Final[str] = "malformed"
REASON_UNKNOWN_MAGIC: Final[str] = "unknown_magic"
REASON_OVER_LONG: Final[str] = "over_long"
REASON_DECLARED_INVALID: Final[str] = "declared_mime_invalid"
REASON_DECLARED_NOT_ALLOWED: Final[str] = "declared_mime_not_allowed"
REASON_DECLARED_MISMATCH: Final[str] = "declared_mime_mismatch"

#: Reasons that more arriving bytes could still change. A streamed upload is
#: probed as its first bytes land, so `ff d8` is the correct beginning of a
#: JPEG and not yet a refusal -- while `ff d8 00` is already wrong at a fixed
#: offset, where no later byte can reach. Without this split a handler would
#: refuse the first batch of every valid upload, or buffer the whole file.
_PROVISIONAL_REASONS: Final[frozenset[str]] = frozenset(
    {REASON_EMPTY, REASON_TRUNCATED, REASON_INCOMPLETE}
)


class ProbeError(RuntimeError):
    """A prefix is not a well-formed, declared, accepted object.

    Carries the design's failure shape in :attr:`reason` so a caller can log or
    audit *why* -- "rejected" and "rejected for the designed reason" are
    different claims.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail

    @property
    def provisional(self) -> bool:
        """Whether more bytes could still change this verdict.

        A property of the evidence, not of the caller's decision: probing is
        pure, so a caller may re-probe a longer prefix at any time and must do
        so at end of stream, where a provisional reason becomes a refusal like
        any other. Only a non-provisional reason is one no later byte can
        rescue.
        """
        return self.reason in _PROVISIONAL_REASONS


@dataclass(frozen=True)
class _Format:
    """One identifiable container format.

    `landmark` is a structural byte string that must appear at
    `landmark_offset` for the file to be what its magic claims. It is the
    "magic legal but the body is incomplete" check: a bare PNG signature with no
    IHDR chunk behind it is refused rather than accepted as a tiny image.
    """

    mime: str
    magic: bytes
    landmark_offset: int = 0
    landmark: bytes = b""

    def landmark_end(self) -> int:
        return self.landmark_offset + len(self.landmark)


# Character set of a `type/subtype` with no parameters. Deliberately narrower
# than RFC 2045's token grammar: anything this probe does not need is refused,
# and a broader grammar can only ever admit more.
_TYPE_CHARS: Final[frozenset[str]] = frozenset(
    "abcdefghijklmnopqrstuvwxyz0123456789.-+"
)

# Ordered, and the first magic that matches decides. The two entries below
# cannot collide (their first bytes differ), but a format added later whose
# magic overlaps an earlier one would silently never be reached, so a new entry
# must be checked against this order and not merely appended to it.
_FORMATS: Final[tuple[_Format, ...]] = (
    # `ff d8` is SOI, which already names the format; the byte after it must
    # open the first marker (or be a fill byte), so it is a landmark rather
    # than part of the magic. Keeping the split there is what makes `ff d8`
    # alone "named but incomplete" rather than "unrecognised".
    _Format(mime="image/jpeg", magic=b"\xff\xd8", landmark_offset=2, landmark=b"\xff"),
    _Format(
        mime="image/png",
        magic=b"\x89PNG\r\n\x1a\n",
        landmark_offset=12,
        landmark=b"IHDR",
    ),
)


@dataclass(frozen=True)
class _Identified:
    """Either a named format or the design-named reason it could not be named."""

    mime: str | None
    failure: str | None


def _identify(prefix: bytes) -> _Identified:
    """Name the format at fixed offsets, or say why it cannot be named."""
    truncated = False
    for fmt in _FORMATS:
        if len(prefix) < len(fmt.magic):
            # A clean prefix of a known magic is truncation; anything else so
            # far is simply unknown, and the two deserve different logs.
            if fmt.magic.startswith(bytes(prefix)):
                truncated = True
            continue
        if prefix[: len(fmt.magic)] != fmt.magic:
            continue
        if fmt.landmark:
            end = fmt.landmark_end()
            if len(prefix) < end:
                # The landmark is not here *yet*: a longer prefix may still
                # complete this format.
                return _Identified(None, REASON_INCOMPLETE)
            if prefix[fmt.landmark_offset : end] != fmt.landmark:
                # The landmark is here and wrong, at an offset nothing later
                # can rewrite. This prefix is not that format, ever.
                return _Identified(None, REASON_MALFORMED)
        return _Identified(fmt.mime, None)
    return _Identified(None, REASON_TRUNCATED if truncated else REASON_UNKNOWN_MAGIC)


def sniff_mime(prefix: bytes) -> str | None:
    """The format these bytes are, or ``None`` if they are not one of them.

    Complete on its own: an unrecognisable, truncated or structurally
    incomplete prefix all return ``None``. Callers that need the distinction
    use :func:`probe_header`.
    """
    return _identify(prefix).mime


def _declared_mime(value: object, allowed: Collection[str]) -> str:
    """Validate the client's declaration against the configured accepted set.

    Not repaired: a parameter, a stray space or a doubled slash is refused
    rather than trimmed (§5.1). Case is the one thing folded, because media
    types are case-insensitive by standard, so folding is the comparison the
    standard prescribes.
    """
    if not isinstance(value, str):
        raise ProbeError(
            REASON_DECLARED_INVALID, "declared media type must be a string"
        )
    text = value.lower()
    major, slash, minor = text.partition("/")
    if not slash or not major or not minor:
        raise ProbeError(
            REASON_DECLARED_INVALID, f"{value!r} is not a media type of the form type/subtype"
        )
    if any(character not in _TYPE_CHARS for character in major + minor):
        raise ProbeError(
            REASON_DECLARED_INVALID,
            f"{value!r} has parameters, whitespace or stray characters",
        )
    if text not in {entry.lower() for entry in allowed}:
        raise ProbeError(
            REASON_DECLARED_NOT_ALLOWED, f"{value!r} is not an accepted media type"
        )
    return text


def probe_header(
    prefix: bytes,
    *,
    declared_mime: object,
    allowed_mimes: Collection[str],
) -> str:
    """Compare a bounded prefix against the declaration, or refuse it.

    Returns the sniffed media type -- which equals the declared one, because
    any disagreement is the :data:`REASON_DECLARED_MISMATCH` refusal. The
    declaration is therefore never taken on trust, and a caller that stores the
    return value stores what the bytes are rather than what the client said.

    `allowed_mimes` is configuration (§4.3) and is required: a probe with a
    built-in whitelist would be a policy no operator could turn off.
    """
    if len(prefix) > PROBE_BYTES:
        raise ProbeError(
            REASON_OVER_LONG,
            f"{len(prefix)} bytes exceeds the {PROBE_BYTES}-byte probe bound",
        )
    declared = _declared_mime(declared_mime, allowed_mimes)
    if not prefix:
        raise ProbeError(REASON_EMPTY, "no bytes to probe")
    identified = _identify(bytes(prefix))
    if identified.mime is None:
        raise ProbeError(identified.failure or REASON_UNKNOWN_MAGIC, "unrecognised header")
    if identified.mime != declared:
        raise ProbeError(
            REASON_DECLARED_MISMATCH,
            f"declared {declared!r} but the bytes are {identified.mime!r}",
        )
    return identified.mime
