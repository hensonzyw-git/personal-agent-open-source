"""Bind the verified MCP invocation to the calendar mirror ingest core.

The ingest is never a model choice — `model_callable=False` keeps it off the
Agent's allowlist, and the sync route calls it through the governed bridge
with a service-key Host Context. The handler therefore treats its arguments
exactly like any other MCP call: schema-verified upstream, merged here.
"""

from __future__ import annotations

from typing import Callable

from personal_agent_core.crypto import KeyRing
from personal_agent_core.timeutil import utc_now
from personal_data_mcp.calendar.ingest import ingest_events
from personal_data_mcp.server.handlers import ToolHandler


class CalendarIngestDependencies:
    """The already-approved runtime inputs for the mirror ingest."""

    def __init__(
        self,
        *,
        sessions: Callable,
        keyring: KeyRing,
        device_id: str,
    ) -> None:
        self.sessions = sessions
        self.keyring = keyring
        #: The uploading device, bound by composition from the verified caller
        #: identity rather than taken from arguments — a device cannot claim
        #: to be another device by re-serialising its payload.
        self.device_id = device_id


def build_handler(dependencies: CalendarIngestDependencies) -> ToolHandler:
    """Return the only handler allowed to expose ``calendar.ingest_events``."""

    async def handler(invocation) -> dict:
        return ingest_events(
            invocation.arguments,
            sessions=dependencies.sessions,
            keyring=dependencies.keyring,
            device_id=dependencies.device_id,
            now=utc_now(),
        )

    return handler
