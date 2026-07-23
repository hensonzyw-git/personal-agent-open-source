"""Validate a ledger's live schema against the protected configuration.

The connector locates a field by its id and then checks everything else: the
name it currently has, its type, whether it is a formula or auto-number field
the connector must never write, and for a single-select the exact option set
(technical design 9.2). Any difference makes the schema drifted, and a drifted
schema fails every write closed. Nothing here creates a field or an option.

This module is credential-free. It validates an already-fetched field list, not
a live Base: the fetch belongs to DEV-017, behind G2. The Feishu field-type
codes used by `observed_field_from_feishu` are the documented ones and are
re-confirmed against the live API when the connector is wired.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from personal_data_mcp.finance.ledger_config import (
    FieldType,
    LedgerConfig,
)


#: Feishu Bitable field-type codes -> the connector's field types. Only the
#: types the connector uses are mapped; anything else stays unsupported, which is
#: itself a drift if a configured field turns out to be one.
_FEISHU_TYPE_CODES: Final[dict[int, FieldType]] = {
    1: FieldType.TEXT,
    2: FieldType.NUMBER,
    3: FieldType.SINGLE_SELECT,
    5: FieldType.DATETIME,
    7: FieldType.CHECKBOX,
}
_FORMULA_CODE: Final[int] = 20
_AUTO_NUMBER_CODE: Final[int] = 1005


class DriftKind(StrEnum):
    MISSING_FIELD = "missing_field"
    NAME_CHANGED = "name_changed"
    TYPE_CHANGED = "type_changed"
    NOT_WRITABLE = "not_writable"
    OPTIONS_CHANGED = "options_changed"


@dataclass(frozen=True)
class ObservedField:
    """One field as the Base currently defines it."""

    field_id: str
    name: str
    type: FieldType | None
    options: tuple[str, ...] | None = None
    is_formula: bool = False
    is_auto_number: bool = False


@dataclass(frozen=True)
class Drift:
    """One reason a table's schema does not match the configuration.

    `detail` names the logical field and the kind of change, never a raw Base or
    field id: this is designed to be safe to log and audit.
    """

    table: str
    logical_name: str
    kind: DriftKind
    detail: str


@dataclass(frozen=True)
class SchemaValidation:
    config_version: str
    drifts: tuple[Drift, ...]

    @property
    def status(self) -> str:
        return "valid" if not self.drifts else "drifted"

    @property
    def is_valid(self) -> bool:
        return not self.drifts


def observed_field_from_feishu(field: dict[str, Any]) -> ObservedField:
    """Normalise one Feishu list-fields entry.

    Feishu returns `field_id`, `field_name`, an integer `type`, and a `property`
    that carries select `options` (each with a `name`) and formula details. A
    type code the connector does not use maps to `type=None`, which a configured
    field will read as a type change rather than being silently accepted.
    """
    code = field.get("type")
    property_ = field.get("property") or {}
    is_formula = code == _FORMULA_CODE
    is_auto_number = code == _AUTO_NUMBER_CODE
    mapped = _FEISHU_TYPE_CODES.get(code) if isinstance(code, int) else None
    if is_formula:
        mapped = FieldType.FORMULA

    options: tuple[str, ...] | None = None
    if mapped is FieldType.SINGLE_SELECT:
        raw_options = property_.get("options") or []
        options = tuple(
            option["name"] for option in raw_options if "name" in option
        )

    return ObservedField(
        field_id=str(field.get("field_id", "")),
        name=str(field.get("field_name", "")),
        type=mapped,
        options=options,
        is_formula=is_formula,
        is_auto_number=is_auto_number,
    )


def validate_table(
    table_name: str,
    table_config,
    observed_fields: list[ObservedField],
) -> list[Drift]:
    by_id = {field.field_id: field for field in observed_fields}
    drifts: list[Drift] = []

    for logical_name, spec in table_config.fields.items():
        observed = by_id.get(spec.id)
        if observed is None:
            drifts.append(
                Drift(
                    table_name,
                    logical_name,
                    DriftKind.MISSING_FIELD,
                    f"{logical_name} is not present under its configured id",
                )
            )
            continue

        # A writable field must not be a formula or auto-number field: writing
        # one is rejected by Feishu, and the design forbids ever attempting it.
        if spec.writable and (
            observed.is_formula or observed.is_auto_number
        ):
            drifts.append(
                Drift(
                    table_name,
                    logical_name,
                    DriftKind.NOT_WRITABLE,
                    f"{logical_name} is a formula or auto-number field",
                )
            )
            continue

        if observed.name != spec.expected_name:
            drifts.append(
                Drift(
                    table_name,
                    logical_name,
                    DriftKind.NAME_CHANGED,
                    f"{logical_name} name no longer matches the configuration",
                )
            )

        if observed.type is not spec.type:
            drifts.append(
                Drift(
                    table_name,
                    logical_name,
                    DriftKind.TYPE_CHANGED,
                    f"{logical_name} type no longer matches the configuration",
                )
            )
            # Options only make sense once the type agrees.
            continue

        if spec.type is FieldType.SINGLE_SELECT:
            expected = set(spec.options or ())
            actual = set(observed.options or ())
            if expected != actual:
                drifts.append(
                    Drift(
                        table_name,
                        logical_name,
                        DriftKind.OPTIONS_CHANGED,
                        f"{logical_name} option set differs from the configuration",
                    )
                )

    return drifts


def validate_schema(
    config: LedgerConfig,
    observed: dict[str, list[ObservedField]],
) -> SchemaValidation:
    """Validate every configured table against its observed fields.

    A configured table with no observed entry at all is treated as every one of
    its fields being missing, so a wrong or empty snapshot fails closed rather
    than validating vacuously.
    """
    drifts: list[Drift] = []
    for table_name, table_config in config.tables.items():
        observed_fields = observed.get(table_name, [])
        drifts.extend(validate_table(table_name, table_config, observed_fields))
    return SchemaValidation(
        config_version=config.config_version, drifts=tuple(drifts)
    )
