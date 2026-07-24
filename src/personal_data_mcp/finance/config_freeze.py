"""Derive the protected annual-ledger configuration from a live Base, read-only.

Freezing an annual ledger is the chicken-and-egg step between DEV-016 and
DEV-018: the hardened probe validates against a config that names every real
field id, but those ids only exist in the Base. This command closes the loop
without a human ever transcribing an id -- and without this codebase opening
`.env.finance.local`, which stays the shell's job.

What it does, in order: mint a tenant token, list every field of the three
configured tables, resolve each *frozen logical field* to exactly one live field,
build the configuration, and re-validate the built configuration with the same
`validate_schema` the connector uses at write time. Only then is the file
written, with `O_EXCL` and mode 600.

Three things it deliberately does not do:

- **It never invents a contract.** Names, types, writability and option sets come
  from `EXPECTED_TABLE_FIELDS`; the live Base supplies field ids and nothing
  else. A live schema that disagrees is an error, not a new expectation.
- **It never freezes production.** The kind must be `synthetic_test` until G5,
  asserted from two independent inputs -- the operator's explicit flag and the
  injected environment -- so neither alone can widen the blast radius.
- **It never writes to Feishu.** Every call is the same read the G2 probe makes.

Its stdout is the redacted report only: ids appear as hashes, and the tenant
token is never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

from personal_data_mcp.feishu.adapter import FeishuAdapter
from personal_data_mcp.feishu.base_source import (
    BaseSource,
    SYNTHETIC_TEST_KIND,
    load_base_source,
)
from personal_data_mcp.feishu.credentials import load_credentials
from personal_data_mcp.feishu.redaction import redact_for_log
from personal_data_mcp.finance.ledger_config import (
    EXPECTED_TABLE_FIELDS,
    FieldType,
    LedgerConfig,
    load_ledger_config,
)
from personal_data_mcp.finance.redacted_report import redacted_schema_report
from personal_data_mcp.finance.schema_validator import (
    ObservedField,
    observed_field_from_feishu,
    validate_schema,
)


class ConfigFreezeError(RuntimeError):
    """The live schema cannot be frozen into a valid configuration.

    Its message names logical fields and tables only -- never a raw field id,
    Base token or provider body -- so it is safe to print.
    """


def resolve_field_id(
    table_name: str,
    logical_name: str,
    expected: tuple[str, FieldType, bool, tuple[str, ...] | None],
    observed_fields: list[ObservedField],
) -> str:
    """Find the one live field a frozen logical field maps to.

    Resolution is by the contract's expected Feishu name, because that is the
    only stable handle before any id is known. Everything else is then checked
    rather than adopted: a same-named field of the wrong type, a formula sitting
    where a writable field belongs, or a select whose options differ from the
    contract is a refusal, so the freeze cannot manufacture agreement.
    """
    expected_name, expected_type, writable, options = expected
    candidates = [f for f in observed_fields if f.name == expected_name]

    if not candidates:
        raise ConfigFreezeError(
            f"{table_name}.{logical_name}: no field named {expected_name!r}"
        )
    if len(candidates) > 1:
        raise ConfigFreezeError(
            f"{table_name}.{logical_name}: {len(candidates)} fields are named "
            f"{expected_name!r}; the mapping would be ambiguous"
        )

    observed = candidates[0]
    # Writability is checked before the type, the same precedence
    # `validate_table` uses, so a formula standing in for a writable field is
    # diagnosed as what it is rather than as a bare type mismatch.
    if writable and (observed.is_formula or observed.is_auto_number):
        raise ConfigFreezeError(
            f"{table_name}.{logical_name}: {expected_name!r} must be writable "
            "but is a formula or auto-number field"
        )
    if observed.type is not expected_type:
        raise ConfigFreezeError(
            f"{table_name}.{logical_name}: {expected_name!r} is not of the "
            f"expected type {expected_type.value}"
        )
    if not writable and expected_type is FieldType.FORMULA and not observed.is_formula:
        raise ConfigFreezeError(
            f"{table_name}.{logical_name}: {expected_name!r} is expected to be "
            "a formula field but is not"
        )
    if expected_type is FieldType.SINGLE_SELECT:
        if set(observed.options or ()) != set(options or ()):
            raise ConfigFreezeError(
                f"{table_name}.{logical_name}: the option set of "
                f"{expected_name!r} differs from the frozen contract; the "
                "connector never creates an option, so this must be fixed in "
                "Feishu"
            )
    if not observed.field_id:
        raise ConfigFreezeError(
            f"{table_name}.{logical_name}: {expected_name!r} has no field id"
        )
    return observed.field_id


def build_config(
    *,
    ledger_year: int,
    config_version: str,
    source: BaseSource,
    observed: dict[str, list[ObservedField]],
) -> LedgerConfig:
    """Assemble the configuration document and validate it end to end.

    The document is round-tripped through `load_ledger_config`, so it is proven
    parseable exactly as the connector will later load it, and then checked with
    the write-time validator against the same observation. A config that only
    validates in memory would not be evidence of anything.
    """
    tables: dict[str, Any] = {}
    for table_name, expected_fields in EXPECTED_TABLE_FIELDS.items():
        observed_fields = observed.get(table_name, [])
        fields: dict[str, Any] = {}
        for logical_name, expected in expected_fields.items():
            expected_name, expected_type, writable, options = expected
            field: dict[str, Any] = {
                "id": resolve_field_id(
                    table_name, logical_name, expected, observed_fields
                ),
                "expected_name": expected_name,
                "type": expected_type.value,
                "writable": writable,
            }
            if options is not None:
                field["options"] = list(options)
            fields[logical_name] = field
        tables[table_name] = {
            "table_id": source.tables[table_name],
            "fields": fields,
        }

    config = load_ledger_config(
        {
            "ledger_year": ledger_year,
            "ledger_kind": source.ledger_kind,
            "config_version": config_version,
            "base_token": source.base_token,
            "effective_from": date(ledger_year, 1, 1).isoformat(),
            "effective_to": date(ledger_year, 12, 31).isoformat(),
            "tables": tables,
        }
    )

    validation = validate_schema(config, observed)
    if not validation.is_valid:
        drifts = ", ".join(
            f"{d.table}.{d.logical_name} {d.kind.value}" for d in validation.drifts
        )
        raise ConfigFreezeError(
            f"the derived configuration does not validate against the live "
            f"schema: {drifts}"
        )
    return config


def require_freezable_kind(source: BaseSource, *, declared_kind: str) -> None:
    """Refuse to freeze anything but the synthetic test Base, from two inputs.

    There is no protected config to bind against yet -- this command is what
    produces one -- so the environment marker alone would be the only guard.
    Requiring the operator to declare the kind on the command line as well means
    a stray or copied marker cannot by itself point a freeze at a real ledger.
    Production is refused outright: that is a G5 decision, not a CLI flag.
    """
    if declared_kind != SYNTHETIC_TEST_KIND:
        raise ConfigFreezeError(
            f"refusing to freeze kind {declared_kind!r}: only "
            f"{SYNTHETIC_TEST_KIND!r} may be frozen before G5"
        )
    if source.ledger_kind != declared_kind:
        raise ConfigFreezeError(
            "refusing to freeze: the injected environment's ledger kind does "
            "not match the declared kind"
        )


async def freeze(
    env: dict[str, str] | None = None,
    *,
    ledger_year: int,
    config_version: str,
    declared_kind: str,
) -> tuple[LedgerConfig, dict[str, Any]]:
    """Run the read-only fetch and return the config plus its redacted report."""
    credentials = load_credentials(env)
    source = load_base_source(env)
    require_freezable_kind(source, declared_kind=declared_kind)

    observed: dict[str, list[ObservedField]] = {}
    async with FeishuAdapter(credentials, now=time.monotonic) as adapter:
        for table_name, table_id in source.tables.items():
            fields = await adapter.list_fields(source.base_token, table_id)
            observed[table_name] = [
                observed_field_from_feishu(field) for field in fields
            ]

    config = build_config(
        ledger_year=ledger_year,
        config_version=config_version,
        source=source,
        observed=observed,
    )
    return config, redacted_schema_report(config, observed)


def write_protected(config: LedgerConfig, out: Path) -> None:
    """Write the config at mode 600, never over an existing file.

    A frozen config is a versioned, reviewable artefact: silently replacing one
    would erase the very change a checksum exists to make visible. Removing the
    old file is a deliberate act the operator has to take.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    document = json.dumps(
        config.model_dump(mode="json"), ensure_ascii=False, indent=2
    )
    try:
        fd = os.open(out, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ConfigFreezeError(
            f"{out} already exists; remove it deliberately before re-freezing"
        ) from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(document + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only: derive the protected annual-ledger config from the live "
            "synthetic test Base. Writes no Feishu data and prints only a "
            "redacted report."
        )
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--ledger-year", type=int, required=True)
    parser.add_argument("--config-version", required=True)
    parser.add_argument(
        "--ledger-kind",
        required=True,
        help=(
            "must be 'synthetic_test' and must match the injected environment; "
            "production is a G5 decision and is refused here"
        ),
    )
    args = parser.parse_args()

    try:
        config, report = asyncio.run(
            freeze(
                ledger_year=args.ledger_year,
                config_version=args.config_version,
                declared_kind=args.ledger_kind,
            )
        )
        write_protected(config, args.out)
    except ConfigFreezeError as exc:
        print(redact_for_log(f"freeze refused: {exc}"), file=sys.stderr)
        sys.exit(1)

    print(redact_for_log(json.dumps(report, ensure_ascii=False, indent=2)))
    sys.exit(0)
