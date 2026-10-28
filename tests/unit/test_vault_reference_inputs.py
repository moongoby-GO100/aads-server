"""Smart Browser 승인 레시피의 서버측 Vault 참조 해석 (AADS-SB-VAULT-REFERENCE-20261028).

secret 입력은 평문 value 가 아니라 승인 판본 metadata.credential_scope 가 정한
{credential_id, field} 로만 채워진다. 합성 비밀값(SECRET/USER)을 쓰고, 어떤 출력·오류·로그·감사에도
그 값이 나오지 않는지 함께 본다.
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from app.core import credential_vault
from app.services.channel_router import ChannelRouter, directive_from_authenticated_context
from app.services.work_recipe import executor as executor_module
from app.services.work_recipe import orchestrator, vault_reference
from app.services.work_recipe.player import RecipePlayer
from app.services.work_recipe.schema import parse_recipe

TENANT = "00000000-0000-0000-0000-000000000001"
OTHER_TENANT = "00000000-0000-0000-0000-000000000002"
CRED_USER = "11111111-1111-4111-8111-111111111111"
CRED_PW = "22222222-2222-4222-8222-222222222222"
SECRET = "Zx9!synthetic-SECRET-pw"
USER = "synthetic-user-id"
DOMAIN = "smartsms.aligo.in"
RECIPE_NAME = "aligo_01_login_to_mfa"
WORK_KEY = "aligo:owner:login"
LOGIN_URL = f"https://{DOMAIN}/login"


def scope(**override: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "version": 1,
        "inputs": {
            "credential_1": {"credential_id": CRED_USER, "field": "username"},
            "credential_2": {"credential_id": CRED_PW, "field": "password"},
        },
        "allowed_domains": [DOMAIN],
        "allowed_recipes": [RECIPE_NAME],
        "work_key": WORK_KEY,
    }
    base.update(override)
    return base


def recipe_dict(credential_scope: Any = "default", **override: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": RECIPE_NAME,
        "domain": DOMAIN,
        "version": 2,
        "inputs": [
            {"name": "credential_1", "secret": True, "required": True},
            {"name": "credential_2", "secret": True, "required": True},
        ],
        "steps": [
            {"action": "navigate", "url": LOGIN_URL, "risk": "READ"},
            {"action": "fill", "selector": "#user_id", "value": "{{credential_1}}", "risk": "READ"},
            {"action": "fill", "selector": "#user_pw", "value": "{{credential_2}}", "risk": "READ"},
            {"action": "click", "selector": "#login", "risk": "READ"},
        ],
        "metadata": {
            "work_key": WORK_KEY,
            "credential_scope": scope() if credential_scope == "default" else credential_scope,
        },
    }
    data.update(override)
    return data


def make_recipe(credential_scope: Any = "default", **override: Any):
    return parse_recipe(recipe_dict(credential_scope, **override))


def vault_row(credential_id: str, **override: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": credential_id,
        "tenant_id": TENANT,
        "is_active": True,
        "origin": f"https://{DOMAIN}",
        "url": f"https://{DOMAIN}/login",
        "work_key": WORK_KEY,
        "username": USER,
        "password": SECRET,
        "metadata": {},
    }
    row.update(override)
    return row


@pytest.fixture
def vault(monkeypatch):
    """credential_vault.get_credential 대역. 호출 기록과 행 교체를 지원한다."""
    state = SimpleNamespace(rows={CRED_USER: vault_row(CRED_USER), CRED_PW: vault_row(CRED_PW)}, calls=[])

    async def get_credential(credential_id, include_secrets=True, tenant_id=None, require_active=False):
        state.calls.append({"id": str(credential_id), "tenant_id": tenant_id, "require_active": require_active})
        row = state.rows.get(str(credential_id))
        return dict(row) if row is not None else None

    monkeypatch.setattr(credential_vault, "get_credential", get_credential)
    return state


async def resolve(recipe=None, inputs=None, **kwargs):
    return await vault_reference.resolve_inputs(
        recipe or make_recipe(), inputs if inputs is not None else {},
        tenant_id=kwargs.pop("tenant_id", TENANT), browser_work_key=kwargs.pop("browser_work_key", WORK_KEY),
        **kwargs,
    )


async def blocked(code: str, **kwargs) -> vault_reference.VaultReferenceError:
    with pytest.raises(vault_reference.VaultReferenceError) as caught:
        await resolve(**kwargs)
    assert caught.value.code == code, caught.value.code
    assert SECRET not in json.dumps(caught.value.to_response(), ensure_ascii=False)
    assert SECRET not in str(caught.value) and USER not in str(caught.value)
    return caught.value


# ── 정상 경로 ───────────────────────────────────────────────────────────────


async def test_normal_reference_resolves_from_approved_metadata(vault):
    resolution = await resolve()
    assert resolution.values == {"credential_1": USER, "credential_2": SECRET}
    assert resolution.secret_names == {"credential_1", "credential_2"}
    assert resolution.fill_steps == {2, 3}
    assert [(u["input"], u["credential_id"], u["field"]) for u in resolution.uses] == [
        ("credential_1", CRED_USER, "username"), ("credential_2", CRED_PW, "password"),
    ]
    # 테넌트는 서버가 넘긴 값이고 비활성 행은 조회 단계에서 이미 걸러진다.
    assert all(c["tenant_id"] == TENANT and c["require_active"] is True for c in vault.calls)
    assert SECRET not in repr(resolution) and USER not in repr(resolution)


async def test_caller_may_repeat_the_approved_reference_but_not_change_it(vault):
    ok = {"credential_1": {"credential_id": CRED_USER, "field": "username"},
          "credential_2": {"credential_id": CRED_PW.upper(), "field": "password"}}
    assert (await resolve(inputs=ok)).values["credential_2"] == SECRET


async def test_binding_does_not_depend_on_input_name_or_order(vault):
    swapped = scope(inputs={
        "credential_1": {"credential_id": CRED_PW, "field": "password"},
        "credential_2": {"credential_id": CRED_USER, "field": "username"},
    })
    resolution = await resolve(make_recipe(swapped))
    assert resolution.values == {"credential_1": SECRET, "credential_2": USER}


async def test_one_credential_can_back_both_fields_with_a_single_lookup(vault):
    same = scope(inputs={
        "credential_1": {"credential_id": CRED_PW, "field": "username"},
        "credential_2": {"credential_id": CRED_PW, "field": "password"},
    })
    resolution = await resolve(make_recipe(same))
    assert resolution.values == {"credential_1": USER, "credential_2": SECRET}
    assert len(vault.calls) == 1


# ── 승인 메타데이터 ─────────────────────────────────────────────────────────


async def test_missing_scope_returns_recovery_with_payload_format_and_no_fallback(vault):
    error = await blocked("credential_scope_missing", recipe=make_recipe(None))
    response = error.to_response()
    assert response["status"] == "credential_reference_blocked"
    assert response["recovery"]["route"] == "human_gateway"
    assert response["recovery"]["auto_fallback"] is False
    assert "credential_scope" in response["recovery"]["payload_format"]["metadata"]
    assert vault.calls == []  # 최근 계정 폴백 없음 — Vault 를 부르지도 않는다


async def test_input_without_binding_is_blocked(vault):
    partial = scope(inputs={"credential_1": {"credential_id": CRED_USER, "field": "username"}})
    error = await blocked("credential_binding_missing", recipe=make_recipe(partial))
    assert error.input_name == "credential_2"
    assert vault.calls == []


@pytest.mark.parametrize("field", ["token", "extra_fields", "totp", "Password", "", None, 7])
async def test_field_outside_allowlist_is_blocked_before_vault(vault, field):
    bad = scope(inputs={
        "credential_1": {"credential_id": CRED_USER, "field": "username"},
        "credential_2": {"credential_id": CRED_PW, "field": field},
    })
    await blocked("credential_field_unsupported", recipe=make_recipe(bad))
    assert vault.calls == []


@pytest.mark.parametrize("broken", [
    scope(version=2),
    scope(inputs={}),
    scope(allowed_domains=[]),
    scope(allowed_recipes=[]),
    scope(allowed_domains=["other.example.com"]),
    scope(allowed_recipes=["another_recipe"]),
    scope(work_key="bad key with spaces"),
    scope(extra_authority=True),
    scope(inputs={"credential_1": {"credential_id": "not-a-uuid", "field": "username"},
                  "credential_2": {"credential_id": CRED_PW, "field": "password"}}),
    scope(inputs={"credential_1": {"credential_id": CRED_USER, "field": "username", "value": "x"},
                  "credential_2": {"credential_id": CRED_PW, "field": "password"}}),
    scope(inputs={"credential_1": {"credential_id": CRED_USER, "field": "username"},
                  "credential_2": {"credential_id": CRED_PW, "field": "password"},
                  "not_a_secret_input": {"credential_id": CRED_PW, "field": "password"}}),
])
async def test_malformed_or_foreign_scope_is_invalid(vault, broken):
    await blocked("credential_scope_invalid", recipe=make_recipe(broken))
    assert vault.calls == []


async def test_scope_work_key_must_match_recipe_work_key(vault):
    await blocked("credential_scope_invalid", recipe=make_recipe(scope(work_key="another:key")))


async def test_secret_default_and_non_fill_use_are_rejected(vault):
    with_default = recipe_dict()
    with_default["inputs"][1]["default"] = "hunter2hunter2"
    await blocked("secret_default_forbidden", recipe=parse_recipe(with_default))

    in_url = recipe_dict()
    in_url["steps"][0]["url"] = "https://smartsms.aligo.in/login?pw={{credential_2}}"
    await blocked("secret_input_unsupported_use", recipe=parse_recipe(in_url))

    partial_value = recipe_dict()
    partial_value["steps"][2]["value"] = "prefix-{{credential_2}}"
    await blocked("secret_input_unsupported_use", recipe=parse_recipe(partial_value))

    on_click = recipe_dict()
    on_click["steps"][3] = {"action": "press", "selector": "#login", "value": "{{credential_2}}", "risk": "READ"}
    await blocked("secret_input_unsupported_use", recipe=parse_recipe(on_click))
    assert vault.calls == []


# ── 호출자 위조 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("supplied", [
    SECRET,
    {"value": SECRET},
    {"value": SECRET, "allowed_domains": [DOMAIN], "allowed_recipes": [RECIPE_NAME]},
])
async def test_caller_plaintext_or_value_bypass_is_rejected_before_vault(vault, supplied):
    error = await blocked("plain_secret_rejected" if not isinstance(supplied, dict) else
                          "caller_authority_rejected", inputs={"credential_2": supplied})
    assert error.input_name == "credential_2"
    assert vault.calls == []


@pytest.mark.parametrize("forged_key", ["allowed_domains", "allowed_recipes", "tenant_id", "value", "work_key"])
async def test_caller_cannot_attach_authority_to_a_reference(vault, forged_key):
    forged = {"credential_id": CRED_PW, "field": "password", forged_key: "x"}
    await blocked("caller_authority_rejected", inputs={"credential_2": forged})
    assert vault.calls == []


async def test_caller_cannot_swap_credential_id_or_field(vault):
    other = "33333333-3333-4333-8333-333333333333"
    vault.rows[other] = vault_row(other, password="other-account-password")
    await blocked("credential_reference_mismatch",
                  inputs={"credential_2": {"credential_id": other, "field": "password"}})
    await blocked("credential_reference_mismatch",
                  inputs={"credential_2": {"credential_id": CRED_PW, "field": "username"}})
    await blocked("credential_reference_invalid", inputs={"credential_2": {"credential_id": CRED_PW}})
    assert vault.calls == []


async def test_tenant_comes_only_from_server_context(vault):
    forged = {"credential_id": CRED_PW, "field": "password", "tenant_id": OTHER_TENANT}
    await blocked("caller_authority_rejected", inputs={"credential_2": forged})
    await blocked("tenant_context_required", tenant_id="")
    assert vault.calls == []


# ── Vault 쪽 거부 ───────────────────────────────────────────────────────────


async def test_unknown_credential_id_is_not_found_without_fallback(vault):
    vault.rows.pop(CRED_PW)
    error = await blocked("credential_not_found")
    assert error.credential_id == CRED_USER or error.credential_id == CRED_PW
    assert [c["id"] for c in vault.calls].count(CRED_PW) == 1  # 다른 계정으로 대체 조회하지 않는다


async def test_cross_tenant_row_is_blocked_even_if_vault_returned_it(vault):
    vault.rows[CRED_PW] = vault_row(CRED_PW, tenant_id=OTHER_TENANT)
    await blocked("credential_not_found")


async def test_inactive_row_is_blocked(vault):
    vault.rows[CRED_PW] = vault_row(CRED_PW, is_active=False)
    await blocked("credential_inactive")


@pytest.mark.parametrize("origin", [
    "https://evil.example.com",
    "https://smartsms.aligo.in.evil.example.com",
    "https://aligo.in",
    "https://evilsmartsms.aligo.in",
])
async def test_wrong_origin_is_blocked(vault, origin):
    vault.rows[CRED_PW] = vault_row(CRED_PW, origin=origin, url=origin)
    await blocked("credential_origin_mismatch")


async def test_missing_origin_is_blocked(vault):
    vault.rows[CRED_PW] = vault_row(CRED_PW, origin="", url="")
    await blocked("credential_origin_missing")


async def test_credential_work_key_must_match_scope_and_session(vault):
    vault.rows[CRED_PW] = vault_row(CRED_PW, work_key="someone:else")
    await blocked("credential_work_key_mismatch")
    vault.rows[CRED_PW] = vault_row(CRED_PW)
    await blocked("work_key_mismatch", browser_work_key="session:other")
    await blocked("work_key_mismatch", browser_work_key=None)


async def test_credential_metadata_can_only_narrow_recipe_and_domain(vault):
    vault.rows[CRED_PW] = vault_row(CRED_PW, metadata={"allowed_recipes": ["another_recipe"]})
    await blocked("credential_recipe_not_allowed")
    vault.rows[CRED_PW] = vault_row(CRED_PW, metadata={"allowed_domains": ["other.example.com"]})
    await blocked("credential_origin_mismatch")
    vault.rows[CRED_PW] = vault_row(CRED_PW, metadata={"allowed_recipes": [RECIPE_NAME],
                                                         "allowed_domains": [DOMAIN]})
    assert (await resolve()).values["credential_2"] == SECRET


@pytest.mark.parametrize("value", ["[복호화 실패]", "", None])
async def test_undecryptable_or_empty_field_is_never_filled(vault, value):
    vault.rows[CRED_PW] = vault_row(CRED_PW, password=value)
    error = await blocked("credential_field_unavailable")
    assert error.field_name == "password"


async def test_vault_failures_become_codes_without_exception_text(vault, monkeypatch, caplog):
    async def unavailable(*args, **kwargs):
        raise credential_vault.VaultUnavailableError(f"down {SECRET}")

    monkeypatch.setattr(credential_vault, "get_credential", unavailable)
    with caplog.at_level(logging.DEBUG):
        await blocked("vault_unavailable")

    async def exploding(*args, **kwargs):
        raise RuntimeError(f"db password={SECRET} user={USER}")

    monkeypatch.setattr(credential_vault, "get_credential", exploding)
    with caplog.at_level(logging.DEBUG):
        error = await blocked("vault_lookup_failed")
    assert error.__cause__ is None and error.__suppress_context__
    assert SECRET not in caplog.text and USER not in caplog.text


async def test_block_and_allow_are_logged_with_id_and_field_only(vault, caplog):
    with caplog.at_level(logging.INFO):
        await resolve()
        vault.rows[CRED_PW] = vault_row(CRED_PW, is_active=False)
        await blocked("credential_inactive")
    assert "vault_reference_allowed" in caplog.text and "vault_reference_blocked" in caplog.text
    assert CRED_PW in caplog.text and "password" in caplog.text
    assert SECRET not in caplog.text and USER not in caplog.text


# ── credential_vault.get_credential(require_active) ────────────────────────


async def test_get_credential_filters_inactive_rows_only_when_asked(monkeypatch):
    seen: list[str] = []

    class Pool:
        async def fetchrow(self, sql, *args):
            seen.append(sql)
            return None

    monkeypatch.setattr(credential_vault, "get_pool", lambda: Pool())
    monkeypatch.setattr(credential_vault, "require_vault_enabled", lambda: None)
    monkeypatch.setattr(credential_vault, "is_standalone_vault", lambda: True)
    await credential_vault.get_credential(CRED_PW, tenant_id=TENANT)
    await credential_vault.get_credential(CRED_PW, tenant_id=TENANT, require_active=True)
    assert "is_active" not in seen[0]
    assert "AND is_active = TRUE" in seen[1] and "tenant_id = $2" in seen[1]


# ── 오케스트레이터: run_directive ───────────────────────────────────────────


def _intent(directive: str, inputs: dict[str, Any]):
    envelope = directive_from_authenticated_context(
        {"tenant": {"id": TENANT}, "membership": {"user_id": "user@example.com"}},
        session_id="session-1", correlation_id="corr-1",
        payload={"directive": directive, "inputs": inputs},
        capabilities=frozenset({"recipe.execute"}),
    )
    return ChannelRouter().route_directive(envelope, capability="recipe.execute")


@pytest.fixture
def run_env(monkeypatch, vault):
    env = SimpleNamespace(played=[], executors=[], recipes=[recipe_dict()])

    async def list_recipes(**kwargs):
        return [{"name": r["name"], "domain": r["domain"], "spec": r} for r in env.recipes]

    async def play(recipe, executor, inputs, **kwargs):
        env.played.append({"recipe": recipe.name, "inputs": dict(inputs), "recorder": kwargs.get("recorder")})
        return SimpleNamespace(to_dict=lambda: {"status": "success"})

    monkeypatch.setattr(orchestrator, "list_recipes", list_recipes)
    monkeypatch.setattr(orchestrator, "play_recipe", play)
    monkeypatch.setattr(orchestrator, "GuardedRunRecorder", lambda **kw: object())
    monkeypatch.setattr(orchestrator, "BrowserRecipeExecutor", lambda **kw: env.executors.append(kw) or object())
    return env


async def _run(inputs: dict[str, Any], **kwargs):
    text = f"{RECIPE_NAME} 실행"
    return await orchestrator.run_directive(
        text, TENANT, triggered_by="user@example.com", action_intent=_intent(text, inputs),
        browser_session_id="session-1", browser_work_key=WORK_KEY, **kwargs,
    )


async def test_run_directive_resolves_on_server_and_marks_secret_fill_steps(run_env):
    result = await _run({})
    assert result.to_dict() == {"status": "success"}
    assert run_env.played[0]["inputs"] == {"credential_1": USER, "credential_2": SECRET}
    assert run_env.executors[0]["secret_fill_steps"] == frozenset({2, 3})


async def test_run_directive_accepts_the_reference_form(run_env):
    refs = {"credential_1": {"credential_id": CRED_USER, "field": "username"},
            "credential_2": {"credential_id": CRED_PW, "field": "password"}}
    await _run(refs)
    assert run_env.played[0]["inputs"]["credential_2"] == SECRET


async def test_run_directive_blocks_forged_scope_before_anything_is_played(run_env, vault):
    forged = {"credential_2": {"value": SECRET, "allowed_domains": ["evil.example.com"],
                               "allowed_recipes": [RECIPE_NAME]}}
    with pytest.raises(vault_reference.VaultReferenceError) as caught:
        await _run(forged)
    assert caught.value.code == "caller_authority_rejected"
    assert run_env.played == [] and run_env.executors == [] and vault.calls == []


async def test_run_directive_blocks_before_play_for_every_vault_refusal(run_env, vault):
    vault.rows[CRED_PW] = vault_row(CRED_PW, tenant_id=OTHER_TENANT)
    with pytest.raises(vault_reference.VaultReferenceError):
        await _run({})
    assert run_env.played == [] and run_env.executors == []


async def test_run_directive_without_scope_asks_for_reregistration_not_plain_value(run_env, vault):
    run_env.recipes = [recipe_dict(None)]
    with pytest.raises(vault_reference.VaultReferenceError) as caught:
        await _run({"credential_2": {"value": SECRET, "allowed_domains": [DOMAIN],
                                     "allowed_recipes": [RECIPE_NAME]}})
    assert caught.value.code == "credential_scope_missing"
    assert run_env.played == [] and vault.calls == []


async def test_legacy_scoped_inputs_refuses_vault_scoped_recipes(vault):
    with pytest.raises(vault_reference.VaultReferenceError) as caught:
        await orchestrator._scoped_inputs(make_recipe(), {"credential_2": {"value": SECRET}})
    assert caught.value.code == "plain_secret_rejected"


async def test_string_scope_recipes_keep_the_existing_contract(run_env, monkeypatch):
    data = recipe_dict("yeoljeong-coupangeats")
    run_env.recipes = [data]
    seen: list[Any] = []

    async def scoped(recipe, inputs):
        seen.append(dict(inputs))
        return {"credential_1": "u", "credential_2": "p"}

    monkeypatch.setattr(orchestrator, "_scoped_inputs", scoped)
    await _run({"credential_1": {"value": "u"}})
    assert seen and "secret_fill_steps" not in run_env.executors[0]
    assert run_env.played[0]["inputs"] == {"credential_1": "u", "credential_2": "p"}


async def test_credential_use_audit_records_ids_and_fields_only(monkeypatch):
    rows: list[tuple] = []

    async def record(run_id, domain, recipe_name, credential_ref):
        rows.append((run_id, domain, recipe_name, credential_ref))

    class Inner:
        async def start_run(self, **kwargs):
            return "run-1"

        async def record_step(self, **kwargs):
            return "inner-record-step"

    monkeypatch.setattr(orchestrator, "record_credential_use", record)
    resolution = vault_reference.VaultResolution(
        {"credential_2": SECRET}, frozenset({"credential_2"}), frozenset({3}),
        [{"input": "credential_2", "credential_id": CRED_PW, "field": "password"}],
    )
    wrapper = orchestrator._VaultUseAuditRecorder(Inner(), make_recipe(), resolution)
    assert await wrapper.start_run(recipe=None, inputs={}) == "run-1"
    assert await wrapper.record_step() == "inner-record-step"
    assert rows == [("run-1", DOMAIN, RECIPE_NAME,
                     {"credential_id": CRED_PW, "field": "password", "decision": "allow"})]
    assert SECRET not in json.dumps(rows)


# ── 채팅 도구: 구조화 복구 오류 ─────────────────────────────────────────────


async def test_chat_tool_returns_structured_recovery_for_blocked_reference(run_env, monkeypatch):
    from app.services import tool_executor as tool_executor_module

    run_env.recipes = [recipe_dict(None)]

    async def tenant(**kwargs):
        return TENANT

    monkeypatch.setattr(tool_executor_module, "resolve_bound_tenant_id", tenant)
    token = tool_executor_module.current_chat_session_id.set("11111111-1111-1111-1111-111111111111")
    try:
        result = await tool_executor_module.ToolExecutor()._smart_browser({
            "action": "run", "directive": f"{RECIPE_NAME} 실행",
            "inputs": {"credential_2": {"value": SECRET}},
            "browser_session_id": "session-1", "browser_work_key": WORK_KEY,
        })
    finally:
        tool_executor_module.current_chat_session_id.reset(token)
    assert result["status"] == "credential_reference_blocked"
    assert result["error"] == "credential_scope_missing"
    assert result["recovery"]["route"] == "human_gateway"
    assert SECRET not in json.dumps(result, ensure_ascii=False)


# ── 실행기: 입력 직전 검증과 누출 방지 ──────────────────────────────────────


class ScreenLocator:
    def __init__(self, page):
        self.page = page

    async def inner_text(self, **kwargs):
        self.page.dom_reads += 1
        return f"화면 {SECRET} {USER}"

    async def aria_snapshot(self):
        self.page.dom_reads += 1
        return f"textbox: {SECRET}"


class ScreenPage:
    """window.location.href 를 순서대로 돌려주는 가짜 페이지. 마지막 값이 계속 유지된다."""

    def __init__(self, hrefs, fill_error: Exception | None = None):
        self.hrefs = list(hrefs)
        self.fill_error = fill_error
        self.fills: list[tuple[str, str]] = []
        self.dom_reads = 0
        self.shots = 0
        self.url = hrefs[0]

    async def evaluate(self, expression, arg=None, **kwargs):
        assert expression == "window.location.href"
        href = self.hrefs.pop(0) if len(self.hrefs) > 1 else self.hrefs[0]
        return href

    async def fill(self, selector, value, **kwargs):
        if self.fill_error is not None:
            raise self.fill_error
        self.fills.append((selector, value))

    def locator(self, selector):
        return ScreenLocator(self)

    async def screenshot(self, **kwargs):
        self.shots += 1
        return b"png"

    async def click(self, selector, **kwargs):
        self.clicks = getattr(self, "clicks", 0) + 1


def make_executor(monkeypatch, page, fill_steps=(2, 3)):
    async def acquire(**kwargs):
        return SimpleNamespace(pages=[page]), None

    monkeypatch.setattr(executor_module, "acquire_browser_context", acquire)
    return executor_module.BrowserRecipeExecutor(secret_fill_steps=fill_steps)


def fill_payload(seq=2, selector="#user_id", value=USER, domain=DOMAIN):
    return {"action": "fill", "seq": seq, "selector": selector, "value": value, "risk": "READ",
            "context": {"domain": domain}}


async def test_fill_happens_only_on_the_exact_https_host(monkeypatch):
    page = ScreenPage([LOGIN_URL])
    result = await make_executor(monkeypatch, page)(fill_payload())
    assert result["ok"] is True
    assert page.fills == [("#user_id", USER)]


@pytest.mark.parametrize("href", [
    "https://evil.example.com/login",
    f"https://{DOMAIN}.evil.example.com/login",
    f"https://evil.example.com@{DOMAIN}.evil.example.com/",
    f"https://user:pw@{DOMAIN}/login",
    f"http://{DOMAIN}/login",
    "about:blank",
    "",
])
async def test_fill_is_blocked_before_typing_when_live_url_is_not_the_recipe_host(monkeypatch, href):
    page = ScreenPage([href])
    result = await make_executor(monkeypatch, page)(fill_payload())
    assert result["ok"] is False and result["status"] == "blocked"
    assert result["recovery"]["reason"] == "secret_fill_origin_mismatch"
    assert page.fills == []
    assert SECRET not in json.dumps(result, ensure_ascii=False) and USER not in json.dumps(result)


async def test_live_url_is_used_not_the_stale_cached_page_url(monkeypatch):
    page = ScreenPage(["https://evil.example.com/after-redirect"])
    page.url = LOGIN_URL  # 퍼사드가 캐시한 옛 URL 은 정상처럼 보인다
    result = await make_executor(monkeypatch, page)(fill_payload())
    assert result["ok"] is False and page.fills == []


async def test_redirect_during_fill_clears_the_field_and_blocks(monkeypatch):
    page = ScreenPage([LOGIN_URL, "https://evil.example.com/landing"])
    result = await make_executor(monkeypatch, page)(fill_payload(seq=3, selector="#user_pw", value=SECRET))
    assert result["ok"] is False and result["recovery"]["reason"] == "secret_fill_origin_changed"
    assert page.fills[-1] == ("#user_pw", "")
    assert SECRET not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize("selector", [
    "iframe >> internal:control=enter-frame >> #user_pw", "#a >> internal:control=enter-frame >> #b",
    "#a >> iframe >> #b", "",
])
async def test_secret_fill_refuses_frame_traversal_selectors(monkeypatch, selector):
    page = ScreenPage([LOGIN_URL])
    result = await make_executor(monkeypatch, page)(fill_payload(selector=selector))
    assert result["ok"] is False and page.fills == []
    assert result["recovery"]["reason"] == "secret_fill_selector_not_allowed"


async def test_fill_into_a_different_tab_is_blocked(monkeypatch):
    page = ScreenPage([LOGIN_URL])
    other = ScreenPage([LOGIN_URL])
    ex = make_executor(monkeypatch, page)
    await ex({"action": "snapshot", "risk": "READ", "context": {"domain": DOMAIN}})  # 첫 페이지를 고정
    ex._context = SimpleNamespace(pages=[other])
    result = await ex(fill_payload())
    assert result["ok"] is False and result["recovery"]["reason"] == "secret_fill_tab_changed"
    assert page.fills == [] and other.fills == []


async def test_fill_failure_never_carries_the_value(monkeypatch):
    page = ScreenPage([LOGIN_URL], fill_error=RuntimeError(f"Page.fill failed for value={SECRET}"))
    result = await make_executor(monkeypatch, page)(fill_payload(seq=3, selector="#user_pw", value=SECRET))
    assert result["ok"] is False
    assert "secret_fill_failed:RuntimeError" in result["error"]
    assert SECRET not in json.dumps(result, ensure_ascii=False, default=str)


async def test_evidence_is_withheld_while_the_secret_is_on_screen(monkeypatch):
    page = ScreenPage([LOGIN_URL])
    ex = make_executor(monkeypatch, page)
    result = await ex(fill_payload())
    assert result["ok"] is True
    evidence = result["evidence"]
    assert evidence["dom"]["status"] == "withheld" and evidence["screenshot"]["status"] == "withheld"
    assert page.dom_reads == 0 and page.shots == 0
    assert SECRET not in json.dumps(result, ensure_ascii=False) and USER not in json.dumps(result)
    # 같은 화면에 머무르는 다음 단계도 증거를 남기지 않는다.
    later = await ex({"action": "click", "seq": 4, "selector": "#login", "risk": "READ",
                      "context": {"domain": DOMAIN}})
    assert later["evidence"]["dom"]["status"] == "withheld" and page.shots == 0
    # 화면이 바뀌면(로그인 후 이동) 증거가 다시 수집된다.
    page.hrefs = [f"https://{DOMAIN}/mfa"]
    moved = await ex({"action": "snapshot", "seq": 5, "risk": "READ", "context": {"domain": DOMAIN}})
    assert "text" in moved["evidence"]["dom"] and page.shots == 1


async def test_non_secret_fill_keeps_the_previous_behaviour(monkeypatch):
    page = ScreenPage(["https://evil.example.com/anything"])
    ex = make_executor(monkeypatch, page)
    result = await ex(fill_payload(seq=9, selector="#memo", value="plain-note"))
    assert result["ok"] is True and page.fills == [("#memo", "plain-note")]
    assert "text" in result["evidence"]["dom"]


# ── 플레이어: 결과·기록 어디에도 비밀이 없다 ────────────────────────────────


class CapturingRecorder:
    def __init__(self):
        self.runs: list[dict] = []
        self.steps: list[Any] = []
        self.finished: list[Any] = []

    def start_run(self, *, recipe, inputs, recipe_id=None):
        self.runs.append({"inputs": dict(inputs)})
        return "run-1"

    def record_step(self, *, run_id, step):
        self.steps.append(step.to_dict())

    def finish_run(self, *, run_id, result):
        self.finished.append(result.to_dict())


async def test_player_run_leaks_no_secret_into_result_or_records(monkeypatch):
    class ExplodingSecretPage(ScreenPage):
        async def click(self, selector, **kwargs):
            raise RuntimeError(f"click failed; page said {SECRET} for {USER}")

    page = ExplodingSecretPage([LOGIN_URL])
    ex = make_executor(monkeypatch, page, fill_steps=(2, 3))
    recorder = CapturingRecorder()
    recipe = make_recipe()
    result = await RecipePlayer(ex, recorder=recorder, retries=1).play(
        recipe, {"credential_1": USER, "credential_2": SECRET}, context={"domain": DOMAIN})
    # navigate 는 가짜 페이지가 goto 를 모르므로 실패한다 — 실패 경로에서도 누출이 없어야 한다.
    blob = json.dumps([result.to_dict(), recorder.runs, recorder.steps, recorder.finished,
                       result.variables], ensure_ascii=False, default=str)
    assert SECRET not in blob and USER not in blob
    assert recorder.runs[0]["inputs"] == {"credential_1": "***", "credential_2": "***"}
    assert result.variables == {"credential_1": "***", "credential_2": "***"}


async def test_player_redacts_secret_echoed_by_a_step_error():
    recipe = make_recipe()

    async def echoing(payload):
        return {"ok": False, "error": f"typed {payload.get('value')} and failed", "llm_calls": 0}

    result = await RecipePlayer(echoing, retries=0).play(
        recipe, {"credential_1": USER, "credential_2": SECRET}, context={"domain": DOMAIN})
    blob = json.dumps(result.to_dict(), ensure_ascii=False)
    assert SECRET not in blob and USER not in blob
    assert result.status == "failed"


# ── 등록: 잘못된 credential_scope 초안은 저장 전에 거부 ─────────────────────


async def test_registration_rejects_malformed_scope_before_any_db_write(monkeypatch):
    from app.services.work_recipe import registration

    async def no_coupang(recipe, tenant_id):
        return None

    def no_pool():
        raise AssertionError("DB 에 접근하면 안 된다")

    monkeypatch.setattr(registration, "_validate_coupangeats_registration", no_coupang)
    monkeypatch.setattr(registration, "get_pool", no_pool)
    bad = make_recipe(scope(inputs={"credential_1": {"credential_id": CRED_USER, "field": "token"},
                                    "credential_2": {"credential_id": CRED_PW, "field": "password"}}))
    with pytest.raises(vault_reference.VaultReferenceError) as caught:
        await registration.request_registration(bad, tenant_id=TENANT, requested_by="t")
    assert caught.value.code == "credential_field_unsupported"

    in_url = recipe_dict()
    in_url["steps"][0]["url"] = "https://smartsms.aligo.in/login?pw={{credential_2}}"
    with pytest.raises(vault_reference.VaultReferenceError) as caught:
        await registration.request_registration(parse_recipe(in_url), tenant_id=TENANT, requested_by="t")
    assert caught.value.code == "secret_input_unsupported_use"
