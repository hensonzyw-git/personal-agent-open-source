"""The A2 witness inside `generate_with_adk`'s real composition.

The witness's own tests drive it directly, which proves the checks work on the
shapes those tests construct. This module answers the different question: does
the *production* call path -- `generate_with_adk`, LiteLlm, the OpenAI SDK,
httpx -- actually produce those shapes, and does the refusal actually stop a
turn before it can reach the caller?

Everything here is offline. `httpx.MockTransport` stands in for the provider, so
no credential is used and no request leaves the machine; everything around it is
the real composition at the versions the design pins
(google-adk 2.5.0 / litellm 1.91.4 / openai 2.47.0 / httpx 0.28.1).
"""

from __future__ import annotations

import base64
import hashlib
import json

import httpx
import pytest

# Load-bearing: `openai._base_client` declares `_DefaultAsyncHttpxClient(httpx.AsyncClient)`
# at module level, and nothing else on this path imports it -- `_witnessed_client`
# imports `AsyncOpenAI` lazily, inside the call. Without this line the class body
# is evaluated while the fixture below has `httpx.AsyncClient` replaced by a plain
# function, and collection-time-valid code fails with
# `TypeError: function() argument 'code' must be code, not str`.
import openai  # noqa: F401

from personal_agent.runtime.a2_witness import A2Violation
from personal_agent.runtime.glm_gateway import generate_with_adk
from personal_agent.runtime.model_gateway import ModelGatewayError
from personal_agent.runtime.model_input import ImageInputPart, TextInputPart
from personal_agent.runtime.model_providers import PROVIDERS, canonical_api_base
from personal_agent_core.errors import ModelFailureReason

pytest.importorskip("google.adk.models.lite_llm")

API_BASE = canonical_api_base(PROVIDERS["deepseek"])
MODEL = "openai/DeepSeek-V4-Flash-Vision-Exp"
FAKE_KEY = "not-a-real-credential"

PNG = b"\x89PNG\r\n\x1a\n" + b"authorized-photo" * 8
OTHER = b"\x89PNG\r\n\x1a\n" + b"a-different-photo" * 8

USAGE_OK = {"prompt_tokens": 451, "completion_tokens": 1, "total_tokens": 452}


def _part(data: bytes = PNG, *, tokens: int = 512) -> ImageInputPart:
    return ImageInputPart(
        mime_type="image/png",
        data=data,
        content_sha256=hashlib.sha256(data).hexdigest(),
        token_upper_bound=tokens,
    )


def _completion(usage: dict[str, int] | None) -> bytes:
    payload = {
        "id": "cmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": MODEL.split("/", 1)[1],
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
    }
    if usage is not None:
        payload["usage"] = usage
    return json.dumps(payload).encode()


class _Provider:
    """A MockTransport handler standing in for the provider, recording requests."""

    def __init__(self, *, usage: dict[str, int] | None = USAGE_OK) -> None:
        self.usage = usage
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, content=_completion(self.usage))


@pytest.fixture()
def provider(monkeypatch):
    """Point every client `generate_with_adk` builds at a mock transport.

    Patching the constructor rather than adding a test-only `transport=`
    parameter to production code: the seam under test is "the real client talks
    to the real composition", and a parameter only tests ever pass would mean the
    production path is the one thing never exercised.

    The replacement is a *subclass*, not a function. LiteLLM asks
    `isinstance(client, httpx.AsyncClient)`, and a functional stand-in turns that
    into `isinstance() arg 2 must be a type` -- a failure in the test harness that
    reads, from the traceback, like a production defect.
    """
    stand_in = _Provider()
    transport = httpx.MockTransport(stand_in)
    real = httpx.AsyncClient

    class MockedAsyncClient(real):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", MockedAsyncClient)
    return stand_in


def _ask(parts, **overrides):
    kwargs = dict(
        model=MODEL,
        api_key=FAKE_KEY,
        api_base=API_BASE,
        system="You are a personal finance agent.",
        messages=[{"role": "user", "content": "这张账单记一下"}],
        declarations=[],
        temperature=0.0,
        max_tokens=512,
        timeout=10.0,
        input_parts=parts,
    )
    kwargs.update(overrides)
    return generate_with_adk(**kwargs)


def _body_images(body: bytes) -> list[bytes]:
    document = json.loads(body)
    found = []
    for message in document["messages"]:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "image_url":
                url = block["image_url"]["url"]
                found.append(base64.b64decode(url.split(";base64,", 1)[1]))
    return found


def _last_content(body: bytes):
    return json.loads(body)["messages"][-1]["content"]


# --- the composition really does carry the image ------------------------------


def test_the_authorized_image_reaches_the_wire_byte_for_byte(provider) -> None:
    """The claim FR-PHOTO-05 rests on: the photo sent is the photo authorized."""
    _ask((TextInputPart("这张账单记一下"), _part()))

    assert len(provider.requests) == 1
    assert _body_images(provider.requests[0].content) == [PNG]


def test_image_only_through_real_sdk_composition(provider):
    _ask((_part(),), messages=[{"role": "user", "content": ""}])
    assert len(provider.requests) == 1
    assert _body_images(provider.requests[0].content) == [PNG]


def test_extra_remote_image_cannot_complete_a_real_sdk_turn(provider, monkeypatch):
    original = httpx.AsyncClient

    class ExtraImageClient(original):
        async def send(self, request, **kwargs):
            document = json.loads(request.content)
            document["messages"][-1]["content"].append({
                "type": "image_url",
                "image_url": {"url": "https://example.invalid/extra.png"},
            })
            headers = {key: value for key, value in request.headers.items()
                       if key.lower() != "content-length"}
            mutated = httpx.Request(
                request.method, request.url, headers=headers,
                content=json.dumps(document).encode(), extensions=request.extensions,
            )
            return await super().send(mutated, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", ExtraImageClient)
    with pytest.raises(ModelGatewayError) as error:
        _ask((TextInputPart("这张账单记一下"), _part()))
    assert error.value.response_shape == "a2_evidence"
    # Content evidence is checked after the synthetic provider reply. This
    # proves no successful turn, not that a real provider was called or blocked.
    assert len(provider.requests) == 1


def test_the_request_goes_to_the_pinned_endpoint(provider) -> None:
    """§8.1(4): supplying `client=` must not move the URL off the pinned host."""
    _ask((TextInputPart("hi"), _part()))

    url = provider.requests[0].url
    assert url.host == PROVIDERS["deepseek"].host
    assert url.scheme == "https"


def test_exactly_one_attempt_is_made(provider) -> None:
    """§8.1(2): `num_retries=0` is what turns three attempts into one."""
    _ask((TextInputPart("hi"), _part()))

    assert len(provider.requests) == 1


def test_the_text_is_sent_before_the_image(provider) -> None:
    """§3.1's wire order, preserved through ADK -> LiteLlm -> HTTP."""
    _ask((TextInputPart("这张账单记一下"), _part()))

    kinds = [block["type"] for block in _last_content(provider.requests[0].content)]
    assert kinds.index("text") < kinds.index("image_url")


def test_a_turn_without_parts_keeps_the_message_it_always_sent(provider) -> None:
    """No part means no change: a text turn must produce the request it did before.

    Asserted against the wire body rather than against `types.Part(text=...)`,
    because what a text turn sends today is the fact being preserved and only the
    serialized form establishes it.
    """
    _ask((), messages=[{"role": "user", "content": "午饭 45"}])

    assert _last_content(provider.requests[0].content) == "午饭 45"


def test_an_empty_part_tuple_is_the_pre_media_request_byte_for_byte(provider) -> None:
    """The parameter's existence must not change what a text turn sends.

    Every text turn in production now arrives with `input_parts=()`, so the
    claim is not "the last message still reads the same" but "the whole request
    is the one this composition built before parts existed". The second call
    omits the argument entirely, which is that request.
    """
    _ask((), messages=[{"role": "user", "content": "午饭 45"}])
    with_the_argument = provider.requests[0].content

    generate_with_adk(
        model=MODEL,
        api_key=FAKE_KEY,
        api_base=API_BASE,
        system="You are a personal finance agent.",
        messages=[{"role": "user", "content": "午饭 45"}],
        declarations=[],
        temperature=0.0,
        max_tokens=512,
        timeout=10.0,
    )
    without_it = provider.requests[1].content

    assert with_the_argument == without_it


def test_a_text_only_parts_request_sends_the_message_a_text_turn_sends(
    provider,
) -> None:
    """§3.1 allows `[text]`; the parts form is not an image-only form.

    It also has to be *the same request*: LiteLlm collapses a lone text part to
    the plain-string content form, so both shapes converge. Compared body to
    body rather than against the collapsed value, so a change in either path
    shows up here instead of in production.
    """
    _ask((TextInputPart("午饭 45"),), messages=[{"role": "user", "content": "午饭 45"}])
    _ask((), messages=[{"role": "user", "content": "午饭 45"}])

    assert provider.requests[0].content == provider.requests[1].content


# --- the checks actually stop a turn -----------------------------------------


@pytest.fixture()
def substituting_photo(monkeypatch):
    """Make the composition build the body from a *different* photo.

    The fault is injected in `_contents`, one layer above the witness, so the
    bytes really differ by the time the request exists. Nothing is stubbed at
    the check: the witness and `verify` run as they do in production, on a
    request the composition genuinely got wrong.
    """
    import personal_agent.runtime.glm_gateway as gateway

    original = gateway._contents

    def substituting(messages, input_parts, types):
        swapped = tuple(
            _part(OTHER) if isinstance(part, ImageInputPart) else part
            for part in input_parts
        )
        return original(messages, swapped, types)

    monkeypatch.setattr(gateway, "_contents", substituting)


def test_a_substituted_image_never_returns_a_response(
    provider, substituting_photo
) -> None:
    """A layer that swaps the user's photo still produces a clean `200`.

    If that can return a response, §8's A2 check is decoration: the model would
    answer about a picture the user never attached and the turn would succeed.
    """
    with pytest.raises(ModelGatewayError) as excinfo:
        _ask((TextInputPart("这张账单记一下"), _part()))

    assert "not the authorized bytes" in str(excinfo.value)


def test_a_refusal_is_the_failure_type_the_caller_handles(
    provider, substituting_photo
) -> None:
    """`A2Violation` must not escape `generate_with_adk`.

    The orchestrator's failure path is written against `ModelGatewayError` --
    that is where a reason, an operation close and an audit record come from. A
    bare `RuntimeError` would be fail-closed and invisible, which is the shape
    §7's "a model saying done is never success" does not cover.
    """
    with pytest.raises(ModelGatewayError) as excinfo:
        _ask((TextInputPart("这张账单记一下"), _part()))

    assert excinfo.value.reason == ModelFailureReason.UNAVAILABLE
    assert excinfo.value.response_shape == "a2_evidence"
    assert not isinstance(excinfo.value, A2Violation)


def test_the_substituted_image_is_what_actually_left(provider, substituting_photo) -> None:
    """Measured, and recorded because it is the uncomfortable half of the claim.

    The witness records the attempt inside the request hook and `verify` runs on
    the response, so this composition detects the substitution *after* the send.
    What is guaranteed is that no turn completes and no answer is returned --
    not that the wrong photo stayed on the machine. §8.1's pre-send refusal is
    about the endpoint, which is checked in the hook; the content check is not
    and cannot be, because the body only exists once it has been serialized.
    """
    with pytest.raises(ModelGatewayError):
        _ask((TextInputPart("这张账单记一下"), _part()))

    assert len(provider.requests) == 1
    assert _body_images(provider.requests[0].content) == [OTHER]


def test_a_refused_send_is_reported_as_a_refusal_not_a_network_fault(
    provider, monkeypatch
) -> None:
    """§8.1(1): the SDK's own words for a refusing hook are "connection error".

    The witness pins the host from `api_base` and the client is built from the
    same string, so in this composition the hook cannot legitimately refuse.
    Forcing the two apart models the case that matters: a hook refusal reaching
    the caller as `APIConnectionError`, where only the recorded violation
    distinguishes "we refused to send" from "the provider was unreachable".
    """
    import personal_agent.runtime.glm_gateway as gateway

    monkeypatch.setattr(gateway, "_pinned_host", lambda api_base: "other.invalid")

    with pytest.raises(ModelGatewayError) as excinfo:
        _ask((TextInputPart("hi"), _part()))

    assert excinfo.value.response_shape == "a2_evidence"
    assert "not the pinned" in str(excinfo.value)
    assert provider.requests == []
    # Pinned to what this composition actually says, measured rather than taken
    # from §8.1. It is *not* the `APIConnectionError` §8.1 records: by the time
    # the refusal crosses LiteLLM it has been relabelled a 500, which an operator
    # reads as "the provider broke, retry" -- further from the truth than the
    # connection error the spike saw at the SDK layer below. A version bump that
    # changes this name is a signal, not noise.
    assert excinfo.value.exception_type == "InternalServerError"


def test_the_sdk_below_litellm_reports_the_refusal_as_a_connection_error() -> None:
    """Where §8.1's observation actually holds, measured at its own layer.

    The spike's finding is about httpx and the OpenAI SDK, and it is correct
    there: a hook that raises makes the SDK report a connection error. Recording
    it separately keeps the two claims from being confused -- the SDK's name is
    wrong about the cause, and LiteLLM's replacement name is wrong about both the
    cause and the remedy.
    """
    import asyncio

    from openai import AsyncOpenAI

    from personal_agent.runtime.a2_witness import A2Witness

    witness = A2Witness(pinned_host="expected.invalid")
    client = httpx.AsyncClient(
        event_hooks={"request": [witness]},
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ok": True})
        ),
        follow_redirects=False,
    )
    sdk = AsyncOpenAI(api_key=FAKE_KEY, base_url=API_BASE, http_client=client)

    async def send():
        try:
            await sdk.chat.completions.create(
                model=MODEL.split("/", 1)[1],
                messages=[{"role": "user", "content": "hi"}],
            )
        except BaseException as exc:  # noqa: BLE001 -- the type is the measurement
            return type(exc).__name__
        finally:
            await client.aclose()
        return None

    assert asyncio.run(send()) == "APIConnectionError"
    # And the witness's record is what survives that name.
    assert witness.violations and witness.attempts == []


def test_an_answered_turn_with_no_usage_closes(provider) -> None:
    """The numeric rule: a clean 200 with no usage is insufficient evidence."""
    provider.usage = None
    with pytest.raises(ModelGatewayError):
        _ask((TextInputPart("hi"), _part()))


def test_a_zero_token_reading_closes(provider) -> None:
    provider.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    with pytest.raises(ModelGatewayError):
        _ask((TextInputPart("hi"), _part()))


def test_a_text_only_turn_is_not_witnessed(provider) -> None:
    """A turn with no image has nothing to vouch for, so it runs unwitnessed.

    Applying the A2 obligation to every text turn would put a newly built HTTP
    client in the path of production chat traffic to buy evidence about nothing.
    """
    _ask((TextInputPart("午饭 45"),), messages=[{"role": "user", "content": "午饭 45"}])

    assert len(provider.requests) == 1


def test_an_unresolvable_endpoint_refuses_an_image_turn(provider) -> None:
    """Without a declared provider there is no pinned host to check against.

    Recording an attempt to an unverified destination would be evidence for the
    wrong claim, so the turn fails before any client is built.
    """
    with pytest.raises(ModelGatewayError, match="pinned endpoint"):
        _ask(
            (TextInputPart("hi"), _part()),
            api_base="https://not-a-declared-provider.invalid/v1/",
        )
    assert provider.requests == []
