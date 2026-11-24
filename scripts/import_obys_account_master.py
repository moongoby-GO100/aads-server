#!/usr/bin/env python3
"""미아점 계정과목 475건을 점검한다. 기본 dry-run, --apply만 INSERT.

--company-id에는 클로브 미아점의 확인된 회사 ID, --tenant-id에는 귀속 테넌트를 지정한다.
기존 obys_clobe_company_link에서 미아점 ID임을 확인한 뒤 조회한다.
클로브 호출은 search_ledger_accounts 하나뿐이며 기존 전량 조회를 재사용한다.
OBYS_DATABASE_URL/OBYS_VAULT_KEY는 기존 ACCT 실행 환경에서 주입한다.
기존 행은 덮어쓰지 않는다. 불완전 조회/475건 불일치/이름 충돌은 적재를 차단한다.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
import json
import logging
import os
from pathlib import Path
import sys
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.clobe_compare_companies import ComparisonError, Reader, collection, normalize

SOURCE_COMPANY_LABEL = '미아점'
EXPECTED_COUNT = 475
SOURCE_SQL = """SELECT EXISTS(SELECT 1 FROM obys_clobe_company_link
WHERE clobe_company_id=$1 AND company_name=$2)"""
EXISTING_SQL = 'SELECT account_code, account_name FROM obys_account_master WHERE tenant_id=$1'
INSERT_SQL = """INSERT INTO obys_account_master
(tenant_id, account_code, account_name, source, source_company_label)
VALUES ($1, $2, $3, 'clobe', $4)
ON CONFLICT (tenant_id, account_code) DO NOTHING RETURNING account_code"""


class AccountReader(Reader):
    """도구 허용 범위를 search 하나로 더 좁힌다. 원문 payload는 출력하지 않는다."""
    def __init__(self, client):
        super().__init__(client)
        self.duplicate_codes: set[str] = set()

    async def call(self, name: str, args: dict) -> dict:
        if name != 'search_ledger_accounts':
            raise ComparisonError('account_search_only')
        payload = await super().call(name, args)
        rows, _ = collection(payload, ('items', 'ledgerAccounts', 'accounts'))
        # 서로 다른 접두 조회의 겹침은 정상이다. 한 응답 내 코드 중복만 집계한다.
        counts = Counter(normalize([row], 'accounts')[0]['code'] for row in rows)
        self.duplicate_codes.update(code for code, count in counts.items() if count > 1)
        return payload


def voucher_comparison(rows: list[dict], accounts: dict) -> list[dict]:
    """12개 상수 각각의 존재·이름 일치 여부. 누락을 가짜 마스터 행으로 보완하지 않는다."""
    by_code = {r['account_code']: r for r in rows}
    result = []
    for key, (code, name) in accounts.items():
        row = by_code.get(code)
        result.append({
            'key': key, 'account_code': code,
            'status': 'missing' if row is None else 'matched' if row['account_name'] == name else 'name_conflict',
        })
    return result


def make_plan(rows: list[dict], existing: list[dict], incomplete: list[str], accounts: dict) -> dict:
    names: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        names[row['code']].add(row['name'])
    current = {r['account_code']: r['account_name'] for r in existing}
    counts = Counter(r['code'] for r in rows)
    conflicts = sorted(code for code, values in names.items()
                       if len(values) > 1 or (code in current and values != {current[code]}))
    return {
        'source_company_label': SOURCE_COMPANY_LABEL,
        'count': len(names), 'expected_count': EXPECTED_COUNT,
        'complete': not incomplete, 'incomplete_slices': incomplete,
        'duplicate_codes': sorted(code for code, count in counts.items() if count > 1),
        'name_conflict_codes': conflicts,
        'would_insert': len(names.keys() - current.keys()),
        'voucher_accounts': voucher_comparison(
            [{'account_code': r['code'], 'account_name': r['name']} for r in rows], accounts),
        'can_apply': not incomplete and len(names) == EXPECTED_COUNT and not conflicts,
    }


async def import_accounts(conn, reader, *, tenant_id: UUID, company_id: str, apply: bool, accounts: dict) -> dict:
    # 기존 회사 연결표의 이름으로 미아점 ID임을 확인한다. 다른 회사를 미아점으로 표시하지 않는다.
    async with conn.transaction(readonly=True):
        if not await conn.fetchval(SOURCE_SQL, company_id, SOURCE_COMPANY_LABEL):
            raise ComparisonError('mia_company_identity_unverified')
    rows, incomplete = await reader.search_accounts(company_id)
    async with conn.transaction(readonly=not apply):
        if apply:
            await conn.execute("SET LOCAL lock_timeout = '5s'")
            # 적재 중 동시 쓰기를 막고 이름 충돌 검사와 INSERT를 한 트랜잭션으로 묶는다.
            await conn.execute('LOCK TABLE obys_account_master IN SHARE ROW EXCLUSIVE MODE')
        table_exists = await conn.fetchval("SELECT to_regclass('public.obys_account_master') IS NOT NULL")
        existing = list(await conn.fetch(EXISTING_SQL, tenant_id)) if table_exists else []
        report = make_plan(rows, existing, incomplete, accounts)
        report.update(mode='apply' if apply else 'dry-run', table_exists=table_exists, inserted=0)
        report['duplicate_codes'] = sorted(set(report['duplicate_codes']) | reader.duplicate_codes)
        if not apply:
            return report
        if not report['can_apply']:
            raise ComparisonError('account_import_validation_failed')
        for row in normalize(rows, 'accounts'):
            inserted = await conn.fetchval(INSERT_SQL, tenant_id, row['code'], row['name'], SOURCE_COMPANY_LABEL)
            report['inserted'] += int(inserted is not None)
        return report


async def run(args) -> int:
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    client = conn = None
    try:
        if not os.getenv('OBYS_DATABASE_URL') or not os.getenv('OBYS_VAULT_KEY'):
            raise ComparisonError('acct_environment_missing')
        import asyncpg
        from app.core.obys_db import obys_db_url
        from app.core import credential_vault
        from app.services import clobe_mcp_client as client
        from app.services.obys_clobe_voucher import ACCOUNTS

        credential_vault.configure_vault_key(os.environ['OBYS_VAULT_KEY'].strip().encode())
        conn = await asyncpg.connect(obys_db_url(), timeout=5)
        report = await import_accounts(conn, AccountReader(client), tenant_id=args.tenant_id,
                                       company_id=args.company_id, apply=args.apply, accounts=ACCOUNTS)
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report['can_apply'] else 3
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ComparisonError) else 'account_import_unavailable'
        print(json.dumps({'status': 'unavailable', 'reason': reason}), file=sys.stderr)
        return 2
    finally:
        try:
            if conn is not None:
                await conn.close()
            if client is not None:
                await client.close_store_pool()
        finally:
            logging.disable(previous)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tenant-id', type=UUID, required=True)
    parser.add_argument('--company-id', required=True, help='클로브 미아점 회사 ID')
    parser.add_argument('--apply', action='store_true')
    return asyncio.run(run(parser.parse_args()))


if __name__ == '__main__':
    raise SystemExit(main())
