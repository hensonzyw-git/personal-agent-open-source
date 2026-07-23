"""Console entrypoint for personal-agent-api."""

from __future__ import annotations


def main() -> None:
    """Refuse to start: DEV-001 only delivers the package skeleton."""
    raise SystemExit(
        "personal-agent-api is a DEV-001 package skeleton with no runnable service. "
        "DEV-026 implements it; until then there is nothing to start."
    )
