"""The A2 witness: what left, checked against what was authorized.

§8.1's four constraints came out of the spike as *measurements*: a witness that
is a plain ``def``, that reuses one client per call, that edits the request, or
that trusts ``api_base`` to pin the host, all fail in ways that look like
something else. Each one has a test here, because a green suite written from the
same assumptions as the code proves nothing about the failure (§5.1).

The cases are offline and use ``MockTransport``: no provider is called and no
credential is involved.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json

import pytest

from personal_agent.runtime.a2_witness import (
    A2Violation,
    A2Witness,
    OutboundAttempt,
    body_images,
    verify,
)
from personal_agent.runtime.model_input import ImageInputPart, TextInputPart
from personal_agent_core.errors import AppError

PNG = b"\x89PNG\r\n\x1a\n" + b"body-bytes" * 4
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-body" * 4
HOST = "api.example.invalid"
#: What §8's coefficient would have made of these bytes' declared dimensions.
#: The witness does not read it -- it checks bytes against a digest -- but the
#: part cannot be built without one, because an unbudgeted image is the failure
#: the field exists to prevent.
PNG_TOKENS = 512
JPEG_TOKENS = 384


def _png_part() -> ImageInputPart:
    return ImageInputPart(
        mime_type="image/png",
        data=PNG,
        content_sha256=hashlib.sha256(PNG).hexdigest(),
        token_upper_bound=PNG_TOKENS,
    )


def _jpeg_part() -> ImageInputPart:
    return ImageInputPart(
        mime_type="image/jpeg",
        data=JPEG,
        content_sha256=hashlib.sha256(JPEG).hexdigest(),
        token_upper_bound=JPEG_TOKENS,
    )


def _body(*blocks: tuple) -> bytes:
    """An OpenAI-style chat body whose single user message carries `blocks`.

    A block is ``("text", text)`` or ``("image", mime, bytes)``; the bytes default
    to the PNG under test, so a case that wants a *different* image in the body
    says so explicitly rather than by rebuilding the helper.
    """
    content = []
    for block in blocks:
        if block[0] == "text":
            content.append({"type": "text", "text": block[1]})
        else:
            mime, data = block[1], (block[2] if len(block) > 2 else PNG)
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64," + _b64(data)},
                }
            )
    return json.dumps({"messages": [{"role": "user", "content": content}]}).encode()


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode()


def _attempt(body: bytes) -> A2Witness:
    witness = A2Witness(pinned_host=HOST)
    witness.attempts.append(
        OutboundAttempt(
            method="POST", scheme="https", host=HOST, path="/v1/chat", body=body
        )
    )
    return witness


class _Request:
    """The parts of an httpx.Request the witness touches."""

    def __init__(self, url: str, body: bytes = b"{}", method: str = "POST") -> None:
        from urllib.parse import urlsplit

        parsed = urlsplit(url)
        self.url = _Url(parsed)
        self.content = body
        self.method = method


class _Url:
    def __init__(self, parsed) -> None:
        self.scheme = parsed.scheme
        self.host = parsed.hostname
        self.path = parsed.path


# --- §8.1(1): the hook is a coroutine function -------------------------------


def test_the_witness_is_a_coroutine_function() -> None:
    """A plain `def` records, then fails on `await None` as a connection error.

    That was a real live failure (stage three §5): 4/4 calls reported
    `APIConnectionError`, which reads as a network fault and is not one. The
    check is on the function, not on a call, because the failure only appears
    once a real send happens.
    """
    assert inspect.iscoroutinefunction(A2Witness.__call__)

    witness = A2Witness(pinned_host=HOST)
    coroutine = witness(_Request(f"https://{HOST}/v1/chat"))
    assert inspect.isawaitable(coroutine)
    asyncio.run(coroutine)
    assert len(witness.attempts) == 1


# --- §8.1(4): the host pin is a mechanism, not a claim -----------------------


def test_an_attempt_at_another_host_is_refused_before_it_is_recorded() -> None:
    witness = A2Witness(pinned_host=HOST)
    with pytest.raises(A2Violation):
        asyncio.run(witness(_Request("https://attacker.invalid/v1/chat")))
    # Refused *and* recorded: if a transport layer ever wraps the raise into a
    # connection error, the verifier must still be able to name the real cause.
    assert witness.violations
    assert witness.attempts == []


def test_a_plaintext_attempt_is_refused() -> None:
    """The credential rides in these headers; http is never the pinned endpoint."""
    witness = A2Witness(pinned_host=HOST)
    with pytest.raises(A2Violation):
        asyncio.run(witness(_Request(f"http://{HOST}/v1/chat")))


def test_the_pinned_host_is_recorded_verbatim() -> None:
    witness = A2Witness(pinned_host=HOST)
    asyncio.run(witness(_Request(f"https://{HOST}/v1/chat", body=_body(("text", "hi")))))
    assert witness.attempts[0].host == HOST


# --- §8.1(2): one attempt per logical call -----------------------------------


def test_more_than_one_attempt_fails_closed() -> None:
    """Measured: without `num_retries` a 500 produces three attempts.

    A second attempt is a second chance for the body to be built differently
    from the one that was checked, so the count is a condition rather than a
    statistic.
    """
    witness = _attempt(_body(("text", "hi"), ("image", "image/png")))
    witness.attempts.append(witness.attempts[0])
    with pytest.raises(A2Violation, match="exactly one outbound attempt"):
        verify(witness, expected_images=(_png_part(),), prompt_tokens=100)


def test_no_attempt_at_all_fails_closed() -> None:
    with pytest.raises(A2Violation, match="exactly one outbound attempt"):
        verify(A2Witness(pinned_host=HOST), expected_images=(), prompt_tokens=100)


def test_a_recorded_violation_is_reported_as_itself() -> None:
    """Not as "something went wrong": the reason has to survive to the log."""
    witness = A2Witness(pinned_host=HOST, violations=["request targeted http://x"])
    with pytest.raises(A2Violation, match="request targeted http://x"):
        verify(witness, expected_images=(), prompt_tokens=100)


# --- §8.1(3) and the image checks --------------------------------------------


def test_the_authorized_image_is_accepted() -> None:
    witness = _attempt(_body(("text", "这张账单记一下"), ("image", "image/png")))
    verify(witness, expected_images=(_png_part(),), prompt_tokens=451)


def test_a_turn_with_no_image_verifies_against_an_empty_set() -> None:
    witness = _attempt(_body(("text", "午饭 45")))
    verify(witness, expected_images=(), prompt_tokens=102)


def test_a_text_only_body_cannot_pass_an_image_turn() -> None:
    witness = _attempt(_body(("text", "午饭 45")))
    with pytest.raises(A2Violation, match="expected 1 image"):
        verify(witness, expected_images=(_png_part(),), prompt_tokens=102)


def test_substituted_bytes_are_refused() -> None:
    """The claim is about *these* bytes; a same-shaped image is a different one.

    The authorized part is the PNG, and the body carries the JPEG under the PNG
    MIME: same declared type, same shape, different photo.
    """
    witness = _attempt(_body(("text", "hi"), ("image", "image/png", JPEG)))
    with pytest.raises(A2Violation, match="not the authorized bytes"):
        verify(witness, expected_images=(_png_part(),), prompt_tokens=451)


def test_an_extra_image_is_refused() -> None:
    witness = _attempt(
        _body(("text", "hi"), ("image", "image/png"), ("image", "image/png"))
    )
    with pytest.raises(A2Violation, match="expected 1 image"):
        verify(witness, expected_images=(_png_part(),), prompt_tokens=451)


def test_a_mime_the_authorized_set_does_not_contain_is_refused() -> None:
    """The bytes are the authorized PNG; only the declared type was changed."""
    witness = _attempt(_body(("text", "hi"), ("image", "image/jpeg", PNG)))
    with pytest.raises(A2Violation, match="MIME"):
        verify(witness, expected_images=(_png_part(),), prompt_tokens=451)


def test_two_images_are_compared_in_order() -> None:
    """Order is meaning (§3.1), so a swap is a different request, not a match."""
    first, second = _png_part(), _jpeg_part()
    swapped = _attempt(
        _body(("text", "hi"), ("image", "image/jpeg"), ("image", "image/png"))
    )
    with pytest.raises(A2Violation):
        verify(swapped, expected_images=(first, second), prompt_tokens=451)


def test_an_image_before_its_text_is_refused() -> None:
    witness = _attempt(_body(("image", "image/png"), ("text", "hi")))
    with pytest.raises(A2Violation, match="before its text"):
        verify(witness, expected_images=(_png_part(),), prompt_tokens=451)


def test_an_unparseable_body_yields_no_images_rather_than_an_error() -> None:
    """A body that cannot be read has zero images, which is then checked.

    The alternative -- treating "unparseable" as "not my problem" -- would let a
    body the witness does not understand pass as one it does.
    """
    assert body_images(b"not json at all") == []
    assert body_images(json.dumps({"messages": "not a list"}).encode()) == []
    with pytest.raises(A2Violation):
        verify(
            _attempt(b"{oops"),
            expected_images=(_png_part(),),
            prompt_tokens=451,
        )


# --- the usage rule is about the number, not the field's presence ------------


@pytest.mark.parametrize("tokens", [None, 0, -1])
def test_insufficient_usage_closes_the_turn(tokens: int | None) -> None:
    """ADK reports `0` for an absent `usage` field, so presence proves nothing."""
    witness = _attempt(_body(("text", "hi"), ("image", "image/png")))
    with pytest.raises(A2Violation, match="insufficient evidence"):
        verify(witness, expected_images=(_png_part(),), prompt_tokens=tokens)


# --- the input part type refuses to be its own evidence ----------------------


def test_an_image_part_must_carry_the_digest_of_its_own_bytes() -> None:
    """Otherwise the witness would check the bytes against themselves."""
    with pytest.raises(AppError) as excinfo:
        ImageInputPart(
            mime_type="image/png",
            data=PNG,
            content_sha256=hashlib.sha256(b"other").hexdigest(),
            token_upper_bound=PNG_TOKENS,
        )
    assert "content_sha256" in (excinfo.value.internal_detail or "")


def test_an_empty_image_part_is_refused() -> None:
    with pytest.raises(AppError) as excinfo:
        ImageInputPart(
            mime_type="image/png",
            data=b"",
            content_sha256=hashlib.sha256(b"").hexdigest(),
            token_upper_bound=PNG_TOKENS,
        )
    assert "must carry bytes" in (excinfo.value.internal_detail or "")


@pytest.mark.parametrize("bound", [0, -1, 512.5, True, None])
def test_an_image_part_must_cost_at_least_one_token(bound) -> None:
    """A photo that is sent but charged nothing is over budget in fact.

    §8 puts the image "计入 mandatory input", which only means something if
    there is a number to count. Zero, a fraction, a bool or a missing bound all
    price it as free, so all four are refused rather than defaulted -- the same
    fail-closed reading as "缺配置不启用图片" (§5.4).
    """
    with pytest.raises(AppError) as excinfo:
        ImageInputPart(
            mime_type="image/png",
            data=PNG,
            content_sha256=hashlib.sha256(PNG).hexdigest(),
            token_upper_bound=bound,
        )
    assert "token" in (excinfo.value.internal_detail or "")


def test_a_missing_token_bound_is_not_defaulted() -> None:
    """The argument is required, so there is no default to inherit."""
    with pytest.raises(TypeError):
        ImageInputPart(  # type: ignore[call-arg]
            mime_type="image/png",
            data=PNG,
            content_sha256=hashlib.sha256(PNG).hexdigest(),
        )


def test_text_parts_carry_no_bytes_to_record() -> None:
    from personal_agent.runtime.model_input import recorded_parts

    recorded = recorded_parts((TextInputPart("午饭 45"), _png_part()))
    assert recorded[0] == {"type": "text", "chars": len("午饭 45")}
    assert recorded[1]["bytes"] == len(PNG)
    # The digest, never the bytes: a transcript is read by people and is not
    # covered by §6's deletion fan-out.
    assert PNG not in json.dumps(recorded).encode()
