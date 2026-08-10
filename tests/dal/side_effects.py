"""Observed side effects for the DAL test harness.

The frozen oracles carry `forbidden_side_effects`: a config load must not make
a provider call, a GitHub write, a Worker start or a production access. That
cannot be proven by the absence of a test failure; it needs an explicit record
of which external boundaries the code under test actually crossed.

Production code does not know about this probe. The harness installs it as the
boundary the DAL component is wired against (a stub Feishu/provider/GitHub
boundary, an instrumented filesystem, etc.), and each stub records the boundary
it guards. After execution the harness compares the observed set against the
oracle's forbidden set: any overlap is a fail, and an observed effect the
oracle did not list is surfaced rather than ignored.

Test-only module.
"""

from __future__ import annotations

from dataclasses import dataclass, field


#: The closed set of external boundaries the DAL-007–013 slice can observe.
#: These mirror the forbidden_side_effects vocabulary of the frozen oracles.
KNOWN_SIDE_EFFECTS: frozenset[str] = frozenset(
    {
        "provider_call",
        "github_write",
        "worker_start",
        "production_access",
        "network_call",
        "filesystem_write",
        "process_spawn",
    }
)


@dataclass
class SideEffectProbe:
    """Collects the external boundaries crossed during one operation."""

    _observed: list[str] = field(default_factory=list)

    def record(self, effect: str) -> None:
        """Record that an external boundary was crossed."""
        if effect not in KNOWN_SIDE_EFFECTS:
            raise ValueError(f"unknown side effect boundary: {effect!r}")
        self._observed.append(effect)

    @property
    def observed(self) -> frozenset[str]:
        return frozenset(self._observed)

    def forbidden_crossings(self, forbidden: list[str]) -> frozenset[str]:
        """The observed effects the oracle forbade. Empty means the test may pass."""
        return self.observed & frozenset(forbidden)


def fresh_probe() -> SideEffectProbe:
    """A probe with nothing observed yet."""
    return SideEffectProbe()
