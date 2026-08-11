#!/usr/bin/env python3
"""Re-freeze the independent authorities for one reviewed contract amendment.

Amendment: add `BLK-DRIFT--paused` to the frozen TransitionSpec registry.
Authorised by Henson on 2026-08-10, option (a) of contract gap G6.

Why the gap existed: contract §2.3 requires that a resume whose base SHA has
drifted lands in `needs_human` with `STATE_DRIFT`, and
`DAL-T-SM-001/paused_resume_base_drift` asserts exactly that. But the frozen
registry expanded `BLK-DRIFT` over eight from-states and omitted `paused`, and
§2.3.1 forbids an implementation adding an edge of its own. The registry was
therefore unable to express a transition the contract requires.

Why this script exists rather than a flag on the generator: the authority
documents are deliberately not written by `build_contract_manifests.py`
(see `verify_transition_oracle_authority.py`). Their whole value is that they
are a second, independent statement of what the contract says, so a registry
change must be re-frozen in a separate, reviewed act. This file is that act,
and it is committed so the amendment is auditable rather than implicit.

How independence is preserved: the new authority rows are derived by applying
the rule the other eight `BLK-DRIFT` edges already encode -- take a sibling
edge and substitute its from-state -- **not** by reading what the generator
produced. If the generator disagrees with the rule applied here, the verifiers
fail. That is the check working, and it is the reason this script must never be
"fixed" by copying from `manifests/` output.

Run once, then run the four `verify_*_authority.py` scripts.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from dal_jcs import canonical_bytes


ROOT = Path(__file__).resolve().parent
MANIFESTS = ROOT / "manifests"

#: The edge being added, and the sibling whose shape it copies.
NEW_STATE = "paused"
TEMPLATE_STATE = "approved"
FAMILY = "BLK-DRIFT"
NEW_SPEC_ID = f"{FAMILY}--{NEW_STATE}"
TEMPLATE_SPEC_ID = f"{FAMILY}--{TEMPLATE_STATE}"

#: Variant ids are `<family_prefix>--<spec_id lowercased, underscores hyphened>`.
VARIANT_PREFIXES = ("expanded_spec_allow", "actor_deny", "evidence_source_deny")

#: The registry hash before this amendment. Every pre-existing fixture pinned
#: it, and restoring it must reproduce the digest the authority already holds --
#: that is how "only the registry hash moved" is proven rather than assumed.
OLD_REGISTRY_SHA256 = (
    "c9ddb46cde67a9cdbc04294ee2e7b1299dea9b00384779471d4b62d2044ec0a8"
)


def _slug(spec_id: str) -> str:
    return spec_id.lower().replace("_", "-")


def _digest(document: dict, hash_field: str) -> str:
    body = {k: v for k, v in document.items() if k != hash_field}
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def _substitute(value: object, old: str, new: str) -> object:
    """Replace the from-state wherever the sibling entry names it."""
    if isinstance(value, str):
        if value == old:
            return new
        if _slug(old) in value:
            return value.replace(_slug(old), _slug(new))
        return value
    if isinstance(value, list):
        return [_substitute(item, old, new) for item in value]
    if isinstance(value, dict):
        return {k: _substitute(v, old, new) for k, v in value.items()}
    return value


def refreeze_transition_authority() -> str:
    """Add the new spec and its three oracle entries; return the new registry hash."""
    path = MANIFESTS / "transition-oracle-authority_v1.0.json"
    authority = json.loads(path.read_text(encoding="utf-8"))

    if NEW_SPEC_ID in authority["transition_specs"]:
        # Already re-frozen. Return the pinned hash so the manifest half can
        # still run: the two authorities are re-frozen independently, and a
        # rerun must be able to finish a partially completed amendment.
        return authority["frozen_transition_registry_sha256"]

    template_spec = authority["transition_specs"][TEMPLATE_SPEC_ID]
    new_spec = copy.deepcopy(template_spec)
    new_spec["spec_id"] = NEW_SPEC_ID
    new_spec["from_state"] = NEW_STATE
    authority["transition_specs"][NEW_SPEC_ID] = new_spec

    added = 0
    for prefix in VARIANT_PREFIXES:
        template_variant = f"{prefix}--{_slug(TEMPLATE_SPEC_ID)}"
        template_key = f"dal.oracle/DAL-T-SM-001/{template_variant}/G1/1.0"
        template_entry = authority["entries"][template_key]

        new_entry = _substitute(
            copy.deepcopy(template_entry), TEMPLATE_STATE, NEW_STATE
        )
        new_key = template_key.replace(_slug(TEMPLATE_SPEC_ID), _slug(NEW_SPEC_ID))
        authority["entries"][new_key] = new_entry
        added += 1

    counts = authority["semantic_counts"]
    counts["transition_specs"] += 1
    counts["transition_oracle_entries"] += added
    counts["complete_pre_state_command_result_entries"] += added
    counts["single_command_entries"] += added
    counts["resolver_command_objects"] += added

    # The registry hash this authority pins must be recomputed from the amended
    # registry itself, which is the one value that genuinely has to come from
    # the generated artifact -- it is a hash, not a semantic claim.
    registry = json.loads(
        (MANIFESTS / "transition-spec-registry_v1.0.json").read_text(encoding="utf-8")
    )
    registry_hash = _digest(registry, "registry_sha256")
    if registry_hash != registry["registry_sha256"]:
        raise SystemExit("regenerate the registry before re-freezing the authority")
    authority["frozen_transition_registry_sha256"] = registry_hash

    authority["authority_sha256"] = _digest(authority, "authority_sha256")
    path.write_text(
        json.dumps(authority, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return registry_hash


def _verified_digest(kind: str, variant_id: str, ref: str) -> str:
    """Hash a generated body, refusing unless the authority already vouches for it."""
    catalog_file, catalog_key = {
        "fixture": ("test-fixtures_v1.0.json", "fixtures"),
        "oracle": ("test-oracles_v1.0.json", "oracles"),
    }[kind]
    body = json.loads((MANIFESTS / catalog_file).read_text(encoding="utf-8"))[
        catalog_key
    ][ref]

    authority = json.loads(
        (MANIFESTS / "transition-oracle-authority_v1.0.json").read_text(
            encoding="utf-8"
        )
    )
    entry = authority["entries"][f"dal.oracle/DAL-T-SM-001/{variant_id}/G1/1.0"]

    if kind == "oracle":
        if body != entry["expected_result"]:
            raise SystemExit(f"generated oracle {ref} contradicts the authority")
    else:
        if body["pre_state"] != entry["pre_state"]:
            raise SystemExit(f"generated fixture {ref} has a different pre-state")
        if body.get("trusted_resolver_context") != entry["trusted_resolver_context"]:
            raise SystemExit(f"generated fixture {ref} has a different resolver context")
        if [body.get("transition_command")] != entry["resolver_input"]["commands"]:
            raise SystemExit(f"generated fixture {ref} has a different command")

    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def refreeze_manifest_authority() -> None:
    """Add the three manifest rows the new edge expands into."""
    path = MANIFESTS / "test-manifest-authority_v1.0.json"
    authority = json.loads(path.read_text(encoding="utf-8"))
    rows = authority["manifest_rows"]

    new_ids = [
        f"{prefix}--{_slug(NEW_SPEC_ID)}" for prefix in VARIANT_PREFIXES
    ]
    if isinstance(rows, dict):
        existing = rows
    else:
        raise SystemExit(f"unexpected manifest_rows shape: {type(rows)}")

    added = 0
    for prefix, new_variant in zip(VARIANT_PREFIXES, new_ids):
        template_variant = f"{prefix}--{_slug(TEMPLATE_SPEC_ID)}"
        template_key = f"DAL-T-SM-001/{template_variant}"
        if template_key not in existing:
            raise SystemExit(f"no template row {template_key}")
        new_key = template_key.replace(template_variant, new_variant)
        if new_key in existing:
            continue
        row = _substitute(
            copy.deepcopy(existing[template_key]), TEMPLATE_STATE, NEW_STATE
        )
        # The two content digests cannot be derived by substitution -- a hash
        # is a function of the bytes, not of the semantics. They are computed
        # from the fixture and oracle bodies, but only after those bodies have
        # been checked against this amendment's *own* independent statement of
        # them in the transition authority. So the authority still vouches for
        # the content; it is only the digest of that content that is read back.
        row["fixture_sha256"] = _verified_digest(
            "fixture", new_variant, row["fixture_ref"]
        )
        row["oracle_sha256"] = _verified_digest(
            "oracle", new_variant, row["oracle_id"]
        )
        existing[new_key] = row
        added += 1

    counts = authority["semantic_counts"]
    counts["manifest_rows"] += added
    counts["complete_expected_projection_rows"] += added
    counts["unique_fixture_refs"] += added
    counts["unique_oracle_ids"] += added
    counts["run_gate_counts"]["G1"] += added
    # SM-001 rows are jointly owned by DAL-009 and DAL-010.
    for owner in ("DAL-009", "DAL-010"):
        counts["owner_task_memberships"][owner] += added

    _repin_fixture_digests(existing)

    manifest = json.loads(
        (MANIFESTS / "test-manifest_v1.2.json").read_text(encoding="utf-8")
    )
    manifest_hash = _digest(manifest, "manifest_sha256")
    if manifest_hash != manifest["manifest_sha256"]:
        raise SystemExit("regenerate the manifest before re-freezing its authority")
    authority["frozen_manifest_sha256"] = manifest_hash

    authority["authority_sha256"] = _digest(authority, "authority_sha256")
    path.write_text(
        json.dumps(authority, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _repin_fixture_digests(rows: dict) -> None:
    """Re-pin every fixture digest the registry-hash change mechanically moved.

    Each fixture body embeds `transition_registry_sha256`, so amending the
    registry changes the *bytes* of all 1068 pre-existing fixtures while
    changing nothing they mean. Their digests therefore have to move too.

    This is the one place the re-freeze reads digests wholesale, so it is
    gated: a fixture may be re-pinned only if it pins the new registry hash and
    nothing else about it has drifted -- proven by requiring that restoring the
    *old* registry hash reproduces the digest the authority already holds. A
    fixture whose semantics changed will not reproduce it, and the re-freeze
    stops rather than blessing the change.
    """
    catalog = json.loads(
        (MANIFESTS / "test-fixtures_v1.0.json").read_text(encoding="utf-8")
    )
    fixtures = catalog["fixtures"]
    registry = json.loads(
        (MANIFESTS / "transition-spec-registry_v1.0.json").read_text(encoding="utf-8")
    )
    new_hash = registry["registry_sha256"]

    repinned = 0
    for key, row in rows.items():
        ref = row.get("fixture_ref")
        body = fixtures.get(ref)
        if body is None:
            raise SystemExit(f"authority row {key} names a missing fixture {ref}")
        actual = hashlib.sha256(canonical_bytes(body)).hexdigest()
        if actual == row["fixture_sha256"]:
            continue
        if body.get("transition_registry_sha256") != new_hash:
            raise SystemExit(
                f"fixture {ref} changed but does not pin the amended registry"
            )
        restored = copy.deepcopy(body)
        restored["transition_registry_sha256"] = OLD_REGISTRY_SHA256
        if hashlib.sha256(canonical_bytes(restored)).hexdigest() != row["fixture_sha256"]:
            raise SystemExit(
                f"fixture {ref} changed by more than the registry hash; "
                "re-freeze refuses to bless it"
            )
        row["fixture_sha256"] = actual
        repinned += 1
    print(json.dumps({"fixture_digests_repinned": repinned}, sort_keys=True))


def main() -> None:
    registry_hash = refreeze_transition_authority()
    refreeze_manifest_authority()
    print(
        json.dumps(
            {
                "amendment": NEW_SPEC_ID,
                "frozen_transition_registry_sha256": registry_hash,
                "re_frozen": [
                    "transition-oracle-authority_v1.0.json",
                    "test-manifest-authority_v1.0.json",
                ],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
