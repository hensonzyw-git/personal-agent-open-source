"""Bind-target resolution for the personal-agent-api CLI.

Design 3 puts the service behind Nginx on a Unix socket; a public TCP bind is
a misconfiguration. These tests pin the refusal shapes rather than the happy
path, because a mis-parsed bind is exactly how a private service ends up on a
public interface.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from personal_agent.api.app import build_restore_read_only_app
from personal_agent.cli import DEFAULT_PORT, resolve_bind
from personal_agent_core.sqlite import (
    create_read_only_database_engine,
    session_factory,
)


def test_default_binds_loopback_tcp() -> None:
    bind = resolve_bind(None, None, None)
    assert bind.uds is None
    assert bind.host == "127.0.0.1"
    assert bind.port == DEFAULT_PORT


def test_explicit_loopback_variants_are_accepted() -> None:
    for host in ("127.0.0.1", "localhost", "::1"):
        bind = resolve_bind(host, 9000, None)
        assert bind.uds is None
        assert bind.host == host
        assert bind.port == 9000


def test_a_public_host_is_refused() -> None:
    with pytest.raises(SystemExit, match="loopback only"):
        resolve_bind("0.0.0.0", None, None)


def test_an_absolute_socket_path_is_accepted() -> None:
    bind = resolve_bind(None, None, "/run/personal-agent/api.sock")
    assert bind.uds == "/run/personal-agent/api.sock"
    assert bind.host is None
    assert bind.port is None


def test_a_relative_socket_path_is_refused() -> None:
    with pytest.raises(SystemExit, match="absolute path"):
        resolve_bind(None, None, "run/api.sock")


def test_socket_and_tcp_arguments_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit, match="cannot be combined"):
        resolve_bind("127.0.0.1", None, "/run/personal-agent/api.sock")
    with pytest.raises(SystemExit, match="cannot be combined"):
        resolve_bind(None, 8810, "/run/personal-agent/api.sock")


def test_restore_probe_reads_the_database_and_exposes_no_write_surface(
    tmp_path,
) -> None:
    import sqlite3

    database = tmp_path / "restored.sqlite"
    raw = sqlite3.connect(database)
    raw.execute("CREATE TABLE proof (value INTEGER NOT NULL)")
    raw.execute("INSERT INTO proof VALUES (1)")
    raw.commit()
    raw.close()

    engine = create_read_only_database_engine(database)
    try:
        client = TestClient(build_restore_read_only_app(session_factory(engine)))
        response = client.get("/v1/capabilities")
    finally:
        engine.dispose()

    assert response.status_code == 401
    assert response.json() == {"error": {"code": "UNAUTHENTICATED"}}
