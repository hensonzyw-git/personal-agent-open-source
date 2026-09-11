"""Bind the verified MCP invocation to the calendar mirror query core.

Composition injects the dependencies; a handler never opens key material or
guesses a database path. The calendar read is credential-free (unlike the
Finance query): the mirror is this process's own database, so the only
dependencies are the session factory, the data keyring for the sealed text,
and the cursor secret.
"""

from __future__ import annotations

from typing import Callable

from personal_agent_core.crypto import KeyRing
from personal_agent_core.timeutil import utc_now
from personal_data_mcp.calendar.query_events import query_events
from personal_data_mcp.server.handlers import ToolHandler


class CalendarQueryDependencies:
    """The already-approved runtime inputs for the read-only calendar tool."""

    def __init__(
        self,
        *,
        sessions: Callable,
        keyring: KeyRing,
        cursor_secret: bytes,
    ) -> None:
        self.sessions = sessions
        self.keyring = keyring
        self.cursor_secret = cursor_secret


def build_handler(dependencies: CalendarQueryDependencies) -> ToolHandler:
    """Return the only handler allowed to expose ``calendar.query_events``."""

    async def handler(invocation) -> dict:
        return query_events(
            invocation.arguments,
            sessions=dependencies.sessions,
            keyring=dependencies.keyring,
            cursor_secret=dependencies.cursor_secret,
            now=utc_now(),
        )

    return handler
