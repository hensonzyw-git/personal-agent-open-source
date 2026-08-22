"""Minimal local-env loading for the risk monitor.

``.env.local`` (mode 600, gitignored) holds ``FRED_API_KEY``. Loaded
idempotently — never overwrites an already-set variable.
"""

from __future__ import annotations

import os
from pathlib import Path

_REPO_ENV = Path(__file__).resolve().parents[2] / ".env.local"


def _candidates(path: str | Path | None) -> list[Path]:
    if path is not None:
        return [Path(path)]
    # Checkout first, then CWD. On the Mac the source tree finds the repo-root
    # ``.env.local``; on ECS the wheel lives under site-packages so that path is
    # wrong, and the operator's ``.env.local`` (mode 600, /opt/personal-agent) is
    # found relative to the current working directory instead.
    return [_REPO_ENV, Path.cwd() / ".env.local"]


def load_dotenv_local(path: str | Path | None = None) -> None:
    for target in _candidates(path):
        if not target.exists():
            continue
        for line in target.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())
