"""ACCT-OBYS-JOIN-AUTO-APPROVE-REASSIGN-20261009 — 가입요청 자동승인 + 승인 직원의 사업자·지점 변경.

CEO 지시(2026-10-09): 가입요청 승인은 자동으로, 잘못 가입해도 사업자·지점은 바꿀 수 있게.
오비서 DB·AADS 인증 DB 는 인메모리 가짜로 흉내 낸다(운영 DB 미접촉). 감사는 가짜 DB 가 INSERT 를
받지 못해 파일 원장 폴백을 타므로, 파일 원장에서 읽어 검증한다.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app import auth as auth_module  # noqa: E402
from app.api import obys_finance as api  # noqa: E402
from app.services import yeoljeong_ops_service  # noqa: E402

svc = api.svc

EMPLOYER = "15055cac-71b0-45ec-b714-7093dde189ff"
PERSONAL = "d184a18c-78c8-45c7-aee7-bfe79fadf95f"
OTHER_TENANT = "2a1b3c4d-0000-4000-8000-000000000001"
OTHER_BIZ = "biz-other-tenant"

EMP_EMAIL = "new-hire@example.com"
EMP_ID = "user-new-hire"
ADMIN_EMAIL = "owner@example.com"
ADMIN_ID = "user-owner"


def _user(tenant_id: str, role: str, *, email: str, user_id: str, user_role: str = "user") -> dict:
    return {
        "user_id": user_id,
        "email": email,
        "is_admin": False,
        "tenant_id": tenant_id,
        "tenant_role": role,
        "user_role": user_role,
        "current_membership": {"tenant_id": tenant_id, "status": "active", "role": role},
    }


ADMIN = _user(EMPLOYER, "owner", email=ADMIN_EMAIL, user_id=ADMIN_ID)
EMPLOYEE = _user(PERSONAL, "owner", email=EMP_EMAIL, user_id=EMP_ID)
MEMBER = _user(EMPLOYER, "member", email=EMP_EMAIL, user_id=EMP_ID)
PLATFORM_ADMIN = _user(EMPLOYER, "owner", email="ceo@example.com", user_id="user-ceo", user_role="ceo")


class FakeWorld:
    def __init__(self):
        self.ledgers: dict[str, list[dict]] = {"employee_join_requests": [], "contracts": []}
        self.mapping = {"biz-sungshin": EMPLOYER, "biz-mia": EMPLOYER, OTHER_BIZ: OTHER_TENANT}
        self.extra_branches: dict[str, list[str]] = {"biz-sungshin": ["성신여대점 2호점"]}
        self.policy: dict[str, dict] = {}
        self.memberships: dict[tuple[str, str], str] = {}
        self.link_calls: list[dict] = []
        self.revoke_calls: list[dict] = []
        self.link_status: dict[str, str] = {}
        self.revoke_fail_tenants: set[str] = set()
        self.admin_tenants: dict[str, list[dict]] = {ADMIN_ID: [{"tenant_id": EMPLOYER, "role": "owner"}]}
        self.notifications: list[dict] = []
        self.notify_fails = False
        self.lock_calls: list[list[tuple[str, str]]] = []
        self.lock_rollbacks = 0
        self.locks_fail = False


@pytest.fixture
def world(tmp_path, monkeypatch):
    for name in ("OBYS_DATABASE_URL", "YEOLJEONG_FINANCE_DATABASE_URL", "OBYS_LEGACY_TENANT_IDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(svc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(svc, "UPLOAD_DIR", tmp_path / "data" / "uploads" / "onboarding")
    w = FakeWorld()

    async def fake_fetch_ledger(name, tenant_id=None):
        return [dict(r) for r in w.ledgers.get(name, []) if tenant_id and r["tenant_id"] == tenant_id and not r.get("deleted_at")]

    async def fake_upsert_ledger(name, record):
        rows = w.ledgers.setdefault(name, [])
        for index, row in enumerate(rows):
            if row["id"] == record["id"]:
                if row["tenant_id"] != record["tenant_id"]:
                    return False
                rows[index] = dict(record)
                return True
        rows.append(dict(record))
        return True

    async def fake_delete_hr_ledger(name, row_id, tenant_id):
        rows = w.ledgers.get(name, [])
        before = len(rows)
        w.ledgers[name] = [r for r in rows if not (r["id"] == row_id and r["tenant_id"] == tenant_id)]
        return len(w.ledgers[name]) < before

    async def fake_business_tenant_id(business_id):
        return w.mapping.get(business_id)

    async def fake_business_tenant_matches(business_id, tenant_id):
        return w.mapping.get(business_id) == tenant_id

    async def fake_hr_record_tenant(name, row_id):
        return next((r["tenant_id"] for r in w.ledgers.get(name, []) if r["id"] == row_id), None)

    async def fake_fetch_by_email(email):
        return [
            dict(r)
            for r in w.ledgers["employee_join_requests"]
            if str(r.get("email") or "").lower() == email and w.mapping.get(r.get("business_id")) == r["tenant_id"]
        ]

    async def fake_business_invite_info(business_id):
        if business_id not in w.mapping:
            return None
        business = next((b for b in svc.CANONICAL_BUSINESSES if b["id"] == business_id), None)
        branches = [name for name, owner in svc.BUSINESS_BY_BRANCH.items() if owner == business_id]
        branches += w.extra_branches.get(business_id, [])
        return {"name": business["name"] if business else business_id, "branches": branches}

    async def fake_assignment_targets(tenant_ids):
        return [
            {"business_id": biz, "business_name": biz, "tenant_id": tid, "branches": []}
            for biz, tid in w.mapping.items()
            if tenant_ids is None or tid in tenant_ids
        ]

    async def fake_get_join_policy(tenant_id):
        return dict(w.policy.get(tenant_id, {}))

    async def fake_set_join_policy(tenant_id, enabled, updated_by):
        w.policy[tenant_id] = {svc.EMPLOYEE_JOIN_POLICY_KEY: bool(enabled)}
        return True

    def run_db(coro):
        if coro.__name__.startswith("fake_"):
            return asyncio.run(coro)
        coro.close()
        return None

    monkeypatch.setattr(svc, "_db_available", lambda: True)
    monkeypatch.setattr(svc, "_run_db", run_db)
    monkeypatch.setattr(svc, "_db_fetch_ledger", fake_fetch_ledger)
    monkeypatch.setattr(svc, "_db_upsert_ledger", fake_upsert_ledger)
    monkeypatch.setattr(svc, "_db_delete_hr_ledger", fake_delete_hr_ledger)
    monkeypatch.setattr(svc, "_db_business_tenant_id", fake_business_tenant_id)
    monkeypatch.setattr(svc, "_db_business_tenant_matches", fake_business_tenant_matches)
    monkeypatch.setattr(svc, "_db_hr_record_tenant", fake_hr_record_tenant)
    monkeypatch.setattr(svc, "_db_fetch_join_requests_by_email", fake_fetch_by_email)
    monkeypatch.setattr(svc, "_db_business_invite_info", fake_business_invite_info)
    monkeypatch.setattr(svc, "_db_assignment_targets", fake_assignment_targets)
    monkeypatch.setattr(svc, "_db_get_join_policy", fake_get_join_policy)
    monkeypatch.setattr(svc, "_db_set_join_policy", fake_set_join_policy)

    @asynccontextmanager
    async def fake_lock(tenant_id, employee_email):
        yield None

    @asynccontextmanager
    async def fake_locks(pairs):
        if w.locks_fail:
            raise RuntimeError("auth db down")
        w.lock_calls.append(sorted(pairs))
        try:
            yield None
        except BaseException:
            w.lock_rollbacks += 1  # 실제 구현은 이 예외로 인증 DB 트랜잭션을 롤백한다
            raise

    async def fake_link(**kwargs):
        w.link_calls.append(kwargs)
        tenant_id = kwargs["tenant_id"]
        status = w.link_status.get(tenant_id, "linked")
        user_id = kwargs["employee_user_id"]
        base = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "employee_email": kwargs["employee_email"],
            "before": None,
            "after": None,
            "owned_by_request": False,
        }
        if status != "linked":
            return {**base, "status": status, "reason": "forced"}
        w.memberships[(tenant_id, user_id)] = "active"
        return {
            **base,
            "status": "linked",
            "owned_by_request": True,
            "after": {"role": "member", "status": "active", "membership_id": f"m-{tenant_id[:4]}-{user_id}"},
        }

    async def fake_revoke(**kwargs):
        w.revoke_calls.append(kwargs)
        if kwargs["tenant_id"] in w.revoke_fail_tenants:
            raise RuntimeError("revoke boom")
        w.memberships[(kwargs["tenant_id"], kwargs["user_id"])] = "removed"
        return {
            "tenant_id": kwargs["tenant_id"],
            "user_id": kwargs["user_id"],
            "before": {"role": "member", "status": "active"},
            "after": {"role": "member", "status": "removed"},
            "status": "removed",
        }

    async def fake_list_user_tenants(user_id):
        return list(w.admin_tenants.get(user_id, []))

    async def fake_create_notification(**kwargs):
        if w.notify_fails:
            raise RuntimeError("notify down")
        w.notifications.append(kwargs)
        return {}

    monkeypatch.setattr(auth_module, "employee_membership_lock", fake_lock)
    monkeypatch.setattr(auth_module, "employee_membership_locks", fake_locks)
    monkeypatch.setattr(auth_module, "link_employee_tenant_membership", fake_link)
    monkeypatch.setattr(auth_module, "revoke_employee_tenant_membership", fake_revoke)
    monkeypatch.setattr(auth_module, "list_user_tenants", fake_list_user_tenants)
    monkeypatch.setattr(yeoljeong_ops_service, "create_notification", fake_create_notification)

    # 알림 대상이 되는 승인된 관리자 직원 한 명.
    w.ledgers["employee_join_requests"].append(
        {
            "id": "adm-1", "tenant_id": EMPLOYER, "business_id": "biz-sungshin", "branch": "성신여대점",
            "name": "관리 직원", "email": "manager@example.com", "status": "approved", "role": "admin",
        }
    )
    return w


def _join(user=EMPLOYEE, **extra) -> dict:
    payload = {"name": "신규 직원", "email": user["email"], "branch": "성신여대점", **extra}
    return asyncio.run(svc.create_join_request_with_auto_approval(payload, user))


def _rows(world, tenant_id=None):
    return [r for r in world.ledgers["employee_join_requests"] if r["id"] != "adm-1" and (tenant_id is None or r["tenant_id"] == tenant_id)]


def _audits() -> list[dict]:
    return svc._read_file_rows(svc.MEMBERSHIP_AUDIT_LOG)


# --- 1. 자동승인 ---------------------------------------------------------------------------
def test_direct_join_is_auto_approved_with_membership_and_audit(world):
    result = _join()
    request = result["request"]
    assert result["auto_approve"] == {"approved": True, "reason": ""}
    assert request["status"] == "approved"
    assert request["auto_approved"] is True
    assert request["tenant_id"] == EMPLOYER
    assert request["membership_link"]["membership_id"] == f"m-{EMPLOYER[:4]}-{EMP_ID}"
    assert request["membership_link"]["tenant_id"] == EMPLOYER
    assert request["reviewed_by"] == svc.AUTO_APPROVE_ACTOR_EMAIL
    assert world.link_calls[0]["tenant_id"] == EMPLOYER
    assert world.link_calls[0]["employee_user_id"] == EMP_ID
    audit = [a for a in _audits() if a["action"] == "approved"]
    assert len(audit) == 1
    assert audit[0]["source"] == "auto_approve_join_request"
    assert audit[0]["actor_user_id"] == svc.AUTO_APPROVE_ACTOR_ID
    assert audit[0]["join_request_id"] == request["id"]
    assert [row["status"] for row in _rows(world)] == ["approved"]


def test_admins_are_notified_and_list_shows_auto_approved(world):
    _join()
    assert [n["target_user"] for n in world.notifications] == ["manager@example.com"]
    assert world.notifications[0]["notification_type"] == "employee_join_auto_approved"
    listed = svc.list_join_requests(ADMIN)
    assert [r["auto_approved"] for r in listed if r["email"] == EMP_EMAIL] == [True]


def test_notification_failure_does_not_undo_approval(world):
    world.notify_fails = True
    result = _join()
    assert result["request"]["status"] == "approved"


def test_invite_accept_is_auto_approved(world):
    svc._write_file_rows(
        "employee_invites", [{"id": "inv-1", "token": "tok-1", "branch": "성신여대점", "name": "초대 직원", "status": "pending"}]
    )
    result = asyncio.run(svc.accept_invite_with_auto_approval({"token": "tok-1"}, EMPLOYEE))
    assert result["request"]["status"] == "approved"
    assert result["requests"][0]["auto_approved"] is True
    assert result["auto_approve"] == [{"approved": True, "reason": ""}]
    assert result["request"]["invite_id"] == "inv-1"
    audit = [a for a in _audits() if a["action"] == "approved"]
    assert audit and audit[0]["source"] == "auto_approve_join_request"


def test_switch_false_keeps_pending(world):
    out = svc.set_employee_join_policy({"employee_join_auto_approve": False}, ADMIN)
    assert out == {"employee_join_auto_approve": False}
    assert svc.get_employee_join_policy(ADMIN) == {"employee_join_auto_approve": False}
    result = _join()
    assert result["request"]["status"] == "pending"
    assert result["auto_approve"] == {"approved": False, "reason": "auto_approve_disabled"}
    assert world.link_calls == []
    # 스위치를 다시 켜고 재가입하면 승인된다.
    svc.set_employee_join_policy({"employee_join_auto_approve": True}, ADMIN)
    assert _join()["request"]["status"] == "approved"


def test_switch_default_is_true_and_admin_only(world):
    assert svc.employee_join_auto_approve_enabled(EMPLOYER) is True
    with pytest.raises(HTTPException) as exc:
        svc.set_employee_join_policy({"employee_join_auto_approve": False}, MEMBER)
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        svc.set_employee_join_policy({"employee_join_auto_approve": "no"}, ADMIN)
    assert exc.value.status_code == 400


def test_membership_failure_leaves_pending_and_join_succeeds(world):
    world.link_status[EMPLOYER] = "pending_account"
    result = _join()
    request = result["request"]
    assert request["status"] == "pending"
    assert "auto_approved" not in request and "reviewed_by" not in request
    assert request["auto_approve_error"]["membership_status"] == "pending_account"
    assert result["auto_approve"]["approved"] is False
    assert [r["status"] for r in _rows(world)] == ["pending"]
    assert world.notifications == []


def test_unexpected_exception_leaves_pending_and_join_succeeds(world, monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("lock exploded")

    monkeypatch.setattr(svc, "review_join_request_with_membership", boom)
    result = _join()
    assert result["request"]["status"] == "pending"
    assert result["request"]["auto_approve_error"]["reason"] == "RuntimeError"
    assert [r["status"] for r in _rows(world)] == ["pending"]


def test_admin_can_reject_auto_approved_request_and_membership_is_revoked(world):
    request = _join()["request"]
    out = asyncio.run(svc.review_join_request_with_membership(request["id"], "rejected", "잘못 가입", ADMIN))
    assert out["request"]["status"] == "rejected"
    assert "auto_approved" not in out["request"]
    assert out["membership"]["status"] == "removed"
    assert world.revoke_calls[0]["membership_id"] == f"m-{EMPLOYER[:4]}-{EMP_ID}"
    assert world.memberships[(EMPLOYER, EMP_ID)] == "removed"


def test_manual_review_still_requires_admin(world):
    request = _join()["request"]
    with pytest.raises(HTTPException) as exc:
        asyncio.run(svc.review_join_request_with_membership(request["id"], "rejected", "", EMPLOYEE))
    assert exc.value.status_code == 403
    assert _rows(world)[0]["status"] == "approved"


def test_auto_approve_never_grants_admin(world):
    request = _join()["request"]
    assert svc._employee_access_role(request.get("role")) == "employee"
    assert all(call["employee_user_id"] == EMP_ID for call in world.link_calls)
    assert not svc._is_admin({**EMPLOYEE, "tenant_id": EMPLOYER, "current_membership": {"tenant_id": EMPLOYER, "status": "active", "role": "member"}, "tenant_role": "member"})
    # 역할이 admin 인 대기 요청은 자동승인하지 않는다.
    world.ledgers["employee_join_requests"].append(
        {
            "id": "pend-admin", "tenant_id": EMPLOYER, "business_id": "biz-sungshin", "branch": "성신여대점", "name": "관리자 후보",
            "email": "cand@example.com", "status": "pending", "role": "admin",
            "requester_user_id": "user-cand", "requester_email": "cand@example.com",
        }
    )
    outcome = asyncio.run(svc.auto_approve_join_request(dict(world.ledgers["employee_join_requests"][-1])))
    assert outcome["approved"] is False and outcome["reason"] == "role_not_employee"
    assert world.ledgers["employee_join_requests"][-1]["status"] == "pending"


def test_admin_proxy_and_rejected_requests_are_not_auto_approved(world):
    proxy = svc.upsert_join_request({"name": "대리 등록", "email": "proxy@example.com", "branch": "성신여대점"}, ADMIN)
    outcome = asyncio.run(svc.auto_approve_join_request(proxy))
    assert outcome["reason"] == "identity_not_bound" and outcome["approved"] is False
    # 관리자가 반려한 요청을 본인이 다시 내도 자동으로 되살아나지 않는다.
    request = _join()["request"]
    asyncio.run(svc.review_join_request_with_membership(request["id"], "rejected", "", ADMIN))
    again = _join()
    assert again["request"]["status"] == "rejected"
    assert again["auto_approve"]["reason"] == "not_pending"


def test_resubmitting_approved_request_does_nothing_twice(world):
    _join()
    _join()
    assert len(world.link_calls) == 1
    assert len([a for a in _audits() if a["action"] == "approved"]) == 1


# --- 2. 사업자·지점 변경 -----------------------------------------------------------------
def _approved_id(world) -> str:
    _join()
    return _rows(world)[0]["id"]


def test_same_tenant_branch_change_updates_record_and_audits(world):
    rid = _approved_id(world)
    out = asyncio.run(
        svc.reassign_approved_employee(rid, {"business_id": "biz-sungshin", "branch": "성신여대점 2호점", "memo": "2호점 발령"}, ADMIN)
    )
    assert out["moved_tenant"] is False
    assert out["previous"] == {"business_id": "biz-sungshin", "branch": "성신여대점"}
    assert out["current"] == {"business_id": "biz-sungshin", "branch": "성신여대점 2호점"}
    stored = _rows(world)[0]
    assert stored["id"] == rid and stored["status"] == "approved" and stored["branch"] == "성신여대점 2호점"
    assert stored["assignment_history"][-1]["memo"] == "2호점 발령"
    audit = [a for a in _audits() if a["action"] == "assignment_changed"]
    assert len(audit) == 1
    assert audit[0]["source"] == "reassign_employee"
    assert audit[0]["before_state"]["branch"] == "성신여대점"
    assert audit[0]["after_state"]["branch"] == "성신여대점 2호점"
    assert world.revoke_calls == []


def test_same_tenant_business_change_between_businesses_of_one_tenant(world):
    rid = _approved_id(world)
    out = asyncio.run(svc.reassign_approved_employee(rid, {"business_id": "biz-mia", "branch": "열정국밥_미아점"}, ADMIN))
    assert out["moved_tenant"] is False
    assert _rows(world)[0]["business_id"] == "biz-mia"
    assert world.link_calls and len(world.link_calls) == 1  # 멤버십은 그대로 — 새로 연결하지 않는다


def test_assignment_validates_and_rejects_noop(world):
    rid = _approved_id(world)
    for payload, code in (
        ({"business_id": "biz-sungshin", "branch": "성신여대점"}, 400),
        ({"business_id": "biz-sungshin", "branch": "없는지점"}, 400),
        ({"business_id": "biz-unknown", "branch": ""}, 400),
        ({"business_id": "biz-mia"}, 400),
    ):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(svc.reassign_approved_employee(rid, payload, ADMIN))
        assert exc.value.status_code == code, payload
    assert _rows(world)[0]["branch"] == "성신여대점"


def test_assignment_requires_admin_and_approved(world):
    rid = _approved_id(world)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(svc.reassign_approved_employee(rid, {"business_id": "biz-mia", "branch": "열정국밥_미아점"}, MEMBER))
    assert exc.value.status_code == 403
    world.ledgers["employee_join_requests"].append(
        {"id": "pend-1", "tenant_id": EMPLOYER, "business_id": "biz-sungshin", "branch": "성신여대점", "email": "p@example.com", "status": "pending"}
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(svc.reassign_approved_employee("pend-1", {"business_id": "biz-mia", "branch": "열정국밥_미아점"}, ADMIN))
    assert exc.value.status_code == 400


def test_cross_tenant_move_success(world):
    world.admin_tenants[ADMIN_ID].append({"tenant_id": OTHER_TENANT, "role": "admin"})
    world.ledgers["contracts"].append(
        {"id": "ct-pending", "tenant_id": EMPLOYER, "status": "requested", "employee_email": EMP_EMAIL, "branch": "성신여대점", "contract_type": "default"}
    )
    world.ledgers["contracts"].append(
        {"id": "ct-signed", "tenant_id": EMPLOYER, "status": "signed", "employee_email": EMP_EMAIL, "branch": "성신여대점", "contract_type": "default"}
    )
    rid = _approved_id(world)
    out = asyncio.run(svc.reassign_approved_employee(rid, {"business_id": OTHER_BIZ, "branch": "", "memo": "타 사업자 이동"}, ADMIN))
    assert out["moved_tenant"] is True
    new = out["employee"]
    assert new["tenant_id"] == OTHER_TENANT and new["business_id"] == OTHER_BIZ and new["status"] == "approved"
    assert new["id"] != rid
    assert new["role"] == "employee"
    assert new["membership_link"]["tenant_id"] == OTHER_TENANT
    old = next(r for r in world.ledgers["employee_join_requests"] if r["id"] == rid)
    assert old["status"] == "transferred" and old["transferred_to"]["request_id"] == new["id"]
    assert world.memberships[(OTHER_TENANT, EMP_ID)] == "active"
    assert world.memberships[(EMPLOYER, EMP_ID)] == "removed"
    assert [c["tenant_id"] for c in world.link_calls] == [EMPLOYER, OTHER_TENANT]
    assert world.revoke_calls[0]["tenant_id"] == EMPLOYER
    # 서명 대기 계약서는 경고로만 돌려주고, 계약서 행은 건드리지 않는다.
    assert [w["contract_id"] for w in out["warnings"]["pending_contracts"]] == ["ct-pending"]
    contracts = {c["id"]: c for c in world.ledgers["contracts"]}
    assert contracts["ct-pending"]["status"] == "requested" and contracts["ct-signed"]["status"] == "signed"
    assert all(c["tenant_id"] == EMPLOYER for c in world.ledgers["contracts"])
    # 새 사업자 승인 + 기존 회수 감사가 모두 reassign_employee 출처로 남는다.
    sources = {(a["action"], a["source"]) for a in _audits()}
    assert ("approved", "reassign_employee") in sources and ("transferred", "reassign_employee") in sources


def test_cross_tenant_move_denied_without_target_admin_rights(world):
    rid = _approved_id(world)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(svc.reassign_approved_employee(rid, {"business_id": OTHER_BIZ, "branch": ""}, ADMIN))
    assert exc.value.status_code == 403
    assert "관리자 권한" in exc.value.detail and OTHER_BIZ in exc.value.detail
    assert _rows(world, OTHER_TENANT) == []
    assert _rows(world, EMPLOYER)[0]["status"] == "approved"
    assert len(world.link_calls) == 1  # 가입 때 한 번뿐


def test_cross_tenant_move_allowed_for_platform_admin(world):
    rid = _approved_id(world)
    out = asyncio.run(svc.reassign_approved_employee(rid, {"business_id": OTHER_BIZ, "branch": ""}, PLATFORM_ADMIN))
    assert out["moved_tenant"] is True
    assert _rows(world, OTHER_TENANT)[0]["status"] == "approved"


def test_cross_tenant_move_rolls_back_when_target_link_fails(world):
    world.admin_tenants[ADMIN_ID].append({"tenant_id": OTHER_TENANT, "role": "owner"})
    rid = _approved_id(world)
    world.link_status[OTHER_TENANT] = "pending_account"
    with pytest.raises(HTTPException) as exc:
        asyncio.run(svc.reassign_approved_employee(rid, {"business_id": OTHER_BIZ, "branch": ""}, ADMIN))
    assert exc.value.status_code == 409
    assert _rows(world, OTHER_TENANT) == []
    old = _rows(world, EMPLOYER)[0]
    assert old["id"] == rid and old["status"] == "approved"
    assert world.memberships[(EMPLOYER, EMP_ID)] == "active"
    assert world.revoke_calls == []


def test_cross_tenant_move_rolls_back_when_old_membership_revoke_fails(world):
    world.admin_tenants[ADMIN_ID].append({"tenant_id": OTHER_TENANT, "role": "owner"})
    rid = _approved_id(world)
    world.revoke_fail_tenants.add(EMPLOYER)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(svc.reassign_approved_employee(rid, {"business_id": OTHER_BIZ, "branch": ""}, ADMIN))
    assert exc.value.status_code == 502
    old = _rows(world, EMPLOYER)[0]
    assert old["id"] == rid and old["status"] == "approved"
    assert "transferred_to" not in old
    # 새 사업자 쪽에 만든 요청은 지워지고 멤버십은 회수된다.
    assert _rows(world, OTHER_TENANT) == []
    assert world.memberships[(OTHER_TENANT, EMP_ID)] == "removed"
    assert world.memberships[(EMPLOYER, EMP_ID)] == "active"


def test_cross_tenant_move_conflicts_with_existing_approved_row(world):
    world.admin_tenants[ADMIN_ID].append({"tenant_id": OTHER_TENANT, "role": "owner"})
    rid = _approved_id(world)
    world.ledgers["employee_join_requests"].append(
        {"id": "dup-1", "tenant_id": OTHER_TENANT, "business_id": OTHER_BIZ, "branch": "", "email": EMP_EMAIL, "status": "approved"}
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(svc.reassign_approved_employee(rid, {"business_id": OTHER_BIZ, "branch": ""}, ADMIN))
    assert exc.value.status_code == 409
    assert _rows(world, EMPLOYER)[0]["status"] == "approved"


def test_assignment_targets_are_limited_to_own_and_administered_tenants(world):
    own = {t["business_id"] for t in asyncio.run(svc.list_assignment_targets(ADMIN))}
    assert own == {"biz-sungshin", "biz-mia"}
    world.admin_tenants[ADMIN_ID].append({"tenant_id": OTHER_TENANT, "role": "admin"})
    both = {t["business_id"] for t in asyncio.run(svc.list_assignment_targets(ADMIN))}
    assert both == {"biz-sungshin", "biz-mia", OTHER_BIZ}
    everything = asyncio.run(svc.list_assignment_targets(PLATFORM_ADMIN))
    assert OTHER_BIZ in {t["business_id"] for t in everything}
    with pytest.raises(HTTPException) as exc:
        asyncio.run(svc.list_assignment_targets(MEMBER))
    assert exc.value.status_code == 403


def test_assignment_targets_endpoint_and_routes_exist():
    paths = {(tuple(sorted(r.methods)), r.path) for r in api.router.routes if hasattr(r, "methods")}
    assert (("PATCH",), "/yeoljeong-finance/employees/approved/{request_id}/assignment") in paths
    assert (("GET",), "/yeoljeong-finance/employees/assignment-targets") in paths
    assert (("GET",), "/yeoljeong-finance/employees/join-policy") in paths
    assert (("PUT",), "/yeoljeong-finance/employees/join-policy") in paths


# --- 재작업 라운드 1: AI 리뷰 반려 지적 8건 -------------------------------------------------
class Crash(BaseException):
    """프로세스가 단계 사이에서 죽는 상황 — except Exception 으로는 잡히지 않는다."""


def _setup_move(world):
    world.admin_tenants[ADMIN_ID].append({"tenant_id": OTHER_TENANT, "role": "owner"})
    return _approved_id(world)


def _all_rows(world, tenant_id):
    return [r for r in world.ledgers["employee_join_requests"] if r["tenant_id"] == tenant_id and r["id"] != "adm-1"]


# 지적 1 — 기본값 true 는 CEO 지시이며 코드·테스트로 고정한다
def test_review1_default_is_true_by_ceo_directive_and_scoped_to_bound_employee_requests(world):
    assert svc.EMPLOYEE_JOIN_AUTO_APPROVE_DEFAULT is True
    # 정책 행이 있어도 키가 없으면 기본값을 따른다.
    world.policy[EMPLOYER] = {"other": 1}
    assert svc.employee_join_auto_approve_enabled(EMPLOYER) is True
    assert _join()["request"]["status"] == "approved"
    # 기본값이 true 여도 관리자 대리 등록(본인 계정 미연결)은 승인하지 않는다.
    proxy = svc.upsert_join_request({"name": "대리", "email": "proxy2@example.com", "branch": "성신여대점"}, ADMIN)
    assert asyncio.run(svc.auto_approve_join_request(proxy))["reason"] == "identity_not_bound"


# 지적 2 — 다단계 이동: 두 테넌트 락·이동 표시·실패 시 롤백·복구 실패 노출
def test_review2_transfer_takes_both_tenant_locks_and_marks_intent_until_finalized(world, monkeypatch):
    rid = _setup_move(world)
    seen: dict = {}
    original_link = auth_module.link_employee_tenant_membership

    async def spy_link(**kwargs):
        if kwargs["tenant_id"] == OTHER_TENANT:
            seen["old"] = next(r for r in _all_rows(world, EMPLOYER) if r["id"] == rid)
            seen["new"] = _all_rows(world, OTHER_TENANT)[0]
        return await original_link(**kwargs)

    monkeypatch.setattr(auth_module, "link_employee_tenant_membership", spy_link)
    out = asyncio.run(svc.reassign_approved_employee(rid, {"business_id": OTHER_BIZ, "branch": ""}, ADMIN))
    assert world.lock_calls[-1] == sorted([(EMPLOYER, EMP_EMAIL), (OTHER_TENANT, EMP_EMAIL)])
    # 연결 시점: 기존 요청에는 이동 표시, 새 요청은 승인 목록에 안 보이는 transfer_pending.
    assert seen["old"]["transfer_intent"]["business_id"] == OTHER_BIZ and seen["old"]["status"] == "approved"
    assert seen["new"]["status"] == "transfer_pending"
    assert out["employee"]["status"] == "approved"
    old = next(r for r in _all_rows(world, EMPLOYER) if r["id"] == rid)
    assert old["status"] == "transferred" and "transfer_intent" not in old
    assert world.lock_rollbacks == 0


def test_review2_failed_transfer_leaves_no_trace_and_rolls_back_auth_transaction(world):
    rid = _setup_move(world)
    before = next(dict(r) for r in _all_rows(world, EMPLOYER) if r["id"] == rid)
    world.link_status[OTHER_TENANT] = "pending_account"
    rollbacks = world.lock_rollbacks
    with pytest.raises(HTTPException) as exc:
        asyncio.run(svc.reassign_approved_employee(rid, {"business_id": OTHER_BIZ, "branch": ""}, ADMIN))
    assert exc.value.status_code == 409
    assert world.lock_rollbacks == rollbacks + 1
    after = next(r for r in _all_rows(world, EMPLOYER) if r["id"] == rid)
    assert after == before and "transfer_intent" not in after
    assert _all_rows(world, OTHER_TENANT) == []


def test_review2_incomplete_rollback_is_reported_not_swallowed(world, monkeypatch, caplog):
    rid = _setup_move(world)
    world.link_status[OTHER_TENANT] = "pending_account"

    def boom(*_a, **_k):
        raise RuntimeError("db write failed")

    monkeypatch.setattr(svc, "_discard_hr_request", boom)
    with caplog.at_level("ERROR"):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(svc.reassign_approved_employee(rid, {"business_id": OTHER_BIZ, "branch": ""}, ADMIN))
    assert exc.value.status_code == 500
    assert "복구도 일부 실패" in exc.value.detail and "새 사업자 가입요청 복구 실패" in exc.value.detail
    assert any("transfer rollback incomplete" in rec.message for rec in caplog.records)
    audit = [a for a in _audits() if a["action"] == "transfer_rollback" and "rollback_incomplete" in a["reason"]]
    assert len(audit) == 1 and audit[0]["classification"] == "membership_failed"


def test_review2_crash_between_steps_leaves_marker_and_retry_resumes(world, monkeypatch):
    rid = _setup_move(world)
    real_sync = svc.sync_employee_tenant_membership

    async def crashing_sync(record, action, user, **kwargs):
        if action == "transferred":
            raise Crash()
        return await real_sync(record, action, user, **kwargs)

    monkeypatch.setattr(svc, "sync_employee_tenant_membership", crashing_sync)
    with pytest.raises(Crash):
        asyncio.run(svc.reassign_approved_employee(rid, {"business_id": OTHER_BIZ, "branch": ""}, ADMIN))
    assert world.lock_rollbacks >= 1  # 락 블록이 예외로 끝났다 → 인증 DB 트랜잭션 롤백
    old = next(r for r in _all_rows(world, EMPLOYER) if r["id"] == rid)
    assert old["status"] == "approved" and old["transfer_intent"]["business_id"] == OTHER_BIZ
    assert [r["status"] for r in _all_rows(world, OTHER_TENANT)] == ["transfer_pending"]
    # 끝나지 않은 직원에게 반려·역할 변경·다른 이동은 막힌다.
    with pytest.raises(HTTPException) as exc:
        asyncio.run(svc.review_join_request_with_membership(rid, "rejected", "", ADMIN))
    assert exc.value.status_code == 409 and "끝나지 않은" in exc.value.detail
    with pytest.raises(HTTPException) as exc:
        asyncio.run(svc.update_approved_employee_role_serialized(rid, "employee", "", ADMIN))
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        asyncio.run(svc.reassign_approved_employee(rid, {"business_id": "biz-mia", "branch": "열정국밥_미아점"}, ADMIN))
    assert exc.value.status_code == 409
    # 같은 대상으로 다시 실행하면 이어서 마무리하고 요청이 중복으로 생기지 않는다.
    monkeypatch.setattr(svc, "sync_employee_tenant_membership", real_sync)
    out = asyncio.run(svc.reassign_approved_employee(rid, {"business_id": OTHER_BIZ, "branch": ""}, ADMIN))
    assert out["employee"]["status"] == "approved"
    assert [r["status"] for r in _all_rows(world, OTHER_TENANT)] == ["approved"]
    old = next(r for r in _all_rows(world, EMPLOYER) if r["id"] == rid)
    assert old["status"] == "transferred" and "transfer_intent" not in old
    assert world.memberships[(OTHER_TENANT, EMP_ID)] == "active" and world.memberships[(EMPLOYER, EMP_ID)] == "removed"


def test_review2_finalize_failure_after_commit_is_resumable(world, monkeypatch):
    rid = _setup_move(world)
    real_finalize = svc._finalize_transfer
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("write failed")
        return real_finalize(*args, **kwargs)

    monkeypatch.setattr(svc, "_finalize_transfer", flaky)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(svc.reassign_approved_employee(rid, {"business_id": OTHER_BIZ, "branch": ""}, ADMIN))
    assert exc.value.status_code == 500 and "이어서 마무리" in exc.value.detail
    assert world.memberships[(OTHER_TENANT, EMP_ID)] == "active"  # 멤버십은 이미 커밋됐다
    out = asyncio.run(svc.reassign_approved_employee(rid, {"business_id": OTHER_BIZ, "branch": ""}, ADMIN))
    assert out["moved_tenant"] is True and len(_all_rows(world, OTHER_TENANT)) == 1


def test_review2_transfer_fails_closed_when_lock_unavailable(world):
    rid = _setup_move(world)
    world.locks_fail = True
    with pytest.raises(HTTPException) as exc:
        asyncio.run(svc.reassign_approved_employee(rid, {"business_id": OTHER_BIZ, "branch": ""}, ADMIN))
    assert exc.value.status_code == 503
    assert _all_rows(world, OTHER_TENANT) == []
    assert "transfer_intent" not in next(r for r in _all_rows(world, EMPLOYER) if r["id"] == rid)


# 지적 3 — 되살리는 행: 락 안에서 스냅샷을 뜨고 실패하면 원래 모습으로 복구
def test_review3_revived_rejected_row_is_restored_exactly_on_failure(world):
    rid = _setup_move(world)
    rejected = {
        "id": "rej-1", "tenant_id": OTHER_TENANT, "business_id": OTHER_BIZ, "branch": "", "email": EMP_EMAIL,
        "name": "예전 이름", "status": "rejected", "review_memo": "기존 반려", "requested_at": "2026-01-01T00:00:00",
    }
    world.ledgers["employee_join_requests"].append(dict(rejected))
    world.link_status[OTHER_TENANT] = "pending_account"
    with pytest.raises(HTTPException):
        asyncio.run(svc.reassign_approved_employee(rid, {"business_id": OTHER_BIZ, "branch": ""}, ADMIN))
    assert _all_rows(world, OTHER_TENANT) == [rejected]
    # 성공하면 같은 행이 되살아나 중복 행이 생기지 않는다.
    world.link_status.clear()
    out = asyncio.run(svc.reassign_approved_employee(rid, {"business_id": OTHER_BIZ, "branch": ""}, ADMIN))
    assert out["employee"]["id"] == "rej-1" and len(_all_rows(world, OTHER_TENANT)) == 1


# 지적 4 — 계획 이후 다른 관리자의 변경을 덮어쓰지 않는다
@pytest.mark.parametrize("cross", [False, True])
def test_review4_concurrent_role_change_is_not_lost(world, monkeypatch, cross):
    rid = _setup_move(world)
    real_plan = svc._plan_employee_assignment

    def plan_then_other_admin_changes_role(*args, **kwargs):
        plan = real_plan(*args, **kwargs)
        row = next(r for r in world.ledgers["employee_join_requests"] if r["id"] == rid)
        row["role"] = "manager"
        row["updated_at"] = "2099-01-01T00:00:00"
        return plan

    monkeypatch.setattr(svc, "_plan_employee_assignment", plan_then_other_admin_changes_role)
    payload = {"business_id": OTHER_BIZ, "branch": ""} if cross else {"business_id": "biz-sungshin", "branch": "성신여대점 2호점"}
    with pytest.raises(HTTPException) as exc:
        asyncio.run(svc.reassign_approved_employee(rid, payload, ADMIN))
    assert exc.value.status_code == 409 and "다른 관리자가 먼저" in exc.value.detail
    row = next(r for r in world.ledgers["employee_join_requests"] if r["id"] == rid)
    assert row["role"] == "manager" and row["status"] == "approved" and row["branch"] == "성신여대점"
    assert "transfer_intent" not in row and _all_rows(world, OTHER_TENANT) == []


def test_review4_assignment_and_role_update_share_the_same_lock(world):
    rid = _approved_id(world)
    asyncio.run(svc.update_approved_employee_role_serialized(rid, "employee", "", ADMIN))
    asyncio.run(svc.reassign_approved_employee(rid, {"business_id": "biz-sungshin", "branch": "성신여대점 2호점"}, ADMIN))
    assert world.lock_calls[-2:] == [[(EMPLOYER, EMP_EMAIL)], [(EMPLOYER, EMP_EMAIL)]]


# 지적 5 — 자동승인 되돌리기: 락 블록 롤백·연결 표시
def test_review5_unlinked_auto_approval_rolls_back_lock_transaction(world):
    world.link_status[EMPLOYER] = "error"
    before = world.lock_rollbacks
    result = _join()
    assert result["request"]["status"] == "pending" and "auto_approve_link_pending" not in result["request"]
    assert world.lock_rollbacks == 0  # 자동승인은 membership_lock(단수)을 쓴다 — 아래 예외 케이스가 롤백을 검증한다
    assert before == 0


def test_review5_sync_exception_reverts_approval(world, monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("sync exploded")

    monkeypatch.setattr(svc, "sync_employee_tenant_membership", boom)
    result = _join()
    assert result["request"]["status"] == "pending"
    assert result["request"]["auto_approve_error"]["reason"] == "RuntimeError"
    assert [r["status"] for r in _rows(world)] == ["pending"]


def test_review5_link_pending_flag_is_cleared_on_success_and_visible_if_left(world, monkeypatch):
    ok = _join()["request"]
    assert ok["status"] == "approved" and "auto_approve_link_pending" not in ok
    stored = _rows(world)[0]
    assert "auto_approve_link_pending" not in stored

    other_email = "second@example.com"
    other = _user(PERSONAL, "owner", email=other_email, user_id="user-second")

    def fail_clear(*_a, **_k):
        raise RuntimeError("write failed")

    monkeypatch.setattr(svc, "_clear_auto_link_pending", fail_clear)
    result = _join(other)
    assert result["request"]["status"] == "approved"  # 연결은 끝났으므로 승인 유지
    assert world.memberships[(EMPLOYER, "user-second")] == "active"
    row = next(r for r in _rows(world) if r["email"] == other_email)
    assert row["auto_approve_link_pending"] is True  # 목록에서 확인 필요로 보인다


def test_review5_manual_review_clears_link_pending(world):
    world.ledgers["employee_join_requests"].append(
        {"id": "x1", "tenant_id": EMPLOYER, "business_id": "biz-sungshin", "branch": "성신여대점", "email": "x@example.com",
         "status": "approved", "auto_approved": True, "auto_approve_link_pending": True}
    )
    out = asyncio.run(svc.review_join_request_with_membership("x1", "rejected", "정리", ADMIN))
    assert "auto_approve_link_pending" not in out["request"] and "auto_approved" not in out["request"]


# 지적 6 — 동기 정책 조회는 루프 안에서 조용히 오판하지 않는다
def test_review6_sync_policy_read_refuses_running_loop_and_async_variant_works(world):
    world.policy[EMPLOYER] = {svc.EMPLOYEE_JOIN_POLICY_KEY: False}

    async def inside_loop():
        with pytest.raises(RuntimeError):
            svc.employee_join_auto_approve_enabled(EMPLOYER)
        return await svc.employee_join_auto_approve_enabled_async(EMPLOYER)

    assert asyncio.run(inside_loop()) is False
    assert svc.employee_join_auto_approve_enabled(EMPLOYER) is False  # 스레드 없이 동기 호출도 같은 값
    world.policy.clear()
    assert asyncio.run(svc.employee_join_auto_approve_enabled_async(EMPLOYER)) is True


def test_review6_async_policy_read_failure_fails_to_manual(world, monkeypatch):
    async def down(_tenant_id):
        raise ConnectionError("db down")

    monkeypatch.setattr(svc, "_db_get_join_policy", down)
    assert asyncio.run(svc.employee_join_auto_approve_enabled_async(EMPLOYER)) is False
    assert _join()["request"]["status"] == "pending"


# 지적 7 — 파일 모드 선택지는 DB 모드와 같은 모양(same_tenant)
def test_review7_file_mode_assignment_targets_have_same_shape(world, monkeypatch):
    monkeypatch.setattr(svc, "_db_available", lambda: False)
    targets = asyncio.run(svc.list_assignment_targets(ADMIN))
    assert targets and all(t["same_tenant"] is True and t["tenant_id"] == EMPLOYER for t in targets)
    assert set(targets[0]) == {"business_id", "business_name", "tenant_id", "same_tenant", "branches", "workplace_mode"}


# 지적 8 — 알림 수신자는 같은 테넌트의 승인된 관리자 직원만
def test_review8_notifications_only_reach_same_tenant_admins(world, monkeypatch):
    real_read = svc._read_hr

    def leaky_read(name, user):
        rows = real_read(name, user)
        if name == "employee_join_requests":
            rows = rows + [
                {"id": "foreign", "tenant_id": OTHER_TENANT, "email": "foreign-admin@example.com", "status": "approved", "role": "admin"},
                {"id": "pending-admin", "tenant_id": EMPLOYER, "email": "pa@example.com", "status": "pending", "role": "admin"},
                {"id": "plain", "tenant_id": EMPLOYER, "email": "plain@example.com", "status": "approved", "role": "employee"},
            ]
        return rows

    monkeypatch.setattr(svc, "_read_hr", leaky_read)
    _join()
    assert [n["target_user"] for n in world.notifications] == ["manager@example.com"]
