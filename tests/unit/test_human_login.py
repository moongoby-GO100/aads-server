import asyncio
import json
import shutil
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.work_recipe import human_login, orchestrator
from app.services.work_recipe.human_login import (
    PROBE,
    HumanLoginExecutor,
    LoginPolicyStore,
    validate_login_steps,
)
from app.services.work_recipe.login_contract import normalize_login_contract
from app.services.work_recipe.player import RecipePlayer
from app.services.work_recipe.schema import parse_recipe


@pytest.mark.parametrize(
    "human,success,limited,expected",
    [
        (True, True, False, None),
        (True, False, False, "human_login_timeout"),
        (False, False, False, "bot_protection_suspected"),
        (False, False, True, "daily_login_submit_limit"),
    ],
)
def test_submit_policy(human, success, limited, expected):
    asyncio.run(_submit_policy(human, success, limited, expected))


async def _submit_policy(human, success, limited, expected):
    browser = type("Browser", (), {})()
    state = {"url": "https://example.com/login", "requests": 1, "navigation": 1, "blocked": ""}
    page = AsyncMock()

    async def evaluate(script, *args):
        return state if script == PROBE else success and wait.await_count > 0

    page.evaluate.side_effect = evaluate
    browser.login_page = page
    executor = AsyncMock(return_value={"ok": True})
    store = AsyncMock()

    async def access(**kwargs):
        return limited if kwargs.get("reserve") else False

    store.access.side_effect = access
    wait = AsyncMock(return_value={"id": "task"})
    finish = AsyncMock()
    wrapped = HumanLoginExecutor(
        executor,
        browser,
        {
            "submit_step": 3,
            "human_submit_required": human,
            "human_submit_timeout_seconds": 0,
            "submit_observation_seconds": 0,
            "succeeds_when": [{"kind": "url_equals", "value": "https://example.com/home"}],
        },
        store,
        wait,
        finish,
    )
    result = await wrapped({"phase": "step", "seq": 3, "action": "click"})
    if expected:
        assert result["error"] == expected
        assert result["status"] == "blocked"
        assert result["recovery"]["auto_retry"] is False
    else:
        assert result["ok"]
        await wrapped({"phase": "step", "seq": 4, "action": "snapshot"})
        assert executor.await_count == 1  # only subsequent business fragment
    if human or limited:
        assert not any(c.args[0].get("seq") == 3 for c in executor.await_args_list)
    assert wait.await_count == 1


def test_contract_flag_validation():
    with pytest.raises(ValueError, match="human_submit_required_invalid"):
        normalize_login_contract({"version": 1, "human_submit_required": "true"})


TENANT = "00000000-0000-0000-0000-000000000001"


def contract(**kwargs):
    return {
        "version": 1,
        "submit_step": 3,
        "succeeds_when": [{"kind": "url_equals", "value": "https://example.com/home"}],
        **kwargs,
    }


def recipe_for_login(**kwargs):
    return parse_recipe(
        {
            "name": "example_login",
            "domain": "example.com",
            "version": 1,
            "inputs": [],
            "steps": [
                {"action": "navigate", "url": "https://example.com/login", "risk": "READ"},
                {
                    "action": "fill",
                    "selector": "#password",
                    "value": "approved-secret",
                    "risk": "READ",
                },
                {"action": "click", "selector": "#submit", "risk": "READ"},
                {"action": "snapshot", "selector": "#home", "risk": "READ"},
            ],
            "verify": [{"action": "snapshot", "selector": "#home", "risk": "READ"}],
            "metadata": {"login_contract": contract(**kwargs)},
        }
    )


@pytest.mark.parametrize(
    "override,error",
    [
        ({"submit_step": True}, "login_submit_step_required"),
        ({"submit_step": 0}, "login_submit_step_required"),
        ({"succeeds_when": []}, "login_success_conditions_required"),
        ({"succeeds_when": [{"kind": "script", "value": "x"}]}, "login_success_condition_invalid"),
        (
            {"succeeds_when": [{"kind": "url_equals", "value": "https://example.com/?token=x"}]},
            "login_success_url_invalid",
        ),
        ({"human_submit_timeout_seconds": -1}, "login_wait_timeout_invalid"),
        ({"submit_observation_seconds": True}, "login_wait_timeout_invalid"),
    ],
)
def test_contract_validation(override, error):
    with pytest.raises(ValueError, match=error):
        normalize_login_contract(contract(**override))


def test_orphan_wait_setting_rejected():
    with pytest.raises(ValueError, match="login_submit_step_required"):
        normalize_login_contract({"version": 1, "human_submit_timeout_seconds": 10})


class PolicyDB:
    """Small SQL boundary fake: time and unique-key behavior are explicit."""

    def __init__(self):
        self.rows = {}
        self.now = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
        self.sql = []

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        yield

    async def execute(self, sql, *args):
        self.sql.append(sql)
        if sql.startswith("DELETE"):
            self.rows = {k: v for k, v in self.rows.items() if k[:2] != args[:2]}
        elif "INSERT" in sql:
            tenant, key = args[:2]
            row = self.rows.setdefault((tenant, key, self.now.date()), {"attempts": 0})
            if key.startswith("site:"):
                row.update(reason=args[2], until=self.now + timedelta(hours=24))
            else:
                row["attempts"] += 1

    async def fetchval(self, sql, tenant, key):
        self.sql.append(sql)
        if "EXISTS" in sql:
            assert "blocked_until > NOW()" in sql
            return any(
                k[:2] == (tenant, key) and v.get("until", self.now) > self.now
                for k, v in self.rows.items()
            )
        assert "day=CURRENT_DATE" in sql
        return self.rows.get((tenant, key, self.now.date()), {}).get("attempts")


def test_policy_daily_limit_expiry_release_and_tenant_scope(monkeypatch):
    db = PolicyDB()
    monkeypatch.setattr(human_login, "get_pool", lambda: db)
    monkeypatch.setenv("AADS_LOGIN_DAILY_SUBMIT_LIMIT", "2")

    async def run():
        store = LoginPolicyStore(TENANT, "example.com", ["credential-id"])
        assert not await store.access(reserve=True)
        assert not await store.access(reserve=True)
        assert await store.access(reserve=True)
        db.now += timedelta(days=1)
        assert not await store.access(reserve=True)
        await store.access(reason="bot_protection_suspected")
        # New executor/store instance retains the site decision.
        other = LoginPolicyStore(TENANT, "example.com", ["credential-id"])
        assert await other.access()
        assert not await LoginPolicyStore(TENANT, "other.com", ["credential-id"]).access()
        assert not await LoginPolicyStore(
            "00000000-0000-0000-0000-000000000002", "example.com", ["credential-id"]
        ).access()
        db.now += timedelta(hours=24)
        assert not await other.access()
        await store.access(reason="access_denied")
        await other.clear_site()
        assert not await store.access()
        # The new day allows two reservations; clear_site does not reset them.
        assert not await store.access(reserve=True)
        await other.clear_site()
        assert not await store.access(reserve=True)
        assert await store.access(reserve=True)

    asyncio.run(run())


def test_multiple_credentials_reserve_none_if_any_limited(monkeypatch):
    db = PolicyDB()
    monkeypatch.setattr(human_login, "get_pool", lambda: db)
    monkeypatch.setenv("AADS_LOGIN_DAILY_SUBMIT_LIMIT", "1")

    async def run():
        assert not await LoginPolicyStore(TENANT, "one.com", ["b"]).access(reserve=True)
        assert await LoginPolicyStore(TENANT, "two.com", ["a", "b"]).access(reserve=True)
        assert not any(k[1] == "credential:a" for k in db.rows)

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["exception", "timeout", "failure"])
def test_player_submit_single_attempt_and_timeout_without_context(monkeypatch, mode):
    recipe = recipe_for_login(human_submit_timeout_seconds=720, submit_observation_seconds=800)
    calls, timeouts = [], []

    async def execute(payload):
        calls.append(payload)
        if payload["seq"] == 3:
            if mode == "timeout":
                raise asyncio.TimeoutError()
            if mode == "exception":
                raise RuntimeError("ambiguous submit")
            return {"ok": False, "error": "ambiguous submit"}
        return {"ok": True}

    player = RecipePlayer(execute, retries=5)
    original = player._invoke

    async def invoke(payload, timeout):
        if payload["seq"] == 3:
            timeouts.append(timeout)
        return await original(payload, timeout)

    monkeypatch.setattr(player, "_invoke", invoke)
    result = asyncio.run(player.play(recipe, context={"login_contract": {"submit_step": 999}}))
    assert result.failed_step_seq == 3
    assert len([c for c in calls if c["seq"] == 3]) == 1
    assert timeouts == [830]


@pytest.mark.parametrize("bad_step", ["click", "press", "snapshot"])
def test_orchestrator_rejects_unsafe_prefix_before_vault(monkeypatch, bad_step):
    recipe = recipe_for_login()
    # Schema validated actions are replaced only to test orchestration boundaries.
    from dataclasses import replace

    recipe = replace(
        recipe,
        steps=[recipe.steps[0], replace(recipe.steps[1], action=bad_step), *recipe.steps[2:]],
    )
    resolve = AsyncMock(return_value=recipe)
    vault = AsyncMock()
    monkeypatch.setattr(orchestrator, "resolve_recipe", resolve)
    monkeypatch.setattr(orchestrator.vault_reference, "resolve_inputs", vault)
    router = SimpleNamespace(
        validate_action_intent=lambda x, **kw: x,
        assert_no_untrusted_page_data=lambda *a, **kw: None,
    )
    monkeypatch.setattr(orchestrator, "ChannelRouter", lambda: router)
    intent = SimpleNamespace(tenant_id=TENANT, payload={"directive": "login"})
    with pytest.raises(ValueError, match="login_prefix_must_not_submit"):
        asyncio.run(orchestrator.run_directive("login", TENANT, action_intent=intent))
    vault.assert_not_awaited()


def test_submit_step_must_exist_and_submit():
    recipe = recipe_for_login()
    for seq in (1, 99):
        with pytest.raises(ValueError, match="login_submit_step_invalid"):
            validate_login_steps(recipe, contract(submit_step=seq))


@pytest.mark.parametrize(
    "stage", ["policy_store", "wait_task", "finish_task", "browser_observation"]
)
def test_dependency_failures_keep_safe_diagnostics(monkeypatch, caplog, stage):
    secret = "secret-value-cookie-token"
    page = AsyncMock()
    state = {"url": 1, "requests": 1, "navigation": 1, "blocked": ""}
    waiting = False

    async def evaluate(script, *args):
        if stage == "browser_observation":
            raise ConnectionError(secret)
        return state if script == PROBE else waiting

    async def wait(*args):
        nonlocal waiting
        if stage == "wait_task":
            raise ConnectionError(secret)
        waiting = True
        return {"id": "task"}

    store = AsyncMock()
    store.access.return_value = False
    if stage == "policy_store":
        store.access.side_effect = ConnectionError(secret)
    finish = AsyncMock()
    if stage == "finish_task":
        finish.side_effect = ConnectionError(secret)
    page.evaluate.side_effect = evaluate
    record = AsyncMock()
    monkeypatch.setattr(human_login, "record_candidate", record)
    wrapper = HumanLoginExecutor(
        AsyncMock(),
        SimpleNamespace(login_page=page),
        contract(human_submit_required=stage != "policy_store"),
        store,
        wait,
        finish,
    )
    result = asyncio.run(wrapper({"phase": "step", "seq": 3}))
    assert result["recovery"]["failure_stage"] == stage
    assert result["recovery"]["exception_type"] == "ConnectionError"
    assert result["recovery"]["auto_retry"] is False
    record.assert_awaited_once()
    assert secret not in caplog.text + repr(result) + repr(record.call_args)


def test_probe_ignores_notices_but_detects_visible_challenges():
    if not shutil.which("node"):
        pytest.skip("node required for browser probe JS fixtures")
    # Execute the actual JavaScript with DOM-shaped fixtures.
    script = """
const probe = PROBE_SCRIPT;
const run = (labels, frames=[], title='', body='') => {
 global.document = {title, body:{innerText:body}, querySelectorAll: selector =>
   (selector === 'iframe' ? frames : labels).map(x => ({innerText:x.text || '',
    src:x.src || '', title:x.title || '', getClientRects:()=>x.hidden?[]:[1]}))};
 global.location = {href:'https://example.com/login'};
 global.performance = {timeOrigin:1,getEntriesByType:()=>[]};
 global.getComputedStyle = ()=>({visibility:'visible'});
 return probe().blocked;
};
console.log(JSON.stringify([
 run([], [], '', 'Protected by reCAPTCHA 보안 확인 Access Denied help'),
 run([{text:'보안 확인'}]), run([{text:'Access Denied',hidden:true}]),
 run([{text:'Access Denied'}]), run([{text:'Verify you are human'}]),
 run([], [{src:'https://google.com/recaptcha/api2/anchor?k=x',title:'reCAPTCHA'}]),
 run([], [{src:'https://google.com/recaptcha/api2/bframe?k=x',title:'challenge'}])
]));
""".replace("PROBE_SCRIPT", PROBE)
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == [
        "",
        "",
        "",
        "access_denied",
        "captcha_required",
        "",
        "captcha_required",
    ]


def test_orchestrator_wires_human_checkpoint_and_continues(monkeypatch):
    recipe = recipe_for_login(
        human_submit_required=True, business_ref="business", account_ref="account"
    )
    state = {"url": 1, "requests": 1, "navigation": 1, "blocked": ""}
    calls, waits, finishes = [], [], []
    authenticated = False
    page = AsyncMock()

    async def evaluate(script, *args):
        return state if script == PROBE else authenticated

    page.evaluate.side_effect = evaluate

    class Browser:
        login_page = None
        login_pc_label = "DESKTOP-TEST"

        def login_lane(self, payload):
            assert payload["context"]["login_contract"]["submit_step"] == 3
            return "pc"

        async def __call__(self, payload):
            self.login_page = page
            calls.append(payload)
            return {"ok": True, "output": {"filled": True}}

    async def wait(**kwargs):
        nonlocal authenticated
        waits.append(kwargs)
        authenticated = True
        return {"id": "task"}

    async def finish(**kwargs):
        finishes.append(kwargs)
        return {"id": "task"}

    from app.services import auth_challenge_orchestrator, browser_task_gateway

    monkeypatch.setattr(auth_challenge_orchestrator, "persist_auth_wait", wait)
    monkeypatch.setattr(
        auth_challenge_orchestrator, "auth_wait_user", AsyncMock(return_value="user")
    )
    monkeypatch.setattr(browser_task_gateway, "update_browser_task_status", finish)
    monkeypatch.setattr(orchestrator, "resolve_recipe", AsyncMock(return_value=recipe))
    monkeypatch.setattr(
        orchestrator,
        "ChannelRouter",
        lambda: SimpleNamespace(
            validate_action_intent=lambda x, **kw: x,
            assert_no_untrusted_page_data=lambda *a, **kw: None,
        ),
    )
    monkeypatch.setattr(orchestrator.vault_reference, "has_vault_scope", lambda r: True)
    monkeypatch.setattr(
        orchestrator.vault_reference,
        "resolve_inputs",
        AsyncMock(
            return_value=SimpleNamespace(
                values={}, uses=[{"credential_id": "credential-id"}], fill_steps=[2]
            )
        ),
    )
    monkeypatch.setattr(orchestrator, "_VaultUseAuditRecorder", lambda recorder, *args: recorder)
    monkeypatch.setattr(orchestrator, "GuardedRunRecorder", lambda **kw: object())
    monkeypatch.setattr(orchestrator, "BrowserRecipeExecutor", lambda **kw: Browser())
    monkeypatch.setattr(
        orchestrator, "GenericVerificationExecutor", lambda executor, recipe: executor
    )
    store = AsyncMock()
    store.access.return_value = False
    monkeypatch.setattr(orchestrator, "LoginPolicyStore", lambda *args: store)
    intent = SimpleNamespace(
        tenant_id=TENANT,
        payload={"directive": "login"},
        session_id="session",
        authenticated_provenance={"user_id": "user"},
    )
    result = asyncio.run(orchestrator.run_directive("login", TENANT, action_intent=intent))
    assert result.ok
    assert [(c["phase"], c["seq"]) for c in calls] == [
        ("step", 1),
        ("step", 2),
        ("step", 4),
        ("verify", 5),
    ]
    assert len(waits) == 1
    assert (
        waits[0]["current_step"]
        == "example.com 사이트 로그인 버튼만 눌러 주세요 (PC: DESKTOP-TEST)"
    )
    assert waits[0]["scope"]["checkpoint"] == 3
    assert finishes[0]["status"] == "completed"
    assert "approved-secret" not in repr(waits) + repr(finishes) + repr(result.to_dict())


def test_auth_wait_uses_existing_notification_once(monkeypatch):
    from app.services import auth_challenge_orchestrator as auth
    from app.services import browser_task_gateway as gateway

    task = {"id": "task", "status": "waiting_for_user"}
    monkeypatch.setattr(auth, "validate_auth_scope", lambda scope: scope)
    monkeypatch.setattr(gateway, "create_browser_task", AsyncMock(return_value=task))
    update = AsyncMock(return_value=task)
    notify = AsyncMock()
    monkeypatch.setattr(gateway, "update_browser_task_status", update)
    monkeypatch.setattr(gateway, "notify_browser_task_status", notify)
    asyncio.run(
        auth.persist_auth_wait(
            scope={
                "tenant_id": TENANT,
                "work_key": "work",
                "session_id": "session",
                "target": "https://example.com/login",
                "checkpoint": 3,
            },
            user_id="user",
            reason="human_submit_required",
            verify=[{"action": "snapshot"}],
            current_step="example.com 사이트 로그인 버튼만 눌러 주세요 (PC: DESKTOP-TEST)",
        )
    )
    assert update.call_args.kwargs["status"] == "waiting_for_user"
    notify.assert_awaited_once_with(task)


def test_human_polling_is_read_only_until_success(monkeypatch):
    clock = [0]
    observations = []
    state = {"url": 1, "requests": 1, "navigation": 1, "blocked": ""}
    page = AsyncMock()

    async def evaluate(script, *args):
        observations.append(script)
        return state if script == PROBE else clock[0] >= 4

    async def sleep(seconds):
        assert seconds == 2
        clock[0] += seconds

    page.evaluate.side_effect = evaluate
    monkeypatch.setattr(human_login.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(human_login.asyncio, "sleep", sleep)
    executor = AsyncMock(return_value={"ok": True})
    wait = AsyncMock(return_value={"id": "task"})
    finish = AsyncMock()
    wrapper = HumanLoginExecutor(
        executor,
        SimpleNamespace(login_page=page),
        contract(human_submit_required=True, human_submit_timeout_seconds=10),
        AsyncMock(),
        wait,
        finish,
    )

    async def run():
        assert (await wrapper({"phase": "step", "seq": 3}))["ok"]
        assert (await wrapper({"phase": "step", "seq": 4}))["ok"]
        # Accidental replay of a completed submit is also harmless.
        assert (await wrapper({"phase": "step", "seq": 3}))["ok"]

    asyncio.run(run())
    assert clock[0] == 4
    assert len(observations) > 4
    assert executor.await_count == 1
    assert executor.call_args.args[0]["seq"] == 4
    wait.assert_awaited_once()
    finish.assert_awaited_once_with({"id": "task"}, True, "")


@pytest.mark.parametrize("reason", ["access_denied", "captcha_required"])
def test_blocked_screen_stops_and_persists_without_submit(reason):
    page = AsyncMock()
    page.evaluate.side_effect = lambda script, *args: (
        {"url": 1, "requests": 0, "navigation": 1, "blocked": reason} if script == PROBE else False
    )
    executor, store, wait = AsyncMock(), AsyncMock(), AsyncMock(return_value={"id": "task"})
    wrapper = HumanLoginExecutor(
        executor, SimpleNamespace(login_page=page), contract(), store, wait, AsyncMock()
    )
    result = asyncio.run(wrapper({"phase": "step", "seq": 3}))
    assert result["error"] == reason
    assert result["recovery"]["auth_task_id"] == "task"
    assert result["recovery"]["auto_retry"] is False
    executor.assert_not_awaited()
    store.access.assert_awaited_once_with(reason=reason)
    wait.assert_awaited_once()


def test_persisted_site_block_routes_next_execution_to_human(monkeypatch):
    db = PolicyDB()
    monkeypatch.setattr(human_login, "get_pool", lambda: db)
    state = {"url": 1, "requests": 0, "navigation": 1, "blocked": ""}
    authenticated = False
    page = AsyncMock()

    async def evaluate(script, *args):
        return state if script == PROBE else authenticated

    page.evaluate.side_effect = evaluate

    async def run():
        nonlocal authenticated
        settings = contract(submit_observation_seconds=0, human_submit_timeout_seconds=0)
        submit = AsyncMock(return_value={"ok": True})
        first = HumanLoginExecutor(
            submit,
            SimpleNamespace(login_page=page),
            settings,
            LoginPolicyStore(TENANT, "example.com", ["credential-id"]),
            AsyncMock(return_value={"id": "first"}),
            AsyncMock(),
        )
        result = await first({"phase": "step", "seq": 3})
        assert result["error"] == "bot_protection_suspected"
        submit.assert_awaited_once()

        async def human_wait(payload, reason):
            nonlocal authenticated
            assert reason == "human_submit_required"
            authenticated = True
            return {"id": "second"}

        second_submit = AsyncMock()
        second = HumanLoginExecutor(
            second_submit,
            SimpleNamespace(login_page=page),
            settings,
            LoginPolicyStore(TENANT, "example.com", ["credential-id"]),
            human_wait,
            AsyncMock(),
        )
        assert (await second({"phase": "step", "seq": 3}))["ok"]
        second_submit.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("case,clicks", [
    ("success", 1), ("unresponsive", 1), ("blocked", 0),
    ("server", 0), ("limited", 0), ("unresolved", 0),
    ("focus_failed", 0), ("click_failed", 1),
])
def test_native_single_click_and_human_fallback(case, clicks):
    events = []
    page = AsyncMock()
    state = {"url": 1, "requests": 1, "navigation": 1,
             "blocked": "captcha_required" if case == "blocked" else ""}
    authenticated = False

    async def evaluate(script, *args):
        if script == PROBE:
            return state
        if script == human_login.SUCCESS_PROBE:
            return authenticated
        events.append("coordinate")
        return {"resolved": "0"} if case == "unresolved" else {"resolved": "1", "x": "20", "y": "30"}

    async def command(name, args, **kwargs):
        nonlocal authenticated
        events.append(name)
        if (case, name) in {("focus_failed", "window_focus"), ("click_failed", "mouse_click")}:
            raise RuntimeError("PC command failed")
        if name == "mouse_click":
            assert args == {"x": 20, "y": 30, "button": "left", "clicks": 1}
            authenticated = case == "success"

    async def wait(payload, reason):
        nonlocal authenticated
        authenticated = True
        assert reason in {"human_submit_required", "captcha_required"}
        return {"id": "task"}

    page.evaluate.side_effect = evaluate
    page._run_browser_command.side_effect = command
    page.title.return_value = "Login"
    store = AsyncMock()
    store.access.side_effect = lambda **kw: case == "limited" and kw.get("reserve", False)
    executor = AsyncMock(return_value={"ok": True})
    wrapper = HumanLoginExecutor(
        executor, SimpleNamespace(login_page=page, login_lane=lambda p: "server" if case == "server" else "pc"),
        contract(submit_mode="pc_native_click", submit_observation_seconds=0), store,
        AsyncMock(side_effect=wait), AsyncMock(),
    )

    async def run():
        result = await wrapper({"phase": "step", "seq": 3, "selector": "#submit"})
        if case == "server":
            assert result["error"] == "pc_native_click_requires_pc_lane"
        elif case == "blocked":
            assert result["error"] == "captcha_required"
        else:
            assert result["ok"]
            expected = {"success": "pc_native_click_submitted", "unresponsive": "pc_native_click_failed_fallback_human",
                        "unresolved": "pc_native_click_coordinate_unresolved"}.get(case)
            if expected:
                assert result["output"]["submit_status"] == expected
            await wrapper({"phase": "step", "seq": 4})
            executor.assert_awaited_once()
    asyncio.run(run())
    assert events.count("mouse_click") == clicks
    if case == "focus_failed":
        assert events == ["coordinate", "window_focus"]
    if clicks:
        assert events == ["coordinate", "window_focus", "mouse_click"]
        assert any(c.kwargs.get("reserve") for c in store.access.await_args_list)


def test_native_contract_normalization():
    assert normalize_login_contract(contract())["submit_mode"] == "browser"
    assert normalize_login_contract(contract(submit_mode="pc_native_click"))["submit_mode"] == "pc_native_click"
    assert normalize_login_contract(contract(submit_mode="pc_native_click", human_submit_required=True))["submit_mode"] == "human"
    with pytest.raises(ValueError, match="login_submit_mode_invalid"):
        normalize_login_contract(contract(submit_mode="other"))


def test_bank_native_click_regression(monkeypatch):
    from app.services import yeoljeong_bank_browser_connector as bank
    page = AsyncMock()
    monkeypatch.setattr(bank, "_evaluate_page", AsyncMock(return_value={"resolved": "1", "x": "10", "y": "20"}))
    monkeypatch.setattr(bank, "_try_pc_agent_keyboard_type", AsyncMock(return_value=True))
    result = asyncio.run(bank._try_pc_agent_click_and_type(page, "#field", "test-value"))
    assert result == {"attempted": "1", "coordinate_resolved": "1", "window_focused": "1",
                      "mouse_clicked": "1", "field_cleared": "1", "typed": "1"}
    assert [c.args[0] for c in page._run_browser_command.await_args_list] == [
        "window_focus", "mouse_click", "keyboard_hotkey", "keyboard_press"]
    assert page._run_browser_command.await_args_list[0].args[1] == {"title": "간편조회서비스"}
