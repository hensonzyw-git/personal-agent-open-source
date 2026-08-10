"""Hash-bound loading of the frozen DAL test contracts.

The Development Agent Loop's tests are not written from the same assumptions
as the code: they replay the frozen fixtures and oracles under
`docs/dal/manifests/`. That only means something if the test is bound to the
exact frozen artifact it claims to exercise, so this loader verifies the
manifest-declared content hash of every fixture and oracle before returning
it. A drifted artifact is a hard failure, never a silent pass.

The binding is `sha256(RFC 8785 JCS(body))`, matching the generator
(`docs/dal/build_contract_manifests.py`) and the shared canonicaliser
(`docs/dal/dal_jcs.py`). The algorithm is verified against the manifest's own
declared hashes at load time, so this loader cannot quietly disagree with the
frozen contract about which bytes are authoritative.

Test-only module: it lives under `tests/`, never on the production path.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final


#: Repository root, resolved from this file: tests/dal/contract_loader.py.
REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
MANIFESTS_DIR: Final[Path] = REPO_ROOT / "docs" / "dal" / "manifests"


def _load_canonical_bytes():
    """Import the shared RFC 8785 canonicaliser from its file path.

    `docs/` is not a Python package, so the shared `dal_jcs.py` is loaded by
    path. Using the generator's own canonicaliser -- rather than a second
    implementation here -- is what guarantees the test recomputes the exact
    hash the frozen contract was bound with.
    """
    module_path = REPO_ROOT / "docs" / "dal" / "dal_jcs.py"
    spec = importlib.util.spec_from_file_location("dal_jcs", module_path)
    if spec is None or spec.loader is None:
        raise ContractBindingError(f"cannot load canonicaliser: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.canonical_bytes


canonical_bytes = _load_canonical_bytes()

_TEST_MANIFEST: Final[str] = "test-manifest_v1.2.json"
_TEST_FIXTURES: Final[str] = "test-fixtures_v1.0.json"
_TEST_ORACLES: Final[str] = "test-oracles_v1.0.json"


class ContractBindingError(RuntimeError):
    """A frozen artifact failed its manifest-declared content hash."""


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ContractBindingError(f"missing frozen artifact: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ContractBindingError(f"frozen artifact is not a JSON object: {path}")
    return data


def content_hash(body: dict[str, Any]) -> str:
    """The canonical content hash the manifest binds a fixture or oracle by."""
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


@dataclass(frozen=True)
class BoundFixture:
    """A fixture whose body hash matched the manifest row that named it."""

    ref: str
    body: dict[str, Any]


@dataclass(frozen=True)
class BoundOracle:
    """An oracle whose body hash matched the manifest row that named it."""

    oracle_id: str
    body: dict[str, Any]


@dataclass(frozen=True)
class TestVariant:
    """One manifest row plus its hash-bound fixture and oracle."""

    test_id: str
    variant_id: str
    run_gate: str
    owner_tasks: tuple[str, ...]
    fixture: BoundFixture
    oracle: BoundOracle
    expected_receipt_code: str | None
    expected_entity_state: str | None
    expected_entity_type: str | None
    injection_point: str | None


class FrozenContracts:
    """The frozen manifest, fixtures and oracles, hash-verified at load."""

    def __init__(self, manifests_dir: Path = MANIFESTS_DIR) -> None:
        self._manifest = _load_json(manifests_dir / _TEST_MANIFEST)
        self._fixtures = _load_json(manifests_dir / _TEST_FIXTURES)["fixtures"]
        self._oracles = _load_json(manifests_dir / _TEST_ORACLES)["oracles"]

    def variants(self, test_id: str) -> list[TestVariant]:
        """All hash-bound variants for one test id, in manifest order."""
        rows = [
            row
            for row in self._manifest["test_variants"]
            if row["test_id"] == test_id
        ]
        if not rows:
            raise ContractBindingError(f"no manifest rows for test id: {test_id}")
        return [self._bind(row) for row in rows]

    def _bind(self, row: dict[str, Any]) -> TestVariant:
        fixture = self._bind_fixture(row)
        oracle = self._bind_oracle(row)
        return TestVariant(
            test_id=row["test_id"],
            variant_id=row["variant_id"],
            run_gate=row["run_gate"],
            owner_tasks=tuple(row["owner_tasks"]),
            fixture=fixture,
            oracle=oracle,
            expected_receipt_code=row.get("expected_receipt_code"),
            expected_entity_state=row.get("expected_entity_state"),
            expected_entity_type=row.get("expected_entity_type"),
            injection_point=row.get("injection_point"),
        )

    def _bind_fixture(self, row: dict[str, Any]) -> BoundFixture:
        ref = row["fixture_ref"]
        body = self._fixtures.get(ref)
        if body is None:
            raise ContractBindingError(f"fixture ref not present in catalog: {ref}")
        declared = row["fixture_sha256"]
        actual = content_hash(body)
        if actual != declared:
            raise ContractBindingError(
                f"fixture hash drift for {ref}: manifest {declared[:16]}… "
                f"!= recomputed {actual[:16]}…"
            )
        return BoundFixture(ref=ref, body=body)

    def _bind_oracle(self, row: dict[str, Any]) -> BoundOracle:
        oracle_id = row["oracle_id"]
        body = self._oracles.get(oracle_id)
        if body is None:
            raise ContractBindingError(
                f"oracle id not present in catalog: {oracle_id}"
            )
        declared = row["oracle_sha256"]
        actual = content_hash(body)
        if actual != declared:
            raise ContractBindingError(
                f"oracle hash drift for {oracle_id}: manifest {declared[:16]}… "
                f"!= recomputed {actual[:16]}…"
            )
        return BoundOracle(oracle_id=oracle_id, body=body)
