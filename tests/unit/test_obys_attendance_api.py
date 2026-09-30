"""근태 CRUD API — 서버 저장·서버 계산·테넌트 스코프·soft delete.

운영 스키마(2026-09-30 진아서버 조회)를 흉내 낸 메모리 DB 로 라우터를 HTTP 로 부른다.
UNIQUE (employee_email, work_date, start_at) 와 deleted_at soft delete 를 그대로 재현한다.
"""
from __future__ import annotations

import itertools
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timezone
from pathlib import Path

import asyncpg
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import obys_workspaces as api
from app.auth import get_current_user


USER = {"tenant_id": "d1695f15-6b68-4929-bc8d-646827363ff9", "email": "owner@example.com"}
BUSINESS = {"id": "biz-lylon-e2e", "name": "주식회사 라일론"}
OTHER_BUSINESS_ID = "biz-other-tenant"
BASE = f"/api/v1/workspaces/{BUSINESS['id']}/attendance/records"
COLUMNS = api.ATTENDANCE_COLUMNS.split(",")


class FakeAttendanceDB:
    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.audit: list[tuple] = []
        self._ids = itertools.count(1)

    def _project(self, row):
        return {key: row[key] for key in COLUMNS}

    def _conflict(self, email, work_date, start_at, exclude_id=None):
        return next((row for row in self.rows.values()
                     if row["id"] != exclude_id and row["employee_email"] == email
                     and row["work_date"] == work_date and row["start_at"] == start_at), None)

    def seed(self, **values):
        row_id = f"att-seed{next(self._ids)}"
        now = datetime.now(timezone.utc)
        self.rows[row_id] = {"id": row_id, "hourly_wage": 0, "memo": "", "created_at": now,
                             "updated_at": now, "deleted_at": None, **values}
        return row_id


class FakeConnection:
    def __init__(self, db: FakeAttendanceDB):
        self.db = db
        self.closed = False

    @asynccontextmanager
    async def transaction(self):
        yield

    async def close(self):
        self.closed = True

    async def execute(self, sql, *args):
        assert "INSERT INTO yeoljeong_audit_logs" in sql
        self.db.audit.append(args)
        return "INSERT 0 1"

    async def fetch(self, sql, business_id):
        assert "FROM yeoljeong_attendance_records" in sql and "deleted_at IS NULL" in sql
        rows = [row for row in self.db.rows.values()
                if row["business_id"] == business_id and row["deleted_at"] is None]
        return [self.db._project(row) for row in sorted(rows, key=lambda r: r["work_date"], reverse=True)]

    async def fetchrow(self, sql, *args):
        db = self.db
        now = datetime.now(timezone.utc)
        if sql.lstrip().startswith("INSERT INTO yeoljeong_attendance_records"):
            (business_id, email, masked, name, branch, work_date, start_at, end_at,
             break_minutes, worked, wage, status, memo, actor) = args
            values = {"business_id": business_id, "employee_email": email,
                      "employee_email_masked": masked, "employee_name": name, "branch": branch,
                      "work_date": work_date, "start_at": start_at, "end_at": end_at,
                      "break_minutes": break_minutes, "worked_minutes": worked,
                      "hourly_wage": wage, "status": status, "memo": memo, "created_by": actor,
                      "created_at": now, "updated_at": now, "deleted_at": None}
            existing = db._conflict(email, work_date, start_at)
            if existing:
                if existing["deleted_at"] is None or existing["business_id"] != business_id:
                    return None  # ON CONFLICT ... WHERE 불충족 → RETURNING 0행
                existing.update(values)
                return db._project(existing)
            row_id = f"att-{next(db._ids):012d}"
            db.rows[row_id] = {"id": row_id, **values}
            return db._project(db.rows[row_id])
        if sql.lstrip().startswith("SELECT"):
            row = db.rows.get(args[0])
            if row and row["business_id"] == args[1] and row["deleted_at"] is None:
                return db._project(row)
            return None
        if sql.lstrip().startswith("UPDATE yeoljeong_attendance_records SET"):
            (record_id, business_id, email, masked, name, branch, work_date, start_at, end_at,
             break_minutes, worked, wage, status, memo) = args
            row = db.rows.get(record_id)
            if not row or row["business_id"] != business_id or row["deleted_at"] is not None:
                return None
            if db._conflict(email, work_date, start_at, exclude_id=record_id):
                raise asyncpg.UniqueViolationError("duplicate key")
            row.update({"employee_email": email, "employee_email_masked": masked,
                        "employee_name": name, "branch": branch, "work_date": work_date,
                        "start_at": start_at, "end_at": end_at, "break_minutes": break_minutes,
                        "worked_minutes": worked, "hourly_wage": wage, "status": status,
                        "memo": memo, "updated_at": now})
            return db._project(row)
        raise AssertionError(f"unexpected SQL: {sql}")

    async def fetchval(self, sql, record_id, business_id):
        assert "SET deleted_at=now()" in sql and "DELETE" not in sql.upper().split("SET")[0]
        row = self.db.rows.get(record_id)
        if not row or row["business_id"] != business_id or row["deleted_at"] is not None:
            return None
        row["deleted_at"] = datetime.now(timezone.utc)
        return record_id


@pytest.fixture
def db():
    return FakeAttendanceDB()


@pytest.fixture
def client(monkeypatch, db):
    async def businesses(*, user):
        assert user is USER
        return [BUSINESS]  # 현재 테넌트 소유 사업자만

    async def connect():
        return FakeConnection(db)

    monkeypatch.setattr(api.upload_svc, "list_businesses", businesses)
    monkeypatch.setattr(api.upload_svc, "_connect", connect)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: USER
    return TestClient(app)


def _payload(**overrides):
    body = {"work_date": "2026-09-29", "employee_name": "김민수", "branch": "중화점",
            "start_at": "10:00", "end_at": "18:00", "break_minutes": 60,
            "hourly_wage": 12000, "status": "pending", "memo": "정상 근무"}
    body.update(overrides)
    return body


def _list(client):
    response = client.get(f"/api/v1/workspaces/{BUSINESS['id']}/attendance/records")
    assert response.status_code == 200
    return response.json()["records"]


def test_create_update_soft_delete_and_list_round_trip(client, db):
    created = client.post(BASE, json=_payload())
    assert created.status_code == 201, created.text
    record = created.json()["record"]
    record_id = record["id"]
    assert record["raw"]["worked_minutes"] == 420
    assert record["raw"]["employee_name"] == "김민수"
    assert record["date"] == "2026-09-29"  # 일자는 created_at 이 아니라 근무일

    listed = _list(client)
    assert [item["id"] for item in listed] == [record_id]
    assert listed[0]["raw"]["memo"] == "정상 근무"
    assert listed[0]["raw"]["hourly_wage"] == 12000

    detail = client.get(f"{BASE}/{record_id}")
    assert detail.status_code == 200
    assert detail.json()["record"]["raw"]["start_at"] == "10:00:00"

    updated = client.put(f"{BASE}/{record_id}", json=_payload(end_at="20:00", status="approved"))
    assert updated.status_code == 200, updated.text
    assert updated.json()["record"]["raw"]["worked_minutes"] == 540
    assert updated.json()["record"]["raw"]["status"] == "approved"

    deleted = client.delete(f"{BASE}/{record_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True, "id": record_id}
    # 물리 삭제가 아니다 — 행은 남고 deleted_at 만 찍힌다.
    assert record_id in db.rows and db.rows[record_id]["deleted_at"] is not None
    assert _list(client) == []
    assert client.get(f"{BASE}/{record_id}").status_code == 404
    assert client.delete(f"{BASE}/{record_id}").status_code == 404
    assert [entry[2] for entry in db.audit] == [
        "attendance.create", "attendance.update", "attendance.delete",
    ]
    assert all(entry[1] == "owner@example.com" for entry in db.audit)


def test_soft_deleted_row_is_hidden_and_same_shift_can_be_recreated(client, db):
    first = client.post(BASE, json=_payload()).json()["record"]["id"]
    # 살아 있는 같은 키는 중복으로 거절한다.
    assert client.post(BASE, json=_payload()).status_code == 409
    assert client.delete(f"{BASE}/{first}").status_code == 200
    again = client.post(BASE, json=_payload(end_at="19:00"))
    assert again.status_code == 201
    listed = _list(client)
    assert len(listed) == 1 and listed[0]["raw"]["worked_minutes"] == 480


@pytest.mark.parametrize(
    ("start_at", "end_at", "break_minutes", "expected"),
    [
        ("10:00", "18:00", 60, 420),   # 일반 근무
        ("22:00", "06:00", 30, 450),   # 자정 넘김 + 휴게 차감
        ("18:00", "23:30", 0, 330),
        ("00:01", "00:00", 0, 1439),   # HH:MM 로 표현 가능한 최장 근무
    ],
)
def test_worked_minutes_is_computed_on_server(client, start_at, end_at, break_minutes, expected):
    response = client.post(BASE, json=_payload(start_at=start_at, end_at=end_at,
                                               break_minutes=break_minutes))
    assert response.status_code == 201, response.text
    assert response.json()["record"]["raw"]["worked_minutes"] == expected


def test_client_supplied_worked_minutes_is_ignored(client, db):
    response = client.post(BASE, json=_payload(worked_minutes=9999))
    assert response.status_code == 201
    record_id = response.json()["record"]["id"]
    assert db.rows[record_id]["worked_minutes"] == 420
    updated = client.put(f"{BASE}/{record_id}", json=_payload(worked_minutes=1))
    assert db.rows[record_id]["worked_minutes"] == 420
    assert updated.json()["record"]["raw"]["worked_minutes"] == 420


@pytest.mark.parametrize(
    "overrides",
    [
        {"start_at": "10:00", "end_at": "11:00", "break_minutes": 90},  # 음수 근무
        {"start_at": "10:00", "end_at": "10:00"},                        # 0분
        {"break_minutes": -10},
        {"work_date": "2026-9-29"},
        {"work_date": "2026-02-30"},
        {"work_date": "29/09/2026"},
        {"start_at": "9:00"},
        {"end_at": "24:00"},
        {"start_at": "10:00:00"},
        {"status": "confirmed"},
        {"hourly_wage": -1},
        {"employee_name": "   "},
        {"employee_email": "not-an-email"},
    ],
)
def test_invalid_input_is_rejected_with_400(client, db, overrides):
    response = client.post(BASE, json=_payload(**overrides))
    assert response.status_code == 400, response.text
    assert db.rows == {} and db.audit == []


def test_invalid_update_is_rejected_with_400_and_row_unchanged(client, db):
    record_id = client.post(BASE, json=_payload()).json()["record"]["id"]
    response = client.put(f"{BASE}/{record_id}", json=_payload(break_minutes=600))
    assert response.status_code == 400
    assert db.rows[record_id]["worked_minutes"] == 420


def test_more_than_24_hours_is_rejected():
    with pytest.raises(HTTPException) as err:
        api.attendance_worked_minutes(time(0, 1), time(0, 0), -120)
    assert err.value.status_code == 400


def test_other_tenant_business_cannot_read_or_write(client, db):
    foreign = f"/api/v1/workspaces/{OTHER_BUSINESS_ID}/attendance/records"
    assert client.post(foreign, json=_payload()).status_code == 404
    assert db.rows == {}

    # 다른 사업자의 행을 내 사업자 경로로 건드려도 보이지 않는다.
    foreign_row = db.seed(business_id=OTHER_BUSINESS_ID, employee_email="x@example.com",
                          employee_email_masked="", employee_name="남의 직원", branch="",
                          work_date=date(2026, 9, 29), start_at=time(9), end_at=time(18),
                          break_minutes=0, worked_minutes=540, status="pending", created_by="")
    assert client.get(f"{BASE}/{foreign_row}").status_code == 404
    assert client.put(f"{BASE}/{foreign_row}", json=_payload()).status_code == 404
    assert client.delete(f"{BASE}/{foreign_row}").status_code == 404
    assert db.rows[foreign_row]["deleted_at"] is None
    assert db.rows[foreign_row]["employee_name"] == "남의 직원"
    assert client.get(f"{foreign}/{foreign_row}").status_code == 404
    assert client.delete(f"{foreign}/{foreign_row}").status_code == 404
    assert _list(client) == []


def test_email_less_staff_are_keyed_per_business():
    values = api._attendance_values(BUSINESS["id"], api.AttendanceRecordIn(**_payload()))
    assert values["employee_email"] == f"name:{BUSINESS['id']}:김민수"
    assert values["employee_email_masked"] == ""
    with_email = api._attendance_values(
        BUSINESS["id"], api.AttendanceRecordIn(**_payload(employee_email=" Kim@Example.com ")))
    assert with_email["employee_email"] == "kim@example.com"
    assert with_email["employee_email_masked"] == "k***@example.com"


def test_attendance_write_routes_are_matched_before_generic_record_routes():
    paths = [(route.path, sorted(route.methods)) for route in api.router.routes]
    specific = paths.index(("/workspaces/{business_id}/attendance/records", ["POST"]))
    generic_post = paths.index(("/workspaces/{business_id}/{route}/records", ["POST"]))
    specific_get = paths.index(("/workspaces/{business_id}/attendance/records/{record_id}", ["GET"]))
    generic_get = paths.index(("/workspaces/{business_id}/{route}/records/{record_id}", ["GET"]))
    assert specific < generic_post and specific_get < generic_get


# ── 화면: 저장 실패를 성공처럼 보이지 않는다 ─────────────────────────────────
INDEX = Path(__file__).resolve().parents[2] / "app" / "static" / "apps" / "obys" / "index.html"


def _shift_submit_handler() -> str:
    html = INDEX.read_text(encoding="utf-8")
    start = html.index('els.shiftForm.addEventListener("submit"')
    end = html.index("els.uploadLocalShiftsBtn?.addEventListener", start)
    return html[start:end]


def test_shift_form_saves_to_server_and_reports_failure_without_clearing_form():
    handler = _shift_submit_handler()
    assert "async event" in handler
    assert "state.shifts.push" not in handler  # localStorage 가 정본이 아니다
    save = handler.index("await saveShiftToServer(shift)")
    catch = handler.index("catch (error)", save)
    failure_toast = handler.index("근태 저장 실패", catch)
    bail_out = handler.index("return;", failure_toast)
    reset = handler.index("els.shiftForm.reset()")
    success_toast = handler.index('showToast("근태를 서버에 저장했습니다.")')
    # 실패 → 실패 문구 → 폼을 비우기 전에 반환. 성공 문구는 서버 저장·재조회 뒤에만.
    assert save < catch < failure_toast < bail_out < reset < success_toast
    assert handler.index("await loadAttendanceRecords()") < success_toast
    assert '"근태를 저장했습니다."' not in handler
    # 로그인 없으면 저장하지 않고 실패로 표시한다.
    assert handler.index("if (!hasServerAuth())") < save


def test_shift_screen_posts_to_attendance_api_and_marks_local_only_rows():
    html = INDEX.read_text(encoding="utf-8")
    assert "fetch(`/api/v1/workspaces${path}`" in html
    assert re.search(r"workspaceApi\(`/\$\{encodeURIComponent\(businessId\)\}/attendance/records`, \{\s*method: \"POST\"", html)
    assert 'method: "DELETE"' in html and "/attendance/records/${encodeURIComponent(serverShiftId)}" in html
    # 기존 로컬 근태는 버리지 않고 미저장으로 표시하고 서버로 올릴 수 있다.
    assert "미저장(로컬 전용)" in html
    assert 'id="uploadLocalShiftsBtn"' in html and "data-upload-shift=" in html
    # 서버 저장이 성공한 로컬 근태만 로컬에서 지운다.
    upload = html[html.index("async function uploadLocalShifts"):]
    upload = upload[:upload.index("\n    }\n")]
    assert upload.index("await saveShiftToServer(shift)") < upload.index("state.shifts = state.shifts.filter")
    # 화면 표시 상태는 서버 허용값으로 바꿔 보낸다.
    assert 'confirmed: "approved"' in html
