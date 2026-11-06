"""보조 러너 우선순위: 실제 DB 없이 셸 SQL 생성과 상태 로그를 검증한다."""
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / 'scripts/pipeline-runner.sh').read_text()
# 변경 전 SQL을 고정해 git 메타데이터 없이도 바이트 호환성을 검증한다.
BASE = r'''_claim_queued_job() {
    local filter="$1"
    local engine_predicate model_return_expr
    if [[ "$RUNNER_ENGINE_MODE" == "litellm" ]]; then
        engine_predicate="AND COALESCE(NULLIF(p.worker_model, ''), NULLIF(p.model, ''), '') LIKE 'litellm:%'"
        model_return_expr="COALESCE(NULLIF(worker_model, ''), NULLIF(model, ''), 'auto')"
    else
        engine_predicate="AND NOT (COALESCE(NULLIF(p.worker_model, ''), NULLIF(p.model, ''), '') LIKE 'litellm:%' AND p.project IN ('GO100','KIS','SF','NTV2'))"
        model_return_expr="COALESCE(NULLIF(worker_model, ''), NULLIF(model, ''), 'auto')"
    fi
    # UTF-8 바이트 hex로 반환해 줄바꿈/RS/pipe를 포함한 원문을 보존한다.
    # AADS-211: depends_on 체크 — 의존 작업이 done이 아니면 스킵
    # RUNNER_ENGINE_MODE=litellm: litellm:* 작업만 claim. general은 원격 litellm 작업을 전용 러너에 넘김.
    # 어느 서버가 집었는지 남긴다. runner_pid 는 숫자라 호스트를 구분하지 못한다.
    db_exec "UPDATE pipeline_jobs SET status='claimed', runner_host='${RUNNER_HOST_NAME}', updated_at=NOW()
             WHERE job_id = (
                SELECT p.job_id FROM pipeline_jobs p
                WHERE p.status='queued' AND p.phase IN ('queued','coding') $filter ${RUNNER_MH_CLAIM_EXTRA:-} $(runner_pool_claim_extra_sql)
                  AND (p.depends_on IS NULL OR EXISTS (
                       SELECT 1 FROM pipeline_jobs dep
                       WHERE dep.job_id = p.depends_on AND dep.status = 'done'))
                  AND (SELECT COUNT(*) FROM pipeline_jobs r
                       WHERE r.project = p.project
                         AND r.status IN ('running', 'claimed')
                         AND r.job_id != p.job_id $(runner_pool_host_scope_sql r)) < ${MAX_CONCURRENT_PER_PROJECT:-6}
                  ${engine_predicate}
                ORDER BY CASE WHEN p.instruction ~* '(^|[[:space:]])PRIORITY:[[:space:]]*P0' THEN 1 ELSE 0 END DESC,
                         CASE WHEN p.logs @> '[{\"event\": \"file_conflict_dependency_requeued\"}]'::jsonb THEN 1 ELSE 0 END DESC,
                         COALESCE(p.priority, 0) DESC, p.created_at ASC LIMIT 1
                FOR UPDATE SKIP LOCKED
             )
             RETURNING job_id, project, encode(convert_to(instruction, 'UTF8'), 'hex'), chat_session_id, max_cycles, ${model_return_expr}, COALESCE(size,'M'), COALESCE(parallel_group,'');"
}

claim_approved_job() {
    local filter="$1"
    db_exec "UPDATE pipeline_jobs SET status='deploying', phase='deploying', updated_at=NOW()
             WHERE job_id = (
                SELECT job_id FROM pipeline_jobs
                WHERE status='approved' $filter
                ORDER BY updated_at ASC LIMIT 1
                FOR UPDATE SKIP LOCKED
             )
             RETURNING job_id, project, chat_session_id;"
}'''


def function(source, name):
    return re.search(rf'^{name}\(\) \{{(?:[^\n]*\}}|\n.*?^\}})', source, re.M | re.S).group()


FUNCTIONS = '\n'.join(function(SRC, name) for name in (
    'runner_overflow_primary_sql', 'runner_overflow_condition_sql',
    'runner_overflow_reason_sql', 'runner_overflow_claim_sql',
    'runner_overflow_log_claim', 'runner_overflow_db_claim', 'runner_pool_claim_extra_sql',
    'runner_is_deploy_delegate', 'runner_pool_host_scope_sql', '_claim_queued_job',
    'claim_approved_job'))
HEADER = next(line for line in SRC.splitlines() if line.startswith('DEPLOY_ONLY_HEADER_SQL='))
STUB = r'''
sql_escape() { printf "'%s'" "${1//\'/\'\'}"; }
log() { printf '%s\n' "$*"; }
db_exec() { printf '%s' "$1"; }
RUNNER_HOST_NAME=secondary
RUNNER_ENGINE_MODE=general
RUNNER_DEPLOY_DELEGATE_HOST=""
RUNNER_CAPACITY_SCOPE=""
RUNNER_OVERFLOW_YIELD_STATE=""
'''


def run(body, primary='', **env):
    result = subprocess.run(['bash', '-uc', STUB + HEADER + '\n' + FUNCTIONS + '\n' + body],
                            env={**os.environ, 'RUNNER_OVERFLOW_OF': primary,
                                 'RUNNER_OVERFLOW_STALE_SEC': '180',
                                 'RUNNER_OVERFLOW_GRACE_SEC': '120', **env},
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize('engine', ['general', 'litellm'])
@pytest.mark.parametrize('delegate', ['', 'server-116'])
def test_disabled_sql_is_byte_identical_to_previous_claim(engine, delegate):
    assert run('runner_overflow_claim_sql') == ''
    old = function(BASE, '_claim_queued_job')
    prefix = f'RUNNER_ENGINE_MODE={engine}; RUNNER_DEPLOY_DELEGATE_HOST="{delegate}"; '
    assert run(prefix + '_claim_queued_job "AND project=\'AADS\'"') == run(
        old + '\n' + prefix + '_claim_queued_job "AND project=\'AADS\'"')


def test_four_conditions_are_per_job_or_in_queued_claim():
    sql = run('runner_overflow_db_claim() { printf "%s" "$1"; }; _claim_queued_job ""', 'contabo42')
    assert 'AND EXISTS (SELECT 1 FROM (SELECT' in sql
    assert run('runner_overflow_claim_sql', 'contabo42').count(' OR ') == 3
    assert "status IN ('running','claimed') AND runner_host='contabo42'" in sql
    assert 'FROM runner_host_policy' in sql and 'FROM pipeline_runner_hosts' in sql
    assert 'o.running >= o.capacity' in sql and 'o.stale' in sql
    assert 'last_seen_at >= NOW() - (180 *' in sql
    assert HEADER.split('=', 1)[0] not in sql and 'DEPLOY_ONLY' in sql
    assert 'GREATEST(p.created_at' in sql
    assert 'COALESCE(dep.completed_at, dep.updated_at)' in sql
    assert "dep.status='done'" in sql
    assert "NOW() - (120 * INTERVAL '1 second')" in sql
    assert 'FOR UPDATE SKIP LOCKED' in sql


def test_host_is_escaped_and_seconds_validated():
    sql = run('runner_overflow_claim_sql', "host'42", RUNNER_OVERFLOW_STALE_SEC='1);BAD',
              RUNNER_OVERFLOW_GRACE_SEC='1);BAD')
    assert "host='host''42'" in sql and "runner_host='host''42'" in sql
    assert 'BAD' not in sql
    assert '(180 *' in sql and '(120 *' in sql
    assert '(9 *' in run('runner_overflow_claim_sql', 'primary', RUNNER_OVERFLOW_GRACE_SEC='9')


def test_approved_claim_unchanged_and_scripts_identical():
    assert function(SRC, 'claim_approved_job') == function(BASE, 'claim_approved_job')
    assert run('claim_approved_job ""', 'primary') == run('claim_approved_job ""')
    assert (ROOT / 'scripts/pipeline-runner.sh').read_bytes() == (
        ROOT / 'scripts/pipeline-runner.sh.local').read_bytes()


@pytest.mark.parametrize('reason', ['full', 'stale', 'deploy_only', 'grace'])
def test_claim_reason_logged_without_polluting_claim_row(reason):
    script = "db_exec() { printf 'job-1\\036AADS\\036" + reason + "'; }; runner_overflow_db_claim SQL"
    result = subprocess.run(['bash', '-uc', STUB + HEADER + '\n' + FUNCTIONS + '\n' + script],
                            env={**os.environ, 'RUNNER_OVERFLOW_OF': 'primary'},
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'job-1\x1eAADS\n'
    assert f'CLAIM_OVERFLOW reason={reason} primary=primary' in result.stderr


def test_yield_logs_only_state_changes_and_resets_after_overflow():
    output = run(r'''
reply=$'2\03612\036yield'
db_exec() { printf '%s' "$reply"; }
runner_overflow_log_claim ''; runner_overflow_log_claim ''
reply=$'3\03612\036yield'
runner_overflow_log_claim ''; runner_overflow_log_claim ''
reply=$'12\03612\036full'
runner_overflow_log_claim ''
reply=$'3\03612\036yield'
runner_overflow_log_claim ''
''', 'primary')
    assert output.count('CLAIM_YIELD_TO_PRIMARY') == 3
    assert 'primary_running=2/12' in output
    assert output.count('primary_running=3/12') == 2


def test_disabled_logging_performs_no_query():
    assert run("db_exec() { echo unexpected; return 1; }; runner_overflow_log_claim ''") == ''


def test_empty_claim_and_query_failure_are_preserved():
    assert run('db_exec() { return 0; }; runner_overflow_db_claim SQL', 'primary') == ''
    assert run('db_exec() { return 7; }; runner_overflow_db_claim SQL; echo $? ', 'primary') == '7\n'


def test_no_queue_resets_yield_state_without_logging():
    output = run(r'''
RUNNER_OVERFLOW_YIELD_STATE=old
reply=$'0\03612\036idle'
db_exec() { printf '%s' "$reply"; }
runner_overflow_log_claim ''
printf '[%s]' "$RUNNER_OVERFLOW_YIELD_STATE"
''', 'primary')
    assert output == '[]'
