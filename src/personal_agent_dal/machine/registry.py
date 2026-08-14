"""The frozen TransitionSpec and guard registries, loaded and hash-verified.

Contract §2.3.1 is explicit that the registry is an exhaustive allowlist and
that an implementation may not add transitions of its own. The strongest way to
honour that is to have no hand-written transition table at all: the service
loads the frozen registry and can therefore only do what the registry says.

Loading is hash-verified against each registry's own declared
`registry_sha256`, recomputed with the same RFC 8785 canonicaliser the
generator used. A drifted registry is a hard failure at startup, not a subtly
different state machine at runtime.

Resolution is by the exact tuple
`(aggregate_type, from_state, command_type, target_state, effect_outcome,
owner_aggregate_type, decision_action, reason_code)`. Wildcards are forbidden
by §2.3.1 — each `from_state` is its own immutable `spec_id` — and the tuple
above is the smallest key that is unique across all 276 specs.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from personal_agent_dal.errors import DalError, DalErrorCode


#: The frozen contract package. Read-only at runtime.
MANIFESTS_DIR: Final[Path] = (
    Path(__file__).resolve().parents[3] / "docs" / "dal" / "manifests"
)

TRANSITION_REGISTRY: Final[str] = "transition-spec-registry_v1.0.json"
GUARD_REGISTRY: Final[str] = "guard-predicate-registry_v1.0.json"

TRANSITION_SPEC_SCHEMA: Final[str] = "dal.transition-spec/1.0"
GUARD_PREDICATE_SCHEMA: Final[str] = "dal.guard-predicate/1.0"


class RegistryError(RuntimeError):
    """A frozen registry is missing, malformed or does not match its hash."""


def _canonical_bytes():
    """The generator's own RFC 8785 canonicaliser, loaded by path.

    `docs/` is not a package. Using the generator's implementation rather than
    a second one here is what guarantees the service recomputes the same digest
    the registry was frozen with, instead of agreeing with itself.
    """
    module_path = MANIFESTS_DIR.parent / "dal_jcs.py"
    spec = importlib.util.spec_from_file_location("dal_jcs_runtime", module_path)
    if spec is None or spec.loader is None:
        raise RegistryError(f"cannot load canonicaliser: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.canonical_bytes


def jcs_sha256(value: Any) -> str:
    """The RFC 8785 JCS SHA-256 digest the frozen contract binds by.

    Contract §3.2 / §3.2.1 freeze `state_sha256` and `artifact_sha256` as
    ``SHA-256(RFC 8785 JCS UTF-8 bytes)`` of the binding object. Recomputing
    that digest here — from the same canonicaliser the generator and the
    registry hash check share — is what lets a binding validator agree with the
    frozen `protected_binding_sha256` instead of with itself.

    Raises the canonicaliser's ``JCSCanonicalizationError`` (a ``ValueError``)
    for any value outside I-JSON; callers that must fail closed on a malformed
    binding catch that and refuse rather than repair.
    """
    return hashlib.sha256(_canonical_bytes()(value)).hexdigest()


def _load_verified(filename: str, body_key: str) -> list[dict[str, Any]]:
    """Load one registry and verify its declared `registry_sha256`."""
    path = MANIFESTS_DIR / filename
    if not path.is_file():
        raise RegistryError(f"missing frozen registry: {path}")
    with path.open("r", encoding="utf-8") as handle:
        document = json.load(handle)

    declared = document.get("registry_sha256")
    if not isinstance(declared, str):
        raise RegistryError(f"{filename} declares no registry_sha256")
    body = document.get(body_key)
    if not isinstance(body, list) or not body:
        raise RegistryError(f"{filename} has no {body_key}")

    # The generator hashes the whole document with the hash field removed, so
    # the digest also covers `schema_version` and any sibling field. Hashing
    # only the rows would leave the registry's own version unauthenticated.
    unhashed = {k: v for k, v in document.items() if k != "registry_sha256"}
    actual = hashlib.sha256(_canonical_bytes()(unhashed)).hexdigest()
    if actual != declared:
        raise RegistryError(
            f"{filename} hash drift: declared {declared[:16]}… != "
            f"recomputed {actual[:16]}…"
        )
    return body


@dataclass(frozen=True)
class ResolutionKey:
    """The exact tuple that identifies one spec. No wildcards, by contract."""

    aggregate_type: str
    from_state: str | None
    command_type: str
    target_state: str | None
    effect_outcome: str | None
    owner_aggregate_type: str | None
    decision_action: str | None
    reason_code: str | None


def _spec_key(spec: dict[str, Any]) -> ResolutionKey:
    parameters = spec["command_parameters"]
    reasons = spec["allowed_reason_codes"]
    if len(reasons) > 1:
        # §3.6: each expanded spec carries exactly one reason. More than one
        # would mean the registry expects a runtime branch to complete it,
        # which the contract forbids.
        raise RegistryError(
            f"spec {spec['spec_id']} declares {len(reasons)} reason codes"
        )
    return ResolutionKey(
        aggregate_type=spec["aggregate_type"],
        from_state=spec["from_state"],
        command_type=spec["command_type"],
        target_state=parameters.get("target_state"),
        effect_outcome=parameters.get("effect_outcome"),
        owner_aggregate_type=parameters.get("owner_aggregate_type"),
        decision_action=spec["requires_decision_action"],
        reason_code=reasons[0] if reasons else None,
    )


class TransitionRegistry:
    """The exhaustive, immutable set of permitted transitions."""

    def __init__(self, specs: list[dict[str, Any]]) -> None:
        self._by_id: dict[str, dict[str, Any]] = {}
        self._by_key: dict[ResolutionKey, dict[str, Any]] = {}
        for spec in specs:
            if spec.get("schema_version") != TRANSITION_SPEC_SCHEMA:
                raise RegistryError(
                    f"spec {spec.get('spec_id')} has unsupported schema "
                    f"{spec.get('schema_version')!r}"
                )
            spec_id = spec["spec_id"]
            if spec_id in self._by_id:
                raise RegistryError(f"duplicate spec_id: {spec_id}")
            self._by_id[spec_id] = spec
            key = _spec_key(spec)
            if key in self._by_key:
                raise RegistryError(
                    f"two specs resolve identically: {self._by_key[key]['spec_id']} "
                    f"and {spec_id}"
                )
            self._by_key[key] = spec

    def __len__(self) -> int:
        return len(self._by_id)

    @property
    def spec_ids(self) -> tuple[str, ...]:
        return tuple(self._by_id)

    def by_id(self, spec_id: str) -> dict[str, Any]:
        try:
            return self._by_id[spec_id]
        except KeyError:
            raise RegistryError(f"unknown spec_id: {spec_id}") from None

    def resolve(self, key: ResolutionKey) -> dict[str, Any] | None:
        """The spec for this exact key, or `None`. Never a fuzzy match."""
        return self._by_key.get(key)

    def specs_for(
        self, aggregate_type: str, from_state: str
    ) -> tuple[dict[str, Any], ...]:
        """Every spec leaving one state. Used to tell "illegal" from "denied"."""
        return tuple(
            spec
            for spec in self._by_id.values()
            if spec["aggregate_type"] == aggregate_type
            and spec["from_state"] == from_state
        )


class GuardRegistry:
    """The frozen guard predicates, keyed by `guard_id`."""

    def __init__(self, guards: list[dict[str, Any]]) -> None:
        self._by_id: dict[str, dict[str, Any]] = {}
        for guard in guards:
            if guard.get("predicate_schema_version") != GUARD_PREDICATE_SCHEMA:
                raise RegistryError(
                    f"guard {guard.get('guard_id')} has unsupported schema "
                    f"{guard.get('predicate_schema_version')!r}"
                )
            self._by_id[guard["guard_id"]] = guard

    def __len__(self) -> int:
        return len(self._by_id)

    def by_id(self, guard_id: str) -> dict[str, Any]:
        try:
            return self._by_id[guard_id]
        except KeyError:
            # An unknown guard is never "no guard". A spec that names a
            # predicate the service does not have must refuse, or the guard
            # silently becomes optional.
            raise DalError(
                DalErrorCode.INTERNAL_ERROR,
                internal_detail=f"spec names an unknown guard: {guard_id}",
            ) from None


@lru_cache(maxsize=1)
def transition_registry() -> TransitionRegistry:
    """The process-wide transition registry, verified once at first use."""
    return TransitionRegistry(_load_verified(TRANSITION_REGISTRY, "specs"))


@lru_cache(maxsize=1)
def guard_registry() -> GuardRegistry:
    """The process-wide guard registry, verified once at first use."""
    return GuardRegistry(_load_verified(GUARD_REGISTRY, "guards"))
