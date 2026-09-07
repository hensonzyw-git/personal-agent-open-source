"""The tool IR: one specification that generates every downstream artifact.

`docs/MCP工具IR_v0.1.md` defines the intermediate representation and
`docs/Finance MCP工具设计草案_v0.1.md` is the canonical Finance contract. This
module is their executable form. The governed bridge, the MCP server, the policy
layer and the tests all read it, so a tool cannot mean one thing to the model and
another to the connector.

Two rules shape the model-visible schemas:

- the model only ever sees business fields. Identity, scopes, idempotency keys,
  duplicate overrides, resource identifiers and timezone are Host injected and
  are dropped if they appear in model output;
- a field with a default is a field the model can quietly omit. `is_family_expense`
  therefore has no default anywhere: a missing personal/family scope must stop and
  ask, never write.

The archived contracts in IR sections 5.1 to 5.7 are non-normative and must not
be regenerated from here.
"""

from __future__ import annotations

from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from personal_agent_core.errors import ErrorCode
from personal_agent_core.money import (
    AMOUNT_PATTERN,
    CURRENCY_PATTERN,
    SIGNED_AMOUNT_PATTERN,
)


IR_VERSION: Final[str] = "0.2.0"

DATE_PATTERN: Final[str] = r"^\d{4}-\d{2}-\d{2}$"

#: The only categories the 2026 ledger accepts. The connector must never create
#: a new Feishu select option.
ALLOWED_EXPENSE_CATEGORIES: Final[tuple[str, ...]] = (
    "出行",
    "餐饮",
    "游戏",
    "日常生活",
    "玩乐",
    "购物",
    "旅行",
    "房租",
)

ENTRY_KINDS: Final[tuple[str, ...]] = ("expense", "refund", "aa_reimbursement")

SCOPE_EXPENSE_READ: Final[str] = "finance.expense.read"
SCOPE_EXPENSE_WRITE: Final[str] = "finance.expense.write"
SCOPE_INCOME_WRITE: Final[str] = "finance.income.write"
SCOPE_FAMILY_FUND_WRITE: Final[str] = "finance.family_fund.write"
SCOPE_META_READ: Final[str] = "meta.capabilities.read"
SCOPE_CALENDAR_READ: Final[str] = "calendar.event.read"
SCOPE_CALENDAR_WRITE: Final[str] = "calendar.event.write"


Effect = Literal["read", "create", "update", "delete"]
RiskLevel = Literal["R0", "R1", "R2", "R3", "R4", "R5"]
Confirmation = Literal["never", "conditional", "always", "forbidden"]


class Idempotency(BaseModel):
    """How repeated delivery of the same request is made harmless."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key_source: Literal["host_injected_uuid4", "not_applicable"]
    replay_result: str


class Retry(BaseModel):
    """Which failures a caller may retry, and under what key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    retryable_errors: tuple[ErrorCode, ...] = ()
    reuse_idempotency_key: bool = True


class Audit(BaseModel):
    """What the audit trail keeps and what it must never keep."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    recorded: tuple[str, ...]
    never_recorded: tuple[str, ...]


class ToolContract(BaseModel):
    """One tool's complete specification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    version: str
    domain: str
    effect: Effect
    risk_level: RiskLevel
    enabled: bool
    #: Whether the model may be offered this tool at all.
    #:
    #: Separate from `enabled` because "this capability is live" and "a language
    #: model may decide to invoke it" are two different permissions, and the
    #: system has now grown a tool where they diverge:
    #: `finance.update_expense_category` exists to serve a deterministic tap on
    #: the receipt card, and the model has no business rewriting the category of
    #: an already-committed ledger row on its own inference.
    #:
    #: This is a field rather than an exclusion list in the composition layer for
    #: the reason this codebase has already been bitten by twice (see
    #: `_RECORD_ID_RESULT_TOOLS`): a hand-maintained set beside the IR drifts
    #: silently, and the drift is only discovered when it has already granted
    #: something. Here the default is permissive, so the risk is a *new* tool
    #: being model-callable when it should not be -- which is why the contract
    #: suite asserts the model-facing set explicitly rather than deriving it
    #: from the same expression twice.
    model_callable: bool = True
    disabled_reason: str | None = None
    #: Where the tool's external effect actually happens. `"mcp"` (default) is
    #: every connector-backed tool: the Host calls it through the governed MCP
    #: bridge. `"device"` names a tool whose fact source lives in the calling
    #: device's own sandbox — the Apple calendar is the first one — so the Host
    #: can never execute it: dispatch returns a device action for the phone to
    #: perform with EventKit, and the phone reports the outcome back. Like
    #: `model_callable`, this is a contract field rather than a list beside the
    #: IR, because the dispatch fork must be derived, not hand-maintained.
    executor: Literal["mcp", "device"] = "mcp"
    summary: str
    model_input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    required_scopes: tuple[str, ...]
    confirmation: Confirmation
    idempotency: Idempotency
    retry: Retry
    audit: Audit
    errors: tuple[ErrorCode, ...] = Field(default=())


#: Host injected context, identical for every tool. It is specified here so the
#: MCP server can reject a call whose signed context does not match, but it is
#: never merged into `model_input_schema`.
HOST_CONTEXT_SCHEMA: Final[dict[str, Any]] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "HostContext",
    "description": (
        "由 Agent Host 在调用前绑定。模型不能提供、读取或伪造这些字段；"
        "模型输出中出现的同名字段一律丢弃。"
    ),
    "type": "object",
    "additionalProperties": False,
    "required": [
        "request_id",
        "idempotency_key",
        "user_id",
        "device_id",
        "granted_scopes",
        "timezone",
        "trace_id",
    ],
    "properties": {
        "request_id": {"type": "string", "format": "uuid"},
        "idempotency_key": {
            "type": "string",
            "format": "uuid",
            "description": (
                "客户端为本次消息生成的 UUIDv4。Phase 1 在“一条消息最多一个副作用”"
                "约束下，同一个值同时作为 Finance MCP 幂等键和飞书 client_token。"
            ),
        },
        "user_id": {"type": "string", "minLength": 1},
        "device_id": {"type": "string", "format": "uuid"},
        "granted_scopes": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },
        "timezone": {"const": "Asia/Shanghai"},
        "trace_id": {"type": "string", "minLength": 1},
        "duplicate_override": {
            "type": ["string", "null"],
            "default": None,
            "description": (
                "Host 在“仍然写入”决策后绑定的 duplicate_check_id，只对本次调用生效。"
                "它只走 Host 签名通道，不是模型参数：模型既看不到也无法伪造。"
            ),
        },
    },
}


ZERO_AMOUNT_TEXTS: Final[tuple[str, ...]] = ("0", "0.0", "0.00")


def _amount_schema(
    description: str, *, nullable: bool, positive: bool
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": ["string", "null"] if nullable else "string",
        "pattern": AMOUNT_PATTERN,
        "description": description,
    }
    if nullable:
        schema["default"] = None
    if positive:
        schema["not"] = {"enum": list(ZERO_AMOUNT_TEXTS)}
    return schema


def _nullable_amount(
    description: str, *, positive: bool = True
) -> dict[str, Any]:
    return _amount_schema(description, nullable=True, positive=positive)


def _positive_amount(description: str) -> dict[str, Any]:
    return _amount_schema(description, nullable=False, positive=True)


_EXPENSE_ENTRY_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "name",
        "input_amount",
        "input_currency",
        "is_family_expense",
        "entry_kind",
    ],
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80,
            "description": (
                "用户表达的事项文本，原样保留。不润色、不纠错、不缩写、不重命名。"
                "仅当可高置信拆出通用餐食/场景提示与明确商户名时，提示只参与分类，"
                "name 只保留商户名；边界不清必须追问。"
                "旅行场次和外币后缀由服务端追加，不要自己拼进来。"
            ),
        },
        "input_amount": _positive_amount(
            (
                "原始输入金额的非零绝对值，最多两位小数。不要输入负号："
                "账务符号完全由 entry_kind 决定。缺失或区间/近似金额必须先追问，"
                "不得估算。"
            )
        ),
        "input_currency": {
            "type": "string",
            "pattern": CURRENCY_PATTERN,
            "default": "CNY",
            "description": (
                "ISO 4217 代码。用户未提币种时为 CNY；$ 等有歧义的符号必须先追问，"
                "不得猜测币种，也不得自行换算汇率。"
            ),
        },
        "settlement_amount_cny": _nullable_amount(
            "用户明确给出的实际人民币结算金额。存在时优先于参考汇率。"
        ),
        "category": {
            "type": ["string", "null"],
            "enum": [*ALLOWED_EXPENSE_CATEGORIES, None],
            "default": None,
            "description": (
                "普通支出必须是合法分类。退款和 AA 收款可为空，由服务端在唯一高置信"
                "原消费匹配时继承；无法可靠判定时追问，绝不新建分类，也不只凭模型"
                "声称认识一个不透明商户名来猜测。"
            ),
        },
        "occurred_on": {
            "type": "string",
            "format": "date",
            "pattern": DATE_PATTERN,
            "description": (
                "实际付款/入账日期的绝对值，YYYY-MM-DD。不要传“昨天”这类相对表达。"
                "今天为未来行程付款仍记今天。模型省略此字段时，Host 按该消息的"
                "Asia/Shanghai 接收日填入绝对日期；显式值必须保持为有效绝对日期。"
            ),
        },
        "is_family_expense": {
            "type": "boolean",
            "description": (
                "必须由当前输入明确表达：true 为家庭支出，false 为个人支出。"
                "没有默认值，不得从历史账单或被退款的原消费继承。"
                "缺失时先追问，不要调用本工具。"
            ),
        },
        "trip_tag": {
            "type": ["string", "null"],
            "minLength": 1,
            "maxLength": 40,
            "pattern": "^[^#]+$",
            "default": None,
            "description": (
                "旅行场次，不含 # 本身。显式给出时原样使用；只有明确目的地而无场次时"
                "由服务端解析，多个同根场次必须追问，绝不猜缩写或编号。"
            ),
        },
        "entry_kind": {
            "type": "string",
            "enum": list(ENTRY_KINDS),
            "description": (
                "expense 为正数普通支出；refund 和 aa_reimbursement 归一化为负数冲减。"
                "仅有负号而没有退款或 AA 语义时必须追问。"
            ),
        },
    },
    "allOf": [
        {
            "if": {
                "properties": {"entry_kind": {"const": "expense"}},
                "required": ["entry_kind"],
            },
            "then": {
                "required": ["category"],
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": list(ALLOWED_EXPENSE_CATEGORIES),
                    }
                },
            },
        }
    ],
}


_EXPENSE_RECORD_OUTPUT: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "status",
        "record_id",
        "source_system",
        "table",
        "committed_at",
        "record",
        "evidence",
    ],
    "properties": {
        "status": {"enum": ["created", "idempotent_replay"]},
        "record_id": {"type": "string", "minLength": 1},
        "source_system": {"const": "feishu_bitable"},
        "table": {"type": "string", "minLength": 1},
        "committed_at": {"type": "string", "format": "date-time"},
        "record": {"type": "object"},
        "evidence": {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "external_id"],
            "properties": {
                "kind": {"const": "feishu_record"},
                "external_id": {"type": "string", "minLength": 1},
            },
        },
    },
}


_WRITE_AUDIT: Final[Audit] = Audit(
    recorded=(
        "trace_id",
        "device_id",
        "tool",
        "contract_version",
        "risk_level",
        "policy_outcome",
        "argument_field_names",
        "category",
        "entry_kind",
        "request_fingerprint",
        "source_status",
        "latency_ms",
        "record_id",
        "schema_snapshot_version",
    ),
    never_recorded=(
        "raw_user_text",
        "model_reasoning",
        "exact_amount",
        "feishu_base_table_field_ids",
        "access_token",
        "app_secret",
        "provider_response_body",
    ),
)

_READ_AUDIT: Final[Audit] = Audit(
    recorded=(
        "trace_id",
        "device_id",
        "tool",
        "contract_version",
        "risk_level",
        "policy_outcome",
        "filters_applied",
        "record_count",
        "latency_ms",
        "query_id",
    ),
    never_recorded=(
        "raw_user_text",
        "model_reasoning",
        "exact_amount",
        "feishu_base_table_field_ids",
        "access_token",
        "provider_response_body",
    ),
)

_SOURCE_ERRORS: Final[tuple[ErrorCode, ...]] = (
    ErrorCode.SOURCE_SCHEMA_CHANGED,
    ErrorCode.SOURCE_UNAVAILABLE,
    ErrorCode.SOURCE_TIMEOUT_UNKNOWN,
    ErrorCode.SOURCE_COMMIT_UNKNOWN,
    ErrorCode.SOURCE_COMMITTED_MISMATCH,
)

_GOVERNANCE_ERRORS: Final[tuple[ErrorCode, ...]] = (
    ErrorCode.INVALID_ARGUMENT,
    ErrorCode.SCOPE_DENIED,
    ErrorCode.TOOL_NOT_ALLOWLISTED,
    ErrorCode.HOST_CONTEXT_MISMATCH,
    ErrorCode.IDEMPOTENCY_CONFLICT,
)


LOG_EXPENSE = ToolContract(
    name="finance.log_expense",
    version="1.0.0",
    domain="finance",
    effect="create",
    risk_level="R2",
    enabled=True,
    summary="新增单笔支出、退款或 AA 收款到年度支出记录表。",
    model_input_schema={
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "FinanceLogExpenseInput",
        **_EXPENSE_ENTRY_SCHEMA,
    },
    output_schema=_EXPENSE_RECORD_OUTPUT,
    required_scopes=(SCOPE_EXPENSE_WRITE,),
    confirmation="never",
    idempotency=Idempotency(
        key_source="host_injected_uuid4",
        replay_result="返回首次写入的同一 record_id，不产生第二条外部记录。",
    ),
    retry=Retry(retryable_errors=(ErrorCode.SOURCE_UNAVAILABLE,)),
    audit=_WRITE_AUDIT,
    errors=(
        *_GOVERNANCE_ERRORS,
        ErrorCode.CLARIFICATION_REQUIRED,
        ErrorCode.CATEGORY_NOT_ALLOWED,
        ErrorCode.POSSIBLE_DUPLICATE,
        ErrorCode.FX_RATE_UNAVAILABLE,
        *_SOURCE_ERRORS,
    ),
)


#: What a category correction returns. Deliberately *not* `_EXPENSE_RECORD_OUTPUT`:
#: an update is not a create, and reusing the create's schema would make
#: `status: "created"` a legal value for a call that never creates anything.
_CATEGORY_UPDATE_OUTPUT: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "status",
        "record_id",
        "source_system",
        "table",
        "category",
        "updated_at",
        "record",
        "evidence",
    ],
    "properties": {
        # `already_current` is a success with no request sent: the row was
        # found at the requested value, which is what a replay after a lost
        # response looks like.
        "status": {"enum": ["updated", "already_current"]},
        "record_id": {"type": "string", "minLength": 1},
        "source_system": {"const": "feishu_bitable"},
        "table": {"const": "expense"},
        "category": {"enum": list(ALLOWED_EXPENSE_CATEGORIES)},
        "updated_at": {"type": "string", "format": "date-time"},
        "record": {"type": "object"},
        "evidence": {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "external_id"],
            "properties": {
                "kind": {"const": "feishu_record"},
                "external_id": {"type": "string", "minLength": 1},
            },
        },
    },
}


#: Correcting the 分类 of an expense that is already in the ledger.
#:
#: `R2` like every other governed write. Henson opened this deterministic
#: receipt-card action on 2026-08-15; it is enabled for that route while remaining
#: unavailable to the model, because modifying committed ledger content requires
#: the explicit picker tap rather than a model inference.
#:
#: `confirmation="never"` is not a relaxation. The confirmation *is* the tap:
#: this tool exists to serve a deterministic user action on the receipt card,
#: not a model inference, and the API route that reaches it is not on the
#: model-facing channel at all. Adding a second confirmation to a button press
#: would be theatre.
UPDATE_EXPENSE_CATEGORY = ToolContract(
    name="finance.update_expense_category",
    version="1.0.0",
    domain="finance",
    effect="update",
    risk_level="R2",
    enabled=True,
    # The model never sees this tool. It is reached only by the device-authenticated
    # route behind the receipt card's picker, where the user's tap *is* the
    # decision. A model that could call it could re-categorise committed ledger
    # rows from inference alone, which is a strictly larger authority than
    # anything else in the Finance surface grants.
    model_callable=False,
    summary="把一条已入账支出的分类改成账本已有的另一个选项，不改动其他任何字段。",
    model_input_schema={
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "FinanceUpdateExpenseCategoryInput",
        "type": "object",
        "additionalProperties": False,
        "required": ["record_id", "category", "expected_current_category"],
        "properties": {
            "record_id": {
                "type": "string",
                "minLength": 1,
                "description": "要修改的支出记录 ID，来自该笔的写入回执。",
            },
            "category": {
                "enum": list(ALLOWED_EXPENSE_CATEGORIES),
                "description": (
                    "新的分类，必须是账本单选字段已有的选项；连接器从不新建选项。"
                ),
            },
            "expected_current_category": {
                "enum": [*ALLOWED_EXPENSE_CATEGORIES, None],
                "description": (
                    "调用方认为这条记录当前的分类，用于 compare-and-swap。与账本"
                    "实际不符时拒绝写入并回报账本当前值，绝不覆盖他处的改动。"
                    "null 表示调用方认为该笔当前没有分类（退款/AA 收款）。"
                ),
            },
        },
    },
    output_schema=_CATEGORY_UPDATE_OUTPUT,
    required_scopes=(SCOPE_EXPENSE_WRITE,),
    confirmation="never",
    idempotency=Idempotency(
        key_source="host_injected_uuid4",
        replay_result=(
            "重放时先读回该记录：已是目标分类则返回 already_current 且不发送任何"
            "请求；已被改成第三个值则拒绝，绝不二次覆盖。"
        ),
    ),
    retry=Retry(retryable_errors=(ErrorCode.SOURCE_UNAVAILABLE,)),
    audit=_WRITE_AUDIT,
    errors=(
        *_GOVERNANCE_ERRORS,
        ErrorCode.CATEGORY_NOT_ALLOWED,
        ErrorCode.CATEGORY_CHANGED_ELSEWHERE,
        # There is deliberately no distinct "record not found" code. The adapter
        # maps every non-zero Feishu code to `SOURCE_UNAVAILABLE`
        # (`_parse_envelope`), so an unknown record id arrives as exactly that
        # today. Listing a code the tool cannot actually emit would make this
        # contract describe a system that does not exist; narrowing the map is
        # its own change, in the adapter, with its own evidence.
        *_SOURCE_ERRORS,
    ),
)


LOG_EXPENSE_BATCH = ToolContract(
    name="finance.log_expense_batch",
    version="1.0.0",
    domain="finance",
    effect="create",
    risk_level="R2",
    enabled=False,
    disabled_reason=(
        "飞书批量新增接口有 client_token，但官方文档未声明事务级全成全败语义。"
        "在非生产测试表证明原子性之前保持关闭，且不得退化为逐笔顺序写入。"
    ),
    summary="一条消息中两笔及以上完全解析的支出，全成功或一笔也不写。",
    model_input_schema={
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "FinanceLogExpenseBatchInput",
        "type": "object",
        "additionalProperties": False,
        "required": ["entries"],
        "properties": {
            "entries": {
                "type": "array",
                "minItems": 2,
                "maxItems": 20,
                "items": _EXPENSE_ENTRY_SCHEMA,
                "description": (
                    "每一笔都必须独立完整。任一笔缺字段、场次歧义、退款无法匹配分类"
                    "或汇率不可得时，整批先追问或失败，一笔也不写。"
                ),
            }
        },
    },
    output_schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "batch_id", "records"],
        "properties": {
            "status": {"enum": ["created", "idempotent_replay"]},
            "batch_id": {"type": "string", "minLength": 1},
            "records": {
                "type": "array",
                "minItems": 2,
                "items": _EXPENSE_RECORD_OUTPUT,
            },
        },
    },
    required_scopes=(SCOPE_EXPENSE_WRITE,),
    confirmation="never",
    idempotency=Idempotency(
        key_source="host_injected_uuid4",
        replay_result="重放必须返回同一组 record_id，不得报告部分成功。",
    ),
    retry=Retry(retryable_errors=()),
    audit=_WRITE_AUDIT,
    errors=(
        *_GOVERNANCE_ERRORS,
        ErrorCode.BATCH_ATOMICITY_UNAVAILABLE,
        ErrorCode.BATCH_COMMIT_UNKNOWN,
        ErrorCode.CLARIFICATION_REQUIRED,
        *_SOURCE_ERRORS,
    ),
)


LOG_INCOME = ToolContract(
    name="finance.log_income",
    version="1.0.0",
    domain="finance",
    effect="create",
    risk_level="R2",
    enabled=True,
    summary="新增单笔收入到年度收入记录表；分类由封闭 Income Policy 决定。",
    model_input_schema={
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "FinanceLogIncomeInput",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "income_description",
            "input_amount",
            "input_currency",
        ],
        "properties": {
            "income_description": {
                "type": "string",
                "minLength": 1,
                "maxLength": 80,
                "description": (
                    "从用户输入提取的收入事项描述。不要提供分类："
                    "服务端的封闭 Income Policy 决定写工资还是其他。"
                ),
            },
            "input_amount": _positive_amount(
                (
                    "非零正数金额，最多两位小数。收入拒绝负数；"
                    "退款和 AA 收款属于支出冲减，不是收入。"
                )
            ),
            "input_currency": {
                "type": "string",
                "pattern": CURRENCY_PATTERN,
                "default": "CNY",
                "description": "ISO 4217 代码；未提币种时为 CNY。",
            },
            "settlement_amount_cny": _nullable_amount(
                "用户明确给出的实际人民币结算额，存在时优先。"
            ),
            "occurred_on": {
                "type": "string",
                "format": "date",
                "pattern": DATE_PATTERN,
                "description": (
                    "实际入账日期的绝对值，YYYY-MM-DD。模型省略此字段时，Host 按"
                    "该消息的 Asia/Shanghai 接收日填入绝对日期；显式值必须保持为"
                    "有效绝对日期。"
                ),
            },
        },
    },
    output_schema=_EXPENSE_RECORD_OUTPUT,
    required_scopes=(SCOPE_INCOME_WRITE,),
    confirmation="never",
    idempotency=Idempotency(
        key_source="host_injected_uuid4",
        replay_result="返回首次写入的同一 record_id。",
    ),
    retry=Retry(retryable_errors=(ErrorCode.SOURCE_UNAVAILABLE,)),
    audit=_WRITE_AUDIT,
    errors=(
        *_GOVERNANCE_ERRORS,
        ErrorCode.CLARIFICATION_REQUIRED,
        ErrorCode.POSSIBLE_DUPLICATE,
        ErrorCode.FX_RATE_UNAVAILABLE,
        *_SOURCE_ERRORS,
    ),
)


UPDATE_FAMILY_FUND = ToolContract(
    name="finance.update_family_fund",
    version="1.0.0",
    domain="finance",
    effect="update",
    risk_level="R2",
    enabled=True,
    summary="家庭基金充值，或按目标余额补齐利息；只记账本变动，不执行资金划转。",
    model_input_schema={
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "FinanceUpdateFamilyFundInput",
        "type": "object",
        "additionalProperties": False,
        "required": ["mode"],
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["top_up", "interest_reconcile"],
                "description": (
                    "top_up 直接充值；interest_reconcile 由用户给出当前目标余额，"
                    "服务端读取当前余额后计算差值的一半。"
                ),
            },
            "recharge_amount_cny": _nullable_amount(
                "top_up 模式的充值金额，必须为正数。"
            ),
            "target_balance_cny": _nullable_amount(
                "interest_reconcile 模式的目标余额。低于或等于当前余额时不写入。",
                positive=False,
            ),
            "note": {
                "type": ["string", "null"],
                "maxLength": 80,
                "default": None,
                "description": "备注；利息补齐的备注由服务端固定写入，不由模型提供。",
            },
        },
        # Dispatched with if/then on `mode` rather than oneOf. Under oneOf every
        # failure reports the branch that was not taken, so a bad top_up amount
        # came back as "'interest_reconcile' was expected" and that text would
        # have travelled into clarification prompts and alerts.
        #
        # The unused mode's fields are excluded by value, not by presence: a
        # caller that spells "not applicable" as an explicit null means the same
        # thing as omitting the key, and the two tools must not disagree about
        # that.
        "allOf": [
            {
                "if": {
                    "properties": {"mode": {"const": "top_up"}},
                    "required": ["mode"],
                },
                "then": {
                    "required": ["recharge_amount_cny"],
                    "properties": {
                        "recharge_amount_cny": _positive_amount(
                            "top_up 模式的充值金额，必须为正数。"
                        ),
                        "target_balance_cny": {"const": None},
                    },
                },
            },
            {
                "if": {
                    "properties": {"mode": {"const": "interest_reconcile"}},
                    "required": ["mode"],
                },
                "then": {
                    "required": ["target_balance_cny"],
                    "properties": {
                        "target_balance_cny": {
                            "type": "string",
                            "pattern": AMOUNT_PATTERN,
                        },
                        "recharge_amount_cny": {"const": None},
                        # The 利息补齐 note is fixed server side.
                        "note": {"const": None},
                    },
                },
            },
        ],
    },
    output_schema={
        "type": "object",
        "additionalProperties": False,
        "required": [
            "status",
            "record_id",
            "mode",
            "recharge_amount_cny",
            "balance_before_cny",
            "balance_after_cny",
            "note",
            "evidence",
        ],
        "properties": {
            "status": {"enum": ["created", "idempotent_replay"]},
            "record_id": {"type": "string", "minLength": 1},
            "mode": {"enum": ["top_up", "interest_reconcile"]},
            "recharge_amount_cny": {"type": "string"},
            # Null only when the fund table had no row at all, so there was no
            # prior balance to read. Reporting "0" or "" there would assert a
            # balance nobody observed.
            "balance_before_cny": {"type": ["string", "null"]},
            "balance_after_cny": {"type": "string"},
            "note": {"type": ["string", "null"]},
            "evidence": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "external_id"],
                "properties": {
                    "kind": {"const": "feishu_record"},
                    "external_id": {"type": "string", "minLength": 1},
                },
            },
        },
    },
    required_scopes=(SCOPE_FAMILY_FUND_WRITE,),
    confirmation="never",
    idempotency=Idempotency(
        key_source="host_injected_uuid4",
        replay_result="返回同一 record_id 和同一写后余额，不重复充值。",
    ),
    retry=Retry(retryable_errors=()),
    audit=_WRITE_AUDIT,
    errors=(
        *_GOVERNANCE_ERRORS,
        ErrorCode.NO_CHANGE_REQUIRED,
        ErrorCode.TARGET_BELOW_CURRENT_BALANCE,
        ErrorCode.TARGET_NOT_REACHED_CONCURRENT_CHANGE,
        *_SOURCE_ERRORS,
    ),
)


QUERY_EXPENSES = ToolContract(
    name="finance.query_expenses",
    version="1.0.0",
    domain="finance",
    effect="read",
    risk_level="R1",
    enabled=True,
    summary="支出明细、总额和分类统计；金额口径固定为飞书“个人支出”公式的有符号值。",
    model_input_schema={
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "FinanceQueryExpensesInput",
        "type": "object",
        "additionalProperties": False,
        "required": ["view"],
        "properties": {
            "view": {
                "type": "string",
                "enum": ["total", "by_category", "records"],
                "description": (
                    "total 返回净个人支出总额；by_category 返回分类统计；"
                    "records 返回分页明细。"
                ),
            },
            "date_range": {
                "type": ["object", "null"],
                "additionalProperties": False,
                "required": ["start", "end"],
                "default": None,
                "properties": {
                    "start": {
                        "type": "string",
                        "format": "date",
                        "pattern": DATE_PATTERN,
                    },
                    "end": {
                        "type": "string",
                        "format": "date",
                        "pattern": DATE_PATTERN,
                    },
                },
                "description": (
                    "发生日期闭区间。“这个月”“今年”必须先解析为绝对日期。"
                    "没有日期且没有其他能界定范围的筛选时，必须先追问。"
                ),
            },
            "categories": {
                "type": "array",
                "items": {"type": "string", "enum": list(ALLOWED_EXPENSE_CATEGORIES)},
                "uniqueItems": True,
                "default": [],
                "description": "多个分类之间是 OR；未传表示全部分类。",
            },
            "name_contains": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 40},
                "maxItems": 5,
                "default": [],
                "description": (
                    "纯文本片段，多个片段之间是 AND。不支持正则、SQL 或任意全文语法。"
                ),
            },
            "is_family_expense": {
                "type": "string",
                "enum": ["all", "true", "false"],
                "default": "all",
                "description": "只过滤是否家庭支出字段，不改变统计口径。",
            },
            "personal_amount_cny": {
                "type": ["object", "null"],
                "additionalProperties": False,
                "default": None,
                "properties": {
                    "min": {
                        "type": ["string", "null"],
                        "pattern": SIGNED_AMOUNT_PATTERN,
                        "default": None,
                    },
                    "min_inclusive": {"type": "boolean", "default": True},
                    "max": {
                        "type": ["string", "null"],
                        "pattern": SIGNED_AMOUNT_PATTERN,
                        "default": None,
                    },
                    "max_inclusive": {"type": "boolean", "default": True},
                },
                "description": (
                    "对飞书“个人支出”公式字段的有符号 CNY 值做范围过滤；"
                    "默认包含负数退款和 AA 收款。"
                ),
            },
            "cursor": {
                "type": ["string", "null"],
                "default": None,
                "description": (
                    "records 视图的服务端不透明游标。不要构造或猜测它的内容。"
                ),
            },
        },
    },
    output_schema={
        "type": "object",
        "required": [
            "status",
            "view",
            "filters_applied",
            "metric",
            "record_count",
            "source_system",
            "evidence",
        ],
        "properties": {
            "status": {"const": "ok"},
            "view": {"enum": ["total", "by_category", "records"]},
            "filters_applied": {"type": "object"},
            "metric": {"const": "personal_spend_total_cny"},
            "record_count": {"type": "integer", "minimum": 0},
            "personal_spend_total_cny": {"type": "string"},
            "by_category": {"type": "array"},
            "records": {"type": "array"},
            "next_cursor": {"type": ["string", "null"]},
            "source_system": {"const": "feishu_bitable"},
            "evidence": {
                "type": "object",
                "required": [
                    "kind",
                    "query_id",
                    "config_checksum",
                    "schema_snapshot_checksum",
                    "scanned_pages",
                    "matched_count",
                    "started_at",
                    "completed_at",
                ],
                "properties": {
                    "kind": {"const": "aggregate_query"},
                    "query_id": {"type": "string"},
                    "config_checksum": {"type": "string"},
                    "schema_snapshot_checksum": {"type": "string"},
                    "scanned_pages": {"type": "integer", "minimum": 1},
                    "matched_count": {"type": "integer", "minimum": 0},
                    "started_at": {"type": "string"},
                    "completed_at": {"type": "string"},
                },
            },
        },
    },
    required_scopes=(SCOPE_EXPENSE_READ,),
    confirmation="never",
    idempotency=Idempotency(
        key_source="not_applicable",
        replay_result="只读查询没有外部副作用。",
    ),
    retry=Retry(
        retryable_errors=(
            ErrorCode.SOURCE_UNAVAILABLE,
            ErrorCode.SOURCE_TIMEOUT_UNKNOWN,
        ),
        reuse_idempotency_key=False,
    ),
    audit=_READ_AUDIT,
    errors=(
        *_GOVERNANCE_ERRORS,
        ErrorCode.CLARIFICATION_REQUIRED,
        ErrorCode.SOURCE_SCHEMA_CHANGED,
        ErrorCode.SOURCE_UNAVAILABLE,
        ErrorCode.SOURCE_TIMEOUT_UNKNOWN,
    ),
)


META_CAPABILITIES = ToolContract(
    name="meta.capabilities",
    version="1.0.0",
    domain="meta",
    effect="read",
    risk_level="R0",
    enabled=True,
    summary="返回当前设备实际可用的工具，用于渐进披露能力。",
    model_input_schema={
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "MetaCapabilitiesInput",
        "type": "object",
        "additionalProperties": False,
        "properties": {},
    },
    output_schema={
        "type": "object",
        "required": ["status", "tools", "allowed_tools_version"],
        "properties": {
            "status": {"const": "ok"},
            "tools": {"type": "array", "items": {"type": "object"}},
            "allowed_tools_version": {"type": "string"},
        },
    },
    required_scopes=(SCOPE_META_READ,),
    confirmation="never",
    idempotency=Idempotency(
        key_source="not_applicable",
        replay_result="只读，无副作用。",
    ),
    retry=Retry(reuse_idempotency_key=False),
    audit=_READ_AUDIT,
    errors=(ErrorCode.SCOPE_DENIED,),
)


#: The Apple calendar is the *authoritative* calendar (Henson's 2026-09-07
#: decision). Its events live in the iPhone's EventKit sandbox, which no server
#: can reach, so the domain splits into a device-executed write and two
#: mirror-backed reads: the phone performs `create_event` locally and reports
#: back; it also uploads a mirror of its own calendar to the server, and
#: `query_events` runs against that mirror — never against the model's own
#: claims about what is on the calendar. Reminders, tasks, recurrence,
#: edit and delete are non-goals for v1.
_CALENDAR_EVENT_FIELDS: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "event_identifier",
        "calendar_identifier",
        "title",
        "start",
        "end",
        "all_day",
    ],
    "properties": {
        "event_identifier": {
            "type": "string",
            "minLength": 1,
            "description": "设备端 EventKit 的事件标识（EKEvent.eventIdentifier）。",
        },
        "calendar_identifier": {
            "type": "string",
            "minLength": 1,
            "description": "该事件所在日历的标识。",
        },
        "title": {"type": "string", "maxLength": 80},
        "start": {
            "type": "string",
            "format": "date-time",
            "description": "开始时刻（UTC 瞬间）；all_day 事件为当日（Asia/Shanghai）零点。",
        },
        "end": {
            "type": "string",
            "format": "date-time",
            "description": "结束时刻（UTC 瞬间）；all_day 事件为次日凌晨零点（排他）。",
        },
        "all_day": {"type": "boolean"},
        "location": {"type": ["string", "null"], "maxLength": 200},
        "notes": {"type": ["string", "null"], "maxLength": 500},
        "created_by_agent": {
            "type": "boolean",
            "description": "该事件是否由本 Agent 的设备动作创建。",
        },
    },
}

_CALENDAR_CREATE_INPUT: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "start", "end", "all_day"],
    "properties": {
        "title": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80,
            "description": "日程标题，使用用户口述的措辞。",
        },
        "start": {
            "type": "string",
            "format": "date-time",
            "description": (
                "开始时刻，Asia/Shanghai 绝对时间（如 2026-09-12T15:00:00+08:00）；"
                "all_day 时为当日语义，服务端落为当日零点。"
                "用户只说了相对表达（明天下午）时必须先换算或追问，"
                "绝不把没说清的时刻猜成整点写入。"
            ),
        },
        "end": {
            "type": "string",
            "format": "date-time",
            "description": (
                "结束时刻，Asia/Shanghai 绝对时间；all_day 时为结束日次日凌晨"
                "（EventKit 全天事件结束时刻排他）。用户没说结束时刻时按 "
                "一小时默认并如实告知。"
            ),
        },
        "all_day": {
            "type": "boolean",
            "description": "是否为全天日程。全天事件只需日期，不需要具体时刻。",
        },
        "location": {
            "type": "string",
            "maxLength": 200,
            "description": "可选地点。",
        },
        "notes": {
            "type": "string",
            "maxLength": 500,
            "description": "可选备注。",
        },
    },
}

_CALENDAR_CREATE_OUTPUT: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "action_id", "event"],
    "properties": {
        # `issued` is the tool's direct output: the Host has prepared a signed
        # device action and the phone performs it. The *receipt* (record_id =
        # event identifier) arrives later from the device's own report, so the
        # output schema must not claim a `record_id` that does not exist yet.
        "status": {"const": "issued"},
        "action_id": {
            "type": "string",
            "format": "uuid",
            "description": "本次设备动作的幂等键，与宿主操作的幂等键相同。",
        },
        "event": {
            "type": "object",
            "required": ["title", "start", "end", "all_day"],
            "properties": {
                "title": {"type": "string"},
                "start": {"type": "string", "format": "date-time"},
                "end": {"type": "string", "format": "date-time"},
                "all_day": {"type": "boolean"},
                "location": {"type": ["string", "null"]},
                "notes": {"type": ["string", "null"]},
            },
        },
    },
}

_CALENDAR_QUERY_INPUT: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["start", "end"],
    "properties": {
        "start": {
            "type": "string",
            "format": "date-time",
            "description": "窗口开始（含），Asia/Shanghai 绝对时间。",
        },
        "end": {
            "type": "string",
            "format": "date-time",
            "description": "窗口结束（不含），Asia/Shanghai 绝对时间。",
        },
        "cursor": {
            "type": ["string", "null"],
            "default": None,
            "description": "服务端不透明游标。不要构造或猜测它的内容。",
        },
    },
}

_CALENDAR_QUERY_OUTPUT: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "status",
        "events",
        "record_count",
        "data_as_of",
        "mirror_stale",
        "source_system",
    ],
    "properties": {
        "status": {"const": "ok"},
        "events": {"type": "array", "items": _CALENDAR_EVENT_FIELDS},
        "record_count": {"type": "integer", "minimum": 0},
        "next_cursor": {"type": ["string", "null"]},
        # "数据截至": the mirror's own freshness, not the model's confidence.
        # `mirror_stale` is true when the newest sync is older than the query
        # window by a margin, so the model must state the staleness instead of
        # presenting the mirror as the live calendar.
        "data_as_of": {
            "type": "string",
            "format": "date-time",
            "description": "日历镜像最近一次同步的时间。",
        },
        "mirror_stale": {"type": "boolean"},
        "source_system": {"const": "apple_calendar_mirror"},
    },
}

_CALENDAR_INGEST_INPUT: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["window_start", "window_end", "events", "window_complete"],
    "properties": {
        "window_start": {
            "type": "string",
            "format": "date-time",
            "description": "本次快照窗口开始。",
        },
        "window_end": {
            "type": "string",
            "format": "date-time",
            "description": "本次快照窗口结束。",
        },
        "events": {
            "type": "array",
            "maxItems": 200,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "event_identifier",
                    "calendar_identifier",
                    "start",
                    "end",
                    "all_day",
                    "last_modified",
                ],
                "properties": {
                    "event_identifier": {"type": "string", "minLength": 1},
                    "calendar_identifier": {"type": "string", "minLength": 1},
                    "title": {"type": ["string", "null"], "maxLength": 80},
                    "start": {"type": "string", "format": "date-time"},
                    "end": {"type": "string", "format": "date-time"},
                    "all_day": {"type": "boolean"},
                    "location": {"type": ["string", "null"], "maxLength": 200},
                    "notes": {"type": ["string", "null"], "maxLength": 500},
                    "last_modified": {
                        "type": "string",
                        "format": "date-time",
                        "description": "设备端该事件最后修改时间，乱序上载的仲裁依据。",
                    },
                },
            },
        },
        "window_complete": {
            "type": "boolean",
            "description": (
                "本批是否已把窗口内全部事件送完。为 true 时，窗口内未在本批"
                "出现的既有镜像行会被标记删除——设备是日历的事实源。"
            ),
        },
    },
}

_CALENDAR_INGEST_OUTPUT: Final[dict[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "upserted", "skipped", "marked_deleted"],
    "properties": {
        "status": {"const": "ok"},
        "upserted": {"type": "integer", "minimum": 0},
        "skipped": {"type": "integer", "minimum": 0},
        "marked_deleted": {"type": "integer", "minimum": 0},
    },
}

#: Device-executed writes audit the same governance fields as connector writes;
#: there is no provider latency or source status to record because no external
#: call happened at dispatch — the phone's own report is the evidence.
_CALENDAR_WRITE_AUDIT: Final[Audit] = Audit(
    recorded=(
        "trace_id",
        "device_id",
        "tool",
        "contract_version",
        "risk_level",
        "policy_outcome",
        "argument_field_names",
        "request_fingerprint",
        "device_report_status",
        "latency_ms",
    ),
    never_recorded=(
        "raw_user_text",
        "model_reasoning",
        "event_title",
        "event_location",
        "event_notes",
        "device_event_identifier",
        "access_token",
        "provider_response_body",
    ),
)


CALENDAR_CREATE_EVENT = ToolContract(
    name="calendar.create_event",
    version="1.0.0",
    domain="calendar",
    effect="create",
    risk_level="R2",
    enabled=True,
    executor="device",
    summary=(
        "在用户 iPhone 的 Apple 日历中创建一条单次日程。全天日程传 all_day；"
        "不支持重复日程——用户要每周重复时如实说明暂不支持，不要建多条冒充。"
    ),
    model_input_schema={
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "CalendarCreateEventInput",
        **_CALENDAR_CREATE_INPUT,
    },
    output_schema=_CALENDAR_CREATE_OUTPUT,
    required_scopes=(SCOPE_CALENDAR_WRITE,),
    confirmation="never",
    idempotency=Idempotency(
        key_source="host_injected_uuid4",
        replay_result=(
            "同一动作键只产生一个设备动作；设备端另有本地查重（同日历、"
            "同标题、开始时刻 ±5 分钟）兜底重复创建。"
        ),
    ),
    # An automatic retry would create a second event: the local dedup window is
    # a mitigation, not a right. The device action itself is settled exactly
    # once by its CAS; a lost report surfaces as needs_manual_review instead.
    retry=Retry(retryable_errors=()),
    audit=_CALENDAR_WRITE_AUDIT,
    errors=(
        *_GOVERNANCE_ERRORS,
        ErrorCode.CLARIFICATION_REQUIRED,
        ErrorCode.DEVICE_ACTION_DENIED,
        ErrorCode.DEVICE_EXECUTION_FAILED,
    ),
)


CALENDAR_QUERY_EVENTS = ToolContract(
    name="calendar.query_events",
    version="1.0.0",
    domain="calendar",
    effect="read",
    risk_level="R1",
    enabled=True,
    summary=(
        "查询日历镜像中的日程（日期窗口必填）。镜像由设备同步上载，"
        "回答必须携带数据截至时间，镜像陈旧时如实说明。"
    ),
    model_input_schema={
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "CalendarQueryEventsInput",
        **_CALENDAR_QUERY_INPUT,
    },
    output_schema=_CALENDAR_QUERY_OUTPUT,
    required_scopes=(SCOPE_CALENDAR_READ,),
    confirmation="never",
    idempotency=Idempotency(
        key_source="not_applicable",
        replay_result="只读查询没有外部副作用。",
    ),
    retry=Retry(
        retryable_errors=(ErrorCode.SOURCE_UNAVAILABLE,),
        reuse_idempotency_key=False,
    ),
    audit=_READ_AUDIT,
    errors=(
        *_GOVERNANCE_ERRORS,
        ErrorCode.CLARIFICATION_REQUIRED,
        ErrorCode.INVALID_CURSOR,
        ErrorCode.SOURCE_UNAVAILABLE,
    ),
)


#: Mirror ingest. The phone uploads its own calendar so `calendar.query_events`
#: has something truthful to read. It is service-facing (the API calls it inside
#: the sync route with a signed Host Context) and never a model choice: a model
#: that could write the mirror could fabricate the calendar the model itself
#: would then be asked to summarise.
CALENDAR_INGEST_EVENTS = ToolContract(
    name="calendar.ingest_events",
    version="1.0.0",
    domain="calendar",
    effect="update",
    risk_level="R1",
    enabled=True,
    model_callable=False,
    disabled_reason=(
        "镜像上载是设备同步路由的确定性动作，不是模型推理的选择；"
        "模型可写镜像等于模型可伪造日历。"
    ),
    summary="把设备上报的日历事件快照按 last_modified 仲裁合并进镜像。",
    model_input_schema={
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "CalendarIngestEventsInput",
        **_CALENDAR_INGEST_INPUT,
    },
    output_schema=_CALENDAR_INGEST_OUTPUT,
    required_scopes=(SCOPE_CALENDAR_READ,),
    confirmation="never",
    idempotency=Idempotency(
        key_source="not_applicable",
        replay_result=(
            "按 (calendar_identifier, event_identifier) upsert，同一批重放"
            "结果不变；last_modified 相同的行跳过。"
        ),
    ),
    retry=Retry(retryable_errors=(), reuse_idempotency_key=False),
    audit=_READ_AUDIT,
    errors=(ErrorCode.INVALID_ARGUMENT, ErrorCode.SCOPE_DENIED),
)


#: Declaration order is part of the generated artifact, so it stays fixed.
TOOL_CONTRACTS: Final[tuple[ToolContract, ...]] = (
    LOG_EXPENSE,
    LOG_EXPENSE_BATCH,
    UPDATE_EXPENSE_CATEGORY,
    LOG_INCOME,
    UPDATE_FAMILY_FUND,
    QUERY_EXPENSES,
    META_CAPABILITIES,
    CALENDAR_CREATE_EVENT,
    CALENDAR_QUERY_EVENTS,
    CALENDAR_INGEST_EVENTS,
)

CALENDAR_TOOL_NAMES: Final[tuple[str, ...]] = tuple(
    contract.name for contract in TOOL_CONTRACTS if contract.domain == "calendar"
)

FINANCE_TOOL_NAMES: Final[tuple[str, ...]] = tuple(
    contract.name for contract in TOOL_CONTRACTS if contract.domain == "finance"
)


def contract_by_name(name: str) -> ToolContract:
    """Look up one contract, raising rather than returning a permissive default."""
    for contract in TOOL_CONTRACTS:
        if contract.name == name:
            return contract
    raise KeyError(f"no tool contract named {name!r}")
