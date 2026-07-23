"""DEV-015 Commit 1: the production Finance MCP server skeleton.

The server runs in its own process over a real loopback socket, driven by the
same independent Client Core the Agent backend uses. These are the transport and
discovery behaviours that will be deployed, not a fixture's approximation.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import time

import httpx
import pytest

from personal_agent.mcp_client.core import McpClientCore, StreamableHttpTransport
from personal_agent_core.manifest import load_manifest


PROD_MODULE = "fixtures.production_mcp_server"
ENV = {"PYTHONPATH": "src:tests", "PATH": "/usr/bin:/bin"}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ProdServer:
    def __init__(self) -> None:
        self.port = free_port()
        self.process = subprocess.Popen(
            [sys.executable, "-m", PROD_MODULE, str(self.port)],
            env=ENV,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self._wait_ready()

    def _wait_ready(self, timeout: float = 20.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"{PROD_MODULE} exited early")
            try:
                with socket.create_connection(("127.0.0.1", self.port), 0.2):
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError(f"{PROD_MODULE} did not start")

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()


@pytest.fixture(scope="module")
def prod_server():
    server = ProdServer()
    yield server
    server.stop()


def run(coro):
    return asyncio.run(coro)


# --- discovery: only enabled, only built ------------------------------------


def test_catalog_advertises_only_the_built_tools(prod_server) -> None:
    async def scenario():
        async with McpClientCore(
            "finance", StreamableHttpTransport(url=prod_server.url)
        ) as client:
            tools = await client.list_tools()
            return [tool.name for tool in tools]

    names = run(scenario())
    # At Commit 1 only meta.capabilities has a handler. The Finance write tools
    # are enabled contracts but have no connector yet, so they are absent.
    assert names == ["meta.capabilities"]


def test_disabled_batch_tool_is_not_discoverable(prod_server) -> None:
    async def scenario():
        async with McpClientCore(
            "finance", StreamableHttpTransport(url=prod_server.url)
        ) as client:
            tools = await client.list_tools()
            return {tool.name for tool in tools}

    names = run(scenario())
    assert "finance.log_expense_batch" not in names
    # And it is genuinely disabled in the source of truth, so this test is not
    # passing merely because the tool is unbuilt.
    manifest = load_manifest()
    disabled = {t["name"] for t in manifest["tools"] if not t["enabled"]}
    assert "finance.log_expense_batch" in disabled


def test_meta_capabilities_reports_the_built_surface(prod_server) -> None:
    async def scenario():
        async with McpClientCore(
            "finance", StreamableHttpTransport(url=prod_server.url)
        ) as client:
            return await client.call_tool("meta.capabilities", {})

    result = run(scenario())
    assert result["status"] == "ok"
    assert [t["name"] for t in result["tools"]] == ["meta.capabilities"]
    assert result["allowed_tools_version"]


def test_calling_a_disabled_tool_by_name_is_refused(prod_server) -> None:
    """Guessing a disabled tool's name does not reach an execution path."""

    async def scenario():
        async with McpClientCore(
            "finance", StreamableHttpTransport(url=prod_server.url)
        ) as client:
            return await client.call_tool("finance.log_expense_batch", {})

    from personal_agent_core.errors import AppError, ErrorCode

    with pytest.raises(AppError) as caught:
        run(scenario())
    # Same code as for an unknown name: the surface cannot be mapped by
    # comparing error codes.
    assert caught.value.code == ErrorCode.TOOL_NOT_ALLOWLISTED


# --- transport: GET refused, DELETE refused ---------------------------------


def test_get_on_the_mcp_path_is_refused_with_405(prod_server) -> None:
    """The explicit DEV-015 decision: this server rejects GET.

    Not inherited from the SDK, which would hold a stream open. A negotiated
    Accept header must still get 405, otherwise the decision would be trivially
    bypassable.
    """
    plain = httpx.get(prod_server.url, timeout=5, trust_env=False)
    assert plain.status_code == 405
    assert plain.headers["allow"] == "POST"

    negotiated = httpx.get(
        prod_server.url,
        timeout=5,
        trust_env=False,
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert negotiated.status_code == 405


def test_delete_on_the_mcp_path_is_refused(prod_server) -> None:
    with httpx.Client(timeout=5, trust_env=False) as client:
        deleted = client.request("DELETE", prod_server.url)
    assert deleted.status_code == 405


def test_post_still_works_after_the_guard(prod_server) -> None:
    """The guard refuses GET without breaking the POST path it wraps."""

    async def scenario():
        async with McpClientCore(
            "finance", StreamableHttpTransport(url=prod_server.url)
        ) as client:
            return await client.call_tool("meta.capabilities", {})

    assert run(scenario())["status"] == "ok"
