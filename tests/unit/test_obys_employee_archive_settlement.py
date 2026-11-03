"""Employee cleanup and freelancer settlement: real HTTP boundary, isolated file store."""
import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from app.api import obys_finance as api
from app import auth

svc = api.svc
TENANT = '15055cac-71b0-45ec-b714-7093dde189ff'
ADMIN = {'email': 'owner@example.com', 'is_admin': True, 'tenant_id': TENANT,
         'current_membership': {'tenant_id': TENANT, 'status': 'active'}}
MEMBER = {**ADMIN, 'email': 'worker@example.com', 'is_admin': False}
ROW = {'id': 'employee01', 'name': '검증 직원', 'email': 'worker@example.com', 'role': 'employee',
       'tenant_id': TENANT, 'status': 'approved', 'business_id': 'biz-mia', 'branch': '열정국밥_미아점'}


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, 'DATA_DIR', tmp_path / 'data')
    monkeypatch.setattr(svc, 'UPLOAD_DIR', tmp_path / 'uploads')
    monkeypatch.setattr(svc, '_db_available', lambda: False)
    monkeypatch.setattr(svc, '_require_business_for_tenant', lambda bid, user: bid)
    # Membership storage is independent of the isolated HR store.
    calls = []
    @asynccontextmanager
    async def lock(*args):
        yield None
    async def membership(row, action, user, **kwargs):
        calls.append((row['id'], action))
        return {'status': 'linked' if action == 'approved' else 'removed'}
    monkeypatch.setattr(auth, 'employee_membership_lock', lock)
    monkeypatch.setattr(svc, 'sync_employee_tenant_membership', membership)
    svc._write_file_rows('employee_join_requests', [copy.deepcopy(ROW)])
    return calls


def client(user=ADMIN):
    app = FastAPI()
    app.include_router(api.router, prefix='/api/v1')
    app.dependency_overrides[api.get_current_user] = lambda: user
    return TestClient(app)


def test_delete_requery_restore_and_preserve_documents_and_other_employments(world):
    duplicate = {**ROW, 'id': 'employee02'}
    other = {**ROW, 'id': 'employee03', 'business_id': 'biz-sungshin', 'branch': '성신여대점'}
    svc._write_file_rows('employee_join_requests', [ROW, duplicate, other])
    for ledger in ['contracts', 'payroll_statements', 'onboarding_documents']:
        svc._write_file_rows(ledger, [{'id': 'evidence', 'tenant_id': TENANT, 'business_id': 'biz-mia', 'immutable': True}])
    before = {name: svc._read_file_rows(name) for name in ['contracts','payroll_statements','onboarding_documents']}
    c = client()
    assert c.delete('/api/v1/yeoljeong-finance/employees/approved/employee01').status_code == 200
    assert [r['id'] for r in svc.list_archived_employees(ADMIN)] == ['employee01']
    assert world == []  # duplicate and other employment retain tenant membership
    assert c.delete('/api/v1/yeoljeong-finance/employees/approved/employee01').json()['changed'] is False
    assert c.post('/api/v1/yeoljeong-finance/employees/approved/employee01/restore').status_code == 200
    assert svc.list_archived_employees(ADMIN) == []
    assert world == [('employee01','approved')]
    assert before == {name: svc._read_file_rows(name) for name in before}
    assert len(svc._read_hr('employee_join_requests', ADMIN)) == 3


def test_archive_last_revokes_then_restore_relinks_and_admin_guard(world):
    result = asyncio.run(svc.archive_employee('employee01', ADMIN))
    assert result['employee']['status'] == 'archived'
    assert world == [('employee01', 'rejected')]
    with pytest.raises(HTTPException) as exc:
        asyncio.run(svc.archive_employee('employee01', MEMBER, restore=True))
    assert exc.value.status_code == 403
    assert client().delete('/api/v1/yeoljeong-finance/employees/approved/unknown').status_code == 404
    assert client(MEMBER).get('/api/v1/yeoljeong-finance/employees/archived').status_code == 403


def test_archive_access_failure_restores_hr_row(world, monkeypatch):
    async def fail(*args, **kwargs):
        return {'status':'error'}
    monkeypatch.setattr(svc, 'sync_employee_tenant_membership', fail)
    response = client().delete('/api/v1/yeoljeong-finance/employees/approved/employee01')
    assert response.status_code == 503
    assert svc._read_hr('employee_join_requests', ADMIN)[0] == ROW


def test_other_tenant_cannot_archive_or_restore(world):
    user = {**ADMIN, 'tenant_id': '2a1b3c4d-0000-4000-8000-000000000001',
            'current_membership': {'tenant_id':'2a1b3c4d-0000-4000-8000-000000000001','status':'active'}}
    assert client(user).delete('/api/v1/yeoljeong-finance/employees/approved/employee01').status_code == 404
    assert client(user).post('/api/v1/yeoljeong-finance/employees/approved/employee01/restore').status_code == 404


def test_concurrent_signup_read_insert_is_serialized(world, monkeypatch):
    monkeypatch.setattr(svc, '_join_request_scope', lambda *args: ADMIN)
    monkeypatch.setattr(svc, '_validate_join_business_branch', lambda *args, **kwargs: None)
    payload = {'name':'반복 가입', 'email':'same@example.com','business_id':'biz-mia','branch':'미아점'}
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _:svc.upsert_join_request(payload, ADMIN), range(16)))
    assert len({r['id'] for r in results}) == 1
    rows = [r for r in svc._read_hr('employee_join_requests', ADMIN) if r['email']=='same@example.com']
    assert len(rows) == 1
    rows[0]['status'] = 'archived'
    svc._write_hr_record('employee_join_requests', rows[0], ADMIN)
    with pytest.raises(HTTPException) as exc:
        svc.upsert_join_request(payload, ADMIN)
    assert exc.value.status_code == 409


@pytest.fixture
def settlement(world, monkeypatch):
    derived = {'contract_id':'contract-signed', 'employment':{'contract_type':'freelancer',
        'wage_type':'case_fee','wage':300000,'start_date':'2026-10-15','end_date':'2026-11-20',
        'freelancer_scope':'사진 촬영 1회','freelancer_settlement_terms':'검수 후 정산','pay_date':'매월 10일'}}
    monkeypatch.setattr(svc, '_derive_current_employment', lambda *args, **kwargs: copy.deepcopy(derived))
    monkeypatch.setattr(svc, '_audit_payroll', lambda *args, **kwargs: True)
    monkeypatch.setattr(svc, '_append_employment_audit', lambda *args, **kwargs: None)
    return {'business_id':'biz-mia','employee_request_id':'employee01','employee_email':ROW['email'],
            'payroll_month':'2026-10','gross_pay':150000,'amount_confirmed':True}


def test_settlement_http_idempotency_period_totals_and_draft_visibility(settlement):
    c=client()
    path='/api/v1/yeoljeong-finance/payroll/draft-from-contract'
    r=c.post(path,json=settlement)
    assert r.status_code == 200, r.text
    row=r.json()['statement']
    assert row['status']=='draft'
    assert row['gross_pay']==150000 and row['tax_withholding']==4950 and row['net_pay']==145050
    assert row['settlement_basis']['period_start']=='2026-10-15'
    assert row['settlement_basis']['period_end']=='2026-10-31'
    assert row['settlement_basis']['scope']=='사진 촬영 1회'
    assert c.post(path,json=settlement).json()['statement']['id']==row['id']
    assert c.post(path,json={**settlement,'gross_pay':200000}).status_code==409
    assert len(svc._read_hr('payroll_statements',ADMIN))==1
    assert client(MEMBER).get('/api/v1/yeoljeong-finance/payroll').json()['statements']==[]


@pytest.mark.parametrize('change,status',[
    ({'amount_confirmed':False},400), ({'gross_pay':0},400), ({'gross_pay':True},400),
    ({'business_id':'other'},403), ({'employee_request_id':'absent'},404),
    ({'employee_email':'different@example.com'},403), ({'payroll_month':'2026-12'},409)])
def test_settlement_rejects_missing_evidence_and_foreign_scope(settlement,change,status):
    r=client().post('/api/v1/yeoljeong-finance/payroll/draft-from-contract',json={**settlement,**change})
    assert r.status_code==status,r.text
    assert svc._read_hr('payroll_statements',ADMIN)==[]


def test_concurrent_settlement_one_draft_only(settlement):
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows=list(pool.map(lambda _:svc.payroll_draft_from_contract(settlement,ADMIN),range(12)))
    assert len({r['id'] for r in rows})==1
    assert len(svc._read_hr('payroll_statements',ADMIN))==1
