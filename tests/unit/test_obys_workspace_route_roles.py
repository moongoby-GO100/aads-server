"""오비서 범용 workspace 읽기 API 의 라우트별 역할표 — 급여·직원·인사 등 9개는 owner/admin 만, 근태는 member 본인 행만.

재현(수정 전): member·viewer 멤버십으로 /payroll/records 등을 부르면 200 + 사업자 전 직원 행이 나왔다.
직원 포털 승인 직원의 테넌트 멤버십 역할은 member 다(yeoljeong_finance_service 의 승인 → 멤버십 흐름).
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import obys_workspaces as api

TENANT = "d1695f15-6b68-4929-bc8d-646827363ff9"
BUSINESS = {"id": "biz-lylon-e2e", "name": "주식회사 라일론"}
REAL_SOURCE_ROWS = api._source_rows
OTHER_EMPLOYEE_ROW = {"id": "pay-1", "employee_name": "다른직원", "gross_pay": 3000000, "net_pay": 2700000,
                      "status": "confirmed"}

RESTRICTED = ["payroll", "employees", "hr-docs", "audit-history", "access-log",
              "integrations", "bank-connect", "sales-connect", "tax-connect"]
OPEN_ROUTES = ["sales", "purchases", "inventory", "branches", "alerts", "approvals", "tasks"]


def _user(role: str) -> dict:
    return {"tenant_id": TENANT, "email": f"{role}@example.com",
            "current_membership": {"tenant_id": TENANT, "status": "active", "role": role}}


@pytest.fixture(autouse=True)
def _stub_sources(monkeypatch):
    calls: list[str] = []

    async def business(user, business_id):
        return BUSINESS

    async def rows(**kwargs):
        calls.append(kwargs["route"])
        return [("row", dict(OTHER_EMPLOYEE_ROW))], "stub"

    async def local_rows(*args, **kwargs):
        calls.append("local")
        return []

    async def remote(*args, **kwargs):
        calls.append("remote")
        return [], "stub"

    monkeypatch.setattr(api, "_business", business)
    monkeypatch.setattr(api, "_source_rows", rows)
    monkeypatch.setattr(api, "_local_source_rows", local_rows)
    monkeypatch.setattr(api.acct_source_ledger, "source_transactions", remote)
    return calls


async def _records(route, user):
    return await api.workspace_records(
        BUSINESS["id"], route, None, None, "전체 상태", "", 200, 0, user)


async def _summary(route, user):
    return await api.workspace_summary(BUSINESS["id"], route, None, None, user)


async def _detail(route, user):
    return await api.workspace_record_detail(BUSINESS["id"], route, "pay-1", user)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", RESTRICTED)
@pytest.mark.parametrize("role", ["member", "viewer"])
@pytest.mark.parametrize("call", [_records, _summary, _detail])
async def test_member_and_viewer_are_forbidden_on_restricted_routes(route, role, call, _stub_sources):
    with pytest.raises(HTTPException) as err:
        await call(route, _user(role))
    assert err.value.status_code == 403
    assert "소유자·관리자" in err.value.detail
    assert _stub_sources == [], "권한 검사 전에 데이터 원천을 조회하면 안 된다"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["applicant", "employee", "", "removed"])
async def test_non_member_roles_are_forbidden(role):
    with pytest.raises(HTTPException) as err:
        await _records("payroll", _user(role))
    assert err.value.status_code == 403


@pytest.mark.asyncio
async def test_inactive_membership_is_forbidden():
    user = _user("owner")
    user["current_membership"]["status"] = "removed"
    with pytest.raises(HTTPException) as err:
        await _records("payroll", user)
    assert err.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("route", RESTRICTED)
@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_owner_and_admin_can_read_restricted_routes(route, role):
    records = await _records(route, _user(role))
    assert records["route"] == route
    assert records["count"] == 1
    summary = await _summary(route, _user(role))
    assert summary["route"] == route


@pytest.mark.asyncio
async def test_owner_can_read_restricted_record_detail():
    detail = await _detail("payroll", _user("admin"))
    assert detail["record"]["id"] == "pay-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["sales", "inventory", "branches", "alerts", "approvals", "tasks"])
@pytest.mark.parametrize("role", ["owner", "admin", "member", "viewer"])
async def test_open_routes_keep_current_behaviour_for_all_roles(route, role):
    records = await _records(route, _user(role))
    assert records["route"] == route


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["member", "viewer"])
async def test_open_acct_route_still_readable(role):
    records = await _records("purchases", _user(role))
    assert records["route"] == "purchases"


@pytest.mark.asyncio
async def test_open_routes_do_not_require_membership_shape():
    records = await _records("sales", {"tenant_id": TENANT})
    assert records["route"] == "sales"


@pytest.mark.asyncio
async def test_unknown_route_keeps_404_before_role_check():
    with pytest.raises(HTTPException) as err:
        await _records("no-such-route", _user("viewer"))
    assert err.value.status_code == 404


def test_role_table_is_admin_only_for_sensitive_routes_and_within_known_routes():
    assert set(api.ROUTE_READ_ROLES) == set(RESTRICTED)
    assert "attendance" not in api.ROUTE_READ_ROLES
    assert set(api.ROUTE_READ_ROLES) <= api.ROUTES
    assert all(roles == frozenset({"owner", "admin"}) for roles in api.ROUTE_READ_ROLES.values())
    assert not set(OPEN_ROUTES) & set(api.ROUTE_READ_ROLES)


# ── attendance: owner/admin 전체, member 본인 행만, viewer 403 ──────────────────

def _att_row(row_id, email, name, wage):
    return {"id": row_id, "employee_email": email, "employee_name": name, "branch": "본점",
            "work_date": "2026-10-15", "start_at": "09:00", "end_at": "18:00", "break_minutes": 60,
            "worked_minutes": 480, "hourly_wage": wage, "status": "정상", "memo": "",
            "created_at": None, "updated_at": None}


ATT_ROWS = [_att_row("att-1", "Me@Example.com", "나", 11000),
            _att_row("att-2", "other@example.com", "다른직원", 99999),
            _att_row("att-3", "me@example.com", "나", 11000)]


class AttConnection:
    def __init__(self, log):
        self.log = log

    async def fetch(self, sql, *args):
        self.log.append((sql, args))
        assert "yeoljeong_attendance_records" in sql
        business_id, *rest = args
        rows = [r for r in ATT_ROWS]
        if "lower(employee_email)=$2" in sql:
            rows = [r for r in rows if r["employee_email"].lower() == rest[0]]
        else:
            raise AssertionError("attendance 목록은 owner/admin 이어도 member 경로 SQL 만 여기로 온다")
        return rows

    async def fetchrow(self, sql, *args):
        self.log.append((sql, args))
        rows = [r for r in ATT_ROWS if r["id"] == args[0]]
        if "lower(employee_email)=$3" in sql:
            rows = [r for r in rows if r["employee_email"].lower() == args[2]]
        return rows[0] if rows else None

    async def close(self):
        pass


@pytest.fixture
def att_db(monkeypatch):
    log: list = []

    async def connect():
        return AttConnection(log)

    monkeypatch.setattr(api.upload_svc, "_connect", connect)
    monkeypatch.setattr(api, "_source_rows", REAL_SOURCE_ROWS)
    return log


def _member(email="me@example.com"):
    return {**_user("member"), "email": email}


@pytest.mark.asyncio
async def test_member_records_are_own_rows_only_and_filtered_in_sql(att_db):
    result = await _records("attendance", _member("ME@example.com"))
    assert [r["id"] for r in result["records"]] == ["att-1", "att-3"]
    dumped = str(result)
    assert "다른직원" not in dumped and "99999" not in dumped
    sql, args = att_db[0]
    assert "business_id=$1 AND deleted_at IS NULL AND lower(employee_email)=$2" in sql
    assert args == (BUSINESS["id"], "me@example.com")


@pytest.mark.asyncio
async def test_member_summary_counts_only_own_rows(att_db):
    summary = await _summary("attendance", _member())
    assert summary["source"]["record_count"] == 2
    assert summary["metrics"][0] == {"label": "DB 건수", "value": "2건"}
    assert "99999" not in str(summary)


@pytest.mark.asyncio
@pytest.mark.parametrize("email", ["", "   ", None])
async def test_member_without_email_gets_empty_list_without_db(monkeypatch, email):
    async def connect():
        raise AssertionError("이메일이 없으면 DB 를 조회하면 안 된다")

    monkeypatch.setattr(api.upload_svc, "_connect", connect)
    monkeypatch.setattr(api, "_source_rows", REAL_SOURCE_ROWS)
    user = {**_user("member"), "email": email}
    assert (await _records("attendance", user))["records"] == []
    assert (await _summary("attendance", user))["source"]["record_count"] == 0
    with pytest.raises(HTTPException) as err:
        await _detail("attendance", user)
    assert err.value.status_code == 404
    with pytest.raises(HTTPException) as err:
        await api.get_attendance_record(BUSINESS["id"], "att-1", user)
    assert err.value.status_code == 404


@pytest.mark.asyncio
async def test_member_detail_own_row_ok_other_row_not_found(att_db):
    own = await api.workspace_record_detail(BUSINESS["id"], "attendance", "att-1", _member())
    assert own["record"]["id"] == "att-1"
    with pytest.raises(HTTPException) as err:
        await api.workspace_record_detail(BUSINESS["id"], "attendance", "att-2", _member())
    assert err.value.status_code == 404


@pytest.mark.asyncio
async def test_member_get_attendance_record_other_row_is_404(att_db):
    own = await api.get_attendance_record(BUSINESS["id"], "att-3", _member())
    assert own["record"]["id"] == "att-3"
    with pytest.raises(HTTPException) as err:
        await api.get_attendance_record(BUSINESS["id"], "att-2", _member())
    assert err.value.status_code == 404
    sql, args = att_db[-1]
    assert "lower(employee_email)=$3" in sql and args[2] == "me@example.com"


@pytest.mark.asyncio
@pytest.mark.parametrize("call", [_records, _summary, _detail])
async def test_viewer_forbidden_on_attendance(call, _stub_sources, monkeypatch):
    async def connect():
        raise AssertionError("권한 검사 전에 DB 에 접속하면 안 된다")

    monkeypatch.setattr(api.upload_svc, "_connect", connect)
    with pytest.raises(HTTPException) as err:
        await call("attendance", _user("viewer"))
    assert err.value.status_code == 403
    assert _stub_sources == []


@pytest.mark.asyncio
async def test_viewer_forbidden_on_attendance_detail_route(monkeypatch):
    async def connect():
        raise AssertionError("권한 검사 전에 DB 에 접속하면 안 된다")

    monkeypatch.setattr(api.upload_svc, "_connect", connect)
    with pytest.raises(HTTPException) as err:
        await api.get_attendance_record(BUSINESS["id"], "att-1", _user("viewer"))
    assert err.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_owner_and_admin_see_all_attendance_rows(role, _stub_sources, monkeypatch):
    records = await _records("attendance", _user(role))
    assert records["count"] == 1 and _stub_sources == ["attendance"]
    assert (await _summary("attendance", _user(role)))["route"] == "attendance"
    assert (await _detail("attendance", _user(role)))["record"]["id"] == "pay-1"


@pytest.mark.asyncio
async def test_owner_get_attendance_record_has_no_email_filter(att_db):
    result = await api.get_attendance_record(BUSINESS["id"], "att-2", _user("owner"))
    assert result["record"]["id"] == "att-2"
    sql, args = att_db[-1]
    assert "lower(employee_email)" not in sql and len(args) == 2
