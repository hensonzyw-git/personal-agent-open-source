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
PRODUCTION_KIND: Final[str] = "production"
REQUIRED_TABLE_KINDS: Final[frozenset[str]] = frozenset(
    {"expense", "income", "family_fund"}
)

#: The explicit G5 production-write authorisation. Only an exact `"1"` grants it;
#: absent, empty or anything else keeps the fail-closed default (synthetic only).
PRODUCTION_WRITE_ALLOW_ENV: Final[str] = "PERSONAL_AGENT_ALLOW_PRODUCTION_WRITE"


def production_write_allowed(env: dict[str, str] | None = None) -> bool:
    """Whether the G5 production-write switch is explicitly on.

    Fail-closed: only the literal value `"1"` returns True. A missing, empty or
    different value means the write path keeps refusing a `production` ledger,
    exactly as before G5.
    """
    env = env if env is not None else dict(os.environ)
    return env.get(PRODUCTION_WRITE_ALLOW_ENV) == "1"


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


def require_configured_base(
    source: BaseSource,
    *,
    approved_base_token: str,
    approved_tables: dict[str, str],
    approved_ledger_kind: str,
) -> BaseSource:
    """Bind an injected source to a protected config, whatever kind it declares.

    The sibling of `require_synthetic_test_base`, for the **read-only** path that
    may legitimately open the real annual ledger (Henson's 2026-08-03 staged-G5
    decision). It keeps every identity check -- kind, Base token under a
    constant-time compare, and the complete table mapping -- and drops only the
    "must be the synthetic Base" clause, because that clause is what the
    read-only path exists to step past.

    Deliberately a separate function rather than a `strict=False` parameter on
    the existing one: a boolean that weakens a red line can be passed by
    accident, while a differently-named import shows up in review and in an AST
    check. Nothing on the write path may call this.
    """
    if source.ledger_kind != approved_ledger_kind:
        raise LedgerSourceError(
            "configured Base kind does not match the protected ledger config"
        )
    source_hash = hashlib.sha256(source.base_token.encode("utf-8")).digest()
    approved_hash = hashlib.sha256(approved_base_token.encode("utf-8")).digest()
    if not hmac.compare_digest(source_hash, approved_hash):
        raise LedgerSourceError(
            "configured Base does not match the protected ledger config"
        )
    if source.tables != approved_tables:
        raise LedgerSourceError(
            "configured tables do not match the protected ledger config"
        )
    return source


def require_write_base(
    source: BaseSource,
    *,
    approved_base_token: str,
    approved_tables: dict[str, str],
    approved_ledger_kind: str,
) -> BaseSource:
    """Bind an injected source to a protected config on the *write* path.

    The sibling of `require_configured_base` for the write path, reachable only
    after the explicit G5 production-write authorisation has been granted (see
    `server.composition.load_protected_config`'s `allow_production`). It keeps
    every identity check -- kind, Base token under a constant-time compare, and
    the complete table mapping -- and drops only the "must be the synthetic
    Base" clause.

    This is deliberately a separate function from `require_configured_base`, not
    a reused one: `require_configured_base`'s docstring promises nothing on the
    write path may call it, and a name that says "write" makes the call site
    self-documenting in review. The write gate that admits `production` is the
    only caller, so an accidental call from a synthetic-only path is a review
    finding rather than a silent widening.
    """
    if source.ledger_kind != approved_ledger_kind:
        raise LedgerSourceError(
            "configured Base kind does not match the protected ledger config"
        )
    source_hash = hashlib.sha256(source.base_token.encode("utf-8")).digest()
    approved_hash = hashlib.sha256(approved_base_token.encode("utf-8")).digest()
    if not hmac.compare_digest(source_hash, approved_hash):
        raise LedgerSourceError(
            "configured Base does not match the protected ledger config"
        )
    if source.tables != approved_tables:
        raise LedgerSourceError(
            "configured tables do not match the protected ledger config"
        )
    return source
