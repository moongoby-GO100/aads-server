from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import acct_source_ledger as source


def test_card_register_scope_and_masking_before_api():
    sql = source._card_master_sql(11)
    assert 'sf.company_id=11' in sql and 'sf.is_current IS TRUE' in sql
    assert "'카드거래처.json'" in sql
    result = sql.split('FROM resolved')[0].rsplit('SELECT card_code', 1)[1]
    assert "right(pan_digits,4)" in result and 'THEN pan ' not in result
    assert 'variants=1' in result


@pytest.mark.asyncio
async def test_card_identity_join_preserves_money_status_and_is_scoped(monkeypatch):
    fetch = AsyncMock(return_value=[dict(card_code='0001', variants=1, source_file_ids=[100],
        card_name='기업카드', card_number_masked='****-****-****-1234', master_use_code='1')])
    monkeypatch.setattr(source, '_fetch_acct_journals', fetch)
    records = [dict(card_code='0001', total_amount=120, status='보류'), dict(card_code='other', total_amount=50)]
    await source._enrich_cards(records, 7, 8)
    assert fetch.call_args.args[1] == 7
    assert 'sf.company_id=8' in fetch.call_args.args[0]
    assert records[0]['card_name'] == '기업카드'
    assert records[0]['card_number_masked'].endswith('1234')
    assert records[0]['status'] == '보류' and records[0]['total_amount'] == 120
    assert records[1]['card_master_status'] == 'missing'
    assert 'card_name' not in records[1]


@pytest.mark.asyncio
async def test_conflicting_and_numberless_cards_are_not_guessed(monkeypatch):
    fetch = AsyncMock(return_value=[
        dict(card_code='conflict', variants=2, source_file_ids=[1,2]),
        dict(card_code='no-pan', variants=1, source_file_ids=[1], card_name='카드 등록', card_number_masked=None)])
    monkeypatch.setattr(source, '_fetch_acct_journals', fetch)
    records = [dict(card_code='conflict'), dict(card_code='no-pan')]
    await source._enrich_cards(records, 11, 11)
    assert records[0]['card_master_status'] == 'conflict' and 'card_name' not in records[0]
    assert records[1]['card_master_status'] == 'number_missing'


@pytest.mark.asyncio
async def test_no_card_does_not_query_master_and_failures_are_visible(monkeypatch):
    fetch = AsyncMock(side_effect=HTTPException(502, 'source unavailable'))
    monkeypatch.setattr(source, '_fetch_acct_journals', fetch)
    await source._enrich_cards([{'card_code': ''}], 11, 11)
    fetch.assert_not_awaited()
    with pytest.raises(HTTPException):
        await source._enrich_cards([{'card_code': '1'}], 11, 11)


@pytest.mark.asyncio
async def test_unauthorized_business_never_reads_card_register(monkeypatch):
    monkeypatch.setattr(source, '_authorized_acct_scope', AsyncMock(side_effect=HTTPException(404)))
    fetch = AsyncMock()
    monkeypatch.setattr(source, '_fetch_acct_journals', fetch)
    with pytest.raises(HTTPException):
        await source.source_transactions({}, 'foreign', 'card')
    fetch.assert_not_awaited()


@pytest.mark.parametrize('status,name,number,expected', [
    ('matched', '등록 카드', '****-****-****-1234', '등록 카드'),
    ('conflict', None, None, '카드 마스터 충돌 · 확인 필요'),
    ('missing', None, None, '원천 카드코드 0001'),
])
def test_workspace_card_display_preserves_safe_identity(status, name, number, expected):
    from app.api.obys_workspaces import _record

    record = _record('acct-source', {
        'id': 'acct:100:1', 'card_code': '0001', 'total_amount': 120,
        'card_master_status': status, 'card_name': name,
        'card_number_masked': number,
    }, '사업자')
    assert record['display']['카드사'] == expected
    assert record['display']['카드번호'] == (number or '번호 미제공')
