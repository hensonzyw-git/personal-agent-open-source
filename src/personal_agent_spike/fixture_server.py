from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

from personal_agent_spike.policy import (
    ALLOWED_EXPENSE_CATEGORIES,
    EXPENSE_AMOUNT_PATTERN,
)


mcp = FastMCP("personal-agent-fixture", json_response=True)


class FixtureRecord(BaseModel):
    name: str
    amount: str
    currency: str
    category: str
    occurred_on: str
    is_family_expense: bool


class FixtureEvidence(BaseModel):
    kind: str
    external_id: str


class ExpenseResult(BaseModel):
    status: str
    record_id: str
    source_system: str
    table: str
    committed_at: str
    record: FixtureRecord
    review_flags: list[str]
    evidence: FixtureEvidence


class CapabilitiesResult(BaseModel):
    server: str
    tools: list[str]
    side_effects: str


@mcp.tool(name="finance.log_expense", structured_output=True)
def log_expense(
    name: str,
    amount: str,
    category: Literal["出行", "餐饮", "游戏", "日常生活", "玩乐", "购物", "旅行", "房租"],
    occurred_on: str,
    is_family_expense: bool,
    currency: Literal["CNY"] = "CNY",
) -> ExpenseResult:
    """Create a deterministic fake expense record without touching Feishu."""
    if category not in ALLOWED_EXPENSE_CATEGORIES:
        raise ValueError("CATEGORY_NOT_ALLOWED")
    if not EXPENSE_AMOUNT_PATTERN.fullmatch(amount):
        raise ValueError("INVALID_AMOUNT")
    try:
        normalized_amount = Decimal(amount).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError("INVALID_AMOUNT") from exc
    if normalized_amount <= 0:
        raise ValueError("INVALID_AMOUNT")

    fingerprint = "|".join(
        [name, str(normalized_amount), category, occurred_on, str(is_family_expense)]
    )
    record_id = f"fixture_{hashlib.sha256(fingerprint.encode()).hexdigest()[:12]}"
    return ExpenseResult(
        status="created",
        record_id=record_id,
        source_system="fixture",
        table="支出记录",
        committed_at=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        record=FixtureRecord(
            name=name,
            amount=str(normalized_amount),
            currency=currency,
            category=category,
            occurred_on=occurred_on,
            is_family_expense=is_family_expense,
        ),
        review_flags=[],
        evidence=FixtureEvidence(kind="fixture_record", external_id=record_id),
    )


@mcp.tool(name="meta.get_capabilities", structured_output=True)
def get_capabilities() -> CapabilitiesResult:
    """Return fixture capabilities for MCP discovery tests."""
    return CapabilitiesResult(
        server="personal-agent-fixture",
        tools=["finance.log_expense", "meta.get_capabilities"],
        side_effects="none",
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
