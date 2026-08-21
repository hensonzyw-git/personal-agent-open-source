"""Load the frozen scoring policy. The policy dict is the single source of
truth for weights, bands, states and alert levels; it is versioned by
``policy_version`` and never mutated by the engine."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# Resolve from src/risk_monitor/scoring/policy.py up to the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_POLICY_PATH = _REPO_ROOT / "spec" / "scoring_policy.yml"


def load_policy(path: str | Path | None = None) -> dict[str, Any]:
    """Return the parsed policy dict. Callers must treat it as read-only."""
    target = Path(path) if path is not None else DEFAULT_POLICY_PATH
    with open(target, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def policy_version(policy: dict[str, Any]) -> str:
    return policy["policy_version"]
