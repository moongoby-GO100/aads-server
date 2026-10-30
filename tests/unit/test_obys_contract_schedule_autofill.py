"""계약서 근무 일정 자동 생성의 고정 패턴·보존·격리 검증."""
from datetime import date
from pathlib import Path
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
import shutil
import subprocess

import pytest
from app.api import obys_workspaces as api


def test_daily_schedule():
    result = api.contract_schedule_pattern({'daily_work_schedule': '\n'.join(
        f'{d} 09:00-21:00 (휴게 90분, 실근로 10시간 30분)' for d in '월화수목일')})
    assert set(result) == {0, 1, 2, 3, 6}
    assert all(v == dict(start_time='09:00', end_time='21:00', break_minutes=90, overnight=False) for v in result.values())


def test_overnight_fallback():
    result = api.contract_schedule_pattern({'work_days': '주 2일(월·화)', 'work_time': '22:00-10:00', 'rest_time': '90분'})
    assert set(result) == {0, 1}
    assert result[0]['overnight'] is True


@pytest.mark.parametrize('terms', [
    {'daily_work_schedule': '해당 없음(용역계약). 회차별 수행 일시는 상호 협의로 정한다.', 'work_time': '22:00~익일 10:00'},
    {'work_days': '주 5일', 'work_time': '09:00-18:00', 'rest_time': '60분'},
    {'daily_work_schedule': '월 29:00-18:00 (휴게 60분)'},
])
def test_no_inferred_days(terms):
    assert api.contract_schedule_pattern(terms) == {}


class Connection:
    def __init__(self, terms):
        self.calls = []
        self.terms = terms

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if 'yeoljeong_contracts' in sql:
            return [dict(employee_email='staff@example.test', employee_name='직원', contract_type='regular',
                         signed_at='2026-10-01', contract_payload=self.terms)]
        return [dict(employee_email='staff@example.test', work_date=date(2026, 10, 26))]

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_preserve_existing_and_scope(monkeypatch):
    conn = Connection({'daily_work_schedule': '월 09:00-21:00 (휴게 90분)'})
    monkeypatch.setattr(api, '_require_attendance_admin', lambda u: None)
    monkeypatch.setattr(api, '_schedule_business', AsyncMock())
    monkeypatch.setattr(api.upload_svc, '_tenant', lambda u: 'tenant-a')
    monkeypatch.setattr(api.upload_svc, '_connect', AsyncMock(return_value=conn))
    save = AsyncMock()
    monkeypatch.setattr(api, '_save_work_schedule', save)
    payload = api.ContractSchedulesIn(date_from='2026-10-26', date_to='2026-10-26')
    result = await api._contract_schedules('biz-a', payload, {}, apply=True)
    assert result['employees'][0]['skipped'] == 1
    save.assert_not_awaited()
    sql, args = conn.calls[0]
    assert 'tenant_id=$1::uuid AND business_id=$2' in sql
    assert "status='signed' AND deleted_at IS NULL" in sql
    assert args[:2] == ('tenant-a', 'biz-a')


@pytest.mark.asyncio
async def test_freelancer_reason(monkeypatch):
    conn = Connection({'daily_work_schedule': '상호 협의', 'work_time': '회차별 22:00~익일 10:00'})
    monkeypatch.setattr(api, '_require_attendance_admin', lambda u: None)
    monkeypatch.setattr(api, '_schedule_business', AsyncMock())
    monkeypatch.setattr(api.upload_svc, '_tenant', lambda u: 'tenant-a')
    monkeypatch.setattr(api.upload_svc, '_connect', AsyncMock(return_value=conn))
    result = await api._contract_schedules('biz-a', api.ContractSchedulesIn(date_from='2026-10-26', date_to='2026-10-26'), {})
    employee = result['employees'][0]
    assert employee['created'] == 0 and employee['unparseable'] == 1
    assert '직접 등록' in employee['reason']
    assert '회차별 22:00~익일 10:00' in employee['basis']


def test_selects_and_design():
    html = Path('app/static/apps/obys/admin.html').read_text()
    assert '<select class="field" name="employee_name"' in html
    assert '<select class="field" name="branch"' in html
    assert '<select class="field" id="peopleSearch"' in html
    assert '.people-screen' in html
    doc = Path('docs/design/20261030_ACCT_오비서 디자인 시스템.md').read_text()
    assert '44px' in doc and 'obys-design-system' in doc


def test_browser_script_syntax(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('node 없음')
    import re
    html = Path('app/static/apps/obys/admin.html').read_text()
    script = tmp_path / 'admin.js'
    script.write_text('\n'.join(re.findall(r'<script>(.*?)</script>', html, re.S)))
    subprocess.run([node, '--check', str(script)], check=True, capture_output=True)


@pytest.mark.asyncio
async def test_contract_dates_and_overwrite(monkeypatch):
    conn = Connection({'daily_work_schedule': '월 09:00-21:00 (휴게 90분)\n화 09:00-21:00 (휴게 90분)',
                       'start_date': '2026-10-26', 'end_date': '2026-10-26'})
    monkeypatch.setattr(api, '_require_attendance_admin', lambda u: None)
    monkeypatch.setattr(api, '_schedule_business', AsyncMock())
    monkeypatch.setattr(api.upload_svc, '_tenant', lambda u: 'tenant-a')
    monkeypatch.setattr(api.upload_svc, '_connect', AsyncMock(return_value=conn))
    save = AsyncMock(return_value={'id': 'existing'})
    monkeypatch.setattr(api, '_save_work_schedule', save)
    payload = api.ContractSchedulesIn(date_from='2026-10-25', date_to='2026-10-28', overwrite=True)
    result = await api._contract_schedules('biz-a', payload, {}, apply=True)
    assert result['employees'][0]['updated'] == 1
    assert save.await_count == 1
    assert save.call_args.args[2].work_date == date(2026, 10, 26)
    assert save.call_args.kwargs == {'preserve_existing': False}
    assert save.call_args.args[2].memo == '계약서 기준(서명일 2026-10-01)'


@pytest.mark.asyncio
async def test_admin_denied_before_database(monkeypatch):
    from fastapi import HTTPException
    connect = AsyncMock()
    monkeypatch.setattr(api.upload_svc, '_connect', connect)
    with pytest.raises(HTTPException) as error:
        await api._contract_schedules('biz-other', api.ContractSchedulesIn(date_from='2026-10-26', date_to='2026-10-26'), {})
    assert error.value.status_code == 403
    connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_insert_preserves_manual_schedule(monkeypatch):
    class InsertConnection:
        async def fetchrow(self, sql, *args):
            assert 'DO NOTHING RETURNING *' in sql
            assert 'DO UPDATE' not in sql
            return None
    monkeypatch.setattr(api.upload_svc, '_actor', lambda u: 'actor')
    audit = AsyncMock()
    monkeypatch.setattr(api, '_attendance_audit', audit)
    result = await api._save_work_schedule(InsertConnection(), 'biz-a', api.WorkScheduleIn(
        employee_email='staff@example.test', work_date='2026-10-26', start_time='09:00', end_time='18:00'), {}, preserve_existing=True)
    assert result is None
    audit.assert_not_awaited()


def test_hour_break_and_daily_priority():
    assert api.contract_schedule_pattern({'work_days': '월·화', 'work_time': '22:00-10:00',
        'rest_time': '1시간 30분'})[0]['break_minutes'] == 90
    result = api.contract_schedule_pattern({'daily_work_schedule': '월 09:00-21:00 (휴게 90분, 실근로 10시간 30분)',
        'work_days': '화·수', 'work_time': '10:00-18:00', 'rest_time': '60분'})
    assert set(result) == {0} and result[0]['break_minutes'] == 90


@pytest.mark.asyncio
async def test_http_preview_apply_and_foreign_business(monkeypatch):
    import httpx
    from fastapi import FastAPI, HTTPException
    from app.auth import get_current_user
    app = FastAPI()
    app.include_router(api.router)
    async def current_user():
        return {'role': 'owner'}
    app.dependency_overrides[get_current_user] = current_user
    handler = AsyncMock(return_value={'employees': [], 'applied': False})
    monkeypatch.setattr(api, '_contract_schedules', handler)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        path = '/workspaces/biz-a/attendance/schedules/from-contracts'
        assert (await client.get(path, params={'from': '2026-10-01', 'to': '2026-10-31'})).status_code == 200
        assert handler.call_args.args[1].date_from == date(2026, 10, 1)
        assert (await client.post(path, json={'from': '2026-10-01', 'to': '2026-10-31',
                                             'employee_emails': ['staff@example.test']})).status_code == 200
        assert handler.call_args.kwargs == {'apply': True}
        assert handler.call_args.args[1].overwrite is False
    # Real service must reject a foreign business before connecting to contract storage.
    monkeypatch.undo()
    monkeypatch.setattr(api, '_require_attendance_admin', lambda u: None)
    monkeypatch.setattr(api, '_business', AsyncMock(side_effect=HTTPException(404, 'foreign')))
    connect = AsyncMock()
    monkeypatch.setattr(api.upload_svc, '_connect', connect)
    with pytest.raises(HTTPException) as error:
        await api._contract_schedules('foreign', api.ContractSchedulesIn(date_from='2026-10-01', date_to='2026-10-31'), {})
    assert error.value.status_code == 403
    connect.assert_not_awaited()
