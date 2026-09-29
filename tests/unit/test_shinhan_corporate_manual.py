from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.work_recipe import shinhan_corporate_manual as manual
from app.services.work_recipe.schema import RecipeStep, WorkRecipe


class _Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return False


def recipe():
    return WorkRecipe(
        name="shinhan corporate manual", domain=manual.DOMAIN, version=1,
        steps=[
            RecipeStep(seq=1, action="navigate", url="https://bank.shinhan.com/rib/easy/index.jsp"),
            RecipeStep(seq=2, action="snapshot"),
            RecipeStep(seq=3, action="click", selector="#login"),
        ],
        metadata={"native_auth_required": True, manual.MANIFEST_KEY: {
            "stages": list(manual.STAGES), "stage_end_seq": [1, 2, 3],
        }},
    )


def evidence(index):
    screens = {1: "신한 기업 로그인", 2: "로그아웃 계좌조회", 3: "계좌번호 잔액 0원"}
    screen = screens[index]
    return {"url": "https://bank.shinhan.com/rib/easy/index.jsp",
            "dom": {"text": screen, "sha256": hashlib.sha256(screen.encode()).hexdigest()}}


def test_receipts_chain_open_authenticate_verify_and_reject_tampering():
    first = manual.receipt_for("open", evidence(1))
    second = manual.receipt_for("authenticate", evidence(2), first)
    third = manual.receipt_for("verify", evidence(3), second)
    assert all(manual.verify_receipt(row) for row in (first, second, third))
    with pytest.raises(manual.FinancialManualBlocked, match="predecessor_evidence_required"):
        manual.receipt_for("verify", evidence(3), {**second, "dom_sha256": "0" * 64})
    with pytest.raises(manual.FinancialManualBlocked, match="predecessor_evidence_required"):
        manual.receipt_for("authenticate", evidence(2))
    with pytest.raises(manual.FinancialManualBlocked, match="stage_screen_assertion_failed"):
        manual.receipt_for("authenticate", evidence(1), first)
    with pytest.raises(manual.FinancialManualBlocked, match="stage_screen_assertion_failed"):
        manual.receipt_for("verify", evidence(2), second)
    label_only = evidence(3)
    label_only["dom"]["text"] = "계좌번호 잔액"
    label_only["dom"]["sha256"] = hashlib.sha256("계좌번호 잔액".encode()).hexdigest()
    with pytest.raises(manual.FinancialManualBlocked, match="account_balance_evidence_required"):
        manual.receipt_for("verify", label_only, second)


@pytest.mark.asyncio
async def test_stage_executor_uses_actual_browser_evidence_for_all_three_stages():
    async def browser(payload):
        return {"ok": True, "evidence": evidence(payload["seq"])}

    wrapped = manual.StageEvidenceExecutor(browser, manual.validate_manifest(recipe()))
    for seq in (1, 2, 3):
        result = await wrapped({"seq": seq})
        assert result["evidence"]["stage_receipt"]["stage"] == manual.STAGES[seq - 1]
    assert len(wrapped.receipts) == 3


@pytest.mark.asyncio
async def test_stage_executor_verify_overlap_and_prior_stage_replay_are_blocked():
    calls = []

    async def browser(payload):
        calls.append((payload.get("phase"), payload["seq"]))
        return {"ok": True, "evidence": evidence(min(payload["seq"], 3))}

    wrapped = manual.StageEvidenceExecutor(browser, manual.validate_manifest(recipe()))
    assert (await wrapped({"seq": 1, "phase": "verify"}))["error"] == "predecessor_evidence_required"
    for seq in (1, 2, 3):
        assert (await wrapped({"seq": seq, "phase": "step"}))["ok"]
    assert (await wrapped({"seq": 2, "phase": "step"}))["error"] == "stage_replay_forbidden"
    assert (await wrapped({"seq": 1, "phase": "verify"}))["ok"]
    assert (await wrapped({"seq": 1, "phase": "verify"}))["error"] == "stage_replay_forbidden"
    assert calls == [("step", 1), ("step", 2), ("step", 3), ("verify", 1)]


@pytest.mark.asyncio
async def test_draft_registration_rejects_forged_status_before_session_or_vault(monkeypatch):
    class Conn:
        async def fetchrow(self, *_args):
            return None  # A caller's approved dict/callback cannot affect this query.

    monkeypatch.setattr(manual, "get_pool", lambda: SimpleNamespace(acquire=lambda: _Acquire(Conn())))
    session_lookup = AsyncMock()
    monkeypatch.setattr(manual, "get_browser_bridge_service", session_lookup)
    with pytest.raises(manual.FinancialManualBlocked, match="approved_registration_required"):
        await manual.assert_approved_execution(recipe(), "tenant", "forged-session", "yeoljeong-bank-shinhan-forged", "user")
    session_lookup.assert_not_called()


@pytest.mark.asyncio
async def test_approved_row_still_rejects_forged_pc_session(monkeypatch):
    class Conn:
        async def fetchrow(self, *_args):
            return {"status": "approved", "enabled": True, "approved_spec": recipe().to_dict(), "requested_spec": recipe().to_dict(), "approved_version": 1}

    monkeypatch.setattr(manual, "get_pool", lambda: SimpleNamespace(acquire=lambda: _Acquire(Conn())))
    monkeypatch.setattr(manual, "get_browser_bridge_service",
                        lambda: SimpleNamespace(sessions=SimpleNamespace(get=lambda _id: None)))
    with pytest.raises(manual.FinancialManualBlocked, match="authenticated_pc_session_required"):
        await manual.assert_approved_execution(recipe(), "tenant", "forged", "yeoljeong-bank-shinhan-forged", "user")


@pytest.mark.asyncio
async def test_approved_spec_uses_actual_registration_and_recipe_schema(monkeypatch):
    approved = recipe()
    approved.version = 2
    stored = {**approved.to_dict(), "id": "db-only-field"}
    requested = {**approved.to_dict(), "version": 1}
    observed = {}

    class Conn:
        async def fetchrow(self, query, *_args):
            observed["query"] = query
            return {
                "status": "approved", "enabled": True, "approved_version": 2,
                "approved_spec": stored, "requested_spec": requested,
            }

    session = SimpleNamespace(
        is_expired=False, work_key="yeoljeong-bank-shinhan-test",
        endpoint=SimpleNamespace(
            kind=manual.BrowserEndpointKind.LOCAL_AGENT, metadata={"agent_id": "agent"},
        ),
    )
    monkeypatch.setattr(manual, "get_pool", lambda: SimpleNamespace(acquire=lambda: _Acquire(Conn())))
    monkeypatch.setattr(manual, "get_browser_bridge_service",
                        lambda: SimpleNamespace(sessions=SimpleNamespace(get=lambda _id: session)))
    monkeypatch.setattr(manual.pc_agent_manager, "get_agent_status", lambda _id: {"status": "online"})
    monkeypatch.setattr(manual.pc_agent_manager, "get_agent", lambda _id: SimpleNamespace(
        user_id="user", tenant_id="tenant",
    ))
    await manual.assert_approved_execution(
        approved, "tenant", "session", "yeoljeong-bank-shinhan-test", "user",
    )
    assert "w.spec AS approved_spec" in observed["query"]
    assert "r.spec AS requested_spec" in observed["query"]
    assert "w.version AS approved_version" in observed["query"]
    stored["steps"][0]["url"] = "https://evil.example"
    with pytest.raises(manual.FinancialManualBlocked, match="approved_recipe_mismatch"):
        await manual.assert_approved_execution(
            approved, "tenant", "session", "yeoljeong-bank-shinhan-test", "user",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_user,owner_tenant,allowed", [
    ("other-user", "tenant", False), ("user", "other-tenant", False),
    ("", "tenant", False), ("user", "tenant", True),
])
async def test_pc_agent_owner_must_match_authenticated_actor_and_tenant(
    monkeypatch, owner_user, owner_tenant, allowed,
):
    class Conn:
        async def fetchrow(self, *_args):
            return {"status": "approved", "enabled": True, "approved_spec": recipe().to_dict(), "requested_spec": recipe().to_dict(), "approved_version": 1}

    session = SimpleNamespace(
        is_expired=False, work_key="yeoljeong-bank-shinhan-test",
        endpoint=SimpleNamespace(
            kind=manual.BrowserEndpointKind.LOCAL_AGENT, metadata={"agent_id": "agent"},
        ),
    )
    monkeypatch.setattr(manual, "get_pool", lambda: SimpleNamespace(acquire=lambda: _Acquire(Conn())))
    monkeypatch.setattr(manual, "get_browser_bridge_service",
                        lambda: SimpleNamespace(sessions=SimpleNamespace(get=lambda _id: session)))
    monkeypatch.setattr(manual.pc_agent_manager, "get_agent_status", lambda _id: {"status": "online"})
    monkeypatch.setattr(manual.pc_agent_manager, "get_agent", lambda _id: SimpleNamespace(
        user_id=owner_user, tenant_id=owner_tenant,
    ))
    if allowed:
        await manual.assert_approved_execution(
            recipe(), "tenant", "session", "yeoljeong-bank-shinhan-test", "user",
        )
    else:
        with pytest.raises(manual.FinancialManualBlocked, match="authenticated_pc_session_required"):
            await manual.assert_approved_execution(
                recipe(), "tenant", "session", "yeoljeong-bank-shinhan-test", "user",
            )


def test_manifest_rejects_missing_stage_and_transfer():
    item = recipe()
    item.metadata[manual.MANIFEST_KEY]["stage_end_seq"] = [1, 2, 2]
    with pytest.raises(manual.FinancialManualBlocked, match="shinhan_stage_boundaries_invalid"):
        manual.validate_manifest(item)
    item = recipe()
    item.steps[2].description = "송금"
    with pytest.raises(manual.FinancialManualBlocked, match="financial_mutation_forbidden"):
        manual.validate_manifest(item)
    item = recipe()
    item.steps[2].action = "api_call"
    item.steps[2].endpoint = "/opaque"
    item.steps[2].value = {"method": "POST", "body": "hidden"}
    with pytest.raises(manual.FinancialManualBlocked, match="shinhan_action_forbidden"):
        manual.validate_manifest(item)


@pytest.mark.parametrize("domain", ["shinhan.com", "bizbank.shinhan.com", "unrelated.example"])
def test_step_url_always_routes_to_financial_gate(domain):
    item = recipe()
    item.domain = domain
    assert manual.is_shinhan_recipe(item)
    with pytest.raises(manual.FinancialManualBlocked, match="shinhan_origin_invalid"):
        manual.validate_manifest(item)


def test_templated_url_cannot_hide_bank_destination():
    item = recipe()
    item.domain = "unrelated.example"
    item.steps[0].url = "{{destination}}"
    assert manual.is_shinhan_recipe(item)
    with pytest.raises(manual.FinancialManualBlocked, match="shinhan_origin_invalid"):
        manual.validate_manifest(item)


@pytest.mark.parametrize("selector", ["#btnTrnsfr", "#remitOk", "#payment", "#unknown"])
def test_unsafe_click_selectors_are_refused(selector):
    item = recipe()
    item.steps[2].selector = selector
    with pytest.raises(manual.FinancialManualBlocked):
        manual.validate_manifest(item)


@pytest.mark.parametrize("action", ["api_call", "press", "select", "evaluate", "script", "press_key"])
def test_unsafe_actions_are_refused(action):
    item = recipe()
    item.steps[2].action = action
    with pytest.raises(manual.FinancialManualBlocked, match="shinhan_action_forbidden"):
        manual.validate_manifest(item)


@pytest.mark.parametrize("url", [
    "http://bank.shinhan.com/login",
    "https://bank.shinhan.com@evil.example/login",
    "https://evil.example/login?next=bank.shinhan.com",
])
def test_bank_url_requires_authenticated_https_origin(url):
    item = recipe()
    item.steps[0].url = url
    with pytest.raises(manual.FinancialManualBlocked, match="shinhan_origin_invalid"):
        manual.validate_manifest(item)


@pytest.mark.asyncio
async def test_registration_request_rejects_opaque_post_before_database(monkeypatch):
    from app.services.work_recipe import registration

    item = recipe()
    item.steps[2].action = "api_call"
    item.steps[2].endpoint = "/opaque"
    item.steps[2].value = {"method": "POST", "body": "hidden"}
    database = AsyncMock()
    monkeypatch.setattr(registration, "get_pool", database)
    with pytest.raises(manual.FinancialManualBlocked, match="shinhan_action_forbidden"):
        await registration.request_registration(item, tenant_id="tenant")
    database.assert_not_called()


@pytest.mark.asyncio
async def test_forged_approval_cannot_reach_vault_or_login_submit(monkeypatch):
    from app.services.work_recipe import orchestrator

    intent = SimpleNamespace(
        tenant_id="tenant", payload={"directive": "shinhan", "inputs": {}},
        authenticated_provenance={"user_id": "user", "tenant_id": "tenant"},
    )
    class Router:
        def validate_action_intent(self, action_intent, **_kwargs):
            return action_intent
        def assert_no_untrusted_page_data(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(orchestrator, "ChannelRouter", Router)
    monkeypatch.setattr(orchestrator, "resolve_recipe", AsyncMock(return_value=recipe()))
    denied = AsyncMock(side_effect=manual.FinancialManualBlocked("approved_registration_required"))
    vault = AsyncMock()
    browser = AsyncMock()
    monkeypatch.setattr(orchestrator, "assert_approved_execution", denied)
    monkeypatch.setattr(orchestrator, "_scoped_inputs", vault)
    monkeypatch.setattr(orchestrator, "BrowserRecipeExecutor", browser)
    with pytest.raises(manual.FinancialManualBlocked, match="approved_registration_required"):
        await orchestrator.run_directive(
            "shinhan", "tenant", action_intent=intent,
            browser_session_id="forged", browser_work_key="yeoljeong-bank-shinhan-forged",
        )
    vault.assert_not_called()
    browser.assert_not_called()


@pytest.mark.asyncio
async def test_financial_lock_precedes_approval_and_vault_and_keeps_pc_key(monkeypatch):
    from app.services.work_recipe import orchestrator

    events = []
    intent = SimpleNamespace(
        tenant_id="tenant", payload={"directive": "shinhan", "inputs": {}},
        authenticated_provenance={"user_id": "user"},
    )

    class Router:
        def validate_action_intent(self, action_intent, **_kwargs):
            return action_intent
        def assert_no_untrusted_page_data(self, *_args, **_kwargs):
            return None

    class Lock:
        def __enter__(self):
            events.append("lock")
        def __exit__(self, *_args):
            events.append("unlock")

    async def approval(*_args):
        events.append("approval")

    async def vault(*_args):
        events.append("vault")
        return {}

    async def play(*_args, **_kwargs):
        events.append("play")
        return "ok"

    def browser(**kwargs):
        assert kwargs["browser_session_id"] == "session"
        assert kwargs["browser_work_key"] == "yeoljeong-bank-shinhan-test"
        return lambda _payload: None

    monkeypatch.setattr(orchestrator, "ChannelRouter", Router)
    monkeypatch.setattr(orchestrator, "resolve_recipe", AsyncMock(return_value=recipe()))
    monkeypatch.setattr(orchestrator, "FinancialExecutionLock", Lock)
    monkeypatch.setattr(orchestrator, "assert_approved_execution", approval)
    monkeypatch.setattr(orchestrator, "_scoped_inputs", vault)
    monkeypatch.setattr(orchestrator, "BrowserRecipeExecutor", browser)
    monkeypatch.setattr(orchestrator, "GuardedRunRecorder", lambda **_kwargs: None)
    monkeypatch.setattr(orchestrator, "play_recipe", play)
    result = await orchestrator.run_directive(
        "shinhan", "tenant", action_intent=intent,
        browser_session_id="session", browser_work_key="yeoljeong-bank-shinhan-test",
    )
    assert result == "ok"
    assert events == ["lock", "approval", "vault", "play", "unlock"]


@pytest.mark.asyncio
async def test_reject_legacy_pending_shinhan_draft_without_manifest(monkeypatch):
    from app.services.work_recipe import registration

    tenant = "2d701a8c-9596-4757-8588-faa4f7837112"
    draft = recipe().to_dict()
    draft["metadata"].pop(manual.MANIFEST_KEY)

    class Conn:
        def transaction(self):
            return self
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            return False
        async def fetchrow(self, query, *_args):
            if "FOR UPDATE" in query:
                return {"status": "pending", "spec": draft}
            return {"status": "rejected", "spec": draft, "tenant_id": tenant}

    conn = Conn()
    monkeypatch.setattr(registration, "get_pool", lambda: SimpleNamespace(acquire=lambda: conn))
    result = await registration.decide_registration(
        "8c6c6931-70d1-48a8-9d59-b3d548e787cb",
        tenant_id=tenant, decision="reject", decided_by="ceo",
    )
    assert result["status"] == "rejected"


@pytest.mark.parametrize("url", ["https://example.com/{{path}}", "https://example.com/?q={{query}}"])
def test_nonbank_fixed_host_template_does_not_enter_financial_gate(url):
    item = WorkRecipe(name="nonfinancial", domain="example.com", steps=[
        RecipeStep(seq=1, action="navigate", url=url),
    ])
    assert manual.is_shinhan_recipe(item) is False
    assert manual.validate_manifest(item) == ()


@pytest.mark.asyncio
async def test_financial_snapshots_are_redacted_on_all_step_and_verify_paths():
    raw_screen = "fixture financial screen not for persistence"
    async def executor(payload):
        stage = {2: 1, 3: 2, 4: 3}.get(payload["seq"], 1)
        return {"ok": True, "status": "success", "output": raw_screen,
                "evidence": {**evidence(stage), "aria": {"text": raw_screen}},
                "narration": raw_screen}
    wrapper = manual.StageEvidenceExecutor(executor, (2, 3, 4))
    for seq in range(1, 5):
        result = await wrapper({"seq": seq, "phase": "step"})
        assert result["ok"] is True
        assert result["output"] is None
        assert "dom" not in result["evidence"]
        assert "aria" not in result["evidence"]
        assert raw_screen not in str(result)
        if seq > 1:
            assert manual.verify_receipt(result["evidence"]["stage_receipt"])
    result = await wrapper({"seq": 1, "phase": "verify"})
    assert result["output"] is None
    assert result["evidence"] == {}
    assert raw_screen not in str(result)
