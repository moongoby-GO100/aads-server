#!/usr/bin/env python3
"""클로브 두 회사 계정·라벨 비교 (읽기 전용).

ACCT 런타임의 기존 CLOBE_STORE_DATABASE_URL 또는 OBYS_DATABASE_URL 및
OBYS_VAULT_KEY 환경으로 실행한다. 자격증명을 인자로 전달하지 않는다.
python3 scripts/clobe_compare_companies.py --base '미아점' --target '윤희에프엔비'
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TOOLS = {'get_my_context', 'search_ledger_accounts', 'get_labels', 'get_trial_balance'}
LABEL_KEYS = ('categoryLabels', 'groupLabels')
ENTITY_KEY = 'businessEntities'
ENTITY_NAME_KEYS = ('businessEntityName', 'entityName', 'tradeName', 'companyName', 'name')
SPLIT_DIGITS = '1234567890'
MAX_PREFIX_DEPTH = 6
MAX_SEARCH_CALLS = 200


class ComparisonError(RuntimeError):
    """오류 메시지는 고정 코드만 사용한다(응답·DSN·토큰 비노출)."""


def safe_text(value: Any) -> str:
    text = str(value).replace('\n', ' ').replace('\r', ' ').replace('\t', ' ').replace('|', '/')
    text = re.sub(r'(?i)(?:bearer\s+\S+|(?:access_token|refresh_token|secret|password)\s*[=:]\s*\S+|sk-[A-Za-z0-9_-]+)', '[비공개]', text)
    text = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[비공개]', text)
    text = re.sub(r'(?<!\d)\d[\d -]{6,}\d(?!\d)', '[비공개]', text)
    return text


def input_schema(schema: dict) -> dict:
    props = schema.get('properties', {})
    return props['input'] if 'input' in props else schema


def collection(payload: dict, keys: tuple[str, ...]) -> tuple[list, dict]:
    for key in (*keys, 'items', 'content', 'results'):
        if isinstance(payload.get(key), list):
            return payload[key], payload
    data = payload.get('data')
    if isinstance(data, dict):
        rows, meta = collection(data, keys)
        return rows, {**payload, **meta}
    if isinstance(data, list):
        return data, payload
    raise ComparisonError('unsupported_collection_shape')


def split_labels(payload: dict, keys: tuple[str, ...]) -> tuple[list, dict, list]:
    """get_labels 응답: categoryLabels/groupLabels 는 라벨, businessEntities 는 거래처."""
    if any(isinstance(payload.get(k), list) for k in (*LABEL_KEYS, ENTITY_KEY)):
        labels = [r for k in LABEL_KEYS for r in (payload.get(k) or [])]
        entities = list(payload.get(ENTITY_KEY) or [])
        return labels, payload, entities
    chunk, meta = collection(payload, keys)
    return chunk, meta, []


def normalize(rows: list, kind: str) -> list[dict[str, str]]:
    out = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ComparisonError('unsupported_record_shape')
        code_keys = ('accountCode', 'ledgerAccountCode', 'code') if kind == 'accounts' else ('labelCode', 'code')
        name_keys = (('accountName', 'ledgerAccountName', 'name') if kind == 'accounts'
                     else ('labelName', 'name'))
        code = next((row[k] for k in code_keys if row.get(k) is not None), '')
        name = next((row[k] for k in name_keys if row.get(k) is not None), '')
        if not name and kind == 'labels':
            # categoryLabels 는 이름 대신 category1~3 계층을 가진다(categoryId 는 회사별 내부 ID라 제외).
            name = ' > '.join(str(row[k]).strip() for k in ('category1', 'category2', 'category3') if row.get(k))
            code = ''
        if not name or (kind == 'accounts' and code == ''):
            raise ComparisonError('missing_record_code_or_name')
        out.add((str(code).strip(), str(name).strip()))
    return [{'code': c, 'name': n} for c, n in sorted(out)]


def entity_fingerprints(rows: list, salt: bytes) -> list[str]:
    """거래처 이름은 개인 이름일 수 있어 원문을 내보내지 않는다. 실행마다 다른 솔트의 HMAC 앞 8자리만 쓴다."""
    out = []
    for row in rows:
        if not isinstance(row, dict):
            raise ComparisonError('unsupported_record_shape')
        name = next((str(row[k]).strip() for k in ENTITY_NAME_KEYS if row.get(k)), '')
        out.append(hmac.new(salt, name.encode(), hashlib.sha256).hexdigest()[:8] if name else '(이름없음)')
    return sorted(out)


def entity_differences(base: list[str], target: list[str]) -> dict:
    left, right = set(base), set(target)
    return {'base_count': len(base), 'target_count': len(target),
            'base_only_count': len(left - right), 'target_only_count': len(right - left),
            'base_only_fingerprints': sorted(left - right), 'target_only_fingerprints': sorted(right - left),
            'complete': True}


def differences(base: list[dict], target: list[dict]) -> dict:
    # 코드 없는 라벨은 이름으로 비교하며 회사별 내부 ID는 비교하지 않는다.
    def index(rows):
        result = {}
        for row in rows:
            key = ('code', row['code']) if row['code'] else ('name', row['name'])
            result.setdefault(key, set()).add(row['name'])
        return result

    left, right = index(base), index(target)

    def records(keys, source):
        return [{'code': safe_text(k[1]) if k[0] == 'code' else '', 'name': safe_text(n)}
                for k in sorted(keys) for n in sorted(source[k])]

    return {
        'base_only': records(left.keys() - right.keys(), left),
        'target_only': records(right.keys() - left.keys(), right),
        'name_mismatch': [
            {'code': safe_text(k[1]), 'base_names': [safe_text(n) for n in sorted(left[k])],
             'target_names': [safe_text(n) for n in sorted(right[k])]}
            for k in sorted(left.keys() & right.keys()) if left[k] != right[k]
        ],
    }


class Reader:
    def __init__(self, client):
        self.client = client

    async def call(self, name: str, args: dict) -> dict:
        if name not in TOOLS or not name.startswith(('get_', 'search_', 'summarize_')):
            raise ComparisonError('read_tool_required')
        # call_tool checks both the read verb and DB allowed=TRUE before MCP.
        result = await self.client.call_tool(name, {'input': dict(args)})
        return self.client.parse_tool_json(result)

    @staticmethod
    def required_args(schema: dict, company_key: str, company_id: str) -> dict:
        props = schema.get('properties', {})
        args = {company_key: company_id}
        for key in schema.get('required', []):
            if key in args:
                continue
            spec = props.get(key, {})
            if 'default' in spec:
                args[key] = spec['default']
            elif key in ('query', 'search', 'keyword'):
                args[key] = ''
            elif key not in ('page', 'cursor', 'size', 'pageSize', 'limit'):
                raise ComparisonError('unsupported_required_input')
        return args

    async def rows(self, name: str, company_id: str, kind: str) -> list[dict]:
        schema = input_schema(await self.client.get_tool_input_schema(name))
        props = schema.get('properties', {})
        company_key = next((k for k in ('companyId', 'company_id') if k in props), None)
        if not company_key:
            raise ComparisonError('company_input_schema_missing')
        args = self.required_args(schema, company_key, company_id)
        size_key = next((k for k in ('size', 'pageSize', 'limit') if k in props), None)
        if size_key:
            args[size_key] = min(100, props[size_key].get('maximum', 100))
        page_key = 'page' if 'page' in props else None
        if page_key:
            args[page_key] = props[page_key].get('default', props[page_key].get('minimum', 0))
        if 'cursor' in schema.get('required', []):
            args['cursor'] = None
        keys = ('items', 'ledgerAccounts', 'accounts') if kind == 'accounts' else ('labels',)
        rows, entities, seen = [], [], set()
        for _ in range(1000):
            payload = await self.call(name, args)
            if kind == 'labels':
                chunk, meta, ents = split_labels(payload, keys)
                entities.extend(ents)
            else:
                chunk, meta = collection(payload, keys)
                ents = []
            page_rows = [*chunk, *(ents if kind == 'labels' else [])]
            fingerprint = json.dumps(page_rows, sort_keys=True, ensure_ascii=False)
            if page_rows and fingerprint in seen:
                raise ComparisonError('repeated_page')
            seen.add(fingerprint)
            rows.extend(chunk)
            paging = meta.get('pagination') or meta.get('pageInfo') or {}
            meta = {**meta, **paging}
            cursor = meta.get('nextCursor')
            total = meta.get('totalElements', meta.get('totalCount', meta.get('total')))
            more = meta.get('hasNext', meta.get('hasMore'))
            if cursor:
                if 'cursor' not in props or args.get('cursor') == cursor:
                    raise ComparisonError('unsupported_cursor')
                args['cursor'] = cursor
                continue
            if total is not None and len(rows) + len(entities) < int(total):
                more = True
            if meta.get('totalPages') is not None and page_key:
                first = props[page_key].get('minimum', 0)
                more = more or args[page_key] - first + 1 < int(meta['totalPages'])
            if more:
                if not (chunk or ents) or not page_key:
                    raise ComparisonError('incomplete_pagination')
                args[page_key] += 1
                continue
            if more is False or (total is not None and len(rows) + len(entities) >= int(total)):
                return self._finish(rows, entities, kind)
            if size_key and len(chunk) >= args[size_key]:
                if not page_key:
                    raise ComparisonError('pagination_completion_unknown')
                args[page_key] += 1
                continue
            return self._finish(rows, entities, kind)
        raise ComparisonError('page_limit_exceeded')

    def _finish(self, rows: list, entities: list, kind: str) -> list[dict]:
        # 거래처(businessEntities)는 라벨과 섞지 않고 따로 보관한다.
        self.entities = list(entities)
        return normalize(rows, kind)

    async def search_accounts(self, company_id: str) -> tuple[list[dict], list[str]]:
        """search_ledger_accounts 는 page/cursor 가 없어 50건에서 잘린다(hasMore).

        query 를 계정 코드 접두로 쪼개 내려가며 모으고, 끝까지 잘리는 조각은
        incomplete 목록(조각 query, 코드 접두라 비민감)에 남긴다.
        """
        schema = input_schema(await self.client.get_tool_input_schema('search_ledger_accounts'))
        props = schema.get('properties', {})
        company_key = next((k for k in ('companyId', 'company_id') if k in props), None)
        if not company_key or 'query' not in props:
            raise ComparisonError('company_input_schema_missing')
        size_key = next((k for k in ('size', 'pageSize', 'limit') if k in props), None)
        rows: list = []
        incomplete: list[str] = []
        calls = 0

        async def fetch(query: str) -> tuple[list, bool]:
            nonlocal calls
            calls += 1
            args = {company_key: company_id, 'query': query}
            if size_key:
                args[size_key] = min(100, props[size_key].get('maximum', 100))
            payload = await self.call('search_ledger_accounts', args)
            chunk, meta = collection(payload, ('items', 'ledgerAccounts', 'accounts'))
            paging = {**meta, **(meta.get('pagination') or meta.get('pageInfo') or {})}
            more = paging.get('hasMore', paging.get('hasNext'))
            total = paging.get('totalElements', paging.get('totalCount', paging.get('total')))
            truncated = bool(more) or (total is not None and len(chunk) < int(total))
            if more is None and total is None and size_key and len(chunk) >= args[size_key]:
                truncated = True
            return chunk, truncated

        def key(r) -> str:
            return json.dumps(r, sort_keys=True, ensure_ascii=False)

        def has_exact(query: str, chunk: list) -> bool:
            return any(isinstance(r, dict) and str(next((r[k] for k in ('accountCode', 'ledgerAccountCode', 'code')
                                                         if r.get(k) is not None), '')).strip() == query
                       for r in chunk)

        async def walk(query: str) -> bool:
            """조각이 완전하면 True. 잘린 조각은 모든 자식 접두(0~9)가 완전하고,
            부모 코드 자체와 일치하는 단독 계정도 확보됐을 때만 완전으로 본다."""
            chunk, truncated = await fetch(query)
            rows.extend(chunk)
            if not truncated:
                return True
            if len(query) >= MAX_PREFIX_DEPTH or calls + len(SPLIT_DIGITS) > MAX_SEARCH_CALLS:
                incomplete.append(query or '(전체)')
                return False
            before = {key(r) for r in rows}
            children_ok = True
            for digit in SPLIT_DIGITS:
                children_ok = await walk(query + digit) and children_ok
            if {key(r) for r in rows} <= before:
                # 접두 검색이 아무것도 더하지 못함 → query 가 코드 접두로 동작하지 않는다.
                incomplete.append(query or '(전체)')
                return False
            if query and not has_exact(query, chunk):
                # 자식 접두 검색으로는 코드가 query 와 정확히 같은 계정을 얻을 수 없다.
                incomplete.append(query)
                return False
            return children_ok

        await walk('')
        return normalize(rows, 'accounts'), sorted(set(incomplete))

    async def trial_balance_accounts(self, company_id: str) -> tuple[list[dict], bool]:
        """시산표의 계정행(accountCode 있는 행)으로 계정과목을 보완한다. (행, 잘림 여부)"""
        schema = input_schema(await self.client.get_tool_input_schema('get_trial_balance'))
        if 'companyId' not in schema.get('properties', {}):
            raise ComparisonError('company_input_schema_missing')
        args = self.required_args(schema, 'companyId', company_id)
        payload = await self.call('get_trial_balance', args)
        rows = [r for r in payload.get('rows', []) if isinstance(r, dict) and r.get('accountCode') and r.get('name')]
        return normalize(rows, 'accounts'), bool(payload.get('truncated'))

    async def optional_trial_balance(self, company_id: str) -> tuple[list[dict], dict]:
        """선택적 보완: 어떤 실패도 비교를 막지 않고 고정 사유 코드만 남긴다."""
        try:
            rows, cut = await self.trial_balance_accounts(company_id)
            return rows, {'status': 'ok', 'rows': len(rows), 'truncated': cut}
        except ComparisonError as exc:
            return [], {'status': 'skipped', 'reason': str(exc)}
        except Exception:
            return [], {'status': 'skipped', 'reason': 'trial_balance_unavailable'}


async def compare(client, base_name: str, target_name: str) -> dict:
    reader = Reader(client)
    companies = client.parse_companies(await reader.call('get_my_context', {}))

    def resolve(name):
        matches = [c for c in companies if c['name'] == name.strip()]
        if len(matches) != 1:
            raise ComparisonError('company_name_missing_or_ambiguous')
        return matches[0]['company_id']

    base, target = resolve(base_name), resolve(target_name)
    if base == target:
        raise ComparisonError('companies_must_differ')
    result = {'base_id_prefix': base[:4], 'target_id_prefix': target[:4]}
    left_acc, left_inc = await reader.search_accounts(base)
    right_acc, right_inc = await reader.search_accounts(target)
    left_tb, left_tb_info = await reader.optional_trial_balance(base)
    right_tb, right_tb_info = await reader.optional_trial_balance(target)
    # 시산표 계정행을 합쳐 검색 누락분을 보완한다(코드 기준 중복 제거).
    left = merge_accounts(left_acc, left_tb)
    right = merge_accounts(right_acc, right_tb)
    result['accounts'] = {
        'base_count': len(left), 'target_count': len(right), **differences(left, right),
        'complete': not (left_inc or right_inc),
        'incomplete_slices': {'base': left_inc, 'target': right_inc},
        'trial_balance_supplement': {'base': left_tb_info, 'target': right_tb_info},
    }
    left_labels = await reader.rows('get_labels', base, 'labels')
    left_ent = reader.entities
    right_labels = await reader.rows('get_labels', target, 'labels')
    right_ent = reader.entities
    result['labels'] = {'base_count': len(left_labels), 'target_count': len(right_labels),
                        **differences(left_labels, right_labels), 'complete': True}
    salt = os.urandom(16)
    result['vendors'] = entity_differences(entity_fingerprints(left_ent, salt), entity_fingerprints(right_ent, salt))
    result['complete'] = result['accounts']['complete']
    return result


def merge_accounts(primary: list[dict], extra: list[dict]) -> list[dict]:
    known = {r['code'] for r in primary}
    return sorted([*primary, *[r for r in extra if r['code'] not in known]], key=lambda r: (r['code'], r['name']))


def render(report: dict) -> str:
    lines = []
    if not report.get('complete', True):
        lines += ['## 경고: 불완전한 비교 — 계정과목 일부 조각이 50건에서 잘렸다. 아래 결과는 전량이 아니다.',
                  f"- 잘린 조각(기준): {report['accounts']['incomplete_slices']['base']}",
                  f"- 잘린 조각(대상): {report['accounts']['incomplete_slices']['target']}", '']
    lines += [json.dumps(report, ensure_ascii=False, indent=2), '',
             '| 구분 | 기준 건수 | 대상 건수 | 기준에만 있음 | 대상에만 있음 | 이름 불일치 |',
             '| --- | ---: | ---: | ---: | ---: | ---: |']
    for key, title in (('accounts', '계정과목'), ('labels', '라벨')):
        item = report[key]
        lines.append(f"| {title} | {item['base_count']} | {item['target_count']} | "
                     f"{len(item['base_only'])} | {len(item['target_only'])} | {len(item['name_mismatch'])} |")
    vendors = report['vendors']
    lines.insert(lines.index('| --- | ---: | ---: | ---: | ---: | ---: |') + 1 + 2,
                 f"| 거래처(이름 비공개) | {vendors['base_count']} | {vendors['target_count']} | "
                 f"{vendors['base_only_count']} | {vendors['target_only_count']} | - |")
    for key, title in (('accounts', '계정과목'), ('labels', '라벨')):
        item = report[key]
        lines.extend(['', title, '', '| 차이 | 코드 | 기준 이름 | 대상 이름 |', '| --- | --- | --- | --- |'])
        for field, label in (('base_only', '기준에만 있음'), ('target_only', '대상에만 있음')):
            for row in item[field]:
                left, right = (row['name'], '') if field == 'base_only' else ('', row['name'])
                lines.append(f"| {label} | {row['code']} | {left} | {right} |")
        for row in item['name_mismatch']:
            lines.append(f"| 이름 불일치 | {row['code']} | {', '.join(row['base_names'])} | {', '.join(row['target_names'])} |")
    return '\n'.join(lines)


async def run(base: str, target: str) -> int:
    # Suppress third party exception/log payloads; only fixed error codes go to stderr.
    previous_logging_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    client = None
    try:
        if not (os.getenv('CLOBE_STORE_DATABASE_URL') or os.getenv('OBYS_DATABASE_URL')):
            raise ComparisonError('acct_store_environment_missing')
        if not os.getenv('OBYS_VAULT_KEY'):
            raise ComparisonError('acct_vault_environment_missing')
        from app.core import credential_vault
        from app.services import clobe_mcp_client as client

        credential_vault.configure_vault_key(os.environ['OBYS_VAULT_KEY'].strip().encode())
        # Standalone vault selects the existing ACCT business pool, never the stale AADS pool.
        report = await compare(client, base, target)
        print(render(report))
        return 0 if report['complete'] else 3
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ComparisonError) else 'acct_comparison_unavailable'
        print(json.dumps({'status': 'unavailable', 'reason': reason}), file=sys.stderr)
        return 2
    finally:
        if client is not None:
            try:
                await client.close_store_pool()
            except Exception:
                pass
        logging.disable(previous_logging_disable)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', required=True)
    parser.add_argument('--target', required=True)
    args = parser.parse_args()
    return asyncio.run(run(args.base, args.target))


if __name__ == '__main__':
    sys.exit(main())
