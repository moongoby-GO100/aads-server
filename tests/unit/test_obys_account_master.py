"""계정과목 적재 안전장치와 전표 상수 대조. 네트워크·DB 쓰기 없이 실행한다."""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from scripts import import_obys_account_master as importer


def voucher_accounts():
    # 서비스 패키지 초기화 없이 실제 코드의 상수를 읽는다. 가짜 475건 정본을 만들지 않는다.
    tree = ast.parse(Path('app/services/obys_clobe_voucher.py').read_text())
    constants = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'UNCLASSIFIED_NAME' for t in node.targets):
            constants['UNCLASSIFIED_NAME'] = ast.literal_eval(node.value)
        if isinstance(node, ast.AnnAssign) and getattr(node.target, 'id', '') == 'ACCOUNTS':
            return eval(compile(ast.Expression(node.value), '<account-constants>', 'eval'), {'__builtins__': {}}, constants)
    raise AssertionError('전표 상수 없음')


def source_rows():
    # 오직 테스트용 데이터다. 실제 미아점 마스터의 검증/복제본을 의미하지 않는다.
    return [{'code': str(10000 + i), 'name': f'테스트 계정 {i}'} for i in range(475)]


def test_all_twelve_voucher_constants_are_compared_without_inventing_missing_rows():
    accounts = voucher_accounts()
    assert len(accounts) == 12
    master = [{'account_code': code, 'account_name': name} for code, name in accounts.values()]
    result = importer.voucher_comparison(master, accounts)
    assert len(result) == 12
    assert {row['status'] for row in result} == {'matched'}
    master = [row for row in master if row['account_code'] != '9999']
    next(row for row in master if row['account_code'] == '10300')['account_name'] = '다른 이름'
    result = {row['account_code']: row['status'] for row in importer.voucher_comparison(master, accounts)}
    assert result['9999'] == 'missing'
    assert result['10300'] == 'name_conflict'
    assert result['10100'] == 'matched'


def test_plan_reports_missing_voucher_codes_instead_of_adding_them():
    rows = source_rows()
    report = importer.make_plan(rows, [], [], voucher_accounts())
    assert len(rows) == report['count'] == report['would_insert'] == 475
    assert report['can_apply'] is True
    assert len(report['voucher_accounts']) == 12
    assert any(row['status'] == 'missing' for row in report['voucher_accounts'])


@pytest.mark.parametrize('change', ['short', 'incomplete', 'source_conflict', 'existing_conflict'])
def test_unsafe_import_is_blocked(change):
    rows, existing, incomplete = source_rows(), [], []
    if change == 'short':
        rows.pop()
    elif change == 'incomplete':
        incomplete = ['1']
    elif change == 'source_conflict':
        rows.append({'code': rows[0]['code'], 'name': '충돌'})
    else:
        existing = [{'account_code': rows[0]['code'], 'account_name': '기존 이름'}]
    report = importer.make_plan(rows, existing, incomplete, {})
    assert report['can_apply'] is False
    if 'conflict' in change:
        assert report['name_conflict_codes'] == ['10000']


def test_exact_duplicates_are_reported_and_deduplicated():
    rows = source_rows()
    rows.append(dict(rows[0]))
    report = importer.make_plan(rows, [], [], {})
    assert report['count'] == 475 and report['duplicate_codes'] == ['10000']
    assert report['can_apply'] is True


class Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Connection:
    def __init__(self, existing=(), table_exists=True):
        self.readonly = []
        self.table_exists = table_exists
        self.existing = {r['account_code']: r['account_name'] for r in existing}
        self.execute = AsyncMock()
        self.fetch = AsyncMock(side_effect=self._fetch)
        self.fetchval = AsyncMock(side_effect=self._fetchval)

    def transaction(self, *, readonly):
        self.readonly.append(readonly)
        return Transaction()

    async def _fetch(self, sql, *args):
        assert sql == importer.EXISTING_SQL
        return [{'account_code': c, 'account_name': n} for c, n in self.existing.items()]

    async def _fetchval(self, sql, *args):
        if sql == importer.SOURCE_SQL:
            return args[1] == '미아점'
        if 'to_regclass' in sql:
            return self.table_exists
        assert sql == importer.INSERT_SQL
        _, code, name, label = args
        assert label == '미아점'
        if code in self.existing:
            return None
        self.existing[code] = name
        return code


def reader_for(rows=None, incomplete=None):
    return SimpleNamespace(search_accounts=AsyncMock(return_value=(rows or source_rows(), incomplete or [])),
                           duplicate_codes=set())


@pytest.mark.parametrize('table_exists', [True, False])
def test_default_dry_run_never_writes_even_before_migration(table_exists):
    conn = Connection(table_exists=table_exists)
    tenant = uuid4()
    report = asyncio.run(importer.import_accounts(conn, reader_for(), tenant_id=tenant,
                                                  company_id='mia-test', apply=False, accounts=voucher_accounts()))
    assert conn.readonly == [True, True]
    conn.execute.assert_not_awaited()
    assert not any(call.args[0] == importer.INSERT_SQL for call in conn.fetchval.await_args_list)
    assert report['inserted'] == 0 and report['mode'] == 'dry-run'
    if table_exists:
        assert conn.fetch.await_args.args == (importer.EXISTING_SQL, tenant)
    else:
        conn.fetch.assert_not_awaited()


def test_apply_inserts_once_with_lock_and_does_not_update_existing():
    conn, tenant, reader = Connection(), uuid4(), reader_for()
    first = asyncio.run(importer.import_accounts(conn, reader, tenant_id=tenant, company_id='mia-test', apply=True, accounts={}))
    second = asyncio.run(importer.import_accounts(conn, reader, tenant_id=tenant, company_id='mia-test', apply=True, accounts={}))
    assert first['inserted'] == 475 and second['inserted'] == 0
    assert conn.readonly == [True, False, True, False]
    assert 'LOCK TABLE' in conn.execute.await_args_list[1].args[0]
    assert 'DO NOTHING' in importer.INSERT_SQL and 'UPDATE' not in importer.INSERT_SQL


def test_apply_conflict_stops_before_any_insert():
    conn = Connection([{'account_code': '10000', 'account_name': '기존 이름'}])
    with pytest.raises(importer.ComparisonError, match='account_import_validation_failed'):
        asyncio.run(importer.import_accounts(conn, reader_for(), tenant_id=uuid4(), company_id='mia-test', apply=True, accounts={}))
    assert not any(call.args[0] == importer.INSERT_SQL for call in conn.fetchval.await_args_list)


@pytest.mark.parametrize('tool', ['get_my_context', 'get_labels', 'create_journal', 'get_trial_balance'])
def test_only_account_search_tool_is_allowed(tool):
    client = SimpleNamespace(call_tool=AsyncMock())
    with pytest.raises(importer.ComparisonError, match='account_search_only'):
        asyncio.run(importer.AccountReader(client).call(tool, {}))
    client.call_tool.assert_not_awaited()


def test_only_account_fields_are_retained_and_duplicates_are_counted():
    payload = {'items': [{'code': '10100', 'name': '현금', 'businessEntities': [{'name': 'PRIVATE_PERSON'}]}] * 2,
               'hasMore': False}
    client = SimpleNamespace(call_tool=AsyncMock(return_value=payload), parse_tool_json=lambda x: x,
                             get_tool_input_schema=AsyncMock(return_value={'properties': {'companyId': {}, 'query': {}}}))
    reader = importer.AccountReader(client)
    rows, incomplete = asyncio.run(reader.search_accounts('mia-test'))
    assert rows == [{'code': '10100', 'name': '현금'}] and incomplete == []
    assert reader.duplicate_codes == {'10100'}
    assert 'PRIVATE_PERSON' not in json.dumps(importer.make_plan(rows, [], incomplete, {}))


def test_schema_exists_only_in_drafts_and_has_tenant_code_key():
    filename = '20261124_obys_account_master.sql'
    assert list(Path('migrations').rglob(filename)) == [Path('migrations/drafts') / filename]
    sql = (Path('migrations/drafts') / filename).read_text()
    assert 'UNIQUE (tenant_id, account_code)' in sql
    assert all(field in sql for field in ('source_company_label', 'is_active', 'created_at', 'updated_at'))
    assert 'INSERT' not in sql and 'DROP' not in sql


def test_api_is_get_only_and_tenant_bound_query_uses_literal_search():
    tree = ast.parse(Path('app/api/obys_finance.py').read_text())
    node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'list_account_master')
    assert ast.unparse(node.decorator_list[0]) == "router.get('/account-master')"
    assert 'tenant_id' not in [arg.arg for arg in node.args.args]
    source = Path('app/services/obys_account_master.py').read_text()
    assert 'WHERE tenant_id = $1' in source and 'strpos(' in source
    assert 'transaction(readonly=True)' in source
    assert source.index('tenant_session_for_user(user)') < source.index('await uploads._connect()')


def test_admin_menu_and_readonly_screen_are_wired():
    html = Path('app/static/apps/obys/admin.html').read_text()
    js = Path('app/static/apps/obys/modules/admin-extras-v41.js').read_text()
    assert '["계정과목","account-master"]' in html
    assert 'admin-extras-v41.js?v=20261124-r1' in html
    assert 'renderAccountMaster(body, ctx, env)' in js
    assert '/api/v1/yeoljeong-finance/account-master?' in js
    section = js[js.index('  function renderAccountMaster'):js.index('  // ── 월마감')]
    assert all(label in section for label in ('코드·이름 검색', '활성', '비활성', '출처', '검색 결과가 없습니다.'))
    assert 'esc(row.account_name)' in section
    assert 'seq !== requestId' in section and 'body.isConnected' in section
    assert 'method:' not in section


def test_cli_defaults_to_readonly_without_credentials():
    import os
    import sys
    env = {k: v for k, v in os.environ.items() if k not in {'OBYS_DATABASE_URL', 'OBYS_VAULT_KEY'}}
    result = subprocess.run([sys.executable, 'scripts/import_obys_account_master.py', '--tenant-id', str(uuid4()),
                             '--company-id', 'mia-test'], env=env, capture_output=True, text=True)
    assert result.returncode == 2
    assert json.loads(result.stderr)['reason'] == 'acct_environment_missing'
    assert not result.stdout


def test_unverified_source_company_is_rejected_before_clobe_call():
    conn, reader = Connection(), reader_for()
    conn.fetchval = AsyncMock(return_value=False)
    with pytest.raises(importer.ComparisonError, match='mia_company_identity_unverified'):
        asyncio.run(importer.import_accounts(conn, reader, tenant_id=uuid4(), company_id='other-company', apply=False, accounts={}))
    reader.search_accounts.assert_not_awaited()
    conn.execute.assert_not_awaited()
