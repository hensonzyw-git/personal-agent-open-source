import asyncio

from personal_agent_spike.framework_probe import run_probe


def test_frameworks_reach_offline_boundary_without_credentials() -> None:
    result = asyncio.run(run_probe())

    assert result["google_adk"]["mcp_tool_discovery"] is True
    assert result["google_adk"]["model_call_attempted"] is False
    assert "finance.log_expense" in result["google_adk"]["tool_names"]

    assert result["claude_agent_sdk"]["mcp_config_constructed"] is True
    assert result["claude_agent_sdk"]["client_constructed"] is True
    assert result["claude_agent_sdk"]["client_connected"] is False
    assert result["claude_agent_sdk"]["model_call_attempted"] is False
