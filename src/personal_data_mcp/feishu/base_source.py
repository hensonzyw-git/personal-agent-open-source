"""Which Base the connector is pointed at, and the guard that it is the test one.

The red line is that nothing before G5 may touch the personal annual ledger; only
the synthetic test Base is allowed. That is enforced here in code rather than
trusted: the environment's kind, Base token and complete three-table mapping
must exactly match a separately protected annual configuration whose own kind is
`synthetic_test`. A marker alone is never accepted, so copying the marker onto a
different Base cannot silently authorise a read.

The Base token and table ids are loaded from the injected environment, the same
place the credentials come from, and are treated as sensitive: they are hashed
before they ever appear in a report or a log.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import Final


BASE_TOKEN_ENV: Final[str] = "FEISHU_FINANCE_BASE_TOKEN"
TABLE_EXPENSE_ENV: Final[str] = "FEISHU_FINANCE_TABLE_EXPENSE"
TABLE_INCOME_ENV: Final[str] = "FEISHU_FINANCE_TABLE_INCOME"
TABLE_FAMILY_FUND_ENV: Final[str] = "FEISHU_FINANCE_TABLE_FAMILY_FUND"
LEDGER_KIND_ENV: Final[str] = "FEISHU_FINANCE_LEDGER_KIND"

SYNTHETIC_TEST_KIND: Final[str] = "synthetic_test"
REQUIRED_TABLE_KINDS: Final[frozenset[str]] = frozenset(
    {"expense", "income", "family_fund"}
)


class LedgerSourceError(RuntimeError):
    """The configured Base is missing, malformed, or not the test Base."""


@dataclass(frozen=True)
class BaseSource:
    """The Base and tables the connector may read, all from the environment."""

    base_token: str
    ledger_kind: str
    tables: dict[str, str]

    @property
    def is_synthetic_test(self) -> bool:
        return self.ledger_kind == SYNTHETIC_TEST_KIND


def load_base_source(env: dict[str, str] | None = None) -> BaseSource:
    env = env if env is not None else dict(os.environ)
    base_token = env.get(BASE_TOKEN_ENV)
    ledger_kind = env.get(LEDGER_KIND_ENV)
    if not base_token or not ledger_kind:
        raise LedgerSourceError(
            f"set {BASE_TOKEN_ENV} and {LEDGER_KIND_ENV} in the environment"
        )
    tables = {
        kind: env[var]
        for kind, var in (
            ("expense", TABLE_EXPENSE_ENV),
            ("income", TABLE_INCOME_ENV),
            ("family_fund", TABLE_FAMILY_FUND_ENV),
        )
        if env.get(var)
    }
    if set(tables) != REQUIRED_TABLE_KINDS:
        missing = sorted(REQUIRED_TABLE_KINDS - set(tables))
        raise LedgerSourceError(
            f"all synthetic ledger tables are required; missing {missing}"
        )
    return BaseSource(
        base_token=base_token, ledger_kind=ledger_kind, tables=tables
    )


def require_synthetic_test_base(
    source: BaseSource,
    *,
    approved_base_token: str,
    approved_tables: dict[str, str],
    approved_ledger_kind: str,
) -> BaseSource:
    """Fail closed unless the source is explicitly the synthetic test Base.

    This is the code-level enforcement of "test Base only before G5". It requires
    two independent inputs -- the injected source and the protected config -- to
    agree on the kind and every resource identifier.
    """
    source_hash = hashlib.sha256(source.base_token.encode("utf-8")).digest()
    approved_hash = hashlib.sha256(
        approved_base_token.encode("utf-8")
    ).digest()
    if (
        approved_ledger_kind != SYNTHETIC_TEST_KIND
        or not source.is_synthetic_test
        or source.ledger_kind != approved_ledger_kind
    ):
        raise LedgerSourceError(
            "refusing to proceed: the configured Base is not marked "
            f"{SYNTHETIC_TEST_KIND!r}. Only the synthetic test Base is allowed "
            "until G5."
        )
    if not hmac.compare_digest(source_hash, approved_hash):
        raise LedgerSourceError(
            "configured Base does not match the protected synthetic-ledger config"
        )
    if source.tables != approved_tables:
        raise LedgerSourceError(
            "configured tables do not match the protected synthetic-ledger config"
        )
    return source
