"""DEV-038: the `MCP restart` and `网络断开` rows of technical design 11.1.

`tests/chaos/test_execution_fault_matrix.py` kills the process at a durability
boundary and `test_storage_exhaustion.py` fails the commit itself. Both stay
inside one service. This file breaks the *seam between* the two services, which
is where a restart and a lost network actually land.

One existing test needed replacing rather than extending.
`test_a_server_restart_is_survivable` in `tests/integration/test_mcp_client_core.py`
calls `client.reconnect()` against a server that is still running and was never
restarted, so what it proves is that a client can reconnect -- not that anything
survives a restart. The server here really dies: the process is killed and a new
one is started on the same port, so the client reconnects to a genuinely
different process with an empty memory.

The property under test is the one the whole two-service split exists to
protect: **a request whose fate is unknown is never reported as a failure**, and
no restart turns one intended write into two.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from personal_agent.mcp_client.core import (
    McpClientCore,
    McpTransportError,
    StreamableHttpTransport,
)


FINANCE_MODULE = "fixtures.mcp_servers.finance_fixture_server"
ENV = {"PYTHONPATH": "src:tests", "PATH": "/usr/bin:/bin"}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class RestartableServer:
    """A fixture MCP server that can really die and come back on the same port.

    Holding the port fixed is what makes this a *restart* rather than a second
    server: the client's configured URL is unchanged, exactly as it is when
    systemd restarts the deployed unit under a socket the Agent keeps pointing
    at.
    """

    def __init__(self, module: str = FINANCE_MODULE) -> None:
        self.module = module
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self.process: subprocess.Popen | None = None
        self.start()

    def start(self) -> None:
        assert self.process is None, "already running"
        self.process = subprocess.Popen(
            [sys.executable, "-m", self.module, "http", str(self.port)],
            env=ENV,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._wait(up=True)

    def kill(self) -> None:
        """Stop hard, the way a crash or an OOM kill stops a service."""
        if self.process is None:
            return
        self.process.kill()
        self.process.wait(timeout=10)
        self.process = None
        self._wait(up=False)

    def restart(self) -> None:
        self.kill()
        self.start()

    def _wait(self, *, up: bool, timeout: float = 20.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), 0.2):
                    reachable = True
            except OSError:
                reachable = False
            if reachable is up:
                return
            time.sleep(0.1)
        raise RuntimeError(
            f"{self.module} did not become {'reachable' if up else 'unreachable'}"
        )


@pytest.fixture()
def server():
    instance = RestartableServer()
    yield instance
    instance.kill()


def run(coro):
    return asyncio.run(coro)


# --- a restart that is actually a restart -------------------------------------


def test_a_real_process_restart_is_survivable(server: RestartableServer) -> None:
    """Kill the process, start a new one on the same port, keep working."""

    async def scenario():
        client = McpClientCore(
            "finance", StreamableHttpTransport(url=server.url)
        )
        await client.connect()
        before = sorted(tool.name for tool in await client.list_tools())
        first_pid = server.process.pid

        server.restart()

        await client.reconnect()
        after = sorted(tool.name for tool in await client.list_tools())
        await client.close()
        return before, after, first_pid, server.process.pid

    before, after, first_pid, second_pid = run(scenario())

    assert first_pid != second_pid, "the point of this test is a new process"
    assert before == after and before, "the catalog must survive the restart"


def test_a_client_that_never_reconnects_fails_rather_than_serving_stale(
    server: RestartableServer,
) -> None:
    """A killed server must surface as a transport error, not a stale answer.

    The dangerous alternative is a client that quietly keeps answering from its
    last catalog: the Agent would then compute an allowlist intersection against
    tools nobody is serving.
    """

    async def scenario():
        client = McpClientCore(
            "finance", StreamableHttpTransport(url=server.url)
        )
        await client.connect()
        await client.list_tools()
        server.kill()
        try:
            await client.list_tools()
        finally:
            await client.close()

    with pytest.raises(McpTransportError):
        run(scenario())


def test_calling_a_dead_server_raises_instead_of_returning_a_result(
    server: RestartableServer,
) -> None:
    """`网络断开`: a call with nowhere to go must never produce a value.

    A returned value here would be indistinguishable from a receipt, and the
    dispatcher would read it as a completed write.
    """

    async def scenario():
        client = McpClientCore(
            "finance", StreamableHttpTransport(url=server.url)
        )
        await client.connect()
        server.kill()
        try:
            return await client.call_tool("meta.capabilities", {})
        finally:
            await client.close()

    with pytest.raises(McpTransportError):
        run(scenario())


def test_a_connect_to_a_dead_port_fails_closed(server: RestartableServer) -> None:
    """Discovery against a down service is a refusal, not an empty catalog.

    An empty catalog would be read as "this connector offers no tools", which is
    a legitimate steady state; being unable to ask is not.
    """
    server.kill()

    async def scenario():
        client = McpClientCore(
            "finance", StreamableHttpTransport(url=server.url)
        )
        await client.connect()
        return await client.list_tools()

    with pytest.raises(McpTransportError):
        run(scenario())


def test_discovery_against_a_dead_service_names_the_service(
    server: RestartableServer,
) -> None:
    """The operator-facing payoff of a uniform transport-error vocabulary.

    Composition catches `McpTransportError` to turn an unreachable connector
    into a diagnosis. While `list_tools` could leak a raw SDK error, a Finance
    MCP that died during discovery skipped that branch entirely and the operator
    got an SDK traceback instead of a sentence naming the service.

    The service must be killed *between* connect and `tools/list`, not before
    it: a dead port fails at connect, which was never the leaking path. So the
    real client is used unchanged and only the moment of death is chosen.
    """
    from personal_agent.api.composition import CompositionError, _discover
    from personal_agent.mcp_client.registry import ConnectorRegistry

    class DiesAfterConnecting(McpClientCore):
        async def connect(self):
            identity = await super().connect()
            server.kill()
            return identity

    async def scenario():
        client = DiesAfterConnecting(
            "finance", StreamableHttpTransport(url=server.url)
        )
        return await _discover(client, ConnectorRegistry(), "finance")

    with pytest.raises(CompositionError) as raised:
        run(scenario())

    assert "could not be reached for discovery" in str(raised.value)


# --- what the seam above it does with that failure ----------------------------


def _dispatcher_with(error: Exception):
    sys.path.insert(0, str(Path(__file__).parents[1] / "integration"))
    import test_finance_dispatcher as harness

    control = harness.FakeControl(execution=None)
    return harness.dispatcher(harness.FakeBridge(error=error), control), control


def test_a_torn_down_call_is_an_unknown_commit_not_a_failure() -> None:
    """A transport failure mid-write parks the operation; it never resolves it."""
    from personal_agent.api.intent import WriteIntent
    from personal_agent.api.orchestrator import CommitUnknown

    for error in (
        McpTransportError("connection reset"),
        __import__(
            "personal_agent.mcp_client.core", fromlist=["McpTimeoutError"]
        ).McpTimeoutError("timed out"),
    ):
        dispatcher, _ = _dispatcher_with(error)
        outcome = dispatcher.commit(
            intent=WriteIntent(tool="finance.log_expense", model_args={}),
            idempotency_key="idem-transport",
            duplicate_override=None,
        )
        assert isinstance(outcome, CommitUnknown)


def test_a_transport_failure_does_not_ask_finance_whether_it_wrote() -> None:
    """The control-plane evidence is only conclusive once Finance has *finished*.

    A structured error means the handler ran to completion, so "no execution
    row" proves the create was never attempted. A torn-down connection proves
    nothing of the sort: the request may be sitting in the server's accept queue
    with its `prepared` row still microseconds away. Reading the control plane
    here would turn a race into a confident "nothing was written", which is the
    one answer that must never be wrong.

    So this asserts an *absence*: the read is not attempted at all.
    """
    from personal_agent.api.intent import WriteIntent
    from personal_agent.api.orchestrator import CommitUnknown

    dispatcher, control = _dispatcher_with(McpTransportError("connection reset"))

    outcome = dispatcher.commit(
        intent=WriteIntent(tool="finance.log_expense", model_args={}),
        idempotency_key="idem-inflight",
        duplicate_override=None,
    )

    assert isinstance(outcome, CommitUnknown)
    assert control.asked_execution == [], (
        "an in-flight request has not necessarily created its execution row "
        "yet, so its absence is a race rather than evidence"
    )
