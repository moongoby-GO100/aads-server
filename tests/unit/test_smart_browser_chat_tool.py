from __future__ import annotations

from app.services import tool_executor as tool_executor_module
from app.services import tool_registry as tool_registry_module
from app.services.tool_executor import ToolExecutor

TENANT_ID = "2d701a8c-9596-4757-8588-faa4f7837112"
SESSION_ID = "11111111-1111-1111-1111-111111111111"


def test_smart_browser_is_an_eager_chat_tool():
    assert "smart_browser" in tool_registry_module._TOOLS
    assert any(
        tool["name"] == "smart_browser"
        for tool in tool_registry_module.ToolRegistry().get_eager_tools()
    )


async def test_register_e2e_requires_screen_artifact(monkeypatch):
    async def tenant(**kwargs):
        return TENANT_ID

    monkeypatch.setattr(tool_executor_module, "resolve_bound_tenant_id", tenant)
    token = tool_executor_module.current_chat_session_id.set(SESSION_ID)
    try:
        result = await ToolExecutor()._smart_browser(
            {
                "action": "register_e2e",
                "recipe_name": "orders",
                "domain": "example.com",
                "steps": [{"action": "navigate", "url": "https://example.com"}],
                "e2e_evidence": {"screen_verified": True},
            }
        )
    finally:
        tool_executor_module.current_chat_session_id.reset(token)
    assert result["error"] == "screenshot_or_snapshot_reference_required"


async def test_register_e2e_binds_recipe_to_chat_session(monkeypatch):
    from app.services.work_recipe import recorder as recorder_module

    captured = {}

    class Recording:
        def record_step(self, payload, *, succeeded=True):
            captured.setdefault("steps", []).append((payload, succeeded))

    async def tenant(**kwargs):
        return TENANT_ID

    def start(name, domain, tenant_id, **kwargs):
        captured.update(name=name, domain=domain, tenant_id=tenant_id, **kwargs)
        return Recording()

    async def finish(recording, *, created_by=""):
        captured["created_by"] = created_by
        return {"id": "registration-1", "status": "pending"}

    monkeypatch.setattr(tool_executor_module, "resolve_bound_tenant_id", tenant)
    monkeypatch.setattr(recorder_module, "start_recording", start)
    monkeypatch.setattr(recorder_module, "finish_recording", finish)
    token = tool_executor_module.current_chat_session_id.set(SESSION_ID)
    try:
        result = await ToolExecutor()._smart_browser(
            {
                "action": "register_e2e",
                "recipe_name": "orders",
                "domain": "https://example.com/orders",
                "steps": [{"action": "navigate", "url": "https://example.com/orders"}],
                "e2e_evidence": {
                    "screen_verified": True,
                    "screenshot_url": "https://aads.newtalk.kr/screenshots/e2e.png",
                    "raw_dom": "must-not-be-persisted",
                },
            }
        )
    finally:
        tool_executor_module.current_chat_session_id.reset(token)

    assert result["status"] == "approval_required"
    assert captured["session_id"] == SESSION_ID
    assert captured["created_by"] == f"chat:{SESSION_ID}"
    assert "raw_dom" not in captured["e2e_evidence"]


async def test_run_uses_authenticated_session_intent(monkeypatch):
    from app.services.work_recipe import orchestrator

    captured = {}

    class Result:
        def to_dict(self):
            return {"status": "success"}

    async def tenant(**kwargs):
        return TENANT_ID

    async def run(directive, tenant_id, **kwargs):
        captured.update(directive=directive, tenant_id=tenant_id, **kwargs)
        return Result()

    monkeypatch.setattr(tool_executor_module, "resolve_bound_tenant_id", tenant)
    monkeypatch.setattr(orchestrator, "run_directive", run)
    token = tool_executor_module.current_chat_session_id.set(SESSION_ID)
    try:
        result = await ToolExecutor()._smart_browser(
            {"action": "run", "directive": "example.com 주문 조회"}
        )
    finally:
        tool_executor_module.current_chat_session_id.reset(token)

    assert result["status"] == "executed"
    assert captured["action_intent"].session_id == SESSION_ID
    assert captured["action_intent"].tenant_id == TENANT_ID
    assert captured["triggered_by"] == f"chat:{SESSION_ID}"
