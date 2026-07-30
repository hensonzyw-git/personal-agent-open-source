import asyncio

from personal_agent_spike.client_probe import run_probe


def test_stdio_fixture_discovery_and_call() -> None:
    result = asyncio.run(run_probe())
    assert result["protocol_version"] == "2026-07-28"
    assert "finance.log_expense" in result["tool_names"]
    assert "meta.get_capabilities" in result["tool_names"]
    assert result["structured_result"]["status"] == "created"
    assert result["structured_result"]["evidence"]["kind"] == "fixture_record"
