from __future__ import annotations

import json

import pytest


def test_media_tools_registered_in_registry_and_mcp_definitions():
    from app.api.ceo_chat_tools import TOOL_DEFINITIONS
    from app.services.tool_registry import ToolRegistry

    expected = {
        "generate_image",
        "edit_image",
        "generate_video",
        "video_status",
        "video_download",
        "local_model_queue_status",
        "local_model_install_test",
        "generate_music",
        "generate_3d_asset",
        "media_job_status",
    }
    registry = ToolRegistry()

    assert expected <= set(registry.list_all())
    assert expected <= {tool["name"] for tool in TOOL_DEFINITIONS}


def test_tool_executor_dispatch_contains_all_media_tools():
    from app.services.tool_executor import ToolExecutor

    expected = {
        "generate_image",
        "edit_image",
        "generate_video",
        "video_status",
        "video_download",
        "local_model_queue_status",
        "local_model_install_test",
        "generate_music",
        "generate_3d_asset",
        "media_job_status",
    }

    assert expected <= ToolExecutor()._get_dispatch_tool_names()


@pytest.mark.asyncio
async def test_tool_executor_routes_generate_video(monkeypatch):
    from app.services import media_generation_service as media_module
    from app.services.tool_executor import ToolExecutor

    calls = []

    async def fake_generate_video(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return {"job_id": "media-test", "status": "failed", "error": "NOT_CONFIGURED"}

    monkeypatch.setattr(media_module.media_generation_service, "generate_video", fake_generate_video)

    raw = await ToolExecutor().execute(
        "generate_video",
        {"prompt": "short clip", "model_id": "sora-2-pro", "input_refs": {"ratio": "16:9"}},
    )
    result = json.loads(raw)

    assert result["job_id"] == "media-test"
    assert result["error"] == "NOT_CONFIGURED"
    assert calls == [
        (
            "short clip",
            {
                "input_refs": {"ratio": "16:9"},
                "model_id": "sora-2-pro",
                "provider": None,
                "session_id": "",
            },
        )
    ]


@pytest.mark.asyncio
async def test_tool_executor_routes_video_status(monkeypatch):
    from app.services import media_generation_service as media_module
    from app.services.tool_executor import ToolExecutor

    async def fake_video_status(job_id):
        return {"job_id": job_id, "status": "succeeded"}

    monkeypatch.setattr(media_module.media_generation_service, "video_status", fake_video_status)

    raw = await ToolExecutor().execute("video_status", {"job_id": "media-1"})
    result = json.loads(raw)

    assert result == {"job_id": "media-1", "status": "succeeded"}


@pytest.mark.asyncio
async def test_tool_executor_routes_local_model_status(monkeypatch):
    from app.services import local_model_manager as local_module
    from app.services.tool_executor import ToolExecutor

    async def fake_queue_status(**kwargs):
        return {"status": "pc_agent_offline_or_not_updated", "queue_count": 18, "kwargs": kwargs}

    monkeypatch.setattr(local_module.local_model_manager, "queue_status", fake_queue_status)

    raw = await ToolExecutor().execute("local_model_queue_status", {"include_items": False})
    result = json.loads(raw)

    assert result["status"] == "pc_agent_offline_or_not_updated"
    assert result["kwargs"] == {"agent_id": "", "include_items": False}


@pytest.mark.asyncio
async def test_tool_executor_routes_generate_music(monkeypatch):
    from app.services import media_generation_service as media_module
    from app.services.tool_executor import ToolExecutor

    calls = []

    async def fake_generate_music(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return {"job_id": "media-music", "status": "queued"}

    monkeypatch.setattr(media_module.media_generation_service, "generate_music", fake_generate_music)

    raw = await ToolExecutor().execute("generate_music", {"prompt": "short jingle"})
    result = json.loads(raw)

    assert result == {"job_id": "media-music", "status": "queued"}
    assert calls[0][0] == "short jingle"
    assert calls[0][1]["provider"] == "pc_local"
