"""Connector registry and tool catalog.

A tool's real identity is `(connector_id, remote_tool_name, schema_hash)`, per
technical design 6.1. The model only ever sees an alias, and the alias is minted
here from a trusted manifest rather than taken from the server. That direction
matters: if the server's own name could reach back and select a connector, a
second server could claim `finance.log_expense` and be routed the real one.

Anything the server says about itself is untrusted metadata. A tool whose schema
does not match the manifest, or that the manifest has never heard of, goes to
quarantine. Quarantine is visible for diagnosis and is never a source of tools
for the model.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from mcp.types import Tool

from personal_agent_core.manifest import canonical_json, load_manifest


class TrustLevel(StrEnum):
    """Only a trusted connector keeps its business names unprefixed."""

    PERSONAL_DATA = "personal_data"
    THIRD_PARTY = "third_party"


class QuarantineReason(StrEnum):
    UNKNOWN_TOOL = "unknown_tool"
    SCHEMA_MISMATCH = "schema_mismatch"
    DISABLED_IN_MANIFEST = "disabled_in_manifest"


def schema_hash(schema: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(schema).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CatalogEntry:
    """One discovered tool that passed manifest verification."""

    connector_id: str
    remote_name: str
    alias: str
    schema_hash: str
    input_schema: dict[str, Any]
    description: str


@dataclass(frozen=True)
class QuarantinedEntry:
    connector_id: str
    remote_name: str
    reason: QuarantineReason
    detail: str


@dataclass
class ConnectorCatalog:
    connector_id: str
    trust_level: TrustLevel
    entries: dict[str, CatalogEntry] = field(default_factory=dict)
    quarantined: list[QuarantinedEntry] = field(default_factory=list)
    catalog_hash: str = ""

    def aliases(self) -> set[str]:
        return set(self.entries)


class ConnectorRegistry:
    """All connectors, and the alias namespace shared between them."""

    def __init__(self) -> None:
        self._catalogs: dict[str, ConnectorCatalog] = {}
        self._manifest = load_manifest()
        self._by_name = {
            tool["name"]: tool for tool in self._manifest["tools"]
        }

    @property
    def connector_ids(self) -> tuple[str, ...]:
        return tuple(self._catalogs)

    def catalog(self, connector_id: str) -> ConnectorCatalog:
        return self._catalogs[connector_id]

    def _alias_for(
        self, connector_id: str, trust_level: TrustLevel, remote_name: str
    ) -> str:
        """Business names stay readable only for the Personal Data connector.

        Every other server is namespaced, so a third party cannot present a tool
        under a name the model has learned to associate with the ledger.
        """
        if trust_level is TrustLevel.PERSONAL_DATA:
            return remote_name
        return f"{connector_id}__{remote_name}"

    def refresh(
        self,
        connector_id: str,
        *,
        trust_level: TrustLevel,
        discovered: list[Tool],
    ) -> ConnectorCatalog:
        """Rebuild one connector's catalog from a fresh discovery.

        Replaces rather than merges: a tool the server has removed must vanish
        from the model's view, and merging would keep it alive forever.
        """
        catalog = ConnectorCatalog(
            connector_id=connector_id, trust_level=trust_level
        )

        for tool in discovered:
            alias = self._alias_for(connector_id, trust_level, tool.name)
            observed_hash = schema_hash(tool.input_schema)

            if trust_level is TrustLevel.PERSONAL_DATA:
                contract = self._by_name.get(tool.name)
                if contract is None:
                    catalog.quarantined.append(
                        QuarantinedEntry(
                            connector_id,
                            tool.name,
                            QuarantineReason.UNKNOWN_TOOL,
                            "not present in the trusted manifest",
                        )
                    )
                    continue
                if not contract["enabled"]:
                    catalog.quarantined.append(
                        QuarantinedEntry(
                            connector_id,
                            tool.name,
                            QuarantineReason.DISABLED_IN_MANIFEST,
                            "the manifest ships this tool disabled",
                        )
                    )
                    continue
                if observed_hash != contract["input_schema_hash"]:
                    catalog.quarantined.append(
                        QuarantinedEntry(
                            connector_id,
                            tool.name,
                            QuarantineReason.SCHEMA_MISMATCH,
                            "advertised schema does not match the contract",
                        )
                    )
                    continue

            catalog.entries[alias] = CatalogEntry(
                connector_id=connector_id,
                remote_name=tool.name,
                alias=alias,
                schema_hash=observed_hash,
                input_schema=tool.input_schema,
                description=tool.description or "",
            )

        catalog.catalog_hash = hashlib.sha256(
            canonical_json(
                sorted(
                    [entry.alias, entry.schema_hash]
                    for entry in catalog.entries.values()
                )
            ).encode("utf-8")
        ).hexdigest()
        self._catalogs[connector_id] = catalog
        return catalog

    def resolve(self, alias: str) -> CatalogEntry:
        """Map a model-visible alias back to exactly one connector and tool."""
        matches = [
            catalog.entries[alias]
            for catalog in self._catalogs.values()
            if alias in catalog.entries
        ]
        if not matches:
            raise KeyError(f"no catalog entry for alias {alias!r}")
        if len(matches) > 1:
            # Cannot happen while aliases are namespaced, and is a hard failure
            # rather than a pick-the-first if it ever does.
            raise RuntimeError(
                f"alias {alias!r} is claimed by "
                f"{[entry.connector_id for entry in matches]}"
            )
        return matches[0]

    def all_aliases(self) -> set[str]:
        aliases: set[str] = set()
        for catalog in self._catalogs.values():
            overlap = aliases & catalog.aliases()
            if overlap:
                raise RuntimeError(f"alias collision across connectors: {overlap}")
            aliases |= catalog.aliases()
        return aliases

    def quarantined(self) -> list[QuarantinedEntry]:
        return [
            entry
            for catalog in self._catalogs.values()
            for entry in catalog.quarantined
        ]
