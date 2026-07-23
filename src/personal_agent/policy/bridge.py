"""Governed Tool Bridge.

Technical design 4.4 fixes the set of tools a model may see and use:

    effective = global allowlist
              ∩ discovered
              ∩ contract schema hash matches
              ∩ device allowed tools
              ∩ device scopes

The same intersection is recomputed when the model's catalog is built and again
immediately before execution. Computing it once and caching it would mean a
device revoked mid-conversation could still execute, which is the failure the
short token TTL is specifically not relied upon to prevent.

Two separate checks, deliberately not one:

- `visible_tools` decides what the model is told exists;
- `authorize` decides what may run. A tool that is invisible is also
  unexecutable, so a model that guesses a name it was never shown gains nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from personal_agent.mcp_client.registry import CatalogEntry, ConnectorRegistry
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.host_context import strip_host_only_fields
from personal_agent_core.manifest import load_manifest


@dataclass(frozen=True)
class DeviceAuthorization:
    """What one device is currently allowed to do.

    Read fresh from the device row on every call. This is a snapshot passed in,
    never a cache owned by the bridge.
    """

    device_id: str
    status: str
    scopes: frozenset[str]
    allowed_tools: frozenset[str]
    allowed_tools_version: str


@dataclass(frozen=True)
class VisibleTool:
    alias: str
    description: str
    input_schema: dict[str, Any]
    risk_level: str
    required_scopes: tuple[str, ...]


class GovernedToolBridge:
    def __init__(
        self,
        registry: ConnectorRegistry,
        *,
        global_allowlist: frozenset[str],
    ) -> None:
        self.registry = registry
        self.global_allowlist = global_allowlist
        manifest = load_manifest()
        self._contracts = {tool["name"]: tool for tool in manifest["tools"]}
        self.allowed_tools_version = manifest["allowed_tools_version"]

    def _contract(self, entry: CatalogEntry) -> dict[str, Any] | None:
        return self._contracts.get(entry.remote_name)

    def _effective_aliases(self, device: DeviceAuthorization) -> set[str]:
        if device.status != "active":
            # A revoked device sees nothing, regardless of its token.
            return set()

        effective: set[str] = set()
        for alias in self.registry.all_aliases():
            entry = self.registry.resolve(alias)
            contract = self._contract(entry)
            if contract is None or not contract["enabled"]:
                continue
            if alias not in self.global_allowlist:
                continue
            if entry.schema_hash != contract["input_schema_hash"]:
                continue
            if alias not in device.allowed_tools:
                continue
            if not set(contract["required_scopes"]).issubset(device.scopes):
                continue
            effective.add(alias)
        return effective

    def visible_tools(self, device: DeviceAuthorization) -> list[VisibleTool]:
        """The catalog handed to the model.

        Descriptions come from the trusted manifest, not from the server. A
        server's own description is untrusted metadata and must never become
        instructions the model reads.
        """
        visible: list[VisibleTool] = []
        for alias in sorted(self._effective_aliases(device)):
            entry = self.registry.resolve(alias)
            contract = self._contracts[entry.remote_name]
            visible.append(
                VisibleTool(
                    alias=alias,
                    description=contract["summary"],
                    input_schema=contract["model_input_schema"],
                    risk_level=contract["risk_level"],
                    required_scopes=tuple(contract["required_scopes"]),
                )
            )
        return visible

    def authorize(
        self, alias: str, arguments: dict[str, Any], device: DeviceAuthorization
    ) -> tuple[CatalogEntry, dict[str, Any]]:
        """Re-check immediately before execution, or raise.

        Returns the resolved connector entry and the cleaned arguments. Host
        injected fields present in model output are dropped here, so nothing
        downstream has to remember to do it.
        """
        if device.status != "active":
            raise AppError(
                ErrorCode.SCOPE_DENIED,
                internal_detail=f"device {device.device_id} is {device.status}",
            )
        if alias not in self._effective_aliases(device):
            # One code for "not allowlisted", "not discovered", "schema drifted"
            # and "not granted to this device": distinguishing them for the
            # caller would map out the tool surface.
            raise AppError(
                ErrorCode.TOOL_NOT_ALLOWLISTED,
                internal_detail=f"{alias} is not effective for {device.device_id}",
            )

        entry = self.registry.resolve(alias)
        contract = self._contracts[entry.remote_name]
        if not set(contract["required_scopes"]).issubset(device.scopes):
            raise AppError(
                ErrorCode.SCOPE_DENIED,
                internal_detail=f"{alias} needs {contract['required_scopes']}",
            )
        if device.allowed_tools_version != self.allowed_tools_version:
            raise AppError(
                ErrorCode.SCOPE_DENIED,
                internal_detail="device carries a stale allowed_tools_version",
            )
        return entry, strip_host_only_fields(arguments)
