"""Exercise AGY admission and post-claim switching without a database."""
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCES = [ROOT / 'scripts/pipeline-runner.sh', ROOT / 'scripts/pipeline-runner.sh.local']


def function(path):
    return re.search(r'^_claim_queued_job\(\) \{\n.*?^\}', path.read_text(), re.M | re.S).group()


def run(tmp_path, host='other', preflight=0, codex=False, result='', **env):
    log = tmp_path / 'log'
    code = r'''
RUNNER_HOST_NAME="$HOST" RUNNER_ENGINE_MODE=general RUNNER_ANTIGRAVITY_EXEC=unused
runner_pool_claim_extra_sql() { :; }; runner_overflow_claim_sql() { :; }
runner_pool_host_scope_sql() { :; }; runner_overflow_reason_sql() { :; }
runner_overflow_primary_sql() { echo 'SELECT dep.job_id FROM pipeline_jobs dep'; }
timeout() { return "$PREFLIGHT"; }
command() { if [[ "$1" == -v && "$2" == codex ]]; then [[ "$CODEX" == 1 ]]; else builtin command "$@"; fi; }
runner_overflow_db_claim() { printf '%s' "$1" > "$LOG"; printf '%s' "$RESULT"; }
db_update() { printf '\nUPDATE:%s\n' "$1" >> "$LOG"; }
sql_escape() { printf "'%s'" "${1//\'/\'\'}"; }
record_runner_event() { printf 'EVENT:%s:%s\n' "$2" "$9" >> "$LOG"; }
''' + function(SOURCES[0]) + '\n_claim_queued_job ""\n'
    proc = subprocess.run(['bash', '-c', code], capture_output=True, text=True,
                          env={**os.environ, 'HOST': host, 'PREFLIGHT': str(preflight),
                               'CODEX': str(int(codex)), 'RESULT': result, 'LOG': str(log), **env})
    assert proc.returncode == 0, proc.stderr
    return log.read_text(), proc.stdout


def test_avoid_even_when_preflight_succeeds(tmp_path):
    sql, _ = run(tmp_path, host='contabo14')
    assert "e.event_type='agy_fallback_model_switch'" in sql
    assert "AND COALESCE(NULLIF(p.worker_model, ''), NULLIF(p.model, ''), '') !~" in sql


def test_success_and_missing_codex_keep_existing_conditions(tmp_path):
    success, _ = run(tmp_path)
    failed, _ = run(tmp_path, preflight=1)
    assert 'p.updated_at <= NOW()' not in success + failed
    assert "e.event_type='agy_fallback_model_switch'" not in success + failed
    assert failed.count(" !~ '") == success.count(" !~ '") + 1


def test_switch_after_claim(tmp_path):
    row = '\x1e'.join(['job-1', 'AADS', '6869', 'chat', '3', 'antigravity:foo', 'M', 'group'])
    log, output = run(tmp_path, preflight=1, codex=True, result=row)
    assert "p.updated_at <= NOW() - (600 * INTERVAL '1 second')" in log
    assert "UPDATE:UPDATE pipeline_jobs SET worker_model='codex:gpt-6.1-terra'" in log
    assert 'EVENT:agy_fallback_model_switch:' in log and 'antigravity:foo' in log
    assert output.rstrip('\n').split('\x1e') == row.split('\x1e')[:5] + ['codex:gpt-6.1-terra', 'M', 'group']


def test_empty_avoid_invalid_seconds_and_overflow(tmp_path):
    sql, _ = run(tmp_path, host='contabo14', preflight=1, codex=True,
                 AGY_AVOID_ONLY_HOSTS='', AGY_FALLBACK_AFTER_SEC='bad', RUNNER_OVERFLOW_OF='primary')
    assert "(600 * INTERVAL '1 second')" in sql
    assert 'detarget.' not in sql and 'dep.' in sql
    assert "e.event_type='agy_fallback_model_switch'" not in sql


def test_functions_identical():
    assert function(SOURCES[0]) == function(SOURCES[1])
