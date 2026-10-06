"""AADS-OBYS-EMPLOYEE-TENANT-MEMBERSHIP-20260930 — 직원 승인 → 고용주 테넌트 멤버십 → 서명.

2026-09-30 진아서버 실측: 승인 직원의 로그인 테넌트가 본인 자동생성 워크스페이스라
계약서 서명이 게이트(403) → _read_hr 스코프 → _is_admin(owner) 순으로 막혔다.
여기서는 그 흐름을 끝까지(조회·서명 → status=signed) 고정한다.

AADS 인증 DB 는 FakeAuthDB 로 흉내 낸다 — tenant_memberships 의 UNIQUE(tenant_id,
user_id)·ON CONFLICT 규칙을 그대로 옮겼고, SQL 문 자체의 핵심 절도 따로 검사한다.
오비서 DB 는 파일 모드로만 돈다(운영 DB·알리고 미접촉).
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import os
import threading
from contextlib import asynccontextmanager
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app import auth as auth_module  # noqa: E402
from app.api import obys_finance as api  # noqa: E402
from app.core import obys_tenant  # noqa: E402
from app.services import aligo_client, yeoljeong_ops_service  # noqa: E402

svc = api.svc

# 고용주 테넌트는 레거시 허용목록 밖으로 잡는다 — 서명 경로 prefix 가 실제로 여는지 본다.
EMPLOYER = "7f0c2d1e-3b4a-4c5d-8e6f-000000000032"
PERSONAL = "d184a18c-78c8-45c7-aee7-bfe79fadf95f"
OTHER = "2a1b3c4d-0000-4000-8000-000000000001"
INTERNAL = "2d701a8c-9596-4757-8588-faa4f7837112"
YEOLJEONG = "15055cac-71b0-45ec-b714-7093dde189ff"

ADMIN_ID = "user-owner"
EMP_ID = "user-hayh"
EMP_EMAIL = "dudgns3738@naver.com"
EMP_NAME = "하영훈"
ADMIN = {
    "user_id": ADMIN_ID,
    "email": "owner@example.com",
    "is_admin": False,
    "tenant_id": EMPLOYER,
    "tenant_role": "owner",
    "user_role": "user",
    "current_membership": {"tenant_id": EMPLOYER, "status": "active", "role": "owner"},
}


def _user(tenant_id: str, role: str, *, user_id: str = EMP_ID, email: str = EMP_EMAIL) -> dict:
    """get_current_user 가 만드는 모양 그대로."""
    return {
        "user_id": user_id,
        "email": email,
        "is_admin": False,
        "tenant_id": tenant_id,
        "tenant_role": role,
        "user_role": "user",
        "current_membership": {"tenant_id": tenant_id, "status": "active", "role": role},
    }


# ---------------------------------------------------------------------------
# AADS 인증 DB 흉내
# ---------------------------------------------------------------------------
class FakeAuthDB:
    def __init__(self):
        self.users = {"owner@example.com": ADMIN_ID, EMP_EMAIL: EMP_ID}
        self.tenants = {EMPLOYER: "customer", PERSONAL: "customer", OTHER: "customer", INTERNAL: "internal"}
        self.memberships: dict[tuple[str, str], dict] = {
            (EMPLOYER, ADMIN_ID): {"role": "owner", "status": "active", "deleted": False, "invited_by": None},
        }
        # 직원은 가입 때 자동 생성된 개인 워크스페이스가 default 다(2026-09-30 실측 상태).
        self.defaults: dict[str, str | None] = {EMP_ID: PERSONAL, ADMIN_ID: EMPLOYER}
        self.personal: set[tuple[str, str]] = {(PERSONAL, EMP_ID)}
        # (tenant_id, lower(email)) 로 남은 초대 — 이메일 조회 테넌트 조건의 한 갈래.
        self.invites: set[tuple[str, str]] = set()
        self.sql: list[str] = []
        # pg_advisory_xact_lock 흉내: 키별 RLock(같은 세션 재진입 허용), 트랜잭션 끝에 해제.
        self.advisory_locks: dict[int, threading.RLock] = {}
        self.advisory_guard = threading.Lock()
        self.lock_log: list[int] = []

    def advisory_lock(self, key: int) -> threading.RLock:
        with self.advisory_guard:
            return self.advisory_locks.setdefault(key, threading.RLock())

    def row(self, tenant_id, user_id):
        m = self.memberships.get((tenant_id, user_id))
        if not m:
            return None
        return {"membership_id": f"m-{tenant_id[:4]}-{user_id}", "tenant_id": tenant_id, "user_id": user_id,
                "role": m["role"], "status": m["status"]}


class FakeConn:
    def __init__(self, db: FakeAuthDB):
        self.db = db
        self.depth = 0
        self.held: list[threading.RLock] = []

    @asynccontextmanager
    async def transaction(self):
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1
            if self.depth == 0:
                # xact lock 은 바깥 트랜잭션이 끝날 때 풀린다(세이브포인트에선 안 풀린다).
                while self.held:
                    self.held.pop().release()

    async def fetch(self, sql, *args):
        self.db.sql.append(sql)
        if "FROM saas_users u" in sql and "lower(u.email) = $1" in sql:
            email, tenant_id = args
            rows = []
            for order, (address, uid) in enumerate(self.db.users.items()):
                if address.lower() != email:
                    continue
                default = self.db.defaults.get(uid)
                membership = self.db.memberships.get((tenant_id, uid))
                bound = default == tenant_id or bool(membership and not membership["deleted"])
                unattached = default is None or (default, uid) in self.db.personal
                if bound or unattached or (tenant_id, email) in self.db.invites:
                    rows.append((not bound, order, {"id": uid, "bound_to_tenant": bound}))
            return [row for _, _, row in sorted(rows, key=lambda item: item[:2])][:2]
        raise AssertionError(f"unexpected fetch: {sql}")

    async def fetchval(self, sql, *args):
        self.db.sql.append(sql)
        if "SELECT EXISTS" in sql and "t.created_by" in sql:
            return (args[0], args[1]) in self.db.personal
        if "SELECT default_tenant_id" in sql:
            return self.db.defaults.get(args[0])
        if "FROM tenants" in sql:
            return self.db.tenants.get(args[0])
        if "FROM saas_users" in sql and "WHERE id = $1" in sql:
            return next((email for email, uid in self.db.users.items() if uid == args[0]), None)
        raise AssertionError(f"unexpected fetchval: {sql}")

    async def execute(self, sql, *args):
        self.db.sql.append(sql)
        if "pg_advisory_xact_lock" in sql:
            assert self.depth > 0, "xact lock 은 트랜잭션 안에서만 건다"
            lock = self.db.advisory_lock(args[0])
            assert lock.acquire(timeout=10), "advisory lock 대기 시간 초과"
            self.held.append(lock)
            self.db.lock_log.append(args[0])
            return "SELECT 1"
        if "UPDATE saas_users SET default_tenant_id" in sql:
            self.db.defaults[args[1]] = args[0]
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute: {sql}")

    async def fetchrow(self, sql, *args):
        self.db.sql.append(sql)
        if "FOR UPDATE" in sql:
            row = self.db.row(args[0], args[1])
            if row:
                row["deleted"] = self.db.memberships[(args[0], args[1])]["deleted"]
            return row
        if "INSERT INTO tenant_memberships" in sql:
            tenant_id, user_id, role, invited_by, preserve = args
            key = (tenant_id, user_id)
            invited = invited_by if invited_by in self.db.users.values() else None
            existing = self.db.memberships.get(key)
            if existing is None:
                self.db.memberships[key] = {"role": role, "status": "active", "deleted": False, "invited_by": invited}
            else:
                keep = (preserve and existing["status"] == "active" and not existing["deleted"]
                        and existing["role"] in {"owner", "admin"})
                existing.update({"role": existing["role"] if keep else role, "status": "active", "deleted": False})
            return self.db.row(tenant_id, user_id)
        if "UPDATE tenant_memberships" in sql and "'removed'" in sql:
            membership_id, tenant_id, user_id = args
            assert self.db.row(tenant_id, user_id)["membership_id"] == membership_id
            self.db.memberships[(tenant_id, user_id)]["status"] = "removed"
            return self.db.row(tenant_id, user_id)
        raise AssertionError(f"unexpected fetchrow: {sql}")


class FakePool:
    def __init__(self, db):
        self.db = db

    @asynccontextmanager
    async def acquire(self):
        yield FakeConn(self.db)


def _disable_db(coroutine):
    close = getattr(coroutine, "close", None)
    if close:
        close()
    return None


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name in ("OBYS_DATABASE_URL", "YEOLJEONG_FINANCE_DATABASE_URL", "OBYS_CONTRACT_NOTIFY_CHANNELS",
                 "OBYS_CONTRACT_PDF_FONT_PATH", "OBYS_CONTRACT_SIGN_BASE_URL", "OBYS_LEGACY_TENANT_IDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OBYS_UPLOAD_ROOT", str(tmp_path / "upload-root"))
    monkeypatch.setattr(svc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(svc, "UPLOAD_DIR", tmp_path / "data" / "uploads" / "onboarding")
    monkeypatch.setattr(svc, "_run_db", _disable_db)

    async def fake_create_notification(**kwargs):
        return {"id": "ntf", **kwargs}

    async def fake_send(*args, **kwargs):
        return {"result_code": 1, "code": 0, "message": "success"}

    monkeypatch.setattr(yeoljeong_ops_service, "create_notification", fake_create_notification)
    monkeypatch.setattr(aligo_client, "is_available", lambda: False)
    monkeypatch.setattr(aligo_client, "send_sms", fake_send)
    monkeypatch.setattr(aligo_client, "send_alimtalk", fake_send)

    db = FakeAuthDB()

    async def ensure_pool():
        return FakePool(db)

    async def schema_ready():
        return None

    monkeypatch.setattr(auth_module, "_ensure_pool", ensure_pool)
    monkeypatch.setattr(auth_module, "require_saas_schema_ready", schema_ready)

    svc._write("employee_join_requests", [{
        "id": "join-hayh", "name": EMP_NAME, "email": EMP_EMAIL, "phone": "010-1234-5678",
        "address": "서울시 직원 주소", "birth_date": "1990-01-01",
        "tenant_id": EMPLOYER, "business_id": "biz-mia", "branch": "열정국밥_미아점", "status": "pending",
        # 직원 본인이 로그인해 제출한 요청 — upsert_join_request 가 남기는 신원 고정 값.
        "requester_user_id": EMP_ID, "requester_email": EMP_EMAIL,
    }])
    return db


def _set_join_request(**changes):
    rows = svc._read_file_rows("employee_join_requests")
    rows[0].update(changes)
    for key, value in list(changes.items()):
        if value is None:
            rows[0].pop(key, None)
    svc._write_file_rows("employee_join_requests", rows)


def _review(action: str, user: dict = ADMIN) -> dict:
    return asyncio.run(svc.review_join_request_with_membership("join-hayh", action, "", user))


def _audit() -> list[dict]:
    return svc._read_file_rows(svc.MEMBERSHIP_AUDIT_LOG)


# ---------------------------------------------------------------------------
# 1. 승인 → 멤버십
# ---------------------------------------------------------------------------
def test_approval_creates_member_membership_in_employer_tenant(env):
    result = _review("approved")

    assert result["request"]["status"] == "approved"
    assert result["membership"]["status"] == "linked"
    assert result["membership"]["role"] == "member"
    assert result["membership"]["tenant_id"] == EMPLOYER
    row = env.memberships[(EMPLOYER, EMP_ID)]
    assert row["role"] == "member" and row["status"] == "active"
    assert row["invited_by"] == ADMIN_ID
    audit = _audit()
    assert len(audit) == 1
    assert audit[0]["classification"] == "membership_linked"
    assert audit[0]["tenant_id"] == EMPLOYER
    assert audit[0]["employee_email"] == EMP_EMAIL
    assert audit[0]["actor_user_id"] == ADMIN_ID
    assert audit[0]["before_state"] is None
    assert audit[0]["after_state"]["role"] == "member"
    # 반려가 회수할 근거 — 이 승인이 만든 멤버십 표시.
    marker = result["request"]["membership_link"]
    assert marker["user_id"] == EMP_ID and marker["tenant_id"] == EMPLOYER
    assert marker["membership_id"] == env.row(EMPLOYER, EMP_ID)["membership_id"]


def test_approval_is_idempotent(env):
    _review("approved")
    second = _review("approved")

    assert second["membership"]["status"] == "unchanged"
    assert [key for key in env.memberships if key[1] == EMP_ID] == [(EMPLOYER, EMP_ID)]
    assert env.memberships[(EMPLOYER, EMP_ID)]["role"] == "member"


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_approval_never_demotes_existing_owner_or_admin(env, role):
    env.memberships[(EMPLOYER, EMP_ID)] = {"role": role, "status": "active", "deleted": False, "invited_by": None}

    result = _review("approved")

    assert env.memberships[(EMPLOYER, EMP_ID)]["role"] == role
    assert result["membership"]["role"] == role
    assert result["membership"]["status"] == "unchanged"


def test_approval_does_not_revive_removed_admin_as_admin(env):
    env.memberships[(EMPLOYER, EMP_ID)] = {"role": "admin", "status": "removed", "deleted": False, "invited_by": None}

    _review("approved")

    assert env.memberships[(EMPLOYER, EMP_ID)] == {
        "role": "member", "status": "active", "deleted": False, "invited_by": None,
    }


def test_rejection_revokes_membership_without_deleting_row(env):
    _review("approved")
    result = _review("rejected")

    assert result["request"]["status"] == "rejected"
    assert result["membership"]["status"] == "removed"
    assert env.memberships[(EMPLOYER, EMP_ID)]["status"] == "removed"
    assert (EMPLOYER, EMP_ID) in env.memberships
    assert [row["classification"] for row in _audit()] == ["membership_removed", "membership_linked"]


def test_rejection_keeps_owner_membership(env):
    env.memberships[(EMPLOYER, EMP_ID)] = {"role": "owner", "status": "active", "deleted": False, "invited_by": None}

    _review("approved")  # 이미 활성 owner — 이 승인이 만든 멤버십이 아니다
    result = _review("rejected")

    assert result["membership"]["status"] == "skipped"
    assert result["membership"]["reason"] == "membership_not_linked_by_request"
    assert env.memberships[(EMPLOYER, EMP_ID)] == {
        "role": "owner", "status": "active", "deleted": False, "invited_by": None,
    }


def test_rejection_keeps_role_promoted_after_link(env):
    _review("approved")
    env.memberships[(EMPLOYER, EMP_ID)]["role"] = "admin"  # 승인 뒤 관리자로 승격

    result = _review("rejected")

    assert result["membership"]["status"] == "kept_elevated"
    assert env.memberships[(EMPLOYER, EMP_ID)]["status"] == "active"


# --- 리뷰 지적 1: 반려는 '이 요청으로 연결된 멤버십'에만 ---------------------
@pytest.mark.parametrize("role", ["member", "viewer"])
def test_rejecting_never_approved_request_keeps_existing_invite_membership(env, role):
    """초대 수락으로 이미 member/viewer 인 사람의 요청을 (승인 없이) 반려해도 멤버십은 그대로."""
    env.memberships[(EMPLOYER, EMP_ID)] = {"role": role, "status": "active", "deleted": False, "invited_by": ADMIN_ID}

    result = _review("rejected")

    assert result["request"]["status"] == "rejected"
    assert result["membership"]["status"] == "skipped"
    assert result["membership"]["reason"] == "request_was_not_approved"
    assert env.memberships[(EMPLOYER, EMP_ID)]["status"] == "active"
    assert not any("'removed'" in sql for sql in env.sql)
    assert _audit()[0]["classification"] == "membership_skipped"


def test_rejecting_approval_that_found_existing_member_does_not_revoke(env):
    """승인 전부터 활성 member(초대)였다면 승인은 unchanged — 반려·퇴사도 그 멤버십을 빼앗지 않는다."""
    env.memberships[(EMPLOYER, EMP_ID)] = {"role": "member", "status": "active", "deleted": False, "invited_by": ADMIN_ID}

    assert _review("approved")["membership"]["status"] == "unchanged"
    result = _review("rejected")

    assert result["membership"]["reason"] == "membership_not_linked_by_request"
    assert env.memberships[(EMPLOYER, EMP_ID)]["status"] == "active"


def test_rejection_skips_when_membership_row_was_replaced(env):
    _review("approved")
    _set_join_request(membership_link={
        **svc._read_file_rows("employee_join_requests")[0]["membership_link"], "membership_id": "m-other",
    })

    result = _review("rejected")

    assert result["membership"]["status"] == "skipped"
    assert result["membership"]["reason"] == "membership_not_linked_by_request"
    assert env.memberships[(EMPLOYER, EMP_ID)]["status"] == "active"


def test_reapproval_after_revocation_relinks_and_can_be_revoked_again(env):
    _review("approved")
    _review("rejected")
    relinked = _review("approved")
    assert relinked["membership"]["status"] == "linked"
    assert env.memberships[(EMPLOYER, EMP_ID)]["status"] == "active"
    assert _review("rejected")["membership"]["status"] == "removed"
    assert env.memberships[(EMPLOYER, EMP_ID)]["status"] == "removed"


# --- 리뷰 지적 2: 이메일만으로 신원을 정하지 않는다 --------------------------
def test_request_not_bound_to_requester_account_links_by_approver_vouched_email(env):
    """R2 리뷰 지적 8: 관리자 대리 등록·초대 흐름·예전 요청(requester_* 없음)은 요청 이메일로 계정을 찾는다.

    직원은 개인 워크스페이스 JWT 로 고용주 테넌트에 가입요청을 낼 수 없다(게이트 403) —
    본인 제출 고정만 인정하면 실제 승인 거의 전부가 pending 에 머문다.
    """
    _set_join_request(requester_user_id=None, requester_email=None)

    result = _review("approved")

    assert result["request"]["status"] == "approved"
    assert result["membership"]["status"] == "linked"
    assert result["membership"]["user_id"] == EMP_ID
    assert env.memberships[(EMPLOYER, EMP_ID)]["role"] == "member"
    assert env.defaults[EMP_ID] == EMPLOYER
    assert _audit()[0]["classification"] == "membership_linked"
    assert _audit()[0]["employee_user_id"] == EMP_ID


def test_employee_can_file_join_request_but_admin_only_paths_stay_closed():
    """2026-10-01: 직원 본인 경로(가입요청·초대 확인/수락)만 연다. 관리자 전용 경로는 닫힌 채다."""
    for path in (
        "/api/v1/yeoljeong-finance/employees/join-requests",
        "/api/v1/yeoljeong-finance/employees/invites/resolve",
        "/api/v1/yeoljeong-finance/employees/invites/accept",
    ):
        assert obys_tenant._is_tenant_scoped_path(path, "POST")
    for path in (
        "/api/v1/yeoljeong-finance/employees/invites",
        "/api/v1/yeoljeong-finance/employees/approved",
        "/api/v1/yeoljeong-finance/employees/approved/x/role",
    ):
        assert not obys_tenant._is_tenant_scoped_path(path, "POST")


def test_unbound_request_without_account_stays_pending(env):
    _set_join_request(requester_user_id=None, requester_email=None, email="nobody@example.com")

    result = _review("approved")

    assert result["request"]["status"] == "approved"
    assert result["membership"]["status"] == "pending_account"
    assert not any(uid == EMP_ID for (tid, uid) in env.memberships if tid == EMPLOYER)


def test_request_with_someone_elses_email_is_not_linked(env):
    """공격자가 본인 계정으로 남의 이메일을 넣은 요청 — requester_email 과 email 이 달라 고정되지 않는다."""
    env.users["attacker@example.com"] = "user-attacker"
    _set_join_request(requester_user_id="user-attacker", requester_email="attacker@example.com")

    result = _review("approved")

    assert result["membership"]["status"] == "pending_identity"
    assert (EMPLOYER, EMP_ID) not in env.memberships
    assert (EMPLOYER, "user-attacker") not in env.memberships


# --- R3 실결함 1: 이메일 조회 전제(테넌트 컨텍스트)를 코드로 강제 ------------------
def _link_by_email(db: FakeAuthDB, *, tenant_id: str, context_tenant_id, email: str = EMP_EMAIL) -> dict:
    return asyncio.run(auth_module.link_employee_tenant_membership(
        tenant_id=tenant_id,
        employee_user_id=None,
        employee_email=email,
        invited_by=ADMIN_ID,
        allow_email_lookup=True,
        context_tenant_id=context_tenant_id,
    ))


@pytest.mark.parametrize("context", [None, "", "   "])
def test_email_lookup_without_tenant_context_raises(env, context):
    """① 컨텍스트 없음 → 예외. DB 에 닿기 전에 막고 멤버십도 만들지 않는다."""
    with pytest.raises(auth_module.EmployeeTenantContextError):
        _link_by_email(env, tenant_id=EMPLOYER, context_tenant_id=context)
    assert env.sql == []
    assert (EMPLOYER, EMP_ID) not in env.memberships
    with pytest.raises(auth_module.EmployeeTenantContextError):
        asyncio.run(auth_module._active_user_id_by_email(
            FakeConn(env), EMP_EMAIL, tenant_id=EMPLOYER, context_tenant_id=context
        ))


def test_email_lookup_with_other_tenant_context_raises(env):
    """② 컨텍스트는 있으나 대상 테넌트와 다름 → 예외(조용히 넘기지 않는다)."""
    with pytest.raises(auth_module.EmployeeTenantContextError):
        _link_by_email(env, tenant_id=EMPLOYER, context_tenant_id=OTHER)
    with pytest.raises(auth_module.EmployeeTenantContextError):
        asyncio.run(auth_module._active_user_id_by_email(
            FakeConn(env), EMP_EMAIL, tenant_id=EMPLOYER, context_tenant_id=OTHER
        ))
    assert not any(key[1] == EMP_ID for key in env.memberships)


def test_same_email_in_two_tenants_matches_only_the_right_one(env):
    """③ 대소문자만 다른 같은 이메일 계정이 두 테넌트에 있을 때 대상 테넌트 쪽만 매칭한다."""
    env.users = {"owner@example.com": ADMIN_ID, "Dup@Example.com": "user-other", "dup@example.com": "user-new"}
    # user-other: OTHER 고객 테넌트의 활성 member(default=OTHER, 개인 워크스페이스 아님)
    env.defaults["user-other"] = OTHER
    env.memberships[(OTHER, "user-other")] = {"role": "member", "status": "active", "deleted": False, "invited_by": None}
    # user-new: 아직 어느 고객 테넌트에도 묶이지 않은 계정(default 없음)
    env.defaults["user-new"] = None

    conn = FakeConn(env)
    assert asyncio.run(auth_module._active_user_id_by_email(
        conn, "dup@example.com", tenant_id=EMPLOYER, context_tenant_id=EMPLOYER
    )) == "user-new"
    assert asyncio.run(auth_module._active_user_id_by_email(
        conn, "DUP@example.com", tenant_id=OTHER, context_tenant_id=OTHER
    )) == "user-other"

    result = _link_by_email(env, tenant_id=EMPLOYER, context_tenant_id=EMPLOYER, email="dup@example.com")
    assert result["status"] == "linked" and result["user_id"] == "user-new"
    assert (EMPLOYER, "user-other") not in env.memberships
    assert env.memberships[(OTHER, "user-other")]["role"] == "member"


def test_email_lookup_sql_carries_tenant_condition(env):
    """컨텍스트 검사를 통과해도 크로스 테넌트 행이 뽑히지 않게 조회 SQL 자체가 테넌트 조건을 건다."""
    asyncio.run(auth_module._active_user_id_by_email(
        FakeConn(env), EMP_EMAIL, tenant_id=EMPLOYER, context_tenant_id=EMPLOYER
    ))
    sql = next(q for q in env.sql if "FROM saas_users u" in q)
    assert "u.default_tenant_id = $2::uuid" in sql
    assert "tm.tenant_id = $2::uuid" in sql
    assert "ti.tenant_id = $2::uuid" in sql
    assert "pt.created_by = u.id" in sql


def test_email_lookup_ambiguous_candidates_pick_nobody(env):
    """같은 우선순위 후보가 둘이면 고르지 않는다 — pending_account 로 남긴다."""
    env.users = {"owner@example.com": ADMIN_ID, "Twin@example.com": "user-t1", "twin@example.com": "user-t2"}
    env.defaults.update({"user-t1": None, "user-t2": None})

    result = _link_by_email(env, tenant_id=EMPLOYER, context_tenant_id=EMPLOYER, email="twin@example.com")

    assert result["status"] == "pending_account"
    assert not any(key[0] == EMPLOYER and key[1].startswith("user-t") for key in env.memberships)


def test_review_passes_jwt_tenant_as_lookup_context(env, monkeypatch):
    _set_join_request(requester_user_id=None, requester_email=None)
    seen: dict = {}
    original = auth_module.link_employee_tenant_membership

    async def spy(**kwargs):
        seen.update(kwargs)
        return await original(**kwargs)

    monkeypatch.setattr(auth_module, "link_employee_tenant_membership", spy)
    assert _review("approved")["membership"]["status"] == "linked"
    assert seen["allow_email_lookup"] is True
    assert seen["context_tenant_id"] == EMPLOYER == seen["tenant_id"]


def test_email_lookup_approval_requires_business_of_jwt_tenant(env):
    """사업자 없는(귀속 확인 불가) 가입요청은 이메일 조회 승인 전에 막는다 — 아무것도 저장하지 않는다."""
    _set_join_request(requester_user_id=None, requester_email=None, business_id="")

    with pytest.raises(HTTPException) as exc:
        _review("approved")

    assert exc.value.status_code == 400
    assert svc._find(svc._read_file_rows("employee_join_requests"), "join-hayh")["status"] == "pending"
    assert (EMPLOYER, EMP_ID) not in env.memberships


def test_employee_membership_context_rejects_other_tenant_record_and_missing_context(env):
    record = dict(svc._read_file_rows("employee_join_requests")[0])
    assert svc._require_employee_membership_context(record, ADMIN) == EMPLOYER
    with pytest.raises(HTTPException) as other:
        svc._require_employee_membership_context({**record, "tenant_id": OTHER}, ADMIN)
    assert other.value.status_code == 404
    with pytest.raises(HTTPException) as missing:
        svc._require_employee_membership_context(record, {**ADMIN, "current_membership": None})
    assert missing.value.status_code == 403


def test_account_email_changed_since_request_is_identity_mismatch(env):
    env.users = {"owner@example.com": ADMIN_ID, "renamed@example.com": EMP_ID}

    result = _review("approved")

    assert result["membership"]["status"] == "identity_mismatch"
    assert (EMPLOYER, EMP_ID) not in env.memberships


def test_upsert_join_request_binds_requester_only_for_own_email(env):
    employee = _user(EMPLOYER, "member")
    own = svc.upsert_join_request({"name": EMP_NAME, "email": EMP_EMAIL, "branch": "열정국밥_미아점"}, employee)
    assert own["requester_user_id"] == EMP_ID and own["requester_email"] == EMP_EMAIL
    assert svc._join_request_account_id(own) == EMP_ID

    forged = svc.upsert_join_request(
        {"name": "피해자", "email": "victim@example.com", "branch": "열정국밥_미아점"}, employee
    )
    assert "requester_user_id" not in forged
    assert forged["registered_by"] == EMP_EMAIL
    assert svc._join_request_account_id(forged) == ""

    # 관리자가 같은 직원 요청을 고쳐도 본인이 남긴 신원 고정은 유지된다.
    edited = svc.upsert_join_request({"name": EMP_NAME, "email": EMP_EMAIL, "memo": "메모"}, ADMIN)
    assert edited["requester_user_id"] == EMP_ID


def test_approval_without_account_stays_pending_and_does_not_fail(env):
    del env.users[EMP_EMAIL]

    result = _review("approved")

    assert result["request"]["status"] == "approved"
    assert result["membership"]["status"] == "pending_account"
    assert (EMPLOYER, EMP_ID) not in env.memberships
    assert _audit()[0]["classification"] == "membership_pending"


def test_membership_failure_does_not_fail_approval(env, monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("auth db down")

    monkeypatch.setattr(auth_module, "link_employee_tenant_membership", boom)

    result = _review("approved")

    assert result["request"]["status"] == "approved"
    assert result["membership"]["status"] == "error"
    assert _audit()[0]["classification"] == "membership_failed"


def test_non_admin_cannot_approve_and_no_membership_is_created(env):
    with pytest.raises(HTTPException) as exc:
        _review("approved", _user(EMPLOYER, "member", user_id="user-x", email="x@example.com"))
    assert exc.value.status_code == 403
    assert (EMPLOYER, EMP_ID) not in env.memberships


def test_membership_sql_is_idempotent_and_does_not_demote():
    source = inspect.getsource(auth_module.upsert_tenant_membership)
    assert "ON CONFLICT (tenant_id, user_id) DO UPDATE" in source
    assert "tenant_memberships.role IN ('owner', 'admin')" in source
    assert "tenant_memberships.status = 'active'" in source
    revoke = inspect.getsource(auth_module.revoke_employee_tenant_membership)
    assert "SET status = 'removed'" in revoke
    assert "DELETE" not in revoke


def test_invite_acceptance_reuses_shared_membership_upsert():
    source = inspect.getsource(auth_module.accept_tenant_invite)
    assert "upsert_tenant_membership(" in source
    assert "INSERT INTO tenant_memberships" not in source


@pytest.mark.parametrize("existing_role", [None, "owner", "viewer"])
def test_invite_acceptance_behaviour_is_unchanged(monkeypatch, existing_role):
    """리뷰 지적 4: 공용 upsert 로 바꾼 뒤에도 초대 수락의 동작·응답이 예전과 같다.

    - invited_by 는 초대 행의 값 그대로(saas_users FK 라 서브쿼리를 거쳐도 같은 값)
    - 초대 역할이 기존 역할을 덮어쓴다(preserve_elevated_role=False — 예전 SET role = EXCLUDED.role)
    - 응답 membership 키는 예전 RETURNING 그대로(membership_id, tenant_id, role, status)
    """
    from datetime import datetime, timedelta, timezone

    db = FakeAuthDB()
    db.users["invitee@example.com"] = "user-invitee"
    if existing_role:
        db.memberships[(EMPLOYER, "user-invitee")] = {
            "role": existing_role, "status": "removed", "deleted": False, "invited_by": None,
        }
    invite = {
        "invite_id": "inv-1", "tenant_id": EMPLOYER, "email": "invitee@example.com", "role": "admin",
        "status": "pending", "expires_at": datetime.now(timezone.utc) + timedelta(days=1), "invited_by": ADMIN_ID,
    }
    executed: list[str] = []

    class InviteConn(FakeConn):
        async def fetchrow(self, sql, *args):
            if "FROM tenant_invites" in sql:
                return invite
            if "SELECT id, email, name FROM saas_users" in sql:
                return {"id": args[0], "email": "invitee@example.com", "name": "초대"}
            return await super().fetchrow(sql, *args)

        async def execute(self, sql, *args):
            executed.append(sql)
            return "UPDATE 1"

    class InvitePool(FakePool):
        @asynccontextmanager
        async def acquire(self):
            yield InviteConn(self.db)

    async def ensure_pool():
        return InvitePool(db)

    async def noop(*args, **kwargs):
        return None

    async def existing_user(email):
        return {"id": "user-invitee", "email": email}

    async def authed(email, password):
        return {"id": "user-invitee", "email": email}

    async def workspace(**kwargs):
        return {"id": "ws"}

    monkeypatch.setattr(auth_module, "_ensure_pool", ensure_pool)
    monkeypatch.setattr(auth_module, "require_saas_schema_ready", noop)
    monkeypatch.setattr(auth_module, "get_saas_user_by_email", existing_user)
    monkeypatch.setattr(auth_module, "authenticate_saas_user", authed)
    monkeypatch.setattr(auth_module, "ensure_default_customer_workspace", workspace)

    result = asyncio.run(auth_module.accept_tenant_invite(token="tok", password="pw"))

    assert set(result["membership"]) == {"membership_id", "tenant_id", "role", "status"}
    assert result["membership"]["role"] == "admin" and result["membership"]["status"] == "active"
    stored = db.memberships[(EMPLOYER, "user-invitee")]
    assert stored["role"] == "admin" and stored["status"] == "active"
    if existing_role is None:
        assert stored["invited_by"] == ADMIN_ID
    assert any("SET status = 'accepted'" in sql for sql in executed)
    assert any("UPDATE saas_users SET default_tenant_id" in sql for sql in executed)


def test_review_route_uses_membership_wrapper():
    source = inspect.getsource(api.review_employee_join_request)
    assert "review_join_request_with_membership" in source


# --- 리뷰 지적 8: 감사 I/O 는 이벤트 루프 밖, 동시 기록에서 행 유실 없음 -------
def test_membership_audit_io_runs_off_the_event_loop(env, monkeypatch):
    import threading

    seen = []
    original = svc._record_membership_audit_sync

    def spy(row):
        seen.append(threading.get_ident())
        return original(row)

    monkeypatch.setattr(svc, "_record_membership_audit_sync", spy)
    loop_thread = []

    async def run():
        loop_thread.append(threading.get_ident())
        return await svc.review_join_request_with_membership("join-hayh", "approved", "", ADMIN)

    asyncio.run(run())
    assert seen and seen[0] != loop_thread[0]


def test_concurrent_file_audit_appends_are_not_lost(env):
    from concurrent.futures import ThreadPoolExecutor

    rows = [{"id": f"a-{i}", "join_request_id": f"j-{i}"} for i in range(40)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(svc._record_membership_audit_sync, rows))
    assert sorted(row["id"] for row in _audit()) == sorted(row["id"] for row in rows)


def test_db_audit_failure_falls_back_to_file_ledger(env, monkeypatch):
    """리뷰 지적 2: 감사 마이그레이션(HOLD) 적용 전 DB INSERT 가 실패해도 이벤트를 잃지 않는다."""
    monkeypatch.setattr(svc, "_db_available", lambda: True)
    monkeypatch.setattr(svc, "_run_db", _disable_db)  # 실패 = None

    assert svc._record_membership_audit_sync({"id": "a-1", "join_request_id": "j-1"}) is True
    rows = _audit()
    assert rows[0]["id"] == "a-1" and rows[0]["db_insert_failed"] is True


def test_concurrent_reviews_are_serialized(env, monkeypatch):
    """리뷰 지적 5: 직전 상태 읽기 → 저장 → 멤버십 동기화가 다른 검토와 끼어들지 않는다."""
    import threading
    import time as _time

    spans: list[tuple[float, float]] = []
    original = svc.sync_employee_tenant_membership

    async def slow_sync(*args, **kwargs):
        start = _time.monotonic()
        await asyncio.sleep(0.15)
        result = await original(*args, **kwargs)
        spans.append((start, _time.monotonic()))
        return result

    monkeypatch.setattr(svc, "sync_employee_tenant_membership", slow_sync)
    results: dict[str, dict] = {}

    def run(action):
        results[action] = _review(action)

    threads = [threading.Thread(target=run, args=(a,)) for a in ("approved", "rejected")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert len(spans) == 2
    (a0, a1), (b0, b1) = sorted(spans)
    assert a1 <= b0, "두 검토의 멤버십 동기화가 겹쳤다"
    final = svc._find(svc._read_file_rows("employee_join_requests"), "join-hayh")["status"]
    member = env.memberships.get((EMPLOYER, EMP_ID))
    # 어느 순서로 끝나든 '반려됐는데 활성 멤버' 는 남지 않는다.
    if final == "rejected":
        assert member is None or member["status"] == "removed"
    else:
        assert member and member["status"] == "active"


# --- R3 실결함 2: flock → DB advisory xact lock --------------------------------
def test_membership_serialization_uses_db_advisory_lock_not_flock():
    """blue/green 두 컨테이너는 파일시스템이 달라 flock 이 서로를 못 본다 — 락은 인증 DB 에."""
    assert not hasattr(svc, "_acquire_join_review_lock")
    assert not hasattr(svc, "_release_join_review_lock")
    assert not hasattr(svc, "JOIN_REVIEW_LOCK")
    review = inspect.getsource(svc.review_join_request_with_membership)
    assert "fcntl" not in review and "_join_review_lock" not in review
    assert "employee_membership_lock(" in review
    assert "pg_advisory_xact_lock($1::bigint)" in inspect.getsource(auth_module._lock_employee_membership)
    for fn in (auth_module.link_employee_tenant_membership, auth_module.revoke_employee_tenant_membership):
        assert "_lock_employee_membership(conn, tenant_id" in inspect.getsource(fn)
    # 최종 보장(UNIQUE(tenant_id, user_id))은 락과 함께 유지한다.
    assert "ON CONFLICT (tenant_id, user_id)" in inspect.getsource(auth_module.upsert_tenant_membership)


def test_membership_lock_key_is_stable_bigint_per_tenant_and_email():
    key = auth_module.employee_membership_lock_key(EMPLOYER, EMP_EMAIL)
    assert -(2 ** 63) <= key < 2 ** 63
    assert key == auth_module.employee_membership_lock_key(EMPLOYER.upper(), "  DUDGNS3738@Naver.com ")
    assert key != auth_module.employee_membership_lock_key(OTHER, EMP_EMAIL)
    assert key != auth_module.employee_membership_lock_key(EMPLOYER, "other@example.com")
    # 프로세스마다 바뀌는 내장 hash() 가 아니다 — 고정 해시(sha256 앞 8바이트).
    assert "hashlib.sha256(" in inspect.getsource(auth_module.employee_membership_lock_key)


def test_same_tenant_email_concurrent_links_create_one_membership(env):
    """동일 (tenant, email) 두 번 — 동시에 불러도 멤버십은 1건, 둘 다 같은 advisory 키를 잡는다."""
    _set_join_request(requester_user_id=None, requester_email=None)
    results: list[dict] = []

    def run():
        results.append(_link_by_email(env, tenant_id=EMPLOYER, context_tenant_id=EMPLOYER))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert sorted(r["status"] for r in results) == ["linked", "unchanged"]
    assert [key for key in env.memberships if key[1] == EMP_ID] == [(EMPLOYER, EMP_ID)]
    assert env.memberships[(EMPLOYER, EMP_ID)] == {
        "role": "member", "status": "active", "deleted": False, "invited_by": ADMIN_ID,
    }
    expected = auth_module.employee_membership_lock_key(EMPLOYER, EMP_EMAIL)
    assert env.lock_log == [expected, expected]


def test_review_holds_advisory_lock_around_state_read_and_membership(env):
    """검토 전체가 같은 키의 xact lock 안에서 돈다 — 락 SQL 이 멤버십 SQL 보다 먼저, 끝나면 풀린다."""
    _review("approved")
    key = auth_module.employee_membership_lock_key(EMPLOYER, EMP_EMAIL)
    assert env.lock_log and set(env.lock_log) == {key}
    first_lock = next(i for i, q in enumerate(env.sql) if "pg_advisory_xact_lock" in q)
    first_membership = next(i for i, q in enumerate(env.sql) if "INSERT INTO tenant_memberships" in q)
    assert first_lock < first_membership
    lock = env.advisory_lock(key)
    assert lock.acquire(blocking=False)  # 트랜잭션이 끝나 풀렸다
    lock.release()
    _review("rejected")
    assert set(env.lock_log) == {key}
    assert env.memberships[(EMPLOYER, EMP_ID)]["status"] == "removed"


def test_review_proceeds_without_lock_when_auth_db_unreachable(env, monkeypatch):
    """인증 DB 에 닿지 못해도 승인 자체는 되돌리지 않는다 — 멤버십은 error 로 감사에 남는다."""
    async def down():
        raise RuntimeError("auth db down")

    monkeypatch.setattr(auth_module, "_ensure_pool", down)
    result = _review("approved")
    assert result["request"]["status"] == "approved"
    assert result["membership"]["status"] == "error"
    assert _audit()[0]["classification"] == "membership_failed"


def test_db_audit_goes_through_run_db_in_worker(monkeypatch):
    """DB 모드에서도 async 경로가 직접 connect 하지 않는다 — 워커 스레드의 _run_db 로만."""
    source = inspect.getsource(svc.record_membership_audit)
    assert "asyncio.to_thread(_record_membership_audit_sync" in source
    assert "_run_db(_db_insert_membership_audit(row))" in inspect.getsource(svc._record_membership_audit_sync)


# ---------------------------------------------------------------------------
# 2. 로그인 시작 테넌트
#
# 로그인 때마다 판정하지 않는다(리뷰 지적 6). 승인이 멤버십을 새로 만든 그 한 번,
# default 가 본인 혼자인 개인 워크스페이스일 때만 default_tenant_id 를 고용주로
# 옮기고, 로그인은 기존 규칙(default 존중)을 그대로 쓴다.
# ---------------------------------------------------------------------------
@pytest.fixture
def no_new_tenant(monkeypatch):
    async def ensure_customer(**kwargs):  # pragma: no cover - 호출되면 실패
        raise AssertionError("소속 조직이 있는 사용자에게 새 조직을 만들면 안 된다")

    monkeypatch.setattr(auth_module, "ensure_customer_tenant_for_user", ensure_customer)


def _listing_from(db: FakeAuthDB):
    async def list_tenants(user_id):
        rows = []
        for (tenant_id, uid), m in db.memberships.items():
            if uid == user_id and m["status"] == "active" and not m["deleted"]:
                rows.append({"tenant_id": tenant_id, "kind": db.tenants[tenant_id], "role": m["role"]})
        return rows

    return list_tenants


def _login_tenant(db: FakeAuthDB, user_id: str = EMP_ID, email: str = EMP_EMAIL, role: str = "user"):
    return asyncio.run(auth_module.resolve_login_tenant_for_user(
        {"id": user_id, "email": email, "role": role, "default_tenant_id": db.defaults.get(user_id)}
    ))


@pytest.fixture
def login_env(env, monkeypatch, no_new_tenant):
    env.memberships[(PERSONAL, EMP_ID)] = {"role": "owner", "status": "active", "deleted": False, "invited_by": None}
    monkeypatch.setattr(auth_module, "list_user_tenants", _listing_from(env))
    return env


def test_employee_with_personal_default_starts_in_employer_tenant(login_env):
    assert _login_tenant(login_env) == PERSONAL  # 승인 전 — 사고 당시 상태
    result = _review("approved")

    assert result["membership"]["default_tenant"] == {"before": PERSONAL, "after": EMPLOYER}
    assert login_env.defaults[EMP_ID] == EMPLOYER
    assert _login_tenant(login_env) == EMPLOYER


def test_explicit_switch_back_to_personal_survives_reapproval(login_env):
    """재승인(unchanged)은 default 를 다시 건드리지 않는다 — 사용자가 고른 개인 워크스페이스가 이긴다."""
    _review("approved")
    login_env.defaults[EMP_ID] = PERSONAL  # /auth/tenants/{id}/switch 가 하는 일

    assert _review("approved")["membership"]["status"] == "unchanged"
    assert login_env.defaults[EMP_ID] == PERSONAL
    assert _login_tenant(login_env) == PERSONAL


def test_non_personal_default_is_not_moved(login_env):
    """default 가 개인 워크스페이스가 아니면(본인 사업장 등) 승인이 시작 테넌트를 바꾸지 않는다."""
    login_env.personal.clear()

    result = _review("approved")

    assert result["membership"]["status"] == "linked"
    assert result["membership"]["default_tenant"] is None
    assert login_env.defaults[EMP_ID] == PERSONAL
    assert _login_tenant(login_env) == PERSONAL


def test_invited_member_elsewhere_keeps_personal_default(login_env):
    """일반 초대 member(직원 승인 경로 밖)는 로그인 테넌트가 바뀌지 않는다."""
    login_env.memberships[(OTHER, EMP_ID)] = {"role": "member", "status": "active", "deleted": False, "invited_by": None}
    assert _login_tenant(login_env) == PERSONAL


def test_revoked_employee_falls_back_from_employer_default(login_env, monkeypatch):
    """회수 후 default(고용주)가 소속이 아니면 기존 폴백 — 기존 활성 customer 소속(개인 워크스페이스)."""
    async def ensure_customer(**kwargs):
        rows = await _listing_from(login_env)(kwargs["user_id"])
        return next(row for row in rows if row["kind"] == "customer")

    monkeypatch.setattr(auth_module, "ensure_customer_tenant_for_user", ensure_customer)
    _review("approved")
    _review("rejected")
    assert _login_tenant(login_env) == PERSONAL


def test_login_resolution_has_no_employee_heuristic():
    source = inspect.getsource(auth_module.resolve_login_tenant_for_user)
    assert "is_personal_workspace" not in source
    assert not hasattr(auth_module, "_employer_tenant_over_personal_default")
    switch = inspect.getsource(auth_module.switch_user_tenant)
    assert "UPDATE tenant_memberships" not in switch


def _tenants(*rows):
    async def list_tenants(user_id):
        return list(rows)

    return list_tenants


async def test_ceo_default_tenant_is_still_respected(monkeypatch, no_new_tenant):
    """2026-09-22 회귀: CEO 가 고른 열정국밥 조직이 internal 로 되돌아가면 안 된다."""
    monkeypatch.setattr(auth_module, "_is_internal_tenant_principal", lambda email, role: True)
    monkeypatch.setattr(
        auth_module,
        "list_user_tenants",
        _tenants({"tenant_id": INTERNAL, "kind": "internal", "role": "owner"},
                 {"tenant_id": YEOLJEONG, "kind": "customer", "role": "owner"}),
    )
    tenant_id = await auth_module.resolve_login_tenant_for_user(
        {"id": "u-ceo", "email": "moongoby@naver.com", "role": "ceo", "default_tenant_id": YEOLJEONG}
    )
    assert tenant_id == YEOLJEONG


async def test_internal_principal_without_default_still_starts_internal(monkeypatch, no_new_tenant):
    monkeypatch.setattr(auth_module, "_is_internal_tenant_principal", lambda email, role: True)
    monkeypatch.setattr(
        auth_module, "list_user_tenants",
        _tenants({"tenant_id": INTERNAL, "kind": "internal", "role": "owner"},
                 {"tenant_id": EMPLOYER, "kind": "customer", "role": "member"}),
    )
    tenant_id = await auth_module.resolve_login_tenant_for_user(
        {"id": "u-ceo", "email": "moongoby@naver.com", "role": "ceo"}
    )
    assert tenant_id == INTERNAL


def test_default_move_sql_only_targets_sole_owner_personal_workspace():
    source = inspect.getsource(auth_module._move_default_off_personal_workspace)
    assert "t.created_by = $2::text" in source
    assert "tm.role = 'owner'" in source
    # 리뷰 지적 7: 본인 owner 멤버십이 회수·삭제된 곳은 개인 워크스페이스가 아니다.
    assert "tm.status = 'active'" in source
    assert "tm.deleted_at IS NULL" in source
    assert "o.user_id <> $2::text" in source
    link = inspect.getsource(auth_module.link_employee_tenant_membership)
    assert "if not active_before:" in link


# ---------------------------------------------------------------------------
# 3. 게이트
# ---------------------------------------------------------------------------
# 서비스가 만드는 서명 토큰 모양(secrets.token_urlsafe(24) → URL-safe 32자).
TOK = "AbCdEfGhIjKlMnOpQrStUvWxYz012-_9"
assert len(TOK) == 32


def _request(path, method="GET"):
    return SimpleNamespace(url=SimpleNamespace(path=path), method=method)


@pytest.mark.parametrize(
    "method,path",
    [("POST", "/api/v1/yeoljeong-finance/contracts/signing"),
     ("GET", f"/api/v1/yeoljeong-finance/contracts/signing/{TOK}")],
)
async def test_gate_opens_only_signing_routes_for_non_legacy_tenant(monkeypatch, method, path):
    monkeypatch.delenv("OBYS_LEGACY_TENANT_IDS", raising=False)
    user = {"tenant_id": EMPLOYER}
    assert await obys_tenant.require_legacy_obys_access(_request(path, method), user) is user


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/v1/yeoljeong-finance/contracts"),
        ("POST", "/api/v1/yeoljeong-finance/contracts"),
        ("POST", "/api/v1/yeoljeong-finance/contracts/abc/request-signature"),
        ("GET", "/api/v1/yeoljeong-finance/contracts/signing-export"),
        ("POST", f"/api/v1/yeoljeong-finance/contracts/signing/{TOK}"),
        ("GET", "/api/v1/yeoljeong-finance/contracts/signing"),
        # DELETE /contracts/{id}, GET /contracts/{id}/signed-pdf 에 id="signing" 을 넣는 우회
        ("DELETE", "/api/v1/yeoljeong-finance/contracts/signing"),
        ("GET", "/api/v1/yeoljeong-finance/contracts/signing/signed-pdf"),
        ("POST", "/api/v1/yeoljeong-finance/contracts/signing/signed-pdf/regenerate"),
        ("GET", "/api/v1/yeoljeong-finance/employees/invites"),
    ],
)
async def test_gate_keeps_other_contract_routes_closed(monkeypatch, method, path):
    monkeypatch.delenv("OBYS_LEGACY_TENANT_IDS", raising=False)
    with pytest.raises(HTTPException) as exc:
        await obys_tenant.require_legacy_obys_access(_request(path, method), {"tenant_id": EMPLOYER})
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        # 리뷰 지적 5: 기존 prefix 는 예전처럼 startswith 로 판정한다(부분 일치 포함, 동작 변경 없음).
        "/api/v1/yeoljeong-finance/journals",
        "/api/v1/yeoljeong-finance/journals/v-1/reverse",
        "/api/v1/yeoljeong-finance/journals-export",
        "/api/v1/yeoljeong-finance/sessionX",
        "/api/v1/yeoljeong-finance/uploads/commit",
    ],
)
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
async def test_gate_existing_prefixes_keep_startswith_semantics(monkeypatch, path, method):
    monkeypatch.delenv("OBYS_LEGACY_TENANT_IDS", raising=False)
    user = {"tenant_id": EMPLOYER}
    assert await obys_tenant.require_legacy_obys_access(_request(path, method), user) is user


def _first_route(path: str, method: str):
    """FastAPI 가 실제로 고르는 첫 라우트(등록 순서 그대로). 없으면 None."""
    from starlette.routing import Match

    for route in api.router.routes:
        match, _ = route.matches({"type": "http", "path": path, "method": method, "root_path": ""})
        if match == Match.FULL:
            return route
    return None


def test_gate_open_paths_resolve_only_to_signing_handlers():
    """리뷰 지적 6: 게이트가 여는 (메서드, 경로) 는 실제 라우트 표에서 서명 핸들러 둘로만 풀린다.

    obys_finance 의 모든 라우트를 훑어 경로 변수에 'signing'·토큰 모양 값을 넣고, 그중
    게이트가 비레거시 테넌트에 여는 요청이 서명 조회·서명 외 다른 핸들러로 가지 않는지
    FastAPI 매칭으로 확인한다. /contracts/{id}/<sub> 가 새로 생겨도 이 테스트가 잡는다.
    """
    import re as _re

    signing_handlers = {
        route.endpoint
        for route in api.router.routes
        if getattr(route, "path", "") in (
            "/yeoljeong-finance/contracts/signing/{token}",
            "/yeoljeong-finance/contracts/signing",
        )
    }
    assert signing_handlers == {api.get_contract_signing, api.sign_contract}

    probes = set()
    for route in api.router.routes:
        path = "/api/v1" + getattr(route, "path", "")
        for value in ("signing", TOK, "signed-pdf"):
            probes.add(_re.sub(r"\{[^}]+\}", value, path))
    probes.update({
        "/api/v1/yeoljeong-finance/contracts/signing/signed-pdf",
        f"/api/v1/yeoljeong-finance/contracts/signing/{TOK}/signed-pdf",
        f"/api/v1/yeoljeong-finance/contracts/signing/{TOK}/signed-pdf/regenerate",
    })
    opened = 0
    for probe in sorted(probes):
        if not probe.startswith("/api/v1/yeoljeong-finance/contracts"):
            continue
        for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            if not obys_tenant._is_contract_signing_route(method, probe):
                continue
            route = _first_route(probe[len("/api/v1"):], method)
            assert route is None or route.endpoint in signing_handlers, (method, probe, route.path)
            opened += 1
    assert opened >= 2


def test_gate_did_not_widen_legacy_allow_list(monkeypatch):
    monkeypatch.delenv("OBYS_LEGACY_TENANT_IDS", raising=False)
    assert obys_tenant._allowed_tenant_ids() == frozenset({YEOLJEONG})


# --- R3 실결함 3: 서명 게이트 GET 하위경로는 명시적 화이트리스트 ----------------------
_SIGNING = "/api/v1/yeoljeong-finance/contracts/signing"
_LOOKALIKES = [
    ("GET", f"{_SIGNING}-extra"),
    ("GET", f"{_SIGNING}-extra/{TOK}"),
    ("GET", f"{_SIGNING}/{TOK}-extra"),
    ("GET", f"{_SIGNING}/{TOK}/sub"),
    ("GET", f"{_SIGNING}/{TOK}/"),
    ("GET", f"{_SIGNING}/{TOK[:-1]}"),
    ("GET", f"{_SIGNING}/{TOK}%2Fsub"),
    ("GET", f"{_SIGNING}/"),
    ("GET", f"{_SIGNING}/sub/{TOK}"),
    ("GET", f"{_SIGNING}/{TOK}\n"),
    ("POST", f"{_SIGNING}-extra"),
    ("POST", f"{_SIGNING}/"),
    ("POST", f"{_SIGNING}/sub"),
    ("PUT", f"{_SIGNING}/{TOK}"),
    ("PATCH", _SIGNING),
]


@pytest.mark.parametrize("method,path", _LOOKALIKES)
async def test_gate_whitelist_does_not_open_lookalike_paths(monkeypatch, method, path):
    monkeypatch.delenv("OBYS_LEGACY_TENANT_IDS", raising=False)
    assert not obys_tenant._is_contract_signing_route(method, path)
    with pytest.raises(HTTPException) as exc:
        await obys_tenant.require_legacy_obys_access(_request(path, method), {"tenant_id": EMPLOYER})
    assert exc.value.status_code == 403


@pytest.mark.parametrize("method,path", _LOOKALIKES)
async def test_gate_whitelist_miss_keeps_legacy_tenant_access(monkeypatch, method, path):
    """화이트리스트 밖 경로는 기존 판정(레거시 테넌트 허용목록)을 그대로 탄다 — 새 403 없음."""
    monkeypatch.delenv("OBYS_LEGACY_TENANT_IDS", raising=False)
    user = {"tenant_id": YEOLJEONG}
    assert await obys_tenant.require_legacy_obys_access(_request(path, method), user) is user


def test_gate_whitelist_accepts_every_service_generated_token():
    import secrets as _secrets

    for _ in range(200):
        token = _secrets.token_urlsafe(24)
        assert obys_tenant._is_contract_signing_route("GET", f"{_SIGNING}/{token}"), token
    assert "secrets.token_urlsafe(24)" in inspect.getsource(svc.request_contract_signature)
    assert obys_tenant._is_contract_signing_route("POST", _SIGNING)
    assert obys_tenant._is_contract_signing_route("post", _SIGNING)


def test_gate_whitelist_is_exact_not_prefix():
    source = inspect.getsource(obys_tenant._is_contract_signing_route)
    assert "fullmatch" in source and "startswith" not in source


def test_signing_lookups_are_bound_to_jwt_tenant():
    """prefix 추가의 전제: 토큰 조회는 _read_hr(테넌트 SQL 스코프) 로만, 레코드는 다시 대조."""
    assert "_sign_contract_locked(payload, user)" in inspect.getsource(svc.sign_contract)
    for fn in (svc.get_contract_by_token, svc._sign_contract_locked):
        source = inspect.getsource(fn)
        assert '_read_hr("contracts", user)' in source
        assert "_contract_signer_email(contract, user)" in source
    assert "_require_hr_record(contract, user" in inspect.getsource(svc._contract_signer_email)
    assert "_is_admin(user)" in inspect.getsource(svc._contract_signer_email)


# ---------------------------------------------------------------------------
# 4. 끝까지: 승인 직원이 requested 계약서를 조회·서명
# ---------------------------------------------------------------------------
def _png() -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (320, 120), "white")
    ImageDraw.Draw(image).line((12, 96, 300, 24), fill="black", width=5)
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _contract_payload():
    return {
        "employee_request_id": "join-hayh", "business_id": "biz-mia", "branch": "열정국밥_미아점",
        "contract_type": "regular", "employment_tax_type": "four_insurance",
        "start_date": "2026-07-22", "contract_date": "2026-07-22", "wage_type": "monthly",
        "wage": 2800000, "workplace": "열정국밥 미아점", "job_description": "매장 운영",
        "work_time": "09:00-18:00", "rest_time": "12:00-13:00", "weekly_hours": "주 40시간",
        "work_days": "주 5일", "holidays": "매주 일요일", "pay_date": "매월 10일",
        "pay_method": "계좌이체", "wage_composition": "기본급 및 법정수당",
        "overtime_terms": "사전 승인 및 법정 가산수당", "leave_terms": "법정 연차유급휴가",
        "insurance_terms": "4대보험 법정 기준 적용",
    }


def _sign_body(token):
    return {
        "token": token,
        "signer_name": EMP_NAME,
        "consent": True,
        "consent_version": svc.CONTRACT_SIGNATURE_CONSENT_VERSION,
        "signature_data_uri": "data:image/png;base64," + base64.b64encode(_png()).decode("ascii"),
    }


def _client(user):
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    app.dependency_overrides[api.get_current_user] = lambda: user
    return TestClient(app)


def _employee_context_after_login(db: FakeAuthDB) -> dict:
    """승인으로 생긴 멤버십 행으로 get_current_user 가 만들 컨텍스트."""
    membership = db.memberships[(EMPLOYER, EMP_ID)]
    assert membership["status"] == "active"
    return _user(EMPLOYER, membership["role"])


def _requested_token():
    saved = svc.save_contract(_contract_payload(), ADMIN)
    return svc.request_contract_signature(saved["id"], ADMIN)["sign_token"]


def test_approved_employee_views_and_signs_requested_contract(env):
    _review("approved")
    employee = _employee_context_after_login(env)
    token = _requested_token()
    client = _client(employee)

    viewed = client.get(f"/api/v1/yeoljeong-finance/contracts/signing/{token}")
    assert viewed.status_code == 200, viewed.text
    assert viewed.json()["contract"]["status"] == "requested"

    signed = client.post("/api/v1/yeoljeong-finance/contracts/signing", json=_sign_body(token))
    assert signed.status_code == 200, signed.text
    contract = signed.json()["contract"]
    assert contract["status"] == "signed"
    assert contract["signed_at"]
    assert len(contract["signature_sha256"]) == 64
    assert len(contract["signed_snapshot_sha256"]) == 64
    assert contract["signer_email"] == EMP_EMAIL
    stored = next(row for row in svc._read_file_rows("contracts") if row["id"] == contract["id"])
    assert stored["status"] == "signed"
    assert stored["tenant_id"] == EMPLOYER


def test_employee_lookalike_signing_path_is_not_opened_by_gate(env):
    """화이트리스트 경로는 직원으로 200(위 테스트), 같은 라우트에 걸리는 유사 경로는 게이트가 403."""
    _review("approved")
    employee = _employee_context_after_login(env)
    token = _requested_token()
    client = _client(employee)

    assert client.get(f"/api/v1/yeoljeong-finance/contracts/signing/{token}").status_code == 200
    blocked = client.get(f"/api/v1/yeoljeong-finance/contracts/signing/{token}-extra")
    assert blocked.status_code == 403
    assert "레거시 데이터 접근 권한" in blocked.json()["detail"]
    assert svc._read_file_rows("contracts")[0]["status"] == "requested"


def test_employee_in_personal_workspace_still_cannot_reach_contract(env):
    """개인 워크스페이스(owner) 컨텍스트로 오면 — 이번 사고의 원래 상태 — 403."""
    _review("approved")
    token = _requested_token()
    client = _client(_user(PERSONAL, "owner"))

    assert client.get(f"/api/v1/yeoljeong-finance/contracts/signing/{token}").status_code == 403
    assert client.post("/api/v1/yeoljeong-finance/contracts/signing", json=_sign_body(token)).status_code == 403
    stored = svc._read_file_rows("contracts")[0]
    assert stored["status"] == "requested"


def test_admin_still_cannot_sign_on_behalf_of_employee(env):
    _review("approved")
    token = _requested_token()
    client = _client(ADMIN)

    assert client.get(f"/api/v1/yeoljeong-finance/contracts/signing/{token}").status_code == 403
    response = client.post("/api/v1/yeoljeong-finance/contracts/signing", json=_sign_body(token))
    assert response.status_code == 403
    assert "관리자는 직원 대신" in response.json()["detail"]


def test_other_tenant_account_gets_403_for_view_and_sign(env):
    _review("approved")
    token = _requested_token()
    # 같은 이메일이라도 다른 테넌트 컨텍스트면 계약서가 보이지 않는다.
    for user in (_user(OTHER, "member"), _user(OTHER, "member", user_id="user-z", email="z@example.com")):
        client = _client(user)
        assert client.get(f"/api/v1/yeoljeong-finance/contracts/signing/{token}").status_code == 403
        assert client.post("/api/v1/yeoljeong-finance/contracts/signing", json=_sign_body(token)).status_code == 403
    assert svc._read_file_rows("contracts")[0]["status"] == "requested"


def test_revoked_employee_cannot_sign(env):
    _review("approved")
    token = _requested_token()
    _review("rejected")
    assert env.memberships[(EMPLOYER, EMP_ID)]["status"] == "removed"
    # 회수 후에는 get_current_user 가 고용주 테넌트 컨텍스트를 만들지 못한다(멤버십 없음).
    # 남은 경로는 개인 워크스페이스뿐이고, 거기서는 계약서가 보이지 않는다.
    client = _client(_user(PERSONAL, "owner"))
    assert client.get(f"/api/v1/yeoljeong-finance/contracts/signing/{token}").status_code == 403


# ---------------------------------------------------------------------------
# 5. 백필 스크립트 판정
# ---------------------------------------------------------------------------
def test_backfill_plan_matches_service_rules():
    from scripts import obys_employee_membership_backfill as backfill

    active = lambda role: {"role": role, "status": "active", "deleted": False}  # noqa: E731
    assert backfill.plan_action(tenant_kind="internal", user_id="u", before=None) == "skipped"
    assert backfill.plan_action(tenant_kind="customer", user_id=None, before=None) == "pending_account"
    assert backfill.plan_action(tenant_kind="customer", user_id="u", before=None) == "link"
    assert backfill.plan_action(tenant_kind="customer", user_id="u", before=active("member")) == "unchanged"
    assert backfill.plan_action(tenant_kind="customer", user_id="u", before=active("owner")) == "kept_elevated"
    assert backfill.plan_action(tenant_kind="customer", user_id="u", before=active("admin")) == "kept_elevated"
    assert backfill.plan_action(
        tenant_kind="customer", user_id="u", before={"role": "admin", "status": "removed", "deleted": False}
    ) == "link"
    rows = [{"tenant_id": EMPLOYER, "email": "a@x"}, {"tenant_id": EMPLOYER, "email": "a@x"},
            {"tenant_id": EMPLOYER, "email": ""}, {"tenant_id": OTHER, "email": "a@x"}]
    assert backfill.dedupe_targets(rows) == [rows[0], rows[3]]


def test_backfill_defaults_to_dry_run():
    from scripts import obys_employee_membership_backfill as backfill

    source = inspect.getsource(backfill.run)
    assert "if args.apply and action == \"link\"" in source
    assert "MARK_LINK_SQL" in source and "_move_default_off_personal_workspace" in source
    assert "request_payload->>'requester_user_id'" in backfill.APPROVED_JOIN_REQUESTS_SQL
    assert 'parser.add_argument("--apply", action="store_true"' in inspect.getsource(backfill.main)


def test_backfill_dry_run_prints_targets_and_writes_nothing(monkeypatch, capsys):
    """dry-run 은 대상·변경 전후를 출력하고 upsert/감사를 부르지 않는다."""
    import sys
    import types

    from scripts import obys_employee_membership_backfill as backfill

    rows = [
        {"id": "j1", "tenant_id": YEOLJEONG, "business_id": "biz-mia", "email": EMP_EMAIL, "reviewed_by": "owner@example.com"},
        {"id": "j2", "tenant_id": YEOLJEONG, "business_id": "biz-mia", "email": "beullingma3@gmail.com", "reviewed_by": ""},
        {"id": "j3", "tenant_id": YEOLJEONG, "business_id": "biz-mia", "email": "not-signed-up@example.com", "reviewed_by": ""},
    ]
    users = {EMP_EMAIL: EMP_ID, "beullingma3@gmail.com": "user-admin", "owner@example.com": ADMIN_ID}

    class Conn:
        @asynccontextmanager
        async def transaction(self):
            yield

        async def fetch(self, sql, *args):
            if "FROM saas_users u" in sql:
                # 테넌트 조건 조회 — 컨텍스트는 가입요청 행의 tenant_id 다.
                assert args[1] == YEOLJEONG
                uid = users.get(args[0])
                return [{"id": uid, "bound_to_tenant": False}] if uid else []
            return rows

        async def execute(self, sql, *args):
            raise AssertionError("dry-run 이 advisory lock·쓰기를 했다")

        async def fetchval(self, sql, *args):
            return "customer" if "FROM tenants" in sql else users.get(args[0])

        async def fetchrow(self, sql, *args):
            assert "INSERT INTO" not in sql and "SET status" not in sql
            return {"role": "owner", "status": "active", "deleted": False} if args[1] == "user-admin" else None

        async def close(self):
            return None

    async def connect(*args, **kwargs):
        return Conn()

    async def forbidden(*args, **kwargs):  # pragma: no cover - 호출되면 실패
        raise AssertionError("dry-run 이 쓰기를 했다")

    monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=connect))
    monkeypatch.setattr(auth_module, "upsert_tenant_membership", forbidden)
    monkeypatch.setattr(backfill, "_record_audit", forbidden)

    assert backfill.main(["--obys-dsn", "obys", "--auth-dsn", "auth"]) == 0
    out = capsys.readouterr().out
    assert "[DRY-RUN]" in out
    assert f"{EMP_EMAIL}" in out and "link" in out and "member/active (예정)" in out
    assert "kept_elevated" in out and "pending_account" in out
    assert '"link": 1' in out
    print(out)  # -rP 로 실행하면 dry-run 출력 예시를 그대로 볼 수 있다
