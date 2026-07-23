"""The tool handler registry, and what the server is willing to advertise.

The catalog is the intersection of two things: the enabled contracts in the
trusted manifest, and the tools this build actually has a handler for. Both
halves matter.

Taking only enabled contracts keeps a disabled tool such as
`finance.log_expense_batch` from being discoverable at all, rather than
discoverable and refusing. Taking only tools with a handler is the same idea
applied to work that has not been built yet: at `DEV-015` the Finance write
tools have no connector behind them, so advertising them would invite the model
to call something that cannot succeed. A tool appears here when it can actually
execute, which makes each later task's gate mean something concrete.

A handler registered for a name that is not an enabled contract is a
programming error and is refused at registration, not at call time.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final, TYPE_CHECKING

from personal_agent_core.manifest import load_manifest

if TYPE_CHECKING:
    from personal_data_mcp.server.authz import VerifiedCall


@dataclass(frozen=True)
class ToolInvocation:
    """One authorised call, as the handler sees it."""

    tool: str
    arguments: dict[str, Any]
    verified_call: "VerifiedCall"


ToolHandler = Callable[[ToolInvocation], Awaitable[dict[str, Any]]]


class ToolRegistrationError(ValueError):
    """A handler was registered for something the contract does not allow."""


def enabled_contracts() -> dict[str, dict[str, Any]]:
    """The enabled contracts from the checked-in manifest, keyed by name."""
    manifest = load_manifest()
    return {
        entry["name"]: entry
        for entry in manifest["tools"]
        if entry["enabled"]
    }


class ToolRegistry:
    """Which tools this server can execute."""

    def __init__(self) -> None:
        self._contracts: Final[dict[str, dict[str, Any]]] = enabled_contracts()
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, name: str, handler: ToolHandler) -> None:
        if name not in self._contracts:
            raise ToolRegistrationError(
                f"{name} is not an enabled contract in the trusted manifest"
            )
        if name in self._handlers:
            raise ToolRegistrationError(f"{name} already has a handler")
        self._handlers[name] = handler

    def handler(self, name: str) -> ToolHandler | None:
        return self._handlers.get(name)

    def contract(self, name: str) -> dict[str, Any] | None:
        """The trusted contract, but only for a tool this server can execute."""
        if name not in self._handlers:
            return None
        return self._contracts[name]

    def names(self) -> frozenset[str]:
        return frozenset(self._handlers)

    def catalog(self) -> list[dict[str, Any]]:
        """What `tools/list` advertises, in manifest order."""
        return [
            {
                "name": entry["name"],
                "description": entry["summary"],
                "inputSchema": entry["model_input_schema"],
            }
            for name, entry in self._contracts.items()
            if name in self._handlers
        ]
