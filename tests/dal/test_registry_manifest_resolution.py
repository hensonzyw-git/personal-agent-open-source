"""The frozen-contract resolution must work in BOTH deployment shapes.

R10 T1 intake (2026-09-09): the first real `apply_transition` on the ECS
wheel deploy raised `RegistryError: missing frozen registry` —
`MANIFESTS_DIR` resolved via `Path(__file__).parents[3]`, which is the
source-checkout shape; a wheel lands in `site-packages`, three parents up
is `.venv/lib`, and the checkout's `docs/dal` tree does not exist there.
R09-B F5 never hit this because the sweep only ran read-only
reconciliation over an empty table; intake is the first write path.

The contract: the manifests are packaged inside
`personal_agent_dal/frozen_contracts/` (force-included at wheel build
time) and the checkout path remains the fallback for the source tree.
Whichever copy the loader opens, it hash-verifies it — a drifted copy is
a hard failure either way. These tests pin: (a) the checkout shape still
loads and equals the packaged copy, (b) the packaged copy itself loads
through the real loader, (c) with neither present the loader fails
closed.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from personal_agent_dal.machine.registry import (
    RegistryError,
    transition_registry,
    _CHECKOUT_CONTRACTS_DIR,
    _PACKAGED_CONTRACTS_DIR,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKOUT_MANIFESTS = REPO_ROOT / "docs" / "dal" / "manifests"


def _packaged_manifests() -> Path:
    return _PACKAGED_CONTRACTS_DIR / "manifests"


def test_checkout_contracts_dir_exists_and_is_used_when_no_package() -> None:
    """The source tree keeps working: the checkout path is the fallback."""
    assert CHECKOUT_MANIFESTS.is_dir(), "repo must carry docs/dal/manifests"


def test_packaged_contracts_load_through_the_real_loader() -> None:
    """The wheel-shape copy is real package data and loads hash-verified.

    This is the deployment shape the ECS intake hit: the loader must accept
    the packaged copy (`src/personal_agent_dal/frozen_contracts/`) without
    any checkout nearby. The copy ships as package data, so its absence in
    the source tree is itself a defect — asserted, not silently repaired.
    """
    packaged = _packaged_manifests()
    assert packaged.is_dir(), (
        "frozen_contracts package data missing — the wheel deploy would "
        "fail closed exactly like the R10 T1 intake did"
    )
    assert (_PACKAGED_CONTRACTS_DIR / "dal_jcs.py").is_file(), (
        "the packaged canonicaliser must ship next to the manifests"
    )
    registry_module = importlib.import_module("personal_agent_dal.machine.registry")
    registry_module.transition_registry.cache_clear()
    registry_module.guard_registry.cache_clear()
    specs = transition_registry()
    assert specs, "the packaged transition registry must load non-empty"
    registry_module.transition_registry.cache_clear()
    registry_module.guard_registry.cache_clear()


def test_packaged_and_checkout_copies_are_byte_identical() -> None:
    """If both shapes exist they must be the same bytes — one frozen truth.

    The wheel build force-includes docs/dal/manifests verbatim, so a
    mismatch here means the packaging or the checkout drifted from the
    frozen contract: fail loudly instead of silently verifying one copy
    while the other ships.
    """
    packaged = _packaged_manifests()
    if not packaged.is_dir():
        pytest.skip("no packaged copy present (wheel not built in this tree)")
    checkout_files = {p.name: p for p in CHECKOUT_MANIFESTS.glob("*.json")}
    packaged_files = {p.name: p for p in packaged.glob("*.json")}
    assert set(checkout_files) == set(packaged_files), (
        "packaged and checkout manifest sets diverge"
    )
    for name, checkout_path in checkout_files.items():
        assert checkout_path.read_bytes() == packaged_files[name].read_bytes(), (
            f"{name} differs between checkout and packaged copy"
        )


def test_missing_contracts_everywhere_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither shape present -> RegistryError, never a silent default.

    `_contracts_dir` is the resolution point at import time; the loaders
    read `MANIFESTS_DIR`. Patching the resolved dir to an absent path must
    make the loader raise "missing frozen registry" — a deploy whose
    package is missing its contract files fails closed at first load, not
    with a silently empty allowlist.
    """
    import personal_agent_dal.machine.registry as registry_module

    monkeypatch.setattr(
        registry_module, "_PACKAGED_CONTRACTS_DIR", tmp_path / "absent-packaged"
    )
    monkeypatch.setattr(
        registry_module, "_CHECKOUT_CONTRACTS_DIR", tmp_path / "absent-checkout"
    )
    with pytest.raises(RegistryError, match="not found"):
        registry_module._contracts_dir()
    monkeypatch.setattr(registry_module, "MANIFESTS_DIR", tmp_path / "absent-manifests")
    # The registries are lru_cached "verified once" — clear so this test
    # actually exercises the load path instead of a prior test's hit.
    registry_module.transition_registry.cache_clear()
    registry_module.guard_registry.cache_clear()
    with pytest.raises(RegistryError, match="missing frozen registry"):
        registry_module.transition_registry()
    registry_module.transition_registry.cache_clear()
    registry_module.guard_registry.cache_clear()
