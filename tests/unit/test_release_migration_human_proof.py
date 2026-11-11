"""운영 이벤트 스키마와 단일 사람 승인 쓰기 경로를 고정한다."""
import ast
from pathlib import Path
import re

from tests.unit.test_release_migration_approval import a, SHA

ROOT = Path(__file__).parents[2]


def event_writers(tree):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        sql = node.args[0]
        if isinstance(sql, ast.Constant) and isinstance(sql.value, str):
            if re.search(r'insert\s+into\s+(?:public\.)?project_document_events\b', sql.value, re.I):
                yield node, sql.value


def test_only_human_guarded_api_can_insert_approval():
    writers = []
    for path in (ROOT / 'app').rglob('*.py'):
        tree = ast.parse(path.read_text())
        calls = list(event_writers(tree))
        # Also reject SQL moved into variables/f-strings outside inspected calls.
        inserts = [n for n in ast.walk(tree) if isinstance(n, ast.Constant)
                   and isinstance(n.value, str) and re.search(
                       r'insert\s+into\s+(?:public\.)?project_document_events\b', n.value, re.I)]
        assert {id(n) for n in inserts} == {id(call.args[0]) for call, _ in calls}
        for call, sql in calls:
            # Pin every event INSERT, including parameterized actions: a new writer
            # or action expression requires security review rather than silent acceptance.
            functions = [n for n in ast.walk(tree)
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                         and any(child is call for child in ast.walk(n))]
            assert len(functions) == 1
            function = functions[0]
            writers.append((path.relative_to(ROOT).as_posix(), function.name))
            action = re.search(r"VALUES\([^)]*?\$4,\s*('([^']+)'|\$5)", sql, re.I)
            assert action, sql
            if action[1] == '$5':
                assert function.name == 'create_revision_in_tx'
                assert ast.dump(call.args[5], include_attributes=False) == ast.dump(
                    ast.parse('"created" if head["generation"] == 0 else "revised"', mode='eval').body,
                    include_attributes=False)
            elif action[2] == 'approved':
                # 승인 INSERT 는 공용 핵심 한 곳. 호출자는 아래 test_approval_core_callers_are_pinned 가 고정한다.
                assert function.name == 'approve_revision_in_tx'
            else:
                assert action[2] in {'archived', 'review', 'goal_linked', 'legacy_linked', 'legacy_unlinked'}
    # approve_revision_in_tx 는 일반/위임 감사(metadata) INSERT 두 개, _record_delegated_denial 은 거절 감사(승인 아님).
    assert sorted(writers) == sorted(
        [('app/api/canonical_documents.py', name) for name in
         ['create_revision_in_tx', 'approve_revision_in_tx', 'approve_revision_in_tx', '_archive_approved',
          'review_revision_in_tx', 'link_goal', 'link_legacy_goal_document', 'unlink_legacy_goal_document']]
        + [('app/services/canonical_document_tools.py', '_record_delegated_denial')])


def test_lookup_matches_existing_schema_and_live_owner(monkeypatch):
    import subprocess

    def capture(cmd, **kwargs):
        sql = cmd[-1]
        assert 'e.metadata' not in sql and 'auth_type' not in sql
        # metadata 열은 위임 마이그레이션 적용 전에는 없다 — 열 참조 대신 to_jsonb(e) 로 읽어야 조회가 깨지지 않는다.
        assert "to_jsonb(e)->'metadata'" in sql
        for clause in ["m.tenant_id=t.id", "m.user_id=e.actor_id", "m.role='owner'",
                       "m.status='active'", 'm.deleted_at IS NULL',
                       "u.status='active'", 'u.deleted_at IS NULL',
                       'u.id=m.user_id', 'ev.tenant_id=h.tenant_id',
                       'ev.project_key=h.project_key', 'ORDER BY ev.id DESC LIMIT 1']:
            assert clause in sql
        return subprocess.CompletedProcess(cmd, 0, '{}', '')

    monkeypatch.setattr(a.subprocess, 'run', capture)
    assert a.lookup(SHA) == {'document': None}


def test_isolated_postgres_human_proof(tmp_path, monkeypatch):
    """Opt-in test DSN only: temporary tables, no production DB transport."""
    import os
    import pytest
    from tests.unit import test_release_migration_approval as approval_tests

    dsn = os.environ.get('AADS_TEST_ISOLATED_POSTGRES_DSN')
    if not dsn:
        pytest.skip('격리 PostgreSQL DSN 미제공; 운영 DB로 대체하지 않음')
    psycopg = pytest.importorskip('psycopg')
    root, state, _, _ = approval_tests.approved.__wrapped__(tmp_path)
    queries = []

    def capture(cmd, **kwargs):
        import subprocess
        queries.append(cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, '{}', '')

    monkeypatch.setattr(a.subprocess, 'run', capture)
    a.lookup(SHA)
    tenant = '11111111-1111-4111-8111-111111111111'
    actor = state['document']['actor_id']
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        # TEMP shadows unqualified names; events deliberately have no metadata.
        cur.execute('''
            CREATE TEMP TABLE tenants(id uuid, slug text, status text, deleted_at timestamptz);
            CREATE TEMP TABLE saas_users(id text, status text, deleted_at timestamptz);
            CREATE TEMP TABLE tenant_memberships(tenant_id uuid, user_id text, role text,
                status text, deleted_at timestamptz);
            CREATE TEMP TABLE project_document_heads(id int, tenant_id uuid, project_key text,
                kind text, document_key text, approved_revision_id int, latest_revision_id int);
            CREATE TEMP TABLE project_document_revisions(id int, head_id int, content text, content_hash text);
            CREATE TEMP TABLE project_document_events(id int, tenant_id uuid, project_key text,
                head_id int, revision_id int, action text, actor_id text, created_at timestamptz,
                goal_document_id uuid);
            CREATE TEMP TABLE schema_migrations(filename text, sha256 text);
        ''')
        cur.execute("INSERT INTO tenants VALUES (%s,'internal','active',NULL)", (tenant,))
        cur.execute("INSERT INTO saas_users VALUES (%s,'active',NULL)", (actor,))
        cur.execute("INSERT INTO tenant_memberships VALUES (%s,%s,'owner','active',NULL)", (tenant, actor))
        cur.execute("INSERT INTO project_document_heads VALUES (1,%s,'AADS','contract',%s,1,1)",
                    (tenant, 'migration-apply-' + SHA[:12]))
        cur.execute('INSERT INTO project_document_revisions VALUES (1,1,%s,%s)',
                    (state['document']['content'], state['document']['content_hash']))
        cur.execute("INSERT INTO project_document_events VALUES (1,%s,'AADS',1,1,'approved',%s,now(),NULL)",
                    (tenant, actor))
        cur.execute(queries[0])
        assert cur.fetchone()[0]['document']['owner'] is True
        for mutation in ["UPDATE tenant_memberships SET role='member'",
                         "UPDATE tenant_memberships SET role='owner', tenant_id='22222222-2222-4222-8222-222222222222'",
                         "UPDATE tenant_memberships SET tenant_id='11111111-1111-4111-8111-111111111111'; UPDATE saas_users SET status='inactive'"]:
            cur.execute(mutation)
            cur.execute(queries[0])
            assert cur.fetchone()[0]['document']['owner'] is False
        conn.rollback()


def test_approval_core_callers_are_pinned():
    """approve_revision_in_tx 를 부르는 곳은 사람 승인 API, 자동승인, 위임 승인 셋뿐이다. 사람 API 는 사람 가드 뒤에서만 부른다."""
    callers = []
    for path in (ROOT / 'app').rglob('*.py'):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and ast.unparse(node.func).endswith('approve_revision_in_tx') \
                        and fn.name != 'approve_revision_in_tx':
                    callers.append((path.relative_to(ROOT).as_posix(), fn.name))
                    if fn.name == 'approve_document':
                        transaction = next(n for n in fn.body if isinstance(n, ast.AsyncWith))
                        guard = transaction.body[0]
                        assert isinstance(guard, ast.Expr) and isinstance(guard.value, ast.Call)
                        assert guard.value.func.id == '_require_human_approver'
                        assert guard.lineno < node.lineno
                        assert ast.unparse(node.args[-1]) == 'actor'
    assert sorted(set(callers)) == [
        ('app/api/canonical_documents.py', 'approve_document'),
        ('app/services/canonical_document_tools.py', 'approve_document_delegated'),
        ('app/services/doc_auto_approval.py', 'maybe_auto_approve'),
    ]
