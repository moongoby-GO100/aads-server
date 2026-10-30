#!/usr/bin/env python3
"""릴리스 마이그레이션 승인 초안과 읽기 전용 검증. 승인 자체는 하지 않는다."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen


class Rejected(ValueError):
    pass


def digest(value):
    return hashlib.sha256(value).hexdigest()


def manifest_hash(items):
    return digest(''.join(f"{x['file']}|{x['sha256']}\n" for x in items).encode())


def valid_manifest(items):
    if not isinstance(items, list):
        raise Rejected('manifest_mismatch')
    names = []
    for x in items:
        if (not isinstance(x, dict) or set(x) != {'file', 'sha256'}
                or not isinstance(x['file'], str)
                or not re.fullmatch(r'migrations/[A-Za-z0-9_.-]+\.sql', x['file'])
                or not isinstance(x['sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', x['sha256'])):
            raise Rejected('manifest_mismatch')
        names.append(x['file'])
    if names != sorted(set(names)):
        raise Rejected('manifest_mismatch')
    return items


def literal(s):
    return "'" + s.replace("'", "''") + "'"


def lookup(release_sha):
    # One SELECT, one statement snapshot; no approval files or environment decisions.
    query = f"""SELECT json_build_object(
      'tenant_id', t.id::text, 'target_db', current_database(),
      'document', (SELECT row_to_json(d) FROM (
        SELECT h.approved_revision_id, h.latest_revision_id, r.id AS revision_id,
          r.content, r.content_hash, e.action, e.actor_id,
          EXISTS(SELECT 1 FROM tenant_memberships m JOIN saas_users u ON u.id=m.user_id
            WHERE m.tenant_id=t.id AND m.user_id=e.actor_id AND m.role='owner'
            AND m.status='active' AND m.deleted_at IS NULL
            AND u.status='active' AND u.deleted_at IS NULL) AS owner
        FROM project_document_heads h
        JOIN project_document_revisions r ON r.id=h.approved_revision_id AND r.head_id=h.id
        LEFT JOIN LATERAL (SELECT * FROM project_document_events ev WHERE ev.head_id=h.id
          AND ev.tenant_id=h.tenant_id AND ev.project_key=h.project_key
          AND ev.action IN ('created','revised','review','approved','archived')
          ORDER BY ev.id DESC LIMIT 1) e ON true
        WHERE h.tenant_id=t.id AND h.project_key='AADS' AND h.kind='contract'
          AND h.document_key={literal('migration-apply-' + release_sha[:12])}
      ) d),
      'ledger', COALESCE((SELECT json_object_agg(filename,sha256) FROM schema_migrations),'{{}}'::json))
      FROM tenants t WHERE t.slug='internal' AND t.status='active' AND t.deleted_at IS NULL;"""
    result = subprocess.run(
        ['timeout', '60', 'docker', 'exec', '-e',
         'PGOPTIONS=-c default_transaction_read_only=on -c statement_timeout=10000',
         os.environ.get('PG_CONTAINER', 'aads-postgres'), 'psql', '-X', '-U',
         os.environ.get('PGUSER', 'aads'), '-d', os.environ.get('PGDATABASE', 'aads'),
         '-qAt', '-v', 'ON_ERROR_STOP=1', '-c', query],
        capture_output=True, text=True, timeout=65, check=True)
    return json.loads(result.stdout)


def inventory(root, ledger):
    baseline = root / 'scripts/migrations_auto_apply_baseline.txt'
    held = {line.split()[0] for line in baseline.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith('#')} if baseline.exists() else set()
    items = []
    for path in sorted((root / 'migrations').glob('*.sql')):
        name = 'migrations/' + path.name
        if path.name.endswith('.verify.sql') or 'rollback' in path.name:
            continue
        sha = digest(path.read_bytes())
        recorded = ledger.get(name, ledger.get(path.name))
        if recorded is not None:
            continue
        if name not in held:
            items.append({'file': name, 'sha256': sha})
    return items


def verify(root, release_sha, supplied, state, now=None):
    if not re.fullmatch('[0-9a-f]{40}', release_sha):
        raise Rejected('release_mismatch')
    d = state.get('document')
    if not d or not d.get('approved_revision_id') or not (
        d['approved_revision_id'] == d['latest_revision_id'] == d['revision_id']
    ) or d.get('action') != 'approved':
        raise Rejected('not_approved')
    if digest(d['content'].encode()) != d['content_hash']:
        raise Rejected('hash_mismatch')
    blocks = re.findall(r'^```aads-migration-approval\s*\n(.*?)^```\s*$', d['content'], re.M | re.S)
    if len(blocks) != 1:
        raise Rejected('invalid_document')
    try:
        data = json.loads(blocks[0])
    except (ValueError, TypeError):
        raise Rejected('invalid_document')
    if not isinstance(data, dict):
        raise Rejected('invalid_document')
    if data.get('release_sha') != release_sha:
        raise Rejected('release_mismatch')
    for k, v in {'purpose': 'migration_apply', 'project': 'AADS',
                 'tenant_id': state['tenant_id'], 'target_db': state['target_db']}.items():
        if data.get(k) != v:
            raise Rejected('scope_mismatch')
    approved = valid_manifest(data.get('manifest'))
    if data.get('manifest_sha256') != manifest_hash(approved):
        raise Rejected('manifest_mismatch')
    try:
        expires = datetime.fromisoformat(data['expires_at'].replace('Z', '+00:00'))
        if expires.tzinfo is None or expires <= (now or datetime.now(timezone.utc)):
            raise Rejected('expired')
    except (KeyError, TypeError, ValueError) as exc:
        raise Rejected('expired') from exc
    actor = d.get('actor_id', '')
    # The sole approved-event writer authenticates a human session before INSERT.
    # Require its real-user UUID, then the live tenant owner/user join above.
    if not isinstance(actor, str) or not re.fullmatch(
            r'[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}', actor):
        raise Rejected('approver_not_human')
    if d.get('owner') is not True:
        raise Rejected('approver_not_owner')
    pending = inventory(root, state['ledger'])
    if valid_manifest(supplied) != pending:
        raise Rejected('manifest_mismatch')
    by_name = {x['file']: x['sha256'] for x in approved}
    if any(by_name.get(x['file']) != x['sha256'] for x in pending):
        raise Rejected('manifest_mismatch')
    for name, sha in by_name.items():
        path = root / name
        if not path.is_file() or digest(path.read_bytes()) != sha:
            raise Rejected('manifest_mismatch')
        if name not in {x['file'] for x in pending} and state['ledger'].get(name, state['ledger'].get(path.name)) != sha:
            raise Rejected('manifest_mismatch')
    return f"APPROVED revision_id={d['revision_id']} approver={actor} manifest_sha256={data['manifest_sha256']}"


def register(content, sha):
    # Caller supplies a normal existing API login credential; never an approval override.
    base = os.environ['AADS_API_URL'].rstrip('/') + '/api/v1/projects/AADS/documents'
    def post(url, body):
        req = Request(url, json.dumps(body).encode(), headers={
            'Content-Type': 'application/json', 'Authorization': 'Bearer ' + os.environ['AADS_ACCESS_TOKEN']})
        with urlopen(req, timeout=30) as response:
            return json.load(response)
    key = 'migration-apply-' + sha[:12]
    result = post(base, dict(document_key=key, kind='contract', title='릴리스 마이그레이션 적용 승인',
        version='1.0.0', content=content, expected_generation=0,
        source_task_id='runner-fffc8833', change_summary='정확한 릴리스 SQL 명세의 사람 승인 검토 요청'))
    return post(base + '/' + key + '/review', dict(revision_id=result['revision_id'], expected_generation=result['generation']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['draft', 'verify'])
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--release-sha')
    parser.add_argument('--manifest-file', type=Path)
    parser.add_argument('--register', action='store_true')
    args = parser.parse_args()
    try:
        sha = args.release_sha or os.environ.get('AADS_RELEASE_SHA') or subprocess.check_output(
            ['git', '-C', str(args.root), 'rev-parse', 'HEAD'], text=True).strip()
        if not re.fullmatch('[0-9a-f]{40}', sha):
            raise Rejected('release_mismatch')
        state = lookup(sha)
        if args.command == 'verify':
            supplied = [{'file': line.split('|')[0], 'sha256': line.split('|')[1]}
                        for line in args.manifest_file.read_text().splitlines() if line]
            print(verify(args.root, sha, supplied, state))
        else:
            items = inventory(args.root, state['ledger'])
            data = dict(purpose='migration_apply', project='AADS', tenant_id=state['tenant_id'],
                target_db=state['target_db'], release_sha=sha, manifest=items,
                manifest_sha256=manifest_hash(items), expires_at=(datetime.now(timezone.utc)+timedelta(hours=24)).isoformat())
            content = '# 릴리스 마이그레이션 적용 승인\n\nAPI 롤백은 적용된 SQL을 되돌리지 않음\n\n'
            for item in items:
                comments = [s.strip('- ').strip() for s in (args.root / item['file']).read_text().splitlines() if s.startswith('--')]
                content += f"- {item['file']}: " + (' '.join(comments[:3]) or 'SQL 본문을 직접 검토하세요') + '\n'
            content += '\n```aads-migration-approval\n' + json.dumps(data, ensure_ascii=False, indent=2) + '\n```\n'
            print(content)
            if args.register:
                print(json.dumps(register(content, sha), ensure_ascii=False))
        return 0
    except Rejected as exc:
        print(str(exc))
    except Exception:
        print('lookup_failed')
    return 1


if __name__ == '__main__':
    sys.exit(main())
