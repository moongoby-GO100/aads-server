"""GO100 개발 작업 claim 은 허용목록 호스트(기본 contabo92)만 — 실제 작업 행으로 판정한다.

단위시험 컨테이너에는 psql 도 PG* 환경변수도 없다. 그래서 claim SQL 은 bash 하네스가 렌더링만 하고
(db_exec 대역이 SQL 텍스트를 파일에 적는다 — psql 을 부르지 않는다), 그 SQL 을 asyncpg 로 **일회용 DB**
`go100_dev_host_<난수>` 에서 실행한다. 운영 DB 의 pipeline_jobs 는 읽지도 쓰지도 않는다.

접속: GOAL_GRANT_TEST_ADMIN_URL (CREATEDB 가능, 예: postgresql://…@host.docker.internal:5433/postgres).
test_goal_grant_pg_semantics.py 와 같은 변수이며 run_unit_tests.sh 가 이미 -e 로 넘긴다.
변수가 없으면 skip 한다(사유: claim 검증 미실행). 변수를 준 실행에서 skip 은 0건이어야 한다.

- SQL 오류(asyncpg 예외)는 삼키지 않고 시험 실패로 둔다.
- "가져가지 않음" 단언은 같은 조건에서 가져가는 대조 행(AADS)이 실제로 잡혔음을 함께 확인한다.
- "변경 전" 기준은 고정 SHA(BASELINE_SHA)다. HEAD 가 아니다 — 커밋 뒤에도 같은 시험이 통과해야 한다.
"""
import asyncio
import json
import os
import re
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

# 이 작업(runner-2a73ea74)을 시작한 origin/main. 전후 비교의 "변경 전" 스크립트.
BASELINE_SHA = '356788586a10c476279a1d37b6ec6417d414c1ae'
ADMIN_URL = os.getenv('GOAL_GRANT_TEST_ADMIN_URL', '')

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / 'scripts/pipeline-runner.sh'
LOCAL = ROOT / 'scripts/pipeline-runner.sh.local'

FUNCS = ['sql_escape', 'runner_is_deploy_delegate', 'runner_pool_host_scope_sql',
         'runner_pool_claim_extra_sql', 'runner_overflow_primary_sql',
         'runner_overflow_condition_sql', 'runner_overflow_reason_sql',
         'runner_overflow_claim_sql', 'runner_overflow_db_claim',
         'go100_dev_runner_hosts', 'go100_dev_host_allowed', 'go100_dev_skip_note',
         'go100_dev_claim_sql', '_claim_queued_job']

HEADER = 'DEPLOY_ONLY: true\nTASK_ID: X\n'
SEP = '\n-- @@SQL@@\n'

# claim SQL 이 읽고 쓰는 pipeline_jobs 와 보조 테이블의 최소 스키마(운영 열 중 이 SQL 이 건드리는 것만).
SCHEMA = """
CREATE TABLE pipeline_jobs (
    job_id TEXT PRIMARY KEY, chat_session_id TEXT, project TEXT, instruction TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued', phase TEXT DEFAULT 'queued',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ, error_detail TEXT, runner_host TEXT, depends_on TEXT,
    priority INTEGER, logs JSONB, max_cycles INTEGER, worker_model TEXT, model TEXT,
    size TEXT, parallel_group TEXT);
CREATE TABLE pipeline_runner_hosts (host TEXT PRIMARY KEY, max_concurrent INTEGER, last_seen_at TIMESTAMPTZ);
CREATE TABLE runner_host_policy (host TEXT PRIMARY KEY, max_concurrent INTEGER);
CREATE TABLE llm_api_keys (provider TEXT, manual_disabled BOOLEAN DEFAULT FALSE, is_active BOOLEAN DEFAULT TRUE);
CREATE TABLE pipeline_runner_events (job_id TEXT, event_type TEXT);
"""

LOOP = asyncio.new_event_loop()


def _run(coro):
    return LOOP.run_until_complete(coro)


@pytest.fixture(scope='module')
def db_url():
    if not ADMIN_URL:
        pytest.skip('GOAL_GRANT_TEST_ADMIN_URL 없음 — claim 검증 미실행 (일회용 DB 를 만들 접속 URL 필요)')
    import asyncpg

    name = 'go100_dev_host_' + uuid.uuid4().hex[:10]
    url = urlunsplit(urlsplit(ADMIN_URL)._replace(path='/' + name))

    async def create():
        admin = await asyncpg.connect(ADMIN_URL)
        try:
            await admin.execute('CREATE DATABASE "%s"' % name)
        finally:
            await admin.close()
        conn = await asyncpg.connect(url)
        try:
            await conn.execute(SCHEMA)
        finally:
            await conn.close()

    async def drop():
        admin = await asyncpg.connect(ADMIN_URL)
        try:
            await admin.execute('DROP DATABASE IF EXISTS "%s" WITH (FORCE)' % name)
        finally:
            await admin.close()

    _run(create())
    try:
        yield url
    finally:
        _run(drop())


@pytest.fixture
def db_required(db_url):
    globals()['_DB_URL'] = db_url
    yield


needs_db = pytest.mark.usefixtures('db_required')


def _baseline_text():
    """고정 SHA 의 스크립트. 읽을 수 없으면 실패한다(skip·비교 생략 없음)."""
    r = subprocess.run(['git', 'show', f'{BASELINE_SHA}:scripts/pipeline-runner.sh'], cwd=ROOT,
                       capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout:
        pytest.fail(f'BASELINE_SHA {BASELINE_SHA} 를 읽을 수 없음: {r.stderr.strip()} '
                    f'(얕은 클론이면 `git fetch origin {BASELINE_SHA}` 후 다시 실행)')
    return r.stdout


def _defs(text):
    out = [re.search(r'^DEPLOY_ONLY_HEADER_SQL=.*$', text, re.M).group()]
    for name in FUNCS:
        m = (re.search(r'^%s\(\) \{ .*\}$' % re.escape(name), text, re.M)
             or re.search(r'^%s\(\) \{\n.*?^\}' % re.escape(name), text, re.M | re.S))
        if m:
            out.append(m.group())
    return '\n'.join(out)


# psql 을 부르지 않는다. db_exec 는 받은 SQL 을 파일에 적기만 한다.
# go100_dev_skip_note 가 쓰는 count(*) 조회만 "대기 작업 있음"(1)으로 답해 UPDATE 분기까지 렌더링되게 한다.
HARNESS = r'''
set -o pipefail
RUNNER_ENGINE_MODE=general RUNNER_ANTIGRAVITY_EXEC=unused
timeout() { return "${HARNESS_TIMEOUT_RC:-0}"; }
codex() { :; }
log() { echo "LOG: $*"; }
record_runner_event() { :; }
db_update() { :; }
db_exec() {
    printf '%s\n-- @@SQL@@\n' "$1" >> "$SQL_OUT"
    case "$1" in "SELECT count(*) FROM pipeline_jobs p WHERE p.status='queued'"*) echo 1;; esac
}
'''


def _insert_sql(rows):
    sql = []
    for job_id, project, instruction, *rest in rows:
        age = rest[0] if rest else 0
        detail = "$q$%s$q$" % rest[1] if len(rest) > 1 else 'NULL'
        sql.append("INSERT INTO pipeline_jobs (job_id, chat_session_id, project, instruction, status, phase, created_at, error_detail) "
                   "VALUES ('%s','sess','%s',$q$%s$q$,'queued','queued', NOW() - INTERVAL '%d minutes', %s);"
                   % (job_id, project, instruction, age, detail))
    return sql


class Result:
    def __init__(self, proc, sqls, ids, details):
        self.stdout, self.stderr, self.returncode = proc.stdout, proc.stderr, proc.returncode
        self.sqls, self.ids, self.details = sqls, ids, details


async def _exec(url, rows, sqls):
    """한 트랜잭션: 시험 행 삽입 → 렌더링된 SQL 순서대로 실행 → 상태 수집 → ROLLBACK. 예외는 그대로 올린다."""
    import asyncpg
    conn = await asyncpg.connect(url)
    try:
        tr = conn.transaction()
        await tr.start()
        try:
            for stmt in _insert_sql(rows):
                await conn.execute(stmt)
            ids = []
            for sql in sqls:
                got = await conn.fetch(sql)
                if sql.lstrip().upper().startswith('UPDATE'):
                    ids += [r[0] for r in got]
            det = {r['job_id']: (r['error_detail'] or '') for r in await conn.fetch(
                'SELECT job_id, error_detail FROM pipeline_jobs ORDER BY job_id')}
            return ids, det
        finally:
            await tr.rollback()
    finally:
        await conn.close()


def claim(tmp_path, host, rows, text=None, overflow='', call='_claim_queued_job ""', **env):
    sql_out = Path(tmp_path) / 'sql_out.txt'
    sql_out.write_text('')
    code = HARNESS + _defs(text or SRC.read_text()) + '\n' + call + '\n'
    proc = subprocess.run(['bash', '-c', code], capture_output=True, text=True, timeout=120,
                          env={**os.environ, 'RUNNER_HOST_NAME': host, 'RUNNER_OVERFLOW_OF': overflow,
                               'SQL_OUT': str(sql_out), 'LOG_DIR': str(tmp_path), 'MAX_CONCURRENT_PER_PROJECT': '6',
                               'RUNNER_DEPLOY_DELEGATE_HOST': '', 'RUNNER_CAPACITY_SCOPE': '',
                               'GO100_DEV_RUNNER_HOSTS': '', **env})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    sqls = [x.strip() for x in sql_out.read_text().split('-- @@SQL@@') if x.strip()]
    try:
        ids, det = _run(_exec(globals()['_DB_URL'], rows, sqls))
    except Exception as exc:  # SQL 오류를 삼키면 거짓 통과다 — 시험을 실패시킨다.
        raise AssertionError('SQL 오류가 났다(거짓 통과 방지): %s: %s' % (type(exc).__name__, exc)) from exc
    (Path(tmp_path) / 'details.json').write_text(json.dumps(det))
    return Result(proc, sqls, ids, det)


def claimed(proc):
    return proc.ids


def details(tmp_path):
    """마지막 claim 직후 TEMP 상태(트랜잭션 안)의 job_id → error_detail."""
    return json.loads((Path(tmp_path) / 'details.json').read_text())


def not_taken(tmp_path, host, rows, **kw):
    """host 가 rows 를 가져가지 않음을 증명한다.

    같은 트랜잭션에 대조 행(AADS, 어느 호스트나 가져감)을 더 늦은 created_at 으로 넣는다.
    rows 가 더 오래돼 먼저 잡힐 순서인데 대조 행만 잡혔다면 쿼리가 실제로 돌았고 rows 가 걸러진 것이다.
    SQL 오류면 claim() 이 이미 실패한다.
    """
    ctl = 'ctl-%s' % host
    got = claimed(claim(tmp_path, host, [(j, p, i, 30) for j, p, i, *_ in rows] + [(ctl, 'AADS', 'TASK_CTL\n', 0)], **kw))
    assert got == [ctl], 'host=%s 가 %s 를 가져갔거나 쿼리가 돌지 않았다' % (host, got)


def test_runner_scripts_identical():
    assert SRC.read_bytes() == LOCAL.read_bytes()


GENERAL = ('GO100', 'TASK_ID: A\nTITLE: 개발\n')
REWORK = ('GO100', 'TASK_ID: B\nREWORK_OF: runner-1\n')
MENTION = ('GO100', 'TASK_ID: C\n본문에 contabo14 가 들어간 일반 작업\n')
DEPLOY = ('GO100', HEADER + 'TITLE: 배포\n')
PROSE = ('GO100', 'TASK_ID: E\n' + '\n' * 25 + 'DEPLOY_ONLY: true 는 본문 산문\n')


@needs_db
@pytest.mark.parametrize('label,row', [('a_general', GENERAL), ('b_rework', REWORK), ('c_mention', MENTION)])
def test_dev_jobs_only_allowlist_host(tmp_path, label, row):
    rows = [('t-' + label, *row)]
    not_taken(tmp_path, 'contabo14', rows)
    not_taken(tmp_path, 'contabo42', rows)
    assert claimed(claim(tmp_path, 'contabo92', rows)) == ['t-' + label]


@needs_db
def test_d_deploy_only_header_still_claimed_by_14(tmp_path):
    rows = [('t-d', *DEPLOY)]
    assert claimed(claim(tmp_path, 'contabo14', rows)) == ['t-d']
    assert claimed(claim(tmp_path, 'contabo92', rows)) == ['t-d']


@needs_db
def test_e_deploy_only_in_body_is_not_release_job(tmp_path):
    rows = [('t-e', *PROSE)]
    not_taken(tmp_path, 'contabo14', rows)
    assert claimed(claim(tmp_path, 'contabo92', rows)) == ['t-e']


@needs_db
def test_f_host_outside_list_never_takes_dev(tmp_path):
    rows = [('t-f', *GENERAL)]
    for host in ('contabo42', 'contabo116', 'other'):
        not_taken(tmp_path, host, rows)
    assert claimed(claim(tmp_path, 'contabo92', rows)) == ['t-f']


@needs_db
@pytest.mark.parametrize('project', ['KIS', 'AADS'])
@pytest.mark.parametrize('host', ['contabo14', 'contabo92', 'contabo42'])
def test_g_other_projects_unchanged_vs_baseline(tmp_path, project, host):
    rows = [('t-g', project, 'TASK_ID: G\n개발\n')]
    assert claimed(claim(tmp_path, host, rows)) == ['t-g']
    base = _baseline_text()
    assert claimed(claim(tmp_path, host, rows, text=base)) == ['t-g']


@needs_db
def test_h_overflow_fallback_does_not_take_go100_dev(tmp_path):
    # 92 러너 없음(pipeline_runner_hosts 비어 stale) → 기존에는 14 가 폴백으로 가져갔다.
    rows = [('t-h', *GENERAL)]
    base = _baseline_text()
    assert claimed(claim(tmp_path, 'contabo14', rows, text=base, overflow='contabo92')) == ['t-h']
    not_taken(tmp_path, 'contabo14', rows, overflow='contabo92')
    # 폴백 경로 자체는 살아 있다: AADS 작업과 GO100 DEPLOY_ONLY 는 가져간다.
    assert claimed(claim(tmp_path, 'contabo14', [('t-h2', 'AADS', 'TASK_ID: H\n')], overflow='contabo92')) == ['t-h2']
    assert claimed(claim(tmp_path, 'contabo14', [('t-h3', *DEPLOY)], overflow='contabo92')) == ['t-h3']


@needs_db
def test_empty_env_means_default_not_unrestricted(tmp_path):
    rows = [('t-i', *GENERAL)]
    not_taken(tmp_path, 'contabo14', rows, GO100_DEV_RUNNER_HOSTS='')
    not_taken(tmp_path, 'contabo14', rows, GO100_DEV_RUNNER_HOSTS=' , ')
    assert claimed(claim(tmp_path, 'contabo92', rows, GO100_DEV_RUNNER_HOSTS='')) == ['t-i']


@needs_db
def test_multiple_hosts_list(tmp_path):
    rows = [('t-j', *GENERAL)]
    env = {'GO100_DEV_RUNNER_HOSTS': 'contabo92, contabo77'}
    assert claimed(claim(tmp_path, 'contabo77', rows, **env)) == ['t-j']
    not_taken(tmp_path, 'contabo14', rows, **env)


@needs_db
def test_skip_log_once_per_interval_and_error_detail(tmp_path):
    rows = [('t-old', *GENERAL, 60), ('t-new', *REWORK, 1)]
    first = claim(tmp_path, 'contabo14', rows)
    assert claimed(first) == []
    logs = [ln for ln in first.stderr.splitlines() if 'GO100_DEV_CLAIM_SKIPPED' in ln]
    assert len(logs) == 1 and 'host=contabo14' in logs[0] and 'allowed=contabo92' in logs[0]
    again = claim(tmp_path, 'contabo14', rows)  # 같은 stamp 디렉터리 → 간격 안이라 로그 없음
    assert 'GO100_DEV_CLAIM_SKIPPED' not in again.stderr
    # 대기 표시: 15분 넘은 작업만, status 는 건드리지 않고 error_detail 만
    fresh = tmp_path / 'fresh'
    fresh.mkdir()
    claim(fresh, 'contabo14', rows, call='go100_dev_skip_note')
    d = details(fresh)
    assert d.get('t-old') == 'waiting_for_go100_dev_runner_host:contabo92'
    assert d.get('t-new') == ''


@needs_db
def test_i_wait_reason_cleared_when_92_claims(tmp_path):
    wait = 'waiting_for_go100_dev_runner_host:contabo92'
    rows = [('t-wait', *GENERAL, 60, wait)]
    assert claimed(claim(tmp_path, 'contabo92', rows)) == ['t-wait']
    assert details(tmp_path)['t-wait'] == ''


@needs_db
def test_j_other_error_detail_preserved(tmp_path):
    rows = [('t-keep', *GENERAL, 60, 'review_failed: 점수 0.5')]
    assert claimed(claim(tmp_path, 'contabo92', rows)) == ['t-keep']
    assert details(tmp_path)['t-keep'] == 'review_failed: 점수 0.5'


@needs_db
def test_i_wait_reason_cleared_on_general_claim_path(tmp_path):
    # 일반 claim 분기: 허용 호스트가 아닌 러너가 DEPLOY_ONLY 를 가져갈 때 대기 문구만 지워진다.
    wait = 'waiting_for_go100_dev_runner_host:contabo92'
    rows = [('t-wd', *DEPLOY, 5, wait)]
    assert claimed(claim(tmp_path, 'contabo14', rows)) == ['t-wd']
    assert details(tmp_path)['t-wd'] == ''


@needs_db
def test_i_wait_reason_cleared_on_fallback_claim_path(tmp_path):
    # 폴백 UPDATE 분기(HARNESS_TIMEOUT_RC=1, AGY 회피 해제)에서도 같은 규칙.
    env = {'HARNESS_TIMEOUT_RC': '1', 'AGY_AVOID_ONLY_HOSTS': ''}
    wait = 'waiting_for_go100_dev_runner_host:contabo92'
    cleared = tmp_path / 'cleared'
    cleared.mkdir()
    proc = claim(cleared, 'contabo42', [('t-fw', *DEPLOY, 5, wait)], **env)
    assert 'p.updated_at <= NOW()' in proc.sqls[-1], '폴백 분기 SQL 이 아니다'
    assert claimed(proc) == ['t-fw']
    assert details(cleared)['t-fw'] == ''
    kept = tmp_path / 'kept'
    kept.mkdir()
    proc = claim(kept, 'contabo42', [('t-fk', *DEPLOY, 5, 'review_failed: 점수 0.5')], **env)
    assert 'p.updated_at <= NOW()' in proc.sqls[-1], '폴백 분기 SQL 이 아니다'
    assert claimed(proc) == ['t-fk']
    assert details(kept)['t-fk'] == 'review_failed: 점수 0.5'


def test_both_claim_updates_clear_wait_reason():
    text = SRC.read_text()
    clause = "error_detail = CASE WHEN starts_with(error_detail, 'waiting_for_go100_dev_runner_host:') THEN NULL ELSE error_detail END"
    assert text.count(clause) == 2
    assert "LIKE 'waiting_for_go100_dev_runner_host" not in text
    assert text.count("SET status='claimed', runner_host='${RUNNER_HOST_NAME}'") == 2


@needs_db
def test_allowlist_host_adds_no_sql(tmp_path):
    proc = claim(tmp_path, 'contabo92', [], call='go100_dev_claim_sql')
    assert proc.stdout.strip() == ''


@needs_db
def test_sql_error_is_not_swallowed(tmp_path):
    """잘못된 SQL 이 통과로 숨지 않는다(asyncpg 예외 → 시험 실패)."""
    with pytest.raises(AssertionError, match='SQL 오류'):
        claim(tmp_path, 'contabo92', [], call='db_exec "SELECT * FROM no_such_table_zz;"')


@needs_db
def test_agy_fallback_claim_path_same_rules(tmp_path):
    """AGY 폴백(fallback_enabled) 쪽 claim SQL 도 같은 허용목록을 따른다."""
    env = {'HARNESS_TIMEOUT_RC': '1', 'AGY_AVOID_ONLY_HOSTS': ''}
    rows = [('t-fb', *GENERAL)]
    not_taken(tmp_path, 'contabo42', rows, **env)
    assert 'p.updated_at <= NOW()' in claim(tmp_path, 'contabo42', rows, **env).sqls[-1], '폴백 분기 SQL 이 아니다'
    assert claimed(claim(tmp_path, 'contabo92', rows, **env)) == ['t-fb']
    assert claimed(claim(tmp_path, 'contabo42', [('t-fb2', *DEPLOY)], **env)) == ['t-fb2']


LOOKALIKES = ['waiting-for-go100-dev-runner-host:contabo92', 'waitingXforXgo100XdevXrunnerXhost:contabo92']


@needs_db
@pytest.mark.parametrize('value', LOOKALIKES)
def test_lookalike_error_detail_preserved_on_general_path(tmp_path, value):
    # `_` 가 LIKE 와일드카드로 동작하면 비슷한 값까지 지워진다 — 정확한 접두어만 지워야 한다.
    assert claimed(claim(tmp_path, 'contabo14', [('t-la', *DEPLOY, 5, value)])) == ['t-la']
    assert details(tmp_path)['t-la'] == value


@needs_db
@pytest.mark.parametrize('value', LOOKALIKES)
def test_lookalike_error_detail_preserved_on_fallback_path(tmp_path, value):
    env = {'HARNESS_TIMEOUT_RC': '1', 'AGY_AVOID_ONLY_HOSTS': ''}
    proc = claim(tmp_path, 'contabo42', [('t-lf', *DEPLOY, 5, value)], **env)
    assert 'p.updated_at <= NOW()' in proc.sqls[-1], '폴백 분기 SQL 이 아니다'
    assert claimed(proc) == ['t-lf']
    assert details(tmp_path)['t-lf'] == value
