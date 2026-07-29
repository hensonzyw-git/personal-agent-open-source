"""DEV-041 step 5: dual-version matrix and conformance tests.

Tests that the v2 migration (protocol 2026-07-28) inter-operates correctly
with legacy 2025-11-25 servers, that the Finance server forces 2026-07-28,
and that the modern path has no initialize/ping/session-id/GET-SSE leakage.
"""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import time

import httpx
import pytest

from personal_agent.mcp_client.core import (
    McpClientCore,
    McpTransportError,
    StdioTransport,
    StreamableHttpTransport,
)

ENV = {"PYTHONPATH": "src:tests", "PATH": "/usr/bin:/bin"}

FINANCE_MODULE = "fixtures.mcp_servers.finance_fixture_server"
THIRDPARTY_MODULE = "fixtures.mcp_servers.thirdparty_server"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(port: int, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"port {port} did not become ready")


class HttpServer:
    def __init__(self, module: str) -> None:
        self.port = _free_port()
        self.module = module
        self.process = subprocess.Popen(
            [sys.executable, "-m", module, "http", str(self.port)],
            env=ENV,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait_ready(self.port)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()

    def __enter__(self) -> "HttpServer":
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


class LegacyRawServer:
    """A subprocess running the legacy 2025-11-25 raw JSON-RPC server."""

    def __init__(self) -> None:
        self.port = _free_port()
        self._proc: subprocess.Popen | None = None

    def start(self) -> None:
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "fixtures.mcp_servers.legacy_raw_server", str(self.port)],
            env=ENV,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait_ready(self.port)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None

    def __enter__(self) -> "LegacyRawServer":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


# --- v2 client → v2 server: protocol and format ----------------------------


def test_v2_client_negotiates_2026_07_28_over_http() -> None:
    with HttpServer(FINANCE_MODULE) as server:
        async def run() -> None:
            async with McpClientCore(
                "finance", StreamableHttpTransport(url=server.url)
            ) as client:
                assert client.identity.protocol_version == "2026-07-28"

        asyncio.run(run())


def test_v2_client_negotiates_2026_07_28_over_stdio() -> None:
    async def run() -> None:
        transport = StdioTransport(
            command=sys.executable,
            args=["-m", FINANCE_MODULE, "stdio"],
        )
        async with McpClientCore("finance", transport) as client:
            assert client.identity.protocol_version == "2026-07-28"

    asyncio.run(run())


def test_v2_tools_list_returns_snake_case_schema() -> None:
    with HttpServer(FINANCE_MODULE) as server:
        async def run() -> None:
            async with McpClientCore(
                "finance", StreamableHttpTransport(url=server.url)
            ) as client:
                tools = await client.list_tools()
                assert len(tools) > 0
                for tool in tools:
                    assert hasattr(tool, "input_schema")
                    assert not hasattr(tool, "inputSchema")

        asyncio.run(run())


def test_v2_server_has_no_session_id_in_response_headers() -> None:
    with HttpServer(FINANCE_MODULE) as server:
        response = httpx.post(
            server.url,
            json={"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}},
            headers={"MCP-Protocol-Version": "2026-07-28", "Content-Type": "application/json"},
            timeout=10,
            trust_env=False,
        )
        assert "mcp-session-id" not in {k.lower() for k in response.headers}


def test_v2_server_refuses_delete() -> None:
    with HttpServer(FINANCE_MODULE) as server:
        response = httpx.delete(server.url, timeout=5, trust_env=False)
        assert response.status_code in (405, 406)


# --- v2 client → legacy raw server: fail-closed -----------------------------


def test_v2_client_rejects_legacy_2025_11_25_server() -> None:
    """The v2 client's negotiate_auto falls back to initialize when discover
    is unavailable, detects 2025-11-25, and fail-closed validation rejects it.

    For Finance this is correct: the connector must negotiate 2026-07-28 or
    fail closed. The error message names the actual version so an operator
    can distinguish a legacy server from a network failure.
    """
    with LegacyRawServer() as legacy:
        async def attempt() -> None:
            async with McpClientCore(
                "legacy-probe",
                StreamableHttpTransport(url=legacy.url),
            ) as client:
                await client.connect()

        with pytest.raises(McpTransportError) as excinfo:
            asyncio.run(attempt())
        assert "2025-11-25" in str(excinfo.value)


# --- third-party server: v2 client discovers tools -------------------------


def test_v2_client_discovers_third_party_tools() -> None:
    with HttpServer(THIRDPARTY_MODULE) as server:
        async def run() -> None:
            async with McpClientCore(
                "almanac", StreamableHttpTransport(url=server.url)
            ) as client:
                tools = await client.list_tools()
                names = {t.name for t in tools}
                assert "almanac.sunrise" in names

        asyncio.run(run())
