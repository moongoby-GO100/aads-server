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


# Root migrations are AADS unless explicitly assigned to the business release.
# Business inventory is pending until its own DB ledger/probe proves application;
# the AADS legacy baseline must never hide it.
MIGRATION_TARGETS = {
    'migrations/20261030_obys_work_schedules.sql': 'acct_business',
    'migrations/20261102_obys_attendance_corrections.sql': 'acct_business',
}
VALID_TARGETS = {'aads', 'acct_business'}


def migration_target(name):
    target = MIGRATION_TARGETS.get(name, 'aads')
    if target not in VALID_TARGETS:
        raise Rejected('unknown_migration_target')
    return target


def header_comments(text):
    """파일 맨 앞의 연속된 `--` 주석 블록. 빈 주석 줄은 ''(문단 구분)로 남긴다."""
    lines = []
    for line in text.splitlines():
        if not line.startswith('--'):
            break
        lines.append(line[2:].strip())
    while lines and not lines[-1]:
        lines.pop()
    return lines


_IDENT = r'((?:"[^"]+"|[A-Za-z_][\w$]*)(?:\.(?:"[^"]+"|[A-Za-z_][\w$]*))?)'


def sql_objects(text):
    """SQL 본문(주석 제외)에서 만드는 열·표·함수·트리거를 뽑는다."""
    body = '\n'.join(x for x in text.splitlines() if not x.lstrip().startswith('--'))
    found = []
    for m in re.finditer(r'ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?' + _IDENT + r'([^;]*)', body, re.I):
        for c in re.finditer(r'ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?' + _IDENT, m.group(2), re.I):
            found.append(f'열 {m.group(1)}.{c.group(1)}')
    for m in re.finditer(r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?' + _IDENT, body, re.I):
        found.append(f'표 {m.group(1)}')
    for m in re.finditer(r'CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+' + _IDENT, body, re.I):
        found.append(f'함수 {m.group(1)}')
    for m in re.finditer(r'CREATE\s+(?:OR\s+REPLACE\s+)?(?:CONSTRAINT\s+)?TRIGGER\s+' + _IDENT, body, re.I):
        found.append(f'트리거 {m.group(1)}')
    return list(dict.fromkeys(found))


def describe_migration(item, text):
    out = f"- {item['file']}:\n"
    head = header_comments(text)
    if any(head):
        out += ''.join(('  ' + x if x else '') + '\n' for x in head)
    else:
        out += '  SQL 본문을 직접 검토하세요\n'
    objs = sql_objects(text)
    if objs:
        out += '  이 파일이 만드는 객체:\n' + ''.join(f'  - {o}\n' for o in objs)
    return out


def lookup(release_sha, root=None):
    # One SELECT, one statement snapshot; no approval files or environment decisions.
    query = f"""SELECT json_build_object(
      'tenant_id', t.id::text, 'target_db', current_database(),
      'documents', (SELECT json_agg(d) FROM (
        SELECT h.document_key, h.approved_revision_id, h.latest_revision_id, r.id AS revision_id,
          r.content, r.content_hash, e.action, e.actor_id,
          EXISTS(SELECT 1 FROM tenant_memberships m JOIN saas_users u ON u.id=m.user_id
            WHERE m.tenant_id=t.id AND m.user_id=e.actor_id AND m.role='owner'
            AND m.status='active' AND m.deleted_at IS NULL
            AND u.status='active' AND u.deleted_at IS NULL) AS owner
        FROM project_document_heads h
        LEFT JOIN project_document_revisions r ON r.id=h.approved_revision_id AND r.head_id=h.id
        LEFT JOIN LATERAL (SELECT * FROM project_document_events ev WHERE ev.head_id=h.id
          AND ev.tenant_id=h.tenant_id AND ev.project_key=h.project_key
          AND ev.action IN ('created','revised','review','approved','archived')
          ORDER BY ev.id DESC LIMIT 1) e ON true
        WHERE h.tenant_id=t.id AND h.project_key='AADS' AND h.kind='contract'
          AND (h.document_key={literal('migration-apply-' + release_sha[:12])}
            OR h.document_key LIKE 'migration-apply-m-%')
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
    state = json.loads(result.stdout)
    documents = {d['document_key']: d for d in state.pop('documents', None) or []}
    release_key = 'migration-apply-' + release_sha[:12]
    if release_key in documents:
        state['document'] = documents[release_key]
    elif root is not None:
        key = 'migration-apply-m-' + manifest_hash(inventory(root, state['ledger']))[:12]
        state['document'] = documents.get(key)
    else:
        state['document'] = None
    return state


def inventory(root, ledger, target="aads"):
    if target not in VALID_TARGETS:
        raise Rejected('unknown_migration_target')
    baseline = root / 'scripts/migrations_auto_apply_baseline.txt'
    held = {line.split()[0] for line in baseline.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith('#')} if baseline.exists() else set()
    items = []
    for path in sorted((root / 'migrations').glob('*.sql')):
        name = 'migrations/' + path.name
        if path.name.endswith('.verify.sql') or 'rollback' in path.name:
            continue
        if migration_target(name) != target:
            continue
        sha = digest(path.read_bytes())
        recorded = ledger.get(name, ledger.get(path.name))
        if recorded is not None:
            continue
        if target != 'aads' or name not in held:
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
    binding = data.get('binding', 'release')
    if binding not in ('release', 'manifest'):
        raise Rejected('scope_mismatch')
    origin_sha = data.get('release_sha')
    if (not isinstance(origin_sha, str) or not re.fullmatch('[0-9a-f]{40}', origin_sha)
            or (binding == 'release' and origin_sha != release_sha)):
        raise Rejected('release_mismatch')
    for k, v in {'purpose': 'migration_apply', 'project': 'AADS',
                 'tenant_id': state['tenant_id'], 'target_db': state['target_db']}.items():
        if data.get(k) != v:
            raise Rejected('scope_mismatch')
    if state.get('target_db') != 'aads':
        raise Rejected('unknown_migration_target')
    expected_key = ('migration-apply-m-' + str(data.get('manifest_sha256', ''))[:12]
                    if binding == 'manifest' else 'migration-apply-' + release_sha[:12])
    if 'document_key' in d and d['document_key'] != expected_key:
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
    if binding == 'manifest' and approved != pending:
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
    return f"APPROVED revision_id={d['revision_id']} approver={actor} manifest_sha256={data['manifest_sha256']} binding={binding}"


def register(content, sha, binding='release', manifest_sha256=None, source_task_id=None):
    if not isinstance(source_task_id, str) or not source_task_id.strip():
        raise Rejected('source_task_id_required')
    # Caller supplies a normal existing API login credential; never an approval override.
    base = os.environ['AADS_API_URL'].rstrip('/') + '/api/v1/projects/AADS/documents'
    def post(url, body):
        req = Request(url, json.dumps(body).encode(), headers={
            'Content-Type': 'application/json', 'Authorization': 'Bearer ' + os.environ['AADS_ACCESS_TOKEN']})
        with urlopen(req, timeout=30) as response:
            return json.load(response)
    key = ('migration-apply-m-' + manifest_sha256[:12] if binding == 'manifest'
           else 'migration-apply-' + sha[:12])
    result = post(base, dict(document_key=key, kind='contract', title='릴리스 마이그레이션 적용 승인',
        version='1.0.0', content=content, expected_generation=0,
        source_task_id=source_task_id, change_summary='정확한 릴리스 SQL 명세의 사람 승인 검토 요청'))
    return post(base + '/' + key + '/review', dict(revision_id=result['revision_id'], expected_generation=result['generation']))


NOTES_MAX_BYTES = 64 * 1024


def read_notes(path):
    """검토 메모 파일을 읽는다. 승인 JSON/manifest 에는 영향을 주지 않는다."""
    try:
        raw = Path(path).read_bytes()
        text = raw.decode('utf-8')
    except (OSError, UnicodeDecodeError) as exc:
        raise Rejected('notes_file_invalid') from exc
    if not text.strip() or len(raw) > NOTES_MAX_BYTES or '```aads-migration-approval' in text:
        raise Rejected('notes_file_invalid')
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['draft', 'verify', 'targets'])
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--release-sha')
    parser.add_argument('--manifest-file', type=Path)
    parser.add_argument('--register', action='store_true')
    parser.add_argument('--source-task-id', help='정본 등록을 요청한 실제 작업 ID')
    parser.add_argument('--binding', choices=['manifest', 'release'], default='manifest')
    parser.add_argument('--notes-file', type=Path, help='draft 본문에 넣을 검토 메모(UTF-8 Markdown, 64KiB 이하)')
    args = parser.parse_args()
    try:
        notes = read_notes(args.notes_file) if args.notes_file is not None else None
        if args.register and not (args.source_task_id and args.source_task_id.strip()):
            raise Rejected('source_task_id_required')
        if args.command == 'targets':
            for path in sorted((args.root / 'migrations').glob('*.sql')):
                name = 'migrations/' + path.name
                print(name + '|' + migration_target(name))
            return 0
        if os.environ.get('PGDATABASE', 'aads') != 'aads':
            raise Rejected('unknown_migration_target')
        sha = args.release_sha or os.environ.get('AADS_RELEASE_SHA') or subprocess.check_output(
            ['git', '-C', str(args.root), 'rev-parse', 'HEAD'], text=True).strip()
        if not re.fullmatch('[0-9a-f]{40}', sha):
            raise Rejected('release_mismatch')
        state = lookup(sha, args.root)
        if args.command == 'verify':
            supplied = [{'file': line.split('|')[0], 'sha256': line.split('|')[1]}
                        for line in args.manifest_file.read_text().splitlines() if line]
            print(verify(args.root, sha, supplied, state))
        else:
            items = inventory(args.root, state['ledger'])
            data = dict(purpose='migration_apply', project='AADS', tenant_id=state['tenant_id'],
                target_db=state['target_db'], release_sha=sha, binding=args.binding, manifest=items,
                manifest_sha256=manifest_hash(items), expires_at=(datetime.now(timezone.utc)+timedelta(hours=24)).isoformat())
            content = '# 릴리스 마이그레이션 적용 승인\n\nAPI 롤백은 적용된 SQL을 되돌리지 않음\n\n'
            for item in items:
                content += describe_migration(item, (args.root / item['file']).read_text())
            if notes is not None:
                content += '\n## 검토 메모\n\n' + notes.rstrip('\n') + '\n'
            content += '\n```aads-migration-approval\n' + json.dumps(data, ensure_ascii=False, indent=2) + '\n```\n'
            print(content)
            if args.register:
                print(json.dumps(register(content, sha, args.binding, data['manifest_sha256'], args.source_task_id), ensure_ascii=False))
        return 0
    except Rejected as exc:
        print(str(exc))
    except Exception:
        print('lookup_failed')
    return 1


if __name__ == '__main__':
    sys.exit(main())
