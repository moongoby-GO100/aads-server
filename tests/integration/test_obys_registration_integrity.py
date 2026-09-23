"""Real PostgreSQL: registration must survive list/detail/summary and tenant filters."""
import io
from uuid import UUID, uuid4
from decimal import Decimal

import asyncpg
import pytest
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers

from app.api import obys_workspaces as api
from app.services import obys_upload_service as service
from tests.integration.test_obys_ledger_details_postgres import _url, _user, _business


@pytest.mark.asyncio
@pytest.mark.parametrize('route,category', [('sales','sales'),('purchases','purchase'),('cards','card'),('accounts','transaction')])
async def test_registration_roundtrip(monkeypatch, tmp_path, route, category):
    url = _url()
    monkeypatch.setenv('OBYS_DATABASE_URL', url)
    monkeypatch.setattr(service, 'UPLOAD_ROOT', tmp_path)
    tenant, foreign = uuid4(), uuid4()
    biz, other = 'verify-'+uuid4().hex[:16], 'verify-'+uuid4().hex[:16]
    conn = await asyncpg.connect(url)
    try:
        await _business(conn, tenant, biz, '등록 검증')
        await _business(conn, tenant, other, '다른 사업자')
    finally:
        await conn.close()
    user = _user(tenant, 'verification@example.invalid')
    async def no_mapping(*args, **kwargs):
        raise HTTPException(status_code=403, detail='isolated test business: no ACCT mapping')
    monkeypatch.setattr(api.acct_source_ledger, 'source_transactions', no_mapping)
    payload = api.WorkspaceManualRecord(occurred_on='2026-09-23', counterparty='new entry',
        description='persist me', supply_amount=100, tax_amount=10, total_amount=110,
        card_last4='1234', direction='out', account_label='검증계좌')
    created = await api.create_workspace_record(biz, route, payload, user)
    record_id = str(created['record']['id'])
    records = await api.workspace_records(biz, route, '2026-09-23', '2026-09-23', '전체 상태', '', 200, 0, user)
    assert [r['id'] for r in records['records']] == [record_id]
    assert records['records'][0]['display']['일자'] == '2026-09-23'
    detail = await api.workspace_record_detail(biz, route, record_id, user)
    assert detail['record']['display']['일자'] == '2026-09-23'
    expected = '-110원' if route == 'accounts' else '110원'
    summary = await api.workspace_summary(biz, route, '2026-09-23', '2026-09-23', user)
    assert summary['metrics'][1]['value'] == expected
    yesterday = await api.workspace_records(biz, route, '2026-09-22', '2026-09-22', '전체 상태', '', 200, 0, user)
    assert yesterday['count'] == 0
    for wrong_biz, wrong_user in [(other,user),(biz,_user(foreign,'other@example.invalid'))]:
        with pytest.raises(HTTPException) as denied:
            await api.workspace_record_detail(wrong_biz, route, record_id, wrong_user)
        assert denied.value.status_code == 404

    csv = b'date,amount,counterparty,description\n2026-09-23,220,upload,persist upload\n2026-09-23,220,upload,persist upload\n'
    def upload():
        return UploadFile(io.BytesIO(csv), filename='ledger.csv', headers=Headers({'content-type':'text/csv'}))
    preview = (await api.preview_workspace_import(biz, route, upload(), user))['preview']
    assert preview['accepted_rows'] == 1 and preview['duplicate_rows'] == 1
    saved = await api.commit_workspace_import(biz, route, upload(), preview['sha256'], user)
    assert saved['upload']['imported_rows'] == 1 and saved['upload']['duplicate_rows'] == 1
    repeated = await api.commit_workspace_import(biz, route, upload(), preview['sha256'], user)
    assert repeated['upload']['status'] == 'duplicate'
    again = (await api.preview_workspace_import(biz, route, upload(), user))['preview']
    assert again['accepted_rows'] == 0 and again['duplicate_rows'] == 2
    records = await api.workspace_records(biz, route, '2026-09-23', '2026-09-23', '전체 상태', '', 200, 0, user)
    assert records['count'] == 2
    uploaded_id = next(r['id'] for r in records['records'] if r['id'] != record_id)
    assert (await api.workspace_record_detail(biz, route, uploaded_id, user))['record']['amount'] == '220.00'
    summary = await api.workspace_summary(biz, route, '2026-09-23', '2026-09-23', user)
    assert summary['metrics'][1]['value'] == ('110원' if route == 'accounts' else '330원')

    # Old uploaded rows must remain available when recent rows exceed a page.
    conn = await asyncpg.connect(url)
    try:
        await conn.execute('''INSERT INTO yeoljeong_uploaded_ledger_rows
          (id,tenant_id,business_id,upload_id,category,source_hash,occurred_on,amount,payload)
          SELECT gen_random_uuid(),$1,$2,$3,$4,'fill-'||n,'2026-09-24',1,'{}'::jsonb
          FROM generate_series(1,501) n''', tenant,biz,UUID(saved['upload']['id']),category)
    finally:
        await conn.close()
    old = await service.list_ledger_rows(user=user,business_id=biz,category=category,date_from='2026-09-23',date_to='2026-09-23')
    assert len(old) == 1
    full = await api.workspace_summary(biz, route, None, None, user)
    assert full['source']['record_count'] == 503
    assert full['metrics'][1]['value'] == ('611원' if route == 'accounts' else '831원')
    next_page = await api.workspace_records(biz, route, None, None, '전체 상태', '', 500, 500, user)
    assert next_page['count'] == 3
    assert (await api.workspace_record_detail(biz, route, uploaded_id, user))['record']['id'] == uploaded_id
    assert (await api.workspace_record_detail(biz, route, record_id, user))['record']['id'] == record_id


def test_bank_import_retains_withdrawal_direction():
    preview = service.preview_upload(category='transaction',filename='bank.csv',content_type='text/csv',
        data='일자,입금액,출금액\n2026-09-23,0,500\n2026-09-23,700,0\n2026-09-23,300,200'.encode())
    assert [r['amount'] for r in preview['preview_rows']] == ['-500','700']
    assert preview['rejected_rows'] == 1


@pytest.mark.asyncio
async def test_tax_sales_classification_and_fractional_bank_rejection(monkeypatch, tmp_path):
    url = _url()
    monkeypatch.setenv('OBYS_DATABASE_URL',url)
    tenant=uuid4();biz='verify-'+uuid4().hex[:16];user=_user(tenant,'tax@example.invalid')
    conn=await asyncpg.connect(url)
    try:
        await _business(conn,tenant,biz,'세무 등록')
    finally:
        await conn.close()
    payload=api.WorkspaceManualRecord(occurred_on='2026-09-23',supply_amount=100,tax_amount=10,total_amount=110,ledger_category='sales')
    saved=await api.create_workspace_record(biz,'tax-evidence',payload,user)
    assert saved['record']['category']=='sales'
    detail=await api.workspace_record_detail(biz,'tax-evidence',saved['record']['id'],user)
    assert detail['record']['raw']['category']=='sales'
    with pytest.raises(HTTPException) as invalid:
        await api.create_workspace_record(biz,'accounts',payload.model_copy(update={'total_amount':Decimal('110.5')}),user)
    assert invalid.value.status_code==422
