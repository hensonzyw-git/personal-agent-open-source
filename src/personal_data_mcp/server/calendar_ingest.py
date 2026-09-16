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
    ) -> None:
        self.sessions = sessions
        self.keyring = keyring


def build_handler(dependencies: CalendarIngestDependencies) -> ToolHandler:
    """Return the only handler allowed to expose ``calendar.ingest_events``."""

    async def handler(invocation) -> dict:
        # The uploading device is the verified caller, not a payload field: it
        # is stamped from the signed Host Context claims the authorizer
        # checked, so a device cannot claim to be another device by
        # re-serialising its payload, and a compromised server-side caller is
        # recorded as exactly what it is.
        return ingest_events(
            invocation.arguments,
            sessions=dependencies.sessions,
            keyring=dependencies.keyring,
            device_id=invocation.verified_call.device_id,
            # Same source as the device identity above and for the same reason:
            # the client protocol version is what the signed Host Context says
            # it is (design §2.5). The barrier refuses a client below the
            # channel's floor, so believing a copy out of the payload would let
            # the payload decide whether it may be written.
            client_wire_version=invocation.verified_call.client_wire_version,
            now=utc_now(),
        )

    return handler
