from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Literal


PolicyOutcome = Literal["allow", "ask", "confirm", "deny"]


@dataclass(frozen=True)
class PolicyDecision:
    outcome: PolicyOutcome
    reason_code: str


TOOL_SCOPES = {
    "finance.log_expense": "finance.expense.write",
    "finance.query_transactions": "finance.expense.read",
    "finance.analyze_period": "finance.expense.read",
    "kb.search": "kb.wiki.read",
    "kb.capture": "kb.raw.write",
    "meta.get_capabilities": "meta.capabilities.read",
}

ALLOWED_EXPENSE_CATEGORIES = {
    "出行",
    "餐饮",
    "游戏",
    "日常生活",
    "玩乐",
    "购物",
    "旅行",
    "房租",
}

EXPENSE_AMOUNT_PATTERN = re.compile(r"^(0|[1-9][0-9]*)(\.[0-9]{1,2})?$")


def decide_tool_call(
    tool_name: str,
    arguments: dict[str, Any],
    granted_scopes: set[str],
) -> PolicyDecision:
    required_scope = TOOL_SCOPES.get(tool_name)
    if required_scope is None:
        return PolicyDecision("deny", "TOOL_NOT_ALLOWLISTED")
    if required_scope not in granted_scopes:
        return PolicyDecision("deny", "SCOPE_DENIED")

    if tool_name == "finance.log_expense":
        missing = [
            field
            for field in ("name", "amount", "category", "occurred_on", "is_family_expense")
            if field not in arguments or arguments[field] in ("", None)
        ]
        if missing:
            return PolicyDecision("ask", f"MISSING_{'_'.join(field.upper() for field in missing)}")
        if arguments["category"] not in ALLOWED_EXPENSE_CATEGORIES:
            return PolicyDecision("ask", "CATEGORY_NOT_ALLOWED")
        raw_amount = arguments["amount"]
        if not isinstance(raw_amount, str) or not EXPENSE_AMOUNT_PATTERN.fullmatch(raw_amount):
            return PolicyDecision("ask", "INVALID_AMOUNT")
        try:
            amount = Decimal(raw_amount)
        except InvalidOperation:
            return PolicyDecision("ask", "INVALID_AMOUNT")
        if amount <= 0:
            return PolicyDecision("ask", "INVALID_AMOUNT")
        return PolicyDecision("allow", "R2_DIRECT_WRITE")

    return PolicyDecision("allow", "ALLOWLISTED_READ_OR_CAPTURE")
