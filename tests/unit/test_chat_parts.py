"""The chat wire format's `parts`, and every way one is refused.

Multimodal design §3.1. The interesting cases here are not the legal ones --
there are three of those -- but the near-misses: a reversed pair that must not
be quietly sorted, an `image_ref` smuggling bytes alongside its id, a type a
later version will add. Each is refused as the whole request, because an old
server that dropped what it did not understand would answer a shorter question
than the user asked.
"""

from __future__ import annotations

import pytest

from personal_agent.api.chat_parts import (
    PARTS_SCHEMA_VERSION,
    ImageRefPart,
    TextPart,
    has_text_part,
    image_refs,
    open_chat_parts,
    parse_chat_parts,
    parts_text,
    seal_chat_parts,
)
from personal_agent_core.errors import AppError, ErrorCode

TEXT_ONLY = [{"type": "text", "text": "这张账单记一下"}]
IMAGE_ONLY = [{"type": "image_ref", "media_id": "media_1"}]
BOTH = [*TEXT_ONLY, *IMAGE_ONLY]


def refused(raw: object) -> AppError:
    with pytest.raises(AppError) as excinfo:
        parse_chat_parts(raw)
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT
    return excinfo.value


def refused_via(call) -> AppError:
    with pytest.raises(AppError) as excinfo:
        call()
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT
    return excinfo.value


# --- the three legal shapes --------------------------------------------------


def test_the_three_legal_shapes_are_accepted() -> None:
    assert parse_chat_parts(TEXT_ONLY) == (TextPart("这张账单记一下"),)
    assert parse_chat_parts(IMAGE_ONLY) == (ImageRefPart("media_1"),)
    assert parse_chat_parts(BOTH) == (
        TextPart("这张账单记一下"),
        ImageRefPart("media_1"),
    )


def test_a_parsed_image_part_carries_no_digest() -> None:
    # The client submits only a media id (§3.2). The digest is the server's and
    # is filled in later, when the part is resolved against the media table.
    (part,) = parse_chat_parts(IMAGE_ONLY)
    assert isinstance(part, ImageRefPart)
    assert part.content_sha256 is None


# --- the refusals ------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        [],
        "text",
        None,
        {},
        [{"type": "text"}],
        [{"type": "text", "text": "hi", "media_id": "m"}],
        [{"type": "image_ref"}],
        [{"type": "image_ref", "media_id": ""}],
        [{"type": "image_ref", "media_id": 7}],
        [{"type": "audio_ref", "media_id": "m"}],
        ["not an object"],
        [{"text": "no type"}],
        [{"type": 7, "text": "hi"}],
    ],
)
def test_a_malformed_parts_list_is_refused(raw: object) -> None:
    refused(raw)


def test_a_duplicated_type_is_refused() -> None:
    refused([*TEXT_ONLY, *TEXT_ONLY])
    refused([*IMAGE_ONLY, *IMAGE_ONLY])
    # Two images are not a valid request in this version, on one side or both.
    refused([*TEXT_ONLY, *IMAGE_ONLY, *IMAGE_ONLY])


def test_a_reversed_pair_is_refused_rather_than_reordered() -> None:
    # FR-COMMON-03. Silently sorting this into the order the server prefers
    # would make a client bug indistinguishable from a server behaviour. The
    # refusal is the whole proof: an implementation that reordered would return
    # the canonical tuple here instead of raising, and this test would fail.
    error = refused([*IMAGE_ONLY, *TEXT_ONLY])
    assert "before" in (error.internal_detail or "")
    assert parse_chat_parts(BOTH) == (
        TextPart("这张账单记一下"),
        ImageRefPart("media_1"),
    )


def test_an_image_ref_may_not_carry_anything_but_its_id() -> None:
    # §3.1: images never enter chat JSON as inline base64. Enforcing it as
    # "no unexpected fields" refuses the typo and the smuggled payload by one
    # rule, rather than by a scan for the keys someone thought of.
    refused([{"type": "image_ref", "media_id": "m", "data": "AAECAwQ="}])
    refused([{"type": "image_ref", "media_id": "m", "content_sha256": "a" * 64}])


def test_a_whitespace_only_text_part_follows_the_pre_media_rule() -> None:
    # §3.1 keeps the old rule for 空白纯文本: whitespace is not content.
    for blank in (" ", "   ", "\n", "\t\n "):
        refused([{"type": "text", "text": blank}])


def test_an_empty_text_part_is_accepted_and_stays_distinct() -> None:
    # §3.1 requires the sealed distinction between "no text part" and "empty
    # string" to survive; a distinction that cannot occur cannot survive, so an
    # empty text part is a real part. An empty string is not whitespace, which
    # is the case the rule above covers.
    empty = parse_chat_parts([{"type": "text", "text": ""}])
    assert empty == (TextPart(""),)

    # Both yield the same effective text, and they are still different
    # requests: one carries a text part and one does not.
    assert parts_text(empty) == parts_text(parse_chat_parts(IMAGE_ONLY)) == ""
    assert has_text_part(empty) is True
    assert has_text_part(parse_chat_parts(IMAGE_ONLY)) is False


# --- sealing and reading back ------------------------------------------------


def test_the_original_structure_is_what_gets_sealed() -> None:
    # §3.2 seals the original structure plus the schema version, so reading it
    # back reproduces exactly what arrived.
    for parts in (
        parse_chat_parts(TEXT_ONLY),
        parse_chat_parts(IMAGE_ONLY),
        parse_chat_parts(BOTH),
    ):
        assert open_chat_parts(
            seal_chat_parts(parts), schema_version=PARTS_SCHEMA_VERSION
        ) == parts


def test_sealing_refuses_a_resolved_part() -> None:
    # A part that has been resolved carries the server's digest, and sealing it
    # would store a value the request never contained -- changing what a later
    # replay is compared against.
    resolved = (ImageRefPart("media_1", content_sha256="a" * 64),)
    with pytest.raises(AppError):
        seal_chat_parts(resolved)


def test_a_sealed_payload_from_a_later_version_is_refused_as_such() -> None:
    # Checked before the structure, so an unknown version is reported as an
    # unknown version rather than as a malformed part.
    error = refused_via(lambda: open_chat_parts(TEXT_ONLY, schema_version=2))
    assert "version" in (error.internal_detail or "")


# --- the two accessors -------------------------------------------------------


def test_the_effective_text_comes_from_the_text_part() -> None:
    assert parts_text(parse_chat_parts(TEXT_ONLY)) == "这张账单记一下"
    assert parts_text(parse_chat_parts(BOTH)) == "这张账单记一下"
    assert parts_text(parse_chat_parts(IMAGE_ONLY)) == ""


def test_image_refs_are_returned_in_order() -> None:
    one = parse_chat_parts(IMAGE_ONLY)
    assert image_refs(one) == (ImageRefPart("media_1"),)
    assert image_refs(parse_chat_parts(TEXT_ONLY)) == ()
