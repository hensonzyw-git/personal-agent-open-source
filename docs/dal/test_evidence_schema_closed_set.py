#!/usr/bin/env python3
"""Prove evidence schemas are an explicit closed set with no generic fallback."""

from __future__ import annotations

import json
from pathlib import Path

from build_contract_manifests import evidence_claim_fields


ROOT = Path(__file__).resolve().parent


def main() -> None:
    registry = json.loads((ROOT / "manifests" / "evidence-schema-registry_v1.0.json").read_text(encoding="utf-8"))
    versions = [row["schema_version"] for row in registry["evidence_schemas"]]
    if len(versions) != 66 or len(versions) != len(set(versions)):
        raise AssertionError("evidence schema denominator drift")
    for version in versions:
        if not evidence_claim_fields(version):
            raise AssertionError(f"empty claim vocabulary: {version}")
    try:
        evidence_claim_fields("dal.evidence.future-unknown/1.0")
    except ValueError as error:
        if "explicit field freeze" not in str(error):
            raise
    else:
        raise AssertionError("unknown evidence schema reached a generic fallback")
    print(json.dumps({"explicit_evidence_schemas": len(versions), "unknown_schema": "REJECTED"}, sort_keys=True))


if __name__ == "__main__":
    main()
