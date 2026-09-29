"""Unit contracts only: synthetic records here are never operational evidence."""
import asyncio
import pytest

from app.services import browser_recipe_registry as registry
from app.services.work_recipe import orchestrator
from app.services.work_recipe.guard import classify_step, requires_approval


RUN_ID = "11111111-1111-1111-1111-111111111111"
SCREEN = "https://aads.newtalk.kr/screenshots/test-fixture.png"


def _evidence(name="coupangeats_01_open_login"):
    fragment = next(part for part in registry.COUPANGEATS_LOGIN_FRAGMENTS if part["name"] == name)
    return {
        "screen_verified": True,
        "screenshot_url": SCREEN,
        "run_id": RUN_ID,
        "url": registry.COUPANGEATS_LOGIN_URL,
        "outcome": fragment["succeeds_when"],
        "blocked": False,
    }


def _trusted(name="coupangeats_01_open_login"):
    return {
        "run_id": RUN_ID, "screenshot_url": SCREEN,
        "url": registry.COUPANGEATS_LOGIN_URL,
        "status": "success", "route": "pc_agent", "verified": True,
        "recipe_name": name,
        "chat_session_id": "recording-session",
    }


def _open_recipe(trusted=None):
    return registry.build_coupangeats_registration_candidate(
        fragment_name="coupangeats_01_open_login",
        observed_steps=[{"action": "navigate", "url": registry.COUPANGEATS_LOGIN_URL, "risk": "READ"}],
        observed_verify=[{"action": "snapshot", "selector": "input[type=password]", "assertion": "login_form_visible"}],
        screen_evidence=_evidence(), trusted_evidence=trusted,
    )


def test_fake_screen_reference_cannot_create_candidate():
    with pytest.raises(ValueError, match="verified_screen_evidence_required"):
        _open_recipe()
    with pytest.raises(ValueError, match="verified_screen_evidence_required"):
        _open_recipe({**_trusted(), "screenshot_url": "other"})
    assert _open_recipe(_trusted()).metadata["source_chat_session_id"] == "recording-session"


def test_approved_requires_url_conditions_and_audited_screen():
    recipe = _open_recipe(_trusted())
    row = {
        "name": recipe.name, "domain": recipe.domain, "version": 2,
        "enabled": True, "max_risk": "READ", "approval_status": "approved",
        "spec": {**recipe.to_dict(), "version": 2},
    }
    assert registry.prepare_coupangeats_login_drafts(row)["reference_verified"] is False
    assert registry.prepare_coupangeats_login_drafts(row, trusted_evidence=_trusted())["reference_verified"]
    for mutation in (
        {"steps": [{"action": "navigate", "url": "https://example.org"}]},
        {"metadata": {**recipe.metadata, "succeeds_when": "anything"}},
        {"metadata": {**recipe.metadata, "starts_when": "anything"}},
        {"metadata": {**recipe.metadata, "screen_e2e": {**_evidence(), "url": "https://example.org"}}},
    ):
        changed = {**row, "spec": {**row["spec"], **mutation}}
        assert not registry.prepare_coupangeats_login_drafts(changed, trusted_evidence=_trusted())["reference_verified"]


def test_login_submit_is_always_approval_gated():
    recipe = registry.build_coupangeats_registration_candidate(
        fragment_name="coupangeats_03_login_confirm",
        observed_steps=[{"action": "click", "selector": "#anonymous-button"}],
        observed_verify=[
            {"action": "snapshot", "selector": "#username", "assertion": "authenticated_username_matches_vault"},
            {"action": "snapshot", "selector": "#store", "assertion": "authenticated_store_matches_vault"},
        ],
        screen_evidence={**_evidence("coupangeats_03_login_confirm"), "url": "https://store.coupangeats.com/merchant/home"},
        trusted_evidence={**_trusted("coupangeats_03_login_confirm"), "url": "https://store.coupangeats.com/merchant/home"},
    )
    assert recipe.steps[0].risk == "WRITE_EXTERNAL"
    assert requires_approval(classify_step(recipe.steps[0], domain=recipe.domain))


def test_wrong_lane_replay_refused_without_executing():
    class WrongLane:
        _page = object()
        def _route(self, payload):
            return {"runtime": "browser_agent"}
        async def __call__(self, payload):
            raise AssertionError("browser action must not run")
    wrapper = orchestrator.CoupangVerificationExecutor(WrongLane(), _open_recipe(_trusted()), {})
    result = asyncio.run(wrapper({"action": "navigate", "phase": "step"}))
    assert result["ok"] is False and result["error"] == "pc_agent_lane_required"


def test_failed_verify_never_reports_success():
    class Element:
        async def is_visible(self):
            return False
        async def inner_text(self):
            return ""
    class Page:
        url = registry.COUPANGEATS_LOGIN_URL
        def locator(self, selector):
            return Element()
    class Executor:
        _page = Page()
        def _route(self, payload):
            return {"runtime": "pc_agent"}
        async def _collect_evidence(self, page):
            raise AssertionError("failed verify must not collect success evidence")
    wrapper = orchestrator.CoupangVerificationExecutor(Executor(), _open_recipe(_trusted()), {})
    result = asyncio.run(wrapper({"action": "snapshot", "phase": "verify", "selector": "input", "assertion": "login_form_visible"}))
    assert result == {"ok": False, "error": "coupangeats_verification_failed", "route": "pc_agent"}


def test_login_submit_before_step_stops_without_approval():
    from app.services.work_recipe.approval import ApprovalRequired
    from app.services.work_recipe.audit import GuardedRunRecorder
    from app.services.work_recipe.schema import RecipeStep

    requested = []
    async def deny(*args, **kwargs):
        requested.append(kwargs["action"])
        raise ApprovalRequired("fixture-approval", summary="login approval pending")
    async def record(*args, **kwargs):
        return None
    async def block(*args, **kwargs):
        return None
    guard = GuardedRunRecorder(
        domain="store.coupangeats.com", tenant_id="fixture-tenant",
        approval_requester=deny, step_recorder=record, run_blocker=block,
    )
    submit = RecipeStep.from_dict(
        {"action": "click", "selector": "#anonymous-button", "risk": "WRITE_EXTERNAL"}, seq=1
    )
    with pytest.raises(ApprovalRequired):
        asyncio.run(guard.before_step(run_id=None, step=submit))
    assert requested == ["click"]


def test_live_attestation_rejects_missing_designated_pc_session(monkeypatch):
    from app.browser_bridge import service

    class Sessions:
        def get(self, session_id):
            return None
    class Bridge:
        sessions = Sessions()
    monkeypatch.setattr(service, "get_browser_bridge_service", lambda: Bridge())
    with pytest.raises(ValueError, match="coupangeats_pc_session_required"):
        asyncio.run(registry.attest_coupangeats_live_screen(
            tenant_id="11111111-1111-1111-1111-111111111111",
            chat_session_id="recording-session", fragment_name="coupangeats_01_open_login",
            browser_session_id="fake", browser_work_key="coupangeats:owner:login",
            observed_verify=[{"action": "snapshot", "selector": "input[type=password]", "assertion": "login_form_visible"}],
            values={},
        ))


def test_vault_identity_requires_the_designated_account_and_store():
    identity = {
        "vault_scope": "yeoljeong-coupangeats", "vault_username": "mimi77",
        "vault_store": "fixture-store", "screen_username": "mimi77",
        "screen_store": "fixture-store",
    }
    assert registry.matches_coupangeats_vault_identity(**identity)
    assert not registry.matches_coupangeats_vault_identity(**{**identity, "vault_username": "other"})
    assert not registry.matches_coupangeats_vault_identity(**{**identity, "screen_store": "other"})


def test_pc_agent_adapter_reads_live_url_and_screen_username():
    """Exercise the actual PC facade, whose locator has no Playwright read methods."""
    from app.browser_bridge.service import _LocalAgentPage

    recipe = registry.build_coupangeats_registration_candidate(
        fragment_name="coupangeats_03_login_confirm",
        observed_steps=[{"action": "click", "selector": "#submit"}],
        observed_verify=[
            {"action": "snapshot", "selector": "#username", "assertion": "authenticated_username_matches_vault"},
            {"action": "snapshot", "selector": "#store", "assertion": "authenticated_store_matches_vault"},
        ],
        screen_evidence={**_evidence("coupangeats_03_login_confirm"), "url": "https://store.coupangeats.com/merchant/home"},
        trusted_evidence={**_trusted("coupangeats_03_login_confirm"), "url": "https://store.coupangeats.com/merchant/home"},
    )

    class Executor:
        def __init__(self, page):
            self._page = page
            self.captured = 0
        def _route(self, payload):
            return {"runtime": "pc_agent"}
        async def _collect_evidence(self, page):
            self.captured += 1
            return {"screenshot": {"status": "captured", "url": SCREEN}}

    async def check(screen_username, screen_store="fixture-store", live_url="https://store.coupangeats.com/merchant/home"):
        page = object.__new__(_LocalAgentPage)
        page.url = registry.COUPANGEATS_LOGIN_URL  # stale after PC Agent click
        seen = []
        async def command(kind, params, **kwargs):
            assert kind == "browser_eval"
            seen.append(params["expression"])
            username_step = len(seen) == 1
            return {"value": {
                "url": live_url,
                "visible": True, "usernameMatches": username_step and screen_username == "mimi77",
                "screenText": screen_username if username_step else screen_store,
                "blocked": False,
            }}
        page._run_browser_command = command
        executor = Executor(page)
        verifier = orchestrator.CoupangVerificationExecutor(
            executor, recipe, {"vault_username": "mimi77", "vault_store": "fixture-store"}
        )
        results = []
        for step in recipe.verify:
            results.append(await verifier({**step.to_dict(), "phase": "verify", "context": {
                "smart_browser": {"server_access_blocked": True}}}))
            if not results[-1]["ok"]:
                break
        return results, executor.captured, page.url, seen

    good, captured, url, commands = asyncio.run(check("mimi77"))
    assert all(result["ok"] for result in good)
    assert captured == 2
    assert url.endswith("/merchant/home")
    assert all("browser-bridge argument redacted" in command for command in commands)

    bad, captured, _, _ = asyncio.run(check("another-user"))
    assert bad[0]["ok"] is False
    assert captured == 0
    bad_store, captured, _, _ = asyncio.run(check("mimi77", screen_store="other-store"))
    assert bad_store[-1]["ok"] is False
    assert captured == 1
    login_page, captured, _, _ = asyncio.run(check("mimi77", live_url=registry.COUPANGEATS_LOGIN_URL))
    assert login_page[0]["ok"] is False
    assert captured == 0


def test_replay_requires_another_chat_session_on_the_designated_pc_lane(monkeypatch):
    from types import SimpleNamespace
    from app.browser_bridge import service

    recipe = _open_recipe(_trusted())
    intent = SimpleNamespace(tenant_id="fixture-tenant", payload={"directive": recipe.name, "inputs": {}})
    class Router:
        def validate_action_intent(self, value, **kwargs):
            return value
        def assert_no_untrusted_page_data(self, *args, **kwargs):
            return None
    class Sessions:
        def __init__(self):
            self.kind = "local_agent"
        def get(self, session_id):
            return SimpleNamespace(
                work_key="coupangeats:owner:login",
                endpoint=SimpleNamespace(kind=SimpleNamespace(value=self.kind)),
            )
    sessions = Sessions()
    monkeypatch.setattr(service, "get_browser_bridge_service", lambda: SimpleNamespace(sessions=sessions))
    monkeypatch.setattr(orchestrator, "ChannelRouter", Router)
    async def resolve(*args):
        return recipe
    async def scoped(*args):
        return {}
    calls = []
    async def play(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(to_dict=lambda: {"status": "success"})
    monkeypatch.setattr(orchestrator, "resolve_recipe", resolve)
    monkeypatch.setattr(orchestrator, "_scoped_inputs", scoped)
    monkeypatch.setattr(orchestrator, "GuardedRunRecorder", lambda **kwargs: object())
    monkeypatch.setattr(orchestrator, "BrowserRecipeExecutor", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(orchestrator, "play_recipe", play)

    async def replay(chat_session):
        return await orchestrator.run_directive(
            recipe.name, "fixture-tenant", browser_session_id="pc-session",
            browser_work_key="coupangeats:owner:login", triggered_by=f"chat:{chat_session}",
            action_intent=intent,
        )
    with pytest.raises(ValueError, match="replay_boundary"):
        asyncio.run(replay("recording-session"))
    sessions.kind = "server"
    with pytest.raises(ValueError, match="pc_lane_required"):
        asyncio.run(replay("other-session"))
    sessions.kind = "local_agent"
    assert asyncio.run(replay("other-session")).to_dict()["status"] == "success"
    assert len(calls) == 1
    assert calls[0]["context"]["smart_browser"]["server_access_blocked"] is True
