"""PreToolUse decisions must use the installed Agent SDK hook wire contract."""

import pytest

from app.services.agent_hooks import pre_tool_use_hook


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name,tool_input",
    [
        ("Bash", {"command": "rm -rf /root/aads"}),
        ("Bash", {"command": "psql -c 'DROP TABLE users'"}),
        ("Write", {"file_path": "/root/aads/.env"}),
        ("run_remote_command", {"command": "sudo shutdown -h now"}),
        ("patch_remote_file", {"path": "/root/aads/.ssh/config"}),
    ],
)
async def test_dangerous_tool_uses_sdk_deny_contract(tool_name, tool_input):
    result = await pre_tool_use_hook({"tool_name": tool_name, "tool_input": tool_input})
    decision = result["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "deny"
    assert decision["permissionDecisionReason"]
    assert "behavior" not in result


@pytest.mark.asyncio
async def test_safe_tool_uses_sdk_allow_contract():
    result = await pre_tool_use_hook(
        {"tool_name": "Read", "tool_input": {"file_path": "/root/aads/README.md"}}
    )
    assert result == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        }
    }
