"""DEV-038: the 磁盘满 row of the chaos matrix in technical design 11.1.

`tests/chaos/test_execution_fault_matrix.py` covers 7.6.4 by killing the process
at a durability boundary: whatever was committed survives, whatever was not is
gone. Storage exhaustion is a different failure and it was never covered. The
process keeps running, the code keeps its variables, and the *commit itself*
fails. That combination reaches lines a crash never reaches -- the `except`
paths, the outward error mapping, and everything after the raise.

**On the fidelity of the injection.** The error raised here is not written by
hand. :func:`genuine_sqlite_full` fills a scratch database to its
``max_page_count`` through the production engine until SQLite really refuses,
and captures the exception object it raised. The matrix then re-raises *that
object* at a chosen statement. So the counterparty is real SQLite and the error
is one it really produced; only the moment is chosen. What is not modelled is a
filesystem that is full for every process at once, including the recovery run --
that belongs to a live drill on the ECS, not to an offline suite.

The property under test is the one that matters when a disk fills mid-write:
**the number of external records never disagrees with what the system says
happened.** A create that landed must never be reported as a safe failure.
"""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError as SAOperationalError

from personal_data_mcp.feishu.adapter import FeishuAdapter
from personal_data_mcp.feishu.base_source import BaseSource
from personal_data_mcp.feishu.credentials import FeishuCredentials
from personal_data_mcp.finance.expense_record import ExpenseEntry
from personal_data_mcp.finance.ledger_config import load_ledger_config
from personal_data_mcp.finance.onboarding import observed_from_snapshot
from personal_data_mcp.finance.schema_validator import validate_schema
from personal_data_mcp.finance.write_path import write_expense
from personal_data_mcp.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_data_mcp.storage.models import (
    AuditEvent,
    ExternalReceipt,
    ToolExecution,
)
from write_switch_fixtures import shared_enabled_write_switch


LEDGER_FIXTURES = Path(__file__).parents[1] / "fixtures" / "ledger"
CONFIG = load_ledger_config(
    json.loads(
        (LEDGER_FIXTURES / "config.synthetic.json").read_text(encoding="utf-8")
    )
)
SNAPSHOT = json.loads(
    (LEDGER_FIXTURES / "snapshot.synthetic.json").read_text(encoding="utf-8")
)
VALIDATION = validate_schema(CONFIG, observed_from_snapshot(SNAPSHOT))
SOURCE = BaseSource(
    base_token=CONFIG.base_token,
    ledger_kind="synthetic_test",
    tables={kind: table.table_id for kind, table in CONFIG.tables.items()},
)
LUNCH = ExpenseEntry(
    name="午饭",
    amount_cny=Decimal("20.00"),
    occurred_on=date(2026, 7, 23),
    is_family_expense=False,
    category="餐饮",
)
KEY = "idem-disk-full"
#: The model-facing arguments of one expense call, as the MCP boundary sees them.
EXPENSE = {
    "name": "午饭",
    "input_amount": "20.00",
    "input_currency": "CNY",
    "occurred_on": "2026-07-23",
    "is_family_expense": False,
    "entry_kind": "expense",
    "category": "餐饮",
}


# --- a real SQLITE_FULL, captured once ---------------------------------------


def genuine_sqlite_full(tmp_path: Path) -> SAOperationalError:
    """Make real SQLite refuse a write for want of space, and keep the error.

    ``max_page_count`` is SQLite's own ceiling on the file, and crossing it
    raises ``SQLITE_FULL`` -- the identical result code and message a filesystem
    with no free blocks produces. Blobs are used because they must grow the
    file: small rows can be absorbed by free space inside pages already
    allocated, which makes the refusal non-deterministic.
    """
    engine = create_database_engine(tmp_path / "exhausted.sqlite")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE filler (blob BLOB)")
            connection.exec_driver_sql("PRAGMA max_page_count = 30")
        for _ in range(200):
            try:
                with engine.begin() as connection:
                    connection.exec_driver_sql(
                        "INSERT INTO filler VALUES (?)", (b"q" * 8_000,)
                    )
            except SAOperationalError as error:
                return error
        raise AssertionError("SQLite never reported the database as full")
    finally:
        engine.dispose()


def test_the_injected_error_is_one_sqlite_really_raised(tmp_path: Path) -> None:
    """The counterparty is SQLite, not a string this file made up."""
    error = genuine_sqlite_full(tmp_path)

    assert isinstance(error, SAOperationalError)
    assert isinstance(error.orig, sqlite3.OperationalError)
    assert str(error.orig) == "database or disk is full"
    assert error.orig.sqlite_errorname == "SQLITE_FULL"


def test_a_full_database_is_not_mistaken_for_a_lost_snapshot(
    tmp_path: Path,
) -> None:
    """`run_write_transaction` must not retry an exhausted disk.

    Both arrive as `OperationalError`. A stale read snapshot is worth re-running
    because fresh state may succeed; a full disk is not, and retrying it three
    times would turn one refusal into three and hide the real cause.
    """
    from personal_agent_core.sqlite import is_snapshot_conflict

    assert is_snapshot_conflict(genuine_sqlite_full(tmp_path)) is False


# --- arming that error at a chosen statement ---------------------------------


class Exhaustion:
    """Re-raise a real SQLITE_FULL at the *n*-th statement matching a predicate.

    Statement text is the only thing available at this seam, which is enough:
    each ordering-critical step of `execute_governed_write` touches a different
    table, and the two that touch the same one are separated by ordinal.
    """

    def __init__(self, engine, error: SAOperationalError) -> None:
        self._error = error
        self._match: Callable[[str], bool] | None = None
        self._skip = 0
        self.fired = False
        event.listen(engine, "before_cursor_execute", self._maybe_raise)

    def arm(self, *, sql_contains: str, verb: str, after: int = 0) -> None:
        self._match = lambda statement: (
            statement.lstrip().upper().startswith(verb.upper())
            and sql_contains in statement
        )
        self._skip = after
        self.fired = False

    def _maybe_raise(
        self, _conn, _cursor, statement, _params, _context, _many
    ) -> None:
        if self._match is None or not self._match(statement):
            return
        if self._skip:
            self._skip -= 1
            return
        self._match = None
        self.fired = True
        raise self._error


class FakeFeishu:
    """A Bitable that counts the creates that actually left the process."""

    def __init__(self) -> None:
        self.creates: list[dict[str, Any]] = []
        self.submitted_tokens: list[str | None] = []
        self.reads = 0
        self.records: dict[str, dict[str, Any]] = {}
        self.tokens: dict[str, str] = {}
        self.next_id = 1

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "t-1", "expire": 7200},
            )
        if request.method == "POST" and path.endswith("/records"):
            body = json.loads(request.content)
            self.creates.append(body)
            token = request.url.params.get("client_token")
            # The token as it reached Feishu. Asserting the persisted column
            # instead would pass even if the submit used a freshly minted one,
            # and the submitted token is the only one dedupe sees.
            self.submitted_tokens.append(token)
            # Feishu dedupes on client_token: a replay under the same token
            # returns the original record and adds no row. Proven live on
            # 2026-07-23; the reconciler depends on exactly this.
            if token and token in self.tokens:
                record_id = self.tokens[token]
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "record": {
                                "record_id": record_id,
                                "fields": self.records[record_id],
                            }
                        },
                    },
                )
            stored = copy.deepcopy(body["fields"])
            record_id = f"rec{self.next_id:06d}"
            self.next_id += 1
            self.records[record_id] = stored
            if token:
                self.tokens[token] = record_id
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"record": {"record_id": record_id, "fields": stored}},
                },
            )
        if request.method == "GET" and "/records/" in path:
            self.reads += 1
            record_id = path.rsplit("/", 1)[1]
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "record": {
                            "record_id": record_id,
                            "fields": self.records[record_id],
                        }
                    },
                },
            )
        raise AssertionError(f"unexpected call {request.method} {path}")

    @property
    def rows(self) -> int:
        """External records that exist -- the number that must never be lied about."""
        return len(self.records)


@pytest.fixture()
def store(tmp_path: Path):
    engine = create_database_engine(tmp_path / "finance.sqlite")
    create_all(engine)
    exhaustion = Exhaustion(engine, genuine_sqlite_full(tmp_path))
    yield session_factory(engine), exhaustion
    engine.dispose()


def adapter_for(fake: FakeFeishu) -> FeishuAdapter:
    return FeishuAdapter(
        FeishuCredentials(app_id="cli_test", app_secret="shh"),
        transport=httpx.MockTransport(fake.handler),
        now=lambda: 1000.0,
    )


async def _write(fake: FakeFeishu, sessions, *, key: str = KEY):
    async with adapter_for(fake) as adapter:
        return await write_expense(
            LUNCH,
            sessions=sessions,
            adapter=adapter,
            config=CONFIG,
            validation=VALIDATION,
            source=SOURCE,
            idempotency_key=key,
            request_fingerprint="fp-1",
            trace_id="trace-1",
        )


def write(fake: FakeFeishu, sessions, *, key: str = KEY):
    return asyncio.run(_write(fake, sessions, key=key))


def execution(sessions, key: str = KEY) -> ToolExecution | None:
    with sessions() as session:
        return session.get(ToolExecution, key)


def receipts(sessions) -> list[ExternalReceipt]:
    with sessions() as session:
        return list(session.query(ExternalReceipt).all())


def audit_types(sessions) -> list[str]:
    with sessions() as session:
        return [
            event.event_type
            for event in session.query(AuditEvent).order_by(AuditEvent.sequence)
        ]


# --- the matrix ---------------------------------------------------------------


def test_exhausted_before_prepared_sends_nothing_and_leaves_nothing(store) -> None:
    """Step 1 commits before any network, so a refusal here is entirely safe."""
    sessions, exhaustion = store
    fake = FakeFeishu()
    exhaustion.arm(sql_contains="tool_executions", verb="INSERT")

    with pytest.raises(SAOperationalError):
        write(fake, sessions)

    assert exhaustion.fired
    assert fake.rows == 0, "nothing may reach the ledger before `prepared` commits"
    assert execution(sessions) is None
    assert receipts(sessions) == []


def test_a_retry_after_space_is_freed_writes_exactly_one_record(store) -> None:
    sessions, exhaustion = store
    fake = FakeFeishu()
    exhaustion.arm(sql_contains="tool_executions", verb="INSERT")
    with pytest.raises(SAOperationalError):
        write(fake, sessions)

    outcome = write(fake, sessions)

    assert outcome.status == "created"
    assert fake.rows == 1
    assert len(fake.creates) == 1
    assert execution(sessions).state == "succeeded"


def test_exhausted_before_submitting_keeps_the_same_client_token(store) -> None:
    """Step 2 refused: nothing was sent, and the retry must reuse the token.

    A new token on the retry would defeat Feishu's dedupe, which is the only
    thing standing between a repeated submit and a second ledger row.
    """
    sessions, exhaustion = store
    fake = FakeFeishu()
    exhaustion.arm(sql_contains="tool_executions", verb="UPDATE")

    with pytest.raises(SAOperationalError):
        write(fake, sessions)

    assert fake.rows == 0
    parked = execution(sessions)
    assert parked.state == "prepared"
    token = parked.client_token

    outcome = write(fake, sessions)

    assert outcome.status == "created"
    assert fake.submitted_tokens == [token], (
        "the retry must submit the token persisted before the first attempt, "
        "not a freshly minted one"
    )
    assert execution(sessions).client_token == token
    assert fake.rows == 1


def test_exhausted_after_the_create_never_loses_the_record(store) -> None:
    """The dangerous one: Feishu wrote, and the receipt cannot be persisted.

    The execution must stay at `submitting` -- a state the reconciler promotes
    to `commit_unknown` and resolves under the same client token -- rather than
    at anything that reads as "never sent".
    """
    sessions, exhaustion = store
    fake = FakeFeishu()
    exhaustion.arm(sql_contains="external_receipts", verb="INSERT")

    with pytest.raises(SAOperationalError):
        write(fake, sessions)

    assert exhaustion.fired
    assert fake.rows == 1, "the create really happened"
    assert receipts(sessions) == [], "and the receipt really did not"
    stranded = execution(sessions)
    assert stranded.state == "submitting"
    assert stranded.client_token is not None


def test_a_stranded_write_is_not_resolvable_by_calling_the_tool_again(
    store,
) -> None:
    """The write path refuses to touch it; recovery is the reconciler's job.

    This is the guard that keeps the next test honest. If `write_expense` were
    willing to carry on from `submitting`, a client retry could race the
    reconciler over one client token.
    """
    from personal_agent_core.errors import AppError, ErrorCode

    sessions, exhaustion = store
    fake = FakeFeishu()
    exhaustion.arm(sql_contains="external_receipts", verb="INSERT")
    with pytest.raises(SAOperationalError):
        write(fake, sessions)

    with pytest.raises(AppError) as raised:
        write(fake, sessions)

    assert raised.value.code is ErrorCode.SOURCE_COMMIT_UNKNOWN
    assert fake.rows == 1, "a refused retry must not create a second record"


def test_the_reconciler_resolves_the_stranded_write_to_one_record(
    tmp_path: Path,
) -> None:
    """The designed recovery: promote `submitting`, replay the same token, verify.

    Run end to end rather than asserted about, because the whole point is that
    the second submit reaches Feishu and still yields one row.
    """
    from personal_agent_core.crypto import KeyRing, generate_key
    from personal_data_mcp.finance.reconciler import reconcile_write
    from personal_data_mcp.storage.execution_store import scan_unfinished

    engine = create_database_engine(tmp_path / "finance.sqlite")
    create_all(engine)
    exhaustion = Exhaustion(engine, genuine_sqlite_full(tmp_path))
    sessions = session_factory(engine)
    keyring = KeyRing([generate_key("chaos-2026")], service="personal_data_mcp")
    fake = FakeFeishu()

    async def stranded() -> None:
        async with adapter_for(fake) as adapter:
            await write_expense(
                LUNCH,
                sessions=sessions,
                adapter=adapter,
                config=CONFIG,
                validation=VALIDATION,
                source=SOURCE,
                idempotency_key=KEY,
                request_fingerprint="fp-1",
                trace_id="trace-1",
                keyring=keyring,
            )

    exhaustion.arm(sql_contains="external_receipts", verb="INSERT")
    with pytest.raises(SAOperationalError):
        asyncio.run(stranded())
    assert fake.rows == 1 and execution(sessions).state == "submitting"

    # A stranded `submitting` is what the recovery scan picks up: it is not a
    # terminal state, so `scan_unfinished` finds it without any special case.
    with sessions() as session:
        assert [row.idempotency_key for row in scan_unfinished(session)] == [KEY]

    async def recover():
        async with adapter_for(fake) as adapter:
            return await reconcile_write(
                KEY,
                sessions=sessions,
                adapter=adapter,
                source=SOURCE,
                config=CONFIG,
                validation=VALIDATION,
                keyring=keyring,
                owner="worker-2",
            )

    result = asyncio.run(recover())

    assert result.final_state == "succeeded"
    assert fake.rows == 1, "the same client token must dedupe, not duplicate"
    assert len(fake.creates) == 2, "two submits, one record"
    assert receipts(sessions)[0].record_id == result.record_id
    engine.dispose()


def test_exhausted_before_succeeded_does_not_report_success(store) -> None:
    """Step 6 refused: verified in the ledger, unrecorded here, and not claimed."""
    sessions, exhaustion = store
    fake = FakeFeishu()
    # Two updates to `tool_executions` precede this one: submitting (step 2)
    # and committed_unverified (step 4).
    exhaustion.arm(sql_contains="tool_executions", verb="UPDATE", after=2)

    with pytest.raises(SAOperationalError):
        write(fake, sessions)

    assert exhaustion.fired
    assert fake.rows == 1
    assert execution(sessions).state == "committed_unverified"
    assert receipts(sessions)[0].verified_at is None


class ControlWithExecution:
    """A control plane that answers the one question that decides the claim.

    Only `get_execution` matters here; `get_pending_duplicate_check` is present
    because the duplicate branch shares the same client.
    """

    def __init__(
        self,
        execution: dict[str, Any] | None,
        *,
        error: Exception | None = None,
    ) -> None:
        self._execution = execution
        self._error = error
        self.asked: list[str] = []

    async def get_execution(self, idempotency_key: str):
        self.asked.append(idempotency_key)
        if self._error is not None:
            raise self._error
        return self._execution

    async def get_pending_duplicate_check(self, idempotency_key: str):
        return None


def test_the_wire_carries_a_code_not_a_filesystem_path(tmp_path: Path) -> None:
    """Whatever else goes wrong, a driver's text must not reach the client.

    A `database or disk is full` message names the database file, which names
    the service data directory.
    """
    from personal_data_mcp.server.app import dispatch
    from personal_data_mcp.server.handlers import ToolInvocation, ToolRegistry

    sys.path.insert(0, str(Path(__file__).parents[1] / "fixtures"))
    from service_keys import SignedCaller

    error = genuine_sqlite_full(tmp_path)
    caller = SignedCaller(
        scopes=("meta.capabilities.read", "finance.expense.write")
    )
    registry = ToolRegistry()

    async def exhausted(_invocation: ToolInvocation) -> dict[str, Any]:
        raise error

    registry.register("finance.log_expense", exhausted)
    headers = {
        key.lower(): value
        for key, value in caller.headers("finance.log_expense", EXPENSE).items()
    }

    result = asyncio.run(
        dispatch(
            registry, caller.authorizer(), "finance.log_expense", EXPENSE, headers,
            shared_enabled_write_switch(),
        )
    )

    assert result.is_error is True
    body = result.content[0].text
    assert json.loads(body)["error"]["code"] == "INTERNAL_ERROR"
    assert "disk is full" not in body
    assert str(tmp_path) not in body and ".sqlite" not in body


def test_an_exhausted_write_is_not_reported_as_a_proven_zero_write(
    tmp_path: Path,
) -> None:
    """The gate: `INTERNAL_ERROR` after a create must not read as "nothing wrote".

    `CommitFailedSafe` resolves the operation to `failed_safe`, which the whole
    system treats as proof that no external record exists -- and which
    **releases the durable idempotency slot**, so the obvious user response
    ("it says it failed, send it again") can add a second ledger row.

    This is the exact state the storage-exhaustion tests above leave behind:
    Feishu holds the record and the Finance execution is stranded at
    `submitting`. The claim must follow that evidence, not the error code.
    """
    sys.path.insert(0, str(Path(__file__).parents[1] / "integration"))
    import test_finance_dispatcher as harness

    from personal_agent.api.intent import WriteIntent
    from personal_agent.api.orchestrator import CommitFailedSafe, CommitUnknown
    from personal_agent_core.errors import AppError, ErrorCode

    control = ControlWithExecution({"state": "submitting"})
    dispatcher = harness.dispatcher(
        harness.FakeBridge(error=AppError(ErrorCode.INTERNAL_ERROR)), control
    )

    outcome = dispatcher.commit(
        intent=WriteIntent(tool="finance.log_expense", model_args={}),
        idempotency_key="idem-exhausted",
        duplicate_override=None,
    )

    assert not isinstance(outcome, CommitFailedSafe), (
        "INTERNAL_ERROR is the code Finance emits for any failure it could not "
        "name, including one raised after the create landed; claiming a proven "
        "zero write on it can hide a real record"
    )
    assert isinstance(outcome, CommitUnknown)
    assert control.asked == ["idem-exhausted"], "the evidence must be read"


def test_no_error_code_alone_can_claim_a_proven_zero_write() -> None:
    """The claim comes from Finance's execution row, never from the code.

    Every code is driven twice against the same control-plane answer. If any
    code's outcome tracked the code rather than the evidence, the two runs would
    disagree with the evidence in one of them.
    """
    sys.path.insert(0, str(Path(__file__).parents[1] / "integration"))
    import test_finance_dispatcher as harness

    from personal_agent.api.intent import WriteIntent
    from personal_agent.api.orchestrator import CommitFailedSafe, CommitUnknown
    from personal_agent_core.errors import AppError, ErrorCode

    from personal_agent.api.finance_dispatcher import _ASSERTS_MAY_HAVE_WRITTEN

    # These two resolve to their own parking outcomes before the question is
    # reached, because Finance states their zero-write guarantee explicitly.
    own_outcome = {
        ErrorCode.POSSIBLE_DUPLICATE,
        ErrorCode.CLARIFICATION_REQUIRED,
    }
    def outcome_for(code: ErrorCode, execution: dict[str, Any] | None):
        control = ControlWithExecution(execution)
        dispatcher = harness.dispatcher(
            harness.FakeBridge(error=AppError(code)), control
        )
        return dispatcher.commit(
            intent=WriteIntent(tool="finance.log_expense", model_args={}),
            idempotency_key=f"idem-{code.value}",
            duplicate_override=None,
        )

    # Finance asserting the write may have landed is authoritative on its own:
    # no evidence read may soften it, even when no execution row is found.
    for code in _ASSERTS_MAY_HAVE_WRITTEN:
        assert isinstance(outcome_for(code, None), CommitUnknown), code

    for code in ErrorCode:
        if code in own_outcome or code in _ASSERTS_MAY_HAVE_WRITTEN:
            continue
        # No execution row: the `prepared` row commits before any network call,
        # so its absence proves the create was never attempted.
        assert isinstance(outcome_for(code, None), CommitFailedSafe), code
        # An execution past submit: the record may exist, whatever the code says.
        assert isinstance(
            outcome_for(code, {"state": "submitting"}), CommitUnknown
        ), code


@pytest.mark.parametrize(
    ("state", "proves_zero_write"),
    [
        # Committed before any network call.
        ("prepared", True),
        # Reachable only from `prepared`.
        ("cancelled_pre_submit", True),
        # Finance's own terminal claim that nothing reached the source.
        ("failed_safe", True),
        # Committed before the request leaves: it may have landed.
        ("submitting", False),
        ("commit_unknown", False),
        ("reconciling_same_client_token", False),
        ("committed_unverified", False),
        ("succeeded", False),
        ("needs_manual_review", False),
        # A state this Agent does not know is not a proof of anything.
        ("some_future_state", False),
    ],
)
def test_each_finance_state_maps_to_the_right_claim(
    state: str, proves_zero_write: bool
) -> None:
    sys.path.insert(0, str(Path(__file__).parents[1] / "integration"))
    import test_finance_dispatcher as harness

    from personal_agent.api.intent import WriteIntent
    from personal_agent.api.orchestrator import CommitFailedSafe
    from personal_agent_core.errors import AppError, ErrorCode

    dispatcher = harness.dispatcher(
        harness.FakeBridge(error=AppError(ErrorCode.INTERNAL_ERROR)),
        ControlWithExecution({"state": state}),
    )

    outcome = dispatcher.commit(
        intent=WriteIntent(tool="finance.log_expense", model_args={}),
        idempotency_key="idem-state",
        duplicate_override=None,
    )

    assert isinstance(outcome, CommitFailedSafe) is proves_zero_write


def test_a_local_refusal_does_not_depend_on_finance_being_reachable() -> None:
    """A denial decided here must not need Finance's opinion to be reported.

    Scope and allowlist are checked on this side, before anything is dispatched,
    so `zero writes` is already proven locally. Routing that through the control
    plane means that when Finance is down -- the very moment a permission error
    is most likely to be seen -- "you do not have permission, nothing was
    written" degrades into "unknown, needs manual review", which is both wrong
    and alarming.
    """
    sys.path.insert(0, str(Path(__file__).parents[1] / "integration"))
    import test_finance_dispatcher as harness

    from personal_agent.api.control_client import ControlPlaneError
    from personal_agent.api.intent import WriteIntent
    from personal_agent.api.orchestrator import CommitFailedSafe
    from personal_agent_core.errors import AppError, ErrorCode

    # The bridge refuses locally; the control plane is unreachable.
    bridge = harness.FakeBridge(error=AppError(ErrorCode.SCOPE_DENIED))
    bridge.refuses_locally = True
    control = ControlWithExecution(None, error=ControlPlaneError("unreachable"))
    dispatcher = harness.dispatcher(bridge, control)

    outcome = dispatcher.commit(
        intent=WriteIntent(tool="finance.log_expense", model_args={}),
        idempotency_key="idem-local-denial",
        duplicate_override=None,
    )

    assert isinstance(outcome, CommitFailedSafe)
    assert outcome.reason == ErrorCode.SCOPE_DENIED.value
    assert control.asked == [], (
        "a refusal decided before dispatch needs no evidence from Finance"
    )
    assert bridge.calls == [], "and nothing may have been dispatched"


def test_an_unreadable_control_plane_never_claims_a_zero_write() -> None:
    """Fail closed: not knowing is `CommitUnknown`, not "nothing was written"."""
    sys.path.insert(0, str(Path(__file__).parents[1] / "integration"))
    import test_finance_dispatcher as harness

    from personal_agent.api.control_client import ControlPlaneError
    from personal_agent.api.intent import WriteIntent
    from personal_agent.api.orchestrator import CommitUnknown
    from personal_agent_core.errors import AppError, ErrorCode

    for control in (
        ControlWithExecution(None, error=ControlPlaneError("unreachable")),
        # A body that does not carry a usable state is not evidence either.
        ControlWithExecution({"state": None}),
        ControlWithExecution({}),
    ):
        dispatcher = harness.dispatcher(
            harness.FakeBridge(error=AppError(ErrorCode.INTERNAL_ERROR)), control
        )
        outcome = dispatcher.commit(
            intent=WriteIntent(tool="finance.log_expense", model_args={}),
            idempotency_key="idem-unreadable",
            duplicate_override=None,
        )
        assert isinstance(outcome, CommitUnknown)


def test_a_refused_audit_append_takes_the_state_change_with_it(store) -> None:
    """Technical design 10.4 / DEV-034: audit failure fails the write closed.

    The audit row and the transition share one transaction precisely so this
    cannot become "the state moved but nothing recorded that it did".
    """
    sessions, exhaustion = store
    fake = FakeFeishu()
    exhaustion.arm(sql_contains="audit_events", verb="INSERT", after=2)

    with pytest.raises(SAOperationalError):
        write(fake, sessions)

    assert exhaustion.fired
    assert execution(sessions).state == "committed_unverified"
    assert "write_succeeded" not in audit_types(sessions)
    assert receipts(sessions)[0].verified_at is None
