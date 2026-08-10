"""Stable error codes for the Development Agent Loop service.

Same acceptance rule as `personal_agent_core.errors`: an outward error can
never carry a provider body, a resource identifier or a secret. `DalError` has
no free-text user message; the outward text comes from a fixed catalogue keyed
by the stable code, and any diagnostic detail stays in `internal_detail`, which
the envelope never serialises.

The DAL keeps its own code namespace rather than reusing the Finance/agent
codes, because the two services have disjoint failure vocabularies and a shared
enum would let one service's code leak into the other's outward surface.

Reference: docs/dal/DAL001-003_合同冻结包_v0.1.md §2 (error taxonomy).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict


class DalErrorCode(StrEnum):
    """Business error codes exposed to the client and to the operator."""

    # Request and authorisation
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    SCOPE_DENIED = "SCOPE_DENIED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    APPROVAL_REPLAYED = "APPROVAL_REPLAYED"
    APPROVAL_HASH_MISMATCH = "APPROVAL_HASH_MISMATCH"
    APPROVAL_REVOKED = "APPROVAL_REVOKED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"

    # State machine
    ILLEGAL_TRANSITION = "ILLEGAL_TRANSITION"
    TERMINAL_STATE = "TERMINAL_STATE"
    ACTOR_NOT_ALLOWED = "ACTOR_NOT_ALLOWED"
    STALE_VERSION = "STALE_VERSION"
    EVENT_OUT_OF_ORDER = "EVENT_OUT_OF_ORDER"
    UNKNOWN_FAILURE = "UNKNOWN_FAILURE"

    # Config and policy (DAL-007). A refused load must never name the offending
    # module, secret or resource outward; the code alone is the whole answer.
    CONFIG_POLICY_DENIED = "CONFIG_POLICY_DENIED"
    CONFIG_UNAVAILABLE = "CONFIG_UNAVAILABLE"

    # Provider / external (carried by later waves; declared so receipts share
    # one closed vocabulary rather than inventing codes per task)
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_LIMIT_EXHAUSTED = "PROVIDER_LIMIT_EXHAUSTED"
    EXTERNAL_RESULT_UNKNOWN = "EXTERNAL_RESULT_UNKNOWN"

    # Internal
    INTERNAL_ERROR = "INTERNAL_ERROR"


#: Outward messages. Fixed text only: no interpolation of provider output,
#: module names, secret names, paths or resource identifiers.
ERROR_MESSAGES: Final[dict[DalErrorCode, str]] = {
    DalErrorCode.INVALID_ARGUMENT: "请求参数不符合开发闭环合同",
    DalErrorCode.SCOPE_DENIED: "当前设备没有执行该操作的权限",
    DalErrorCode.APPROVAL_REQUIRED: "该操作需要结构化确认后才能执行",
    DalErrorCode.APPROVAL_EXPIRED: "该确认已过期，请重新确认",
    DalErrorCode.APPROVAL_REPLAYED: "该确认已被使用，不能重复执行",
    DalErrorCode.APPROVAL_HASH_MISMATCH: "确认内容与当前状态或产物不一致，已拒绝",
    DalErrorCode.APPROVAL_REVOKED: "该确认已被撤销",
    DalErrorCode.IDEMPOTENCY_CONFLICT: "同一请求键对应了不同的请求内容",
    DalErrorCode.ILLEGAL_TRANSITION: "该状态迁移不被允许",
    DalErrorCode.TERMINAL_STATE: "该任务已进入终态，不能再变更",
    DalErrorCode.ACTOR_NOT_ALLOWED: "该角色无权执行此迁移",
    DalErrorCode.STALE_VERSION: "状态已被并发修改，请基于最新状态重试",
    DalErrorCode.EVENT_OUT_OF_ORDER: "事件顺序与状态机不一致",
    DalErrorCode.UNKNOWN_FAILURE: "发生未知异常，已停止后续动作",
    DalErrorCode.CONFIG_POLICY_DENIED: "配置加载被安全策略拒绝",
    DalErrorCode.CONFIG_UNAVAILABLE: "配置暂时不可用",
    DalErrorCode.PROVIDER_UNAVAILABLE: "编码/审查服务暂时不可用",
    DalErrorCode.PROVIDER_LIMIT_EXHAUSTED: "编码/审查服务额度已用尽",
    DalErrorCode.EXTERNAL_RESULT_UNKNOWN: "外部动作结果未知，正在核验",
    DalErrorCode.INTERNAL_ERROR: "服务内部错误",
}


class DalErrorEnvelope(BaseModel):
    """The single outward error shape for the DAL service."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: DalErrorCode
    message: str
    operation_id: str | None = None
    trace_id: str | None = None


class DalError(Exception):
    """A DAL business failure carrying a stable code and no free-text message.

    `internal_detail` is for logs and audit summaries that already apply their
    own redaction. It is deliberately unreachable from `to_envelope`.
    """

    def __init__(
        self,
        code: DalErrorCode,
        *,
        internal_detail: str | None = None,
    ) -> None:
        if code not in ERROR_MESSAGES:
            raise ValueError(f"error code has no outward message: {code!r}")
        self.code = code
        self.internal_detail = internal_detail
        super().__init__(code.value)

    def to_envelope(
        self,
        *,
        operation_id: str | None = None,
        trace_id: str | None = None,
    ) -> DalErrorEnvelope:
        """Build the outward envelope. Never includes `internal_detail`."""
        return DalErrorEnvelope(
            code=self.code,
            message=ERROR_MESSAGES[self.code],
            operation_id=operation_id,
            trace_id=trace_id,
        )

    def __str__(self) -> str:
        return self.code.value
