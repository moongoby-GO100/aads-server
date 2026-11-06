"""Run the real preflight with read-only Git and DB transports simulated."""
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPERS = (
    'scripts/apply_migration.sh',
    'scripts/apply_release_migrations.sh',
    'scripts/release_migration_approval.py',
    'scripts/release_migration_autoverify.py',
    'scripts/migrations_auto_apply_baseline.txt',
)


def run_preflight(tmp_path, *, stale=False, missing='', bypass='', verifier='1', detached='', db=False):
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    checker = scripts / 'verify-bluegreen-release-contract.sh'
    if verifier != 'missing':
        checker.write_text('#!/bin/bash\necho verifier-ran\nexit ' + verifier + '\n')
        checker.chmod(0o755)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    git = bin_dir / 'git'
    git.write_text('''#!/bin/bash
case "$*" in
  *HEAD:deploy.sh*) echo expected ;;
  hash-object*) if [[ "$TEST_STALE" == 1 ]]; then echo old; else echo expected; fi ;;
  *cat-file*) [[ -z "$TEST_MISSING" || "$*" != *"HEAD:$TEST_MISSING" ]] ;;
  *) echo test-sha ;;
esac
''')
    git.chmod(0o755)
    docker = bin_dir / 'docker'
    docker.write_text(
        '#!/bin/bash\n'
        '[[ "$TEST_DB" == 1 ]] || exit 1\n'
        'printf "%s\\n" "$*" >> "$TEST_SQL_LOG"\n'
        'case "$*" in *State.Running*) echo true ;; *"RETURNING id"*) echo 42 ;; esac\n'
    )
    docker.chmod(0o755)
    return subprocess.run(
        ['bash', str(ROOT / 'deploy.sh'), 'bluegreen'], capture_output=True, text=True,
        timeout=10, env={**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}',
            'AADS_DEPLOY_SOURCE_DIR': str(tmp_path), 'AADS_DEPLOY_STATE_DIR': str(tmp_path),
            'AADS_CONTROL_AUDIT_LOG': str(tmp_path / 'audit.jsonl'),
            'AADS_DEPLOY_ALLOW_STALE_SCRIPT': bypass, 'AADS_DEPLOY_DETACHED': detached,
            'AADS_DEPLOY_FOREGROUND': '0' if (stale or missing) and not bypass else '1', 'TEST_STALE': str(int(stale)),
            'TEST_MISSING': missing, 'TEST_DB': str(int(db)),
            'TEST_SQL_LOG': str(tmp_path / 'sql.log')},
    )


@pytest.mark.parametrize('detached', ['', '1'])
@pytest.mark.parametrize('missing', ['', *HELPERS])
def test_stale_or_missing_helper_blocks(tmp_path, detached, missing):
    proc = run_preflight(tmp_path, stale=not missing, missing=missing, detached=detached)
    assert proc.returncode == 78, proc.stdout + proc.stderr
    assert 'stale deploy.sh:' in proc.stderr
    assert 'verifier-ran' not in proc.stdout
    assert 'deploy-script-freshness' in (tmp_path / 'audit.jsonl').read_text()
    if missing:
        assert missing in proc.stderr


@pytest.mark.parametrize('stale', [False, True])
def test_explicit_bypass_preserves_verifier_failure(tmp_path, stale):
    proc = run_preflight(tmp_path, stale=stale, bypass='1')
    assert proc.returncode == 1
    assert 'verifier-ran' in proc.stdout


@pytest.mark.parametrize('stale', [False, True])
def test_missing_verifier(tmp_path, stale):
    proc = run_preflight(tmp_path, stale=stale, verifier='missing')
    assert proc.returncode == (78 if stale else 1)
    assert ('stale deploy.sh:' if stale else 'verifier missing') in proc.stdout + proc.stderr


def test_failing_verifier_cannot_mask_stale_exit_78(tmp_path):
    proc = run_preflight(tmp_path, stale=True)
    assert proc.returncode == 78, proc.stdout + proc.stderr
    assert 'stale deploy.sh:' in proc.stderr
    print('verifier exit 1 + stale deploy.sh => rc=78 + stale deploy.sh message')


def test_failing_verifier_keeps_fresh_exit_1(tmp_path):
    proc = run_preflight(tmp_path)
    assert proc.returncode == 1
    assert 'verifier-ran' in proc.stdout
    assert 'stale deploy.sh:' not in proc.stderr


def test_stale_reason_persists_to_deploy_runs_when_db_available(tmp_path):
    proc = run_preflight(tmp_path, stale=True, db=True)
    assert proc.returncode == 78, proc.stdout + proc.stderr
    sql = (tmp_path / 'sql.log').read_text()
    assert 'UPDATE deploy_runs' in sql
    assert "status='blocked'" in sql
    assert "error_summary=NULLIF('stale deploy.sh:" in sql
