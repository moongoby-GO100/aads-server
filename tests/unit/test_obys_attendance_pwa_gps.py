"""PWA 출퇴근 — 반경 판정·본인 한정·동의·멱등·PWA 자산 정적 검사.

메모리 DB 로 라우터를 HTTP 로 부른다. 운영 스키마의 UNIQUE(employee_email, work_date, start_at)
와 soft delete 를 흉내 낸다. SQL 은 키워드로만 분기한다 — 테스트에 SQL 전문을 적지 않는다
(scripts/dup_guard.py 가 테스트 안의 SQL 리터럴을 실제 SQL 로 본다).
"""
from __future__ import annotations

import itertools
import json
import math
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import obys_workspaces as api
from app.auth import get_current_user


ROOT = Path(__file__).resolve().parents[2]
KST = ZoneInfo("Asia/Seoul")
TENANT = "d1695f15-6b68-4929-bc8d-646827363ff9"
OTHER_TENANT = "0b0b0b0b-1111-4222-8333-444444444444"
BUSINESS = {"id": "biz-lylon-e2e", "name": "주식회사 라일론"}
STORE = (37.5665, 126.9780)
M_PER_DEG_LAT = api._EARTH_RADIUS_M * math.pi / 180


def _member(email, role="member", tenant=TENANT, name="김직원"):
    return {"tenant_id": tenant, "email": email, "name": name,
            "current_membership": {"tenant_id": tenant, "status": "active", "role": role}}


EMPLOYEE = _member("Staff@Example.com")
COWORKER = _member("coworker@example.com", name="이동료")
OWNER = _member("owner@example.com", role="owner", name="대표")
VIEWER = _member("viewer@example.com", role="viewer")
FOREIGN = _member("intruder@example.com", tenant=OTHER_TENANT)

BASE = f"/api/v1/workspaces/{BUSINESS['id']}"


def _north(meters: float) -> tuple[float, float]:
    # 서버는 거리를 올림한다. 0.5m 덜 가서 올림 결과가 정확히 meters 가 되게 한다.
    return STORE[0] + (meters - 0.5) / M_PER_DEG_LAT, STORE[1]


def _columns(sql: str) -> list[str]:
    for const in (api.ATTENDANCE_LOCATION_COLUMNS, api.ATTENDANCE_CLOCK_COLUMNS):
        if const in sql:
            cols = const.split(",")
            return (["employee_email"] + cols) if "SELECT employee_email," in sql else cols
    raise AssertionError("unknown projection")


class FakeDB:
    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.branches: dict[str, dict] = {}
        self.audit: list[dict] = []
        self.locks: list[str] = []
        self._ids = itertools.count(1)

    def add_branch(self, branch_id, name, lat=None, lng=None, radius=None, business_id=BUSINESS["id"]):
        self.branches[branch_id] = {"id": branch_id, "business_id": business_id, "name": name,
                                    "latitude": lat, "longitude": lng, "geofence_radius_m": radius,
                                    "deleted_at": None, "updated_by": ""}


class FakeConnection:
    def __init__(self, db: FakeDB):
        self.db = db

    @asynccontextmanager
    async def transaction(self):
        yield

    async def close(self):
        pass

    async def execute(self, sql, *args):
        if "pg_advisory_xact_lock" in sql:
            self.db.locks.append(args[0])
            return "SELECT 1"
        assert "INSERT INTO yeoljeong_audit_logs" in sql
        business_id, actor, action, record_id, details = args
        self.db.audit.append({"business_id": business_id, "actor": actor, "action": action,
                              "resource_id": record_id, "details": details,
                              "created_at": datetime.now(KST) + timedelta(microseconds=len(self.db.audit))})
        return "INSERT 0 1"

    def _branches(self, business_id):
        return [dict(b) for b in self.db.branches.values()
                if b["business_id"] == business_id and b["deleted_at"] is None]

    async def fetch(self, sql, *args):
        if "FROM yeoljeong_branches" in sql:
            return self._branches(args[0])
        assert "FROM yeoljeong_attendance_records" in sql
        cols = _columns(sql)
        rows = [r for r in self.db.rows.values() if r["business_id"] == args[0] and r["deleted_at"] is None]
        if "employee_email=$2" in sql:
            rows = [r for r in rows if r["employee_email"] == args[1]]
        if "source='pwa'" in sql:
            rows = [r for r in rows if r["source"] == "pwa"]
        return [{c: r.get(c) for c in cols} for r in sorted(rows, key=lambda r: r["work_date"], reverse=True)]

    async def fetchval(self, sql, business_id, email, work_date):
        assert "check_out_at IS NULL" in sql
        return next((r["id"] for r in self.db.rows.values()
                     if r["business_id"] == business_id and r["employee_email"] == email
                     and r["work_date"] == work_date and r["source"] == "pwa"
                     and r["check_in_at"] is not None and r["check_out_at"] is None
                     and r["deleted_at"] is None), None)

    async def fetchrow(self, sql, *args):
        db = self.db
        head = sql.lstrip()
        if "FROM yeoljeong_audit_logs" in sql:
            business_id, actor, actions = args
            hits = [a for a in db.audit if a["business_id"] == business_id and a["actor"] == actor
                    and a["action"] in actions]
            return {"action": hits[-1]["action"], "created_at": hits[-1]["created_at"]} if hits else None
        if head.startswith("INSERT INTO yeoljeong_attendance_records"):
            (business_id, email, masked, name, branch, work_date, start_at, status, memo, actor,
             check_in_at, lat, lng, accuracy, distance, result, device, consent_at) = args
            if any(r["employee_email"] == email and r["work_date"] == work_date and r["start_at"] == start_at
                   for r in db.rows.values()):
                raise asyncpg.UniqueViolationError("duplicate key")
            row_id = f"att-{next(db._ids):06d}"
            now = datetime.now(KST)
            db.rows[row_id] = {
                "id": row_id, "business_id": business_id, "employee_email": email,
                "employee_email_masked": masked, "employee_name": name, "branch": branch,
                "work_date": work_date, "start_at": start_at, "end_at": start_at, "break_minutes": 0,
                "worked_minutes": 0, "hourly_wage": 0, "source": "pwa", "status": status, "memo": memo,
                "created_by": actor, "created_at": now, "updated_at": now, "deleted_at": None,
                "check_in_at": check_in_at, "check_out_at": None, "check_in_lat": lat, "check_in_lng": lng,
                "check_in_accuracy_m": accuracy, "check_in_distance_m": distance, "check_out_lat": None,
                "check_out_lng": None, "check_out_accuracy_m": None, "check_out_distance_m": None,
                "geofence_result": result, "device_info": device, "location_consent_at": consent_at,
            }
            return {c: db.rows[row_id][c] for c in _columns(sql)}
        if "FOR UPDATE" in sql:
            business_id, email, since = args
            open_rows = [r for r in db.rows.values()
                         if r["business_id"] == business_id and r["employee_email"] == email
                         and r["source"] == "pwa" and r["check_in_at"] is not None
                         and r["check_in_at"] >= since and r["check_out_at"] is None and r["deleted_at"] is None]
            if not open_rows:
                return None
            row = max(open_rows, key=lambda r: r["check_in_at"])
            return {c: row[c] for c in _columns(sql)}
        if head.startswith("UPDATE yeoljeong_attendance_records"):
            (record_id, business_id, check_out_at, end_at, worked, lat, lng, accuracy, distance,
             result, status, memo, device, consent_at) = args
            row = db.rows.get(record_id)
            if not row or row["business_id"] != business_id or row["check_out_at"] is not None:
                return None
            row.update({"check_out_at": check_out_at, "end_at": end_at, "worked_minutes": worked,
                        "check_out_lat": lat, "check_out_lng": lng, "check_out_accuracy_m": accuracy,
                        "check_out_distance_m": distance, "geofence_result": result, "status": status,
                        "memo": memo, "device_info": row["device_info"] or device,
                        "location_consent_at": row["location_consent_at"] or consent_at})
            return {c: row[c] for c in _columns(sql)}
        if head.startswith("SELECT employee_email,"):
            row = db.rows.get(args[0])
            if row and row["business_id"] == args[1] and row["deleted_at"] is None:
                return {c: row[c] for c in _columns(sql)}
            return None
        if head.startswith("UPDATE yeoljeong_branches"):
            branch_id, business_id, lat, lng, radius, actor = args
            branch = db.branches.get(branch_id)
            if not branch or branch["business_id"] != business_id or branch["deleted_at"] is not None:
                return None
            branch.update({"latitude": lat, "longitude": lng, "geofence_radius_m": radius, "updated_by": actor})
            return {k: branch[k] for k in ("id", "name", "latitude", "longitude", "geofence_radius_m")}
        raise AssertionError(f"unexpected SQL: {sql[:80]}")


@pytest.fixture
def db():
    fake = FakeDB()
    fake.add_branch("br-junghwa", "중화점", *STORE)
    return fake


@pytest.fixture
def clock(monkeypatch):
    now = {"value": datetime(2026, 9, 30, 10, 0, tzinfo=KST)}
    monkeypatch.setattr(api, "_clock_now", lambda: now["value"])
    return now


@pytest.fixture
def as_user():
    return {"user": EMPLOYEE}


@pytest.fixture
def client(monkeypatch, db, clock, as_user):
    async def businesses(*, user):
        return [BUSINESS] if user["tenant_id"] == TENANT else []

    async def connect():
        return FakeConnection(db)

    monkeypatch.setattr(api.upload_svc, "list_businesses", businesses)
    monkeypatch.setattr(api.upload_svc, "_connect", connect)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: as_user["user"]
    return TestClient(app)


def _consent(client, agree=True):
    response = client.post(f"{BASE}/attendance/consent", json={"agree": agree})
    assert response.status_code == 200, response.text


def _punch(client, action, meters=None, accuracy=10, consent=True, **extra):
    body = {"location_consent": consent, "device_info": "test-agent", **extra}
    if meters is not None:
        body["latitude"], body["longitude"] = _north(meters)
        body["accuracy_m"] = accuracy
    return client.post(f"{BASE}/attendance/{action}", json=body)


def _stored(db, response):
    return db.rows[response.json()["record"]["id"]]


# ── 반경 판정 ────────────────────────────────────────────────────────────────

def test_inside_radius_is_auto_approved_as_pwa(client, db):
    _consent(client)
    response = _punch(client, "check-in", meters=20)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["record"]["status"] == "approved"
    assert body["record"]["geofence_result"] == "inside"
    assert body["record"]["source"] == "pwa"
    assert body["judgement"]["distance_m"] == 20
    row = _stored(db, response)
    assert row["check_in_lat"] == pytest.approx(_north(20)[0]) and row["check_in_distance_m"] == 20
    assert row["location_consent_at"] is not None and row["check_in_at"] == datetime(2026, 9, 30, 10, 0, tzinfo=KST)
    # 직원 응답에는 좌표 원본·기기 정보·이메일 원문이 없다.
    assert not {"check_in_lat", "check_in_lng", "device_info", "employee_email"} & set(body["record"])
    # 감사로그는 기존 _attendance_audit 경로 — 좌표 원문을 남기지 않는다.
    entry = db.audit[-1]
    assert entry["action"] == "attendance.check_in" and entry["resource_id"] == row["id"]
    assert str(row["check_in_lat"])[:7] not in entry["details"] and "latitude" not in entry["details"]


def test_outside_radius_is_pending_with_distance(client, db):
    _consent(client)
    response = _punch(client, "check-in", meters=80)
    assert response.status_code == 201
    record = response.json()["record"]
    assert (record["status"], record["geofence_result"]) == ("pending", "outside")
    assert record["check_in_distance_m"] == 80
    assert "80m" in record["memo"] and "밖" in record["memo"]


def test_accuracy_worse_than_radius_is_unknown_not_inside(client, db):
    _consent(client)
    # 매장에서 5m 로 찍혔더라도 오차가 반경(50m)보다 크면 안으로 판정하지 않는다.
    response = _punch(client, "check-in", meters=5, accuracy=120)
    record = response.json()["record"]
    assert (record["status"], record["geofence_result"]) == ("pending", "unknown")
    assert "오차 120m" in record["memo"]


def test_branch_without_coordinates_is_unknown_with_reason(client, db):
    db.branches["br-junghwa"].update(latitude=None, longitude=None)
    _consent(client)
    response = _punch(client, "check-in", meters=0)
    record = response.json()["record"]
    assert (record["status"], record["geofence_result"]) == ("pending", "unknown")
    assert "지점 좌표 미등록" in record["memo"]
    assert db.audit[-1]["action"] == "attendance.check_in" and "지점 좌표 미등록" in db.audit[-1]["details"]


def test_business_without_any_branch_still_checks_in_as_unknown(client, db):
    db.branches.clear()
    _consent(client)
    response = _punch(client, "check-in", meters=0)
    assert response.status_code == 201
    record = response.json()["record"]
    assert (record["status"], record["geofence_result"], record["branch"]) == ("pending", "unknown", "")
    assert "지점 좌표 미등록" in record["memo"]


def test_without_consent_location_is_not_stored_and_pending(client, db):
    # 서버 동의 없음 + 요청은 동의했다고 보내도 위치를 저장하지 않는다.
    response = _punch(client, "check-in", meters=10, consent=True)
    row = _stored(db, response)
    assert row["check_in_lat"] is None and row["check_in_lng"] is None
    assert row["check_in_accuracy_m"] is None and row["check_in_distance_m"] is None
    assert row["location_consent_at"] is None
    assert (row["status"], row["geofence_result"]) == ("pending", "unknown")
    assert "미동의" in row["memo"]


def test_withdrawn_consent_stops_location_collection(client, db, clock):
    _consent(client)
    assert _punch(client, "check-in", meters=10).json()["record"]["status"] == "approved"
    _consent(client, agree=False)
    clock["value"] = datetime(2026, 9, 30, 18, 0, tzinfo=KST)
    out = _punch(client, "check-out", meters=10)
    assert out.status_code == 200, out.text
    row = _stored(db, out)
    assert row["check_out_lat"] is None and row["check_out_distance_m"] is None
    assert row["status"] == "pending" and row["geofence_result"] == "unknown"
    assert [a["action"] for a in db.audit if "consent" in a["action"]] == [
        "attendance.consent", "attendance.consent_withdraw"]


def test_request_consent_false_overrides_server_consent(client, db):
    _consent(client)
    row = _stored(db, _punch(client, "check-in", meters=10, consent=False))
    assert row["check_in_lat"] is None and row["status"] == "pending"


@pytest.mark.parametrize(("meters", "expected"), [(49, "inside"), (50, "inside"), (51, "outside")])
def test_radius_boundary(meters, expected):
    branch = {"latitude": STORE[0], "longitude": STORE[1], "geofence_radius_m": None}
    lat, lng = _north(meters)
    verdict = api.judge_geofence(consent=True, latitude=lat, longitude=lng, accuracy_m=10, branch=branch)
    assert verdict["distance_m"] == meters and verdict["result"] == expected


@pytest.mark.parametrize(("accuracy", "expected"), [(50, "inside"), (50.2, "unknown"), (None, "unknown")])
def test_accuracy_boundary(accuracy, expected):
    branch = {"latitude": STORE[0], "longitude": STORE[1], "geofence_radius_m": 50}
    lat, lng = _north(10)
    verdict = api.judge_geofence(consent=True, latitude=lat, longitude=lng, accuracy_m=accuracy, branch=branch)
    assert verdict["result"] == expected
    assert verdict["status"] == ("approved" if expected == "inside" else "pending")


def test_branch_radius_is_used_but_capped():
    assert api.geofence_radius_m({"geofence_radius_m": None}) == api.GEOFENCE_DEFAULT_RADIUS_M
    assert api.geofence_radius_m({"geofence_radius_m": 35}) == 35
    assert api.geofence_radius_m({"geofence_radius_m": 300}) == api.GEOFENCE_MAX_RADIUS_M
    assert "pwa" in api.ATTENDANCE_SOURCES and "manual" in api.ATTENDANCE_SOURCES


# ── 멱등·상태 ─────────────────────────────────────────────────────────────────

def test_second_check_in_same_day_is_409(client, db, clock):
    _consent(client)
    assert _punch(client, "check-in", meters=10).status_code == 201
    clock["value"] = datetime(2026, 9, 30, 10, 5, tzinfo=KST)
    again = _punch(client, "check-in", meters=10)
    assert again.status_code == 409
    assert len(db.rows) == 1
    assert db.locks and all(key == f"obys-clock:{BUSINESS['id']}:staff@example.com" for key in db.locks)


def test_check_out_without_check_in_is_409(client, db):
    response = _punch(client, "check-out", meters=10)
    assert response.status_code == 409
    assert db.rows == {}


def test_check_out_uses_existing_worked_minutes_function(client, db, clock):
    _consent(client)
    _punch(client, "check-in", meters=10)
    clock["value"] = datetime(2026, 9, 30, 18, 30, tzinfo=KST)
    out = _punch(client, "check-out", meters=15)
    assert out.status_code == 200, out.text
    record = out.json()["record"]
    assert record["worked_minutes"] == api.attendance_worked_minutes(time(10, 0), time(18, 30), 0) == 510
    assert (record["start_at"], record["end_at"]) == ("10:00:00", "18:30:00")
    assert record["status"] == "approved" and record["geofence_result"] == "inside"
    assert record["open"] is False
    # 두 번째 퇴근은 열린 출근이 없으므로 409.
    assert _punch(client, "check-out", meters=15).status_code == 409


def test_overnight_shift_and_outside_check_out_is_pending(client, db, clock):
    _consent(client)
    clock["value"] = datetime(2026, 9, 30, 22, 0, tzinfo=KST)
    _punch(client, "check-in", meters=10)
    clock["value"] = datetime(2026, 10, 1, 6, 0, tzinfo=KST)
    out = _punch(client, "check-out", meters=300)
    record = out.json()["record"]
    assert record["work_date"] == "2026-09-30"
    assert record["worked_minutes"] == api.attendance_worked_minutes(time(22, 0), time(6, 0), 0) == 480
    assert (record["status"], record["geofence_result"]) == ("pending", "outside")
    assert "출근:" in record["memo"] and "퇴근:" in record["memo"] and "300m" in record["memo"]


def test_multiple_branches_require_selection(client, db):
    db.add_branch("br-mia", "미아점", 37.62, 127.03)
    _consent(client)
    assert _punch(client, "check-in", meters=10).status_code == 400
    assert _punch(client, "check-in", meters=10, branch_id="br-none").status_code == 404
    ok = _punch(client, "check-in", meters=10, branch_id="br-junghwa")
    assert ok.status_code == 201 and ok.json()["record"]["branch"] == "중화점"


# ── 본인 한정·테넌트 ────────────────────────────────────────────────────────────

def test_body_employee_email_is_ignored(client, db):
    _consent(client)
    response = _punch(client, "check-in", meters=10, employee_email="boss@example.com",
                      employee_name="대표", status="approved")
    row = _stored(db, response)
    assert row["employee_email"] == "staff@example.com"
    assert row["employee_name"] == "김직원"
    assert row["created_by"] == "Staff@Example.com"


def test_other_employee_record_is_403(client, db, as_user):
    _consent(client)
    mine = _punch(client, "check-in", meters=10).json()["record"]["id"]
    assert client.get(f"{BASE}/attendance/me/{mine}").status_code == 200
    as_user["user"] = COWORKER
    assert client.get(f"{BASE}/attendance/me/{mine}").status_code == 403
    # 동료의 퇴근 버튼은 내 열린 출근을 닫지 못한다.
    assert _punch(client, "check-out", meters=10).status_code == 409
    assert db.rows[mine]["check_out_at"] is None
    state = client.get(f"{BASE}/attendance/clock/state").json()
    assert state["recent"] == [] and state["open_record"] is None


def test_other_tenant_account_is_403(client, db, as_user):
    as_user["user"] = FOREIGN
    assert _punch(client, "check-in", meters=10).status_code == 403
    assert client.get(f"{BASE}/attendance/clock/state").status_code == 403
    assert client.post(f"{BASE}/attendance/consent", json={"agree": True}).status_code == 403
    assert db.rows == {} and db.audit == []


def test_membership_mismatch_and_viewer_are_403(client, db, as_user):
    as_user["user"] = {**EMPLOYEE, "current_membership": {"tenant_id": OTHER_TENANT, "status": "active", "role": "member"}}
    assert _punch(client, "check-in", meters=10).status_code == 403
    as_user["user"] = VIEWER
    assert _punch(client, "check-in", meters=10).status_code == 403
    assert db.rows == {}


def test_state_shows_own_open_record_without_coordinates(client, db):
    _consent(client)
    _punch(client, "check-in", meters=10)
    state = client.get(f"{BASE}/attendance/clock/state")
    assert state.status_code == 200
    body = state.json()
    assert body["consent"]["agreed"] is True
    assert body["open_record"]["geofence_result"] == "inside"
    assert body["employee"] == {"name": "김직원", "email_masked": "s***@example.com"}
    assert body["branches"] == [{"id": "br-junghwa", "name": "중화점", "has_geofence": True, "radius_m": 50}]
    assert "check_in_lat" not in json.dumps(body) and "latitude" not in json.dumps(body["branches"])


# ── 관리자 ────────────────────────────────────────────────────────────────────

def test_admin_location_summary_has_coordinates_member_forbidden(client, db, as_user):
    _consent(client)
    _punch(client, "check-in", meters=80)
    assert client.get(f"{BASE}/attendance/locations").status_code == 403
    as_user["user"] = OWNER
    response = client.get(f"{BASE}/attendance/locations", params={"result": "outside"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 1 and body["summary"]["outside"] == 1 and body["summary"]["pending"] == 1
    record = body["records"][0]
    assert record["check_in_distance_m"] == 80 and record["check_in_accuracy_m"] == 10
    assert record["check_in_lat"] == pytest.approx(_north(80)[0])
    assert "employee_email" not in record and record["employee_email_masked"] == "s***@example.com"
    assert client.get(f"{BASE}/attendance/locations", params={"result": "bogus"}).status_code == 400


def test_admin_cannot_clock_for_someone_else(client, db, as_user):
    # 관리자가 이 경로를 쓰면 관리자 본인 기록이 된다 — 대리 체크인은 불가.
    as_user["user"] = OWNER
    response = _punch(client, "check-in", consent=False, employee_email="staff@example.com")
    assert _stored(db, response)["employee_email"] == "owner@example.com"


def test_branch_geofence_patch_is_admin_only_and_audited(client, db, as_user):
    body = {"latitude": 37.5, "longitude": 127.0, "radius_m": 40}
    assert client.patch(f"{BASE}/branches/br-junghwa/geofence", json=body).status_code == 403
    as_user["user"] = OWNER
    response = client.patch(f"{BASE}/branches/br-junghwa/geofence", json=body)
    assert response.status_code == 200, response.text
    assert db.branches["br-junghwa"]["geofence_radius_m"] == 40
    assert db.audit[-1]["action"] == "attendance.geofence_config" and db.audit[-1]["resource_id"] == "br-junghwa"
    assert client.patch(f"{BASE}/branches/br-junghwa/geofence", json={**body, "radius_m": 51}).status_code == 400
    assert client.patch(f"{BASE}/branches/br-junghwa/geofence", json={**body, "radius_m": 29}).status_code == 400
    assert client.patch(f"{BASE}/branches/br-other/geofence", json=body).status_code == 404
    default = client.patch(f"{BASE}/branches/br-junghwa/geofence", json={"latitude": 37.5, "longitude": 127.0})
    assert default.json()["branch"]["geofence_radius_m"] == api.GEOFENCE_DEFAULT_RADIUS_M
    listed = client.get(f"{BASE}/branches/geofence").json()
    assert listed["branches"][0]["latitude"] == 37.5 and listed["policy"]["max_radius_m"] == 50


def test_clock_routes_are_registered_before_generic_routes():
    paths = [(route.path, sorted(route.methods)) for route in api.router.routes]
    generic_post = paths.index(("/workspaces/{business_id}/{route}/records", ["POST"]))
    generic_get = paths.index(("/workspaces/{business_id}/{route}/records/{record_id}", ["GET"]))
    generic_summary = paths.index(("/workspaces/{business_id}/{route}/summary", ["GET"]))
    for path, method in [("/workspaces/{business_id}/attendance/check-in", "POST"),
                         ("/workspaces/{business_id}/attendance/check-out", "POST"),
                         ("/workspaces/{business_id}/attendance/consent", "POST"),
                         ("/workspaces/{business_id}/attendance/me/{record_id}", "GET"),
                         ("/workspaces/{business_id}/attendance/locations", "GET"),
                         ("/workspaces/{business_id}/branches/geofence", "GET"),
                         ("/workspaces/{business_id}/branches/{branch_id}/geofence", "PATCH")]:
        index = paths.index((path, [method]))
        assert index < generic_post and index < generic_get
    # clock/state 는 세그먼트 수가 달라 제네릭 summary 와 겹치지 않지만 그래도 먼저 둔다.
    assert paths.index(("/workspaces/{business_id}/attendance/clock/state", ["GET"])) < generic_get
    assert generic_summary < generic_get


# ── PWA 자산 정적 검사 ────────────────────────────────────────────────────────
OBYS = ROOT / "app" / "static" / "apps" / "obys"


def test_manifest_required_fields():
    manifest = json.loads((OBYS / "manifest.webmanifest").read_text(encoding="utf-8"))
    for key in ("name", "short_name", "start_url", "scope", "display", "icons"):
        assert manifest.get(key), key
    assert manifest["scope"] == "/static/apps/obys/"
    assert manifest["start_url"].startswith(manifest["scope"])
    assert manifest["display"] in {"standalone", "fullscreen", "minimal-ui"}
    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes
    for icon in manifest["icons"]:
        assert (ROOT / "app" / icon["src"].lstrip("/")).is_file(), icon["src"]


def _app_shell(sw: str) -> list[str]:
    block = re.search(r"const APP_SHELL = \[(.*?)\];", sw, re.S).group(1)
    return re.findall(r'"([^"]+)"', block)


def test_service_worker_caches_only_app_shell():
    sw = (OBYS / "sw.js").read_text(encoding="utf-8")
    shell = _app_shell(sw)
    assert shell and all(path.startswith("/static/apps/obys/") for path in shell)
    for path in shell:
        assert "/api/" not in path and "token" not in path.lower() and "?" not in path
        assert (ROOT / "app" / path.lstrip("/")).is_file(), path
    # 셸이 아닌 요청(API·근태)은 손대지 않는다 — respondWith 이전에 반환.
    fetch_handler = sw[sw.index('addEventListener("fetch"'):]
    assert fetch_handler.index("if (!APP_SHELL_PATHS.has(url.pathname)) return;") < fetch_handler.index("respondWith")
    assert 'request.method !== "GET"' in fetch_handler
    for forbidden in ("Authorization", "localStorage", "aads_token", "/api/v1", "indexedDB", "sync"):
        assert forbidden not in sw.replace("// API·근태·기타 화면은 캐시하지 않는다", ""), forbidden
    assert re.search(r'const CACHE_VERSION = "obys-clock-shell-\d{8}-r\d+";', sw)


def test_clock_screen_reads_location_once_and_never_queues_offline():
    html = (OBYS / "clock.html").read_text(encoding="utf-8")
    assert "getCurrentPosition" in html
    for forbidden in ("watchPosition", "indexedDB", "SyncManager", "periodicSync", "setInterval(readPosition"):
        assert forbidden not in html, forbidden
    assert 'navigator.serviceWorker.register("/static/apps/obys/sw.js", { scope: "/static/apps/obys/" })' in html
    assert '<link rel="manifest" href="/static/apps/obys/manifest.webmanifest">' in html
    # 동의 화면: 항목·목적·보관기간·거부 결과·철회 경로
    for text in ("수집 항목", "목적", "보관 기간", "확인필요", "위치 동의 철회"):
        assert text in html, text
    assert "attendance/${action}" in html  # check-in / check-out
    assert "attendance/consent" in html
    # 오프라인이면 기록하지 않고 재시도 안내만 한다.
    assert "지금 기록할 수 없음" in html and "오프라인 기록은 저장하지 않습니다" in html
    # 직원 화면에 관리자 설정(지점 좌표·반경 저장)이 없다.
    assert "/geofence" not in html and "attendance/locations" not in html


def test_admin_geofence_screen_is_separate_and_linked():
    admin = (OBYS / "attendance-admin.html").read_text(encoding="utf-8")
    assert "/branches/${encodeURIComponent(row.dataset.branch)}/geofence" in admin and 'method: "PATCH"' in admin
    assert "watchPosition" not in admin
    index = (OBYS / "index.html").read_text(encoding="utf-8")
    assert 'href="/static/apps/obys/attendance-admin.html"' in index


# ── 마이그레이션 ─────────────────────────────────────────────────────────────

def test_migration_is_additive_manual_and_has_rollback():
    name = "20260930_obys_attendance_pwa_gps"
    sql = (ROOT / "migrations" / f"{name}.sql").read_text(encoding="utf-8")
    adds = [line for line in sql.splitlines() if line.startswith("ALTER TABLE") and "ADD COLUMN" in line]
    assert len(adds) == 16
    assert all("NOT NULL" not in line and "DEFAULT" not in line for line in adds)
    for column in ("latitude", "longitude", "geofence_radius_m", "check_in_at", "check_out_at",
                   "check_in_lat", "check_in_lng", "check_in_accuracy_m", "check_out_lat", "check_out_lng",
                   "check_out_accuracy_m", "check_in_distance_m", "check_out_distance_m",
                   "geofence_result", "device_info", "location_consent_at"):
        assert re.search(rf"ADD COLUMN IF NOT EXISTS {column} ", sql), column
    assert not re.search(r"\b(UPDATE|DELETE|TRUNCATE)\s+(FROM\s+)?yeoljeong_", sql)
    baseline = (ROOT / "scripts" / "migrations_auto_apply_baseline.txt").read_text(encoding="utf-8").splitlines()
    assert f"migrations/{name}.sql" in baseline
    assert (ROOT / "migrations" / "rollback" / f"{name}.down.sql").is_file()
    assert date(2026, 9, 30)  # 적용일 기준 스냅샷
