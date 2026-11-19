#!/usr/bin/env python3
"""클로브 두 회사 계정·라벨 비교 (읽기 전용).

ACCT 런타임의 기존 CLOBE_STORE_DATABASE_URL 또는 OBYS_DATABASE_URL 및
OBYS_VAULT_KEY 환경으로 실행한다. 자격증명을 인자로 전달하지 않는다.
python3 scripts/clobe_compare_companies.py --base '미아점' --target '윤희에프엔비'
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import re
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TOOLS = {'get_my_context', 'search_ledger_accounts', 'get_labels'}


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


def normalize(rows: list, kind: str) -> list[dict[str, str]]:
    out = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ComparisonError('unsupported_record_shape')
        code_keys = ('accountCode', 'ledgerAccountCode', 'code') if kind == 'accounts' else ('labelCode', 'code')
        name_keys = ('accountName', 'ledgerAccountName', 'name') if kind == 'accounts' else ('labelName', 'name')
        code = next((row[k] for k in code_keys if row.get(k) is not None), '')
        name = next((row[k] for k in name_keys if row.get(k) is not None), '')
        if not name or (kind == 'accounts' and code == ''):
            raise ComparisonError('missing_record_code_or_name')
        out.add((str(code).strip(), str(name).strip()))
    return [{'code': c, 'name': n} for c, n in sorted(out)]


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

    async def rows(self, name: str, company_id: str, kind: str) -> list[dict]:
        schema = input_schema(await self.client.get_tool_input_schema(name))
        props = schema.get('properties', {})
        company_key = next((k for k in ('companyId', 'company_id') if k in props), None)
        if not company_key:
            raise ComparisonError('company_input_schema_missing')
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
        size_key = next((k for k in ('size', 'pageSize', 'limit') if k in props), None)
        if size_key:
            args[size_key] = min(100, props[size_key].get('maximum', 100))
        page_key = 'page' if 'page' in props else None
        if page_key:
            args[page_key] = props[page_key].get('default', props[page_key].get('minimum', 0))
        if 'cursor' in schema.get('required', []):
            args['cursor'] = None
        keys = ('ledgerAccounts', 'accounts') if kind == 'accounts' else ('labels',)
        rows, seen = [], set()
        for _ in range(1000):
            payload = await self.call(name, args)
            chunk, meta = collection(payload, keys)
            fingerprint = json.dumps(chunk, sort_keys=True, ensure_ascii=False)
            if chunk and fingerprint in seen:
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
            if total is not None and len(rows) < int(total):
                more = True
            if meta.get('totalPages') is not None and page_key:
                first = props[page_key].get('minimum', 0)
                more = more or args[page_key] - first + 1 < int(meta['totalPages'])
            if more:
                if not chunk or not page_key:
                    raise ComparisonError('incomplete_pagination')
                args[page_key] += 1
                continue
            if more is False or (total is not None and len(rows) >= int(total)):
                return normalize(rows, kind)
            if size_key and len(chunk) >= args[size_key]:
                if not page_key:
                    raise ComparisonError('pagination_completion_unknown')
                args[page_key] += 1
                continue
            return normalize(rows, kind)
        raise ComparisonError('page_limit_exceeded')


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
    for kind, tool in (('accounts', 'search_ledger_accounts'), ('labels', 'get_labels')):
        left = await reader.rows(tool, base, kind)
        right = await reader.rows(tool, target, kind)
        result[kind] = {'base_count': len(left), 'target_count': len(right), **differences(left, right)}
    return result


def render(report: dict) -> str:
    lines = [json.dumps(report, ensure_ascii=False, indent=2), '',
             '| 구분 | 기준 건수 | 대상 건수 | 기준에만 있음 | 대상에만 있음 | 이름 불일치 |',
             '| --- | ---: | ---: | ---: | ---: | ---: |']
    for key, title in (('accounts', '계정과목'), ('labels', '라벨')):
        item = report[key]
        lines.append(f"| {title} | {item['base_count']} | {item['target_count']} | "
                     f"{len(item['base_only'])} | {len(item['target_only'])} | {len(item['name_mismatch'])} |")
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
        return 0
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
