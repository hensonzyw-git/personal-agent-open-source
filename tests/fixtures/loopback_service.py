"""Start the real Finance MCP service on a loopback port, in its own process.

Used by tests that need a genuine counterparty rather than an in-process fake:
the transport, the second authorisation gate and the internal control API are
the deployed ones. Pass a database path to get `/internal` as well.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path


MODULE = "fixtures.production_mcp_server"
BASE_ENV = {"PYTHONPATH": "src:tests", "PATH": "/usr/bin:/bin"}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LoopbackFinanceService:
    """The credential-free Finance MCP surface, over a real socket."""

    def __init__(
        self,
        key_env: dict[str, str],
        *,
        database: Path | None = None,
        write_fixture: bool = False,
    ) -> None:
        self.port = free_port()
        args = [sys.executable, "-m", MODULE, str(self.port)]
        if database is not None:
            args.append(str(database))
        if write_fixture:
            if database is None:
                raise ValueError("the write fixture requires a Finance database")
            args.append("write-fixture")
        self.process = subprocess.Popen(
            args,
            env={**BASE_ENV, **key_env},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._wait_ready()

    @property
    def mcp_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    @property
    def control_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _wait_ready(self, timeout: float = 20.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"{MODULE} exited early")
            try:
                with socket.create_connection(("127.0.0.1", self.port), 0.2):
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError(f"{MODULE} did not start")

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            self.process.kill()
            self.process.wait(timeout=10)
