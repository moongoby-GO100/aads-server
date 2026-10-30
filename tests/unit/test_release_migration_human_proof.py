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
                assert function.name == 'approve_document'
                # Guard must be an unconditional statement in the same transaction,
                # before all writes; a guard hidden in an if branch cannot qualify.
                transaction = next(n for n in function.body if isinstance(n, ast.AsyncWith))
                guard = transaction.body[0]
                assert isinstance(guard, ast.Expr) and isinstance(guard.value, ast.Call)
                assert isinstance(guard.value.func, ast.Name)
                assert guard.value.func.id == '_require_human_approver'
                assert ast.unparse(guard.value.args[0]) == 'context'
                assert guard.lineno < call.lineno
                assert ast.unparse(call.args[-1]) == 'actor'
            else:
                assert action[2] in {'archived', 'review', 'goal_linked', 'legacy_linked', 'legacy_unlinked'}
    assert sorted(writers) == sorted(
        ('app/api/canonical_documents.py', name) for name in
        ['create_revision_in_tx', 'approve_document', '_archive_approved', 'review_revision_in_tx',
         'link_goal', 'link_legacy_goal_document', 'unlink_legacy_goal_document'])



def test_lookup_matches_existing_schema_and_live_owner(monkeypatch):
    import subprocess

    def capture(cmd, **kwargs):
        sql = cmd[-1]
        assert 'metadata' not in sql and 'auth_type' not in sql
        for clause in ["m.tenant_id=t.id", "m.user_id=e.actor_id", "m.role='owner'",
                       "m.status='active'", 'm.deleted_at IS NULL',
                       "u.status='active'", 'u.deleted_at IS NULL',
                       'u.id=m.user_id', 'ev.tenant_id=h.tenant_id',
                       'ev.project_key=h.project_key', 'ORDER BY ev.id DESC LIMIT 1']:
            assert clause in sql
        return subprocess.CompletedProcess(cmd, 0, '{}', '')

    monkeypatch.setattr(a.subprocess, 'run', capture)
    assert a.lookup(SHA) == {}


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
