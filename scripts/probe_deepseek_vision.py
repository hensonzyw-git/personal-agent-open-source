"""One-shot DeepSeek vision capability probe (2026-09-10).

Run from the worktree root:

    PYTHONPATH=src <main checkout>/.venv/bin/python scripts/probe_deepseek_vision.py

Why this exists: the DeepSeek provider evidence of 2026-09-05
(`docs/evidence/DeepSeek_provider接入_离线与live冒烟_2026-09-05.md`) records
that the model id contains `vision`, and states in the same breath that
"此模型是否对 vision 输入生效未验证（无 vision 用例）". A provider-given name
is not a verified capability. The 2026-09-10 rename moved the default id to
`deepseek-flash` (V4.1 Flash), which carries no vision evidence at all.

The probe answers exactly one question: **does the configured model actually
read image input through the ADK/LiteLlm path, or does it merely accept the
request shape and answer from priors?** Those two are indistinguishable from
a 200 response, which is why the image carries a secret the model cannot
know — a randomly placed unique bar in a randomly sized row — and why a
no-image control runs alongside it. A model that answers the control is
answering from priors, so its image answers prove nothing.

Failure shapes per §5.1: unreadable image, image part rejected by the
provider, a model id that does not exist (must fail closed, not silently
fall back), and the no-image control.

Prints PASS/FAIL lines and a summary; exit code 0 only if a model passed
every trial AND the control refused. The key itself is never printed.
"""

from __future__ import annotations

import asyncio
import os
import re
import struct
import sys
import zlib
from pathlib import Path

# litellm fetches a remote model-cost map at import and blocks on it; the
# fetch times out on this network and delays the first call by minutes. The
# probe has no use for pricing data.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

sys.path.insert(0, "src")

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


def _load_credential() -> None:
    for raw in _ENV_LOCAL.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("DEEPSEEK_API_KEY="):
            value = line.split("=", 1)[1].strip()
            if value and not os.environ.get("DEEPSEEK_API_KEY"):
                os.environ["DEEPSEEK_API_KEY"] = value


# --------------------------------------------------------------------------
# A PNG writer in the standard library, so the probe has no image dependency.
# --------------------------------------------------------------------------

BLACK = (0, 0, 0)
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
    """A row of `count` bars; exactly one is bright red, the rest dark grey.

    `seed` only varies the gutter widths, so two trial images with the same
    (count, red_index) are still not byte-identical.
    """
    bar_w, height = 34, 110
    gutter = 24 + (seed % 5) * 4
    width = gutter * (count + 1) + bar_w * count
    pixels = [[BLACK] * width for _ in range(height)]
    for i in range(count):
        left = gutter * (i + 1) + bar_w * i
        colour = BRIGHT_RED if i == red_index - 1 else DARK_GREY
        for y in range(18, height - 18):
            for x in range(left, left + bar_w):
                pixels[y][x] = colour
    return _png(width, height, pixels)


#: The format is described without a numeric example on purpose. An earlier
#: revision of this probe wrote "For example: n=7 red=3" while trial 1's
#: ground truth happened to be exactly n=7 red=3 — and the no-image control
#: returned that answer verbatim. The model had been copying the example, and
#: the image trials alone would have "proved" a vision capability that was
#: never exercised. Keep the control in the loop; it is what caught this.
_QUESTION = (
    "This image shows one horizontal row of vertical bars. Exactly one bar is "
    "bright red; every other bar is dark grey. Reply with the total number of "
    "bars and the 1-based position of the red bar counting from the left, and "
    "nothing else. Use exactly the form n=<total> red=<position>, replacing "
    "the angle-bracketed parts with digits."
)
_ANSWER = re.compile(r"n\s*=\s*(\d+)\s*red\s*=\s*(\d+)")


# --------------------------------------------------------------------------
# The provider call, mirroring `runtime/glm_gateway.generate_with_adk`'s shape.
# --------------------------------------------------------------------------


#: `deepseek-flash` is a reasoning model: it emits its trace into
#: `reasoning_content` and only then writes `content`. A small cap therefore
#: yields an empty `content` with `finish_reason=length` — which reads exactly
#: like a refusal unless the budget is raised. 2048 leaves room for the trace
#: and the answer in the shapes this probe sends.
_MAX_OUTPUT_TOKENS = 2048


async def _ask(model_id: str, api_key: str, api_base: str, image: bytes | None):
    """Return (text, prompt_tokens, error). `image=None` sends no image.

    `prompt_tokens` is the load-bearing evidence: a 729-byte PNG costs real
    image tokens, so a call that returns the text-only count did not carry the
    image at all. That is a different finding from "the model looked and was
    wrong", and the two must not be collapsed.
    """
    from google.adk.models.lite_llm import LiteLlm
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types

    parts = [types.Part(text=_QUESTION)]
    if image is not None:
        parts.append(
            types.Part(inline_data=types.Blob(mime_type="image/png", data=image))
        )

    llm = LiteLlm(
        model=f"openai/{model_id}",
        api_key=api_key,
        api_base=api_base,
        timeout=180,
        num_retries=0,
    )
    request = LlmRequest(
        contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=_MAX_OUTPUT_TOKENS,
        ),
    )
    try:
        responses = [
            response
            async for response in llm.generate_content_async(request, stream=False)
        ]
    except Exception as exc:  # noqa: BLE001 - the error shape is the finding
        return None, None, f"{type(exc).__name__}: {exc}"[:400]
    if len(responses) != 1:
        return None, None, f"expected 1 response, got {len(responses)}"
    response = responses[0]
    prompt_tokens = None
    meta = getattr(response, "usage_metadata", None)
    if meta is not None:
        prompt_tokens = getattr(meta, "prompt_token_count", None)
    if prompt_tokens is None:
        raw = getattr(response, "raw_response", None)
        usage = getattr(raw, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", None)

    content = getattr(response, "content", None)
    if content is None or not getattr(content, "parts", None):
        return "", prompt_tokens, None
    texts = [part.text for part in content.parts if getattr(part, "text", None)]
    return "\n".join(texts).strip(), prompt_tokens, None


async def main() -> int:
    _load_credential()
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        print("no DEEPSEEK_API_KEY in environment")
        return 2

    api_base = "https://api.deepseek.com/"
    candidates = [
        "deepseek-flash",  # the registry default since 2026-09-10
        "deepseek-v4-flash-vision-exp",  # the id DeepSeek itself names "vision"
        "deepseek-v4-pro",
    ]
    results: list[tuple[str, str]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        results.append((name, "PASS" if ok else "FAIL"))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")

    print(f"endpoint (pinned host): {api_base}")
    print("trials per model: 1 control + 3 image\n")

    for model_id in candidates:
        # --- image trials first, so the control has truths to be checked against
        truths: list[tuple[int, int]] = []
        image_tokens: list[int | None] = []
        correct = 0
        for trial in range(1, 4):
            count = 6 + (trial * 3 + len(model_id)) % 4  # 6..9, varies per cell
            red_index = 1 + (trial * 5 + len(model_id) * 3) % count
            truths.append((count, red_index))
            image = _bar_image(count, red_index, seed=trial + len(model_id))
            text, tokens, err = await _ask(model_id, api_key, api_base, image=image)
            image_tokens.append(tokens)
            name = f"{model_id} / image trial {trial}"
            truth = f"n={count} red={red_index}"
            if err:
                record(name, False, f"request failed: {err[:200]}")
                continue
            match = _ANSWER.search(text or "")
            if not match:
                record(
                    name,
                    False,
                    f"no parsable answer (truth {truth}, ptok={tokens}): {text[:100]!r}",
                )
            elif (int(match.group(1)), int(match.group(2))) == (count, red_index):
                correct += 1
                record(name, True, f"read the image correctly ({truth}, ptok={tokens})")
            else:
                record(
                    name,
                    False,
                    f"WRONG: said n={match.group(1)} red={match.group(2)}, "
                    f"truth {truth} (ptok={tokens})",
                )

        # --- the no-image control, judged on correctness, not on format ----
        # An earlier revision failed the control merely for emitting a parsable
        # answer. That is the wrong test: a model that cannot see the image and
        # says so, then guesses, is behaving correctly — the question is
        # whether the guess is RIGHT. `deepseek-flash` guesses the constant
        # "n=10 red=5" and admits "I'll output a guess"; that is exactly what a
        # control should show, and it collides with none of the truths above.
        text, tokens, err = await _ask(model_id, api_key, api_base, image=None)
        if err:
            verdict_control, detail_control = True, f"errored without image ({err[:60]})"
            guessed = None
        else:
            match = _ANSWER.search(text or "")
            guessed = (
                (int(match.group(1)), int(match.group(2))) if match else None
            )
            hit = guessed in truths if guessed else False
            verdict_control = not hit
            detail_control = (
                f"guessed {guessed} with no image -> COLLIDED with a truth"
                if hit
                else f"guessed {guessed} with no image, collided with no truth"
            )
        record(f"{model_id} / control", verdict_control, detail_control)

        # --- the decisive discriminator ------------------------------------
        baseline = tokens
        seen = [t for t in image_tokens if t]
        carried = bool(seen) and baseline is not None and min(seen) > baseline + 50
        record(
            f"{model_id} / image reached the model",
            carried,
            f"text-only ptok={baseline}, with-image ptok={seen}; "
            + (
                "image tokens present"
                if carried
                else "NO image tokens -> the provider dropped the image silently"
            ),
        )

        vision_ok = correct == 3 and verdict_control and carried
        record(
            f"{model_id} / VERDICT",
            vision_ok,
            (
                "vision confirmed: 3/3 correct, control did not collide, "
                "image tokens observed"
                if vision_ok
                else f"{correct}/3 correct, control_pass={verdict_control}, "
                f"image_carried={carried}"
            ),
        )

    failed = [name for name, verdict in results if verdict == "FAIL"]
    print(f"\nsummary: {len(results) - len(failed)}/{len(results)} PASS")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
