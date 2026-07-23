"""A committable, credential-free view of a ledger's schema shape.

The raw field snapshot names the Base, tables and fields, so it is not safe to
commit. This report is: it keeps the shape a reviewer needs -- field names,
types, formula flags, option sets, the validation outcome -- and replaces every
resource identifier (`base_token`, `table_id`, `field_id`) with a stable hash.
The category option names themselves are not secret; they are already in the
frozen contract, so they are shown in full.

"Stable" means the same id always hashes to the same short token within a
report, so structure stays legible, while the raw id does not appear. This is a
review and fixture aid, not a security boundary against someone who already has
the ids.
"""

from __future__ import annotations

import hashlib
from typing import Any

from personal_data_mcp.finance.ledger_config import LedgerConfig
from personal_data_mcp.finance.schema_validator import (
    ObservedField,
    SchemaValidation,
    validate_schema,
)


_HASH_PREFIX = "h:"


def _hash_id(raw: str) -> str:
    return _HASH_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def redacted_schema_report(
    config: LedgerConfig,
    observed: dict[str, list[ObservedField]],
    *,
    validation: SchemaValidation | None = None,
) -> dict[str, Any]:
    """Build the redacted report for a config and its observed snapshot."""
    validation = validation or validate_schema(config, observed)
    drift_index: dict[tuple[str, str], list[str]] = {}
    for drift in validation.drifts:
        drift_index.setdefault((drift.table, drift.logical_name), []).append(
            drift.kind.value
        )

    tables: dict[str, Any] = {}
    for table_name, table_config in config.tables.items():
        observed_by_id = {
            field.field_id: field for field in observed.get(table_name, [])
        }
        fields: dict[str, Any] = {}
        for logical_name, spec in table_config.fields.items():
            observed_field = observed_by_id.get(spec.id)
            fields[logical_name] = {
                "field_id_hash": _hash_id(spec.id),
                "expected_name": spec.expected_name,
                "type": spec.type.value,
                "options": list(spec.options) if spec.options else None,
                "observed_present": observed_field is not None,
                "observed_is_formula": (
                    observed_field.is_formula if observed_field else None
                ),
                "observed_is_auto_number": (
                    observed_field.is_auto_number if observed_field else None
                ),
                "drifts": drift_index.get((table_name, logical_name), []),
            }
        tables[table_name] = {
            "table_id_hash": _hash_id(table_config.table_id),
            "fields": fields,
        }

    return {
        "ledger_year": config.ledger_year,
        "ledger_kind": config.ledger_kind,
        "config_version": config.config_version,
        "config_checksum": config.checksum(),
        "base_token_hash": _hash_id(config.base_token),
        "status": validation.status,
        "tables": tables,
    }
