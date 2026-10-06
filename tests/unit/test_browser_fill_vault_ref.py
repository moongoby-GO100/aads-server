"""browser_fill Vault 참조 입력 + 비밀번호 평문 거부 + tools_called 저장 마스킹
(AADS-BROWSER-FILL-VAULT-REF-20261006-R2).

지키는 것.
  1. {{vault:password}} → 복호화 값으로 page.fill, 반환 문자열에 값 없음.
  2. Vault 항목이 없으면 vault_credential_not_found, fill 미호출.
  3. Vault 항목이 있는 origin 의 비밀번호 칸 평문은 거부, fill 미호출.
  4. Vault 항목이 없는 origin 의 비밀번호 칸 평문은 허용(첫 로그인), autosave.on_fill 호출.
  5. 아이디 칸 평문은 허용.
  6. credential_id 의 tenant 불일치는 거부.
  7. tools_called 저장 시 비밀번호 value·credential_register password 는 ***MASKED***.
"""
from __future__ import annotations

import json

import pytest

from app.services import agent_vault_service as svc
from app.services import browser_login_autosave as als

TENANT = "0f9d4f2a-1111-4222-8333-444455556666"
OTHER_TENANT = "9a9a9a9a-1111-4222-8333-444455556666"
SESSION = "5090a247-47f7-4a05-9a1e-2f0b6c1d8e30"
ORIGIN = "https://shop.example.com"
CRED_ID = "11111111-2222-4333-8444-555555555555"
SECRET = "Zx9!hunter2-SECRET-pw"
USER = "moongo@example.com"


class _Page:
    def __init__(self, url=f"{ORIGIN}/login", attrs=None, fail_with=None):
        self.url = url
        self._attrs = attrs or {}
        self.fills: list[tuple[str, str]] = []
        self._fail_with = fail_with

    async def eval_on_selector(self, selector, script):
        return self._attrs.get(selector, {})

    async def fill(self, selector, value, timeout=None):
        if self._fail_with:
            raise RuntimeError(self._fail_with.format(value=value))
        self.fills.append((selector, value))


@pytest.fixture
def env(monkeypatch):
    from app.api import ceo_chat_tools as tools

    als.clear_all()
    state = {
        "page": _Page(attrs={
            "#pw": {"type": "password", "name": "x"},
            "#id": {"type": "email", "name": "email"},
        }),
        "cred": None,
        "listed": [],
        "used": [],
        "on_fill": [],
        "by_id_calls": [],
        "for_url_calls": [],
    }

    class _Ctx:
        pages = [state["page"]]

    async def acquire(*a, **kw):
        return _Ctx(), None

    async def for_url(**kw):
        state["for_url_calls"].append(kw)
        return state["cred"]

    async def by_id(*, tenant_id, credential_id):
        state["by_id_calls"].append((tenant_id, credential_id))
        cred = state["cred"]
        return cred if cred and cred["tenant_id"] == tenant_id and cred["id"] == credential_id else None

    async def list_active(**kw):
        return state["listed"]

    async def mark_used(**kw):
        state["used"].append(kw)
        return True

    async def on_fill(page, selector, value, **kw):
        state["on_fill"].append((selector, value, kw))

    monkeypatch.setattr(tools, "_acquire_pw_context", acquire)
    monkeypatch.setattr(svc, "get_agent_credential_for_url", for_url)
    monkeypatch.setattr(svc, "get_agent_credential_by_id", by_id)
    monkeypatch.setattr(svc, "list_agent_credentials", list_active)
    monkeypatch.setattr(svc, "mark_agent_credential_used", mark_used)
    monkeypatch.setattr(als, "on_fill", on_fill)
    state["tools"] = tools
    yield state
    als.clear_all()


def _cred(**over):
    base = {
        "id": CRED_ID, "tenant_id": TENANT, "origin": ORIGIN, "work_key": "wk",
        "username": USER, "password": SECRET,
    }
    base.update(over)
    return base


async def _fill(env, selector, value, **kw):
    return await env["tools"].tool_browser_fill(
        selector, value, tenant_id=kw.pop("tenant_id", TENANT),
        browser_work_key=SESSION, **kw,
    )


async def test_vault_password_ref_fills_decrypted_value_without_echo(env):
    env["cred"] = _cred()
    out = await _fill(env, "#pw", "{{vault:password}}")
    assert env["page"].fills == [("#pw", SECRET)]
    assert out == f"[입력 완료] selector=#pw source=vault credential_id={CRED_ID}"
    assert SECRET not in out
    assert env["for_url_calls"][0]["tenant_id"] == TENANT
    assert env["for_url_calls"][0]["url"] == ORIGIN
    assert env["for_url_calls"][0]["work_key"] == SESSION
    assert env["used"] and env["used"][0]["credential_id"] == CRED_ID
    assert SECRET not in json.dumps(env["used"], default=str)


async def test_vault_ref_marks_autosave_as_vault_fill(env):
    env["cred"] = _cred()
    await _fill(env, "#pw", "{{vault:password}}")
    assert len(env["on_fill"]) == 1
    _selector, value, kw = env["on_fill"][0]
    assert kw.get("from_vault") is True
    assert SECRET not in value


async def test_vault_username_ref_fills_username(env):
    env["cred"] = _cred()
    out = await _fill(env, "#id", "{{vault:username}}")
    assert env["page"].fills == [("#id", USER)]
    assert "source=vault" in out


async def test_vault_ref_without_entry_is_not_found_and_does_not_fill(env):
    env["cred"] = None
    out = await _fill(env, "#pw", "{{vault:password}}")
    assert out == f"[ERROR] vault_credential_not_found origin={ORIGIN}"
    assert env["page"].fills == []
    assert env["on_fill"] == []


async def test_vault_ref_does_not_fall_back_to_other_origin(env):
    env["page"].url = "https://other.example.org/login"
    env["cred"] = None
    out = await _fill(env, "#pw", "{{vault:password}}")
    assert "vault_credential_not_found origin=https://other.example.org" in out
    assert env["for_url_calls"][0]["url"] == "https://other.example.org"
    assert env["page"].fills == []


async def test_plaintext_password_blocked_when_vault_has_entry(env):
    env["listed"] = [{"id": CRED_ID}]
    out = await _fill(env, "#pw", SECRET)
    assert out.startswith("[ERROR] plaintext_password_blocked")
    assert '{{vault:password}}' in out
    assert SECRET not in out
    assert env["page"].fills == []
    assert env["on_fill"] == []


async def test_plaintext_password_blocked_by_selector_hint_for_pc_pages(env):
    env["listed"] = [{"id": CRED_ID}]
    for selector in ("input[type=password]", "input[name='current-password']", "#pw", "input#비밀번호"):
        env["page"]._attrs = {}
        out = await _fill(env, selector, SECRET)
        assert out.startswith("[ERROR] plaintext_password_blocked"), selector
    assert env["page"].fills == []


async def test_plaintext_password_allowed_without_vault_entry_and_autosave_called(env):
    env["listed"] = []
    out = await _fill(env, "#pw", SECRET)
    assert out == "[입력 완료] selector=#pw"
    assert env["page"].fills == [("#pw", SECRET)]
    assert len(env["on_fill"]) == 1
    selector, value, kw = env["on_fill"][0]
    assert (selector, value) == ("#pw", SECRET)
    assert not kw.get("from_vault")


async def test_plaintext_username_allowed_even_with_vault_entry(env):
    env["listed"] = [{"id": CRED_ID}]
    out = await _fill(env, "#id", USER)
    assert out == "[입력 완료] selector=#id"
    assert env["page"].fills == [("#id", USER)]


async def test_credential_id_of_other_tenant_is_rejected(env):
    env["cred"] = _cred(tenant_id=OTHER_TENANT)
    out = await _fill(env, "#pw", "{{vault:password}}", credential_id=CRED_ID)
    assert out.startswith("[ERROR] vault_credential_not_found")
    assert env["by_id_calls"] == [(TENANT, CRED_ID)]
    assert env["for_url_calls"] == []
    assert env["page"].fills == []
    assert SECRET not in out


async def test_credential_id_scopes_to_that_entry(env):
    env["cred"] = _cred()
    out = await _fill(env, "#pw", "{{vault:password}}", credential_id=CRED_ID)
    assert env["by_id_calls"] == [(TENANT, CRED_ID)]
    assert env["for_url_calls"] == []
    assert env["page"].fills == [("#pw", SECRET)]
    assert f"credential_id={CRED_ID}" in out


async def test_credential_id_of_other_origin_is_rejected(env):
    env["cred"] = _cred(origin="https://elsewhere.example.org")
    out = await _fill(env, "#pw", "{{vault:password}}", credential_id=CRED_ID)
    assert out.startswith("[ERROR] vault_credential_origin_mismatch")
    assert env["page"].fills == []


async def test_invalid_credential_id_is_rejected(env):
    out = await _fill(env, "#pw", "{{vault:password}}", credential_id="not-a-uuid")
    assert out == "[ERROR] vault_invalid_credential_id"
    assert env["page"].fills == []


async def test_missing_tenant_rejects_vault_ref(env):
    env["cred"] = _cred()
    out = await _fill(env, "#pw", "{{vault:password}}", tenant_id="")
    assert out == "[ERROR] vault_tenant_required"
    assert env["page"].fills == []


async def test_fill_exception_message_does_not_leak_secret(env):
    env["cred"] = _cred()
    env["page"]._fail_with = "boom while typing {value}"
    out = await _fill(env, "#pw", "{{vault:password}}")
    assert out.startswith("[ERROR] 입력 실패")
    assert SECRET not in out


# ── 저장 시 마스킹 ──────────────────────────────────────────────────

def _stored(tool_name, tool_input):
    from app.services.chat_service import normalize_tool_events

    events = normalize_tool_events([
        {"type": "tool_use", "tool_name": tool_name, "tool_use_id": "t1", "tool_input": tool_input},
    ])
    return events[0]["tool_input"], json.dumps(events)


def test_tools_called_masks_browser_fill_password_value():
    ti, raw = _stored("browser_fill", {"selector": "input[type=password]", "value": SECRET})
    assert ti["value"] == "***MASKED***"
    assert ti["selector"] == "input[type=password]"
    assert SECRET not in raw


def test_tools_called_keeps_username_value_and_vault_refs():
    ti, _ = _stored("browser_fill", {"selector": "#email", "value": USER})
    assert ti["value"] == USER
    ti, _ = _stored("browser_fill", {"selector": "#pw", "value": "{{vault:password}}"})
    assert ti["value"] == "{{vault:password}}"


def test_tools_called_masks_credential_register_secrets():
    ti, raw = _stored("credential_register", {
        "service": "shop", "username": USER, "password": SECRET,
        "extra_fields": {"pin": "1234", "memo": "x"},
        "login_steps": [
            {"action": "fill", "selector": "#email", "value": USER},
            {"action": "fill", "selector": "input[type=password]", "value": SECRET},
        ],
    })
    assert ti["password"] == "***MASKED***"
    assert ti["extra_fields"] == {"pin": "***MASKED***", "memo": "***MASKED***"}
    assert ti["username"] == USER
    assert ti["login_steps"][0]["value"] == USER
    assert ti["login_steps"][1]["value"] == "***MASKED***"
    assert SECRET not in raw and "1234" not in raw


def test_tools_called_masks_secret_named_keys_but_not_max_tokens():
    ti, raw = _stored("some_tool", {
        "passwd": "a1", "client_secret": "s3cr3t", "access_token": "tok-abc",
        "max_tokens": 100, "query": "hello",
    })
    assert ti["passwd"] == ti["client_secret"] == ti["access_token"] == "***MASKED***"
    assert ti["max_tokens"] == 100 and ti["query"] == "hello"
    assert "tok-abc" not in raw


def test_tool_use_event_snapshot_is_masked():
    from app.services.chat_service import _tool_use_event_snapshot

    snap = _tool_use_event_snapshot({
        "tool_name": "browser_fill", "tool_use_id": "t1",
        "tool_input": {"selector": "input[name=password]", "value": SECRET},
    })
    assert snap["tool_input"]["value"] == "***MASKED***"


def test_is_password_field_hints():
    for selector in ("input[type=password]", "#pw", "input[name=user_pw]", "#loginPw", "#pwd", "input#비밀번호"):
        assert als.is_password_field(selector), selector
    for selector in ("#email", "input[name=username]", "#pwa-banner", "#flow"):
        assert not als.is_password_field(selector), selector
    assert als.is_password_field("#x", {"type": "password"})
    assert als.is_password_field("#x", {"autocomplete": "current-password"})
