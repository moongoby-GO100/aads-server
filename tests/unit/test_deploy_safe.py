"""deploy_safe 도구 단위 테스트."""
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

def _expected_reload_cmd():
    from app.services import tool_executor as module
    from app.services.tool_executor import ToolExecutor

    command_parts, _, unavailable_reason = ToolExecutor()._deploy_safe_resolve_command("reload", "")
    assert unavailable_reason == ""
    assert command_parts is not None
    if ToolExecutor._deploy_safe_in_container():
        assert command_parts == list(module._DEPLOY_SAFE_CONTAINER_RELOAD_CMD)
    else:
        assert command_parts == list(module._DEPLOY_SAFE_RELOAD_CMD)
    return ToolExecutor._deploy_safe_join_command(command_parts)


@pytest.mark.asyncio
async def test_deploy_safe_reload_dry_run_default_command():
    from app.services.tool_executor import ToolExecutor

    result = await ToolExecutor()._deploy_safe({"mode": "reload"})

    assert result["dry_run"] is True
    assert result["command"] == _expected_reload_cmd()
    assert "Python" in result["description"] or "reload" in result["description"].lower()


@pytest.mark.asyncio
async def test_deploy_safe_bluegreen_dry_run_container_unavailable():
    """컨테이너 내부에서 bluegreen은 호스트 컨텍스트가 없으면 불가."""
    from app.services.tool_executor import ToolExecutor

    result = await ToolExecutor()._deploy_safe({"mode": "bluegreen"})

    if "command" in result:
        assert "bluegreen" in result["command"]
    else:
        assert result["success"] is False
        assert "error" in result


@pytest.mark.asyncio
async def test_deploy_safe_null_dry_run_stays_safe():
    from app.services.tool_executor import ToolExecutor

    result = await ToolExecutor()._deploy_safe({"mode": "reload", "dry_run": None})

    assert result["dry_run"] is True
    assert result["command"] == _expected_reload_cmd()


@pytest.mark.asyncio
async def test_deploy_safe_restart_single_dry_run_container_unavailable():
    """컨테이너 내부에서 restart-single은 호스트 컨텍스트가 없으면 불가."""
    from app.services.tool_executor import ToolExecutor

    result = await ToolExecutor()._deploy_safe({
        "mode": "restart-single",
        "service": "litellm",
    })

    if "command" in result:
        assert "litellm" in result["command"]
    else:
        assert result["success"] is False
        assert "error" in result


@pytest.mark.asyncio
async def test_deploy_safe_restart_single_requires_service():
    from app.services.tool_executor import ToolExecutor

    result = await ToolExecutor()._deploy_safe({"mode": "restart-single"})

    assert "error" in result
    assert "service" in result["error"]


@pytest.mark.asyncio
async def test_deploy_safe_rejects_aads_server_restart_single():
    from app.services.tool_executor import ToolExecutor

    result = await ToolExecutor()._deploy_safe({
        "mode": "restart-single",
        "service": "aads-server",
    })

    assert "reload 또는 bluegreen" in result["error"]


@pytest.mark.asyncio
async def test_deploy_safe_rejects_forbidden_supervisor_restart_pattern():
    from app.services.tool_executor import ToolExecutor

    result = await ToolExecutor()._deploy_safe({
        "mode": "restart-single",
        "service": "litellm; supervisorctl restart aads-api",
    })

    assert "error" in result


@pytest.mark.asyncio
async def test_deploy_safe_rejects_full_compose_up_pattern():
    from app.services.tool_executor import ToolExecutor

    result = await ToolExecutor()._deploy_safe({
        "mode": "restart-single",
        "service": "litellm; docker compose up -d",
    })

    assert "error" in result


@pytest.mark.asyncio
async def test_deploy_safe_rejects_force_pattern():
    from app.services.tool_executor import ToolExecutor

    result = await ToolExecutor()._deploy_safe({
        "mode": "restart-single",
        "service": "litellm --force",
    })

    assert "error" in result
    assert "--force" in result["error"]


@pytest.mark.asyncio
async def test_deploy_safe_rejects_compose_up_without_no_deps_pattern():
    from app.services.tool_executor import ToolExecutor

    result = await ToolExecutor()._deploy_safe({
        "mode": "restart-single",
        "service": "litellm; docker compose up app",
    })

    assert "error" in result
    assert "--no-deps 없는 전체 up" in result["error"]


@pytest.mark.asyncio
async def test_deploy_safe_execute_uses_subprocess_with_health_checks(monkeypatch):
    from app.services import tool_executor as module
    from app.services.tool_executor import ToolExecutor

    executor = ToolExecutor()
    calls = []

    async def fake_run_subprocess(parts):
        calls.append(parts)
        return {
            "ok": True,
            "command": " ".join(parts),
            "returncode": 0,
            "stdout": "ok",
            "stderr": "",
        }

    async def fake_sleep(seconds):
        calls.append({"sleep": seconds})

    monkeypatch.setattr(executor, "_deploy_safe_run_subprocess", fake_run_subprocess)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    result = await executor._deploy_safe({"mode": "reload", "dry_run": False})

    assert result["success"] is True
    assert result["command"] == _expected_reload_cmd()
    assert calls[0] == list(module._DEPLOY_SAFE_HEALTH_ARGS)
    command_parts, _, unavailable_reason = ToolExecutor()._deploy_safe_resolve_command("reload", "")
    assert unavailable_reason == ""
    assert command_parts is not None
    assert calls[1] == command_parts
    assert calls[2] == {"sleep": 5}
    assert calls[3] == list(module._DEPLOY_SAFE_HEALTH_ARGS)


@pytest.mark.asyncio
async def test_deploy_safe_async_mode_starts_background_without_post_poll(monkeypatch):
    from app.services import tool_executor as module
    from app.services.tool_executor import ToolExecutor

    executor = ToolExecutor()
    calls = []

    async def fake_run_subprocess(parts):
        calls.append(("run", parts))
        return {
            "ok": True,
            "command": " ".join(parts),
            "returncode": 0,
            "stdout": "ok",
            "stderr": "",
        }

    async def fake_spawn_subprocess(parts):
        calls.append(("spawn", parts))
        return {
            "ok": True,
            "command": " ".join(parts),
            "pid": 12345,
            "log_path": "/root/aads/aads-server/logs/deploy-safe-test.log",
            "status": "started",
        }

    async def fake_sleep(seconds):
        calls.append(("sleep", seconds))

    monkeypatch.setattr(executor, "_deploy_safe_run_subprocess", fake_run_subprocess)
    monkeypatch.setattr(executor, "_deploy_safe_spawn_subprocess", fake_spawn_subprocess)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    result = await executor._deploy_safe({"mode": "reload", "dry_run": False, "async_mode": True})

    assert result["success"] is True
    assert result["async_mode"] is True
    assert result["status"] == "started"
    assert result["post_health"]["ok"] is None
    assert calls == [
        ("run", list(module._DEPLOY_SAFE_HEALTH_ARGS)),
        ("spawn", list(module._DEPLOY_SAFE_RELOAD_CMD if not ToolExecutor._deploy_safe_in_container() else module._DEPLOY_SAFE_CONTAINER_RELOAD_CMD)),
    ]


@pytest.mark.asyncio
async def test_deploy_safe_dispatch_registered():
    from app.services.tool_executor import ToolExecutor

    raw = await ToolExecutor().execute("deploy_safe", {"mode": "reload"})
    result = json.loads(raw)

    assert result["dry_run"] is True
    assert result["command"] == _expected_reload_cmd()
