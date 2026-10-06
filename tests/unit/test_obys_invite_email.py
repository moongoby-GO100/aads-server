"""ACCT-OBYS-INVITE-EMAIL-20261006 — 초대에 이메일 고정 + 수락 시 이름만 있던 급여내역서 연결.

(1) 생성: 형식 검사·소문자 정규화·응답/목록/resolve 는 마스킹본만, 이메일 없는 예전 방식도 동작.
(2) 수락: 초대 이메일과 다른 계정은 403(회사·점포 비노출), 같은 이메일은 성공, 이메일 없는 예전 초대는 하위호환.
(3) 급여 연결: 이름 정확일치·이메일 없는 행만, 별칭/부분일치·confirmed·동명이인 의심은 미연결, 멱등.
오비서 DB 는 인메모리 가짜 — 운영 DB 미접촉.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from fastapi import HTTPException

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app.api import obys_finance as api  # noqa: E402

svc = api.svc

EMPLOYER = "15055cac-71b0-45ec-b714-7093dde189ff"
OTHER_TENANT = "2a1b3c4d-0000-4000-8000-000000000001"
NEW_EMP_TENANT = "d184a18c-78c8-45c7-aee7-bfe79fadf95f"
STRANGER_TENANT = "9f9f9f9f-0000-4000-8000-000000000009"
EMP_EMAIL = "yang@example.com"


def _user(tenant_id: str, role: str, *, email: str, user_id: str) -> dict:
    return {
        "user_id": user_id,
        "email": email,
        "is_admin": False,
        "tenant_id": tenant_id,
        "tenant_role": role,
        "user_role": "user",
        "current_membership": {"tenant_id": tenant_id, "status": "active", "role": role},
    }


ADMIN = _user(EMPLOYER, "owner", email="owner@example.com", user_id="user-owner")
EMPLOYEE = _user(NEW_EMP_TENANT, "owner", email=EMP_EMAIL, user_id="user-yang")
STRANGER = _user(STRANGER_TENANT, "owner", email="stranger@example.com", user_id="user-stranger")

BUSINESS_NAMES = {"biz-sungshin": "열정국밥 성신여대점", "biz-mia": "열정국밥_미아점", "biz-other": "남의 사업자"}
BRANCHES = {"biz-sungshin": ["성신여대점", "성신2호점"], "biz-mia": ["열정국밥_미아점"], "biz-other": ["타지점"]}
MAPPING = {"biz-sungshin": EMPLOYER, "biz-mia": EMPLOYER, "biz-other": OTHER_TENANT}


class FakeObysDB:
    def __init__(self):
        self.ledgers: dict[str, list[dict]] = {"employee_join_requests": [], "payroll_statements": []}
        self.upserts: list[tuple[str, str]] = []


def _statement(sid: str, name: str, *, business_id="biz-sungshin", branch="성신여대점", month="2026-09",
               email="", status="draft", tenant=EMPLOYER, **extra) -> dict:
    return {
        "id": sid, "employee_name": name, "employee_email": email, "employee_email_masked": "",
        "business_id": business_id, "branch": branch, "payroll_month": month, "status": status,
        "gross_pay": 2_000_000, "net_pay": 1_800_000, "tenant_id": tenant, "payroll_validation_errors": [], **extra,
    }


@pytest.fixture
def db(tmp_path, monkeypatch):
    for name in ("OBYS_DATABASE_URL", "YEOLJEONG_FINANCE_DATABASE_URL", "OBYS_LEGACY_TENANT_IDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(svc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(svc, "UPLOAD_DIR", tmp_path / "data" / "uploads" / "onboarding")
    fake = FakeObysDB()

    async def fake_fetch_ledger(name, tenant_id=None):
        return [dict(r) for r in fake.ledgers[name] if tenant_id and r["tenant_id"] == tenant_id]

    async def fake_upsert_ledger(name, record):
        rows = fake.ledgers[name]
        fake.upserts.append((name, str(record["id"])))
        for index, row in enumerate(rows):
            if row["id"] == record["id"]:
                if row["tenant_id"] != record["tenant_id"]:
                    return False
                rows[index] = dict(record)
                return True
        rows.append(dict(record))
        return True

    async def fake_delete_hr_ledger(name, row_id, tenant_id):
        before = len(fake.ledgers[name])
        fake.ledgers[name] = [r for r in fake.ledgers[name] if not (r["id"] == row_id and r["tenant_id"] == tenant_id)]
        return len(fake.ledgers[name]) < before

    async def fake_business_tenant_id(business_id):
        return MAPPING.get(business_id)

    async def fake_business_tenant_matches(business_id, tenant_id):
        return MAPPING.get(business_id) == tenant_id

    async def fake_hr_record_tenant(name, row_id):
        return next((r["tenant_id"] for r in fake.ledgers[name] if r["id"] == row_id), None)

    async def fake_fetch_by_email(email):
        return [dict(r) for r in fake.ledgers["employee_join_requests"] if str(r.get("email") or "").lower() == email]

    async def fake_business_invite_info(business_id):
        if business_id not in BUSINESS_NAMES:
            return None
        return {"name": BUSINESS_NAMES[business_id], "branches": list(BRANCHES[business_id])}

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
    return fake


def _invite(**payload) -> dict:
    base = {"name": "양재혁", "phone": "010-1234-5678", "targets": [{"business_id": "biz-sungshin", "branch": "성신여대점"}]}
    return svc.create_invite({**base, **payload}, ADMIN)


def _stored(invite_id: str) -> dict:
    return svc._find(svc._read_file_rows("employee_invites"), invite_id)


def _payroll(db: FakeObysDB) -> dict[str, dict]:
    return {r["id"]: r for r in db.ledgers["payroll_statements"]}


# --- 1. 생성 -----------------------------------------------------------------------------
def test_create_normalizes_email_and_exposes_only_masked(db):
    invite = _invite(email="  Yang@Example.COM ")
    assert _stored(invite["id"])["email"] == "yang@example.com"
    assert "email" not in invite
    assert invite["email_masked"] == "ya**@example.com"
    assert invite["token"]  # 링크 생성에는 계속 필요


@pytest.mark.parametrize("bad", ["no-at-sign", "a@b", "a b@c.com", "@x.com", "a@@x.com", "한글@x.com"])
def test_create_rejects_malformed_email(db, bad):
    with pytest.raises(HTTPException) as excinfo:
        _invite(email=bad)
    assert excinfo.value.status_code == 400
    assert svc._read_file_rows("employee_invites") == []


def test_list_and_resolve_never_return_raw_email(db):
    invite = _invite(email="yang@example.com")
    listed = svc.list_invites(ADMIN)
    assert listed[0]["email_masked"] == "ya**@example.com"
    assert "email" not in listed[0]
    resolved = svc.resolve_invite(invite["token"])
    assert resolved["email_masked"] == "ya**@example.com"
    assert "email" not in resolved and "token" not in resolved
    assert "yang@example.com" not in str(listed) + str(resolved)


def test_invite_without_email_is_still_created(db):
    invite = _invite()
    assert invite["email_masked"] == ""
    assert _stored(invite["id"])["email"] == ""


# --- 2. 수락 ------------------------------------------------------------------------------
def test_other_email_account_gets_403_without_leaking_business(db):
    invite = _invite(email="yang@example.com")
    with pytest.raises(HTTPException) as excinfo:
        svc.accept_invite({"token": invite["token"]}, STRANGER)
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "초대받은 이메일로 로그인/가입해 주십시오"
    assert "열정" not in excinfo.value.detail and "성신" not in excinfo.value.detail
    assert db.ledgers["employee_join_requests"] == []
    assert _stored(invite["id"])["status"] == "pending"


def test_wrong_account_cannot_probe_an_already_accepted_invite(db):
    invite = _invite(email="yang@example.com")
    svc.accept_invite({"token": invite["token"]}, EMPLOYEE)
    with pytest.raises(HTTPException) as excinfo:
        svc.accept_invite({"token": invite["token"]}, STRANGER)
    assert excinfo.value.status_code == 403


def test_same_email_accepts_case_insensitively(db):
    invite = _invite(email="Yang@Example.com")
    result = svc.accept_invite({"token": invite["token"]}, EMPLOYEE)
    assert result["request"]["email"] == EMP_EMAIL
    assert _stored(invite["id"])["status"] == "accepted"
    assert len(db.ledgers["employee_join_requests"]) == 1


def test_legacy_invite_without_email_accepts_any_account(db):
    svc._write_file_rows("employee_invites", [{
        "id": "inv-old", "token": "tok-old", "name": "양재혁", "status": "pending",
        "targets": [{"business_id": "biz-sungshin", "branch": "성신여대점"}],
    }])
    result = svc.accept_invite({"token": "tok-old"}, STRANGER)
    assert result["request"]["email"] == "stranger@example.com"


# --- 3. 급여내역서 연결 ---------------------------------------------------------------------
def test_accept_links_exact_name_statements_in_employer_tenant(db):
    db.ledgers["payroll_statements"] += [
        _statement("s-sep", "양재혁", month="2026-09"),
        _statement("s-aug", " 양재혁  ", month="2026-08"),  # 공백 정리 후 일치
        _statement("s-alias", "양재혁(재혁)"),
        _statement("s-partial", "양재혁수"),
        _statement("s-other-name", "김철수"),
        _statement("s-other-biz", "양재혁", business_id="biz-mia", branch="열정국밥_미아점"),
        _statement("s-has-email", "양재혁", month="2026-07", email="someone@example.com"),
    ]
    invite = _invite(email="yang@example.com")
    result = svc.accept_invite({"token": invite["token"]}, EMPLOYEE)
    rows = _payroll(db)
    assert sorted(result["payroll_link"]["linked_ids"]) == ["s-aug", "s-sep"]
    assert result["payroll_link"]["linked"] == 2
    for sid in ("s-sep", "s-aug"):
        assert rows[sid]["employee_email"] == EMP_EMAIL
        assert rows[sid]["employee_email_masked"] == "ya**@example.com"
        assert rows[sid]["linked_by_invite"]["invite_id"] == invite["id"]
        assert rows[sid]["linked_by_invite"]["statement_id"] == sid
        assert rows[sid]["linked_by_invite"]["linked_at"]
        assert rows[sid]["tenant_id"] == EMPLOYER  # 고용주 테넌트 그대로
    for sid in ("s-alias", "s-partial", "s-other-name", "s-other-biz"):
        assert rows[sid]["employee_email"] == ""
        assert "linked_by_invite" not in rows[sid]
    assert rows["s-has-email"]["employee_email"] == "someone@example.com"


def test_confirmed_statement_is_never_touched(db):
    db.ledgers["payroll_statements"] += [
        _statement("s-conf", "양재혁", month="2026-08", status="confirmed"),
        _statement("s-draft", "양재혁", month="2026-09"),
    ]
    before = dict(_payroll(db)["s-conf"])
    invite = _invite(email="yang@example.com")
    result = svc.accept_invite({"token": invite["token"]}, EMPLOYEE)
    assert result["payroll_link"]["linked_ids"] == ["s-draft"]
    assert _payroll(db)["s-conf"] == before
    assert ("payroll_statements", "s-conf") not in db.upserts


def test_second_accept_is_idempotent(db):
    db.ledgers["payroll_statements"].append(_statement("s-sep", "양재혁"))
    invite = _invite(email="yang@example.com")
    first = svc.accept_invite({"token": invite["token"]}, EMPLOYEE)
    after_first = dict(_payroll(db)["s-sep"])
    second = svc.accept_invite({"token": invite["token"]}, EMPLOYEE)
    assert first["payroll_link"]["linked"] == 1
    assert second["payroll_link"]["linked"] == 0
    assert second["payroll_link"]["reason"] == "no_matching_statement"
    assert _payroll(db)["s-sep"] == after_first
    assert len(db.ledgers["payroll_statements"]) == 1
    assert ("payroll_statements", "s-sep") in db.upserts and db.upserts.count(("payroll_statements", "s-sep")) == 1


def test_ambiguous_same_month_same_branch_homonym_is_not_linked(db):
    db.ledgers["payroll_statements"] += [
        _statement("s-a", "양재혁", month="2026-09"),
        _statement("s-b", "양재혁", month="2026-09"),  # 같은 달·같은 지점에 둘 → 동명이인 의심
    ]
    invite = _invite(email="yang@example.com")
    result = svc.accept_invite({"token": invite["token"]}, EMPLOYEE)
    assert result["payroll_link"] == {"linked": 0, "linked_ids": [], "reason": "ambiguous_name"}
    assert all(r["employee_email"] == "" for r in db.ledgers["payroll_statements"])
    assert _stored(invite["id"])["status"] == "accepted"  # 수락 자체는 성공


def test_ambiguous_different_employee_identity_is_not_linked(db):
    db.ledgers["payroll_statements"] += [
        _statement("s-a", "양재혁", month="2026-09", employee_request_id="req-1"),
        _statement("s-b", "양재혁", month="2026-08", employee_request_id="req-2"),
    ]
    invite = _invite(email="yang@example.com")
    result = svc.accept_invite({"token": invite["token"]}, EMPLOYEE)
    assert result["payroll_link"]["linked"] == 0 and result["payroll_link"]["reason"] == "ambiguous_name"


def test_multi_branch_same_person_is_linked(db):
    db.ledgers["payroll_statements"] += [
        _statement("s-a", "양재혁", branch="성신여대점", month="2026-09"),
        _statement("s-b", "양재혁", branch="성신2호점", month="2026-09"),  # 겸직: 지점이 다르면 동명이인으로 보지 않는다
    ]
    invite = _invite(email="yang@example.com")
    result = svc.accept_invite({"token": invite["token"]}, EMPLOYEE)
    assert result["payroll_link"]["linked"] == 2


def test_legacy_invite_without_email_never_links_payroll(db):
    db.ledgers["payroll_statements"].append(_statement("s-sep", "양재혁"))
    svc._write_file_rows("employee_invites", [{
        "id": "inv-old", "token": "tok-old", "name": "양재혁", "status": "pending",
        "targets": [{"business_id": "biz-sungshin", "branch": "성신여대점"}],
    }])
    result = svc.accept_invite({"token": "tok-old"}, EMPLOYEE)
    assert result["payroll_link"]["reason"] == "invite_without_email"
    assert _payroll(db)["s-sep"]["employee_email"] == ""


def test_statements_with_invalid_amounts_are_left_alone(db):
    db.ledgers["payroll_statements"].append(_statement("s-bad", "양재혁", payroll_validation_errors=["gross_pay"]))
    invite = _invite(email="yang@example.com")
    result = svc.accept_invite({"token": invite["token"]}, EMPLOYEE)
    assert result["payroll_link"]["linked"] == 0
    assert _payroll(db)["s-bad"]["employee_email"] == ""


def test_link_failure_does_not_undo_accept(db, monkeypatch):
    invite = _invite(email="yang@example.com")

    def boom(*_args, **_kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(svc, "_link_payroll_by_invite", boom)
    result = svc.accept_invite({"token": invite["token"]}, EMPLOYEE)
    assert result["payroll_link"]["reason"] == "link_failed"
    assert _stored(invite["id"])["status"] == "accepted"


# --- 4. API 모델·화면 ------------------------------------------------------------------------
def test_invite_create_model_carries_email():
    assert api.InviteCreate(email="a@b.co").model_dump()["email"] == "a@b.co"
    assert api.InviteCreate().model_dump()["email"] == ""


def test_index_invite_form_requires_email_and_locks_signup_email():
    html = Path("app/static/apps/obys/index.html").read_text(encoding="utf-8")
    form = html[html.index('id="employeeInviteForm"'):html.index('id="createEmployeeInviteBtn"')]
    assert 'name="email" type="email"' in form and "required" in form[form.index('name="email"'):form.index("</label>", form.index('name="email"'))]
    assert "email: inviteEmail" in html
    assert "yf_invite_email" in html
    assert "input.readOnly = true" in html
    assert "초대받은 이메일로 가입해 주십시오" in html
    assert "invite.email_masked" in html


def test_v41_invite_form_requires_email():
    html = Path("app/static/apps/obys/mockup-v4-1.html").read_text(encoding="utf-8")
    assert 'name="email" type="email" placeholder="직원 이메일(필수)" required' in html
    assert "body={name:form.name.value.trim(),email," in html
    assert "invite.email_masked" in html
