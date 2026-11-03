"""근무표 정책·권한·격리·일괄 상한 회귀 검증."""

import shutil
import subprocess
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException

from app.api import obys_workspaces as api
from app.auth import get_current_user

NOW = datetime(2026, 10, 30, 8, tzinfo=UTC)
SCHEDULE = {
    "employee_email": "staff@example.com",
    "work_date": date(2026, 10, 29),
    "start_time": "09:00",
    "end_time": "18:00",
    "break_minutes": 60,
    "kind": "work",
}


@pytest.mark.parametrize(
    "schedule,record,label,minutes",
    [
        (
            SCHEDULE,
            {"start_at": "09:00", "end_at": "18:00", "break_minutes": 60},
            "정상",
            (0, 0, 0),
        ),
        (
            SCHEDULE,
            {"start_at": "09:05", "end_at": "18:00", "break_minutes": 60},
            "지각",
            (5, 0, 0),
        ),
        (
            SCHEDULE,
            {"start_at": "09:00", "end_at": "17:50", "break_minutes": 60},
            "조퇴",
            (0, 10, 0),
        ),
        (SCHEDULE, None, "확인필요", (0, 0, 0)),
        (
            SCHEDULE,
            {"start_at": "09:00", "end_at": "19:00", "break_minutes": 60},
            "초과",
            (0, 0, 60),
        ),
        (None, {"start_at": "09:00"}, "일정없음", (0, 0, 0)),
        ({**SCHEDULE, "kind": "off"}, None, "휴무", (0, 0, 0)),
        ({**SCHEDULE, "kind": "leave"}, None, "휴무", (0, 0, 0)),
        ({**SCHEDULE, "work_date": date(2026, 10, 30)}, None, "미출근", (0, 0, 0)),
    ],
)
def test_judgment_table(schedule, record, label, minutes):
    result = api.attendance_schedule_judgment(schedule, record, now=NOW)
    assert result["labels"] == [label]
    assert (
        tuple(result[k] for k in ("late_minutes", "early_minutes", "overtime_minutes")) == minutes
    )
    assert api.ATTENDANCE_LATE_GRACE_MINUTES == 0


def test_kst_midnight_and_overnight():
    s = {**SCHEDULE, "work_date": date(2026, 10, 30)}
    assert (
        api.attendance_schedule_judgment(s, now=datetime(2026, 10, 30, 14, 59, tzinfo=UTC))[
            "status"
        ]
        == "미출근"
    )
    assert (
        api.attendance_schedule_judgment(s, now=datetime(2026, 10, 30, 15, 0, tzinfo=UTC))["status"]
        == "확인필요"
    )
    s.update(start_time="22:00", end_time="06:00")
    r = {"start_at": "22:00", "end_at": "06:00", "break_minutes": 60}
    assert api.attendance_schedule_judgment(s, r, now=NOW)["status"] == "정상"
    r.update(
        start_at="00:10", check_in_at="2026-10-30T15:10:00Z", check_out_at="2026-10-30T21:00:00Z"
    )
    assert api.attendance_schedule_judgment(s, r, now=NOW)["late_minutes"] == 130


class Connection:
    def __init__(self):
        self.calls = []
        self.rows = [
            {
                "id": "s1",
                "business_id": "biz-a",
                **SCHEDULE,
                "employee_name": "직원",
                "actual_record": None,
            }
        ]

    async def close(self):
        pass

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetch(self, sql, business, *args):
        if "UPDATE yeoljeong_work_schedules SET deleted_at" in sql:
            emails, start, end, weekdays = args
            retired = []
            for row in self.rows:
                day = date.fromisoformat(str(row["work_date"])[:10])
                if (row["business_id"] == business and row["employee_email"] in emails
                        and start <= day <= end and day.weekday() not in weekdays
                        and row.get("kind") == "work" and not row.get("memo", "")
                        and not row.get("deleted_at")):
                    row["deleted_at"] = "fixture-retired"
                    retired.append(dict(row))
            return retired
        if "WITH ranked" in sql:
            return [{**r["actual_record"], "employee_email": r["employee_email"], "work_date": r["work_date"]}
                    for r in self.rows if r.get("actual_record")]
        start, end, email, _offset, tenant = args
        self.calls.append((sql, business, email, tenant))
        assert "s.business_id=$1" in sql and "tenant_id=$6::uuid" in sql
        return [
            r
            for r in self.rows
            if r["business_id"] == business
            and (email is None or email == r["employee_email"])
            and start <= r["work_date"] <= end
        ]

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if sql.lstrip().startswith("INSERT"):
            keys = [
                "id",
                "business_id",
                "employee_email",
                "employee_email_masked",
                "employee_name",
                "branch",
                "work_date",
                "start_time",
                "end_time",
                "break_minutes",
                "kind",
                "memo",
                "created_by",
            ]
            return dict(zip(keys, args))
        assert "WHERE business_id=$1 AND id=$2 AND deleted_at IS NULL" in sql
        row = next(
            (r for r in self.rows if r["business_id"] == args[0] and r["id"] == args[1]), None
        )
        if row is not None and sql.lstrip().startswith("UPDATE"):
            keys = ("employee_email", "employee_email_masked", "employee_name", "branch",
                    "work_date", "start_time", "end_time", "break_minutes", "kind", "memo")
            row.update(zip(keys, args[2:]))
        return row

    async def execute(self, *args):
        return "INSERT 0 1"


@pytest_asyncio.fixture
async def client(monkeypatch):
    user = {
        "tenant_id": "d1695f15-6b68-4929-bc8d-646827363ff9",
        "email": "staff@example.com",
        "current_membership": {
            "tenant_id": "d1695f15-6b68-4929-bc8d-646827363ff9",
            "status": "active",
            "role": "member",
        },
    }
    db = Connection()
    monkeypatch.setattr(api, "_legacy_people_rows", AsyncMock(return_value=[]))

    async def businesses(*, user):
        return (
            [{"id": "biz-a", "name": "사업자"}]
            if user["tenant_id"] == "d1695f15-6b68-4929-bc8d-646827363ff9"
            else []
        )

    monkeypatch.setattr(api.upload_svc, "list_businesses", businesses)
    monkeypatch.setattr(api.upload_svc, "_connect", AsyncMock(return_value=db))
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    async def current_user():
        return user
    app.dependency_overrides[get_current_user] = current_user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http, user, db


BASE = "/api/v1/workspaces/biz-a/attendance/schedules"
PAYLOAD = {**SCHEDULE, "work_date": "2026-10-29"}


@pytest.mark.asyncio
async def test_member_own_scope_and_write_denial(client):
    http, _user, db = client
    response = await http.get(BASE + "?from=2026-10-01&to=2026-10-31&employee_email=other@example.com")
    assert response.status_code == 200 and len(response.json()["schedules"]) == 1
    assert db.calls[-1][2] == "staff@example.com"
    for method, path, payload in [
        ("post", BASE, PAYLOAD),
        ("put", BASE + "/s1", PAYLOAD),
        ("delete", BASE + "/s1", None),
        (
            "post",
            BASE + "/bulk",
            {
                "employees": [{"employee_email": "staff@example.com"}],
                "date_from": "2026-10-29",
                "date_to": "2026-10-29",
                "weekdays": [3],
                "kind": "off",
            },
        ),
    ]:
        response = await http.request(method, path, **({"json": payload} if payload is not None else {}))
        assert response.status_code == 403


@pytest.mark.parametrize("foreign_tenant", [False, True])
@pytest.mark.asyncio
async def test_foreign_business_and_tenant_forbidden(client, foreign_tenant):
    http, user, db = client
    user["current_membership"]["role"] = "owner"
    path = BASE.replace("biz-a", "biz-other")
    if foreign_tenant:
        user["tenant_id"] = "11111111-1111-1111-1111-111111111111"
        user["current_membership"]["tenant_id"] = user["tenant_id"]
        path = BASE
    for method, target, payload in [
        ("get", path, None),
        ("post", path, PAYLOAD),
        ("put", path + "/s1", PAYLOAD),
        ("delete", path + "/s1", None),
    ]:
        assert (
            (await http.request(method, target, **({"json": payload} if payload else {}))).status_code
            == 403
        )
    assert not db.calls


@pytest.mark.asyncio
async def test_admin_bulk_cap_and_create_update_delete_scope(client):
    http, user, db = client
    user["current_membership"]["role"] = "owner"
    bulk = {
        "employees": [{"employee_email": f"e{i}@example.com"} for i in range(1000)],
        "date_from": "2026-10-29",
        "date_to": "2026-10-30",
        "weekdays": [3, 4],
        "kind": "off",
    }
    assert (await http.post(BASE + "/bulk", json=bulk)).status_code == 422
    assert not db.calls
    assert (await http.post(BASE, json=PAYLOAD)).status_code == 201
    assert (await http.put(BASE + "/foreign-id", json=PAYLOAD)).status_code == 403
    assert (await http.delete(BASE + "/foreign-id")).status_code == 403
    assert (await http.delete(BASE + "/s1")).status_code == 200
    bulk["employees"] = [{"employee_email": "a@example.com"}]
    assert (await http.post(BASE + "/bulk", json=bulk)).json()["count"] == 2


def test_contract_difference_absent_and_changed():
    s = {**SCHEDULE, "start_time": "09:00", "end_time": "18:00"}
    assert api._contract_schedule_difference(s, {}) is None
    assert not api._contract_schedule_difference(
        s, {"daily_work_schedule": "목 09:00-18:00 (휴게 60분, 실근로 8시간)"}
    )["different"]
    assert api._contract_schedule_difference(
        s, {"dailyWorkSchedule": "목 10:00-19:00 (휴게 60분)"}
    )["different"]


def test_migration_and_ui_contract():
    sql = Path("migrations/20261030_obys_work_schedules.sql").read_text()
    import re

    assert "CREATE TABLE IF NOT EXISTS" in sql and "CREATE UNIQUE INDEX IF NOT EXISTS" in sql
    assert not re.search(r"\b(DROP|TRUNCATE|DELETE)\b", sql, re.IGNORECASE)
    assert "WHERE deleted_at IS NULL" in sql
    # 운영 앱 역할 권한이 없으면 근무표 API 가 permission denied 로 실패한다.
    assert "GRANT SELECT, INSERT, UPDATE ON public.yeoljeong_work_schedules TO acct_business_runtime_r5" in sql
    # 배포 스크립트는 적용 후 probe 로 확인하므로, probe 가 없으면 배포가 exit 7 로 실패한다.
    deploy = Path("scripts/deploy_acct_app_cafe24.sh").read_text()
    probe = deploy.split("migration_probe_sql()", 1)[1].split("*) return 1", 1)[0]
    assert "20261030_obys_work_schedules.sql)" in probe and "yeoljeong_work_schedules" in probe
    assert (
        "20261030_obys_work_schedules.sql"
        in Path("scripts/deploy_acct_app_cafe24.sh")
        .read_text()
        .split("ACCT_AUTH_MIGRATION_ALLOWLIST")[0]
    )
    html = Path("app/static/apps/obys/admin.html").read_text()
    for marker in (
        "scheduleBulk",
        "weeklyRoster",
        "data-roster-save",
        "주간 요일 패턴",
        "근무표 미등록 — 근무 일정 탭에서 등록",
    ):
        assert marker in html
    assert "근무표 미연결" not in html
    # 근무 일정은 이메일을 직접 치지 않고 승인 직원 목록에서 고른다(2026-10-30 대표 지시).
    assert "scheduleEmployeePicker" in html and "schedulePickAll" in html
    assert "/employees/approved?business_id=" in html
    assert "근무표 직원 이메일" not in html and 'name="emails"' not in html
    assert "근무 일정을 등록할 직원을 선택하세요." in html
    assert "employeeWorkSchedules" in Path("app/static/apps/obys/index.html").read_text()


@pytest.mark.skipif(not shutil.which("node"), reason="node unavailable")
def test_inline_scripts_parse():
    import re

    for path in ("admin.html", "index.html"):
        html = Path("app/static/apps/obys", path).read_text()
        for source in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, re.DOTALL):
            subprocess.run(
                ["node", "--check"], input=source, text=True, capture_output=True, check=True
            )


def test_split_shifts_use_daily_total_and_combined_labels():
    records = [
        {"start_at": "09:00", "end_at": "12:00", "break_minutes": 0},
        {"start_at": "13:00", "end_at": "18:00", "break_minutes": 0},
    ]
    assert api.attendance_schedule_judgment(SCHEDULE, records, now=NOW)["status"] == "정상"
    result = api.attendance_schedule_judgment(
        SCHEDULE, {"start_at": "09:10", "end_at": "17:50", "break_minutes": 60}, now=NOW
    )
    assert result["labels"] == ["지각", "조퇴"]
    assert result["late_minutes"] == result["early_minutes"] == 10


def test_bulk_exact_limit_and_invalid_patterns():
    payload = api.WorkScheduleBulkIn(
        employees=[{"employee_email": f"e{i}@example.com"} for i in range(1000)],
        date_from="2026-10-29",
        date_to="2026-10-29",
        weekdays=[3],
        kind="off",
    )
    assert len(api.expand_work_schedules(payload)) == 1000
    with pytest.raises(HTTPException) as error:
        api.expand_work_schedules(payload.model_copy(update={"weekdays": [7]}))
    assert error.value.status_code == 422
    with pytest.raises(HTTPException):
        api._schedule_values(api.WorkScheduleIn(**{**PAYLOAD, "break_minutes": 600}))


@pytest.mark.asyncio
async def test_schedule_list_includes_server_counts_and_contract_notice(client):
    http, _user, db = client
    db.rows[0].update(
        actual_record={"start_at": "09:07", "end_at": "18:00", "break_minutes": 60},
        contract_payload={"daily_work_schedule": "목 10:00-19:00 (휴게 60분)"},
    )
    response = await http.get(BASE + "?from=2026-10-29&to=2026-10-29")
    assert response.status_code == 200
    data = response.json()
    assert data["counts"]["지각"] == 1 and data["counts"]["출근"] == 1
    assert data["schedules"][0]["judgment"]["late_minutes"] == 7
    assert data["schedules"][0]["contract_difference"]["different"]
    assert (
        data["schedules"][0]["employee_key"]
        == api._record("generic", SCHEDULE, "사업자")["employee_key"]
    )


@pytest.mark.asyncio
async def test_bulk_cells_apply_only_selected_dates(client):
    http, user, _db = client
    user["current_membership"]["role"] = "owner"
    payload = {
        "employees": [{"employee_email": "staff@example.com"}],
        "date_from": "2026-10-29",
        "date_to": "2026-10-30",
        "weekdays": [3, 4],
        "kind": "off",
        "cells": [{**PAYLOAD, "kind": "off"}],
    }
    response = await http.post(BASE + "/bulk", json=payload)
    assert response.status_code == 200 and response.json()["count"] == 1
    assert response.json()["schedules"][0]["work_date"] == "2026-10-29"


@pytest.mark.parametrize('total', [None, 480])
def test_nullable_total_worked(total):
    record = {'start_at': '09:00', 'end_at': '18:00', 'break_minutes': 60,
              'total_worked_minutes': total}
    assert api.attendance_schedule_judgment(SCHEDULE, record, now=NOW)['status'] == '정상'


def test_timestamp_shifts_and_malformed_record_isolation():
    rows = [
        {'start_at': '2026-10-29T00:00:00Z', 'end_at': '2026-10-29T03:00:00Z'},
        {'start_at': '2026-10-29T04:00:00Z', 'end_at': '2026-10-29T09:00:00Z'},
        {'start_at': 'broken', 'end_at': 'broken'},
    ]
    assert api.attendance_schedule_judgment(SCHEDULE, rows, now=NOW)['status'] == '정상'


@pytest.mark.asyncio
async def test_list_response_preserves_judgment_for_immutable_records(monkeypatch):
    from types import MappingProxyType
    row = MappingProxyType({**SCHEDULE, 'id': 'a', 'start_at': '09:05', 'end_at': '18:00'})
    db = AsyncMock()
    db.fetch.return_value = [row]
    monkeypatch.setattr(api, '_business', AsyncMock(return_value={'id': 'biz-a', 'name': '사업자'}))
    monkeypatch.setattr(api, '_require_route_read', lambda *args: None)
    monkeypatch.setattr(api.upload_svc, '_connect', AsyncMock(return_value=db))
    monkeypatch.setattr(api, '_legacy_people_rows', AsyncMock(return_value=[]))
    monkeypatch.setattr(api, '_attendance_schedule_matches', AsyncMock(return_value={api._schedule_record_key(row): SCHEDULE}))
    result = await api.list_attendance_records('biz-a', limit=200, offset=0, date_from=None,
        date_to=None, employee_email=None, current_user={})
    assert result['records'][0]['raw']['judgment']['late_minutes'] == 5
    assert 'judgment' not in row


@pytest.mark.asyncio
async def test_legacy_enrichment_is_one_read(monkeypatch):
    db = AsyncMock()
    db.fetch.side_effect = [[{**SCHEDULE, 'id': str(i)} for i in range(1000)], []]
    legacy = AsyncMock(return_value=[])
    monkeypatch.setattr(api, '_schedule_business', AsyncMock(return_value={'name': '사업자'}))
    monkeypatch.setattr(api, '_require_route_read', lambda *args: None)
    monkeypatch.setattr(api.upload_svc, '_tenant', lambda user: 'tenant')
    monkeypatch.setattr(api.upload_svc, '_connect', AsyncMock(return_value=db))
    monkeypatch.setattr(api, '_legacy_people_rows', legacy)
    result = await api.list_work_schedules('biz-a', date_from='2026-10-29', date_to='2026-10-29',
        employee_email=None, offset=0, current_user={})
    assert len(result['schedules']) == 1000
    legacy.assert_awaited_once()
    assert db.fetch.await_count == 2


@pytest.mark.asyncio
async def test_direct_member_scope_and_foreign_business(monkeypatch):
    user = {'tenant_id': 'd1695f15-6b68-4929-bc8d-646827363ff9', 'email': 'staff@example.com',
            'current_membership': {'role': 'member', 'status': 'active',
                'tenant_id': 'd1695f15-6b68-4929-bc8d-646827363ff9'}}
    db = AsyncMock()
    db.fetch.return_value = []
    monkeypatch.setattr(api.upload_svc, 'list_businesses', AsyncMock(return_value=[{'id': 'biz-a', 'name': '사업자'}]))
    monkeypatch.setattr(api.upload_svc, '_connect', AsyncMock(return_value=db))
    await api.list_work_schedules('biz-a', date_from='2026-10-29', date_to='2026-10-29',
        employee_email='other@example.com', offset=0, current_user=user)
    assert db.fetch.call_args.args[4] == 'staff@example.com'
    payload = api.WorkScheduleIn(**PAYLOAD)
    for action in [api.create_work_schedule('biz-a', payload, user),
                   api.update_work_schedule('biz-a', 's1', payload, user),
                   api.delete_work_schedule('biz-a', 's1', user)]:
        with pytest.raises(HTTPException) as error:
            await action
        assert error.value.status_code == 403
    monkeypatch.setattr(api.upload_svc, 'list_businesses', AsyncMock(return_value=[]))
    for tenant in [user['tenant_id'], '11111111-1111-1111-1111-111111111111']:
        user['tenant_id'] = tenant
        with pytest.raises(HTTPException) as error:
            await api.list_work_schedules('biz-other', date_from='2026-10-29', date_to='2026-10-29',
                employee_email=None, offset=0, current_user=user)
        assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_legacy_outage_does_not_report_absence(monkeypatch):
    db = AsyncMock()
    db.fetch.side_effect = [[SCHEDULE], []]
    monkeypatch.setattr(api, '_schedule_business', AsyncMock(return_value={'name': '사업자'}))
    monkeypatch.setattr(api, '_require_route_read', lambda *args: None)
    monkeypatch.setattr(api.upload_svc, '_tenant', lambda user: 'tenant')
    monkeypatch.setattr(api.upload_svc, '_connect', AsyncMock(return_value=db))
    monkeypatch.setattr(api, '_legacy_people_rows', AsyncMock(side_effect=RuntimeError('unavailable')))
    with pytest.raises(HTTPException) as error:
        await api.list_work_schedules('biz-a', date_from='2026-10-29', date_to='2026-10-29',
            employee_email=None, offset=0, current_user={})
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_bulk_keep_is_enforced_in_database_not_loaded_roster(client, monkeypatch):
    http, user, db = client
    user['current_membership']['role'] = 'owner'
    original = db.fetchrow

    async def conflict(sql, *args):
        if sql.lstrip().startswith('INSERT') and 'DO NOTHING' in sql:
            return None  # Existing row absent from the browser's loaded week.
        return await original(sql, *args)

    monkeypatch.setattr(db, 'fetchrow', conflict)
    payload = {
        'employees': [{'employee_email': 'staff@example.com'}],
        'date_from': '2026-11-02', 'date_to': '2026-11-02',
        'weekdays': [0], 'kind': 'off', 'preserve_existing': True,
    }
    response = await http.post(BASE + '/bulk', json=payload)
    assert response.status_code == 200
    assert response.json() == {'schedules': [], 'count': 0, 'preserved': 1}
    payload['preserve_existing'] = False
    result = (await http.post(BASE + '/bulk', json=payload)).json()
    assert result['count'] == 1 and result['preserved'] == 0
    assert result['schedules'][0]['work_date'] == '2026-11-02'


@pytest.mark.asyncio
async def test_bulk_empty_weekday_range_is_not_success(client):
    http, user, db = client
    user['current_membership']['role'] = 'owner'
    response = await http.post(BASE + '/bulk', json={
        'employees': [{'employee_email': 'staff@example.com'}],
        'date_from': '2026-11-01', 'date_to': '2026-11-01',
        'weekdays': [0], 'kind': 'off',
    })
    assert response.status_code == 422
    assert not db.calls


@pytest.mark.asyncio
async def test_november_overwrite_is_visible_to_month_week_and_person(client, monkeypatch):
    http, user, db = client
    user['current_membership']['role'] = 'owner'
    db.rows[0].update(work_date='2026-11-02', start_time='09:00', end_time='18:00',
                      break_minutes=60, actual_record={'start_at': '09:30', 'end_at': '18:00'})
    before = dict(db.rows[0])
    original = db.fetchrow

    async def upsert(sql, *args):
        if sql.lstrip().startswith('INSERT'):
            incoming = await original(sql, *args)
            assert incoming['business_id'] == 'biz-a'
            row = db.rows[0]
            assert incoming['employee_email'] == row['employee_email']
            if 'DO NOTHING' in sql:
                return None
            row.update({k: v for k, v in incoming.items() if k != 'id'})
            return dict(row)
        return await original(sql, *args)

    monkeypatch.setattr(db, 'fetchrow', upsert)
    result = await http.post(BASE + '/bulk', json={
        'employees': [{'employee_email': 'staff@example.com'}],
        'date_from': '2026-11-02', 'date_to': '2026-11-02', 'weekdays': [0],
        'start_time': '10:00', 'end_time': '19:00', 'break_minutes': 45,
    })
    assert result.status_code == 200 and result.json()['count'] == 1
    for span in ['from=2026-11-01&to=2026-11-30', 'from=2026-11-02&to=2026-11-08']:
        for suffix in ['', '&employee_email=staff@example.com']:
            for _refresh in range(2):
                response = await http.get(BASE + '?' + span + suffix)
                row = response.json()['schedules'][0]
                assert row['start_time'] == '10:00:00' and row['break_minutes'] == 45
                assert row['judgment']['late_minutes'] == 0
    assert db.rows[0]['id'] == before['id']
    assert db.rows[0]['actual_record'] == before['actual_record']


@pytest.mark.asyncio
@pytest.mark.parametrize("method,suffix", [("post", ""), ("put", "/s1")])
async def test_single_write_never_returns_preserved_null(client, method, suffix):
    http, user, _db = client
    user["current_membership"]["role"] = "owner"
    response = await http.request(method, BASE + suffix, json={
        "employee_email": "staff@example.com", "work_date": "2026-11-02",
        "start_time": "10:00", "end_time": "19:00", "break_minutes": 45,
    })
    assert response.status_code in (200, 201)
    assert response.json()["schedule"]["break_minutes"] == 45


@pytest.mark.asyncio
async def test_save_keep_and_overwrite_postgres_temp_fixture(monkeypatch):
    """Run production INSERT against a temporary table; never write business data."""
    import os

    import asyncpg
    dsn = os.getenv("AADS_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("AADS_TEST_POSTGRES_DSN not configured")
    conn = await asyncpg.connect(dsn, timeout=5)
    transaction = conn.transaction()
    await transaction.start()
    try:
        # Use the migration's actual schema, replacing only the business FK.
        migration = Path("migrations/20261030_obys_work_schedules.sql").read_text()
        ddl = migration[migration.index("CREATE TABLE"):migration.index("CREATE INDEX")]
        ddl = ddl.replace("CREATE TABLE IF NOT EXISTS", "CREATE TEMP TABLE")
        ddl = ddl.replace(" REFERENCES yeoljeong_businesses(id)", "")
        await conn.execute(ddl)
        await conn.execute("SET search_path TO pg_temp")
        monkeypatch.setattr(api, "_attendance_audit", AsyncMock())
        user = {"email": "fixture@example.test"}
        first = api.WorkScheduleIn(employee_email="fixture@example.test", work_date="2026-11-02",
            start_time="09:00", end_time="18:00", break_minutes=60)
        updated = api.WorkScheduleIn(**{**first.model_dump(), "start_time": "10:00", "break_minutes": 45})
        saved = await api._save_work_schedule(conn, "fixture-business", first, user)
        assert await api._save_work_schedule(conn, "fixture-business", updated, user,
            preserve_existing=True) is None
        row = await conn.fetchrow("SELECT * FROM yeoljeong_work_schedules")
        assert row["break_minutes"] == 60
        result = await api._save_work_schedule(conn, "fixture-business", updated, user)
        assert result["id"] == saved["id"] and result["break_minutes"] == 45
        assert await conn.fetchval("SELECT count(*) FROM yeoljeong_work_schedules") == 1
        assert api._attendance_audit.await_count == 2
        # Execute the production replacement SQL against the same isolated schema.
        tuesday = first.model_copy(update={"work_date": date(2026, 11, 3)})
        exception = first.model_copy(update={"work_date": date(2026, 11, 4), "memo": "manual exception"})
        await api._save_work_schedule(conn, "fixture-business", tuesday, user)
        await api._save_work_schedule(conn, "fixture-business", exception, user)
        bulk = api.WorkScheduleBulkIn(employees=[{"employee_email": first.employee_email}],
            date_from="2026-11-01", date_to="2026-11-30", weekdays=[0],
            start_time="21:00", end_time="09:00")
        await api._retire_excluded_schedule_days(conn, "fixture-business", bulk, user)
        await api._retire_excluded_schedule_days(conn, "fixture-business", bulk, user)
        assert await conn.fetchval("SELECT count(*) FROM yeoljeong_work_schedules WHERE deleted_at IS NULL") == 2
        assert await conn.fetchval("SELECT memo FROM yeoljeong_work_schedules WHERE work_date=$1 AND deleted_at IS NULL",
                                   date(2026, 11, 4)) == "manual exception"
        assert await conn.fetchval("SELECT deleted_at IS NOT NULL FROM yeoljeong_work_schedules WHERE work_date=$1",
                                   date(2026, 11, 3))
    finally:
        await transaction.rollback()
        await conn.close()


@pytest.mark.parametrize('actual_break,expected_overtime', [(90, 0), (0, 90)])
@pytest.mark.parametrize('timestamps', [False, True])
def test_overnight_630_minutes_and_explained_break_difference(actual_break, expected_overtime, timestamps):
    schedule = {**SCHEDULE, 'id': 'night', 'start_time': '21:00', 'end_time': '09:00',
                'break_minutes': 90}
    record = {'start_at': '21:00', 'end_at': '09:00', 'break_minutes': actual_break}
    if timestamps:
        record.update(check_in_at='2026-10-29T12:00:00Z', check_out_at='2026-10-30T00:00:00Z')
    result = api.attendance_schedule_judgment(schedule, record, now=NOW)
    assert result['overtime_minutes'] == expected_overtime
    basis = result['basis']
    assert basis['planned_worked_minutes'] == 630
    assert basis['actual_worked_minutes'] == 720 - actual_break
    assert basis['planned_break_minutes'] == 90
    assert basis['actual_break_minutes'] == actual_break
    assert basis['overnight'] and basis['source'] == 'work_schedule'
    assert basis['schedule_id'] == 'night'
    assert record['break_minutes'] == actual_break  # Evidence is never rewritten.


def test_split_shift_explanation_uses_sum_of_actual_breaks():
    records = [{'start_at': '09:00', 'end_at': '12:00', 'break_minutes': 10},
               {'start_at': '13:00', 'end_at': '18:00', 'break_minutes': 20}]
    result = api.attendance_schedule_judgment(SCHEDULE, records, now=NOW)
    assert result['basis']['actual_worked_minutes'] == 450
    assert result['basis']['actual_break_minutes'] == 30
    assert result['basis']['actual_source'] == 'daily_total'


def test_corrected_night_clocks_override_stale_pwa_evidence():
    schedule = {**SCHEDULE, 'start_time': '21:00', 'end_time': '09:00', 'break_minutes': 90}
    record = {'start_at': '21:00', 'end_at': '09:00', 'break_minutes': 90,
              'check_in_at': '2026-10-29T11:00:00Z',
              'check_out_at': '2026-10-30T01:00:00Z'}
    original = dict(record)
    uncorrected = {**record, 'start_at': '20:00', 'end_at': '10:00'}
    assert api.attendance_schedule_judgment(schedule, uncorrected, now=NOW)['overtime_minutes'] == 120
    result = api.attendance_schedule_judgment(schedule, record, now=NOW)
    assert result['overtime_minutes'] == 0
    assert result['basis']['actual_worked_minutes'] == 630
    assert result['basis']['clock_basis'] == 'corrected_wall_clock'
    assert record == original


@pytest.mark.parametrize('seconds', ['30', '30.123456'])
def test_pwa_seconds_do_not_remove_night_record_date(seconds):
    schedule = {**SCHEDULE, 'start_time': '21:00', 'end_time': '09:00', 'break_minutes': 90}
    record = {'start_at': '08:00', 'end_at': '09:00', 'break_minutes': 0,
              'check_in_at': f'2026-10-29T08:00:{seconds}+09:00',
              'check_out_at': f'2026-10-29T09:00:{seconds}+09:00'}
    original = dict(record)
    result = api.attendance_schedule_judgment(schedule, record, now=NOW)
    assert (result['late_minutes'], result['early_minutes'], result['overtime_minutes']) == (0, 1440, 0)
    assert result['basis']['clock_basis'] == 'record_clock'
    assert record == original


@pytest.mark.parametrize('raw_seconds', ['00', '30'])
@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('timestamp_style', ['kst', 'utc', 'mixed'])
def test_corrected_and_original_rows_keep_first_in_last_out(reverse, timestamp_style, raw_seconds):
    records = [
        {'start_at': '09:00', 'end_at': '12:00', 'break_minutes': 0,
         'check_in_at': f'2026-10-29T09:00:{raw_seconds}+09:00',
         'check_out_at': f'2026-10-29T12:00:{raw_seconds}+09:00'},
        {'start_at': '13:00', 'end_at': '18:00', 'break_minutes': 0,
         'check_in_at': '2026-10-29T13:30:30+09:00',
         'check_out_at': '2026-10-29T18:00:30+09:00'},
    ]
    if timestamp_style != 'kst':
        for row in records[:1] if timestamp_style == 'mixed' else records:
            for field in ('check_in_at', 'check_out_at'):
                row[field] = datetime.fromisoformat(row[field]).astimezone(UTC).isoformat()
    if reverse:
        records.reverse()
    originals = [dict(r) for r in records]
    result = api.attendance_schedule_judgment(SCHEDULE, records, now=NOW)
    assert result['labels'] == ['정상']
    assert (result['late_minutes'], result['early_minutes'], result['overtime_minutes']) == (0, 0, 0)
    assert result['basis']['actual_start_minutes'] == 540
    assert result['basis']['actual_end_minutes'] == 1080
    assert result['basis']['actual_worked_minutes'] == 480
    assert records == originals


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('day', ['2026-10-29', '2026-10-31'])
def test_boundary_correction_mixed_with_original_row_uses_effective_order(day, reverse):
    rows = [
        dict(work_date=day, start_at='09:00', end_at='12:00', break_minutes=0,
             check_in_at='2026-10-29T21:00:30+09:00',
             check_out_at='2026-10-30T09:00:30+09:00'),
        dict(work_date=day, start_at='13:00', end_at='18:00', break_minutes=0,
             check_in_at=day+'T13:00:30+09:00', check_out_at=day+'T18:00:30+09:00'),
    ]
    if reverse:
        rows.reverse()
    original = [dict(row) for row in rows]
    result = api.attendance_schedule_judgment({**SCHEDULE, 'work_date': day}, rows, now=NOW)
    assert result['labels'] == ['정상']
    assert result['basis']['actual_worked_minutes'] == 480
    assert result['basis']['actual_start_minutes'] == 540
    assert result['basis']['actual_end_minutes'] == 1080
    assert rows == original
