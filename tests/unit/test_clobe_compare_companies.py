"""두 회사 비교: 네트워크 mock, 전 계정·페이지·비공개 필드 검증."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts import clobe_compare_companies as compare


def client_for(payloads, props=None):
    async def call(name, arguments):
        return {'structuredContent': payloads(name, arguments['input'])}

    return SimpleNamespace(
        call_tool=AsyncMock(side_effect=call),
        get_tool_input_schema=AsyncMock(return_value={'properties': {'input': {
            'properties': props or {'companyId': {'type': 'string'}},
        }}}),
        parse_tool_json=lambda result: result['structuredContent'],
        parse_companies=lambda ctx: ctx['companies'],
    )


@pytest.mark.asyncio
async def test_complete_comparison_outputs_only_account_and_label_fields():
    def payload(name, args):
        if name == 'get_my_context':
            return {'companies': [
                {'name': '미아점', 'company_id': 'base-private-company'},
                {'name': '윤희에프엔비', 'company_id': 'targ-private-company'},
            ]}
        base = args['companyId'].startswith('base')
        if name == 'search_ledger_accounts':
            return {'accounts': [
                {'code': '400', 'name': '매출' if base else '매출액', 'memo': 'PRIVATE MEMO'},
                {'code': '500' if base else '600', 'name': '매출원가' if base else '판매관리비',
                 'bankAccountNumber': '123456789012', 'employeeName': 'PRIVATE PERSON'},
                {'code': '100', 'name': '자산'},
            ]}
        return {'labels': [{'labelName': '공통'}, {'labelName': '기준' if base else '대상'}]}

    client = client_for(payload)
    report = await compare.compare(client, '미아점', '윤희에프엔비')
    assert report['accounts']['base_count'] == 3
    assert report['accounts']['base_only'] == [{'code': '500', 'name': '매출원가'}]
    assert report['accounts']['target_only'] == [{'code': '600', 'name': '판매관리비'}]
    assert report['accounts']['name_mismatch'][0]['code'] == '400'
    assert len(report['labels']['base_only']) == len(report['labels']['target_only']) == 1
    output = compare.render(report)
    assert '기준에만 있음' in output and '매출원가' in output
    for secret in ('private-company', 'PRIVATE', '123456789012', 'bankAccountNumber', 'employeeName'):
        assert secret not in output
    assert report['base_id_prefix'] == 'base' and report['target_id_prefix'] == 'targ'
    assert {c.args[0] for c in client.call_tool.await_args_list} == compare.TOOLS


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['update_memo', 'delete_employee', 'get_transactions', 'summarize_unknown'])
async def test_only_fixed_read_tools_can_be_called(name):
    client = client_for(lambda *_: {})
    with pytest.raises(compare.ComparisonError, match='read_tool_required'):
        await compare.Reader(client).call(name, {})
    client.call_tool.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('cursor', [False, True])
async def test_fetches_every_page(cursor):
    props = {'companyId': {}, 'cursor' if cursor else 'page': {}}

    def payload(_, args):
        second = args.get('cursor') == 'next' if cursor else args.get('page') == 1
        return {'data': {'accounts': [{'code': '500' if second else '400', 'name': '계정'}]},
                'pagination': {'hasNext': not second, 'nextCursor': 'next' if cursor and not second else None}}

    client = client_for(payload, props)
    rows = await compare.Reader(client).rows('search_ledger_accounts', 'base', 'accounts')
    assert len(rows) == client.call_tool.await_count == 2


@pytest.mark.asyncio
async def test_repeated_pages_fail_instead_of_reporting_partial_success():
    client = client_for(lambda *_: {'accounts': [{'code': '400', 'name': '매출'}], 'hasNext': True},
                        {'companyId': {}, 'page': {}})
    with pytest.raises(compare.ComparisonError, match='repeated_page'):
        await compare.Reader(client).rows('search_ledger_accounts', 'base', 'accounts')


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [
    {'accounts': [], 'hasNext': True}, {'accounts': [], 'totalElements': 10},
    {'unexpected': []}, {'accounts': [{'name': '매출'}]},
])
async def test_unknown_or_incomplete_results_are_not_empty_success(payload):
    client = client_for(lambda *_: payload)
    with pytest.raises(compare.ComparisonError):
        await compare.Reader(client).rows('search_ledger_accounts', 'base', 'accounts')


@pytest.mark.asyncio
@pytest.mark.parametrize('companies', [[], [{'name': '미아점', 'company_id': '1'}] * 2])
async def test_company_resolution_is_exact_and_unique(companies):
    client = client_for(lambda *_: {'companies': companies})
    with pytest.raises(compare.ComparisonError, match='company_name_missing_or_ambiguous'):
        await compare.compare(client, '미아점', '윤희에프엔비')
    assert client.call_tool.await_count == 1


@pytest.mark.asyncio
async def test_missing_acct_environment_stops_before_import_or_network(monkeypatch, capsys):
    monkeypatch.delenv('CLOBE_STORE_DATABASE_URL', raising=False)
    monkeypatch.delenv('OBYS_DATABASE_URL', raising=False)
    assert await compare.run('미아점', '윤희에프엔비') == 2
    assert json.loads(capsys.readouterr().err)['reason'] == 'acct_store_environment_missing'
    # run() disables logs for the standalone CLI; restore for other test modules.
    import logging
    logging.disable(logging.NOTSET)


def test_label_ids_are_not_cross_company_keys_and_duplicates_are_preserved():
    base = compare.normalize([{'id': 'private1', 'name': '공통'}, {'code': 'A', 'name': '라벨1'},
                              {'code': 'A', 'name': '라벨2'}], 'labels')
    target = compare.normalize([{'id': 'private2', 'name': '공통'}, {'code': 'A', 'name': '라벨1'}], 'labels')
    diff = compare.differences(base, target)
    assert diff['base_only'] == diff['target_only'] == []
    assert diff['name_mismatch'][0]['base_names'] == ['라벨1', '라벨2']


def test_obvious_personal_information_is_redacted_even_in_label_names():
    diff = compare.differences([{'code': '', 'name': '담당자 a@example.com 010-1234-5678'}], [])
    output = json.dumps(diff, ensure_ascii=False)
    assert 'a@example.com' not in output and '010-1234-5678' not in output


@pytest.mark.asyncio
async def test_schema_required_inputs_fail_before_account_call():
    client = client_for(lambda *_: {})
    client.get_tool_input_schema.return_value = {
        'properties': {'input': {'properties': {'companyId': {}, 'unknown': {}}, 'required': ['unknown']}}}
    with pytest.raises(compare.ComparisonError, match='unsupported_required_input'):
        await compare.Reader(client).rows('search_ledger_accounts', 'base', 'accounts')
    client.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_total_count_forces_next_page_despite_incorrect_has_next():
    def payload(_, args):
        page = args['page']
        return {'accounts': [{'code': str(page), 'name': '계정'}], 'totalElements': 2, 'hasNext': False}

    client = client_for(payload, {'companyId': {}, 'page': {}})
    assert len(await compare.Reader(client).rows('search_ledger_accounts', 'base', 'accounts')) == 2


@pytest.mark.asyncio
async def test_one_based_schema_pagination():
    def payload(_, args):
        return {'labels': [{'code': str(args['page']), 'name': '라벨'}], 'totalPages': 2}

    client = client_for(payload, {'companyId': {}, 'page': {'minimum': 1}})
    assert len(await compare.Reader(client).rows('get_labels', 'base', 'labels')) == 2
    assert [c.args[1]['input']['page'] for c in client.call_tool.await_args_list] == [1, 2]


@pytest.mark.asyncio
async def test_same_company_cannot_compare_itself():
    client = client_for(lambda *_: {'companies': [{'name': '미아점', 'company_id': 'base'}]})
    with pytest.raises(compare.ComparisonError, match='companies_must_differ'):
        await compare.compare(client, '미아점', '미아점')


def test_token_values_in_names_are_redacted():
    assert 'sk-private-token' not in compare.safe_text('라벨 sk-private-token')
    assert 'secret-value' not in compare.safe_text('라벨 access_token=secret-value')
