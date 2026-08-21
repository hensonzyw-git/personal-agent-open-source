"""Minimal local-env loading for the risk monitor.

``.env.local`` (mode 600, gitignored) holds ``FRED_API_KEY``. Loaded
idempotently — never overwrites an already-set variable.
"""

from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_PATH = _REPO_ROOT / ".env.local"


def load_dotenv_local(path: str | Path | None = None) -> None:
    target = Path(path) if path is not None else DEFAULT_ENV_PATH
    if not target.exists():
        return
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())
