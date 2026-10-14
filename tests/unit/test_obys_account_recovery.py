"""오비서 아이디 찾기·비밀번호 재설정 단위 테스트 (Postgres/asyncpg 없이 메모리 저장소로 검증)."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import auth as auth_module
from app.services import obys_account_recovery as rec

pytestmark = pytest.mark.skipif(auth_module._bcrypt_mod is None, reason="bcrypt 모듈 없음")

TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"
RAW_PHONE = "010-1234-5678"
RAW_EMAIL = "hongsukbin@example.com"


def run(coro):
    return asyncio.run(coro)


class FakeStore:
    """RecoveryStore 의 SQL 의미(원자적 소진·만료·세션 번호 증가)를 메모리로 옮긴 것."""

    def __init__(self):
        self.now = datetime.now(timezone.utc)
        self.users = {
            "u-hong": {"email": RAW_EMAIL, "name": "홍석빈", "password_hash": rec.hash_secret("OldPass123"),
                        "must_change_password": False, "session_version": 0},
            "u-admin": {"email": "boss@example.com", "name": "대표", "password_hash": "x",
                         "must_change_password": False, "session_version": 0},
        }
        self.roles = {"u-hong": {TENANT_A: "member"}, "u-admin": {TENANT_A: "owner"}}
        self.joins = [{"user_id": "u-hong", "email": RAW_EMAIL, "name": "홍석빈", "phone": RAW_PHONE, "tenant_id": TENANT_A}]
        self.requests: dict[str, dict] = {}
        self.audits: list[dict] = []
        self.notified: list[str] = []
        self._seq = 0

    async def schema_ready(self) -> bool:
        return True

    async def find_accounts(self, name, phone, email):
        out = []
        for j in self.joins:
            if rec.normalize_name(j["name"]) != name or rec.normalize_phone(j["phone"]) != phone:
                continue
            if email and j["email"].lower() != email:
                continue
            out.append({"user_id": j["user_id"], "email": j["email"], "tenant_id": j["tenant_id"],
                        "business_id": "biz", "name": j["name"]})
        return out

    async def read_phone(self, account):
        return rec.normalize_phone(RAW_PHONE)

    async def open_request(self, account, method, ip_hash):
        for rid, r in self.requests.items():
            if (r["user_id"], r["tenant_id"], r["method"]) == (account["user_id"], account["tenant_id"], method) \
                    and r["status"] in ("pending", "issued"):
                return rid
        self._seq += 1
        rid = f"00000000-0000-0000-0000-{self._seq:012d}"
        self.requests[rid] = {"id": rid, "user_id": account["user_id"], "tenant_id": account["tenant_id"],
                              "method": method, "status": "pending", "temp_password_hash": None,
                              "temp_expires_at": None, "sms_code_hash": None, "sms_failed_attempts": 0,
                              "ip_hash": ip_hash}
        return rid

    async def notify_admins(self, account, request_id):
        self.notified.append(request_id)

    async def list_requests(self, tenant_id):
        return [dict(r) for r in self.requests.values()
                if r["tenant_id"] == tenant_id and r["method"] == "admin" and r["status"] in ("pending", "issued")]

    async def get_request(self, request_id):
        r = self.requests.get(request_id)
        return dict(r) if r else None

    async def member_roles(self, user_id):
        return dict(self.roles.get(user_id, {}))

    async def issue_temp(self, request_id, tenant_id, issued_by, temp_hash, expires_at):
        r = self.requests.get(request_id)
        if not r or r["tenant_id"] != tenant_id or r["status"] not in ("pending", "issued"):
            return False
        r.update(status="issued", temp_password_hash=temp_hash, temp_expires_at=expires_at, issued_by=issued_by)
        self.users[r["user_id"]]["session_version"] += 1
        return True

    async def cancel_request(self, request_id, tenant_id, cancelled_by):
        r = self.requests.get(request_id)
        if not r or r["tenant_id"] != tenant_id or r["status"] not in ("pending", "issued"):
            return False
        r.update(status="cancelled", temp_password_hash=None)
        return True

    async def consume_temp(self, email, password):
        for r in self.requests.values():
            user = self.users[r["user_id"]]
            if user["email"].lower() != email or r["status"] != "issued" or r["method"] != "admin":
                continue
            if r["temp_expires_at"] <= self.now:
                continue
            if not await rec._check_async(password, r["temp_password_hash"] or ""):
                continue
            r.update(status="used", temp_password_hash=None)
            user["must_change_password"] = True
            return {"id": r["user_id"], "email": user["email"], "name": user["name"], "tenant_id": r["tenant_id"],
                    "role": "member", "request_id": r["id"], "request_tenant_id": r["tenant_id"]}
        return None

    async def set_sms_code(self, request_id, code_hash, expires_at):
        self.requests[request_id].update(sms_code_hash=code_hash, sms_code_expires_at=expires_at, sms_failed_attempts=0)

    async def verify_sms(self, email, code):
        for r in self.requests.values():
            if r["method"] != "sms" or r["status"] != "pending" or self.users[r["user_id"]]["email"] != email:
                continue
            if not r["sms_code_hash"] or r["sms_failed_attempts"] >= rec.SMS_MAX_FAILURES:
                return None
            if r["sms_code_expires_at"] <= self.now:
                return None
            if not await rec._check_async(code, r["sms_code_hash"]):
                r["sms_failed_attempts"] += 1
                return None
            r.update(status="used", sms_code_hash=None)
            return {"user_id": r["user_id"], "request_id": r["id"]}
        return None

    async def user_password_hash(self, user_id):
        return self.users[user_id]["password_hash"]

    async def apply_new_password(self, user_id, password_hash, complete_methods):
        user = self.users[user_id]
        user.update(password_hash=password_hash, must_change_password=False)
        user["session_version"] += 1
        for r in self.requests.values():
            if r["user_id"] == user_id and r["status"] in ("pending", "issued", "used") and r["method"] in complete_methods:
                r.update(status="completed", temp_password_hash=None, sms_code_hash=None)
        return {"session_version": user["session_version"]}

    async def audit(self, event, *, request_id=None, tenant_id=None, user_id=None, actor=None, ip_hash=""):
        self.audits.append({"event": event, "request_id": request_id, "tenant_id": tenant_id,
                            "user_id": user_id, "actor": actor, "ip_hash": ip_hash})


@pytest.fixture(autouse=True)
def _fresh_limiter():
    rec.limiter().reset()
    yield
    rec.limiter().reset()


@pytest.fixture
def store():
    return FakeStore()


def _open_admin_request(store: FakeStore) -> str:
    run(rec.request_password_reset(store, RAW_EMAIL, "홍석빈", RAW_PHONE, "1.1.1.1"))
    return next(iter(store.requests))


def _issue(store: FakeStore, rid: str, **overrides):
    args = dict(request_id=rid, actor_id="u-admin", actor_role="owner", actor_tenant_id=TENANT_A)
    args.update(overrides)
    return run(rec.issue_temp_password(store, **args))


# ── 마스킹 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("email,expected", [
    ("hongsukbin@gmail.com", "ho***@gmail.com"),
    ("abc@x.kr", "a***@x.kr"),
    ("ab@x.kr", "a***@x.kr"),
    ("abcd@x.kr", "ab***@x.kr"),
    ("not-an-email", "***"),
    ("", "***"),
])
def test_mask_email_format(email, expected):
    assert rec.mask_email(email) == expected


def test_masked_email_never_contains_the_full_local_part():
    masked = rec.mask_email(RAW_EMAIL)
    assert "hongsukbin" not in masked and masked.endswith("@example.com")


def test_phone_and_name_normalization():
    assert rec.normalize_phone("+82 10-1234-5678") == "01012345678"
    assert rec.normalize_phone("010 1234 5678") == "01012345678"
    assert rec.normalize_phone("123") == ""
    assert rec.normalize_name(" 홍 석빈 ") == rec.normalize_name("홍석빈")


# ── 아이디 찾기 ───────────────────────────────────────────────────────────

def test_find_id_returns_masked_email_only(store):
    result = run(rec.find_id(store, "홍석빈", "010-1234-5678", "1.1.1.1"))
    assert result["found"] is True
    assert result["masked_email"] == "ho***@example.com"
    assert RAW_EMAIL not in repr(result)


def test_find_id_no_match_and_multi_match_are_identical(store):
    store.joins.append({"user_id": "u-hong2", "email": "other@example.com", "name": "홍석빈",
                        "phone": RAW_PHONE, "tenant_id": TENANT_B})
    store.joins.append({"user_id": "u-kim", "email": "kim@example.com", "name": "김철수",
                        "phone": "010-9999-0000", "tenant_id": TENANT_A})
    multi = run(rec.find_id(store, "홍석빈", RAW_PHONE, "2.2.2.2"))
    none = run(rec.find_id(store, "없는사람", "010-0000-0000", "3.3.3.3"))
    blank = run(rec.find_id(store, "", "", "4.4.4.4"))
    assert multi == none == blank
    assert multi["found"] is False and multi["masked_email"] is None


def test_find_id_same_account_in_two_orgs_is_still_one_account(store):
    store.joins.append({"user_id": "u-hong", "email": RAW_EMAIL, "name": "홍석빈", "phone": RAW_PHONE, "tenant_id": TENANT_B})
    assert run(rec.find_id(store, "홍석빈", RAW_PHONE, "1.1.1.1"))["found"] is True


def test_find_id_rate_limit_is_per_ip_and_name(store):
    for _ in range(rec.RATE_LIMIT_MAX):
        run(rec.find_id(store, "홍석빈", RAW_PHONE, "9.9.9.9"))
    with pytest.raises(rec.RecoveryError) as err:
        run(rec.find_id(store, "홍석빈", RAW_PHONE, "9.9.9.9"))
    assert err.value.status_code == 429
    assert run(rec.find_id(store, "홍석빈", RAW_PHONE, "8.8.8.8"))["found"] is True
    assert run(rec.find_id(store, "김철수", RAW_PHONE, "9.9.9.9"))["found"] is False


def test_rate_limiter_window_expires_and_keys_are_hashed():
    now = [0.0]
    limiter = rec.RateLimiter(max_attempts=2, window_seconds=10, clock=lambda: now[0])
    assert limiter.hit("k") and limiter.hit("k") and not limiter.hit("k")
    now[0] = 11.0
    assert limiter.hit("k")
    key = rec._rate_key("find", "10.0.0.1", rec.normalize_name("홍석빈"))
    assert "10.0.0.1" not in key and "홍석빈" not in key


def test_no_raw_email_phone_or_name_in_logs_or_audit(store, caplog):
    caplog.set_level(logging.DEBUG)
    run(rec.find_id(store, "홍석빈", RAW_PHONE, "1.1.1.1"))
    run(rec.request_password_reset(store, RAW_EMAIL, "홍석빈", RAW_PHONE, "1.1.1.1"))
    blob = caplog.text + repr(store.audits)
    for needle in (RAW_EMAIL, "hongsukbin", "1234-5678", "01012345678", "홍석빈", "1.1.1.1"):
        assert needle not in blob


# ── 비밀번호 재설정 요청 ──────────────────────────────────────────────────

def test_reset_request_response_is_identical_for_known_and_unknown(store):
    known = run(rec.request_password_reset(store, RAW_EMAIL, "홍석빈", RAW_PHONE, "1.1.1.1"))
    unknown = run(rec.request_password_reset(store, "nobody@example.com", "가나다", "010-0000-0000", "2.2.2.2"))
    assert known == unknown and known["accepted"] is True
    assert len(store.requests) == 1 and len(store.notified) == 1


def test_reset_request_requires_all_three_fields_to_match(store):
    run(rec.request_password_reset(store, RAW_EMAIL, "홍석빈", "010-0000-0000", "1.1.1.1"))
    run(rec.request_password_reset(store, RAW_EMAIL, "다른이름", RAW_PHONE, "1.1.1.2"))
    assert store.requests == {}


def test_repeated_reset_request_keeps_a_single_open_request(store):
    run(rec.request_password_reset(store, RAW_EMAIL, "홍석빈", RAW_PHONE, "1.1.1.1"))
    run(rec.request_password_reset(store, RAW_EMAIL, "홍석빈", RAW_PHONE, "1.1.1.1"))
    assert len(store.requests) == 1


def test_admin_notify_failure_does_not_block_the_request(store):
    async def boom(*_a, **_k):
        raise RuntimeError("notify down")

    store.notify_admins = boom
    assert run(rec.request_password_reset(store, RAW_EMAIL, "홍석빈", RAW_PHONE, "1.1.1.1"))["accepted"] is True
    assert len(store.requests) == 1


def test_reset_request_rate_limited(store):
    for _ in range(rec.RATE_LIMIT_MAX):
        run(rec.request_password_reset(store, RAW_EMAIL, "홍석빈", RAW_PHONE, "7.7.7.7"))
    with pytest.raises(rec.RecoveryError) as err:
        run(rec.request_password_reset(store, RAW_EMAIL, "홍석빈", RAW_PHONE, "7.7.7.7"))
    assert err.value.status_code == 429


# ── 임시 비밀번호 발급·사용 ───────────────────────────────────────────────

def test_issue_stores_only_a_bcrypt_hash_and_24h_expiry(store):
    rid = _open_admin_request(store)
    out = _issue(store, rid)
    stored = store.requests[rid]
    assert out["temp_password"] not in repr(stored)
    assert stored["temp_password_hash"].startswith("$2")
    assert rec.check_secret(out["temp_password"], stored["temp_password_hash"])
    expires = datetime.fromisoformat(out["expires_at"])
    assert timedelta(hours=23, minutes=59) < expires - datetime.now(timezone.utc) <= timedelta(hours=24)
    assert out["temp_password"] not in repr(store.audits)


def test_temp_password_is_single_use_and_forces_password_change(store):
    rid = _open_admin_request(store)
    temp = _issue(store, rid)["temp_password"]
    assert run(rec.try_temp_password_login(store, RAW_EMAIL.upper(), "wrong-password")) is None
    user = run(rec.try_temp_password_login(store, RAW_EMAIL, temp))
    assert user and user["id"] == "u-hong"
    assert store.users["u-hong"]["must_change_password"] is True
    assert store.requests[rid]["status"] == "used" and store.requests[rid]["temp_password_hash"] is None
    assert run(rec.try_temp_password_login(store, RAW_EMAIL, temp)) is None


def test_temp_password_expires_after_24_hours(store):
    rid = _open_admin_request(store)
    temp = _issue(store, rid)["temp_password"]
    store.now = datetime.now(timezone.utc) + timedelta(hours=24, seconds=5)
    assert run(rec.try_temp_password_login(store, RAW_EMAIL, temp)) is None
    assert store.users["u-hong"]["must_change_password"] is False


def test_temp_password_does_not_replace_the_real_password(store):
    old_hash = store.users["u-hong"]["password_hash"]
    rid = _open_admin_request(store)
    _issue(store, rid)
    assert store.users["u-hong"]["password_hash"] == old_hash
    assert rec.check_secret("OldPass123", old_hash)


def test_reissue_invalidates_the_previous_temp_password(store):
    rid = _open_admin_request(store)
    first = _issue(store, rid)["temp_password"]
    second = _issue(store, rid)["temp_password"]
    assert run(rec.try_temp_password_login(store, RAW_EMAIL, first)) is None
    assert run(rec.try_temp_password_login(store, RAW_EMAIL, second)) is not None


def test_generated_temp_password_shape():
    for _ in range(50):
        value = rec.generate_temp_password()
        assert len(value) == 10
        assert any(c.isupper() for c in value) and any(c.islower() for c in value) and any(c.isdigit() for c in value)
        assert not set(value) & set("0O1lI")


def test_cancelled_request_cannot_be_issued_or_used(store):
    rid = _open_admin_request(store)
    run(rec.cancel_reset_request(store, request_id=rid, actor_id="u-admin", actor_tenant_id=TENANT_A))
    with pytest.raises(rec.RecoveryError) as err:
        _issue(store, rid)
    assert err.value.status_code == 409


# ── 발급 권한 ─────────────────────────────────────────────────────────────

def test_issue_from_another_org_is_blocked(store):
    rid = _open_admin_request(store)
    with pytest.raises(rec.RecoveryError) as err:
        _issue(store, rid, actor_tenant_id=TENANT_B)
    assert err.value.status_code == 404
    assert store.requests[rid]["status"] == "pending" and store.requests[rid]["temp_password_hash"] is None


def test_cancel_from_another_org_is_blocked(store):
    rid = _open_admin_request(store)
    with pytest.raises(rec.RecoveryError) as err:
        run(rec.cancel_reset_request(store, request_id=rid, actor_id="u-x", actor_tenant_id=TENANT_B))
    assert err.value.status_code == 404
    assert store.requests[rid]["status"] == "pending"


def test_admin_cannot_issue_for_self(store):
    store.joins.append({"user_id": "u-admin", "email": "boss@example.com", "name": "대표",
                        "phone": "010-5555-6666", "tenant_id": TENANT_A})
    run(rec.request_password_reset(store, "boss@example.com", "대표", "010-5555-6666", "5.5.5.5"))
    rid = next(iter(store.requests))
    with pytest.raises(rec.RecoveryError) as err:
        _issue(store, rid)
    assert err.value.status_code == 403


def test_equal_or_higher_role_cannot_be_issued(store):
    rid = _open_admin_request(store)
    store.roles["u-hong"][TENANT_A] = "admin"
    with pytest.raises(rec.RecoveryError) as err:
        _issue(store, rid, actor_role="admin")
    assert err.value.status_code == 403
    store.roles["u-hong"][TENANT_A] = "owner"
    with pytest.raises(rec.RecoveryError):
        _issue(store, rid, actor_role="admin")


def test_target_holding_admin_role_in_another_org_is_blocked(store):
    rid = _open_admin_request(store)
    store.roles["u-hong"][TENANT_B] = "admin"
    with pytest.raises(rec.RecoveryError) as err:
        _issue(store, rid)
    assert err.value.status_code == 403
    assert store.users["u-hong"]["session_version"] == 0


def test_internal_admin_may_issue_across_the_role_checks(store):
    rid = _open_admin_request(store)
    store.roles["u-hong"][TENANT_B] = "admin"
    out = _issue(store, rid, actor_is_internal_admin=True)
    assert out["temp_password"]


def test_target_without_membership_in_actor_org_is_not_found(store):
    rid = _open_admin_request(store)
    store.roles["u-hong"] = {TENANT_B: "member"}
    with pytest.raises(rec.RecoveryError) as err:
        _issue(store, rid)
    assert err.value.status_code == 404


# ── 세션 무효화·비밀번호 변경 ─────────────────────────────────────────────

def test_issue_bumps_session_version_to_invalidate_existing_sessions(store):
    rid = _open_admin_request(store)
    assert store.users["u-hong"]["session_version"] == 0
    _issue(store, rid)
    assert store.users["u-hong"]["session_version"] == 1


def test_change_password_after_temp_login_needs_no_current_password_and_bumps_session(store):
    rid = _open_admin_request(store)
    temp = _issue(store, rid)["temp_password"]
    run(rec.try_temp_password_login(store, RAW_EMAIL, temp))
    before = store.users["u-hong"]["session_version"]
    result = run(rec.change_password(store, user_id="u-hong", new_password="NewPass456",
                                     current_password=None, must_change=True))
    user = store.users["u-hong"]
    assert result["session_version"] == before + 1 == user["session_version"]
    assert user["must_change_password"] is False
    assert rec.check_secret("NewPass456", user["password_hash"])
    assert store.requests[rid]["status"] == "completed"


def test_voluntary_change_requires_the_current_password(store):
    with pytest.raises(rec.RecoveryError) as err:
        run(rec.change_password(store, user_id="u-hong", new_password="NewPass456",
                                current_password="wrong", must_change=False))
    assert err.value.status_code == 400
    with pytest.raises(rec.RecoveryError):
        run(rec.change_password(store, user_id="u-hong", new_password="NewPass456",
                                current_password=None, must_change=False))
    run(rec.change_password(store, user_id="u-hong", new_password="NewPass456",
                            current_password="OldPass123", must_change=False))
    assert rec.check_secret("NewPass456", store.users["u-hong"]["password_hash"])


@pytest.mark.parametrize("bad", ["short1", "onlyletters", "12345678", "가" * 30 + "a1"])
def test_weak_or_oversized_new_password_is_rejected(store, bad):
    with pytest.raises(rec.RecoveryError) as err:
        run(rec.change_password(store, user_id="u-hong", new_password=bad, current_password=None, must_change=True))
    assert err.value.status_code == 400
    assert rec.check_secret("OldPass123", store.users["u-hong"]["password_hash"])


# ── 문자 인증(선택) ───────────────────────────────────────────────────────

def test_sms_path_is_unavailable_without_aligo(store, monkeypatch):
    monkeypatch.setattr(rec, "sms_enabled", lambda: False)
    assert rec.options()["reset_sms"] is False
    with pytest.raises(rec.RecoveryError) as err:
        run(rec.request_sms_code(store, RAW_EMAIL, "홍석빈", RAW_PHONE, "1.1.1.1"))
    assert err.value.status_code == 404
    with pytest.raises(rec.RecoveryError):
        run(rec.confirm_sms_reset(store, RAW_EMAIL, "123456", "NewPass456", "1.1.1.1"))


def _code_of(text: str) -> str:
    return re.search(r"\d{6}", text).group(0)


def _sms_flow(store, monkeypatch):
    monkeypatch.setattr(rec, "sms_enabled", lambda: True)
    sent: list[str] = []

    async def fake_send(phone, text):
        sent.append(text)
        return {"result_code": "1"}

    from app.services import aligo_client
    monkeypatch.setattr(aligo_client, "send_sms", fake_send)
    run(rec.request_sms_code(store, RAW_EMAIL, "홍석빈", RAW_PHONE, "1.1.1.1"))
    return sent


def test_sms_code_is_hashed_and_confirm_sets_the_new_password_once(store, monkeypatch):
    sent = _sms_flow(store, monkeypatch)
    code = _code_of(sent[0])
    stored = next(iter(store.requests.values()))
    assert code not in repr(stored) and stored["sms_code_hash"].startswith("$2")
    out = run(rec.confirm_sms_reset(store, RAW_EMAIL, code, "NewPass456", "1.1.1.1"))
    assert out["completed"] is True
    assert rec.check_secret("NewPass456", store.users["u-hong"]["password_hash"])
    assert store.users["u-hong"]["session_version"] == 1
    with pytest.raises(rec.RecoveryError):
        run(rec.confirm_sms_reset(store, RAW_EMAIL, code, "Another789", "1.1.1.1"))


def test_sms_code_locks_after_five_failures_and_expires(store, monkeypatch):
    sent = _sms_flow(store, monkeypatch)
    code = _code_of(sent[0])
    wrong = "000000" if code != "000000" else "111111"
    for i in range(rec.SMS_MAX_FAILURES):
        rec.limiter().reset()
        with pytest.raises(rec.RecoveryError):
            run(rec.confirm_sms_reset(store, RAW_EMAIL, wrong, "NewPass456", f"10.0.0.{i}"))
    rec.limiter().reset()
    with pytest.raises(rec.RecoveryError) as err:
        run(rec.confirm_sms_reset(store, RAW_EMAIL, code, "NewPass456", "10.9.9.9"))
    assert err.value.detail == rec.MSG_SMS_INVALID
    assert rec.check_secret("OldPass123", store.users["u-hong"]["password_hash"])


def test_sms_code_expires_after_five_minutes(store, monkeypatch):
    sent = _sms_flow(store, monkeypatch)
    store.now = datetime.now(timezone.utc) + rec.SMS_CODE_TTL + timedelta(seconds=5)
    with pytest.raises(rec.RecoveryError):
        run(rec.confirm_sms_reset(store, RAW_EMAIL, _code_of(sent[0]), "NewPass456", "1.1.1.1"))


def test_sms_request_for_unknown_account_answers_the_same_and_sends_nothing(store, monkeypatch):
    sent = _sms_flow(store, monkeypatch)
    known_count = len(sent)
    out = run(rec.request_sms_code(store, "nobody@example.com", "가나다", "010-0000-0000", "2.2.2.2"))
    assert out == {"accepted": True, "message": rec.MSG_SMS_ACCEPTED}
    assert len(sent) == known_count


# ── get_current_user: 세션 번호·비밀번호 변경 강제 ─────────────────────────

def _request(path: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": path, "headers": [], "query_string": b""})


@pytest.fixture
def guard(monkeypatch):
    state: dict = {"value": {"must_change_password": False, "session_version": 0}}

    async def fake_state(_uid):
        return state["value"]

    async def fake_ctx(_user, requested_tenant_id=None):
        return {"tenant": {"id": TENANT_A, "slug": "a", "name": "A", "kind": "business", "status": "active"},
                "membership": {"id": "m", "tenant_id": TENANT_A, "user_id": "u-hong", "role": "member", "status": "active"},
                "user_role": "member"}

    monkeypatch.setattr(auth_module, "load_session_state", fake_state)
    monkeypatch.setattr(auth_module, "_load_tenant_context", fake_ctx)
    return state


def _call(path: str, token: str):
    return run(auth_module.get_current_user(_request(path), authorization=f"Bearer {token}",
                                            x_tenant_id=None, x_monitor_key=None))


def test_token_with_stale_session_version_is_rejected(guard):
    guard["value"] = {"must_change_password": False, "session_version": 3}
    stale = auth_module.create_token("u-hong", RAW_EMAIL, tenant_id=TENANT_A, session_version=2)
    with pytest.raises(HTTPException) as err:
        _call("/api/v1/yeoljeong/anything", stale)
    assert err.value.status_code == 401
    fresh = auth_module.create_token("u-hong", RAW_EMAIL, tenant_id=TENANT_A, session_version=3)
    assert _call("/api/v1/yeoljeong/anything", fresh)["user_id"] == "u-hong"


def test_legacy_token_without_sv_matches_default_version_zero(guard):
    import jwt as pyjwt
    legacy = pyjwt.encode({"sub": "u-hong", "email": RAW_EMAIL, "is_admin": False, "tenant_id": TENANT_A,
                           "exp": int(datetime.now(timezone.utc).timestamp()) + 600},
                          auth_module.SECRET_KEY, algorithm=auth_module.ALGORITHM)
    assert _call("/api/v1/auth/me", legacy)["user_id"] == "u-hong"
    guard["value"] = {"must_change_password": False, "session_version": 1}
    with pytest.raises(HTTPException) as err:
        _call("/api/v1/auth/me", legacy)
    assert err.value.status_code == 401


def test_must_change_password_blocks_everything_except_change_and_me(guard):
    guard["value"] = {"must_change_password": True, "session_version": 0}
    token = auth_module.create_token("u-hong", RAW_EMAIL, tenant_id=TENANT_A, session_version=0)
    for path in ("/api/v1/yeoljeong/finance/summary", "/api/v1/auth/tenants", "/api/v1/auth/password-reset-requests"):
        with pytest.raises(HTTPException) as err:
            _call(path, token)
        assert err.value.status_code == 403 and "password_change_required" in err.value.detail
    for path in ("/api/v1/auth/password/change", "/api/v1/auth/me", "/auth/password/change/"):
        assert _call(path, token)["must_change_password"] is True


def test_guard_is_a_noop_when_the_migration_is_absent(guard):
    guard["value"] = None
    token = auth_module.create_token("u-hong", RAW_EMAIL, tenant_id=TENANT_A, session_version=7)
    assert _call("/api/v1/yeoljeong/anything", token)["user_id"] == "u-hong"


def test_env_admin_principal_skips_the_session_guard(guard):
    guard["value"] = {"must_change_password": True, "session_version": 9}
    token = auth_module.create_token("admin", "ceo@example.com", is_admin=True, tenant_id=TENANT_A)
    assert _call("/api/v1/yeoljeong/anything", token)["is_admin"] is True


# ── 로그인 엔드포인트: 임시 비밀번호 → must_change_password ────────────────

def test_login_with_temp_password_returns_short_lived_forced_change_token(store, monkeypatch):
    from app.api import auth as auth_api

    rid = _open_admin_request(store)
    temp = _issue(store, rid)["temp_password"]

    async def ready():
        return None

    async def no_normal_login(_email, _password):
        return None

    async def tenant_for(_user):
        return TENANT_A

    async def noop(_uid):
        return None

    async def sv(_uid):
        return store.users["u-hong"]["session_version"]

    monkeypatch.setattr(auth_module, "require_saas_schema_ready", ready)
    monkeypatch.setattr(auth_module, "authenticate_saas_user", no_normal_login)
    monkeypatch.setattr(auth_module, "resolve_login_tenant_for_user", tenant_for)
    monkeypatch.setattr(auth_module, "update_saas_user_last_login", noop)
    monkeypatch.setattr(auth_module, "session_version_for", sv)
    monkeypatch.setattr(auth_api.recovery, "get_store", lambda: store)

    resp = run(auth_api.login(auth_api.LoginRequest(email=RAW_EMAIL, password=temp)))
    assert resp.must_change_password is True and resp.user_id == "u-hong"
    claims = auth_module.verify_token(resp.token)
    assert claims["sv"] == store.users["u-hong"]["session_version"]
    assert claims["exp"] - claims["iat"] == auth_api._FORCED_CHANGE_TOKEN_SECONDS

    with pytest.raises(HTTPException) as err:
        run(auth_api.login(auth_api.LoginRequest(email=RAW_EMAIL, password=temp)))
    assert err.value.status_code == 401


def test_login_without_recovery_schema_keeps_the_normal_failure_path(monkeypatch):
    from app.api import auth as auth_api

    class NoSchema:
        async def schema_ready(self):
            return False

    async def ready():
        return None

    async def no_login(_e, _p):
        return None

    monkeypatch.setattr(auth_module, "require_saas_schema_ready", ready)
    monkeypatch.setattr(auth_module, "authenticate_saas_user", no_login)
    monkeypatch.setattr(auth_module, "ADMIN_PASSWORD", "")
    monkeypatch.setattr(auth_api.recovery, "get_store", lambda: NoSchema())
    with pytest.raises(HTTPException) as err:
        run(auth_api.login(auth_api.LoginRequest(email="x@example.com", password="whatever1")))
    assert err.value.status_code == 401


def test_recovery_endpoints_return_503_until_the_migration_is_applied(monkeypatch):
    from app.api import auth as auth_api

    class NoSchema:
        async def schema_ready(self):
            return False

    monkeypatch.setattr(auth_api.recovery, "get_store", lambda: NoSchema())
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b"",
                   "client": ("1.2.3.4", 1)})
    with pytest.raises(HTTPException) as err:
        run(auth_api.recovery_find_id(auth_api.FindIdRequest(name="홍석빈", phone=RAW_PHONE), req))
    assert err.value.status_code == 503
