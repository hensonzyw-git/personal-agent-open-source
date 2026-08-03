"""DEV-039: the external-write kill switch, at both layers.

The failing cases were written before the implementation, per §5.1. The shape of
the risk here is unusual: this control's *default* is the thing that can be
wrong, and a wrong default is silent. So the reader tests are all about what
happens when the state file is anything other than a well-formed `enabled` --
missing, a directory, a symlink, truncated, oversized, unreadable, or carrying a
value this build has never heard of. Every one of them must refuse.

The enforcement tests never assert "it raised". They count execution rows,
because "the write tool refused" and "nothing reached the fact source" are
different claims and only the second one matters.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from fixtures.service_keys import SignedCaller
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.write_switch import (
    MAX_STATE_FILE_BYTES,
    WRITES_DISABLED,
    WRITES_ENABLED,
    WRITE_SWITCH_PATH_ENV,
    WriteSwitch,
    WriteSwitchConfigError,
    disabled_write_switch,
    load_write_switch,
    render_state_file,
)
from personal_data_mcp.server.app import dispatch
from personal_data_mcp.server.handlers import ToolInvocation, ToolRegistry
from personal_data_mcp.server.meta import build_handler as build_meta_handler
from personal_data_mcp.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_data_mcp.storage.execution_store import prepare_execution
from personal_data_mcp.storage.models import ToolExecution
from write_switch_fixtures import write_switch_state


NOW = datetime(2026, 8, 2, 7, 0, tzinfo=timezone.utc)
EXPENSE = {
    "name": "午饭",
    "input_amount": "45.00",
    "input_currency": "CNY",
    "occurred_on": "2026-08-02",
    "is_family_expense": False,
    "entry_kind": "expense",
    "category": "餐饮",
}


def run(coro):
    return asyncio.run(coro)


# --- the reader: everything that is not a clean `enabled` refuses -------------


def test_a_well_formed_enabled_file_allows_writes(tmp_path: Path) -> None:
    switch = WriteSwitch(
        write_switch_state(tmp_path / "s.json", writes=WRITES_ENABLED)
    )
    assert switch.read().writes_allowed is True


def test_a_well_formed_disabled_file_refuses(tmp_path: Path) -> None:
    switch = WriteSwitch(
        write_switch_state(tmp_path / "s.json", writes=WRITES_DISABLED)
    )
    state = switch.read()
    assert state.writes_allowed is False
    assert "kill switch" in state.detail


def test_a_missing_state_file_refuses(tmp_path: Path) -> None:
    """The whole design decision, in one assertion.

    "No file" is the state a fresh host, a botched deploy or an accidental
    deletion produces. If that meant "writes allowed", the switch could
    disengage without anyone seeing it happen.
    """
    switch = WriteSwitch(tmp_path / "absent.json")
    assert switch.read().writes_allowed is False


def test_a_directory_at_the_state_path_refuses(tmp_path: Path) -> None:
    (tmp_path / "s.json").mkdir()
    assert WriteSwitch(tmp_path / "s.json").read().writes_allowed is False


def test_a_symlink_is_refused_rather_than_followed(tmp_path: Path) -> None:
    """Following one would move the answer to a path nobody configured."""
    real = write_switch_state(tmp_path / "real.json", writes=WRITES_ENABLED)
    link = tmp_path / "link.json"
    link.symlink_to(real)
    assert WriteSwitch(link).read().writes_allowed is False


@pytest.mark.parametrize(
    "body, why",
    [
        pytest.param("", "empty", id="empty"),
        pytest.param("{", "truncated", id="truncated-json"),
        pytest.param('["enabled"]', "not an object", id="json-array"),
        pytest.param('"enabled"', "not an object", id="json-string"),
        pytest.param("{}", "missing key", id="no-writes-key"),
        pytest.param('{"writes": "Enabled"}', "case", id="wrong-case"),
        pytest.param('{"writes": "ENABLED"}', "case", id="upper-case"),
        pytest.param('{"writes": " enabled "}', "padding", id="padded-value"),
        pytest.param('{"writes": true}', "not a string", id="boolean"),
        pytest.param('{"writes": "on"}', "unknown word", id="unknown-word"),
        pytest.param(
            '{"writes": "enabled", "override": true}',
            "unknown key",
            id="unknown-key",
        ),
    ],
)
def test_a_malformed_state_file_refuses(tmp_path: Path, body: str, why: str) -> None:
    path = tmp_path / "s.json"
    path.write_text(body, encoding="utf-8")
    assert WriteSwitch(path).read().writes_allowed is False, why


def test_an_oversized_state_file_refuses_before_it_is_parsed(tmp_path: Path) -> None:
    """A log redirected onto the path, or a padded file, is refused by size."""
    path = tmp_path / "s.json"
    padding = " " * (MAX_STATE_FILE_BYTES + 1)
    path.write_text(
        json.dumps({"writes": WRITES_ENABLED}) + padding, encoding="utf-8"
    )
    assert WriteSwitch(path).read().writes_allowed is False


def test_a_non_utf8_state_file_refuses(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_bytes(b'{"writes": "\xff\xfe"}')
    assert WriteSwitch(path).read().writes_allowed is False


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_an_unreadable_state_file_refuses(tmp_path: Path) -> None:
    path = write_switch_state(tmp_path / "s.json", writes=WRITES_ENABLED)
    os.chmod(path, 0o000)
    try:
        assert WriteSwitch(path).read().writes_allowed is False
    finally:
        os.chmod(path, 0o600)


def test_a_relative_path_is_refused_at_construction(tmp_path: Path) -> None:
    with pytest.raises(WriteSwitchConfigError):
        WriteSwitch(Path("write-switch.json"))


def test_the_state_is_reread_on_every_call(tmp_path: Path) -> None:
    """No caching. A cached kill switch is a delayed kill switch."""
    path = write_switch_state(tmp_path / "s.json", writes=WRITES_ENABLED)
    switch = WriteSwitch(path)
    assert switch.read().writes_allowed is True
    write_switch_state(path, writes=WRITES_DISABLED)
    assert switch.read().writes_allowed is False
    write_switch_state(path, writes=WRITES_ENABLED)
    assert switch.read().writes_allowed is True


# --- configuration: a service without a switch must not start ----------------


def test_loading_without_the_environment_variable_is_refused() -> None:
    with pytest.raises(WriteSwitchConfigError):
        load_write_switch(env={})
    with pytest.raises(WriteSwitchConfigError):
        load_write_switch(env={WRITE_SWITCH_PATH_ENV: "   "})


def test_loading_uses_the_configured_path(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    switch = load_write_switch(env={WRITE_SWITCH_PATH_ENV: str(path)})
    assert switch.path == path


def test_the_pinned_off_switch_cannot_be_talked_into_allowing_writes() -> None:
    switch = disabled_write_switch("restore probe")
    assert switch.read().writes_allowed is False
    # And there is no counterpart. An "always enabled" helper would be a way to
    # obtain writes with no state file, which is the hole the design closes.
    import personal_agent_core.write_switch as module

    assert not [
        name
        for name in dir(module)
        if "enabled" in name.lower() and callable(getattr(module, name))
    ]


def test_rendering_refuses_a_change_without_a_reason() -> None:
    with pytest.raises(WriteSwitchConfigError):
        render_state_file(writes=WRITES_DISABLED, reason="  ", changed_at="t")
    with pytest.raises(WriteSwitchConfigError):
        render_state_file(writes="maybe", reason="why", changed_at="t")


# --- layer 2: the Finance MCP gate, measured in execution rows ---------------


@pytest.fixture()
def finance_session(tmp_path: Path):
    engine = create_database_engine(tmp_path / "finance.sqlite")
    create_all(engine)
    with session_factory(engine)() as session:
        yield session
    engine.dispose()


def execution_count(session) -> int:
    return len(list(session.scalars(select(ToolExecution))))


def registry_with_probes(finance_session) -> ToolRegistry:
    """Handlers that create an execution row, so absence is real evidence."""
    registry = ToolRegistry()
    registry.register("meta.capabilities", build_meta_handler(registry))

    def probe(tool: str):
        async def handler(invocation: ToolInvocation) -> dict:
            prepare_execution(
                finance_session,
                idempotency_key=invocation.verified_call.idempotency_key,
                tool=tool,
                request_fingerprint="fp",
                client_token=str(uuid.uuid4()),
                encrypted_payload=None,
                now=NOW,
            )
            finance_session.commit()
            return {
                "status": "created",
                "record_id": "probe",
                "source_system": "test",
                "committed_at": "2026-08-02T15:00:00+08:00",
            }

        return handler

    async def query(_: ToolInvocation) -> dict:
        return {"items": [], "total_count": 0, "has_more": False}

    registry.register("finance.log_expense", probe("finance.log_expense"))
    registry.register("finance.query_expenses", query)
    return registry


ALL_SCOPES = (
    "meta.capabilities.read",
    "finance.expense.write",
    "finance.expense.read",
)


def test_a_disabled_switch_refuses_the_write_with_zero_execution_rows(
    finance_session, tmp_path: Path
) -> None:
    registry = registry_with_probes(finance_session)
    caller = SignedCaller(scopes=ALL_SCOPES)
    headers = {
        k.lower(): v
        for k, v in caller.headers("finance.log_expense", EXPENSE).items()
    }
    switch = WriteSwitch(
        write_switch_state(tmp_path / "s.json", writes=WRITES_DISABLED)
    )

    result = run(
        dispatch(
            registry,
            caller.authorizer(),
            "finance.log_expense",
            EXPENSE,
            headers,
            switch,
        )
    )

    assert result.is_error is True
    body = json.loads(result.content[0].text)
    assert body["error"]["code"] == ErrorCode.WRITES_DISABLED.value
    # The claim that matters: the handler never ran.
    assert execution_count(finance_session) == 0


def test_the_positive_control_shows_a_row_is_reachable(
    finance_session, tmp_path: Path
) -> None:
    """Without this, the test above would pass on a broken probe."""
    registry = registry_with_probes(finance_session)
    caller = SignedCaller(scopes=ALL_SCOPES)
    headers = {
        k.lower(): v
        for k, v in caller.headers("finance.log_expense", EXPENSE).items()
    }
    switch = WriteSwitch(
        write_switch_state(tmp_path / "s.json", writes=WRITES_ENABLED)
    )

    result = run(
        dispatch(
            registry,
            caller.authorizer(),
            "finance.log_expense",
            EXPENSE,
            headers,
            switch,
        )
    )

    assert result.is_error is False
    assert execution_count(finance_session) == 1


@pytest.mark.parametrize(
    "tool, arguments",
    [
        ("finance.query_expenses", {"view": "total"}),
        ("meta.capabilities", {}),
    ],
)
def test_reads_keep_working_while_writes_are_disabled(
    finance_session, tmp_path: Path, tool: str, arguments: dict
) -> None:
    """Design 10.6: close the write tools, keep read-only and device state."""
    registry = registry_with_probes(finance_session)
    caller = SignedCaller(scopes=ALL_SCOPES)
    headers = {k.lower(): v for k, v in caller.headers(tool, arguments).items()}
    switch = WriteSwitch(
        write_switch_state(tmp_path / "s.json", writes=WRITES_DISABLED)
    )

    result = run(
        dispatch(
            registry, caller.authorizer(), tool, arguments, headers, switch
        )
    )

    assert result.is_error is False


def test_flipping_the_file_takes_effect_without_restarting_anything(
    finance_session, tmp_path: Path
) -> None:
    """The reason this is a file and not an environment variable.

    A restart would tear down whatever was in flight at `submitting` and
    manufacture `commit_unknown` rows -- turning "stop new writes" into "strand
    the writes already running".
    """
    registry = registry_with_probes(finance_session)
    caller = SignedCaller(scopes=ALL_SCOPES)
    path = write_switch_state(tmp_path / "s.json", writes=WRITES_ENABLED)
    switch = WriteSwitch(path)

    def call() -> dict:
        headers = {
            k.lower(): v
            for k, v in caller.headers("finance.log_expense", EXPENSE).items()
        }
        result = run(
            dispatch(
                registry,
                caller.authorizer(),
                "finance.log_expense",
                EXPENSE,
                headers,
                switch,
            )
        )
        return {"error": result.is_error}

    assert call()["error"] is False
    assert execution_count(finance_session) == 1

    write_switch_state(path, writes=WRITES_DISABLED, reason="incident")
    assert call()["error"] is True
    assert execution_count(finance_session) == 1

    write_switch_state(path, writes=WRITES_ENABLED, reason="resolved")
    assert call()["error"] is False
    assert execution_count(finance_session) == 2


def test_the_refusal_leaks_neither_the_path_nor_the_operators_reason(
    finance_session, tmp_path: Path
) -> None:
    registry = registry_with_probes(finance_session)
    caller = SignedCaller(scopes=ALL_SCOPES)
    headers = {
        k.lower(): v
        for k, v in caller.headers("finance.log_expense", EXPENSE).items()
    }
    switch = WriteSwitch(
        write_switch_state(
            tmp_path / "secret-name.json",
            writes=WRITES_DISABLED,
            reason="账本被写坏了 /var/lib/personal-data-mcp/finance.sqlite",
        )
    )

    result = run(
        dispatch(
            registry,
            caller.authorizer(),
            "finance.log_expense",
            EXPENSE,
            headers,
            switch,
        )
    )

    wire = result.content[0].text
    assert "secret-name" not in wire
    assert "/var/lib" not in wire
    assert "账本被写坏了" not in wire


# --- the operator CLI --------------------------------------------------------


def cli(*args: str, env: dict[str, str] | None = None):
    return subprocess.run(
        [sys.executable, "-m", "personal_agent_core.write_switch_cli", *args],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin", **(env or {})},
    )


def test_the_cli_disables_then_enables_and_reports_by_exit_code(
    tmp_path: Path,
) -> None:
    path = tmp_path / "switch.json"

    # Nothing exists yet. Writes are refused, but that is not a decision anyone
    # made, so it must not report as one: exit 2, not 1.
    assert cli("--path", str(path), "status").returncode == 2

    assert cli("--path", str(path), "disable", "--reason", "drill").returncode == 1
    assert WriteSwitch(path).read().writes_allowed is False
    assert cli("--path", str(path), "status").returncode == 1

    assert cli("--path", str(path), "enable", "--reason", "drill over").returncode == 0
    assert WriteSwitch(path).read().writes_allowed is True
    assert cli("--path", str(path), "status").returncode == 0


def test_a_corrupt_state_file_is_not_reported_as_a_decision(tmp_path: Path) -> None:
    """Exit 2, not 1.

    A file somebody garbled and a file somebody deliberately set to `disabled`
    produce the same refusal to every caller -- which is correct -- but they are
    not the same fact. Reporting the fault as a decision is how a fault stops
    being looked at.
    """
    path = tmp_path / "switch.json"
    path.write_text('{"writes": "disabl', encoding="utf-8")
    assert cli("--path", str(path), "status").returncode == 2


def test_a_deliberate_state_is_marked_deliberate(tmp_path: Path) -> None:
    for writes in (WRITES_ENABLED, WRITES_DISABLED):
        state = WriteSwitch(
            write_switch_state(tmp_path / "s.json", writes=writes)
        ).read()
        assert state.deliberate is True
    assert WriteSwitch(tmp_path / "gone.json").read().deliberate is False


def test_the_cli_writes_a_file_both_services_can_read(tmp_path: Path) -> None:
    """Two different service users read it; only the operator writes it."""
    path = tmp_path / "switch.json"
    cli("--path", str(path), "enable", "--reason", "install")
    assert path.stat().st_mode & 0o777 == 0o644
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["writes"] == WRITES_ENABLED
    assert document["reason"] == "install"
    assert document["changed_at"]


def test_the_cli_refuses_a_change_without_a_reason(tmp_path: Path) -> None:
    path = tmp_path / "switch.json"
    result = cli("--path", str(path), "disable")
    assert result.returncode != 0
    assert not path.exists()


def test_the_cli_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    path = tmp_path / "switch.json"
    cli("--path", str(path), "disable", "--reason", "drill")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["switch.json"]


def test_the_cli_takes_the_path_from_the_environment_when_not_given(
    tmp_path: Path,
) -> None:
    path = tmp_path / "switch.json"
    result = cli(
        "status", env={WRITE_SWITCH_PATH_ENV: str(path)}
    )
    assert result.returncode == 2
    assert str(path) in result.stdout


def test_the_cli_refuses_a_relative_path(tmp_path: Path) -> None:
    assert cli("--path", "switch.json", "status").returncode != 0


# --- composition: a service with no switch configured must not start ---------


@pytest.mark.parametrize(
    "entrypoint, args",
    [
        ("personal_data_mcp.cli", ["--host", "127.0.0.1", "--port", "0"]),
        (
            "personal_agent.cli",
            ["--host", "127.0.0.1", "--port", "0", "--database", "unused.sqlite"],
        ),
    ],
)
def test_a_service_without_a_configured_switch_refuses_to_start(
    entrypoint: str, args: list[str], tmp_path: Path
) -> None:
    """The env var is not optional in either entrypoint.

    Without this, a deployment that forgot the setting would start happily and
    then have to decide what "no switch" means at the first write -- which is
    the ambiguity the whole design removes.
    """
    # Through the console entrypoint's own `main`, which is what systemd runs.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; from {entrypoint} import main; "
            f"sys.argv = ['svc', *{args!r}]; main()",
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env={
            "PYTHONPATH": str(Path.cwd() / "src"),
            "PATH": "/usr/bin:/bin",
            "PERSONAL_AGENT_USER_ID": "henson",
        },
        timeout=60,
    )
    assert result.returncode != 0
    assert WRITE_SWITCH_PATH_ENV in result.stderr
