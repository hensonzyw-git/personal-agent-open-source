"""Bind-target resolution for the personal-agent-api CLI.

Design 3 puts the service behind Nginx on a Unix socket; a public TCP bind is
a misconfiguration. These tests pin the refusal shapes rather than the happy
path, because a mis-parsed bind is exactly how a private service ends up on a
public interface.
"""

from __future__ import annotations

import pytest

from personal_agent.cli import DEFAULT_PORT, resolve_bind


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
