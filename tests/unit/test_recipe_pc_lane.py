"""metadata.lane=pc 레시피의 PC 레인 강제와 조각 레시피 시작조건 검사."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.browser_bridge import service as bridge_service
from app.services import tool_executor as tool_executor_module
from app.services.work_recipe import orchestrator
from app.services.work_recipe.schema import WorkRecipe

TENANT = "2d701a8c-9596-4757-8588-faa4f7837112"


def _recipe(steps, **metadata) -> WorkRecipe:
    return WorkRecipe.from_dict({
        "name": "사방넷 테스트", "domain": "sabangnet.co.kr", "steps": steps, "metadata": metadata,
    })


LOGIN = [{"action": "navigate", "url": "https://www.sabangnet.co.kr/login"},
         {"action": "snapshot", "selector": "body"}]
FRAGMENT = [{"action": "click", "selector": "#enter"}, {"action": "snapshot", "selector": "body"}]


class Page:
    def __init__(self, url):
        self.url = url

    async def evaluate(self, expression, *args, **kwargs):
        assert expression == "window.location.href"
        if isinstance(self.url, Exception):
            raise self.url
        return self.url


class Env:
    def __init__(self):
        self.kind = "local_agent"
        self.session_work_key = ""
        self.url = "https://www.sabangnet.co.kr/main"
        self.session_missing = False
        self.played = []
        self.executor_kwargs = []
        self.scoped = 0


@pytest.fixture
def env(monkeypatch):
    e = Env()

    class Sessions:
        def get(self, session_id):
            if e.session_missing:
                return None
            return SimpleNamespace(
                work_key=e.session_work_key,
                endpoint=SimpleNamespace(kind=SimpleNamespace(value=e.kind)),
            )

    class Service:
        sessions = Sessions()

        async def acquire_playwright_context(self, session_id=None):
            return SimpleNamespace(pages=[Page(e.url)]), None

    class Router:
        def validate_action_intent(self, value, **kwargs):
            return value

        def assert_no_untrusted_page_data(self, *args, **kwargs):
            return None

    async def scoped(recipe, inputs):
        e.scoped += 1
        return {}

    async def play(recipe, executor, values, **kwargs):
        e.played.append(kwargs["context"])
        return SimpleNamespace(to_dict=lambda: {"status": "success"})

    monkeypatch.setattr(bridge_service, "get_browser_bridge_service", lambda: Service())
    monkeypatch.setattr(orchestrator, "ChannelRouter", Router)
    monkeypatch.setattr(orchestrator, "_scoped_inputs", scoped)
    monkeypatch.setattr(orchestrator, "GuardedRunRecorder", lambda **kw: object())
    monkeypatch.setattr(
        orchestrator, "BrowserRecipeExecutor",
        lambda **kw: e.executor_kwargs.append(kw) or SimpleNamespace(**kw),
    )
    monkeypatch.setattr(orchestrator, "play_recipe", play)
    return e


def _run(monkeypatch, recipe, **kwargs):
    async def resolve(*args):
        return recipe

    monkeypatch.setattr(orchestrator, "resolve_recipe", resolve)
    intent = SimpleNamespace(tenant_id=TENANT, payload={"directive": recipe.name, "inputs": {}})
    return asyncio.run(orchestrator.run_directive(
        recipe.name, TENANT, action_intent=intent, triggered_by="chat:s1", **kwargs))


@pytest.mark.parametrize("lane", ["pc", "pc_agent"])
def test_pc_lane_without_session_fails_before_vault_or_steps(env, monkeypatch, lane):
    with pytest.raises(ValueError, match="pc_lane_session_required"):
        _run(monkeypatch, _recipe(LOGIN, lane=lane))
    assert env.played == [] and env.scoped == 0 and env.executor_kwargs == []


def test_pc_lane_with_unknown_session_fails(env, monkeypatch):
    env.session_missing = True
    with pytest.raises(ValueError, match="pc_lane_session_required"):
        _run(monkeypatch, _recipe(LOGIN, lane="pc"), browser_session_id="gone")
    assert env.played == []


@pytest.mark.parametrize("kind", ["headless", "cdp", "storage_state", "websocket"])
def test_pc_lane_rejects_non_local_agent_session(env, monkeypatch, kind):
    env.kind = kind
    with pytest.raises(ValueError, match="pc_lane_session_required"):
        _run(monkeypatch, _recipe(LOGIN, lane="pc"), browser_session_id="srv")
    assert env.played == [] and env.scoped == 0


def test_pc_lane_with_local_agent_session_runs_that_session(env, monkeypatch):
    result = _run(monkeypatch, _recipe(LOGIN, lane="pc"), browser_session_id="pc-1")
    assert result.to_dict()["status"] == "success"
    assert env.executor_kwargs[0]["browser_session_id"] == "pc-1"
    assert env.played[0]["smart_browser"]["server_access_blocked"] is True


def test_work_key_is_compared_only_when_recipe_declares_one(env, monkeypatch):
    # work_key 없음 → 세션/호출자의 work_key 와 무관하게 통과
    env.session_work_key = "other"
    _run(monkeypatch, _recipe(LOGIN, lane="pc"), browser_session_id="pc-1", browser_work_key="x")
    recipe = _recipe(LOGIN, lane="pc", work_key="sabangnet:main")
    with pytest.raises(ValueError, match="pc_lane_session_required"):
        _run(monkeypatch, recipe, browser_session_id="pc-1", browser_work_key="sabangnet:main")
    env.session_work_key = "sabangnet:main"
    with pytest.raises(ValueError, match="pc_lane_session_required"):
        _run(monkeypatch, recipe, browser_session_id="pc-1", browser_work_key="wrong")
    with pytest.raises(ValueError, match="pc_lane_session_required"):
        _run(monkeypatch, recipe, browser_session_id="pc-1")
    assert _run(monkeypatch, recipe, browser_session_id="pc-1",
                browser_work_key="sabangnet:main").to_dict()["status"] == "success"


@pytest.mark.parametrize("url", ["about:blank", "", "https://evil.example.com/sabangnet.co.kr",
                                 "https://notsabangnet.co.kr/", RuntimeError("cdp down")])
def test_fragment_recipe_fails_when_start_page_is_not_on_domain(env, monkeypatch, url):
    env.url = url
    played_before = len(env.played)
    with pytest.raises(ValueError, match=r"^recipe_precondition_not_met:sabangnet\.co\.kr$"):
        _run(monkeypatch, _recipe(FRAGMENT, lane="pc"), browser_session_id="pc-1")
    assert len(env.played) == played_before and env.scoped == 0


@pytest.mark.parametrize("url", ["https://www.sabangnet.co.kr/", "https://sabangnet.co.kr/a",
                                 "https://admin.sabangnet.co.kr:8443/x"])
def test_fragment_recipe_runs_when_session_is_on_domain(env, monkeypatch, url):
    env.url = url
    assert _run(monkeypatch, _recipe(FRAGMENT, lane="pc"),
                browser_session_id="pc-1").to_dict()["status"] == "success"
    assert len(env.played) == 1


def test_navigate_first_recipe_skips_precondition(env, monkeypatch):
    env.url = "about:blank"
    assert _run(monkeypatch, _recipe(LOGIN, lane="pc"),
                browser_session_id="pc-1").to_dict()["status"] == "success"


@pytest.mark.parametrize("meta", [{}, {"lane": "server"}, {"lane": ""}])
def test_non_pc_lane_recipes_behave_as_before(env, monkeypatch, meta):
    env.url = "about:blank"
    env.kind = "headless"
    result = _run(monkeypatch, _recipe(FRAGMENT, **meta))
    assert result.to_dict()["status"] == "success"
    assert env.executor_kwargs == [{"browser_session_id": None, "browser_work_key": None}]
    assert env.played[0]["smart_browser"] == {
        "native_auth_required": False,
        "requires_local_security_programs": False,
        "server_access_blocked": False,
        "requires_residential_ip": False,
    }
    # 세션을 넘겨도 서버 레인 레시피는 세션 종류를 검사하지 않는다.
    assert _run(monkeypatch, _recipe(FRAGMENT, **meta),
                browser_session_id="srv").to_dict()["status"] == "success"


def test_server_lane_recipe_keeps_explicit_server_access_blocked_flag(env, monkeypatch):
    _run(monkeypatch, _recipe(LOGIN, server_access_blocked=True))
    assert env.played[0]["smart_browser"]["server_access_blocked"] is True


def test_run_directive_wires_pc_lane_check_after_shinhan_and_coupang_paths():
    import inspect

    source = inspect.getsource(orchestrator.run_directive)
    assert source.index("shinhan_manual.run_financial_recipe") < source.index("_assert_pc_lane_ready")
    assert "pc_lane = not coupang and" in source
    assert source.index("_assert_pc_lane_ready") < source.index("_scoped_inputs(recipe")


async def test_smart_browser_run_surfaces_precondition_errors_to_user(monkeypatch):
    """ValueError 가 smart_browser 에서 잡히지 않고 execute() 의 일반 예외 처리까지 올라가는지."""
    session = "11111111-1111-1111-1111-111111111111"

    async def tenant(**kwargs):
        return TENANT

    async def run(*args, **kwargs):
        raise ValueError("pc_lane_session_required")

    monkeypatch.setattr(tool_executor_module, "resolve_bound_tenant_id", tenant)
    monkeypatch.setattr(orchestrator, "run_directive", run)
    token = tool_executor_module.current_chat_session_id.set(session)
    try:
        with pytest.raises(ValueError, match="pc_lane_session_required"):
            await tool_executor_module.ToolExecutor()._smart_browser(
                {"action": "run", "directive": "사방넷 로그인"})
    finally:
        tool_executor_module.current_chat_session_id.reset(token)


async def test_execute_converts_value_error_to_user_visible_error(monkeypatch):
    executor = tool_executor_module.ToolExecutor()

    async def boom(inp):
        raise ValueError("recipe_precondition_not_met:sabangnet.co.kr")

    monkeypatch.setattr(executor, "_smart_browser", boom)
    out = await executor.execute("smart_browser", {"action": "run", "directive": "x"})
    assert json.loads(out)["error"] == "recipe_precondition_not_met:sabangnet.co.kr"
