"""AADS-OBYS-INVITE-BUSINESS-MULTISTORE-20261001 — 초대에 사업자 귀속 + 여러 매장 복수선택.

2026-10-01 진아244 실측: 초대 레코드에 business_id 가 없었고(지점명 역추론), 화면의 지점 목록은 하드코딩이었다.
여기서는 (1) 초대가 사업자·매장 목록(targets)을 담고 호출자 테넌트 소유 사업자만 허용하는지,
(2) 수락 시 매장마다 가입요청이 그 매장의 고용주 테넌트에 생기고 한 건 실패하면 하나도 안 남는지,
(3) 목록·해석 응답이 사업자명·매장 표시를 주는지, (4) list_businesses 가 지점을 포함하는지,
(5) 화면이 하드코딩 대신 DB 목록을 쓰는지를 고정한다.

오비서 DB 는 인메모리 가짜로 흉내 낸다 — 운영 DB·AADS 인증 DB 미접촉.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import HTTPException

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-key-that-is-at-least-32-bytes-long")

from app.api import obys_finance as api  # noqa: E402
from app.services import obys_upload_service as upload_svc  # noqa: E402

svc = api.svc

EMPLOYER = "15055cac-71b0-45ec-b714-7093dde189ff"
SECOND_EMPLOYER = "3c4d5e6f-0000-4000-8000-000000000002"
OTHER_TENANT = "2a1b3c4d-0000-4000-8000-000000000001"
NEW_EMP_TENANT = "d184a18c-78c8-45c7-aee7-bfe79fadf95f"
OTHER_BIZ = "biz-other-tenant"

EMP_EMAIL = "new-hire@example.com"


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
EMPLOYEE = _user(NEW_EMP_TENANT, "owner", email=EMP_EMAIL, user_id="user-new-hire")

BUSINESS_NAMES = {
    "biz-sungshin": "열정국밥 성신여대점",
    "biz-mia": "열정국밥_미아점",
    "biz-junghwa": "열정국밥 중화점",
    "biz-eonni-naengmyeon": "언니냉면",
    OTHER_BIZ: "남의 사업자",
}
BRANCHES = {
    "biz-sungshin": ["성신여대점"],
    "biz-mia": ["열정국밥_미아점"],
    "biz-junghwa": ["중화점"],
    "biz-eonni-naengmyeon": [],
    OTHER_BIZ: ["타지점"],
}


class FakeObysDB:
    def __init__(self):
        self.rows: list[dict] = []
        self.mapping = {
            "biz-sungshin": EMPLOYER,
            "biz-mia": EMPLOYER,
            "biz-eonni-naengmyeon": EMPLOYER,
            "biz-junghwa": SECOND_EMPLOYER,
            OTHER_BIZ: OTHER_TENANT,
        }


@pytest.fixture
def db(tmp_path, monkeypatch):
    for name in ("OBYS_DATABASE_URL", "YEOLJEONG_FINANCE_DATABASE_URL", "OBYS_LEGACY_TENANT_IDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(svc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(svc, "UPLOAD_DIR", tmp_path / "data" / "uploads" / "onboarding")
    fake = FakeObysDB()

    async def fake_fetch_ledger(name, tenant_id=None):
        assert name == "employee_join_requests"
        return [dict(r) for r in fake.rows if tenant_id and r["tenant_id"] == tenant_id]

    async def fake_upsert_ledger(name, record):
        assert name == "employee_join_requests"
        for index, row in enumerate(fake.rows):
            if row["id"] == record["id"]:
                if row["tenant_id"] != record["tenant_id"]:
                    return False
                fake.rows[index] = dict(record)
                return True
        fake.rows.append(dict(record))
        return True

    async def fake_delete_hr_ledger(name, row_id, tenant_id):
        before = len(fake.rows)
        fake.rows = [r for r in fake.rows if not (r["id"] == row_id and r["tenant_id"] == tenant_id)]
        return len(fake.rows) < before

    async def fake_business_tenant_id(business_id):
        return fake.mapping.get(business_id)

    async def fake_business_tenant_matches(business_id, tenant_id):
        return fake.mapping.get(business_id) == tenant_id

    async def fake_hr_record_tenant(name, row_id):
        return next((r["tenant_id"] for r in fake.rows if r["id"] == row_id), None)

    async def fake_fetch_by_email(email):
        return [dict(r) for r in fake.rows if str(r.get("email") or "").lower() == email]

    async def fake_business_invite_info(business_id):
        if business_id not in BUSINESS_NAMES:
            return None
        return {"name": BUSINESS_NAMES[business_id], "branches": list(BRANCHES[business_id])}

    def run_db(coro):
        # 가짜 코루틴만 실행한다 — 그 밖의 DB 접근은 실패(None)로 취급해 파일 폴백을 탄다.
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
    return svc.create_invite({"name": "양재혁", "phone": "010-1234-5678", **payload}, ADMIN)


def _stored(invite_id: str) -> dict:
    return svc._find(svc._read_file_rows("employee_invites"), invite_id)


# --- 1. create_invite ---------------------------------------------------------------
def test_create_invite_stores_targets_with_one_token(db):
    invite = _invite(targets=[
        {"business_id": "biz-sungshin", "branch": "성신여대점"},
        {"business_id": "biz-mia", "branch": "열정국밥_미아점"},
    ])
    assert invite["targets"] == [
        {"business_id": "biz-sungshin", "branch": "성신여대점"},
        {"business_id": "biz-mia", "branch": "열정국밥_미아점"},
    ]
    assert invite["business_id"] == "biz-sungshin"
    assert invite["business_name"] == "열정국밥 성신여대점"
    assert invite["branch_labels"] == ["열정국밥 성신여대점 · 성신여대점", "열정국밥_미아점 · 열정국밥_미아점"]
    assert invite["token"]
    rows = svc._read_file_rows("employee_invites")
    assert len(rows) == 1
    assert rows[0]["targets"] == invite["targets"]
    assert rows[0]["business_id"] == "biz-sungshin"


def test_business_without_branches_is_a_business_level_target(db):
    invite = _invite(targets=[{"business_id": "biz-eonni-naengmyeon"}])
    assert invite["targets"] == [{"business_id": "biz-eonni-naengmyeon", "branch": ""}]
    assert invite["branch_labels"] == ["언니냉면"]


# --- 2. 다른 테넌트 사업자 ------------------------------------------------------------
@pytest.mark.parametrize("business_id", [OTHER_BIZ, "biz-junghwa", "biz-unknown", ""])
def test_business_not_owned_by_caller_tenant_is_400(db, business_id):
    with pytest.raises(HTTPException) as excinfo:
        _invite(targets=[{"business_id": "biz-sungshin", "branch": "성신여대점"}, {"business_id": business_id, "branch": "중화점"}])
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "초대할 수 없는 사업자입니다"
    assert svc._read_file_rows("employee_invites") == []


# --- 3. 그 사업자의 지점이 아닌 branch ---------------------------------------------------
def test_branch_of_another_business_is_400(db):
    with pytest.raises(HTTPException) as excinfo:
        _invite(targets=[{"business_id": "biz-sungshin", "branch": "열정국밥_미아점"}])
    assert excinfo.value.status_code == 400
    assert svc._read_file_rows("employee_invites") == []


def test_two_stores_of_one_business_are_rejected(db):
    BRANCHES["biz-mia"].append("미아2호점")
    try:
        with pytest.raises(HTTPException) as excinfo:
            _invite(targets=[
                {"business_id": "biz-mia", "branch": "열정국밥_미아점"},
                {"business_id": "biz-mia", "branch": "미아2호점"},
            ])
        assert excinfo.value.status_code == 400
    finally:
        BRANCHES["biz-mia"].remove("미아2호점")


def test_non_admin_still_cannot_invite(db):
    with pytest.raises(HTTPException) as excinfo:
        svc.create_invite({"targets": [{"business_id": "biz-sungshin", "branch": "성신여대점"}]},
                          _user(EMPLOYER, "member", email="m@example.com", user_id="user-m"))
    assert excinfo.value.status_code == 403


# --- 4. 하위호환: branch 만 ----------------------------------------------------------
def test_legacy_branch_only_builds_one_target(db):
    invite = _invite(branch="성신여대점")
    assert invite["targets"] == [{"business_id": "biz-sungshin", "branch": "성신여대점"}]
    assert invite["business_id"] == "biz-sungshin"
    assert invite["branch"] == "성신여대점"
    assert invite["branch_labels"] == ["열정국밥 성신여대점 · 성신여대점"]
    # 예전 UI 값(성신여대역점 = 언니냉면 별칭)도 종전처럼 받는다.
    legacy = _invite(branch="성신여대역점")
    assert legacy["business_id"] == "biz-eonni-naengmyeon"


# --- 5. accept: 매장마다 가입요청, 고용주 테넌트 귀속 -----------------------------------------
def test_accept_creates_one_request_per_store_in_each_employer_tenant(db):
    # 두 매장이 서로 다른 고용주 테넌트 소속인 초대(레코드를 직접 심는다).
    svc._write_file_rows("employee_invites", [{
        "id": "inv-multi", "token": "tok-multi", "name": "초대 직원", "status": "pending",
        "targets": [
            {"business_id": "biz-sungshin", "branch": "성신여대점"},
            {"business_id": "biz-junghwa", "branch": "중화점"},
        ],
    }])
    result = svc.accept_invite({"token": "tok-multi"}, EMPLOYEE)
    assert [r["business_id"] for r in result["requests"]] == ["biz-sungshin", "biz-junghwa"]
    assert result["request"] == result["requests"][0]
    assert {r["business_id"]: r["tenant_id"] for r in db.rows} == {"biz-sungshin": EMPLOYER, "biz-junghwa": SECOND_EMPLOYER}
    assert len(db.rows) == 2
    assert all(r["invite_id"] == "inv-multi" and r["status"] == "pending" for r in db.rows)
    assert all(r["requester_user_id"] == EMPLOYEE["user_id"] for r in db.rows)
    assert _stored("inv-multi")["status"] == "accepted"


def test_created_invite_round_trips_through_accept(db):
    invite = _invite(targets=[
        {"business_id": "biz-sungshin", "branch": "성신여대점"},
        {"business_id": "biz-mia", "branch": "열정국밥_미아점"},
        {"business_id": "biz-eonni-naengmyeon"},
    ])
    result = svc.accept_invite({"token": invite["token"], "branch": "중화점"}, EMPLOYEE)
    assert [r["business_id"] for r in result["requests"]] == ["biz-sungshin", "biz-mia", "biz-eonni-naengmyeon"]
    assert {r["tenant_id"] for r in db.rows} == {EMPLOYER}
    assert len(db.rows) == 3
    # 같은 초대를 다시 수락해도 요청이 늘지 않는다(이메일+사업자 키).
    svc.accept_invite({"token": invite["token"]}, EMPLOYEE)
    assert len(db.rows) == 3


def test_legacy_invite_without_targets_still_accepts_single_request(db):
    svc._write_file_rows("employee_invites", [{"id": "inv-old", "token": "tok-old", "branch": "성신여대점", "status": "pending"}])
    result = svc.accept_invite({"token": "tok-old", "name": "양재혁", "branch": "미아점"}, EMPLOYEE)
    assert result["request"]["business_id"] == "biz-sungshin"
    assert len(result["requests"]) == 1
    assert result["business_id"] == "biz-sungshin"  # 단일 매장 호출부 호환


# --- 6. 한 건이라도 실패하면 전체 400, 부분 생성 없음 --------------------------------------------
def test_accept_with_one_invalid_store_creates_nothing(db):
    svc._write_file_rows("employee_invites", [{
        "id": "inv-bad", "token": "tok-bad", "status": "pending",
        "targets": [
            {"business_id": "biz-sungshin", "branch": "성신여대점"},
            {"business_id": "biz-unmapped", "branch": ""},
        ],
    }])
    with pytest.raises(HTTPException) as excinfo:
        svc.accept_invite({"token": "tok-bad"}, EMPLOYEE)
    assert excinfo.value.status_code == 400
    assert db.rows == []
    assert _stored("inv-bad")["status"] == "pending"


def test_accept_rolls_back_when_a_later_write_fails(db, monkeypatch):
    # 기존 가입요청이 있던 매장은 이전 값으로, 새로 만든 매장은 삭제로 되돌린다.
    svc.upsert_join_request({"name": "기존", "email": EMP_EMAIL, "branch": "성신여대점", "memo": "처음"}, ADMIN)
    before = [dict(r) for r in db.rows]
    svc._write_file_rows("employee_invites", [{
        "id": "inv-w", "token": "tok-w", "status": "pending",
        "targets": [
            {"business_id": "biz-sungshin", "branch": "성신여대점"},
            {"business_id": "biz-mia", "branch": "열정국밥_미아점"},
            {"business_id": "biz-junghwa", "branch": "중화점"},
        ],
    }])
    original = svc._write_hr_record
    calls = {"n": 0}

    def flaky(name, record, user):
        calls["n"] += 1
        if record.get("business_id") == "biz-junghwa" and not str(record.get("memo") or "").startswith("처음"):
            raise HTTPException(status_code=503, detail="저장 실패")
        return original(name, record, user)

    monkeypatch.setattr(svc, "_write_hr_record", flaky)
    with pytest.raises(HTTPException) as excinfo:
        svc.accept_invite({"token": "tok-w"}, EMPLOYEE)
    assert excinfo.value.status_code == 400
    assert db.rows == before
    assert _stored("inv-w")["status"] == "pending"


# --- 7. list_invites / resolve_invite ------------------------------------------------------------
def test_list_invites_has_business_name_and_labels(db):
    _invite(targets=[{"business_id": "biz-sungshin", "branch": "성신여대점"}, {"business_id": "biz-mia", "branch": "열정국밥_미아점"}])
    svc._write_file_rows("employee_invites", [
        *svc._read_file_rows("employee_invites"),
        {"id": "inv-old", "token": "tok-old", "name": "예전", "branch": "열정국밥_미아점", "status": "pending", "created_at": "2020-01-01T00:00:00+09:00"},
    ])
    listed = svc.list_invites(ADMIN)
    new, old = listed[0], listed[1]
    assert new["business_id"] == "biz-sungshin"
    assert new["business_name"] == "열정국밥 성신여대점"
    assert new["branch_labels"] == ["열정국밥 성신여대점 · 성신여대점", "열정국밥_미아점 · 열정국밥_미아점"]
    assert len(new["targets"]) == 2
    # targets 없는 예전 초대도 지점명에서 사업자를 유추해 보여 준다.
    assert old["business_id"] == "biz-mia"
    assert old["business_name"] == "열정국밥_미아점"
    assert old["branch_labels"] == ["열정국밥_미아점 · 열정국밥_미아점"]
    assert svc.list_invites(_user(EMPLOYER, "member", email="m@example.com", user_id="user-m")) == []


def test_resolve_invite_shows_stores_without_returning_the_token(db):
    invite = _invite(targets=[{"business_id": "biz-sungshin", "branch": "성신여대점"}, {"business_id": "biz-mia", "branch": "열정국밥_미아점"}])
    resolved = svc.resolve_invite(invite["token"])
    assert "token" not in resolved
    assert resolved["business_name"] == "열정국밥 성신여대점"
    assert resolved["targets"] == invite["targets"]
    assert resolved["branch_labels"] == invite["branch_labels"]
    assert resolved["id"] == invite["id"]
    with pytest.raises(HTTPException) as excinfo:
        svc.resolve_invite("no-such-token")
    assert excinfo.value.status_code == 404


# --- 8. list_businesses 가 지점을 담는다 -----------------------------------------------------------
class RegistryConn:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []
        self.businesses = [
            {"id": "biz-a", "entity_type": "individual", "name": "A 사업자", "registration_no": "123-45-67890", "representative": "대표",
             "tax_type": "일반과세", "opened_at": "2024-01-01", "address": "서울", "memo": "", "created_at": None, "updated_at": None},
            {"id": "biz-b", "entity_type": "individual", "name": "B 사업자", "registration_no": "", "representative": "",
             "tax_type": "", "opened_at": "", "address": "", "memo": "", "created_at": None, "updated_at": None},
        ]
        self.branches = [
            {"id": "branch-a1", "business_id": "biz-a", "name": "A1점", "status": "active"},
            {"id": "branch-a2", "business_id": "biz-a", "name": "A2점", "status": "inactive"},
        ]

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if "FROM yeoljeong_businesses" in sql:
            return [dict(r) for r in self.businesses]
        if "FROM yeoljeong_branches" in sql:
            ids = set(args[0])
            return [dict(r) for r in self.branches if r["business_id"] in ids]
        raise AssertionError(sql)

    async def close(self):
        return None


def test_list_businesses_includes_branches_and_keeps_existing_keys(monkeypatch):
    conn = RegistryConn()

    async def connect():
        return conn

    monkeypatch.setattr(upload_svc, "_connect", connect)
    rows = asyncio.run(upload_svc.list_businesses(user=_user(EMPLOYER, "owner", email="o@example.com", user_id="u")))
    by_id = {r["id"]: r for r in rows}
    assert by_id["biz-a"]["branches"] == [
        {"id": "branch-a1", "name": "A1점", "status": "active"},
        {"id": "branch-a2", "name": "A2점", "status": "inactive"},
    ]
    assert by_id["biz-b"]["branches"] == []
    for key in ("id", "entity_type", "name", "registration_no", "representative", "tax_type", "opened_at", "address", "memo",
                "created_at", "updated_at", "needs_registration_info", "missing_registration_fields"):
        assert key in by_id["biz-a"]
    assert by_id["biz-b"]["needs_registration_info"] is True
    branch_sql, branch_args = conn.calls[1]
    assert "deleted_at IS NULL" in branch_sql
    assert branch_args == (["biz-a", "biz-b"],)  # 이 테넌트 사업자 id 로만 좁힌다
    assert conn.calls[0][1] == (UUID(EMPLOYER),)


# --- 9. API 모델·게이트·화면 -----------------------------------------------------------------------
def test_invite_create_model_accepts_targets_and_keeps_branch():
    model = api.InviteCreate(branch="중화점", targets=[{"business_id": "biz-junghwa", "branch": "중화점"}, {"business_id": "biz-x"}])
    dumped = model.model_dump()
    assert dumped["branch"] == "중화점"
    assert dumped["targets"] == [{"business_id": "biz-junghwa", "branch": "중화점"}, {"business_id": "biz-x", "branch": ""}]
    assert api.InviteCreate().targets == []


def test_invites_list_stays_closed_to_tenant_scoped_gate():
    from app.core import obys_tenant

    path = "/api/v1/yeoljeong-finance/employees/invites"
    assert obys_tenant._is_tenant_scoped_path(path, "GET") is False
    assert obys_tenant._is_tenant_scoped_path(path, "POST") is False


def test_v41_screen_uses_db_businesses_and_multi_select_targets():
    html = Path("app/static/apps/obys/mockup-v4-1.html").read_text(encoding="utf-8")
    assert "INVITE_BRANCHES_V41" not in html
    assert 'financeApiV41("/tenant-registry/businesses")' in html
    assert 'type="checkbox" name="invite_target"' in html
    assert "매장을 한 곳 이상 고르십시오" in html
    assert "targets" in html and 'financeApiV41("/employees/invites")' in html
    assert "<th>직원</th><th>매장</th><th>권한</th><th>만료</th><th>상태</th>" in html
    assert "승인 대기 가입요청" in html and "data-join-approve" in html


def test_index_invite_table_has_store_column_and_colspan_six():
    html = Path("app/static/apps/obys/index.html").read_text(encoding="utf-8")
    head = html[html.index("<thead>", html.index('id="employeeInviteRows"') - 600):html.index('id="employeeInviteRows"')]
    assert "<th>매장</th>" in head
    assert head.count("<th>") == 6
    start = html.index("if (!els.employeeInviteRows) return;")
    block = html[start:html.index("renderEmployeeJoinRows();", start)]
    assert 'colspan="5"' not in block.replace('els.employeeJoinRows.innerHTML = `<tr><td colspan="5">', "")
    assert block.count('colspan="6"') == 3
    assert "branch_labels" in block
