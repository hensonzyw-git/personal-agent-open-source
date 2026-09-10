"""Governed Tool Bridge.

Technical design 4.4 fixes the set of tools a model may see and use:

    effective = global allowlist
              ∩ discovered
              ∩ contract schema hash matches
              ∩ device allowed tools
              ∩ device scopes
              ∩ (write kill switch enabled, for any effect other than read)

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

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from personal_agent.mcp_client.core import (
    DEFAULT_READ_CALL_TIMEOUT,
    DEFAULT_WRITE_CALL_TIMEOUT,
    McpClientCore,
)
from personal_agent.mcp_client.registry import CatalogEntry, ConnectorRegistry
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.host_context import (
    HostContext,
    ServiceKeyRing,
    declared_model_fields,
    sign_host_context,
    strip_host_only_fields,
)
from personal_agent_core.manifest import canonical_json, load_manifest
from personal_agent_core.write_switch import WriteSwitch


MAX_RESULT_BYTES = 64 * 1024
_SENSITIVE_RESULT_KEYS = frozenset(
    {
        "authorization",
        "accesstoken",
        "tenantaccesstoken",
        "refreshtoken",
        "pushtoken",
        "appsecret",
        "clientsecret",
        "apikey",
        "privatekey",
        "apptoken",
        "baseid",
        "tableid",
    }
)


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


@dataclass(frozen=True)
class BridgeCallContext:
    """Trusted per-call state supplied by the Agent API, never by the model."""

    host: HostContext
    signing_keys: ServiceKeyRing


@dataclass(frozen=True)
class BridgeExecutionResult:
    """Keep the App receipt separate from the redacted model-facing result."""

    trusted_result: dict[str, Any]
    model_result: dict[str, Any]


def _normalise_result(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return result
    if isinstance(result, list) and len(result) == 1:
        item = result[0]
        text = getattr(item, "text", None)
        if isinstance(text, str):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                return payload
    raise AppError(
        ErrorCode.INTERNAL_ERROR,
        internal_detail="MCP result is not one structured JSON object",
    )


def _result_key_fingerprint(key: str) -> str:
    return "".join(
        character for character in key.casefold() if character.isalnum()
    )


def _filter_sensitive_result(value: Any) -> Any:
    """Remove service credentials and resource identifiers recursively."""
    if isinstance(value, dict):
        return {
            key: _filter_sensitive_result(child)
            for key, child in value.items()
            if _result_key_fingerprint(key) not in _SENSITIVE_RESULT_KEYS
        }
    if isinstance(value, list):
        return [_filter_sensitive_result(child) for child in value]
    return value


class GovernedToolBridge:
    def __init__(
        self,
        registry: ConnectorRegistry,
        *,
        global_allowlist: frozenset[str],
        write_switch: WriteSwitch,
        clients: Mapping[str, McpClientCore] | None = None,
    ) -> None:
        self.registry = registry
        self.global_allowlist = global_allowlist
        self._write_switch = write_switch
        self._clients = dict(clients or {})
        manifest = load_manifest()
        self._contracts = {tool["name"]: tool for tool in manifest["tools"]}
        self.allowed_tools_version = manifest["allowed_tools_version"]

    def _contract(self, entry: CatalogEntry) -> dict[str, Any] | None:
        return self._contracts.get(entry.remote_name)

    def _contract_for_alias(self, alias: str) -> dict[str, Any] | None:
        """The trusted contract behind an alias, or None if there is no entry."""
        try:
            entry = self.registry.resolve(alias)
        except (KeyError, RuntimeError):
            return None
        return self._contract(entry)

    def _effective_aliases(self, device: DeviceAuthorization) -> set[str]:
        if device.status != "active":
            # A revoked device sees nothing, regardless of its token.
            return set()

        # Read once per intersection, not once per alias: a switch that flipped
        # between two aliases of the same catalog would otherwise produce a
        # catalog that was never a real state of the system.
        writes_allowed = self._write_switch.read().writes_allowed

        effective: set[str] = set()
        for alias in self.registry.all_aliases():
            entry = self.registry.resolve(alias)
            contract = self._contract(entry)
            if contract is None or not contract["enabled"]:
                continue
            if not writes_allowed and contract["effect"] != "read":
                # Design 10.6's first rollback unit: close every write tool and
                # keep read-only. Narrowing the catalog here means the model is
                # never shown a tool it cannot use, so it answers the user
                # instead of proposing a write that Finance would refuse.
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
        # Checked before the generic membership test purely so the refusal is
        # actionable. Everything else that removes a tool from the effective set
        # shares one opaque code, because distinguishing them would map out the
        # surface. The kill switch is different: it is the only user's own
        # deliberate act, and telling him "writes are off" is the whole point.
        contract_for_switch = self._contract_for_alias(alias)
        if contract_for_switch is not None and contract_for_switch["effect"] != "read":
            switch_state = self._write_switch.read()
            if not switch_state.writes_allowed:
                raise AppError(
                    ErrorCode.WRITES_DISABLED,
                    internal_detail=f"{alias} refused: {switch_state.detail}",
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
        # A host-only name this tool's own contract declares is the tool's
        # business field, not the Host's (see `declared_model_fields`), and it
        # must survive to the connector and into the argument hash.
        cleaned = strip_host_only_fields(
            arguments, declared=declared_model_fields(contract["model_input_schema"])
        )
        try:
            Draft202012Validator(
                contract["model_input_schema"],
                format_checker=FormatChecker(),
            ).validate(cleaned)
        except ValidationError as exc:
            raise AppError(
                ErrorCode.INVALID_ARGUMENT,
                internal_detail=(
                    f"{alias} input failed schema validation at "
                    f"{list(exc.absolute_path)}"
                ),
            ) from exc
        return entry, cleaned

    def adk_tools(
        self,
        *,
        device_provider: Callable[[], DeviceAuthorization],
        context_provider: Callable[
            [str, dict[str, Any], DeviceAuthorization, Any], BridgeCallContext
        ],
    ) -> list[Any]:
        """Build model tools from the current effective catalog.

        Each wrapper fetches the device again during `run_async`, so revocation
        or a scope change after catalog construction still takes effect.
        """
        # ADK remains an optional adapter dependency. Keeping this import lazy
        # lets policy, MCP and Finance services run without loading a model SDK.
        from personal_agent.policy.adk_bridge import build_adk_tools

        device = device_provider()
        return build_adk_tools(
            visible_tools=self.visible_tools(device),
            bridge=self,
            device_provider=device_provider,
            context_provider=context_provider,
        )

    async def execute(
        self,
        alias: str,
        arguments: dict[str, Any],
        device: DeviceAuthorization,
        *,
        call_context: BridgeCallContext,
    ) -> BridgeExecutionResult:
        """Authorize, bind, execute and filter one model-selected tool call."""
        entry, cleaned = self.authorize(alias, arguments, device)
        host = call_context.host
        if (
            host.tool != entry.remote_name
            or host.device_id != device.device_id
            or frozenset(host.scopes) != device.scopes
            or host.allowed_tools_version != device.allowed_tools_version
        ):
            raise AppError(
                ErrorCode.HOST_CONTEXT_MISMATCH,
                internal_detail="Bridge call context does not match authorization",
            )
        if not host.user_id or not host.trace_id or not host.timezone:
            raise AppError(
                ErrorCode.HOST_CONTEXT_MISMATCH,
                internal_detail="Bridge call context is missing identity or trace",
            )
        try:
            client = self._clients[entry.connector_id]
        except KeyError:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                internal_detail=f"no MCP client for connector {entry.connector_id}",
            ) from None

        token = sign_host_context(
            call_context.signing_keys,
            host,
            cleaned,
            # The verifier derives the same exemption from the same contract,
            # so a field the schema declares is hashed on both sides.
            declared=declared_model_fields(
                self._contracts[entry.remote_name]["model_input_schema"]
            ),
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Request-ID": host.request_id,
            "Idempotency-Key": host.idempotency_key,
            "traceparent": host.trace_id,
            "X-User-ID": host.user_id,
            "X-Timezone": host.timezone,
        }
        if host.duplicate_override is not None:
            # Sent on the Host channel only, and only because the same value is
            # inside the signature above. It is never added to `cleaned`.
            headers["X-Duplicate-Override"] = host.duplicate_override
        # The contract's own effect decides the budget: a create must be given
        # the write timeout, and a timeout on a write is an *unknown* commit
        # rather than a failure, which is what the Finance state machine
        # resolves. Reading it from the contract keeps a caller from quietly
        # giving a write a read-shaped deadline.
        effect = self._contracts[entry.remote_name]["effect"]
        raw_result = await client.call_tool(
            entry.remote_name,
            cleaned,
            timeout=(
                DEFAULT_READ_CALL_TIMEOUT
                if effect == "read"
                else DEFAULT_WRITE_CALL_TIMEOUT
            ),
            host_context=headers,
        )
        trusted = _normalise_result(raw_result)
        try:
            encoded = canonical_json(trusted).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                internal_detail=f"{alias} result is not canonical JSON",
            ) from exc
        if len(encoded) > MAX_RESULT_BYTES:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                internal_detail=(
                    f"{alias} result exceeded {MAX_RESULT_BYTES} bytes"
                ),
            )
        contract = self._contracts[entry.remote_name]
        try:
            Draft202012Validator(
                contract["output_schema"],
                format_checker=FormatChecker(),
            ).validate(trusted)
        except ValidationError as exc:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                internal_detail=(
                    f"{alias} output failed schema validation at "
                    f"{list(exc.absolute_path)}"
                ),
            ) from exc
        return BridgeExecutionResult(
            trusted_result=trusted,
            model_result=_filter_sensitive_result(trusted),
        )
