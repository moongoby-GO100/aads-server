from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.api import acct_source_documents as docs, acct_source_ledger as source, obys_workspaces as api


def test_tax_invoice_is_not_misclassified_as_cash_receipt():
    row = source._row({'evidence_code':'2','detail_type':'51','invoice_number':'invoice'},'purchase')
    assert row['evidence_label']=='전자세금계산서'
    assert source._row({'evidence_code':'2'},'purchase')['evidence_label']=='증빙 확인 필요'
    assert source._row({'detail_type':'61'},'purchase')['evidence_label']=='현금영수증'


def test_bank_withdrawals_are_signed_and_do_not_become_sales():
    row = source._row({'deposit_amount':0,'withdraw_amount':1250,'status_code':'3'},'bank')
    assert row['total_amount']==Decimal('-1250')
    assert row['direction']=='out'
    assert row['status']=='제외(중복/취소)'
    assert row['category']=='bank'
    assert '확인 필요' in row['account_label']


def test_query_filters_and_totals_precede_page_limit():
    sql=source._pivot_sql(11,'card','2026-01-01','2026-08-31',limit=50,offset=2500,search='매입처',status='확정')
    assert "sf.company_id=11" in sql
    assert "marker.field_key='cd_ctrade'" in sql
    assert 'LIMIT 50 OFFSET 2500' in sql
    assert sql.index("status_code IN ('2')") < sql.index('LIMIT 50')
    assert 'source_confirmed_amount' in sql
    assert 'source_file_id::text' in sql  # no sequence: never merge rec_idx across files
    with pytest.raises(HTTPException):
        source._pivot_sql(11,'bank',None,None,search="'; DROP TABLE x;--")


@pytest.mark.asyncio
async def test_documents_never_expose_absolute_paths_or_book_raw_cells(monkeypatch):
    monkeypatch.setattr(docs,'_authorized_acct_scope',AsyncMock(return_value=(11,11)))
    fetch=AsyncMock(return_value=[{'id':12,'total':1,'filename':'월별.xlsx','sheets':'[{"id":327,"name":"집계"}]'}])
    monkeypatch.setattr(docs,'_fetch_acct_journals',fetch)
    result=await docs.source_documents({},'owned','sales')
    assert result['files'][0]['transaction_status']=='원문 조회 가능 · 거래 합계 미포함'
    assert 'total_amount' not in result['files'][0]
    sql,tenant=fetch.call_args.args
    assert tenant==11 and 'sf.company_id=11' in sql
    assert "regexp_replace(sf.abs_path, '^.*/', '')" in sql


@pytest.mark.asyncio
async def test_sheet_rejects_cross_company_file_before_cell_read(monkeypatch):
    monkeypatch.setattr(docs,'_authorized_acct_scope',AsyncMock(return_value=(11,11)))
    fetch=AsyncMock(return_value=[])
    monkeypatch.setattr(docs,'_fetch_acct_journals',fetch)
    with pytest.raises(HTTPException) as e:
        await docs.source_sheet({},'owned',99,3)
    assert e.value.status_code==404 and fetch.await_count==1
    assert 'atom_cell' not in fetch.call_args.args[0]


@pytest.mark.asyncio
async def test_sheet_paginates_sparse_rows_and_masks_identifiers(monkeypatch):
    monkeypatch.setattr(docs,'_authorized_acct_scope',AsyncMock(return_value=(11,11)))
    fetch=AsyncMock(side_effect=[[{'id':3}],[{'row_idx':12,'cells':{'1':'010-1234-5678','2':'=SUM(A1:A2)'}},{'row_idx':1000,'cells':{'1':'next'}}]])
    monkeypatch.setattr(docs,'_fetch_acct_journals',fetch)
    result=await docs.source_sheet({},'owned',99,3,after_row=8,limit=1)
    assert result['has_more'] and result['next_after_row']==12
    assert result['rows'][0]['cells']['1']=='[식별번호 가림]'
    assert result['rows'][0]['cells']['2']=='=SUM(A1:A2)'
    sql=fetch.call_args.args[0]
    assert 'c.row_idx>8' in sql and 'sf.company_id=11' in sql


@pytest.mark.asyncio
async def test_source_errors_are_not_returned_as_empty_lists(monkeypatch):
    monkeypatch.setattr(docs,'_authorized_acct_scope',AsyncMock(return_value=(11,11)))
    monkeypatch.setattr(docs,'_fetch_acct_journals',AsyncMock(side_effect=HTTPException(502)))
    with pytest.raises(HTTPException) as e:
        await docs.source_documents({},'owned')
    assert e.value.status_code==502


@pytest.mark.asyncio
async def test_records_page_combines_local_and_remote_without_skips(monkeypatch):
    monkeypatch.setattr(api,'_business',AsyncMock(return_value={'id':'owned','name':'회사'}))
    monkeypatch.setattr(api,'_local_source_rows',AsyncMock(return_value=[('ledger',{'id':'local','category':'sales'})]))
    async def remote(*args,**kwargs):
        start=kwargs['offset'];n=kwargs['limit']
        return [{'id':f'acct:1:{i}','source_total_count':2501} for i in range(start,min(start+n,2501))],'acct'
    mock=AsyncMock(side_effect=remote)
    monkeypatch.setattr(source,'source_transactions',mock)
    app=FastAPI();app.include_router(api.router)
    app.dependency_overrides[api.get_current_user]=lambda:{'tenant_id':'owned'}
    async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as c:
        first=(await c.get('/workspaces/owned/sales/records?limit=2')).json()
        second=(await c.get('/workspaces/owned/sales/records?limit=2&offset=2')).json()
        late=(await c.get('/workspaces/owned/sales/records?limit=2&offset=2500')).json()
    assert [r['id'] for r in first['records']]==['local','acct:1:0']
    assert [r['id'] for r in second['records']]==['acct:1:1','acct:1:2']
    assert [r['id'] for r in late['records']]==['acct:1:2499','acct:1:2500']
    assert not late['has_more']
