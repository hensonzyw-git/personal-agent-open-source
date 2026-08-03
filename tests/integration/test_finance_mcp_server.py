"""DEV-015 Commit 1: the production Finance MCP server skeleton.

The server runs in its own process over a real loopback socket, driven by the
same independent Client Core the Agent backend uses. These are the transport and
discovery behaviours that will be deployed, not a fixture's approximation.
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from fixtures.service_keys import SignedCaller
from personal_agent.mcp_client.core import McpClientCore, StreamableHttpTransport
from personal_agent_core.manifest import load_manifest
from personal_agent_core.write_switch import WRITES_ENABLED, render_state_file


PROD_MODULE = "fixtures.production_mcp_server"
BASE_ENV = {"PYTHONPATH": "src:tests", "PATH": "/usr/bin:/bin"}
MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {
        "name": "personal-agent-wire-test",
        "version": "0.1",
    },
}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ProdServer:
    def __init__(self, key_env: dict[str, str]) -> None:
        self.port = free_port()
        # A real state file, read by the child through the production loader:
        # this process is the deployed entrypoint, so it must be configured the
        # way the deployed one is.
        self._switch_dir = tempfile.mkdtemp(prefix="prod-server-write-switch.")
        switch_path = Path(self._switch_dir) / "write-switch.json"
        switch_path.write_text(
            render_state_file(
                writes=WRITES_ENABLED,
                reason="server transport test",
                changed_at="2026-08-02T00:00:00+00:00",
            ),
            encoding="utf-8",
        )
        self.process = subprocess.Popen(
            [sys.executable, "-m", PROD_MODULE, str(self.port)],
            env={
                **BASE_ENV,
                "PERSONAL_AGENT_WRITE_SWITCH_FILE": str(switch_path),
                **key_env,
            },
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
        shutil.rmtree(self._switch_dir, ignore_errors=True)


@pytest.fixture(scope="module")
def caller(tmp_path_factory) -> SignedCaller:
    # A caller that carries both Finance and meta scopes, so a single instance
    # can sign every call in this module.
    return SignedCaller(
        scopes=("meta.capabilities.read", "finance.expense.write"),
    )


@pytest.fixture(scope="module")
def prod_server(caller, tmp_path_factory):
    key_dir = tmp_path_factory.mktemp("service_key")
    server = ProdServer(caller.env(key_dir))
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


def test_meta_capabilities_reports_the_built_surface(prod_server, caller) -> None:
    async def scenario():
        async with McpClientCore(
            "finance", StreamableHttpTransport(url=prod_server.url)
        ) as client:
            return await client.call_tool(
                "meta.capabilities",
                {},
                host_context=caller.headers("meta.capabilities", {}),
            )

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


def test_post_still_works_after_the_guard(prod_server, caller) -> None:
    """The guard refuses GET without breaking the POST path it wraps."""

    async def scenario():
        async with McpClientCore(
            "finance", StreamableHttpTransport(url=prod_server.url)
        ) as client:
            return await client.call_tool(
                "meta.capabilities",
                {},
                host_context=caller.headers("meta.capabilities", {}),
            )

    assert run(scenario())["status"] == "ok"


def test_omitted_tool_arguments_are_an_empty_object(
    prod_server, caller
) -> None:
    """MCP makes `arguments` optional; a no-argument tool must still work."""
    response = httpx.post(
        prod_server.url,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "meta.capabilities",
                "_meta": MODERN_META,
            },
        },
        headers={
            **caller.headers("meta.capabilities", {}),
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "tools/call",
            "Mcp-Name": "meta.capabilities",
            "Content-Type": "application/json",
        },
        timeout=10,
        trust_env=False,
    )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result.get("isError", False) is False
    assert result["structuredContent"]["status"] == "ok"


# --- the gate over the real transport ---------------------------------------


def test_a_call_without_a_host_context_is_refused(prod_server) -> None:
    """No signed context, no execution: loopback is not an auth boundary."""

    async def scenario():
        async with McpClientCore(
            "finance", StreamableHttpTransport(url=prod_server.url)
        ) as client:
            return await client.call_tool("meta.capabilities", {})

    from personal_agent_core.errors import AppError, ErrorCode

    with pytest.raises(AppError) as caught:
        run(scenario())
    assert caught.value.code == ErrorCode.HOST_CONTEXT_MISMATCH


def test_tampering_with_an_argument_after_signing_is_refused(
    prod_server, caller
) -> None:
    """Headers signed for one argument set cannot authorise another."""
    signed_for = caller.headers("meta.capabilities", {})

    async def scenario():
        async with McpClientCore(
            "finance", StreamableHttpTransport(url=prod_server.url)
        ) as client:
            # meta.capabilities takes no arguments, so any argument is already a
            # divergence from what was signed; the recomputed hash will not match.
            return await client.call_tool(
                "meta.capabilities", {"injected": 1}, host_context=signed_for
            )

    from personal_agent_core.errors import AppError, ErrorCode

    with pytest.raises(AppError) as caught:
        run(scenario())
    assert caught.value.code == ErrorCode.HOST_CONTEXT_MISMATCH


def test_a_token_from_an_untrusted_signer_is_refused(prod_server) -> None:
    """A well-formed token the server has no public key for is rejected."""

    async def scenario(headers):
        async with McpClientCore(
            "finance", StreamableHttpTransport(url=prod_server.url)
        ) as client:
            return await client.call_tool(
                "meta.capabilities", {}, host_context=headers
            )

    from personal_agent_core.errors import AppError, ErrorCode

    # A different key entirely; its kid is not in the server's ring.
    stranger = SignedCaller(kid="not-the-servers-key")
    with pytest.raises(AppError) as caught:
        run(scenario(stranger.headers("meta.capabilities", {})))
    assert caught.value.code == ErrorCode.HOST_CONTEXT_MISMATCH
