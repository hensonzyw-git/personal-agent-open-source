import asyncio

from personal_agent_spike.framework_probe import run_probe


def test_frameworks_reach_offline_boundary_without_credentials() -> None:
    result = asyncio.run(run_probe())

    # ADK's McpToolset requires mcp<2 and is incompatible with mcp v2.
    # The probe reports this honestly rather than pretending to work.
    adk = result["google_adk"]
    assert adk["model_call_attempted"] is False
    if adk.get("mcp_tool_discovery") is True:
        assert "finance.log_expense" in adk["tool_names"]
    else:
        assert "error" in adk

    # claude-agent-sdk was dropped (requires mcp<2). The probe reports
    # "not installed" rather than crashing on import.
    assert result["claude_agent_sdk"]["client_constructed"] is False
    assert result["claude_agent_sdk"]["client_connected"] is False
    assert result["claude_agent_sdk"]["model_call_attempted"] is False
