"""Stable error codes and the outward error envelope.

The acceptance rule for this module is that an error can never leak a provider
response body, a resource identifier or a secret. That is enforced structurally
rather than by review: `AppError` has no free-text user message. The outward
message is looked up from a fixed catalogue keyed by the stable code, and any
diagnostic text a caller attaches stays in `internal_detail`, which the envelope
never serialises.

Reference: MCP tool IR 5.5, technical design 5.3 and 8.4.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict


class ErrorCode(StrEnum):
    """Business error codes exposed to the client and to the model."""

    # Request and authorisation
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    SCOPE_DENIED = "SCOPE_DENIED"
    TOOL_NOT_ALLOWLISTED = "TOOL_NOT_ALLOWLISTED"
    HOST_CONTEXT_MISMATCH = "HOST_CONTEXT_MISMATCH"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"

    # Finance semantics
    CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"
    CATEGORY_NOT_ALLOWED = "CATEGORY_NOT_ALLOWED"
    POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"
    FX_RATE_UNAVAILABLE = "FX_RATE_UNAVAILABLE"
    NO_CHANGE_REQUIRED = "NO_CHANGE_REQUIRED"
    TARGET_BELOW_CURRENT_BALANCE = "TARGET_BELOW_CURRENT_BALANCE"
    TARGET_NOT_REACHED_CONCURRENT_CHANGE = "TARGET_NOT_REACHED_CONCURRENT_CHANGE"

    # Fact source
    SOURCE_SCHEMA_CHANGED = "SOURCE_SCHEMA_CHANGED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    SOURCE_TIMEOUT_UNKNOWN = "SOURCE_TIMEOUT_UNKNOWN"
    SOURCE_COMMIT_UNKNOWN = "SOURCE_COMMIT_UNKNOWN"
    SOURCE_COMMITTED_MISMATCH = "SOURCE_COMMITTED_MISMATCH"

    # Batch, disabled in v0.1
    BATCH_ATOMICITY_UNAVAILABLE = "BATCH_ATOMICITY_UNAVAILABLE"
    BATCH_COMMIT_UNKNOWN = "BATCH_COMMIT_UNKNOWN"

    # Internal
    INTERNAL_ERROR = "INTERNAL_ERROR"


#: Outward messages. Fixed text only: no interpolation of provider output,
#: amounts, record identifiers, Base or table identifiers.
ERROR_MESSAGES: Final[dict[ErrorCode, str]] = {
    ErrorCode.INVALID_ARGUMENT: "请求参数不符合工具合同",
    ErrorCode.SCOPE_DENIED: "当前设备没有执行该操作的权限",
    ErrorCode.TOOL_NOT_ALLOWLISTED: "该工具不在服务端允许集合内",
    ErrorCode.HOST_CONTEXT_MISMATCH: "调用上下文与签名不一致，已拒绝执行",
    ErrorCode.IDEMPOTENCY_CONFLICT: "同一请求键对应了不同的请求内容",
    ErrorCode.CLARIFICATION_REQUIRED: "信息不完整，需要先确认后才能记账",
    ErrorCode.CATEGORY_NOT_ALLOWED: "分类不在账本的合法选项内",
    ErrorCode.POSSIBLE_DUPLICATE: "发现同日完全相同的记录，请确认是否仍然记录",
    ErrorCode.FX_RATE_UNAVAILABLE: "暂时无法取得参考汇率，未写入任何记录",
    ErrorCode.NO_CHANGE_REQUIRED: "目标余额与当前余额相同，无需写入",
    ErrorCode.TARGET_BELOW_CURRENT_BALANCE: "目标余额低于当前余额，不写入负充值",
    ErrorCode.TARGET_NOT_REACHED_CONCURRENT_CHANGE: (
        "写入期间余额被外部改动，未自动补写"
    ),
    ErrorCode.SOURCE_SCHEMA_CHANGED: "账本字段结构已变化，写入已熔断",
    ErrorCode.SOURCE_UNAVAILABLE: "账本暂时不可用",
    ErrorCode.SOURCE_TIMEOUT_UNKNOWN: "账本响应超时，正在核验是否已写入",
    ErrorCode.SOURCE_COMMIT_UNKNOWN: "写入结果尚未能从账本确认",
    ErrorCode.SOURCE_COMMITTED_MISMATCH: "写入后回读的字段与预期不一致",
    ErrorCode.BATCH_ATOMICITY_UNAVAILABLE: "多笔写入尚未启用，一笔也没有记录",
    ErrorCode.BATCH_COMMIT_UNKNOWN: "多笔写入结果未知，正在按批次键核验",
    ErrorCode.INTERNAL_ERROR: "服务内部错误",
}

#: Only a clean, side-effect-free unavailability is safely retryable by the
#: client. Anything that may already have reached the ledger is not: it goes
#: through same-key reconciliation instead.
RETRYABLE_CODES: Final[frozenset[ErrorCode]] = frozenset(
    {ErrorCode.SOURCE_UNAVAILABLE}
)


class ErrorEnvelope(BaseModel):
    """The single outward error shape, per technical design 5.3."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: ErrorCode
    message: str
    retryable: bool
    operation_id: str | None = None
    trace_id: str | None = None


class AppError(Exception):
    """A business failure carrying a stable code and no free-text message.

    `internal_detail` is for logs and audit summaries that already apply their
    own redaction. It is deliberately unreachable from `to_envelope`.
    """

    def __init__(
        self,
        code: ErrorCode,
        *,
        internal_detail: str | None = None,
    ) -> None:
        if code not in ERROR_MESSAGES:
            raise ValueError(f"error code has no outward message: {code!r}")
        self.code = code
        self.internal_detail = internal_detail
        super().__init__(code.value)

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_CODES

    def to_envelope(
        self,
        *,
        operation_id: str | None = None,
        trace_id: str | None = None,
    ) -> ErrorEnvelope:
        """Build the outward envelope. Never includes `internal_detail`."""
        return ErrorEnvelope(
            code=self.code,
            message=ERROR_MESSAGES[self.code],
            retryable=self.retryable,
            operation_id=operation_id,
            trace_id=trace_id,
        )

    def __str__(self) -> str:
        return self.code.value
