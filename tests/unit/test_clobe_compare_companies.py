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
            return {'hasMore': False, 'items': [
                {'code': 400, 'accountName': '매출' if base else '매출액', 'memo': 'PRIVATE MEMO'},
                {'code': 500 if base else 600, 'accountName': '매출원가' if base else '판매관리비',
                 'bankAccountNumber': '123456789012', 'employeeName': 'PRIVATE PERSON'},
                {'code': 100, 'accountName': '자산'},
            ]}
        if name == 'get_trial_balance':
            return {'rows': [{'name': '합계', 'accountCode': None}], 'truncated': False}
        return {'categoryLabels': [{'category1': '공통', 'categoryId': 1 if base else 2},
                                   {'category1': '기준' if base else '대상', 'category2': '하위'}],
                'groupLabels': [], 'businessEntities': [{'name': '거래처A'}] + ([{'name': '기준거래처'}] if base else []),
                'totalElements': 4 if base else 3, 'hasNext': False}

    client = client_for(payload, {'companyId': {}, 'query': {}, 'page': {}})
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
    assert report['accounts']['complete'] is True and report['complete'] is True
    assert report['vendors']['base_count'] == 2 and report['vendors']['base_only_count'] == 1
    assert '기준거래처' not in output and '거래처A' not in output
    assert report['labels']['base_count'] == 2  # 거래처는 라벨에 섞이지 않는다


def acct_client(handler, props=None):
    def payload(name, args):
        return handler(args.get('query', ''))
    return client_for(payload, props or {'companyId': {}, 'query': {}, 'size': {'maximum': 50}})


@pytest.mark.asyncio
async def test_truncated_search_is_split_by_code_prefix_until_complete():
    def handler(q):
        if q == '':
            return {'items': [{'code': 10000, 'accountName': 'a'}], 'hasMore': True}
        if q == '4':
            return {'items': [{'code': 40000 + i, 'accountName': f'n{i}'} for i in range(3)], 'hasMore': False}
        return {'items': [], 'hasMore': False}

    rows, incomplete = await compare.Reader(acct_client(handler)).search_accounts('base')
    assert incomplete == [] and len(rows) == 4


@pytest.mark.asyncio
async def test_slice_that_stays_truncated_is_marked_incomplete():
    def handler(q):
        if len(q) >= 1 and q != '4' and q != '44' and q != '444' and q != '4444' and q != '44444':
            return {'items': [], 'hasMore': False}
        return {'items': [{'code': int(q + '0' * (6 - len(q)) or 0) or 1, 'accountName': q or 'root'}], 'hasMore': True}

    rows, incomplete = await compare.Reader(acct_client(handler)).search_accounts('base')
    assert incomplete and all(set(i) <= {'4', '('} or i == '(전체)' or set(i) == {'4'} for i in incomplete)


@pytest.mark.asyncio
async def test_prefix_search_that_adds_nothing_is_marked_incomplete():
    handler = lambda q: {'items': [{'code': 1, 'accountName': 'a'}], 'hasMore': q == ''}
    _, incomplete = await compare.Reader(acct_client(handler)).search_accounts('base')
    assert incomplete == ['(전체)']


def test_incomplete_report_is_labelled_not_complete():
    empty = {'base_count': 0, 'target_count': 0, 'base_only': [], 'target_only': [], 'name_mismatch': []}
    vendors = {'base_count': 0, 'target_count': 0, 'base_only_count': 0, 'target_only_count': 0}
    report = {'complete': False, 'accounts': {**empty, 'incomplete_slices': {'base': ['4'], 'target': []}},
              'labels': empty, 'vendors': vendors}
    assert compare.render(report).startswith('## 경고: 불완전한 비교')


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


@pytest.mark.asyncio
async def test_parent_row_excluded_from_truncated_first_page_is_incomplete():
    # 코드 4 와 400~459(60건): 4 가 최초 50건에서 빠지면 자식 검색만으로는 얻을 수 없다(실제 61건).
    codes = [400 + i for i in range(60)]

    def handler(q):
        hits = [c for c in codes if str(c).startswith(q)]
        # 코드 4 는 최초 50건(그리고 '4' 검색의 50건)에서 빠진다.
        return {'items': [{'code': c, 'accountName': f'n{c}'} for c in hits[:50]], 'hasMore': len(hits) > 50}

    rows, incomplete = await compare.Reader(acct_client(handler)).search_accounts('base')
    assert len(rows) == 60 and '4' in incomplete


@pytest.mark.asyncio
async def test_truncated_parent_is_complete_when_own_row_is_in_parent_page():
    def handler(q):
        if q == '':
            return {'items': [{'code': 1, 'accountName': 'x'}], 'hasMore': True}
        if q == '1':
            return {'items': [{'code': 1, 'accountName': 'x'}], 'hasMore': True}
        if q == '10':
            return {'items': [{'code': 10, 'accountName': 'y'}], 'hasMore': False}
        return {'items': [], 'hasMore': False}

    _, incomplete = await compare.Reader(acct_client(handler)).search_accounts('base')
    assert incomplete == []


@pytest.mark.asyncio
async def test_vendor_names_are_never_printed_only_fingerprints():
    def payload(name, args):
        if name == 'get_my_context':
            return {'companies': [{'name': 'A', 'company_id': 'base-x'}, {'name': 'B', 'company_id': 'targ-x'}]}
        if name == 'search_ledger_accounts':
            return {'hasMore': False, 'items': [{'code': 1, 'accountName': 'a'}]}
        if name == 'get_trial_balance':
            return {'rows': []}
        base = args['companyId'].startswith('base')
        return {'categoryLabels': [], 'groupLabels': [], 'hasNext': False,
                'businessEntities': [{'businessEntityName': '홍길동'}] if base else [{'entityName': '김철수'}]}

    client = client_for(payload, {'companyId': {}, 'query': {}})
    report = await compare.compare(client, 'A', 'B')
    output = compare.render(report)
    assert '홍길동' not in output and '김철수' not in output
    assert report['vendors']['base_only_count'] == report['vendors']['target_only_count'] == 1
    assert all(len(f) == 8 for f in report['vendors']['base_only_fingerprints'])


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['required', 'error'])
async def test_trial_balance_is_optional(mode):
    def payload(name, args):
        if name == 'get_my_context':
            return {'companies': [{'name': 'A', 'company_id': 'base-x'}, {'name': 'B', 'company_id': 'targ-x'}]}
        if name == 'search_ledger_accounts':
            return {'hasMore': False, 'items': [{'code': 1, 'accountName': 'a'}]}
        if name == 'get_trial_balance':
            raise RuntimeError('boom PRIVATE')
        return {'categoryLabels': [], 'groupLabels': [], 'businessEntities': [], 'hasNext': False}

    client = client_for(payload, {'companyId': {}, 'query': {}})
    if mode == 'required':
        real = client.get_tool_input_schema.side_effect

        async def schema(name):
            if name == 'get_trial_balance':
                return {'properties': {'input': {'properties': {'companyId': {}, 'period': {}}, 'required': ['period']}}}
            return {'properties': {'input': {'properties': {'companyId': {}, 'query': {}}}}}
        client.get_tool_input_schema = AsyncMock(side_effect=schema)
    report = await compare.compare(client, 'A', 'B')
    sup = report['accounts']['trial_balance_supplement']['base']
    assert sup['status'] == 'skipped' and 'PRIVATE' not in json.dumps(sup)
    assert report['accounts']['base_count'] == 1 and report['complete'] is True


@pytest.mark.asyncio
async def test_entity_only_page_with_has_next_advances_instead_of_incomplete():
    def payload(_, args):
        n = args['page']
        return {'categoryLabels': [], 'groupLabels': [], 'hasNext': n == 0,
                'businessEntities': [{'name': f'거래처{n}'}]}

    client = client_for(payload, {'companyId': {}, 'page': {}})
    reader = compare.Reader(client)
    await reader.rows('get_labels', 'base', 'labels')
    assert client.call_tool.await_count == 2 and len(reader.entities) == 2


@pytest.mark.asyncio
async def test_empty_page_with_has_next_is_still_incomplete():
    client = client_for(lambda *_: {'categoryLabels': [], 'groupLabels': [], 'businessEntities': [], 'hasNext': True},
                        {'companyId': {}, 'page': {}})
    with pytest.raises(compare.ComparisonError, match='incomplete_pagination'):
        await compare.Reader(client).rows('get_labels', 'base', 'labels')
