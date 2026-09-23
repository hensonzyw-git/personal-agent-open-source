#!/usr/bin/env python
"""Spike phase 3: the A2 witness against the real endpoint (live).

**Historical spike evidence, not a production template** (AGENTS.md §5.1).

What this buys that the offline harness and the capability probe together did
not:

1. The witness has only ever observed `MockTransport`. A hook that fires on a
   mock is not yet a hook that fires on a real connection.
2. The probe (`scripts/probe_deepseek_vision.py`) ran **without tools or a
   system instruction**. That the image survives the production composition is
   offline fact (phase 2); that the provider still reads it when tools are
   present is not.
3. `follow_redirects=False` and one-attempt-per-call are offline facts about a
   mock. The real endpoint's redirect behaviour is unknown until asked.

Already established and deliberately **not** re-bought here:
`deepseek-flash` reads images (3/3, `prompt_tokens` 102 -> ~290, controls
refused) — `docs/evidence/DeepSeek_vision能力探针_2026-09-10.md`.

Budget: Henson authorised `deepseek-flash` with a ceiling of 20 calls
(2026-09-10). This script spends 4 — 3 image trials and 1 matched no-image
control under the same composition. The control is not optional: without a
tools-bearing text-only baseline, the `prompt_tokens` delta cannot be
attributed to the image.

Run:
    .venv/bin/python \
        scripts/spike_multimodal_live.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import struct
import sys
import zlib
from pathlib import Path
from typing import Any

# litellm fetches a remote model-cost map at import and blocks on it; the fetch
# times out on this network. The probe found this first; same fix.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

#: The operator's repo-root `.env.local`. A worktree's `.git` file points at
#: the main checkout's `.git/worktrees/<name>`, so the main checkout root is
#: three levels above that gitdir.
_WT_ROOT = Path(__file__).resolve().parents[1]
_GIT_POINTER = _WT_ROOT / ".git"
if _GIT_POINTER.is_file():  # worktree checkout
    _GIT_DIR = Path(_GIT_POINTER.read_text().split(":", 1)[1].strip())
    _ENV_LOCAL = _GIT_DIR.parents[2] / ".env.local"
else:  # main checkout
    _ENV_LOCAL = _WT_ROOT / ".env.local"


class EgressBlocked(RuntimeError):
    """A request tried to leave for somewhere other than the pinned endpoint."""


def _load_credential() -> None:
    """Read the operator's key. It is never printed, logged or recorded."""
    for raw in _ENV_LOCAL.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("DEEPSEEK_API_KEY="):
            value = line.split("=", 1)[1].strip()
            if value and not os.environ.get("DEEPSEEK_API_KEY"):
                os.environ["DEEPSEEK_API_KEY"] = value


# --------------------------------------------------------------------------
# Image and ground truth, unchanged from the probe so the two records are
# directly comparable. An earlier probe revision leaked its answer through a
# numeric example in the prompt; the format below still carries no example.
# --------------------------------------------------------------------------

DARK_GREY = (70, 70, 78)
BRIGHT_RED = (237, 28, 36)


def _png(width: int, height: int, pixels: list[list[tuple[int, int, int]]]) -> bytes:
    raw = bytearray()
    for row in pixels:
        raw.append(0)  # filter type 0 (None)
        for r, g, b in row:
            raw += bytes((r, g, b))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return (
            struct.pack(">I", len(payload))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def _bar_image(count: int, red_index: int, seed: int) -> bytes:
    """A row of `count` bars; exactly one is bright red, the rest dark grey."""
    bar_w, height = 34, 110
    gutter = 24 + (seed % 5) * 4
    width = gutter * (count + 1) + bar_w * count
    rows: list[list[tuple[int, int, int]]] = []
    for _ in range(height):
        row: list[tuple[int, int, int]] = []
        for _ in range(gutter):
            row.append((0, 0, 0))
        for index in range(count):
            colour = BRIGHT_RED if index + 1 == red_index else DARK_GREY
            row.extend([colour] * bar_w)
            row.extend([(0, 0, 0)] * gutter)
        rows.append(row)
    return _png(width, height, rows)


_QUESTION = (
    "This image shows one horizontal row of vertical bars. Exactly one bar is "
    "bright red; every other bar is dark grey. Reply with the total number of "
    "bars and the 1-based position of the red bar counting from the left, and "
    "nothing else. Use exactly the form n=<total> red=<position>, replacing "
    "the angle-bracketed parts with digits."
)

#: Mirrors the shape of `glm_gateway`'s system instruction: an operator prompt
#: that also tells the model to answer rather than reach for a tool. Without
#: this the model may call `record_expense` and never answer the question.
_SYSTEM = (
    "You are a personal finance agent. Answer the user's question directly in "
    "the requested format. Only call a tool when the user asks you to record "
    "or query something; a question about an image is answered in prose."
)

#: A two-declaration mirror of `glm_gateway._declarations + _internal_declarations`.
#: Present so the live request carries tools exactly as production does.
_DECLARATIONS = [
    {
        "name": "record_expense",
        "description": "Record one expense line.",
        "parameters": {
            "type": "object",
            "properties": {"amount": {"type": "number"}},
            "required": ["amount"],
        },
    },
    {
        "name": "ask_clarification",
        "description": "Ask the user one clarifying question.",
        "parameters": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
    },
]

#: The probe's lesson, applied with more headroom: `deepseek-flash` reasons
#: first and writes `content` after, so a small cap yields a truncated answer
#: that reads like a refusal. The probe needed 2048 without tools; a
#: system-instruction + tools request reasons longer, so this run allows 4096.
_MAX_OUTPUT_TOKENS = 4096


# --------------------------------------------------------------------------
# The witness, now enforcing the pin rather than merely observing
# --------------------------------------------------------------------------


class PinnedWitness:
    """Records each outbound attempt, and refuses any that is not pinned.

    The guard is the point. `AGENTS.md` §5.1 requires that a credential travel
    only to the pinned endpoint; a hook that raises before the send makes that
    a mechanism instead of a claim, because the credential rides in the
    request's headers and the request never goes out.
    """

    def __init__(self, allowed_host: str) -> None:
        self.allowed_host = allowed_host
        self.records: list[dict[str, Any]] = []

    async def __call__(self, request: Any) -> None:
        # Must be a coroutine function: `AsyncClient._send_handling_redirects`
        # does `await hook(request)`. A plain `def` still runs its body and
        # still records, then fails on `await None` -- and the SDK reports that
        # as `APIConnectionError: Connection error`, which looks like a network
        # fault. The first live run of this script died exactly that way.
        host = request.url.host
        if host != self.allowed_host or request.url.scheme != "https":
            raise EgressBlocked(
                f"request targeted {request.url.scheme}://{host}, "
                f"not the pinned https://{self.allowed_host}"
            )
        body = request.content  # raises RequestNotRead, never a silent empty
        self.records.append(
            {
                "method": request.method,
                "host": host,
                "path": request.url.path,
                "body": body,
            }
        )


def _image_payloads(body: bytes) -> list[bytes]:
    """Return every decoded inline image in an OpenAI-style chat body."""
    import base64

    document = json.loads(body)
    found: list[bytes] = []
    for message in document.get("messages", []):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "image_url":
                continue
            url = (item.get("image_url") or {}).get("url") or ""
            if url.startswith("data:") and ";base64," in url:
                found.append(base64.b64decode(url.split(";base64,", 1)[1]))
    return found


def _check_records(
    records: list[dict[str, Any]], *, expect_image: bytes | None
) -> str:
    """Fail closed on anything other than one pinned attempt carrying `expect_image`."""
    if len(records) != 1:
        raise AssertionError(f"expected exactly 1 outbound attempt, saw {len(records)}")
    record = records[0]
    if record["method"] != "POST":
        raise AssertionError(f"unexpected method {record['method']}")
    images = _image_payloads(record["body"])
    if expect_image is None:
        if images:
            raise AssertionError(f"control request carried {len(images)} image(s)")
        return "1 attempt, no image in the body (control)"
    if len(images) != 1:
        raise AssertionError(f"expected exactly one image in the body, saw {len(images)}")
    digest = hashlib.sha256(images[0]).hexdigest()
    if digest != hashlib.sha256(expect_image).hexdigest():
        raise AssertionError("image bytes in the body are not the expected image")
    return (
        f"1 attempt, POST {record['path']}, image {len(images[0])}B "
        f"sha256={digest[:12]}…"
    )


# --------------------------------------------------------------------------
# The call, mirroring `glm_gateway.generate_with_adk`'s composition
# --------------------------------------------------------------------------


async def _ask(
    *, image: bytes | None, api_base: str, api_key: str, model: str, bare: bool = False
):
    import httpx
    from google.adk.models.lite_llm import LiteLlm
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types
    from openai import AsyncOpenAI

    from personal_agent.runtime.model_providers import PROVIDERS

    host = PROVIDERS["deepseek"].host
    witness = PinnedWitness(host)
    http_client = httpx.AsyncClient(
        event_hooks={"request": [witness]},
        follow_redirects=False,
        timeout=httpx.Timeout(180.0),
    )
    openai_client = AsyncOpenAI(
        api_key=api_key, base_url=api_base, http_client=http_client
    )
    functions = [
        types.FunctionDeclaration(
            name=item["name"],
            description=item["description"],
            parameters_json_schema=item["parameters"],
        )
        for item in _DECLARATIONS
    ]
    parts = [types.Part(text=_QUESTION)]
    if image is not None:
        parts.append(types.Part(inline_data=types.Blob(mime_type="image/png", data=image)))

    llm = LiteLlm(
        model=f"openai/{model}",
        api_key=api_key,
        api_base=api_base,
        timeout=180.0,
        num_retries=0,
        extra_body={},  # the DeepSeek provider takes no thinking params
        client=openai_client,
    )
    # `bare` reproduces the capability probe's composition exactly -- no system
    # instruction, no tools. Held against the composed run on the *same*
    # images, it is what separates "the model cannot read these bars" from
    # "the composition changes how it reads them".
    config = types.GenerateContentConfig(
        temperature=0.0,
        max_output_tokens=_MAX_OUTPUT_TOKENS,
    )
    if not bare:
        config.system_instruction = _SYSTEM
        config.tools = [types.Tool(function_declarations=functions)]
    request = LlmRequest(
        contents=[types.Content(role="user", parts=parts)],
        config=config,
    )

    prompt_tokens: int | None = None
    tool_calls = 0
    texts: list[str] = []
    finish_reason: str | None = None
    error: str | None = None
    try:
        async for response in llm.generate_content_async(request, stream=False):
            metadata = getattr(response, "usage_metadata", None)
            if metadata is not None:
                prompt_tokens = metadata.prompt_token_count
            raw = getattr(response, "raw_response", None)
            choices = getattr(raw, "choices", None)
            if choices:
                finish_reason = getattr(choices[0], "finish_reason", None)
            content = getattr(response, "content", None)
            for part in getattr(content, "parts", None) or []:
                if getattr(part, "function_call", None) is not None:
                    tool_calls += 1
                elif isinstance(getattr(part, "text", None), str):
                    texts.append(part.text)
    except Exception as exc:  # noqa: BLE001 - the error shape is a finding
        error = f"{type(exc).__name__}: {exc}"[:300]
    finally:
        await http_client.aclose()
    return witness, prompt_tokens, tool_calls, "\n".join(texts).strip(), error, finish_reason


async def main() -> int:
    import re

    _load_credential()
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        print(f"no DEEPSEEK_API_KEY in {_ENV_LOCAL}")
        return 2

    from personal_agent.runtime.model_providers import PROVIDERS, canonical_api_base

    provider = PROVIDERS["deepseek"]
    api_base = canonical_api_base(provider)
    model = provider.default_model
    answer_re = re.compile(r"n\s*=\s*(\d+)\s*red\s*=\s*(\d+)")
    # `--bare` isolates one variable: the same images, the capability probe's
    # composition. The bare text-only baseline (102) is already in the probe
    # record, so this mode spends 3 calls and re-buys no control.
    bare = "--bare" in sys.argv
    global _MAX_OUTPUT_TOKENS
    if "--max-tokens" in sys.argv:
        _MAX_OUTPUT_TOKENS = int(sys.argv[sys.argv.index("--max-tokens") + 1])
    print(f"endpoint {api_base} model {model} (host pinned by the registry)")
    print(
        f"composition: {'bare (no system, no tools)' if bare else 'system + tools'} "
        f"+ temperature=0.0 + max_output_tokens={_MAX_OUTPUT_TOKENS}"
    )

    failures = 0
    calls = 0
    image_tokens: list[int | None] = []
    truths: list[tuple[int, int]] = []

    for trial in range(3):
        count = 6 + (trial % 3)
        red_index = 1 + ((trial * 2) % count)
        truths.append((count, red_index))
        image = _bar_image(count, red_index, seed=trial + 7)
        witness, prompt_tokens, tool_calls, text, error, finish = await _ask(
            image=image, api_base=api_base, api_key=api_key, model=model, bare=bare
        )
        calls += 1
        image_tokens.append(prompt_tokens)
        try:
            detail = _check_records(witness.records, expect_image=image)
        except AssertionError as exc:
            failures += 1
            print(f"FAIL image-{trial + 1}: {exc}")
            continue
        if error:
            failures += 1
            print(f"FAIL image-{trial + 1}: {detail}; provider error {error}")
            continue
        match = answer_re.search(text)
        matched = match is not None and (
            int(match.group(1)), int(match.group(2))
        ) == (count, red_index)
        status = "PASS" if matched else "FAIL"
        if not matched:
            failures += 1
        # The whole answer, not a prefix: a length-truncated reply and a wrong
        # reply read identically in the first 60 characters.
        print(
            f"{status} image-{trial + 1}: {detail}; truth n={count} red={red_index}; "
            f"ptok={prompt_tokens} tool_calls={tool_calls} finish={finish} "
            f"len={len(text)} answer_tail={text[-260:]!r}"
        )

    control_tokens: int | None = None
    if bare:
        # The probe already bought this: the same question, no image, no
        # composition, cost 102 prompt tokens. Re-running it would spend a
        # call to re-learn a number that is already in the evidence record.
        control_tokens = 102
        print("control skipped: the probe's bare text-only baseline is 102 ptok")
    else:
        witness, prompt_tokens, tool_calls, text, error, finish = await _ask(
            image=None, api_base=api_base, api_key=api_key, model=model
        )
        calls += 1
        try:
            detail = _check_records(witness.records, expect_image=None)
        except AssertionError as exc:
            failures += 1
            print(f"FAIL control: {exc}")
            detail = ""
        if error:
            failures += 1
            print(f"FAIL control: {detail}; provider error {error}")
        else:
            match = answer_re.search(text)
            guess = (int(match.group(1)), int(match.group(2))) if match else None
            # The control must not collide with any truth above: if it does, an
            # image trial could have "passed" by guessing.
            collided = guess in truths if guess else False
            if collided:
                failures += 1
            control_tokens = prompt_tokens
            print(
                f"{'FAIL' if collided else 'PASS'} control: {detail}; no image; "
                f"ptok={prompt_tokens} tool_calls={tool_calls} finish={finish} "
                f"guessed={guess} collided={collided}"
            )

    print(f"calls spent: {calls} (authorised ceiling 20)")
    if all(isinstance(item, int) for item in image_tokens) and isinstance(
        control_tokens, int
    ):
        deltas = [item - control_tokens for item in image_tokens if item is not None]
        print(
            f"text-only baseline ptok={control_tokens}; image ptok={image_tokens}; "
            f"image-token delta={deltas}"
        )
    else:
        print("ptok comparison unavailable: at least one call did not report usage")
    print(f"{calls - failures}/{calls} calls as expected")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
