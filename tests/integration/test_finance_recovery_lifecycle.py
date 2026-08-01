"""Production composition owns Finance recovery for its whole lifetime."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

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
    monkeypatch.setattr(composition, "load_protected_config", lambda _: config)
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

    async def recover(_dependencies, *, owner, validation=None):
        calls.append(validation)
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

    assert calls[0] is not None, "startup must reuse boot-time schema validation"
    assert calls[1] is None, "periodic scans must obtain fresh schema validation"
    assert adapter.closed and fx.closed


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

    monkeypatch.setattr(composition, "scan_unfinished", lambda _session: rows)

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
            validation=object(),
        )
    )

    assert attempted == ["first", "second"]
    assert results == [("second", "succeeded")]
