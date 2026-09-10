"""Wrapper: load DEEPSEEK_API_KEY from the repo-root .env.local, then run eval_cli.

Mirrors scripts/deepseek_live_smoke.py: a worktree's `.git` file points at the
main checkout's gitdir, so `.env.local` lives three levels above it. Only
DEEPSEEK_API_KEY is loaded, and a stale Zhipu credential / base URL is popped
so the run cannot silently target the wrong provider.

Usage (from the worktree root):

    PYTHONPATH=src .venv/bin/python scripts/run_trv005_eval.py \
        --dataset evals/finance_v0.2.jsonl --case TRV-005 \
        --out evals/results/<name>.jsonl
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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
            os.environ["DEEPSEEK_API_KEY"] = line.split("=", 1)[1].strip()
            return
    raise SystemExit(f"no DEEPSEEK_API_KEY in {_ENV_LOCAL}")


def main() -> int:
    _load_credential()
    # DeepSeek composition, matching the ECS production env (2026-09-05;
    # id updated 2026-09-10 when V4.1 Flash renamed deepseek-flash).
    os.environ["MODEL_PROVIDER"] = "deepseek"
    os.environ["GLM_MODEL"] = "deepseek-flash"
    # A stale Zhipu credential or base URL must not leak into this run.
    for stale in ("ZAI_API_KEY", "GLM_OPENAI_BASE_URL", "GLM_CLASSIFIER_MODEL"):
        os.environ.pop(stale, None)

    from personal_agent.eval_cli import main as eval_main

    return eval_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
