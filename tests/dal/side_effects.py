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

import builtins
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
import http.client
import io
import os
import socket
import sqlite3
import subprocess
from collections.abc import Iterator
from unittest.mock import patch
import urllib.request


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


class ForbiddenSideEffectError(RuntimeError):
    """A supposedly pure DAL handler crossed an external boundary."""


@contextmanager
def guard_pure_policy(probe: SideEffectProbe) -> Iterator[None]:
    """Fail before a pure policy can touch file, DB, process, or network I/O.

    The guard wraps only the production handler invocation, after fixtures have
    already been loaded.  Each blocked boundary records the attempted effect so
    the oracle trace is based on observation rather than an untouched probe.
    """

    def blocked(effect: str):
        def refuse(*_args, **_kwargs):
            probe.record(effect)
            raise ForbiddenSideEffectError(
                f"pure policy crossed forbidden boundary: {effect}"
            )

        return refuse

    patches = (
        patch.object(builtins, "open", blocked("filesystem_write")),
        patch.object(io, "open", blocked("filesystem_write")),
        patch.object(sqlite3, "connect", blocked("filesystem_write")),
        patch.object(os, "system", blocked("process_spawn")),
        patch.object(subprocess, "Popen", blocked("process_spawn")),
        patch.object(subprocess, "run", blocked("process_spawn")),
        patch.object(socket, "create_connection", blocked("network_call")),
        patch.object(urllib.request, "urlopen", blocked("network_call")),
        patch.object(http.client.HTTPConnection, "connect", blocked("network_call")),
        patch.object(http.client.HTTPSConnection, "connect", blocked("network_call")),
    )
    with ExitStack() as stack:
        for boundary_patch in patches:
            stack.enter_context(boundary_patch)
        yield
