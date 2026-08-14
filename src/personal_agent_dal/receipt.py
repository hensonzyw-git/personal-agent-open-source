"""The DAL operation receipt.

Every executed operation produces exactly one receipt, whether it applied or
was refused. The receipt is the unit the frozen oracles assert against, so its
shape is fixed here rather than invented per handler: a receipt is a code plus
the schema that code is expressed in, and nothing else reaches the test
comparison or the outward surface.

`POLICY_DENIED` is the DAL-007 refusal code: the operation did not apply, the
entity stayed where it was, and the reason it was denied is diagnostic only.

Reference: docs/dal/DAL001-003_合同冻结包_v0.1.md §2.5 (TransitionReceipt) and
the `dal.operation-receipt/1.0` schema referenced by the frozen oracles.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


OPERATION_RECEIPT_SCHEMA: Final[str] = "dal.operation-receipt/1.0"


class ReceiptCode(StrEnum):
    """The closed set of operation receipt codes the DAL-007–013 slice emits.

    `APPLIED` is the only success. `POLICY_DENIED` is a clean refusal with no
    side effect. `UNKNOWN` marks an outcome the service cannot prove either
    way; it is never a silent success.

    `VERSION_CONFLICT` and `IDEMPOTENCY_CONFLICT` are DAL-008's two refusals.
    They are separate codes because they answer different questions: the first
    says another writer moved the aggregate first, so the caller must re-read
    and retry; the second says this exact idempotency key was already used for
    *different* content, which retrying can never fix. Collapsing them into one
    code would make a retry loop chase a conflict that will never clear.

    Both are returned to the caller and **not persisted**: the frozen oracles
    for `cas_conflict` and `idempotency_unique` allow an empty write set, so a
    refused operation leaves no row behind — not even an audit row. See
    docs/dal/DAL008_数据库Schema设计草案_v0.1.md §3 and D6.
    """

    APPLIED = "APPLIED"
    POLICY_DENIED = "POLICY_DENIED"
    UNKNOWN = "UNKNOWN"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    #: A clean no-op: there was nothing to do (e.g. a notification batch whose
    #: every member is invalid). Not a success and not a refusal — zero writes.
    NOOP = "NOOP"


@dataclass(frozen=True)
class OperationReceipt:
    """One immutable record of an operation's outcome.

    The dataclass is frozen so a receipt cannot be edited after the fact to
    turn a refusal into a success. Equality is by value, which is what the
    oracle comparator relies on.
    """

    code: ReceiptCode
    schema_version: str = OPERATION_RECEIPT_SCHEMA

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code.value, "schema_version": self.schema_version}
