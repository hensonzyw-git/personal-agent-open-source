"""Bind every Finance read/write to the schema and source that were approved."""

from __future__ import annotations

import hmac

from personal_agent_core.errors import AppError, ErrorCode
from personal_data_mcp.feishu.base_source import BaseSource
from personal_data_mcp.finance.ledger_config import LedgerConfig
from personal_data_mcp.finance.schema_validator import SchemaValidation


def require_validated_source(
    *,
    config: LedgerConfig,
    validation: SchemaValidation,
    source: BaseSource,
    operation: str,
) -> None:
    """Fail closed unless schema evidence, protected config and source agree."""
    if (
        not validation.is_valid
        or validation.config_version != config.config_version
        or validation.config_checksum != config.checksum()
    ):
        raise AppError(
            ErrorCode.SOURCE_SCHEMA_CHANGED,
            internal_detail=(
                f"{operation} refused: schema validation does not match "
                "the active ledger config"
            ),
        )

    expected_tables = {
        kind: table.table_id for kind, table in config.tables.items()
    }
    if (
        source.ledger_kind != config.ledger_kind
        or not hmac.compare_digest(source.base_token, config.base_token)
        or source.tables != expected_tables
    ):
        raise AppError(
            ErrorCode.SOURCE_SCHEMA_CHANGED,
            internal_detail=(
                f"{operation} refused: source does not match "
                "the validated ledger config"
            ),
        )
