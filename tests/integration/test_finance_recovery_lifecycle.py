"""Production composition owns Finance recovery for its whole lifetime."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from personal_agent_core.timeutil import utc_now
from personal_data_mcp.server import composition


class Closable:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def test_finance_composition_runs_startup_and_periodic_recovery(
    monkeypatch, tmp_path: Path
) -> None:
    adapter = Closable()
    fx = Closable()
    calls: list[object] = []

    config = SimpleNamespace(
        base_token="bas_test",
        ledger_kind="synthetic_test",
        tables={},
    )
    monkeypatch.setattr(
        composition, "load_protected_config", lambda _, **kwargs: config
    )
    monkeypatch.setattr(composition, "load_credentials", lambda: object())
    monkeypatch.setattr(composition, "load_base_source", lambda: object())
    monkeypatch.setattr(
        composition, "require_synthetic_test_base", lambda *_args, **_kwargs: object()
    )
    monkeypatch.setattr(composition, "FeishuAdapter", lambda *_args, **_kwargs: adapter)
    monkeypatch.setattr(composition, "FxConnector", lambda *_args, **_kwargs: fx)
    monkeypatch.setattr(composition, "load_data_keyring", lambda: object())
    monkeypatch.setattr(composition, "build_expense_handler", lambda _: object())
    monkeypatch.setattr(composition, "build_income_handler", lambda _: object())
    monkeypatch.setattr(composition, "build_family_fund_handler", lambda _: object())
    monkeypatch.setattr(composition, "build_registry", lambda **_: object())
    monkeypatch.setattr(composition, "build_record_reader", lambda _: object())

    async def validate(_dependencies):
        return object()

    async def recover(_dependencies, *, owner, limit=composition.RECOVERY_SCAN_LIMIT):
        calls.append(limit)
        return []

    monkeypatch.setattr(composition, "fresh_validation", validate)
    monkeypatch.setattr(composition, "recover_unfinished", recover)

    class Sessions:
        pass

    async def scenario() -> None:
        async with composition.finance_tools(
            config_path=tmp_path / "ledger.json",
            sessions=Sessions,
            now=utc_now,
            recovery_interval_seconds=0.01,
        ):
            for _ in range(100):
                if len(calls) >= 2:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("periodic Finance recovery never ran")

    asyncio.run(scenario())

    assert len(calls) >= 2
    assert set(calls) == {composition.RECOVERY_SCAN_LIMIT}
    assert adapter.closed and fx.closed


@pytest.mark.parametrize(
    ("cursor_secret", "should_advertise"),
    [(None, False), (b"q" * 32, True)],
)
def test_finance_composition_advertises_query_only_with_its_cursor_key(
    monkeypatch, tmp_path: Path, cursor_secret, should_advertise
) -> None:
    adapter = Closable()
    fx = Closable()
    source = object()
    validation = object()
    query_handler = object()
    captured_query_dependencies: list[object] = []
    captured_registry: list[dict[str, object]] = []
    config = SimpleNamespace(
        base_token="bas_test",
        ledger_kind="synthetic_test",
        tables={},
    )
    monkeypatch.setattr(
        composition, "load_protected_config", lambda _, **_kwargs: config
    )
    monkeypatch.setattr(composition, "load_credentials", lambda: object())
    monkeypatch.setattr(composition, "load_base_source", lambda: object())
    monkeypatch.setattr(
        composition,
        "require_synthetic_test_base",
        lambda *_args, **_kwargs: source,
    )
    monkeypatch.setattr(composition, "FeishuAdapter", lambda *_a, **_k: adapter)
    monkeypatch.setattr(composition, "FxConnector", lambda *_a, **_k: fx)
    monkeypatch.setattr(composition, "load_data_keyring", lambda: object())
    monkeypatch.setattr(
        composition,
        "load_query_cursor_secret",
        lambda **_kwargs: cursor_secret,
    )

    async def validate(_dependencies):
        return validation

    async def recover(*_args, **_kwargs):
        return []

    def build_query(dependencies):
        captured_query_dependencies.append(dependencies)
        return query_handler

    def build_registry(**handlers):
        captured_registry.append(handlers)
        return object()

    monkeypatch.setattr(composition, "fresh_validation", validate)
    monkeypatch.setattr(composition, "recover_unfinished", recover)
    monkeypatch.setattr(composition, "build_expense_query_handler", build_query)
    monkeypatch.setattr(composition, "build_expense_handler", lambda _: object())
    monkeypatch.setattr(composition, "build_income_handler", lambda _: object())
    monkeypatch.setattr(composition, "build_family_fund_handler", lambda _: object())
    monkeypatch.setattr(composition, "build_registry", build_registry)
    monkeypatch.setattr(composition, "build_record_reader", lambda _: object())

    async def scenario() -> None:
        async with composition.finance_tools(
            config_path=tmp_path / "ledger.json",
            sessions=object(),
            recovery_interval_seconds=3600,
        ):
            pass

    asyncio.run(scenario())

    if should_advertise:
        assert len(captured_query_dependencies) == 1
        dependencies = captured_query_dependencies[0]
        assert dependencies.adapter is adapter
        assert dependencies.source is source
        assert dependencies.config is config
        assert asyncio.run(dependencies.validate_schema()) is validation
        assert dependencies.cursor_secret == b"q" * 32
        assert captured_registry[0]["expense_query_handler"] is query_handler
    else:
        assert captured_query_dependencies == []
        assert captured_registry[0]["expense_query_handler"] is None


def test_one_failed_execution_does_not_suppress_the_rest_of_the_scan(
    monkeypatch,
) -> None:
    class SessionContext:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return None

    dependencies = SimpleNamespace(
        sessions=lambda: SessionContext(),
        adapter=object(),
        source=object(),
        config=object(),
        keyring=object(),
        now=utc_now,
    )
    rows = [
        SimpleNamespace(idempotency_key="first"),
        SimpleNamespace(idempotency_key="second"),
    ]
    attempted: list[str] = []

    observed_limits: list[int] = []

    def scan(_session, *, limit):
        observed_limits.append(limit)
        return rows

    monkeypatch.setattr(composition, "scan_unfinished", scan)

    validations: list[object] = []

    async def validate(_dependencies):
        value = object()
        validations.append(value)
        return value

    monkeypatch.setattr(composition, "fresh_validation", validate)

    async def reconcile(key, **_kwargs):
        attempted.append(key)
        if key == "first":
            raise RuntimeError("one broken row")
        return SimpleNamespace(final_state="succeeded")

    monkeypatch.setattr(composition, "reconcile_write", reconcile)
    results = asyncio.run(
        composition.recover_unfinished(
            dependencies,
            owner="worker",
        )
    )

    assert attempted == ["first", "second"]
    assert results == [("second", "succeeded")]
    assert observed_limits == [composition.RECOVERY_SCAN_LIMIT]
    assert len(validations) == 2, "every execution needs fresh live schema evidence"


def test_recovery_scan_is_bounded_before_network_work(monkeypatch) -> None:
    class SessionContext:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return None

    dependencies = SimpleNamespace(sessions=lambda: SessionContext())
    observed: list[int] = []

    def scan(_session, *, limit):
        observed.append(limit)
        return []

    monkeypatch.setattr(composition, "scan_unfinished", scan)

    assert asyncio.run(
        composition.recover_unfinished(dependencies, owner="worker", limit=7)
    ) == []
    assert observed == [7]
