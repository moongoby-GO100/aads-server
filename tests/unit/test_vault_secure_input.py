"""Vault 미등록 사이트 보안 입력 요청 (AADS-VAULT-SECURE-INPUT-BACKEND-20261006).

지키는 것.
  a. Vault 에 계정 없는 origin 의 {{vault:*}} fill → 입력 요청+카드 생성, 같은 키는 재사용, 다른 오류는 그대로.
  b. submit → 암호화 저장 경로 호출(source=chat_secure_input, unverified, policy=ask), 요청 submitted,
     credential_id 기록, 카드 approved, 시스템 알림. 응답·로그·예외에 비밀번호 없음.
  c. 만료 410 / 타 tenant 403 / 재제출 409 / 분당 상한 429 / 검증 실패 422 (원문 미반사).
  d. 로그인 성공 → verified + 요청 verified, 실패 → failed + 요청 failed.
  e. VAULT_SECURE_INPUT_ENABLED=0 → 기존 오류 문구, 라우터 404.
  f. 카드 목록 응답에 대시보드용 필드(credential_request, choices) 포함, 일반 승인으로는 못 연다.
"""
from __future__ import annotations

import json
import logging
import uuid

import pytest
from fastapi import HTTPException

from app.services import agent_vault_service as svc
from app.services import browser_login_autosave as als
from app.services import vault_secure_input as vsi

TENANT = "0f9d4f2a-1111-4222-8333-444455556666"
OTHER_TENANT = "9a9a9a9a-1111-4222-8333-444455556666"
SESSION = "5090a247-47f7-4a05-9a1e-2f0b6c1d8e30"
ORIGIN = "https://v2.example.com"
CRED_ID = "11111111-2222-4333-8444-555555555555"
SECRET = "Zx9!hunter2-SECRET-pw"
USER = "ceo@example.com"
CTX = {"tenant": {"id": TENANT}, "membership": {"user_id": "user-1"}}


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakeDB:
    """agent_vault_credential_requests / agent_permission_requests 의 SQL 조각 기반 가짜."""

    def __init__(self):
        self.requests: dict[str, dict] = {}
        self.cards: dict[str, dict] = {}
        self.access_logs = 0

    def acquire(self):
        return _Acquire(self)

    def transaction(self):
        return _Tx()

    async def execute(self, query, *args):
        if "SET status = 'expired'" in query:
            tenant, session, origin = (str(args[0]), args[1], args[2])
            for r in self.requests.values():
                if (r["tenant_id"], r["session_id"], r["origin"]) == (tenant, session, origin) \
                        and r["status"] == "pending" and r["is_expired"]:
                    r["status"] = "expired"
        elif "INSERT INTO agent_vault_credential_requests" in query:
            rid = str(args[0])
            self.requests[rid] = {
                "id": rid, "tenant_id": str(args[1]), "session_id": args[2], "origin": args[3],
                "login_url": args[4], "browser_work_key": args[5], "status": "pending",
                "credential_id": None, "permission_request_id": args[6], "reason": args[7],
                "expires_at": None, "created_at": None, "updated_at": None, "is_expired": False,
            }
        elif "SET status = 'pending'" in query:
            r = self.requests[str(args[0])]
            if r["status"] == "submitted" and not r["credential_id"]:
                r["status"] = "pending"
        elif "SET credential_id" in query:
            self.requests[str(args[0])]["credential_id"] = args[1]
        elif "UPDATE agent_permission_requests" in query:
            card = self.cards[args[0]]
            if card["decision"] == "pending" and card["tenant_id"] == str(args[1]):
                card["decision"], card["decided_by"] = args[2], args[3]
        elif "SET status = 'verified'" in query:
            for r in self.requests.values():
                if r["tenant_id"] == str(args[0]) and r["credential_id"] == args[1] \
                        and r["status"] in ("submitted", "failed"):
                    r["status"] = "verified"
        elif "SET status = 'failed'" in query:
            for r in self.requests.values():
                if r["tenant_id"] == str(args[0]) and r["credential_id"] == args[1] \
                        and r["status"] == "submitted":
                    r["status"], r["reason"] = "failed", args[2]
        elif "SET status = 'cancelled'" in query:
            for r in self.requests.values():
                if r["permission_request_id"] == args[0] and r["status"] == "pending":
                    r["status"] = "cancelled"
        else:
            raise AssertionError(f"unexpected execute: {query[:80]}")

    async def fetchrow(self, query, *args):
        if "status = 'pending' AND expires_at > now()" in query and "SELECT" in query:
            for r in self.requests.values():
                if (r["tenant_id"], r["session_id"], r["origin"]) == (str(args[0]), args[1], args[2]) \
                        and r["status"] == "pending" and not r["is_expired"]:
                    return dict(r)
            return None
        if "(expires_at <= now()) AS is_expired" in query:
            r = self.requests.get(str(args[0]))
            return dict(r) if r else None
        if "SET status = 'submitted'" in query:
            r = self.requests.get(str(args[0]))
            if r and r["tenant_id"] == str(args[1]) and r["status"] == "pending" and not r["is_expired"]:
                r["status"] = "submitted"
                return {"id": r["id"]}
            return None
        if "SET status = 'cancelled'" in query:
            r = self.requests.get(str(args[0]))
            if r and r["tenant_id"] == str(args[1]) and r["status"] == "pending":
                r["status"] = "cancelled"
                return {"permission_request_id": r["permission_request_id"]}
            return None
        raise AssertionError(f"unexpected fetchrow: {query[:80]}")

    async def fetchval(self, query, *args):
        if "INSERT INTO agent_permission_requests" in query:
            cid = str(uuid.uuid4())
            self.cards[cid] = {
                "id": cid, "tenant_id": str(args[0]), "work_key": args[1], "origin": args[2],
                "action_type": args[3], "summary": args[4], "reason": args[5],
                "requested_by": args[6], "scope": json.loads(args[7]), "gate_source": args[9],
                "decision": "pending", "decided_by": None,
            }
            return cid
        if "FROM agent_vault_access_logs" in query:
            return self.access_logs
        raise AssertionError(f"unexpected fetchval: {query[:80]}")


class _Page:
    def __init__(self, url=f"{ORIGIN}/login"):
        self.url = url
        self.fills: list[tuple[str, str]] = []

    async def fill(self, selector, value, timeout=None):
        self.fills.append((selector, value))


@pytest.fixture
def env(monkeypatch):
    db = FakeDB()
    state = {
        "db": db, "notices": [], "saved": [], "updated": [], "labels": [], "existing": None,
        "verify": [], "creds": [], "save_error": None,
    }
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: db)
    monkeypatch.delenv("VAULT_SECURE_INPUT_ENABLED", raising=False)
    monkeypatch.delenv("VAULT_SECURE_INPUT_RATE_PER_MIN", raising=False)

    async def notify(session_id, text):
        state["notices"].append((session_id, text))
        return "notified"

    async def find(**kw):
        return state["existing"]

    async def upsert(**kw):
        if state["save_error"]:
            raise state["save_error"]
        state["saved"].append(kw)
        return {"id": CRED_ID, **{k: kw[k] for k in ("origin", "label")}}

    async def update(**kw):
        state["updated"].append(kw)
        return {"id": kw["credential_id"]}

    async def list_active(**kw):
        return state["labels"]

    async def write_log(*, conn, **kw):
        conn.access_logs += 1
        assert SECRET not in json.dumps(kw, default=str)

    async def set_verification(**kw):
        state["verify"].append(kw)
        return True

    monkeypatch.setattr(vsi, "_notify_session", notify)
    monkeypatch.setattr(svc, "find_agent_credential_by_username", find)
    monkeypatch.setattr(svc, "upsert_agent_credential", upsert)
    monkeypatch.setattr(svc, "update_agent_credential", update)
    monkeypatch.setattr(svc, "list_agent_credentials", list_active)
    monkeypatch.setattr(svc, "write_access_log", write_log)
    monkeypatch.setattr(svc, "set_credential_verification", set_verification)
    vsi.clear_awaiting()
    als.clear_all()
    yield state
    vsi.clear_awaiting()
    als.clear_all()


async def _request(**over):
    args = {"tenant_id": TENANT, "session_id": SESSION, "url": f"{ORIGIN}/login?token=abc#x"}
    args.update(over)
    return await vsi.request_credential_input(**args)


async def _submit(request_id, **over):
    args = {
        "tenant_id": TENANT, "user_id": "user-1", "request_id": request_id,
        "username": USER, "password": SECRET, "label": "",
    }
    args.update(over)
    return await vsi.submit_credential(**args)


# ── a. 요청 생성 ─────────────────────────────────────────────────────

async def test_request_creates_request_and_card_without_secrets(env):
    result = await _request(reason="로그인 필요")
    db = env["db"]
    req = db.requests[result["request_id"]]
    card = db.cards[result["permission_request_id"]]
    assert req["status"] == "pending" and req["origin"] == ORIGIN and req["session_id"] == SESSION
    assert req["login_url"] == f"{ORIGIN}/login"
    assert card["gate_source"] == "vault_credential_input" and card["action_type"] == "vault_credential_input"
    assert card["decision"] == "pending" and card["requested_by"] == SESSION
    assert "v2.example.com" in card["summary"] and "token" not in card["summary"]
    assert card["scope"]["credential_request_id"] == result["request_id"]
    assert result["reused"] is False


async def test_same_tenant_session_origin_reuses_pending_request(env):
    first = await _request()
    second = await _request()
    assert second["request_id"] == first["request_id"] and second["reused"] is True
    assert len(env["db"].requests) == 1 and len(env["db"].cards) == 1
    other = await _request(session_id="other-session")
    assert other["request_id"] != first["request_id"]


async def test_expired_pending_request_is_replaced(env):
    first = await _request()
    env["db"].requests[first["request_id"]]["is_expired"] = True
    second = await _request()
    assert second["request_id"] != first["request_id"]
    assert env["db"].requests[first["request_id"]]["status"] == "expired"


def test_tool_result_text_carries_request_id_and_refill_instruction():
    text = vsi.tool_result_text({"request_id": "rid-1", "host": "v2.example.com", "reused": False})
    assert text.startswith("credential_input_requested request_id=rid-1 — 대표님 입력 대기")
    assert "{{vault:username}}" in text and "{{vault:password}}" in text


@pytest.fixture
def fill_env(monkeypatch, env):
    from app.api import ceo_chat_tools as tools

    page = _Page()
    state = {"page": page, "cred": None, "tools": tools, "on_fill": []}

    class _Ctx:
        pages = [page]

    async def acquire(*a, **kw):
        return _Ctx(), None

    async def for_url(**kw):
        return state["cred"]

    async def by_id(**kw):
        return state["cred"]

    async def mark_used(**kw):
        return True

    monkeypatch.setattr(tools, "_acquire_pw_context", acquire)
    monkeypatch.setattr(svc, "get_agent_credential_for_url", for_url)
    monkeypatch.setattr(svc, "get_agent_credential_by_id", by_id)
    monkeypatch.setattr(svc, "mark_agent_credential_used", mark_used)
    env["fill"] = state
    return env


async def test_browser_fill_without_vault_account_requests_input(fill_env):
    tools = fill_env["fill"]["tools"]
    out = await tools.tool_browser_fill(
        "#pw", "{{vault:password}}", tenant_id=TENANT, browser_work_key=SESSION,
    )
    assert out.startswith("credential_input_requested request_id=")
    assert "대표님 입력 대기" in out and "{{vault:password}}" in out
    assert fill_env["fill"]["page"].fills == []
    assert len(fill_env["db"].requests) == 1
    again = await tools.tool_browser_fill(
        "#id", "{{vault:username}}", tenant_id=TENANT, browser_work_key=SESSION,
    )
    assert len(fill_env["db"].requests) == 1
    assert again.split("request_id=")[1].split()[0] == out.split("request_id=")[1].split()[0]


async def test_browser_fill_other_vault_errors_are_unchanged(fill_env):
    tools = fill_env["fill"]["tools"]
    out = await tools.tool_browser_fill("#pw", "{{vault:password}}", tenant_id="", browser_work_key="")
    assert out in ("[ERROR] vault_tenant_required",) or out.startswith("[ERROR] vault_")
    bad = await tools.tool_browser_fill(
        "#pw", "{{vault:password}}", tenant_id=TENANT, browser_work_key=SESSION, credential_id="nope",
    )
    assert bad == "[ERROR] vault_invalid_credential_id"
    assert fill_env["db"].requests == {}


async def test_request_failure_falls_back_to_original_error(fill_env, monkeypatch):
    async def boom(**kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(vsi, "request_credential_input", boom)
    tools = fill_env["fill"]["tools"]
    out = await tools.tool_browser_fill(
        "#pw", "{{vault:password}}", tenant_id=TENANT, browser_work_key=SESSION,
    )
    assert out == f"[ERROR] vault_credential_not_found origin={ORIGIN}"


async def test_flag_off_keeps_old_error_and_blocks_endpoints(fill_env, monkeypatch):
    monkeypatch.setenv("VAULT_SECURE_INPUT_ENABLED", "0")
    tools = fill_env["fill"]["tools"]
    out = await tools.tool_browser_fill(
        "#pw", "{{vault:password}}", tenant_id=TENANT, browser_work_key=SESSION,
    )
    assert out == f"[ERROR] vault_credential_not_found origin={ORIGIN}"
    assert fill_env["db"].requests == {}
    tool_out = await tools.tool_vault_request_credential_input(url=ORIGIN, tenant_id=TENANT)
    assert tool_out == "[ERROR] vault_secure_input_disabled"
    from app.routers import agent_vault as router

    with pytest.raises(HTTPException) as err:
        await router.api_get_credential_request("x", context=CTX)
    assert err.value.status_code == 404


async def test_chat_tool_requests_input_and_skips_when_vault_has_account(fill_env):
    tools = fill_env["fill"]["tools"]
    out = await tools.tool_vault_request_credential_input(
        url=f"{ORIGIN}/login", reason="로그인", tenant_id=TENANT, browser_work_key=SESSION,
    )
    assert out.startswith("credential_input_requested request_id=")
    fill_env["labels"] = [{"label": "x"}]
    exists = await tools.tool_vault_request_credential_input(
        url=f"{ORIGIN}/login", tenant_id=TENANT, browser_work_key=SESSION,
    )
    assert exists.startswith("vault_credential_exists")


# ── b. 제출 ──────────────────────────────────────────────────────────

async def test_submit_saves_encrypted_via_service_and_closes_card(env, caplog):
    caplog.set_level(logging.DEBUG)
    req = await _request()
    result = await _submit(req["request_id"], label="내 계정")
    assert result["status"] == "submitted" and result["credential_id"] == CRED_ID
    assert result["mode"] == "saved" and result["verification_status"] == "unverified"
    saved = env["saved"][0]
    assert saved["password"] == SECRET and saved["username"] == USER and saved["origin"] == ORIGIN
    assert saved["metadata"] == {"source": "chat_secure_input", "policy": "ask", "verification_status": "unverified"}
    assert saved["label"] == "내 계정"
    row = env["db"].requests[req["request_id"]]
    assert row["status"] == "submitted" and row["credential_id"] == CRED_ID
    card = env["db"].cards[req["permission_request_id"]]
    assert card["decision"] == "approved" and card["decided_by"] == "user-1"
    session, text = env["notices"][0]
    assert session == SESSION and "v2.example.com" in text and "계정 입력 완료" in text
    blob = json.dumps([result, text, row, card], default=str) + caplog.text
    assert SECRET not in blob


async def test_submit_updates_existing_same_origin_username(env):
    env["existing"] = {
        "id": CRED_ID, "work_key": "wk", "label": "기존", "metadata": {"policy": "auto", "note": "keep"},
        "password": "old-pw",
    }
    req = await _request()
    result = await _submit(req["request_id"])
    assert result["mode"] == "updated" and env["saved"] == []
    upd = env["updated"][0]
    assert upd["credential_id"] == CRED_ID and upd["label"] == "기존" and upd["password"] == SECRET
    assert upd["metadata"]["source"] == "chat_secure_input"
    assert upd["metadata"]["verification_status"] == "unverified"
    assert upd["metadata"]["policy"] == "auto" and upd["metadata"]["note"] == "keep"


async def test_submit_label_collision_gets_unique_suffix(env):
    env["labels"] = [{"label": f"v2.example.com - {USER}"}]
    req = await _request()
    await _submit(req["request_id"])
    label = env["saved"][0]["label"]
    assert label.startswith(f"v2.example.com - {USER} #") and label != f"v2.example.com - {USER}"


async def test_submit_is_one_shot(env):
    req = await _request()
    await _submit(req["request_id"])
    with pytest.raises(vsi.SecureInputError) as err:
        await _submit(req["request_id"])
    assert err.value.status_code == 409 and err.value.extra["status"] == "submitted"
    assert len(env["saved"]) == 1


async def test_save_failure_reverts_to_pending_without_leaking(env, caplog):
    caplog.set_level(logging.DEBUG)
    env["save_error"] = RuntimeError(f"insert failed for {SECRET}")
    req = await _request()
    with pytest.raises(vsi.SecureInputError) as err:
        await _submit(req["request_id"])
    assert err.value.status_code == 503
    assert SECRET not in str(err.value) and SECRET not in caplog.text
    assert err.value.__cause__ is None and err.value.__suppress_context__
    assert env["db"].requests[req["request_id"]]["status"] == "pending"
    env["save_error"] = None
    assert (await _submit(req["request_id"]))["status"] == "submitted"


# ── c. 거절 경로 ─────────────────────────────────────────────────────

async def test_expired_request_is_rejected_with_410(env):
    req = await _request()
    env["db"].requests[req["request_id"]]["is_expired"] = True
    with pytest.raises(vsi.SecureInputError) as err:
        await _submit(req["request_id"])
    assert err.value.status_code == 410 and env["saved"] == []
    assert (await vsi.get_credential_request(tenant_id=TENANT, request_id=req["request_id"]))["status"] == "expired"


async def test_other_tenant_and_unknown_request_are_rejected(env):
    req = await _request()
    with pytest.raises(vsi.SecureInputError) as err:
        await _submit(req["request_id"], tenant_id=OTHER_TENANT)
    assert err.value.status_code == 403
    with pytest.raises(vsi.SecureInputError) as err:
        await vsi.get_credential_request(tenant_id=OTHER_TENANT, request_id=req["request_id"])
    assert err.value.status_code == 403
    with pytest.raises(vsi.SecureInputError) as err:
        await _submit(str(uuid.uuid4()))
    assert err.value.status_code == 404
    with pytest.raises(vsi.SecureInputError) as err:
        await _submit("not-a-uuid")
    assert err.value.status_code == 404
    assert env["saved"] == []


async def test_rate_limit_per_tenant_per_minute(env, monkeypatch):
    monkeypatch.setenv("VAULT_SECURE_INPUT_RATE_PER_MIN", "2")
    req = await _request()
    for _ in range(2):
        with pytest.raises(vsi.SecureInputError) as err:
            await _submit(str(uuid.uuid4()))
        assert err.value.status_code == 404
    with pytest.raises(vsi.SecureInputError) as err:
        await _submit(req["request_id"])
    assert err.value.status_code == 429 and err.value.extra["retry_after"] == 60
    assert env["saved"] == []


async def test_cancel_closes_request_and_card_once(env):
    req = await _request()
    out = await vsi.cancel_credential_request(tenant_id=TENANT, user_id="user-1", request_id=req["request_id"])
    assert out["status"] == "cancelled"
    assert env["db"].requests[req["request_id"]]["status"] == "cancelled"
    assert env["db"].cards[req["permission_request_id"]]["decision"] == "rejected"
    assert "취소" in env["notices"][0][1]
    with pytest.raises(vsi.SecureInputError) as err:
        await vsi.cancel_credential_request(tenant_id=TENANT, user_id="user-1", request_id=req["request_id"])
    assert err.value.status_code == 409
    with pytest.raises(vsi.SecureInputError) as err:
        await _submit(req["request_id"])
    assert err.value.status_code == 409


async def test_cancel_by_card_reject_path(env):
    req = await _request()
    await vsi.cancel_by_card(tenant_id=TENANT, permission_request_id=req["permission_request_id"])
    assert env["db"].requests[req["request_id"]]["status"] == "cancelled"


# ── 라우터 ───────────────────────────────────────────────────────────

async def test_router_maps_errors_and_never_echoes_password(env):
    from app.routers import agent_vault as router

    req = await _request()
    body = router.SecureInputSubmitIn(username=USER, password=SECRET)
    ok = await router.api_submit_credential_request(req["request_id"], body, context=CTX)
    assert ok["status"] == "submitted" and SECRET not in json.dumps(ok)
    with pytest.raises(HTTPException) as err:
        await router.api_submit_credential_request(req["request_id"], body, context=CTX)
    assert err.value.status_code == 409 and err.value.detail["error"] == "request_not_pending"
    assert SECRET not in json.dumps(err.value.detail)

    bad = router.SecureInputSubmitIn(username=USER, password=[SECRET])
    with pytest.raises(HTTPException) as err:
        await router.api_submit_credential_request(req["request_id"], bad, context=CTX)
    assert err.value.status_code == 422 and SECRET not in json.dumps(err.value.detail)

    fresh = await _request(session_id="s2")
    env["db"].requests[fresh["request_id"]]["is_expired"] = True
    with pytest.raises(HTTPException) as err:
        await router.api_submit_credential_request(fresh["request_id"], body, context=CTX)
    assert err.value.status_code == 410

    got = await router.api_get_credential_request(req["request_id"], context=CTX)
    assert got["request"]["status"] == "submitted" and got["request"]["credential_id"] == CRED_ID
    assert "password" not in json.dumps(got).lower()


async def test_router_rate_limit_sets_retry_after(env, monkeypatch):
    from app.routers import agent_vault as router

    monkeypatch.setenv("VAULT_SECURE_INPUT_RATE_PER_MIN", "1")
    body = router.SecureInputSubmitIn(username=USER, password=SECRET)
    with pytest.raises(HTTPException):
        await router.api_submit_credential_request(str(uuid.uuid4()), body, context=CTX)
    with pytest.raises(HTTPException) as err:
        await router.api_submit_credential_request(str(uuid.uuid4()), body, context=CTX)
    assert err.value.status_code == 429 and err.value.headers == {"Retry-After": "60"}


async def test_router_cancel(env):
    from app.routers import agent_vault as router

    req = await _request()
    out = await router.api_cancel_credential_request(req["request_id"], context=CTX)
    assert out["status"] == "cancelled"


# ── d. 로그인 검증 ───────────────────────────────────────────────────

async def _filled(env, monkeypatch, *, login_ok, metadata=None):
    req = await _request()
    await _submit(req["request_id"])
    page = _Page()
    for_url_cred = {
        "id": CRED_ID, "tenant_id": TENANT, "origin": ORIGIN, "work_key": "wk",
        "username": USER, "password": SECRET, "metadata": metadata or {"verification_status": "unverified"},
    }

    async def for_url(**kw):
        return for_url_cred

    async def mark_used(**kw):
        return True

    async def wait_login(page_, login_url):
        return login_ok

    from app.api import ceo_chat_tools as tools

    async def acquire(*a, **kw):
        class _Ctx:
            pages = [page]
        return _Ctx(), None

    monkeypatch.setattr(tools, "_acquire_pw_context", acquire)
    monkeypatch.setattr(svc, "get_agent_credential_for_url", for_url)
    monkeypatch.setattr(svc, "mark_agent_credential_used", mark_used)
    monkeypatch.setattr(als, "_wait_login_completed", wait_login)
    out = await tools.tool_browser_fill("#pw", "{{vault:password}}", tenant_id=TENANT, browser_work_key=SESSION)
    assert out.endswith(f"source=vault credential_id={CRED_ID}") and SECRET not in out
    return req, page


async def test_login_success_marks_credential_and_request_verified(env, monkeypatch):
    req, page = await _filled(env, monkeypatch, login_ok=True)
    note = await als.on_submit(page, ORIGIN, tenant_id=TENANT, fallback_session=SESSION)
    assert "검증됨" in note
    assert env["verify"] == [{
        "tenant_id": TENANT, "credential_id": CRED_ID, "status": "verified",
        "from_statuses": ("unverified", "failed"),
    }]
    assert env["db"].requests[req["request_id"]]["status"] == "verified"
    assert not vsi.has_awaiting(TENANT, SESSION, ORIGIN)


async def test_login_failure_marks_failed_with_reason(env, monkeypatch):
    req, page = await _filled(env, monkeypatch, login_ok=False)
    note = await als.on_submit(page, ORIGIN, tenant_id=TENANT, fallback_session=SESSION)
    assert "검증 실패" in note
    assert env["verify"][0]["status"] == "failed" and env["verify"][0]["from_statuses"] == ("unverified",)
    row = env["db"].requests[req["request_id"]]
    assert row["status"] == "failed" and row["reason"] == "login_not_completed"


async def test_later_success_heals_failed_request(env, monkeypatch):
    req, page = await _filled(env, monkeypatch, login_ok=False)
    await als.on_submit(page, ORIGIN, tenant_id=TENANT, fallback_session=SESSION)
    assert env["db"].requests[req["request_id"]]["status"] == "failed"

    async def ok(page_, login_url):
        return True

    monkeypatch.setattr(als, "_wait_login_completed", ok)
    await als.on_submit(page, ORIGIN, tenant_id=TENANT, fallback_session=SESSION)
    assert env["db"].requests[req["request_id"]]["status"] == "verified"


async def test_verified_or_legacy_credentials_are_not_tracked(env, monkeypatch):
    await _filled(env, monkeypatch, login_ok=True, metadata={"verification_status": "verified"})
    assert not vsi.has_awaiting(TENANT, SESSION, ORIGIN)
    page = _Page()
    assert await als.on_submit(page, ORIGIN, tenant_id=TENANT, fallback_session=SESSION) == ""
    assert env["verify"] == []


async def test_username_fill_does_not_arm_verification():
    vsi.note_vault_fill(
        tenant_id=TENANT, session_id=SESSION, origin=ORIGIN, credential_id=CRED_ID,
        field="username", login_url=f"{ORIGIN}/login",
    )
    assert not vsi.has_awaiting(TENANT, SESSION, ORIGIN)


async def test_verification_marker_expires(env, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(vsi, "_clock", lambda: now[0])
    vsi.note_vault_fill(
        tenant_id=TENANT, session_id=SESSION, origin=ORIGIN, credential_id=CRED_ID,
        field="password", login_url=f"{ORIGIN}/login",
    )
    assert vsi.has_awaiting(TENANT, SESSION, ORIGIN)
    now[0] += vsi.VERIFY_TTL_SECONDS + 1
    assert not vsi.has_awaiting(TENANT, SESSION, ORIGIN)


# ── f. 대시보드용 카드 필드 ──────────────────────────────────────────

class _PendingPool:
    def __init__(self, rows):
        self.rows = rows
        self.queries: list[str] = []

    async def fetch(self, query, *args):
        self.queries.append(query)
        return self.rows


def _pending_row(**over):
    row = {
        "id": "card-1", "action_type": "vault_credential_input", "action_summary": "요약",
        "risk_level": "medium", "gate_source": "vault_credential_input", "tier": "approve",
        "requested_by": SESSION, "work_key": "vault_credential_input:rid", "origin": ORIGIN,
        "credential_request_id": "rid-1", "credential_host": "v2.example.com",
        "credential_login_url": f"{ORIGIN}/login", "source_message_id": None,
        "at": "10-06 12:00", "decision": "pending", "expires_in_min": 29,
    }
    row.update(over)
    return row


async def test_pending_list_exposes_credential_request_fields(monkeypatch):
    from app.api import project_docs

    pool = _PendingPool([_pending_row()])
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: pool)
    out = await project_docs.approvals_pending(limit=50, session_id=SESSION)
    card = out["pending"][0]
    assert card["tool"] == "vault_credential_input" and card["gate_source"] == "vault_credential_input"
    assert card["credential_request"] == {
        "id": "rid-1", "origin": ORIGIN, "host": "v2.example.com", "login_url": f"{ORIGIN}/login",
        "submit_path": "/api/v1/agent-vault/credential-requests/rid-1/submit",
        "cancel_path": "/api/v1/agent-vault/credential-requests/rid-1/cancel",
    }
    assert [c["key"] for c in card["choices"]] == ["reject"]
    assert "credential_request_id" in pool.queries[0]


async def test_pending_list_other_cards_have_no_credential_request(monkeypatch):
    from app.api import project_docs

    pool = _PendingPool([_pending_row(gate_source="browser_login_save", action_type="save_browser_credential")])
    monkeypatch.setattr("app.core.db_pool.get_pool", lambda: pool)
    card = (await project_docs.approvals_pending(limit=50, session_id=""))["pending"][0]
    assert card["credential_request"] is None
    assert [c["key"] for c in card["choices"]] == ["single", "reject"]


def test_generic_decide_sql_cannot_approve_credential_input_card():
    import inspect

    from app.api import project_docs

    source = inspect.getsource(project_docs.approvals_decide)
    assert "NOT ($2 = 'approved' AND action_type = 'vault_credential_input')" in source
    assert "cancel_by_card" in source


# ── 정합성 ───────────────────────────────────────────────────────────

def test_migration_is_additive_and_has_no_secret_columns():
    import pathlib

    sql = pathlib.Path("migrations/20261006_agent_vault_credential_requests.sql").read_text()
    low = "\n".join(line for line in sql.lower().splitlines() if not line.strip().startswith("--"))
    assert "create table if not exists agent_vault_credential_requests" in low
    for forbidden in ("password", "secret", "drop ", "delete ", "truncate", "alter table"):
        assert forbidden not in low
    assert "create unique index if not exists" in low


def test_safe_login_url_strips_query_userinfo_and_fragment():
    assert vsi.safe_login_url("https://u:p@Site.example.com/login?token=abc#frag") == "https://site.example.com/login"
    assert vsi.safe_login_url("javascript:alert(1)") == ""
