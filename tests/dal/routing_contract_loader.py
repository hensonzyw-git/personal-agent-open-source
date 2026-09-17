"""Hash-bound loading of the frozen routing-handoff contracts.

The routing handoff (`DAL-T-ROUTING-CONTRACT-001`) is a separate manifest family
from the controller-dispatch graph: its fixtures and oracles are catalog-hash
bound rather than per-row bound, and its manifest rows carry `fixture_ref` /
`oracle_id` without individual `fixture_sha256` / `oracle_sha256` fields.  This
loader verifies the catalog self-hashes and that the manifest's declared
`fixture_catalog_sha256` / `oracle_catalog_sha256` match, then binds each row to
its fixture and oracle body.

The binding is `sha256(RFC 8785 JCS(body))`, matching the generator
(`docs/dal/build_routing_manifests.py`) and the shared canonicaliser.  A drifted
artifact is a hard failure, never a silent pass.

Test-only module.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from tests.dal.contract_loader import (
    MANIFESTS_DIR,
    ContractBindingError,
    content_hash,
)

ROUTING_MANIFEST: Final[str] = "routing-manifest_v1.0.json"
ROUTING_FIXTURES: Final[str] = "routing-fixtures_v1.0.json"
ROUTING_ORACLES: Final[str] = "routing-oracles_v1.0.json"


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ContractBindingError(f"missing frozen routing artifact: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ContractBindingError(
            f"frozen routing artifact is not a JSON object: {path}"
        )
    return data


def _catalog_hash(document: dict[str, Any]) -> str:
    """The catalog self-hash: the whole document minus its `catalog_sha256`."""
    return content_hash({k: v for k, v in document.items() if k != "catalog_sha256"})


@dataclass(frozen=True)
class RoutingVariant:
    """One routing manifest row plus its hash-bound fixture and oracle."""

    test_id: str
    variant_id: str
    run_gate: str
    owner_tasks: tuple[str, ...]
    fixture: dict[str, Any]
    oracle: dict[str, Any]


class RoutingContracts:
    """The frozen routing manifest, fixtures and oracles, hash-verified at load."""

    def __init__(self, manifests_dir: Path = MANIFESTS_DIR) -> None:
        self._manifest = _load_json(manifests_dir / ROUTING_MANIFEST)
        self._fixtures = _load_json(manifests_dir / ROUTING_FIXTURES)
        self._oracles = _load_json(manifests_dir / ROUTING_ORACLES)
        self._verify()

    def _verify(self) -> None:
        if _catalog_hash(self._fixtures) != self._fixtures.get("catalog_sha256"):
            raise ContractBindingError("routing fixture catalog hash drift")
        if _catalog_hash(self._oracles) != self._oracles.get("catalog_sha256"):
            raise ContractBindingError("routing oracle catalog hash drift")
        if (
            self._manifest.get("fixture_catalog_sha256")
            != self._fixtures.get("catalog_sha256")
        ):
            raise ContractBindingError("fixture catalog not bound by routing manifest")
        if (
            self._manifest.get("oracle_catalog_sha256")
            != self._oracles.get("catalog_sha256")
        ):
            raise ContractBindingError("oracle catalog not bound by routing manifest")

    def variants(self, test_id: str) -> list[RoutingVariant]:
        """All hash-bound routing variants for one test id, in manifest order."""
        rows = [
            row for row in self._manifest["test_variants"] if row["test_id"] == test_id
        ]
        if not rows:
            raise ContractBindingError(f"no routing manifest rows for test id: {test_id}")

        fixtures = self._fixtures["fixtures"]
        oracles = self._oracles["oracles"]
        variants: list[RoutingVariant] = []
        for row in rows:
            fixture = fixtures.get(row["fixture_ref"])
            oracle = oracles.get(row["oracle_id"])
            if fixture is None or oracle is None:
                raise ContractBindingError(
                    f"routing row unresolved: {row.get('variant_id')!r}"
                )
            variants.append(
                RoutingVariant(
                    test_id=row["test_id"],
                    variant_id=row["variant_id"],
                    run_gate=row["run_gate"],
                    owner_tasks=tuple(row["owner_tasks"]),
                    fixture=fixture,
                    oracle=oracle,
                )
            )
        return variants
