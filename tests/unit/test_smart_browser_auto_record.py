from __future__ import annotations

import logging
import uuid

import pytest

from app.services import tool_executor as tool_executor_module
from app.services.tool_executor import ToolExecutor
from app.services.work_recipe import auto_record, recorder as recorder_module

TENANT = "2d701a8c-9596-4757-8588-faa4f7837112"
SECRET = "pw-fixture-value"
COOKIE = "sessionid=fixture-cookie"

NAV_OK = "[탐색 완료]\n제목: 주문 목록\nURL: https://shop.example.com/orders"
SNAP_OK = "[ARIA 스냅샷 — https://shop.example.com/orders]\n- heading 주문 목록\n- table 주문 3건"
SHOT_OK = "[스크린샷 PNG — base64]\nURL: https://shop.example.com/orders\nDATA:" + "A" * 400


class FakeConn:
    def __init__(self, db):
        self.db = db

    async def execute(self, sql, *args):
        self.db["sql_args"].append(args)
        if "INSERT INTO smart_browser_auto_traces" in sql:
            key = (str(args[0]), args[1], args[2], args[3])
            self.db["traces"][key] = self.db["traces"].get(key, 0) + 1
        elif "UPDATE smart_browser_auto_drafts" in sql:
            row = self.db["drafts"][(str(args[0]), args[1], args[2])]
            row.update(status=args[3], registration_id=args[4])
        elif "DELETE FROM smart_browser_auto_drafts" in sql:
            self.db["drafts"].pop((str(args[0]), args[1], args[2]), None)

    async def fetchval(self, sql, *args):
        return len({k[3] for k in self.db["traces"] if k[:3] == (str(args[0]), args[1], args[2])})

    async def fetchrow(self, sql, *args):
        key = (str(args[0]), args[1], args[2])
        if key in self.db["drafts"]:
            return None
        self.db["drafts"][key] = {"recipe_name": args[3], "status": "claimed"}
        return {"signature": args[2]}


class FakePool:
    def __init__(self, db):
        self.db = db

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self_inner):
                return FakeConn(pool.db)

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


@pytest.fixture
def env(monkeypatch):
    monkeypatch.delenv(auto_record.FLAG_ENV, raising=False)
    monkeypatch.delenv(auto_record.MIN_STEPS_ENV, raising=False)
    monkeypatch.delenv(auto_record.MIN_SESSIONS_ENV, raising=False)
    auto_record.get_book().clear()
    db = {"traces": {}, "drafts": {}, "sql_args": [], "registered": [], "recipes": []}
    monkeypatch.setattr(auto_record, "get_pool", lambda: FakePool(db))

    async def list_recipes(**kwargs):
        return db["recipes"]

    async def request_registration(recipe, **kwargs):
        db["registered"].append(recipe)
        return {"id": str(uuid.uuid4()), "status": "pending", "spec": recipe.to_dict()}

    monkeypatch.setattr(auto_record.store, "list_recipes", list_recipes)
    monkeypatch.setattr(recorder_module.registration, "request_registration", request_registration)
    yield db
    auto_record.get_book().clear()


async def run_flow(session, *, steps=None, evidence=SNAP_OK, nav=NAV_OK, fill_value=SECRET):
    """navigate -> fill -> click -> evidence 한 번. 마지막 observe 결과를 돌려준다."""
    key = {"browser_work_key": "wk"}
    plan = steps or [
        ("browser_navigate", {"url": f"https://shop.example.com/orders?token={COOKIE}", **key}, nav),
        ("browser_fill", {"selector": "#search", "value": fill_value, **key}, "[입력 완료] selector=#search"),
        ("browser_click", {"selector": "button.go", **key}, "[클릭 완료] selector=button.go"),
        (
            "browser_snapshot" if evidence.startswith("[ARIA") else "browser_screenshot",
            dict(key),
            evidence,
        ),
    ]
    last = None
    for tool, inp, result in plan:
        last = await auto_record.observe(
            tool, inp, result, session_id=session, tenant_id=TENANT
        )
    return last


async def test_two_distinct_sessions_create_one_draft(env):
    first = await run_flow("session-a")
    second = await run_flow("session-b")
    assert first == {"status": "recorded", "sessions": 1}
    assert second["status"] == "drafted" and second["sessions"] == 2
    assert len(env["registered"]) == 1
    recipe = env["registered"][0]
    assert recipe.domain == "shop.example.com"
    assert [step.action for step in recipe.steps] == ["navigate", "fill", "click", "snapshot"]
    assert recipe.metadata["screen_e2e"]["auto_recorded"] is True


async def test_same_session_twice_creates_nothing(env):
    await run_flow("session-a")
    again = await run_flow("session-a")
    assert again == {"status": "recorded", "sessions": 1}
    assert env["registered"] == []


async def test_third_success_does_not_duplicate_draft(env):
    await run_flow("session-a")
    await run_flow("session-b")
    third = await run_flow("session-c")
    assert third["status"] == "already_drafted"
    assert len(env["registered"]) == 1


@pytest.mark.parametrize(
    "bad_snapshot",
    [
        "[ARIA 스냅샷 — https://shop.example.com/orders]\n- heading Access Denied\n- text no permission",
        "[ARIA 스냅샷 — https://shop.example.com/orders]\n- text 로그인 실패: 아이디 또는 비밀번호 확인",
        "[ARIA 스냅샷 — https://shop.example.com/orders]\n- iframe reCAPTCHA challenge required",
        "[ARIA 스냅샷 — https://shop.example.com/orders]\n- heading 404 Not Found page",
        "[ARIA 스냅샷 — https://shop.example.com/orders]\n- heading 500 Internal Server Error",
    ],
)
async def test_error_screen_is_not_a_success(env, bad_snapshot):
    for session in ("session-a", "session-b"):
        assert await run_flow(session, evidence=bad_snapshot) is None
    assert env["traces"] == {} and env["registered"] == []


async def test_navigate_ok_without_screen_evidence_is_not_a_success(env):
    for session in ("session-a", "session-b"):
        await auto_record.observe(
            "browser_navigate", {"url": "https://shop.example.com/orders", "browser_work_key": "wk"},
            NAV_OK, session_id=session, tenant_id=TENANT,
        )
    assert env["traces"] == {} and env["registered"] == []


async def test_login_redirect_and_error_title_are_rejected(env):
    redirected = "[스크린샷 PNG — base64]\nURL: https://shop.example.com/login\nDATA:" + "A" * 400
    assert await run_flow("session-a", evidence=redirected) is None
    denied_nav = "[탐색 완료]\n제목: 403 Forbidden\nURL: https://shop.example.com/orders"
    assert await run_flow("session-b", nav=denied_nav) is None
    assert env["traces"] == {}


async def test_screenshot_evidence_counts(env):
    await run_flow("session-a", evidence=SHOT_OK)
    result = await run_flow("session-b", evidence=SHOT_OK)
    assert result["status"] == "drafted"


async def test_failed_step_is_not_recorded_but_success_path_is(env):
    key = {"browser_work_key": "wk"}
    plan = [
        ("browser_navigate", {"url": "https://shop.example.com/orders", **key}, NAV_OK),
        ("browser_click", {"selector": "#missing", **key}, "[ERROR] 클릭 실패 (#missing): timeout"),
        ("browser_click", {"selector": "button.go", **key}, "[클릭 완료] selector=button.go"),
        ("browser_snapshot", dict(key), SNAP_OK),
    ]
    result = await run_flow("session-a", steps=plan)
    assert result["status"] == "recorded"
    assert "#missing" not in repr(env["sql_args"])
    assert '"selector": "button.go"' in repr(env["sql_args"]).replace("\\", "")


async def test_fill_secret_never_persisted_anywhere(env, caplog):
    caplog.set_level(logging.DEBUG)
    key = {"browser_work_key": "wk"}
    for session in ("session-a", "session-b"):
        plan = [
            ("browser_navigate", {"url": f"https://shop.example.com/login-page?t={COOKIE}#frag", **key}, NAV_OK),
            ("browser_fill", {"selector": "input[type=password]", "value": SECRET, **key}, "[입력 완료] selector=input[type=password]"),
            ("browser_fill", {"selector": "#memo", "value": SECRET + "-memo", **key}, "[입력 완료] selector=#memo"),
            ("browser_click", {"selector": "button.go", **key}, "[클릭 완료] selector=button.go"),
            ("browser_snapshot", dict(key), SNAP_OK),
        ]
        await run_flow(session, steps=plan)
        # 진행 중 상태에도 값이 없어야 한다.
        assert SECRET not in repr(auto_record.get_book()._traces)
    assert len(env["registered"]) == 1
    recipe = env["registered"][0]
    dump = recipe.to_yaml() + repr(env["sql_args"]) + repr(env["drafts"]) + caplog.text
    for forbidden in (SECRET, COOKIE, "fixture-cookie", "frag"):
        assert forbidden not in dump
    fills = [step for step in recipe.steps if step.action == "fill"]
    assert fills[0].value == "{{credential_1}}"
    assert fills[1].value == "{{fill_2}}"
    secret_inputs = {item.name: item.secret for item in recipe.inputs}
    assert secret_inputs["credential_1"] is True and secret_inputs["fill_2"] is False
    assert recipe.steps[0].url == "https://shop.example.com/login-page"


async def test_single_char_key_press_discards_sequence(env):
    key = {"browser_work_key": "wk"}
    plan = [
        ("browser_navigate", {"url": "https://shop.example.com/orders", **key}, NAV_OK),
        ("browser_press_key", {"key": "p", **key}, "[키 입력 완료] key=p"),
        ("browser_snapshot", dict(key), SNAP_OK),
    ]
    assert await run_flow("session-a", steps=plan) is None
    assert env["traces"] == {}


async def test_equivalent_existing_recipe_skips_draft(env):
    await run_flow("session-a")
    env["recipes"].append(
        {
            "spec": {
                "steps": [
                    {"action": "navigate", "url": "https://shop.example.com/orders"},
                    {"action": "fill", "selector": "#search", "value": "{{q}}"},
                    {"action": "click", "selector": "button.go"},
                    {"action": "snapshot", "selector": "body"},
                ]
            }
        }
    )
    result = await run_flow("session-b")
    assert result["status"] == "skipped_existing"
    assert env["registered"] == []


async def test_registration_failure_releases_claim(env, monkeypatch):
    async def boom(recipe, **kwargs):
        raise RuntimeError("db down")

    await run_flow("session-a")
    monkeypatch.setattr(recorder_module.registration, "request_registration", boom)
    assert await run_flow("session-b") is None  # observe 는 예외를 삼킨다
    assert env["drafts"] == {}


async def test_flag_off_wrapper_is_noop(env, monkeypatch):
    monkeypatch.setenv(auto_record.FLAG_ENV, "0")
    executor = ToolExecutor()

    async def handler(inp):
        return "[클릭 완료]"

    assert executor._auto_record_wrap("browser_click", handler) is handler
    assert await run_flow("session-a") is None
    assert len(auto_record.get_book()) == 0 and env["traces"] == {}


async def test_wrapper_records_and_never_changes_result(env, monkeypatch):
    executor = ToolExecutor()

    async def handler(inp):
        return NAV_OK

    async def tenant(*args, **kwargs):
        return TENANT

    monkeypatch.setattr(tool_executor_module, "resolve_bound_tenant_id", tenant)
    token = tool_executor_module.current_chat_session_id.set("session-w")
    try:
        wrapped = executor._auto_record_wrap("browser_navigate", handler)
        assert wrapped is not handler
        assert await wrapped({"url": "https://shop.example.com/orders", "browser_work_key": "wk"}) == NAV_OK
    finally:
        tool_executor_module.current_chat_session_id.reset(token)
    assert len(auto_record.get_book()) == 1

    async def broken(*args, **kwargs):
        raise RuntimeError("observer down")

    monkeypatch.setattr(auto_record, "observe", broken)
    token = tool_executor_module.current_chat_session_id.set("session-w")
    try:
        assert await wrapped({"url": "https://shop.example.com/x"}) == NAV_OK
    finally:
        tool_executor_module.current_chat_session_id.reset(token)


async def test_smart_browser_list_exposes_pending_drafts(monkeypatch):
    async def tenant(**kwargs):
        return TENANT

    async def no_recipes(**kwargs):
        return []

    async def pending(*, tenant_id, status="pending"):
        return [
            {
                "id": "reg-1", "name": "auto_shop_1", "domain": "shop.example.com",
                "status": "pending", "requested_at": "2026-10-02",
                "dry_run": {"proposed_version": 1, "max_risk": "WRITE_EXTERNAL", "steps": [{}, {}]},
                "spec": {"metadata": {"screen_e2e": {"auto_recorded": True}}, "steps": [{"value": SECRET}]},
            }
        ]

    from app.services.work_recipe import store

    monkeypatch.setattr(tool_executor_module, "resolve_bound_tenant_id", tenant)
    monkeypatch.setattr(store, "list_recipes", no_recipes)
    monkeypatch.setattr(auto_record.registration, "list_registrations", pending)
    token = tool_executor_module.current_chat_session_id.set("session-l")
    try:
        result = await ToolExecutor()._smart_browser({"action": "list"})
    finally:
        tool_executor_module.current_chat_session_id.reset(token)
    assert result["recipes"] == []
    assert result["drafts"] == [
        {
            "registration_id": "reg-1", "name": "auto_shop_1", "domain": "shop.example.com",
            "proposed_version": 1, "max_risk": "WRITE_EXTERNAL", "step_count": 2,
            "auto_recorded": True, "status": "pending", "requested_at": "2026-10-02",
        }
    ]
    assert SECRET not in repr(result)


def test_tool_descriptions_point_to_smart_browser_first():
    from app.services import tool_registry

    for name in ("smart_browser", "browser_navigate"):
        text = tool_registry._TOOLS[name]["description"]
        assert "smart_browser list" in text or "list 로 승인 레시피" in text
        assert "자동 기록" in text
