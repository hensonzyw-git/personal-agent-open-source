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
from enum import StrEnum
from typing import Any, Final

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


class FieldSpec(BaseModel):
    """One expected field: located by id, validated by everything else."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    expected_name: str
    type: FieldType
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

    table_id: str
    #: Logical field name -> expected field. Logical names are the connector's,
    #: not Feishu's; the Feishu name is validated against `expected_name`.
    fields: dict[str, FieldSpec]


class LedgerConfig(BaseModel):
    """The whole protected configuration for one annual ledger."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_year: int
    config_version: str
    base_token: str
    effective_from: str
    effective_to: str
    tables: dict[str, TableConfig] = Field(default_factory=dict)

    def checksum(self) -> str:
        """A stable checksum over the whole configuration.

        Any change -- a new field id, a renamed expectation, a shifted option
        set -- changes this value, which is what makes a config change a
        reviewable, versioned event.
        """
        return hashlib.sha256(
            canonical_json(self.model_dump(mode="json")).encode("utf-8")
        ).hexdigest()


#: The expense category options every 2026 config must expect, taken from the
#: frozen contract so the config cannot drift from what the model may send.
EXPECTED_EXPENSE_CATEGORIES: Final[tuple[str, ...]] = ALLOWED_EXPENSE_CATEGORIES


def load_ledger_config(data: dict[str, Any]) -> LedgerConfig:
    """Parse and validate a configuration document."""
    return LedgerConfig.model_validate(data)
