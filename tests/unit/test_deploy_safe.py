"""deploy_safe 도구 단위 테스트."""
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))


@pytest.fixture(autouse=True)
def deploy_safe_host_context(monkeypatch):
    from app.services import tool_executor as module

    original_exists = os.path.exists

    def fake_exists(path):
        if path == "/.dockerenv":
            return False
        if path in {
            "/root/aads/aads-server/deploy.sh",
            "/root/aads/aads-server/docker-compose.prod.yml",
            "/root/aads/aads-server/scripts/reload-api.sh",
        }:
            return True
        return original_exists(path)

    monkeypatch.setattr(module.os.path, "exists", fake_exists)
    monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)


@pytest.mark.asyncio
async def test_deploy_safe_reload_dry_run_default_command():
    from app.services.tool_executor import ToolExecutor

    result = await ToolExecutor()._deploy_safe({"mode": "reload"})

    assert result["dry_run"] is True
    assert result["command"] == "bash /root/aads/aads-server/scripts/reload-api.sh"
    assert "Python" in result["description"]


@pytest.mark.asyncio
async def test_deploy_safe_bluegreen_dry_run_command():
    from app.services.tool_executor import ToolExecutor

    result = await ToolExecutor()._deploy_safe({"mode": "bluegreen"})

    assert result["dry_run"] is True
    assert result["command"] == "bash /root/aads/aads-server/deploy.sh bluegreen"


@pytest.mark.asyncio
async def test_deploy_safe_null_dry_run_stays_safe():
    from app.services.tool_executor import ToolExecutor

    result = await ToolExecutor()._deploy_safe({"mode": "reload", "dry_run": None})

    assert result["dry_run"] is True
    assert result["command"] == "bash /root/aads/aads-server/scripts/reload-api.sh"


@pytest.mark.asyncio
async def test_deploy_safe_reload_uses_container_script_inside_container(monkeypatch):
    from app.services import tool_executor as module
    from app.services.tool_executor import ToolExecutor

    def fake_exists(path):
        return path in {"/.dockerenv", "/app/scripts/reload-api.sh"}

    monkeypatch.setattr(module.os.path, "exists", fake_exists)
    monkeypatch.setattr(module.shutil, "which", lambda name: None)

    result = await ToolExecutor()._deploy_safe({"mode": "reload"})

    assert result["dry_run"] is True
    assert result["command"] == "bash /app/scripts/reload-api.sh"


@pytest.mark.asyncio
async def test_deploy_safe_restart_single_dry_run_command():
    from app.services.tool_executor import ToolExecutor

    result = await ToolExecutor()._deploy_safe({
        "mode": "restart-single",
        "service": "litellm",
    })

    assert result["dry_run"] is True
    assert result["command"] == (
        "docker compose -f /root/aads/aads-server/docker-compose.prod.yml restart litellm"
    )


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

    assert result == {"error": "aads-server는 reload 또는 bluegreen 모드를 사용하세요"}


@pytest.mark.asyncio
async def test_deploy_safe_rejects_forbidden_supervisor_restart_pattern():
    from app.services.tool_executor import ToolExecutor

    result = await ToolExecutor()._deploy_safe({
        "mode": "restart-single",
        "service": "litellm; supervisorctl restart aads-api",
    })

    assert "error" in result
    assert "supervisorctl restart" in result["error"]


@pytest.mark.asyncio
async def test_deploy_safe_rejects_full_compose_up_pattern():
    from app.services.tool_executor import ToolExecutor

    result = await ToolExecutor()._deploy_safe({
        "mode": "restart-single",
        "service": "litellm; docker compose up -d",
    })

    assert "error" in result
    assert "docker compose up -d" in result["error"]


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
async def test_deploy_safe_bluegreen_inside_container_without_host_context_errors(monkeypatch):
    from app.services import tool_executor as module
    from app.services.tool_executor import ToolExecutor

    monkeypatch.setattr(module.os.path, "exists", lambda path: path == "/.dockerenv")
    monkeypatch.setattr(module.shutil, "which", lambda name: None)

    result = await ToolExecutor()._deploy_safe({"mode": "bluegreen", "dry_run": False})

    assert result["success"] is False
    assert "호스트 docker CLI" in result["error"]


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
    assert result["command"] == "bash /root/aads/aads-server/scripts/reload-api.sh"
    assert calls == [
        module._DEPLOY_SAFE_HEALTH_ARGS,
        ["bash", "/root/aads/aads-server/scripts/reload-api.sh"],
        {"sleep": 5},
        module._DEPLOY_SAFE_HEALTH_ARGS,
    ]


@pytest.mark.asyncio
async def test_deploy_safe_dispatch_registered():
    from app.services.tool_executor import ToolExecutor

    raw = await ToolExecutor().execute("deploy_safe", {"mode": "reload"})
    result = json.loads(raw)

    assert result["dry_run"] is True
    assert result["command"] == "bash /root/aads/aads-server/scripts/reload-api.sh"
