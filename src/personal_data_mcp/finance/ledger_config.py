"""The protected annual-ledger configuration and its checksum.

A new annual ledger is a server-side configuration change, not a new app or a
prompt change: it names the Base, the tables and every field id, name, type and
option set the connector is allowed to touch (technical design 9.1). This module
models that configuration and computes a checksum over it, so a change is
detectable, auditable and rollback-versioned rather than silent.

Secrets do not live here. `app_id`, `app_secret`, the internal JWT key and the
payload-encryption key are injected separately as systemd credentials; the only
resource identifier this config carries is the Base token and the ids, which are
sensitive but not secret. The `--redacted` report replaces even those with
stable hashes so a schema shape can be reviewed and committed as a fixture while
the raw snapshot is not.

The configuration is stored as JSON rather than the YAML shown illustratively in
the design, so the credential-holding service needs no YAML runtime dependency
(the production package must import without the optional model SDKs).
"""

from __future__ import annotations

import hashlib
from datetime import date
from enum import StrEnum
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from personal_agent_core.manifest import canonical_json
from personal_agent_core.tool_ir import ALLOWED_EXPENSE_CATEGORIES


class FieldType(StrEnum):
    """The Feishu Bitable field types the connector writes or reads."""

    NUMBER = "number"
    TEXT = "text"
    DATETIME = "datetime"
    CHECKBOX = "checkbox"
    SINGLE_SELECT = "single_select"
    FORMULA = "formula"


class FieldSpec(BaseModel):
    """One expected field: located by id, validated by everything else."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    expected_name: str = Field(min_length=1)
    type: FieldType
    writable: bool = True
    #: Present only for a single-select field. The exact option set the ledger
    #: is expected to have; the connector never creates an option, so a
    #: difference is drift, not something to reconcile by writing.
    options: tuple[str, ...] | None = None

    @model_validator(mode="after")
    def _options_only_for_select(self) -> "FieldSpec":
        if self.type is FieldType.SINGLE_SELECT and self.options is None:
            raise ValueError(f"single-select field {self.id} needs an option set")
        if self.type is not FieldType.SINGLE_SELECT and self.options is not None:
            raise ValueError(
                f"field {self.id} of type {self.type} must not carry options"
            )
        return self


class TableConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    table_id: str = Field(min_length=1)
    #: Logical field name -> expected field. Logical names are the connector's,
    #: not Feishu's; the Feishu name is validated against `expected_name`.
    fields: dict[str, FieldSpec] = Field(min_length=1)


class LedgerConfig(BaseModel):
    """The whole protected configuration for one annual ledger."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_year: int = Field(ge=2000, le=2100)
    ledger_kind: Literal["synthetic_test", "production"]
    config_version: str = Field(min_length=1)
    base_token: str = Field(min_length=1)
    effective_from: date
    effective_to: date
    tables: dict[str, TableConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _matches_the_frozen_ledger_contract(self) -> "LedgerConfig":
        expected_tables = set(EXPECTED_TABLE_FIELDS)
        if set(self.tables) != expected_tables:
            raise ValueError(
                f"tables must be exactly {sorted(expected_tables)}"
            )
        if self.effective_from != date(self.ledger_year, 1, 1):
            raise ValueError("effective_from must be the ledger year's first day")
        if self.effective_to != date(self.ledger_year, 12, 31):
            raise ValueError("effective_to must be the ledger year's last day")
        if len({table.table_id for table in self.tables.values()}) != len(
            self.tables
        ):
            raise ValueError("table ids must be unique across the ledger")

        for table_name, expected_fields in EXPECTED_TABLE_FIELDS.items():
            table = self.tables[table_name]
            if set(table.fields) != set(expected_fields):
                raise ValueError(
                    f"{table_name} fields must be exactly "
                    f"{sorted(expected_fields)}"
                )
            if len({field.id for field in table.fields.values()}) != len(
                table.fields
            ):
                raise ValueError(f"{table_name} field ids must be unique")
            for logical_name, expected in expected_fields.items():
                field = table.fields[logical_name]
                name, type_, writable, options = expected
                if (
                    field.expected_name != name
                    or field.type is not type_
                    or field.writable is not writable
                    or field.options != options
                ):
                    raise ValueError(
                        f"{table_name}.{logical_name} does not match "
                        "the frozen field contract"
                    )
        return self

    def checksum(self) -> str:
        """A stable checksum over the whole configuration.

        Any change -- a new field id, a renamed expectation, a shifted option
        set -- changes this value, which is what makes a config change a
        reviewable, versioned event.
        """
        return hashlib.sha256(
            canonical_json(self.model_dump(mode="json")).encode("utf-8")
        ).hexdigest()


#: The expense category options every annual config must expect, taken from the
#: frozen contract so the config cannot drift from what the model may send.
EXPECTED_EXPENSE_CATEGORIES: Final[tuple[str, ...]] = ALLOWED_EXPENSE_CATEGORIES
EXPECTED_INCOME_CATEGORIES: Final[tuple[str, ...]] = ("工资", "其他")

# logical field -> (Feishu name, type, writable, exact options)
EXPECTED_TABLE_FIELDS: Final[
    dict[str, dict[str, tuple[str, FieldType, bool, tuple[str, ...] | None]]]
] = {
    "expense": {
        "amount": ("原始金额", FieldType.NUMBER, True, None),
        "name": ("名称", FieldType.TEXT, True, None),
        "occurred_on": ("日期", FieldType.DATETIME, True, None),
        "is_family_expense": (
            "是否家庭支出",
            FieldType.CHECKBOX,
            True,
            None,
        ),
        "category": (
            "分类",
            FieldType.SINGLE_SELECT,
            True,
            EXPECTED_EXPENSE_CATEGORIES,
        ),
    },
    "income": {
        "amount": ("金额", FieldType.NUMBER, True, None),
        "name": ("名称", FieldType.TEXT, True, None),
        "occurred_on": ("日期", FieldType.DATETIME, True, None),
        "category": (
            "分类",
            FieldType.SINGLE_SELECT,
            True,
            EXPECTED_INCOME_CATEGORIES,
        ),
    },
    "family_fund": {
        "recharge_amount": ("充值金额", FieldType.NUMBER, True, None),
        "occurred_on": ("日期", FieldType.DATETIME, True, None),
        "note": ("备注", FieldType.TEXT, True, None),
        "actual_credit": ("实际入账", FieldType.FORMULA, False, None),
        "balance": ("家庭基金余额", FieldType.FORMULA, False, None),
    },
}


def load_ledger_config(data: dict[str, Any]) -> LedgerConfig:
    """Parse and validate a configuration document."""
    return LedgerConfig.model_validate(data)
