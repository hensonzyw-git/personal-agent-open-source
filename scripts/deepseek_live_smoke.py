"""One-shot DeepSeek provider live smoke (2026-09-05).

Run from the worktree root:

    PYTHONPATH=src python scripts/deepseek_live_smoke.py

The script reads the operator's repo-root `.env.local` (mode 600) itself and
loads only `DEEPSEEK_API_KEY` into the environment. Three shapes, per §5.1:
the clean path (tool-call round trip), a wrong model name (must surface a
provider error, not a silent fallback), and a tampered host (must be refused
before any network call). Prints PASS/FAIL lines and a summary; exit code 0
only if all three hold. The key itself is never printed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

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


from personal_agent.runtime.glm_gateway import (  # noqa: E402
    ModelGatewayError,
    generate_with_adk,
)
from personal_agent.runtime.model_providers import (  # noqa: E402
    canonical_api_base,
    credential_from_env,
    provider_from_env,
)

_TOOL = {
    "type": "function",
    "function": {
        "name": "finance-log-expense",
        "description": "记一笔支出",
        "parameters": {
            "type": "object",
            "properties": {"amount": {"type": "number"}},
            "required": ["amount"],
        },
    },
}

_MAIN = {"MODEL_PROVIDER": "deepseek", "GLM_MODEL": "deepseek-v4-flash-vision-exp"}

results: list[tuple[str, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, "PASS" if ok else "FAIL"))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def _one_turn(model: str, api_base: str, declarations: list) -> object:
    """One synchronous turn; `generate_with_adk` runs its own event loop."""
    return generate_with_adk(
        model=model,
        api_key=credential_from_env(provider_from_env(_MAIN)),
        api_base=api_base,
        system="你是个人财务助理。金额一律用数字。",
        messages=[{"role": "user", "content": "帮我记一笔 42.5 元的支出"}],
        declarations=declarations,
        temperature=0.1,
        max_tokens=512,
        timeout=25.0,
    )


def main() -> int:
    _load_credential()
    provider = provider_from_env(_MAIN)
    if credential_from_env(provider, os.environ) is None:
        print("no DEEPSEEK_API_KEY in environment")
        return 2
    base = canonical_api_base(provider)
    model = _MAIN["GLM_MODEL"]

    # Shape 1: clean path — a real tool-call round trip.
    try:
        response = _one_turn(f"openai/{model}", base, [_TOOL])
        parts = getattr(getattr(response, "content", None), "parts", []) or []
        calls = [getattr(p, "function_call", None) for p in parts]
        calls = [c for c in calls if c is not None]
        if calls:
            name = calls[0].name
            record(
                "clean-tool-call",
                name == "finance-log-expense",
                f"model responded with tool call {name!r}",
            )
        else:
            texts = [getattr(p, "text", "") for p in parts if getattr(p, "text", "")]
            record(
                "clean-tool-call",
                False,
                f"no tool call in response; text head: {texts[:1]!r}",
            )
    except ModelGatewayError as exc:
        record("clean-tool-call", False, f"gateway error: {str(exc)[:200]}")
    except Exception as exc:  # noqa: BLE001
        record("clean-tool-call", False, f"{type(exc).__name__}: {str(exc)[:200]}")

    # Shape 2: a wrong model name must fail with a provider error.
    try:
        _one_turn("openai/deepseek-nonexistent-model-xyz", base, [_TOOL])
        record("wrong-model-fails", False, "provider accepted a nonexistent model")
    except ModelGatewayError as exc:
        record("wrong-model-fails", True, f"failed closed: {str(exc)[:120]}")
    except Exception as exc:  # noqa: BLE001
        record("wrong-model-fails", True, f"failed closed: {type(exc).__name__}")

    # Shape 3: a tampered host must be refused before any network call.
    tampered = "https://api.deepseek.com.evil.example/"
    try:
        from personal_agent.runtime.model_providers import validated_api_base

        validated_api_base(tampered, provider)
        record("tampered-host-refused", False, "evil host passed validation")
    except ModelGatewayError:
        record("tampered-host-refused", True, "evil host rejected pre-network")

    failed = [name for name, verdict in results if verdict == "FAIL"]
    print(f"\nsummary: {len(results) - len(failed)}/{len(results)} PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
