"""직원 본인 신규 근무 신청(원본 근태 없음) — 신청·중복·승인·반려·권한 회귀."""
from contextlib import asynccontextmanager
from datetime import date, time
from unittest.mock import AsyncMock
import json
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from app.services import obys_attendance_corrections as svc


def entry(**kw):
    return svc.NewEntryIn(**{'work_date': '2026-11-03', 'start_at': '09:00', 'end_at': '18:00',
                             'break_minutes': 60, 'reason': '위치 확인 불가로 직접 입력', **kw})


class DB:
    def __init__(self):
        self.inserted_requests = []
        self.attendance_inserts = []
        self.updates = []
        self.live_record = False
        self.pending_dup = False
        self.request = None
        self.execute = AsyncMock()
        self.close = AsyncMock()
        self.fetch = AsyncMock(return_value=[])
        self.committed = False

    @asynccontextmanager
    async def transaction(self):
        yield
        self.committed = True

    async def fetchval(self, sql, *args):
        if 'FROM yeoljeong_attendance_records' in sql:
            return self.live_record
        if 'FROM obys_attendance_corrections' in sql:
            return self.pending_dup
        raise AssertionError(sql)

    async def fetchrow(self, sql, *args):
        if sql.startswith('INSERT INTO obys_attendance_corrections'):
            row = dict(id=args[0], tenant_id=args[1], business_id=args[2], record_id=None, employee_email=args[3],
                       original=args[4], requested=args[5], reason=args[6], kind='new_entry', status='pending', version=1,
                       decided_by=None, decision_reason=None)
            self.inserted_requests.append(row)
            self.request = row
            return row
        if sql.startswith('SELECT * FROM obys_attendance_corrections'):
            return self.request
        if sql.startswith('SELECT employee_name,branch'):
            return {'employee_name': '김직원', 'branch': '미아점', 'hourly_wage': 10030}
        if sql.startswith('INSERT INTO yeoljeong_attendance_records'):
            self.attendance_inserts.append(args)
            return {'id': 'rec-1', 'employee_email': args[1], 'work_date': args[5], 'source': 'manual'}
        if sql.startswith('UPDATE obys_attendance_corrections'):
            self.updates.append(args)
            return {**self.request, 'status': args[3], 'version': 2, 'record_id': args[6], 'decided_by': args[4], 'decision_reason': args[5]}
        raise AssertionError(sql)


@pytest.fixture
def db(monkeypatch):
    db = DB()
    state = {'admin': True}
    db.state = state
    monkeypatch.setattr(svc.ws, '_clock_identity', lambda u: {'email': 'Me@Example.com', 'masked': 'm***@example.com', 'name': '김직원'})
    monkeypatch.setattr(svc.ws, '_clock_business', AsyncMock())
    def require_admin(u):
        if not state['admin']:
            raise HTTPException(403, '소유자·관리자만')
    monkeypatch.setattr(svc.ws, '_require_attendance_admin', require_admin)
    monkeypatch.setattr(svc.ws.upload_svc, '_tenant', lambda u: 'tenant')
    monkeypatch.setattr(svc.ws.upload_svc, 'tenant_session_for_user', lambda u: {'permissions': {'can_manage_settings': state['admin']}})
    monkeypatch.setattr(svc.ws.upload_svc, '_connect', AsyncMock(return_value=db))
    monkeypatch.setattr(svc.ws.upload_svc, '_require_business', AsyncMock())
    monkeypatch.setattr(svc.ws, '_attendance_audit', AsyncMock())
    monkeypatch.setattr(svc.ws, '_recompute_attendance_week', AsyncMock())
    return db


@pytest.mark.asyncio
async def test_employee_creates_pending_request_for_self(db):
    db.state['admin'] = False
    result = await svc.run({}, 'b', 'create_new', payload=entry())
    assert result['status'] == 'pending' and result['kind'] == 'new_entry'
    assert result['employee_email'] == 'me@example.com'
    assert result['record_id'] is None
    assert db.committed and not db.attendance_inserts
    assert json.loads(db.inserted_requests[0]['requested'])['work_date'] == '2026-11-03'
    svc.ws._recompute_attendance_week.assert_not_awaited()


@pytest.mark.parametrize('field', ['employee_email', 'employee_name', 'business_id', 'record_id'])
def test_cannot_name_another_employee(field):
    with pytest.raises(ValidationError):
        entry(**{field: 'other@example.com'})


@pytest.mark.parametrize('values', [{'reason': ' '}, {'break_minutes': -1}, {'start_at': '09:00:30'}, {'end_at': '09:00'}])
def test_invalid_entry(values):
    with pytest.raises((ValidationError, HTTPException)):
        entry(**values)


@pytest.mark.asyncio
async def test_foreign_business_is_403(db, monkeypatch):
    async def denied(*a):
        raise HTTPException(403, '다른 테넌트')
    monkeypatch.setattr(svc.ws, '_clock_business', denied)
    with pytest.raises(HTTPException) as e:
        await svc.run({}, 'foreign', 'create_new', payload=entry())
    assert e.value.status_code == 403
    svc.ws.upload_svc._connect.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('dup', ['live', 'pending'])
async def test_duplicate_is_409(db, dup):
    db.live_record = dup == 'live'
    db.pending_dup = dup == 'pending'
    with pytest.raises(HTTPException) as e:
        await svc.run({}, 'b', 'create_new', payload=entry())
    assert e.value.status_code == 409
    assert not db.inserted_requests and not db.committed


@pytest.mark.asyncio
async def test_admin_approval_creates_one_manual_record_and_recomputes_week(db):
    await svc.run({}, 'b', 'create_new', payload=entry())
    result = await svc.run({}, 'b', 'approve', correction_id=db.request['id'], payload=svc.DecisionIn(version=1, reason='확인'))
    assert result['status'] == 'approved' and result['record_id'] == 'rec-1'
    assert len(db.attendance_inserts) == 1
    args = db.attendance_inserts[0]
    assert args[1] == 'me@example.com' and args[5] == date(2026, 11, 3) and args[6] == time(9)
    assert args[3] == '김직원' and args[4] == '미아점' and args[9] == 480
    svc.ws._recompute_attendance_week.assert_awaited_once()
    assert svc.ws._recompute_attendance_week.await_args.args[2] == 'me@example.com'
    assert svc.ws._attendance_audit.await_args.args[3] == 'attendance.correction.approve'


@pytest.mark.asyncio
async def test_rejection_creates_no_record(db):
    await svc.run({}, 'b', 'create_new', payload=entry())
    result = await svc.run({}, 'b', 'reject', correction_id=db.request['id'], payload=svc.DecisionIn(version=1, reason='근거 부족'))
    assert result['status'] == 'rejected'
    assert not db.attendance_inserts
    svc.ws._recompute_attendance_week.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['approve', 'reject'])
async def test_employee_cannot_decide(db, operation):
    await svc.run({}, 'b', 'create_new', payload=entry())
    db.state['admin'] = False
    with pytest.raises(HTTPException) as e:
        await svc.run({}, 'b', operation, correction_id=db.request['id'], payload=svc.DecisionIn(version=1, reason='x'))
    assert e.value.status_code == 403
    assert not db.attendance_inserts


@pytest.mark.asyncio
async def test_approval_blocked_by_confirmed_payroll(db):
    await svc.run({}, 'b', 'create_new', payload=entry())
    db.fetch.return_value = [{'id': 'p', 'payroll_month': '2026-11', 'status': 'paid'}]
    with pytest.raises(HTTPException) as e:
        await svc.run({}, 'b', 'approve', correction_id=db.request['id'], payload=svc.DecisionIn(version=1, reason='확인'))
    assert e.value.status_code == 409
    assert not db.attendance_inserts


@pytest.mark.asyncio
async def test_api_route_is_login_bound(monkeypatch):
    from fastapi import FastAPI
    import httpx
    from app.api.obys_attendance_corrections import router
    from app.auth import get_current_user
    app = FastAPI()
    app.include_router(router, prefix='/api/v1')
    app.dependency_overrides[get_current_user] = lambda: {'email': 'me@example.com'}
    run = AsyncMock(return_value={'id': 'n'})
    monkeypatch.setattr(svc, 'run', run)
    path = '/api/v1/workspaces/b/attendance/corrections'
    body = {'work_date': '2026-11-03', 'start_at': '09:00', 'end_at': '18:00', 'break_minutes': 60, 'reason': '직접 입력'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://t') as client:
        assert (await client.post(path, json={**body, 'employee_email': 'x@example.com'})).status_code == 422
        run.assert_not_awaited()
        assert (await client.post(path, json=body)).status_code == 201
    assert run.await_args.args[2] == 'create_new'


def test_restore_of_deleted_row_clears_old_clock_and_location_evidence():
    """삭제된 PWA 기록과 같은 키로 승인해도 과거 출퇴근·위치 증거가 manual 행에 남지 않는다."""
    import inspect
    sql = inspect.getsource(svc.approve_new_entry)
    update = sql[sql.index("DO UPDATE SET"):sql.index("RETURNING *")]
    for column in ("check_in_at", "check_out_at", "check_in_lat", "check_in_lng", "check_in_accuracy_m",
                   "check_out_lat", "check_out_lng", "check_out_accuracy_m", "check_in_distance_m",
                   "check_out_distance_m", "geofence_result", "device_info", "location_consent_at"):
        assert f"{column}=NULL" in update, column


def test_self_entry_ui_clears_on_logout_and_refreshes_on_reload():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "app/static/apps/obys"
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "modules/attendance-corrections.js").read_text(encoding="utf-8")
    logout = html[html.index("async function logoutFromServer"):html.index("async function refreshFinanceSession")]
    assert "clearEmployeeSelfEntry()" in logout
    mount = html[html.index("function mountEmployeeSelfEntry"):html.index("async function loadAttendanceRecords")]
    assert "currentEmployeeEmail()" in mount and "obysRefresh" in mount
    assert "data-self-refresh" in js and "host.obysRefresh = refresh" in js
