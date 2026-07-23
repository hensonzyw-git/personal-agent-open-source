"""The annual-ledger onboarding command: validate a snapshot, emit a report.

Credential-free by construction. It reads a protected config and a saved field
snapshot -- a JSON file with, per configured table, the Feishu list-fields
entries -- and reports whether the schema is valid or drifted. With `--redacted`
it prints the committable report instead of the raw outcome.

The snapshot is produced by the credentialed onboarding fetch (DEV-017, behind
G2). Keeping validation a separate, offline step means the same logic runs in a
test against a fixture snapshot and in production against a real one, and the raw
snapshot never has to be committed to exercise it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from personal_data_mcp.finance.ledger_config import load_ledger_config
from personal_data_mcp.finance.redacted_report import redacted_schema_report
from personal_data_mcp.finance.schema_validator import (
    ObservedField,
    observed_field_from_feishu,
    validate_schema,
)


def observed_from_snapshot(
    snapshot: dict[str, Any],
) -> dict[str, list[ObservedField]]:
    """Turn a raw `{table: [feishu field, ...]}` snapshot into observed fields."""
    return {
        table: [observed_field_from_feishu(field) for field in fields]
        for table, fields in snapshot.items()
    }


def run(config_path: Path, snapshot_path: Path, *, redacted: bool) -> int:
    config = load_ledger_config(json.loads(config_path.read_text("utf-8")))
    snapshot = json.loads(snapshot_path.read_text("utf-8"))
    observed = observed_from_snapshot(snapshot)
    validation = validate_schema(config, observed)

    if redacted:
        report = redacted_schema_report(
            config, observed, validation=validation
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"config_version: {config.config_version}")
        print(f"checksum: {config.checksum()}")
        print(f"status: {validation.status}")
        for drift in validation.drifts:
            print(f"  drift: {drift.table}.{drift.logical_name} {drift.kind.value}")

    # A drifted schema is a non-zero exit so a deploy step fails closed.
    return 0 if validation.is_valid else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate an annual ledger schema against its configuration."
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument(
        "--redacted",
        action="store_true",
        help="print the committable redacted report instead of the raw status",
    )
    args = parser.parse_args()
    sys.exit(run(args.config, args.snapshot, redacted=args.redacted))
