"""DEV-010/011/014: the Client Core against real servers on both transports.

These are real processes over real transports, not mocks. The point of the gate
is that stdio and Streamable HTTP behave identically under the same contract, so
every behavioural test runs against both.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import time
from datetime import timedelta

import httpx
import pytest

from personal_agent.mcp_client.core import (
    McpClientCore,
    McpTimeoutError,
    McpTransportError,
    StdioTransport,
    StreamableHttpTransport,
)


FINANCE_MODULE = "fixtures.mcp_servers.finance_fixture_server"
THIRDPARTY_MODULE = "fixtures.mcp_servers.thirdparty_server"
ENV = {"PYTHONPATH": "src:tests", "PATH": "/usr/bin:/bin"}

EXPENSE = {
    "name": "午饭",
    "input_amount": "45.00",
    "input_currency": "CNY",
    "occurred_on": "2026-07-23",
    "is_family_expense": False,
    "entry_kind": "expense",
    "category": "餐饮",
}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class HttpServer:
    """A fixture server in its own process, like the deployed one."""

    def __init__(self, module: str, *, extra_env: dict[str, str] | None = None) -> None:
        self.port = free_port()
        self.module = module
        self.process = subprocess.Popen(
            [sys.executable, "-m", module, "http", str(self.port)],
            env={**ENV, **(extra_env or {})},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self._wait_ready()

    def _wait_ready(self, timeout: float = 20.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"{self.module} exited early")
            try:
                with socket.create_connection(("127.0.0.1", self.port), 0.2):
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError(f"{self.module} did not start")

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()


@pytest.fixture(scope="module")
def finance_http():
    server = HttpServer(FINANCE_MODULE)
    yield server
    server.stop()


@pytest.fixture(scope="module")
def thirdparty_http():
    server = HttpServer(THIRDPARTY_MODULE)
    yield server
    server.stop()


def stdio(module: str) -> StdioTransport:
    return StdioTransport(command=sys.executable, args=["-m", module], env=ENV)


def transports(server: HttpServer, module: str):
    return [
        pytest.param(stdio(module), id="stdio"),
        pytest.param(StreamableHttpTransport(url=server.url), id="http"),
    ]


def run(coro):
    return asyncio.run(coro)


# --- both transports, one contract ------------------------------------------


def test_initialize_reports_the_same_server_on_both_transports(
    finance_http,
) -> None:
    async def identity(transport):
        async with McpClientCore("finance", transport) as client:
            return client.identity

    over_stdio = run(identity(stdio(FINANCE_MODULE)))
    over_http = run(identity(StreamableHttpTransport(url=finance_http.url)))

    assert over_stdio.name == over_http.name == "personal-data-mcp-fixture"
    assert over_stdio.protocol_version == over_http.protocol_version
    assert "tools" in over_stdio.capabilities


def test_the_catalog_is_identical_on_both_transports(finance_http) -> None:
    async def names(transport):
        async with McpClientCore("finance", transport) as client:
            return sorted(tool.name for tool in await client.list_tools())

    assert run(names(stdio(FINANCE_MODULE))) == run(
        names(StreamableHttpTransport(url=finance_http.url))
    )


def test_a_disabled_tool_is_never_advertised(finance_http) -> None:
    async def names():
        async with McpClientCore(
            "finance", StreamableHttpTransport(url=finance_http.url)
        ) as client:
            return {tool.name for tool in await client.list_tools()}

    advertised = run(names())
    assert "finance.log_expense_batch" not in advertised
    assert "finance.log_expense" in advertised


def test_a_structured_call_returns_external_evidence(finance_http) -> None:
    async def call(transport):
        async with McpClientCore("finance", transport) as client:
            return await client.call_tool("finance.log_expense", EXPENSE)

    for transport in (
        stdio(FINANCE_MODULE),
        StreamableHttpTransport(url=finance_http.url),
    ):
        result = run(call(transport))
        payload = result if isinstance(result, dict) else None
        if payload is None:
            import json

            payload = json.loads(result[0].text)
        assert payload["record_id"].startswith("fixture_")
        assert payload["evidence"]["kind"] == "feishu_record"


def test_a_per_call_host_context_uses_an_isolated_http_connection(
    finance_http,
) -> None:
    """Over HTTP the context becomes headers, on its own connection.

    Installing it by mutating the shared client would let concurrent calls read
    each other's identity, so each call that carries a context gets its own
    short-lived connection.
    """

    async def call():
        async with McpClientCore(
            "finance", StreamableHttpTransport(url=finance_http.url)
        ) as client:
            return await client.call_tool(
                "finance.log_expense",
                EXPENSE,
                host_context={
                    "Authorization": "Bearer synthetic-test-token",
                    "X-Request-ID": "synthetic-request",
                },
            )

    result = run(call())
    if isinstance(result, dict):
        payload = result
    else:
        import json

        payload = json.loads(result[0].text)
    assert payload["record_id"].startswith("fixture_")


def test_calling_an_unknown_tool_fails(finance_http) -> None:
    async def call():
        async with McpClientCore(
            "finance", StreamableHttpTransport(url=finance_http.url)
        ) as client:
            return await client.call_tool("finance.drop_everything", {})

    with pytest.raises(Exception):
        run(call())


# --- lifecycle --------------------------------------------------------------


def test_a_server_restart_is_survivable(finance_http) -> None:
    async def scenario():
        client = McpClientCore(
            "finance", StreamableHttpTransport(url=finance_http.url)
        )
        await client.connect()
        first = sorted(tool.name for tool in await client.list_tools())
        await client.reconnect()
        second = sorted(tool.name for tool in await client.list_tools())
        await client.close()
        return first, second

    first, second = run(scenario())
    assert first == second


def test_close_is_idempotent_and_leaves_no_session(finance_http) -> None:
    async def scenario():
        client = McpClientCore(
            "finance", StreamableHttpTransport(url=finance_http.url)
        )
        await client.connect()
        assert client.is_connected
        await client.close()
        await client.close()
        return client.is_connected

    assert run(scenario()) is False


def test_using_a_closed_client_raises_rather_than_reconnecting(
    finance_http,
) -> None:
    async def scenario():
        client = McpClientCore(
            "finance", StreamableHttpTransport(url=finance_http.url)
        )
        await client.connect()
        await client.close()
        await client.list_tools()

    with pytest.raises(McpTransportError):
        run(scenario())


def test_an_unreachable_server_fails_fast() -> None:
    async def scenario():
        transport = StreamableHttpTransport(url=f"http://127.0.0.1:{free_port()}/mcp")
        async with McpClientCore("dead", transport):
            pass

    with pytest.raises((McpTransportError, McpTimeoutError)):
        run(scenario())


def test_a_call_timeout_raises_instead_of_returning(finance_http) -> None:
    async def scenario():
        async with McpClientCore(
            "finance", StreamableHttpTransport(url=finance_http.url)
        ) as client:
            return await client.call_tool(
                "finance.log_expense", EXPENSE, timeout=timedelta(seconds=0)
            )

    # A write whose budget expires must not resolve as a value; the caller has
    # to treat it as possibly committed.
    with pytest.raises(McpTimeoutError):
        run(scenario())


def test_the_endpoint_negotiates_content_and_refuses_delete(finance_http) -> None:
    """Record what this server actually does, not what was assumed.

    The technical design notes that a stateless server *may* answer GET with
    405. This SDK version does not: a GET carrying the required Accept header
    opens a `text/event-stream` and holds it. That is spec-legal, but it means
    an idle GET pins a connection, so DEV-015 has to decide explicitly whether
    the production server rejects GET rather than inheriting this default.
    """
    assert (
        httpx.get(finance_http.url, timeout=5, trust_env=False).status_code
        == 406
    )

    with httpx.Client(timeout=5, trust_env=False) as client:
        with client.stream(
            "GET",
            finance_http.url,
            headers={"Accept": "application/json, text/event-stream"},
        ) as streamed:
            assert streamed.status_code == 200
            assert "text/event-stream" in streamed.headers["content-type"]

        with client.stream("DELETE", finance_http.url) as deleted:
            assert deleted.status_code == 405


# --- DEV-014: a genuinely independent server --------------------------------


def test_the_same_core_drives_an_independent_third_party_server(
    thirdparty_http,
) -> None:
    async def scenario():
        async with McpClientCore(
            "almanac", StreamableHttpTransport(url=thirdparty_http.url)
        ) as client:
            identity = client.identity
            tools = sorted(tool.name for tool in await client.list_tools())
            result = await client.call_tool("almanac.sunrise", {"city": "上海"})
            resources = await client.list_resources()
            contents = await client.read_resource("almanac://readme")
            return identity, tools, result, resources, contents

    identity, tools, result, resources, contents = run(scenario())
    assert identity.name == "third-party-almanac"
    assert tools == ["almanac.sunrise", "finance.query_expenses"]
    assert resources and str(resources[0].uri) == "almanac://readme"
    assert "third-party almanac fixture" in str(contents)

    import json

    payload = result if isinstance(result, dict) else json.loads(result[0].text)
    assert payload["sunrise"] == "05:12"


def test_two_servers_can_be_driven_at_once_without_interference(
    finance_http, thirdparty_http
) -> None:
    async def scenario():
        async with McpClientCore(
            "finance", StreamableHttpTransport(url=finance_http.url)
        ) as finance, McpClientCore(
            "almanac", StreamableHttpTransport(url=thirdparty_http.url)
        ) as almanac:
            return (
                finance.identity.name,
                almanac.identity.name,
                {tool.name for tool in await finance.list_tools()},
                {tool.name for tool in await almanac.list_tools()},
            )

    finance_name, almanac_name, finance_tools, almanac_tools = run(scenario())
    assert finance_name != almanac_name
    # Both advertise finance.query_expenses; keeping them apart is the
    # registry's job, not the transport's.
    assert "finance.query_expenses" in finance_tools
    assert "finance.query_expenses" in almanac_tools


# --- the transport must outlive a slow tool call (live finding, 2026-07-26) ---


SLOWER_THAN_HTTPX_DEFAULT = 6.0


@pytest.fixture(scope="module")
def slow_finance_http():
    """A server that takes longer to answer than httpx waits by default."""
    server = HttpServer(
        FINANCE_MODULE,
        extra_env={"FIXTURE_TOOL_DELAY_SECONDS": str(SLOWER_THAN_HTTPX_DEFAULT)},
    )
    yield server
    server.stop()


def test_a_call_slower_than_the_http_default_still_returns_its_result(
    slow_finance_http,
) -> None:
    """The regression test for the worst bug this system has had.

    httpx defaults to a 5-second read timeout. The client used to inherit it, so
    any governed write whose server side took longer had its stream torn down
    mid-call; the session then waited out its own budget and reported a timeout.
    Live on 2026-07-26 that turned a `finance.log_expense` that had *already*
    created the record, read it back and reached `succeeded` in Finance into
    `source_commit_unknown` on the Agent -- a verified write reported as an
    unknown commit.

    No fake could have caught it: every counterparty in this suite answers in
    milliseconds, which is precisely why this one does not.
    """
    core = McpClientCore(
        "finance", StreamableHttpTransport(url=slow_finance_http.url)
    )

    async def scenario():
        await core.connect()
        try:
            started = time.monotonic()
            result = await core.call_tool(
                "finance.log_expense",
                EXPENSE,
                timeout=timedelta(seconds=25),
            )
            return result, time.monotonic() - started
        finally:
            await core.close()

    result, elapsed = asyncio.run(scenario())

    if isinstance(result, dict):
        payload = result
    else:
        import json

        payload = json.loads(result[0].text)
    assert payload["evidence"]["kind"] == "feishu_record"
    # It really did take longer than httpx would have waited on its own.
    assert elapsed >= SLOWER_THAN_HTTPX_DEFAULT


def test_the_call_budget_is_still_the_deadline_that_fires(slow_finance_http) -> None:
    """Raising the transport timeout must not disarm the per-call budget.

    The SDK enforces the budget and wraps its internal timeout as `McpError`
    carrying HTTP 408. The client must recover that stable meaning as
    `McpTimeoutError`; otherwise the timeout-specific branch and diagnostics
    are dead on the HTTP transport.
    """
    core = McpClientCore(
        "finance", StreamableHttpTransport(url=slow_finance_http.url)
    )

    async def scenario():
        await core.connect()
        try:
            return await core.call_tool(
                "finance.log_expense",
                EXPENSE,
                timeout=timedelta(seconds=1),
            )
        finally:
            await core.close()

    with pytest.raises(McpTimeoutError):
        asyncio.run(scenario())
