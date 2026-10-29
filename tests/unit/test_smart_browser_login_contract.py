from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from app.services.work_recipe.login_contract import (
    normalize_login_contract,
    contract_route,
    assert_oauth_scope,
)
from app.services.work_recipe.schema import WorkRecipe
from app.services.auth_challenge_orchestrator import validate_auth_scope


def contract(access="server_verified", lane="server"):
    return {
        "version": 1,
        "access": access,
        "primary": ["password"],
        "mfa": ["totp"],
        "popups": ["notice_modal"],
        "options": ["social_oauth", "passkey"],
        "observation": {
            "origin": "https://example.com",
            "lane": lane,
            "artifact_id": "artifact",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        },
    }


def test_contract_roundtrip_keeps_options_separate_from_active_challenge():
    raw = contract()
    recipe = WorkRecipe.from_dict(
        {
            "name": "login",
            "domain": "example.com",
            "steps": [{"action": "snapshot", "selector": "body"}],
            "metadata": {"login_contract": raw},
        }
    )
    restored = WorkRecipe.from_yaml(recipe.to_yaml())
    assert restored.metadata["login_contract"] == normalize_login_contract(raw)
    assert "challenge" not in restored.metadata["login_contract"]
    assert contract_route(raw)["runtime"] == "browser_agent"


def test_server_restriction_never_implies_native_pc():
    assert contract_route(contract("server_restricted_pc_unverified"))["runtime"] == "human_gateway"
    assert (
        contract_route(contract("pc_verified_after_server_restriction", "pc"))["runtime"]
        == "pc_agent"
    )


def test_expired_observation_blocks_replay():
    raw = contract()
    raw["observation"]["expires_at"] = "2000-01-01T00:00:00+00:00"
    assert contract_route(raw)["reason"] == "login_observation_expired"


@pytest.mark.parametrize(
    "key,value",
    [("state", "secret"), ("code", "secret"), ("nonce", "secret"), ("password", "secret")],
)
def test_secret_fields_are_rejected(key, value):
    raw = contract()
    raw[key] = value
    with pytest.raises(ValueError):
        normalize_login_contract(raw)


def test_oauth_checks_callback_opener_target_without_returning_query_secrets():
    raw = contract()
    raw["oauth"] = {
        "provider": "https://id.example.com/login",
        "callback": "https://example.com/callback",
        "opener": "https://example.com",
        "target": "https://example.com",
    }
    assert_oauth_scope(
        raw,
        current_url="https://example.com/callback?code=secret&state=secret",
        opener_url="https://example.com",
        target_url="https://example.com",
    )
    with pytest.raises(ValueError):
        assert_oauth_scope(
            raw,
            current_url="https://evil.example/callback",
            opener_url="https://example.com",
            target_url="https://example.com",
        )
    invalid = deepcopy(raw)
    invalid["oauth"]["callback"] += "?state=secret"
    with pytest.raises(ValueError):
        normalize_login_contract(invalid)


def test_scope_rejects_secret_or_incomplete_checkpoint():
    with pytest.raises(ValueError):
        validate_auth_scope({"tenant_id": "tenant", "otp": "secret"})


async def test_resume_checkpoint_does_not_repeat_submit_or_otp_send():
    from app.services.work_recipe.player import play_recipe

    recipe = WorkRecipe.from_dict(
        {
            "name": "login",
            "domain": "example.com",
            "steps": [
                {"action": "click", "selector": "#send-otp", "risk": "READ"},
                {"action": "snapshot", "selector": "#account", "risk": "READ"},
            ],
        }
    )
    calls = []

    async def execute(payload):
        calls.append(payload["selector"])
        return {"ok": True, "output": {"verified": True}}

    result = await play_recipe(recipe, execute, context={"resume_checkpoint": 1})
    assert result.ok
    assert calls == ["#account"]


def test_legacy_browser_recipe_adapter_preserves_vault_references():
    from app.services.work_recipe.login_contract import adapt_browser_recipe

    scope = {
        "version": 1,
        "inputs": {
            "password_ref": {
                "credential_id": "22222222-2222-2222-2222-222222222222",
                "field": "password",
            }
        },
        "allowed_domains": ["example.com"],
        "allowed_recipes": ["legacy.login"],
        "work_key": "legacy.login",
    }
    recipe = adapt_browser_recipe(
        {
            "recipe_id": "legacy.login",
            "version": "v2",
            "allowed_origins": ["https://example.com"],
            "login_steps": [
                {"action": "fill", "selector": "#password", "value": "{{password_ref}}"}
            ],
        },
        credential_scope=scope,
    )
    assert recipe.version == 2
    assert recipe.metadata["credential_scope"] == scope
    assert "login_contract" not in recipe.metadata
    assert recipe.steps[0].value == "{{password_ref}}"


@pytest.mark.parametrize("failure", ["lane", "page", "verify"])
async def test_resume_live_failure_returns_to_resumable_wait(monkeypatch, failure):
    import uuid
    from types import SimpleNamespace
    from datetime import datetime, timedelta, timezone
    from app.core import db_pool
    from app.services.channel_router import ChannelRouter, directive_from_authenticated_context
    from app.services.work_recipe import orchestrator as o
    from app.services import auth_challenge_orchestrator as auth

    tenant, task = str(uuid.uuid4()), str(uuid.uuid4())
    recipe = WorkRecipe.from_dict(
        {
            "name": "login",
            "domain": "example.com",
            "steps": [{"action": "snapshot", "selector": "body"}],
            "verify": [
                {"action": "snapshot", "assertion": "element_visible", "selector": "#account"}
            ],
            "metadata": {"login_contract": {**contract(), "business_ref": "biz", "account_ref": "acct"}},
        }
    )
    scope = dict(
        tenant_id=tenant,
        session_id="session",
        business_ref="biz",
        account_ref="acct",
        recipe_name=recipe.name,
        recipe_version=recipe.version,
        work_key="login",
        lane="server",
        checkpoint=0,
        target="https://example.com",
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )
    row = {
        "status": "queued",
        "user_id": "user",
        "result": {"auth_scope": scope, "resume_artifact_id": "artifact"},
    }

    class Conn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def transaction(self):
            return self

        async def fetchrow(self, *args):
            return dict(row)

        async def execute(self, sql, *args):
            assert "status='running'" not in sql
            row["status"] = "waiting_for_user"

    monkeypatch.setattr(db_pool, "get_pool", lambda: SimpleNamespace(acquire=Conn))

    async def resolve(*args):
        return recipe

    async def user(**kwargs):
        return "user"

    monkeypatch.setattr(o, "resolve_recipe", resolve)
    monkeypatch.setattr(auth, "auth_wait_user", user)
    monkeypatch.setattr(o, "GuardedRunRecorder", lambda **kwargs: None)
    monkeypatch.setattr(o, "_recipe_requires_pc_lane", lambda recipe: False)

    class Executor:
        def __init__(self, **kwargs):
            pass

        def _route(self, payload):
            return {"runtime": "pc_agent" if failure == "lane" else "browser_agent"}

        async def _get_page(self, payload):
            if failure == "page":
                raise ValueError("page_unavailable")

    class Verifier:
        def __init__(self, *args):
            pass

        async def __call__(self, payload):
            return {"ok": False}

    monkeypatch.setattr(o, "BrowserRecipeExecutor", Executor)
    monkeypatch.setattr(o, "GenericVerificationExecutor", Verifier)
    envelope = directive_from_authenticated_context(
        {"tenant": {"id": tenant}, "membership": {"user_id": "user"}},
        session_id="session",
        correlation_id="test",
        payload={"directive": "login", "auth_task_id": task},
        capabilities=frozenset({"recipe.execute"}),
    )
    intent = ChannelRouter().route_directive(envelope, capability="recipe.execute")
    with pytest.raises(ValueError):
        await o.run_directive("login", tenant, action_intent=intent)
    assert row["status"] == "waiting_for_user"  # transition_auth_wait accepts resume/cancel again


async def test_unexecuted_human_gateway_step_replays_at_zero_checkpoint(monkeypatch):
    from app.services.work_recipe.executor import BrowserRecipeExecutor
    from app.services.work_recipe.player import play_recipe

    recipe = WorkRecipe.from_dict(
        {
            "name": "login",
            "domain": "example.com",
            "steps": [{"action": "click", "selector": "#send", "risk": "READ"}],
        }
    )
    executor = BrowserRecipeExecutor()
    monkeypatch.setattr(
        executor, "_route", lambda payload: {"runtime": "human_gateway", "reason": "manual"}
    )
    waiting = await executor({"action": "click", "selector": "#send"})
    assert not waiting["recovery"].get("step_completed")
    checkpoint = recipe.steps[0].seq - 1
    validate_auth_scope(
        dict(
            tenant_id="t",
            business_ref="b",
            account_ref="a",
            session_id="s",
            work_key="w",
            lane="server",
            target="https://example.com",
            recipe_name="login",
            recipe_version=1,
            checkpoint=checkpoint,
            expires_at="2099-01-01T00:00:00+00:00",
        )
    )
    calls = []

    async def execute(payload):
        calls.append(payload["selector"])
        return {"ok": True}

    result = await play_recipe(recipe, execute, context={"resume_checkpoint": checkpoint})
    assert result.ok and calls == ["#send"]


@pytest.mark.parametrize('completed,expected', [(False, 0), (True, 1)])
async def test_run_directive_persists_last_completed_checkpoint(monkeypatch, completed, expected):
    from types import SimpleNamespace
    from app.services.channel_router import ChannelRouter, directive_from_authenticated_context
    from app.services.work_recipe import orchestrator as o
    from app.services import auth_challenge_orchestrator as auth
    recipe = WorkRecipe.from_dict({
        'name': 'login', 'domain': 'example.com',
        'steps': [{'action': 'click', 'selector': '#send', 'risk': 'READ'}],
        'verify': [{'action': 'snapshot', 'selector': '#account', 'assertion': 'element_visible'}],
        'metadata': {'login_contract': {**contract(), 'business_ref': 'biz', 'account_ref': 'acct'}},
    })
    async def resolve(*args): return recipe
    async def user(**kwargs): return 'user'
    async def scoped(*args): return {}
    recovery = {'route': 'human_gateway', 'reason': 'active_auth_challenge', 'step_completed': completed}
    async def play(*args, **kwargs):
        return SimpleNamespace(ok=False, steps=[SimpleNamespace(seq=1, recovery=recovery)])
    saved = []
    async def persist(**kwargs):
        saved.append(kwargs['scope'])
        return {'id': 'task'}
    monkeypatch.setattr(o, 'resolve_recipe', resolve)
    monkeypatch.setattr(o, '_scoped_inputs', scoped)
    monkeypatch.setattr(o, 'play_recipe', play)
    monkeypatch.setattr(o, 'GuardedRunRecorder', lambda **kwargs: None)
    monkeypatch.setattr(auth, 'auth_wait_user', user)
    monkeypatch.setattr(auth, 'persist_auth_wait', persist)
    envelope = directive_from_authenticated_context(
        {'tenant': {'id': 'tenant'}, 'membership': {'user_id': 'user'}},
        session_id='session', correlation_id='test', payload={'directive': 'login'},
        capabilities=frozenset({'recipe.execute'}),
    )
    intent = ChannelRouter().route_directive(envelope, capability='recipe.execute')
    await o.run_directive('login', 'tenant', action_intent=intent)
    assert saved[0]['checkpoint'] == recovery['checkpoint'] == expected
