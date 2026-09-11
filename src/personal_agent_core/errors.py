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
    UNSUPPORTED_OPERATION = "UNSUPPORTED_OPERATION"
    HOST_CONTEXT_MISMATCH = "HOST_CONTEXT_MISMATCH"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    WRITES_DISABLED = "WRITES_DISABLED"
    # DEV-040: the model answered a bookkeeping request with prose and no tool
    # call. `DirectAnswer` carries no side effect, so a "我来帮你记录" that
    # never invoked a write tool must not look like success.
    BOOKKEEPING_TOOL_REQUIRED = "BOOKKEEPING_TOOL_REQUIRED"
    #: A Finance read was answered from model text instead of the fact source.
    FINANCE_TOOL_REQUIRED = "FINANCE_TOOL_REQUIRED"
    #: An explicit calendar-create request was answered with prose and no
    #: EventKit-backed device action was issued.
    CALENDAR_TOOL_REQUIRED = "CALENDAR_TOOL_REQUIRED"

    # Finance semantics
    CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"
    CLARIFICATION_REPEATED = "CLARIFICATION_REPEATED"
    CATEGORY_NOT_ALLOWED = "CATEGORY_NOT_ALLOWED"
    #: A category correction was refused because the row no longer holds the
    #: category the caller believed it held. Deliberately distinct from a plain
    #: conflict: nothing was written, and the caller is expected to show the
    #: value the ledger actually has rather than retry blindly.
    CATEGORY_CHANGED_ELSEWHERE = "CATEGORY_CHANGED_ELSEWHERE"
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

    # Calendar device-executed actions. The iPhone EventKit is the fact source:
    # a denial or an execution failure it *reports* is zero-write evidence, at
    # the same trust level as Finance reporting its own refusal. The timeout is
    # deliberately not here as a zero-write claim — a report that never arrived
    # means the write may exist, which is `needs_manual_review`, not a code.
    DEVICE_ACTION_DENIED = "DEVICE_ACTION_DENIED"
    DEVICE_EXECUTION_FAILED = "DEVICE_EXECUTION_FAILED"

    # Timeline and context (`CAP-001`). The later cross-cutting codes
    # (`MEMORY_POLICY_REJECTED`, `MEDIA_NOT_READY`, `UNSUPPORTED_MODALITY`,
    # `INVALID_EVENT_CURSOR`) arrive with the CAP that serves their route; a
    # code with no caller is surface, not readiness.
    TIMELINE_MISMATCH = "TIMELINE_MISMATCH"
    #: The progress trail polls by idempotency key because the chat POST can
    #: hold the client for up to 30 seconds before naming the operation. A key
    #: nothing has anchored yet is a distinguishable 400, never a bare 404 that
    #: could be read as "this key is free, send a new request".
    OPERATION_NOT_ANCHORED = "OPERATION_NOT_ANCHORED"
    INVALID_CURSOR = "INVALID_CURSOR"
    CONTEXT_BUDGET_EXCEEDED = "CONTEXT_BUDGET_EXCEEDED"
    CONTEXT_UNAVAILABLE = "CONTEXT_UNAVAILABLE"
    PENDING_OPERATION_NOT_CANCELLABLE = "PENDING_OPERATION_NOT_CANCELLABLE"
    CALENDAR_SYNC_RESET_REQUIRED = "CALENDAR_SYNC_RESET_REQUIRED"

    # Internal
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ClarificationQuestion(StrEnum):
    """Closed Finance questions that may cross the MCP error boundary.

    A connector is never allowed to put a resolver's arbitrary detail on the
    wire.  These are deliberately fixed product questions, selected only from
    the small enum below.  The Host persists one of them as the exact pending
    question, so a later answer has a meaningful continuation context instead
    of the generic ``CLARIFICATION_REQUIRED`` catalogue message.
    """

    EXPENSE_CATEGORY = "这笔支出属于哪个分类？"
    TRIP = "这笔旅行支出对应哪一趟行程？"
    ORIGINAL_EXPENSE = "这笔退款或 AA 对应哪一笔原消费？"
    TRIP_CATEGORY_CONFLICT = "这笔应按旅行还是原分类记录？"
    INCOME_SUBJECT = "这笔收入的事项是什么？"


class ModelFailureReason(StrEnum):
    """Stable, safe-to-store reasons for a pre-submit model-turn failure.

    These are operation ``failure_reason`` values rather than ``ErrorCode``
    envelopes: a chat operation has already been accepted and needs a durable,
    retry-safe outcome.  They intentionally contain no provider message or
    identifier.
    """

    UNAVAILABLE = "model_unavailable"
    PROVIDER_TIMEOUT = "model_provider_timeout"
    PROVIDER_RATE_LIMITED = "model_provider_rate_limited"
    PROVIDER_AUTH_FAILED = "model_provider_auth_failed"
    PROVIDER_REJECTED = "model_provider_rejected"
    PROVIDER_UNAVAILABLE = "model_provider_unavailable"
    RESPONSE_INVALID = "model_response_invalid"
    RESPONSE_PROVIDER_ERROR = "model_response_provider_error"
    RESPONSE_PARTIAL = "model_response_partial"
    RESPONSE_EMPTY = "model_response_empty"
    RESPONSE_AMBIGUOUS = "model_response_ambiguous"
    RESPONSE_SCHEMA_INVALID = "model_response_schema_invalid"
    RESPONSE_UNSUPPORTED_CONTENT = "model_response_unsupported_content"


# A failed model turn has not reached a governed business tool.  These reasons
# are therefore eligible for the sealed, one-shot Finance retry flow; the
# caller still has to make that explicit and the normal policy path still runs.
MODEL_RETRYABLE_FAILURE_REASONS: Final[frozenset[str]] = frozenset(
    reason.value for reason in ModelFailureReason
)


#: Outward messages. Fixed text only: no interpolation of provider output,
#: amounts, record identifiers, Base or table identifiers.
ERROR_MESSAGES: Final[dict[ErrorCode, str]] = {
    ErrorCode.INVALID_ARGUMENT: "请求参数不符合工具合同",
    ErrorCode.SCOPE_DENIED: "当前设备没有执行该操作的权限",
    ErrorCode.TOOL_NOT_ALLOWLISTED: "该工具不在服务端允许集合内",
    ErrorCode.UNSUPPORTED_OPERATION: "该操作一期不开放，请在电脑端飞书账本处理",
    ErrorCode.HOST_CONTEXT_MISMATCH: "调用上下文与签名不一致，已拒绝执行",
    ErrorCode.IDEMPOTENCY_CONFLICT: "同一请求键对应了不同的请求内容",
    ErrorCode.WRITES_DISABLED: "写入已被手动停用，未写入任何记录",
    ErrorCode.BOOKKEEPING_TOOL_REQUIRED: (
        "这是记账请求，但未调用记账工具，没有写入任何记录"
    ),
    ErrorCode.FINANCE_TOOL_REQUIRED: (
        "这是账务查询，但未调用 Finance 工具，未返回账本结果"
    ),
    ErrorCode.CALENDAR_TOOL_REQUIRED: (
        "这是创建日程请求，但未调用日历工具，没有创建任何日程"
    ),
    ErrorCode.CLARIFICATION_REQUIRED: "信息不完整，需要先确认后才能记账",
    ErrorCode.CLARIFICATION_REPEATED: (
        "同一项信息已回答但仍无法完成，未写入任何记录"
    ),
    ErrorCode.CATEGORY_NOT_ALLOWED: "分类不在账本的合法选项内",
    ErrorCode.CATEGORY_CHANGED_ELSEWHERE: (
        "这笔的分类已被其他地方改过，未覆盖；请确认账本当前的分类后再决定"
    ),
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
    ErrorCode.DEVICE_ACTION_DENIED: "设备上的日历没有授权这次写入",
    ErrorCode.DEVICE_EXECUTION_FAILED: "设备写入日历失败，没有产生日程",
    ErrorCode.TIMELINE_MISMATCH: "该会话标识不属于当前对话记录",
    ErrorCode.OPERATION_NOT_ANCHORED: "该请求键还没有对应的操作记录，请继续等待",
    ErrorCode.INVALID_CURSOR: "翻页游标无效或已过期",
    ErrorCode.CONTEXT_BUDGET_EXCEEDED: "本轮必要上下文超出可用长度，未调用模型",
    ErrorCode.CONTEXT_UNAVAILABLE: "暂时无法安全组装对话上下文",
    ErrorCode.PENDING_OPERATION_NOT_CANCELLABLE: (
        "当前待办可能已提交，不能放弃后另开话题"
    ),
    ErrorCode.CALENDAR_SYNC_RESET_REQUIRED: "日历镜像已重建，请重新同步当前日历",
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
    clarification_question: ClarificationQuestion | None = None


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
        clarification_question: ClarificationQuestion | None = None,
    ) -> None:
        if code not in ERROR_MESSAGES:
            raise ValueError(f"error code has no outward message: {code!r}")
        if (
            clarification_question is not None
            and code is not ErrorCode.CLARIFICATION_REQUIRED
        ):
            raise ValueError(
                "clarification_question is valid only for CLARIFICATION_REQUIRED"
            )
        self.code = code
        self.internal_detail = internal_detail
        self.clarification_question = clarification_question
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
            clarification_question=self.clarification_question,
        )

    def __str__(self) -> str:
        return self.code.value
