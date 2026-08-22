"""Load the frozen scoring policy. The policy dict is the single source of
truth for weights, bands, states and alert levels; it is versioned by
``policy_version`` and never mutated by the engine."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# Resolve the policy from the checkout first, then the packaged copy. The
# checkout keeps the canonical editable source at ``spec/scoring_policy.yml``
# (next to ADR-0001); ``pyproject.toml`` force-includes that same file into the
# wheel at ``risk_monitor/scoring/scoring_policy.yml`` so an installed wheel is
# self-contained. This mirrors ``personal_agent_core.evalset``.
_REPOSITORY_POLICY = Path(__file__).resolve().parents[3] / "spec" / "scoring_policy.yml"
_PACKAGED_POLICY = Path(__file__).resolve().with_name("scoring_policy.yml")
DEFAULT_POLICY_PATH = _REPOSITORY_POLICY if _REPOSITORY_POLICY.exists() else _PACKAGED_POLICY


def load_policy(path: str | Path | None = None) -> dict[str, Any]:
    """Return the parsed policy dict. Callers must treat it as read-only."""
    target = Path(path) if path is not None else DEFAULT_POLICY_PATH
    with open(target, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def policy_version(policy: dict[str, Any]) -> str:
    return policy["policy_version"]
