#!/usr/bin/env python3
"""Spike artefact: A2 transport-seam harness for the multimodal input round.

**This is historical spike evidence, not a production template** (CLAUDE.md
§5.1). Nothing here is wired into the product, and nothing here authorises a
product change. The seam it exercises is a *candidate composition* recorded in
`docs/evidence/多模态输入spike阶段一_2026-09-10.md`; the product path is
untouched and still text-only.

What it does: drives a synthetic image through the **locked** ADK -> litellm ->
openai -> httpx path with a `MockTransport`, so the entire run is offline, and
checks what a request-event-hook witness can and cannot establish.

It never touches the network. A socket guard turns any egress attempt into a
failure, so "offline" is enforced rather than asserted.

It prints no headers, no credentials and no request bodies: only hosts, sizes,
counts and digests.

Run with the interpreter that carries the locked versions:

    /path/to/main/.venv/bin/python scripts/spike_multimodal_a2_seam.py
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import socket
import struct
import sys
import zlib
from typing import Any, Callable

import httpx

MODEL = "openai/spike-vision-model"
# Reserved TLD: unresolvable by design, so even a leaked socket cannot reach a
# real provider. The harness pins this host and never follows a redirect off it.
PINNED_BASE_URL = "https://spike-pinned.invalid/v1"
DECOY_BASE_URL = "https://spike-decoy.invalid/v1"
FAKE_API_KEY = "spike-not-a-real-credential"  # never printed

TEXT = "识别这张账单"

# --------------------------------------------------------------------------
# Deterministic synthetic image (stdlib only; no PIL, no randomness)
# --------------------------------------------------------------------------


def _png(width: int, height: int, pixel: Callable[[int, int], tuple[int, int, int]]) -> bytes:
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0
        for x in range(width):
            raw += bytes(pixel(x, y))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


EXPECTED_IMAGE = _png(24, 16, lambda x, y: ((x * 11) % 256, (y * 17) % 256, ((x + y) * 7) % 256))
OTHER_IMAGE = _png(24, 16, lambda x, y: ((x * 3) % 256, (y * 5) % 256, ((x * y) * 2) % 256))
EXPECTED_DIGEST = hashlib.sha256(EXPECTED_IMAGE).hexdigest()


# --------------------------------------------------------------------------
# Offline enforcement
# --------------------------------------------------------------------------


class NetworkEgressAttempted(RuntimeError):
    pass


def install_socket_guard() -> list[str]:
    """Make any outbound connection attempt a visible failure."""
    attempts: list[str] = []

    def block(*args: Any, **kwargs: Any) -> Any:
        target = args[1] if len(args) > 1 else kwargs.get("address")
        attempts.append(repr(target))
        raise NetworkEgressAttempted(f"outbound connection attempted: {target!r}")

    socket.socket.connect = block  # type: ignore[method-assign]
    socket.create_connection = block  # type: ignore[assignment]
    return attempts


# --------------------------------------------------------------------------
# The witness and its verifier
# --------------------------------------------------------------------------


class A2Violation(RuntimeError):
    pass


class A2Witness:
    """Request-event hook: records the serialized body before transport.

    It observes only. It must never rewrite the request, or it would be
    vouching for its own edit (spike plan v0.2 §2, case N1).
    """

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def __call__(self, request: httpx.Request) -> None:
        self.records.append(
            {
                "method": request.method,
                "host": request.url.host,
                "path": request.url.path,
                "body": request.content,  # materialized at construction (bytes body)
            }
        )


def _image_uris(body: bytes) -> list[str]:
    """Every image data URI in the first user message, in order."""
    payload = json.loads(body)
    uris: list[str] = []
    for message in payload.get("messages", []):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "image_url":
                url = (block.get("image_url") or {}).get("url")
                if isinstance(url, str):
                    uris.append(url)
    return uris


def _text_before_image(body: bytes) -> bool:
    payload = json.loads(body)
    for message in payload.get("messages", []):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        kinds = [b.get("type") for b in content if isinstance(b, dict)]
        if "image_url" in kinds:
            return kinds.index("text") < kinds.index("image_url")
    return False


def verify(
    records: list[dict[str, Any]], *, expect_image: bool, prompt_tokens: int | None
) -> None:
    """The design's A2 check. Fails closed; never repairs or truncates.

    The usage rule is pre-registered from the design (G1: usage corroborates,
    and absent or malformed usage closes): the prompt token count must be a
    positive integer. It is deliberately a rule about the *value*, because a
    response with no `usage` field at all still reaches here (see case
    USAGE-ABSENT) — presence alone proves nothing.
    """
    if len(records) != 1:
        raise A2Violation(f"expected exactly one outbound request, saw {len(records)}")
    record = records[0]
    if record["host"] != httpx.URL(PINNED_BASE_URL).host:
        raise A2Violation(f"request left the pinned host: {record['host']}")

    uris = _image_uris(record["body"])
    if expect_image:
        if len(uris) != 1:
            raise A2Violation(f"expected exactly one image in the body, saw {len(uris)}")
        _prefix, _, encoded = uris[0].partition(",")
        digest = hashlib.sha256(base64.b64decode(encoded)).hexdigest()
        if digest != EXPECTED_DIGEST:
            raise A2Violation("image bytes in the body are not the expected image")
        if not _text_before_image(record["body"]):
            raise A2Violation("image precedes text; the round requires text first")
    elif uris:
        raise A2Violation("expected no image in the body, found one")

    if prompt_tokens is None or prompt_tokens < 1:
        raise A2Violation(f"prompt token count is {prompt_tokens!r}; insufficient evidence, close")


# --------------------------------------------------------------------------
# Candidate composition
# --------------------------------------------------------------------------


def _completion_json(*, usage: dict[str, int] | None) -> bytes:
    payload: dict[str, Any] = {
        "id": "spike-cmpl-1",
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


USAGE_OK = {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}
USAGE_ZERO = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


async def run_case(
    *,
    parts: list[Any],
    handler: Callable[[httpx.Request], httpx.Response],
    api_base: str = PINNED_BASE_URL,
    num_retries: int | None = 0,
) -> tuple[A2Witness, int | None, str | None]:
    """Drive one request through LiteLlm and return the witness + prompt tokens."""
    from google.adk.models.lite_llm import LiteLlm
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types
    from openai import AsyncOpenAI

    witness = A2Witness()
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        event_hooks={"request": [witness]},
        follow_redirects=False,  # precondition, not hygiene (evidence §4b)
        timeout=httpx.Timeout(10.0),
    )
    openai_client = AsyncOpenAI(
        api_key=FAKE_API_KEY,
        base_url=PINNED_BASE_URL,  # the client owns the URL once supplied
        http_client=http_client,
    )
    extra: dict[str, Any] = {"client": openai_client}
    if num_retries is not None:
        extra["num_retries"] = num_retries
    llm = LiteLlm(model=MODEL, api_key=FAKE_API_KEY, api_base=api_base, **extra)

    request = LlmRequest(contents=[types.Content(role="user", parts=parts)])

    prompt_tokens: int | None = None
    error: str | None = None
    try:
        async for response in llm.generate_content_async(request, stream=False):
            metadata = response.usage_metadata
            if metadata is not None:
                prompt_tokens = metadata.prompt_token_count
    except NetworkEgressAttempted:
        raise
    except Exception as exc:  # provider error: the case records it, does not hide it
        error = f"{type(exc).__name__}"
    finally:
        await http_client.aclose()
    return witness, prompt_tokens, error


def _part_text(text: str) -> Any:
    from google.genai import types

    return types.Part(text=text)


def _part_image(data: bytes, mime: str = "image/png") -> Any:
    from google.genai import types

    return types.Part(inline_data=types.Blob(mime_type=mime, data=data))


# --------------------------------------------------------------------------
# Cases
# --------------------------------------------------------------------------


def _ok_response(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=_completion_json(usage=USAGE_OK))


async def case_a2_exact() -> str:
    witness, prompt_tokens, error = await run_case(
        parts=[_part_text(TEXT), _part_image(EXPECTED_IMAGE)], handler=_ok_response
    )
    assert error is None, f"unexpected error: {error}"
    verify(witness.records, expect_image=True, prompt_tokens=prompt_tokens)
    image_bytes = len(EXPECTED_IMAGE)
    return f"1 request, image {image_bytes}B sha256={EXPECTED_DIGEST[:12]}…, text before image"


async def case_redirect_not_followed() -> str:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302, headers={"Location": "https://spike-elsewhere.invalid/v1/chat/completions"}
        )

    witness, _usage, error = await run_case(
        parts=[_part_text(TEXT), _part_image(EXPECTED_IMAGE)], handler=handler
    )
    hosts = {r["host"] for r in witness.records}
    assert hosts == {"spike-pinned.invalid"}, f"redirect was followed: hosts={hosts}"
    assert len(witness.records) == 1, f"expected one attempt, saw {len(witness.records)}"
    return f"1 attempt, stayed on the pinned host, raised {error}"


async def case_base_url_authority() -> str:
    witness, prompt_tokens, _error = await run_case(
        parts=[_part_text(TEXT), _part_image(EXPECTED_IMAGE)],
        handler=_ok_response,
        api_base=DECOY_BASE_URL,  # deliberately wrong; must not govern the URL
    )
    verify(witness.records, expect_image=True, prompt_tokens=prompt_tokens)
    return "LiteLlm api_base was the decoy; the request still went to the client's base_url"


async def case_n1_text_only() -> str:
    """The product's current mapping: text-only flattening, no image part."""
    witness, prompt_tokens, _error = await run_case(
        parts=[_part_text(TEXT)], handler=_ok_response
    )
    try:
        verify(witness.records, expect_image=True, prompt_tokens=prompt_tokens)
    except A2Violation as exc:
        return f"rejected as required: {exc}"
    raise AssertionError("A text-only body passed an image-required check")


async def case_n1_substituted() -> str:
    witness, prompt_tokens, _error = await run_case(
        parts=[_part_text(TEXT), _part_image(OTHER_IMAGE)], handler=_ok_response
    )
    try:
        verify(witness.records, expect_image=True, prompt_tokens=prompt_tokens)
    except A2Violation as exc:
        return f"rejected as required: {exc}"
    raise AssertionError("A substituted image passed the expected-image check")


async def case_n1_extra_image() -> str:
    witness, prompt_tokens, _error = await run_case(
        parts=[_part_text(TEXT), _part_image(EXPECTED_IMAGE), _part_image(OTHER_IMAGE)],
        handler=_ok_response,
    )
    try:
        verify(witness.records, expect_image=True, prompt_tokens=prompt_tokens)
    except A2Violation as exc:
        return f"rejected as required: {exc}"
    raise AssertionError("A doubled image passed the single-image check")


async def case_usage_absent() -> str:
    """A response with no `usage` field at all: measurement plus fail-closed."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_completion_json(usage=None))

    witness, prompt_tokens, _error = await run_case(
        parts=[_part_text(TEXT), _part_image(EXPECTED_IMAGE)], handler=handler
    )
    try:
        verify(witness.records, expect_image=True, prompt_tokens=prompt_tokens)
    except A2Violation as exc:
        return f"ADK surfaced prompt_token_count={prompt_tokens!r}; rejected: {exc}"
    raise AssertionError(
        f"A response with no usage field passed with prompt_token_count={prompt_tokens!r}"
    )


async def case_usage_zero() -> str:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_completion_json(usage=USAGE_ZERO))

    witness, prompt_tokens, _error = await run_case(
        parts=[_part_text(TEXT), _part_image(EXPECTED_IMAGE)], handler=handler
    )
    try:
        verify(witness.records, expect_image=True, prompt_tokens=prompt_tokens)
    except A2Violation as exc:
        return f"rejected as required: {exc}"
    raise AssertionError(f"An all-zero usage passed with prompt_token_count={prompt_tokens!r}")


async def case_error_no_retry() -> str:
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500, content=b'{"error":{"message":"spike"}}')

    witness, _usage, error = await run_case(
        parts=[_part_text(TEXT), _part_image(EXPECTED_IMAGE)],
        handler=handler,
        num_retries=0,
    )
    assert len(witness.records) == 1, f"num_retries=0 still produced {len(witness.records)} attempts"
    assert error is not None, "a 500 should surface as an error"
    return f"1 attempt under num_retries=0, error={error}"


async def case_error_default_retries() -> str:
    """Measurement, not a pass: how many attempts when retries are left alone?"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b'{"error":{"message":"spike"}}')

    witness, _usage, error = await run_case(
        parts=[_part_text(TEXT), _part_image(EXPECTED_IMAGE)],
        handler=handler,
        num_retries=None,
    )
    return f"measured {len(witness.records)} attempt(s) with retries left at the default, error={error}"


async def case_pdf_fails_closed() -> str:
    witness, _usage, error = await run_case(
        parts=[_part_text(TEXT), _part_image(b"%PDF-1.7 spike", mime="application/pdf")],
        handler=_ok_response,
    )
    assert not witness.records, "a pdf part reached the transport"
    return f"no outbound request; error={error}"


CASES: list[tuple[str, Callable[[], Any]]] = [
    ("A2-EXACT", case_a2_exact),
    ("REDIRECT-NOT-FOLLOWED", case_redirect_not_followed),
    ("BASEURL-AUTHORITY", case_base_url_authority),
    ("N1-TEXT-ONLY", case_n1_text_only),
    ("N1-SUBSTITUTED", case_n1_substituted),
    ("N1-EXTRA-IMAGE", case_n1_extra_image),
    ("USAGE-ABSENT", case_usage_absent),
    ("USAGE-ZERO", case_usage_zero),
    ("ERROR-NO-RETRY", case_error_no_retry),
    ("ERROR-DEFAULT-RETRIES", case_error_default_retries),
    ("PDF-FAIL-CLOSED", case_pdf_fails_closed),
]


async def main() -> int:
    attempts = install_socket_guard()
    failures = 0
    for name, case in CASES:
        try:
            detail = await case()
        except NetworkEgressAttempted as exc:
            failures += 1
            print(f"FAIL {name}: network egress attempted — {exc}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:  # noqa: BLE001 - spike harness reports, never hides
            failures += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {name}: {detail}")
    print(f"\nsocket guard blocked {len(attempts)} egress attempt(s)")
    print(f"{len(CASES) - failures}/{len(CASES)} cases as expected")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
