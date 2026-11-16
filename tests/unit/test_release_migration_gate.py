"""로컬 격리 셸에서 승인 바인딩과 plan 분류를 검증한다."""
import hashlib
import os
import subprocess

import pytest

from tests.unit import test_deploy_release_migrations as migration_tests

RUNNER = migration_tests.RUNNER
release = migration_tests.release


@pytest.fixture
def gated(release):
    path = release.root / 'scripts/apply_release_migrations.sh'
    # Keep the fixture ledger transport and deny approval by default.
    path.write_text(path.read_text().replace('    return 0\n}', '    return 1\n}', 1))
    return release


def test_pending_without_approval_stops(gated):
    gated.write('001.sql', 'SELECT 1;')
    result = gated.run()
    assert result.returncode == 7
    assert 'BLOCK-CEO-APPROVAL' in result.stdout
    assert 'migrations/001.sql|' in result.stdout
    assert not gated.applied()


def test_zero_pending_passes_without_source(gated):
    assert gated.run().returncode == 0


def stub_source(gated, digest, *, fail=False):
    """Simulate a future trusted DB verifier in a disposable script, never via env."""
    path = gated.root / 'scripts/apply_release_migrations.sh'
    script = path.read_text()
    replacement = 'return 1' if fail else (
        '[[ "$1" == "' + 'a' * 40 + '" && "$2" == '
        "'migrations/001.sql|" + digest + "'" + "$'\\n' ]]"
    )
    path.write_text(script.replace(
        '    return 1\n}', '    ' + replacement + '\n}', 1,
    ))


def test_trusted_source_matching_sha_and_manifest_pass(gated, monkeypatch):
    path = gated.write('001.sql', 'SELECT 1;')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setenv('AADS_RELEASE_SHA', 'a' * 40)
    stub_source(gated, digest)
    assert gated.run().returncode == 0
    assert gated.applied() == ['migrations/001.sql']


@pytest.mark.parametrize('change', ['file', 'release'])
def test_changed_approval_binding_stops(gated, monkeypatch, change):
    path = gated.write('001.sql', 'SELECT 1;')
    stub_source(gated, hashlib.sha256(path.read_bytes()).hexdigest())
    monkeypatch.setenv('AADS_RELEASE_SHA', ('b' if change == 'release' else 'a') * 40)
    if change == 'file':
        path.write_text('SELECT 2;')
    assert gated.run().returncode == 7
    assert not gated.applied()


def test_source_failure_stops(gated):
    gated.write('001.sql', 'SELECT 1;')
    stub_source(gated, '', fail=True)
    assert gated.run().returncode == 7
    assert not gated.applied()


def test_environment_cannot_self_approve(gated, monkeypatch):
    gated.write('001.sql', 'SELECT 1;')
    monkeypatch.setenv('AADS_MIGRATION_CEO_APPROVED', 'true')
    assert gated.run().returncode == 7


def fake_catalog(gated, monkeypatch, body):
    binary = gated.root / 'docker'
    binary.write_text('#!/bin/bash\n' + body)
    binary.chmod(0o755)
    monkeypatch.setenv('PATH', str(gated.root) + os.pathsep + os.environ['PATH'])


def test_plan_reports_unledgered_columns_and_constraints(gated, monkeypatch):
    gated.write('001.sql', '''ALTER TABLE public.items
      ADD COLUMN IF NOT EXISTS tenant_id uuid,
      ADD CONSTRAINT items_unique UNIQUE (tenant_id);''')
    fake_catalog(gated, monkeypatch, '''case "$*" in
      *"SELECT count(*) FROM information_schema.columns"*) echo 1;;
      *"SELECT count(*) FROM information_schema.table_constraints"*) echo 1;;
      *) exit 99;; esac\n''')
    result = gated.run('--plan')
    assert result.returncode == 0, result.stderr
    assert 'unledgered COLUMN public.items.tenant_id already exists' in result.stdout
    assert 'unledgered CONSTRAINT public.items.items_unique already exists' in result.stdout
    assert 'PENDING migrations/001.sql' in result.stdout
    assert not gated.applied()


def test_catalog_failure_does_not_report_clean_plan(gated, monkeypatch):
    gated.write('001.sql', 'ALTER TABLE items ADD COLUMN tenant_id uuid;')
    fake_catalog(gated, monkeypatch, 'exit 1\n')
    assert gated.run('--plan').returncode == 8
    assert not gated.applied()


def _stub_verifier(gated, verify_rc, postcheck_rc=0):
    gated.write('001.sql', 'SELECT 1;')
    stub = gated.root / 'scripts/release_migration_autoverify.py'
    stub.write_text(
        'import sys\n'
        f'cmd = sys.argv[1]\nprint("STUB", cmd)\nsys.exit({verify_rc} if cmd == "verify" else {postcheck_rc})\n')


def test_auto_verify_pass_applies_without_human_approval(gated):
    # 2026-11-05 대표님 결정: 사람 승인 근거가 없어도 자동 검증을 통과하면 적용한다.
    _stub_verifier(gated, 0)
    result = gated.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'AUTO-APPROVED' in result.stdout and 'STUB postcheck' in result.stdout


def test_auto_verify_fail_still_requires_human_approval(gated):
    _stub_verifier(gated, 1)
    result = gated.run()
    assert result.returncode == 7
    assert 'BLOCK-CEO-APPROVAL' in result.stdout


def test_auto_verify_can_be_disabled(gated, monkeypatch):
    _stub_verifier(gated, 0)
    monkeypatch.setenv('AADS_MIGRATION_AUTOVERIFY', '0')
    assert gated.run().returncode == 7


def test_postcheck_failure_fails_release(gated):
    _stub_verifier(gated, 0, postcheck_rc=1)
    assert gated.run().returncode == 5


# ── 2026-11-16 교차검토 id=552 보강: precheck · verify.sql ROLLBACK · down 소유권 가드 ──

def _install_precheck(gated, body):
    stub = gated.root / 'scripts/release_migration_precheck.py'
    stub.write_text(body)


def test_precheck_existing_object_stops_before_apply(gated):
    _stub_verifier(gated, 0)
    _install_precheck(gated, 'import sys\nprint("PRECHECK-EXISTS column public.t.c", file=sys.stderr)\nsys.exit(9)\n')
    result = gated.run()
    assert result.returncode == 9
    assert 'BLOCK-PRECHECK migrations/001.sql' in result.stderr
    assert not gated.applied()


def test_precheck_catalog_failure_stops_before_apply(gated):
    _stub_verifier(gated, 0)
    _install_precheck(gated, 'import sys\nsys.exit(8)\n')
    assert gated.run().returncode == 9
    assert not gated.applied()


def test_precheck_absent_passes_note_to_apply(gated):
    _stub_verifier(gated, 0)
    _install_precheck(gated, 'print(\'{"precheck":"absent","objects":[]}\')\n')
    fake = gated.root / 'fake_apply.sh'
    fake.write_text(fake.read_text() + f'echo "$AADS_MIGRATION_NOTE" > "{gated.root}/note.txt"\n')
    result = gated.run()
    assert result.returncode == 0, result.stderr
    assert (gated.root / 'note.txt').read_text().strip() == '{"precheck":"absent","objects":[]}'


def test_precheck_script_is_read_only_and_reports_absent_json():
    src = (migration_tests.ROOT / 'scripts/release_migration_precheck.py').read_text()
    assert 'read_only=True' in src and '"precheck": "absent"' in src
    for word in ('INSERT', 'UPDATE ', 'DELETE', 'DROP', 'CREATE TABLE'):
        assert word not in src.replace('CREATE TABLE IF NOT EXISTS', '')


def _apply_migration_script():
    return (migration_tests.ROOT / 'scripts/apply_migration.sh').read_text()


def test_apply_migration_runs_verify_before_commit_and_records_note():
    s = _apply_migration_script()
    assert 'verify_file="${file%.sql}.verify.sql"' in s
    verify_at = s.index('sed -e \'$a\\\' "$verify_file"')
    commit_at = s.index("'COMMIT;'")
    insert_at = s.index('INSERT INTO schema_migrations')
    # 적용 기록 INSERT → verify → COMMIT : verify 실패(ON_ERROR_STOP)면 둘 다 ROLLBACK
    assert insert_at < verify_at < commit_at
    assert 'ON_ERROR_STOP=1' in s
    assert 'note = EXCLUDED.note' in s and 'AADS_MIGRATION_NOTE' in s
    assert 'COMMIT 전 정의 검증 없이 적용' in s  # 없으면 경고만, 기존 동작 유지


def test_apply_migration_verify_failure_rolls_back_ledger(tmp_path):
    """가짜 docker 가 stdin SQL 을 받아 psql 처럼 실패하면, COMMIT 이 SQL 스트림 끝에 있어 도달하지 못함."""
    root = migration_tests.ROOT
    sql = tmp_path / 'm.sql'
    sql.write_text('SELECT 1;\n')
    (tmp_path / 'm.verify.sql').write_text("DO $$ BEGIN RAISE EXCEPTION 'VERIFY_FAIL'; END $$;\n")
    captured = tmp_path / 'stdin.sql'
    fake = tmp_path / 'bin'
    fake.mkdir()
    docker = fake / 'docker'
    docker.write_text(
        '#!/bin/bash\n'
        'case "$*" in\n'
        '  *"information_schema.tables"*) echo t;;\n'
        '  *"SELECT sha256 FROM"*) exit 0;;\n'
        f'  *"psql -v ON_ERROR_STOP=1"*) cat > "{captured}"; exit 3;;\n'
        'esac\n')
    docker.chmod(0o755)
    import hashlib as h
    digest = h.sha256(f'{sql}:{h.sha256(sql.read_bytes()).hexdigest()}\n'.encode()).hexdigest()
    result = subprocess.run(
        ['bash', str(root / 'scripts/apply_migration.sh'), str(sql)],
        env=dict(os.environ, PATH=f'{fake}:{os.environ["PATH"]}', AADS_MIGRATION_GATE_MANIFEST=digest,
                 AADS_MIGRATION_NOTE='{"precheck":"absent","objects":[]}'),
        capture_output=True, text=True, timeout=60)
    assert result.returncode != 0
    stream = captured.read_text()
    assert stream.index('INSERT INTO schema_migrations') < stream.index('VERIFY_FAIL') < stream.rindex('COMMIT;')
    assert '"precheck":"absent"' in stream


DOWN_FILES = [
    ('20261111_canonical_document_approval_delegation', '20261111'),
    ('20261112_code_review_requests_revert_of_verified', '20261112'),
]


@pytest.mark.parametrize('stem,tag', DOWN_FILES)
def test_down_requires_precheck_absent_ownership_and_wide_depend(stem, tag):
    root = migration_tests.ROOT
    down = (root / f'migrations/rollback/{stem}.down.sql').read_text()
    assert (root / f'migrations/{stem}_rollback.sql').read_text() == down
    assert "'precheck'" in down and 'absent' in down and '소유권 미입증' in down
    assert "deptype IN ('n', 'a', 'i')" in down
    assert "deptype = 'n'" not in down
    assert "'pg_class'::regclass" in down
    assert down.index('소유권 미입증') < down.index('DROP ') or 'DROP COLUMN' in down
    assert down.index('소유권 미입증') < down.index('ALTER TABLE IF EXISTS')


@pytest.mark.parametrize('stem,tag', DOWN_FILES)
def test_verify_sql_raises_and_covers_definition(stem, tag):
    verify = (migration_tests.ROOT / f'migrations/{stem}.verify.sql').read_text()
    assert 'RAISE EXCEPTION' in verify
    for needle in ('data_type', 'is_nullable', 'column_default'):
        assert needle in verify
    if tag == '20261111':
        for needle in ('pg_get_indexdef', 'indisvalid', 'conrelid', 'tenant_id, project_key, grantee'):
            assert needle in verify


@pytest.mark.parametrize('stem,tag', DOWN_FILES)
def test_forward_sql_documents_postcheck_recovery(stem, tag):
    fwd = (migration_tests.ROOT / f'migrations/{stem}.sql').read_text()
    assert 'postcheck 실패 시 복구 절차' in fwd
    for needle in ('COMMIT 전 실패', 'COMMIT 후 실패', 'API 전환을 하지 않는다', 'CEO 승인', 'CTO 세션'):
        assert needle in fwd


# 카탈로그 지문 계약: 속성별 down 허용 분기를 다시 추가하지 않는다.
def test_fingerprint_recorded_after_verify_before_commit():
    s = _apply_migration_script()
    assert s.index('sed -e \'$a\\\' "$verify_file"') < s.index('UPDATE schema_migrations SET note') < s.index("'COMMIT;'")
    helper = (migration_tests.ROOT / 'migrations/release_object_fingerprint.verify.sql').read_text()
    for part in ('format_type', 'attnotnull', 'pg_get_expr', 'pg_get_constraintdef(k.oid, true)',
                 'convalidated', 'pg_get_indexdef', 'indisvalid', 'pg_get_triggerdef',
                 'ORDER BY obj::text COLLATE "C"', 'md5(objects::text)'):
        assert part in helper
    assert 'regexp_replace' not in helper


@pytest.mark.parametrize('stem,tag', DOWN_FILES)
def test_down_fingerprint_failure_precedes_drop(stem, tag):
    down = (migration_tests.ROOT / f'migrations/rollback/{stem}.down.sql').read_text()
    check = down.index("v_recorded->>'fingerprint' IS NULL")
    mismatch = down.index("RAISE EXCEPTION '소유권 미입증: fingerprint missing/mismatch")
    end_guard = down.index('END $$;')
    drop = down.index('\nDROP TABLE') if tag == '20261111' else down.index('\nALTER TABLE IF EXISTS')
    assert check < mismatch < end_guard < drop
    assert 'pg_temp.release_object_fingerprint' in down
    assert 'regexp_replace' not in down
    assert 'pg_get_constraintdef' not in down  # 공통 함수만 사용


def test_verify_exact_catalog_definitions_without_normalization():
    for stem, _ in DOWN_FILES:
        verify = (migration_tests.ROOT / f'migrations/{stem}.verify.sql').read_text()
        assert 'regexp_replace' not in verify
    verify = (migration_tests.ROOT / f'migrations/{DOWN_FILES[0][0]}.verify.sql').read_text()
    assert 'pg_get_constraintdef(k.oid, true)' in verify
    assert 'pg_get_indexdef(i.indexrelid)' in verify
    assert 'PostgreSQL 15' in verify
    assert "''^[A-Z0-9][A-Z0-9_-]{0,63}$''::text" in verify


# ── 2026-11-17 P2/S: down/rollback \ir 의존 제거 및 run_migration_down.sh 실행기 검증 ──

def test_down_and_rollback_sql_have_no_psql_include():
    root = migration_tests.ROOT
    files = [
        root / 'migrations/rollback/20261111_canonical_document_approval_delegation.down.sql',
        root / 'migrations/20261111_canonical_document_approval_delegation_rollback.sql',
        root / 'migrations/rollback/20261112_code_review_requests_revert_of_verified.down.sql',
        root / 'migrations/20261112_code_review_requests_revert_of_verified_rollback.sql',
    ]
    for path in files:
        text = path.read_text()
        assert r'\ir' not in text, f'{path.name} contains \\ir'
        assert r'\i ' not in text, f'{path.name} contains \\i '
        assert '실행은 scripts/run_migration_down.sh 로만 한다(지문 함수 선주입)' in text


def test_run_migration_down_execute_requires_approval_env():
    root = migration_tests.ROOT
    script = root / 'scripts/run_migration_down.sh'
    env_without_appr = {k: v for k, v in os.environ.items() if k != 'AADS_DOWN_APPROVED'}

    # --execute without AADS_DOWN_APPROVED must fail with non-zero exit code
    res = subprocess.run(
        ['bash', str(script), '--execute', '20261111_canonical_document_approval_delegation.sql'],
        capture_output=True, text=True, check=False, env=env_without_appr,
    )
    assert res.returncode != 0
    assert 'AADS_DOWN_APPROVED' in res.stderr or '승인' in res.stderr

    # empty AADS_DOWN_APPROVED must also fail
    env_empty = dict(env_without_appr, AADS_DOWN_APPROVED='')
    res2 = subprocess.run(
        ['bash', str(script), '--execute', '20261111_canonical_document_approval_delegation.sql'],
        capture_output=True, text=True, check=False, env=env_empty,
    )
    assert res2.returncode != 0


def test_run_migration_down_static_script_structure():
    root = migration_tests.ROOT
    src = (root / 'scripts/run_migration_down.sh').read_text()
    assert 'release_object_fingerprint.verify.sql' in src
    assert 'set -euo pipefail' in src
    assert 'mode="print"' in src or "mode='print'" in src
    assert 'AADS_DOWN_APPROVED' in src
    assert 'docker exec -i "$PG_CONTAINER" psql -v ON_ERROR_STOP=1' in src

    # Verify that helper function file is cat'ed before down file
    cat_helper_idx = src.index('cat "$helper_file"')
    cat_down_idx = src.index('cat "$down_file"')
    assert cat_helper_idx < cat_down_idx

    # Verify BEGIN / COMMIT handling
    assert 'BEGIN' in src and 'COMMIT' in src


@pytest.mark.parametrize('stem,tag', DOWN_FILES)
def test_run_migration_down_dry_run_prepends_fingerprint_helper(stem, tag):
    root = migration_tests.ROOT
    script = root / 'scripts/run_migration_down.sh'

    # Run with default (which is --print)
    res_default = subprocess.run(
        ['bash', str(script), f'{stem}.sql'],
        capture_output=True, text=True, check=True,
    )
    # Run with explicit --print
    res_print = subprocess.run(
        ['bash', str(script), '--print', f'{stem}.sql'],
        capture_output=True, text=True, check=True,
    )
    assert res_default.stdout == res_print.stdout

    stdout = res_print.stdout
    fp_idx = stdout.index('CREATE OR REPLACE FUNCTION pg_temp.release_object_fingerprint')
    if tag == '20261111':
        down_idx = stdout.index('DROP TABLE IF EXISTS public.project_document_approval_delegations;')
    else:
        down_idx = stdout.index('ALTER TABLE IF EXISTS public.code_review_requests DROP COLUMN IF EXISTS revert_of_verified;')

    assert fp_idx < down_idx
    # Ensure there is no duplicated BEGIN
    assert stdout.count('BEGIN;') == 1
    assert stdout.count('COMMIT;') == 1

