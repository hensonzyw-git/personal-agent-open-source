"""Console entrypoint for personal-agent-api."""

from __future__ import annotations


def main() -> None:
    """Refuse to start until the DEV-027 production composition root exists."""
    raise SystemExit(
        "personal-agent-api has no production composition root yet. "
        "The DEV-026 API and DEV-027 model adapter exist, but the governed "
        "Finance dispatcher and service wiring must be completed before startup."
    )
