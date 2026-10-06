"""스마트브라우저 로그인 입력 → Agent Vault 저장 제안 (AADS-VAULT-BROWSER-LOGIN-AUTOSAVE-20261006).

지키는 것 여섯 가지.

  1. 슬롯은 TTL(10분)이 지나면 사라진다.
  2. 로그인 실패면 제안이 없다.
  3. Vault 에 같은 (origin, username, password) 가 있으면 제안이 없다.
  4. 승인하면 기존 저장 함수로 저장하고 슬롯이 폐기된다. 거절·만료도 슬롯 폐기.
  5. Vault 자동입력 경로(browser_fill 을 거치지 않는 값)는 슬롯이 생기지 않는다.
  6. 비밀번호 원문은 카드 payload·로그·tool 결과·예외 어디에도 없다.

DB 는 붙지 않는다. 가짜 풀로 질의 인자를 본다.
"""
from __future__ import annotations

import json
import logging

import pytest

from app.services import browser_login_autosave as als

TENANT = "0f9d4f2a-1111-4222-8333-444455556666"
SESSION = "5090a247-47f7-4a05-9a1e-2f0b6c1d8e30"
ORIGIN = "https://shop.example.com"
LOGIN_URL = f"{ORIGIN}/login"
USER = "moongo@example.com"
SECRET = "Zx9!hunter2-SECRET-pw"
SECRET2 = "Another!Secret#4711"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    als.clear_all()
    now = {"t": 1000.0}
    monkeypatch.setattr(als, "_clock", lambda: now["t"])
    als._test_now = now  # type: ignore[attr-defined]
    monkeypatch.setattr(als, "_SETTLE_DELAY_SECONDS", 0)
    yield
    als.clear_all()


class _Page:
    def __init__(self, url=LOGIN_URL, attrs=None):
        self.url = url
        self._attrs = attrs or {}

    async def eval_on_selector(self, selector, script):
        return self._attrs.get(selector, {})


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _Conn:
    def __init__(self):
        self.pending: dict[str, dict] = {}
        self.calls: list[tuple] = []

    async def fetchval(self, query, *args):
        self.calls.append((query, args))
        if "INSERT INTO agent_permission_requests" in query:
            rid = f"card-{len(self.pending) + 1}"
            self.pending[args[1]] = {"id": rid, "args": args}
            return rid
        if "SELECT id::text FROM agent_permission_requests" in query:
            card = self.pending.get(args[1])
            return card["id"] if card else None
        return None

    async def execute(self, query, *args):
        self.calls.append((query, args))


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)

    async def execute(self, query, *args):
        self.conn.calls.append((query, args))


@pytest.fixture
def db(monkeypatch):
    conn = _Conn()
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: _Pool(conn))
    return conn


@pytest.fixture
def vault(monkeypatch):
    state = {"existing": None, "upserts": [], "updates": []}

    async def list_active(**kw):
        return []

    async def find(**kw):
        return state["existing"]

    async def upsert(**kw):
        state["upserts"].append(kw)
        return {"id": "cred-new"}

    async def update(**kw):
        state["updates"].append(kw)
        return {"id": kw["credential_id"]}

    import app.services.agent_vault_service as svc

    monkeypatch.setattr(svc, "list_agent_credentials", list_active)
    monkeypatch.setattr(svc, "find_agent_credential_by_username", find)
    monkeypatch.setattr(svc, "upsert_agent_credential", upsert)
    monkeypatch.setattr(svc, "update_agent_credential", update)
    return state


def _fill_login(password=SECRET, user=USER):
    als.record_fill(tenant_id=TENANT, session_id=SESSION, page_url=LOGIN_URL,
                    selector="input[name=email]", value=user,
                    attrs={"type": "email", "name": "email"})
    als.record_fill(tenant_id=TENANT, session_id=SESSION, page_url=LOGIN_URL,
                    selector="input[name=pw]", value=password,
                    attrs={"type": "password", "name": "pw"})


def _login_result(monkeypatch, ok: bool):
    async def completed(page, login_url):
        return ok

    monkeypatch.setattr(als, "_wait_login_completed", completed)


def _assert_no_secret(obj):
    text = obj if isinstance(obj, str) else json.dumps(obj, default=str, ensure_ascii=False)
    assert SECRET not in text and SECRET2 not in text


# ── 분류 / 슬롯 ──────────────────────────────────────────────────────

def test_classify_password_by_type_name_autocomplete_and_selector():
    assert als.classify_field("#x", {"type": "password"}) == "password"
    assert als.classify_field("#x", {"type": "text", "name": "user_password"}) == "password"
    assert als.classify_field("#x", {"type": "text", "autocomplete": "current-password"}) == "password"
    assert als.classify_field("input[type=password]") == "password"
    assert als.classify_field("#x", {"type": "email"}) == "username"
    assert als.classify_field("input[name=username]") == "username"
    assert als.classify_field("#search-box", {"type": "search"}) == ""


def test_slot_holds_username_and_password_for_same_origin():
    _fill_login()
    assert als.has_pending_slot(TENANT, SESSION, ORIGIN)
    assert not als.has_pending_slot(TENANT, "other-session", ORIGIN)
    assert not als.has_pending_slot("other-tenant", SESSION, ORIGIN)
    assert not als.has_pending_slot(TENANT, SESSION, "https://other.example.com")


def test_slot_ttl_expiry():
    _fill_login()
    als._test_now["t"] += als.SLOT_TTL_SECONDS - 1
    assert als.has_pending_slot(TENANT, SESSION, ORIGIN)
    als._test_now["t"] += 2
    assert not als.has_pending_slot(TENANT, SESSION, ORIGIN)
    assert als._slots == {}


def test_slot_repr_hides_secret():
    _fill_login()
    slot = next(iter(als._slots.values()))
    _assert_no_secret(repr(slot))
    assert USER not in repr(slot)


# ── 제안 ─────────────────────────────────────────────────────────────

async def test_login_failure_creates_no_card(monkeypatch, db, vault):
    _fill_login()
    _login_result(monkeypatch, False)
    note = await als.on_submit(_Page(), ORIGIN, tenant_id=TENANT, fallback_session=SESSION)
    assert note == ""
    assert db.calls == []
    assert als.has_pending_slot(TENANT, SESSION, ORIGIN)


async def test_success_creates_card_without_password(monkeypatch, db, vault, caplog):
    caplog.set_level(logging.DEBUG)
    _fill_login()
    _login_result(monkeypatch, True)
    note = await als.on_submit(_Page(), ORIGIN, tenant_id=TENANT, fallback_session=SESSION)
    assert "승인 카드" in note
    _assert_no_secret(note)
    inserts = [c for c in db.calls if "INSERT INTO agent_permission_requests" in c[0]]
    assert len(inserts) == 1
    args = inserts[0][1]
    assert als.ACTION_TYPE in args and als.GATE_SOURCE in args
    _assert_no_secret(list(args))
    scope = json.loads(args[6])
    assert set(scope) == {"scope", "slot_id", "origin", "host", "username_masked", "mode"}
    assert scope["origin"] == ORIGIN and scope["host"] == "shop.example.com"
    assert scope["username_masked"] == "mo***@example.com"
    assert USER not in json.dumps(args[4:6], ensure_ascii=False)
    assert scope["mode"] == "new"
    _assert_no_secret(caplog.text)
    assert USER not in caplog.text


async def test_duplicate_account_creates_no_card(monkeypatch, db, vault):
    vault["existing"] = {"id": "cred-1", "password": SECRET, "work_key": "w", "label": "l"}
    _fill_login()
    _login_result(monkeypatch, True)
    note = await als.on_submit(_Page(), ORIGIN, tenant_id=TENANT, fallback_session=SESSION)
    assert note == ""
    assert not [c for c in db.calls if "INSERT" in c[0]]
    assert not als.has_pending_slot(TENANT, SESSION, ORIGIN)


async def test_changed_password_proposes_update(monkeypatch, db, vault):
    vault["existing"] = {"id": "cred-1", "password": "old-password", "work_key": "w", "label": "l"}
    _fill_login()
    _login_result(monkeypatch, True)
    await als.on_submit(_Page(), ORIGIN, tenant_id=TENANT, fallback_session=SESSION)
    insert = next(c for c in db.calls if "INSERT INTO agent_permission_requests" in c[0])
    assert json.loads(insert[1][6])["mode"] == "update"
    assert "업데이트" in insert[1][4]


async def test_second_submit_reuses_pending_card(monkeypatch, db, vault):
    _fill_login()
    _login_result(monkeypatch, True)
    await als.on_submit(_Page(), ORIGIN, tenant_id=TENANT, fallback_session=SESSION)
    # 같은 계정으로 다시 로그인 → 새 슬롯, 카드는 하나
    _fill_login()
    await als.on_submit(_Page(), ORIGIN, tenant_id=TENANT, fallback_session=SESSION)
    assert len([c for c in db.calls if "INSERT INTO agent_permission_requests" in c[0]]) == 1
    assert any("UPDATE agent_permission_requests" in c[0] for c in db.calls)


async def test_vault_autofill_values_never_enter_slot(monkeypatch, db, vault):
    # Vault 자동입력은 browser_fill 을 거치지 않으므로 record_fill 이 불리지 않는다.
    # 슬롯이 없으면 제출이 성공해도 카드가 없다.
    _login_result(monkeypatch, True)
    note = await als.on_submit(_Page(), ORIGIN, tenant_id=TENANT, fallback_session=SESSION)
    assert note == ""
    assert db.calls == []


# ── 승인 / 거절 ──────────────────────────────────────────────────────

def _slot_id():
    return next(iter(als._slots.values())).id


async def test_approve_saves_via_vault_service_and_discards_slot(db, vault):
    _fill_login()
    sid = _slot_id()
    result = await als.apply_decision(
        slot_id=sid, approved=True, tenant_id=TENANT, decided_by="user-1", request_id="card-1",
    )
    assert result["status"] == "saved"
    assert len(vault["upserts"]) == 1
    saved = vault["upserts"][0]
    assert saved["password"] == SECRET and saved["username"] == USER
    assert saved["origin"] == ORIGIN
    assert saved["metadata"] == {
        "source": "browser_autosave", "policy": "ask", "verification_status": "verified",
    }
    assert als._slots == {}
    _assert_no_secret(result)
    consumed = [c for c in db.calls if "'{\"used\": 1}'" in c[0]]
    assert consumed and consumed[0][1] == ("card-1",)


async def test_approve_update_existing_credential(db, vault):
    vault["existing"] = {
        "id": "cred-1", "password": "old", "work_key": "wk", "label": "lbl",
        "metadata": {"policy": "allow", "note": "keep"},
    }
    _fill_login()
    result = await als.apply_decision(
        slot_id=_slot_id(), approved=True, tenant_id=TENANT, decided_by="user-1",
    )
    assert result["status"] == "updated"
    upd = vault["updates"][0]
    assert upd["credential_id"] == "cred-1" and upd["password"] == SECRET
    assert upd["work_key"] == "wk" and upd["label"] == "lbl"
    assert upd["metadata"]["note"] == "keep"
    assert upd["metadata"]["source"] == "browser_autosave"
    assert vault["upserts"] == []


async def test_reject_discards_slot_without_saving(vault):
    _fill_login()
    result = await als.apply_decision(
        slot_id=_slot_id(), approved=False, tenant_id=TENANT, decided_by="user-1",
    )
    assert result["status"] == "discarded"
    assert vault["upserts"] == [] and vault["updates"] == []
    assert als._slots == {}


async def test_approve_after_expiry_saves_nothing(vault):
    _fill_login()
    sid = _slot_id()
    als._test_now["t"] += als.SLOT_TTL_SECONDS + 1
    result = await als.apply_decision(
        slot_id=sid, approved=True, tenant_id=TENANT, decided_by="user-1",
    )
    assert result["status"] == "expired"
    assert vault["upserts"] == []


async def test_approve_with_other_tenant_is_refused_and_keeps_slot(vault):
    _fill_login()
    result = await als.apply_decision(
        slot_id=_slot_id(), approved=True, tenant_id="other-tenant", decided_by="u",
    )
    assert result["status"] == "error"
    assert vault["upserts"] == []
    assert len(als._slots) == 1


async def test_new_password_invalidates_old_card_slot(vault):
    _fill_login(password=SECRET)
    old = _slot_id()
    _fill_login(password=SECRET2)
    result = await als.apply_decision(
        slot_id=old, approved=True, tenant_id=TENANT, decided_by="u",
    )
    assert result["status"] == "expired"
    assert vault["upserts"] == []


async def test_save_failure_message_has_no_password(monkeypatch, vault, caplog):
    import app.services.agent_vault_service as svc

    async def boom(**kw):
        raise RuntimeError(f"insert failed for {kw['password']}")

    monkeypatch.setattr(svc, "upsert_agent_credential", boom)
    caplog.set_level(logging.DEBUG)
    _fill_login()
    result = await als.apply_decision(
        slot_id=_slot_id(), approved=True, tenant_id=TENANT, decided_by="u",
    )
    assert result["status"] == "error"
    _assert_no_secret(result)
    _assert_no_secret(caplog.text)
    assert als._slots == {}


# ── 도구 연동 ────────────────────────────────────────────────────────

async def test_on_fill_swallows_errors_and_records_slot(monkeypatch):
    page = _Page(attrs={
        "#u": {"type": "text", "name": "username"},
        "#p": {"type": "password", "name": "x"},
    })
    await als.on_fill(page, "#u", USER, tenant_id=TENANT, fallback_session=SESSION)
    await als.on_fill(page, "#p", SECRET, tenant_id=TENANT, fallback_session=SESSION)
    assert als.has_pending_slot(TENANT, SESSION, ORIGIN)

    class _Broken:
        url = LOGIN_URL

        async def eval_on_selector(self, *a):
            raise RuntimeError(SECRET)

    als.clear_all()
    await als.on_fill(_Broken(), "input[type=password]", SECRET,
                      tenant_id=TENANT, fallback_session=SESSION)


async def test_browser_tools_do_not_echo_value_and_trigger_submit_hook(monkeypatch, db, vault):
    from app.api import ceo_chat_tools as tools

    page = _Page(attrs={
        "#u": {"type": "email", "name": "email"},
        "#p": {"type": "password", "name": "pw"},
    })

    async def _fill(selector, value, timeout=None):
        return None

    async def _click(selector, timeout=None):
        page.url = f"{ORIGIN}/dashboard"

    page.fill = _fill
    page.click = _click

    class _Ctx:
        pages = [page]

    async def acquire(*a, **kw):
        return _Ctx(), None

    monkeypatch.setattr(tools, "_acquire_pw_context", acquire)
    _login_result(monkeypatch, True)

    r1 = await tools.tool_browser_fill("#u", USER, tenant_id=TENANT, browser_work_key=SESSION)
    r2 = await tools.tool_browser_fill("#p", SECRET, tenant_id=TENANT, browser_work_key=SESSION)
    _assert_no_secret(r1)
    _assert_no_secret(r2)
    r3 = await tools.tool_browser_click("#submit", tenant_id=TENANT, browser_work_key=SESSION)
    assert "승인 카드" in r3
    _assert_no_secret(r3)
    assert len([c for c in db.calls if "INSERT INTO agent_permission_requests" in c[0]]) == 1


async def test_non_submit_key_does_not_trigger(monkeypatch, db, vault):
    from app.api import ceo_chat_tools as tools

    page = _Page(attrs={"#p": {"type": "password"}, "#u": {"type": "email"}})

    async def _noop(*a, **kw):
        return None

    page.fill = _noop

    class _Kb:
        press = staticmethod(_noop)

    page.keyboard = _Kb()

    class _Ctx:
        pages = [page]

    async def acquire(*a, **kw):
        return _Ctx(), None

    monkeypatch.setattr(tools, "_acquire_pw_context", acquire)
    _login_result(monkeypatch, True)
    await tools.tool_browser_fill("#u", USER, tenant_id=TENANT, browser_work_key=SESSION)
    await tools.tool_browser_fill("#p", SECRET, tenant_id=TENANT, browser_work_key=SESSION)
    out = await tools.tool_browser_press_key("Tab", tenant_id=TENANT, browser_work_key=SESSION)
    assert "승인 카드" not in out and db.calls == []
    out = await tools.tool_browser_press_key("Enter", tenant_id=TENANT, browser_work_key=SESSION)
    assert "승인 카드" in out
    _assert_no_secret(out)


def test_mask_username():
    assert als.mask_username("moongo@example.com") == "mo***@example.com"
    assert als.mask_username("ab@x.com") == "a***@x.com"
    assert als.mask_username("admin") == "ad***"
    assert als.mask_username("") == "***"


def test_decide_handler_wiring_keeps_slot_reference_and_action_in_sync():
    import inspect

    from app.api import project_docs as pd

    assert pd._BROWSER_LOGIN_SAVE_ACTION == als.ACTION_TYPE
    src = inspect.getsource(pd.approvals_decide)
    assert f"'{als.ACTION_TYPE}'" in src and "a.approval_scope->>'slot_id'" in src
    assert "a.tenant_id::text AS tenant_id" in src
    assert "apply_decision" in src


def test_chat_note_has_no_password_and_covers_every_outcome():
    from app.api import project_docs as pd

    base = {"host": "shop.example.com", "username_masked": "mo***@example.com"}
    for approved, status in [(True, "saved"), (True, "updated"), (True, "already_saved"),
                             (True, "expired"), (True, "error"), (False, "discarded")]:
        note = pd._browser_login_save_note(
            head="✅ 승인", approved=approved, summary="s",
            result={**base, "status": status, "error": "RuntimeError"},
        )
        assert "shop.example.com" in note and USER not in note
        _assert_no_secret(note)
