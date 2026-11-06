"""watchdog 는 자기 서버가 가져간 작업의 runner_pid 만 확인·종료한다(2026-11-04 runner-9b48ed0c)."""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (ROOT / "scripts/pipeline-runner.sh").read_text(encoding="utf-8")


def _fn(name):
    i = SCRIPT.index(f"{name}() {{")
    return SCRIPT[i:SCRIPT.index("\n}\n", i) + 3]


def test_own_jobs_sql():
    out = subprocess.run(["bash", "-c", _fn("runner_own_jobs_sql") + "\nrunner_own_jobs_sql"],
                         env={**os.environ, "RUNNER_HOST_NAME": "contabo42"}, capture_output=True, text=True).stdout
    assert out == "AND (runner_host IS NULL OR runner_host = 'contabo42')"


def test_pid_based_queries_are_host_scoped():
    wd = _fn("_watchdog_check")
    assert "$filter $(runner_own_jobs_sql)\n                         RETURNING job_id;" in wd
    assert "WHERE status='running' AND runner_pid IS NOT NULL\n                          $filter $(runner_own_jobs_sql);" in wd
    z = SCRIPT[SCRIPT.index("zombie_rows=$(db_exec"):]
    z = z[:z.index('2>/dev/null) || true')]
    assert "$filter $(runner_own_jobs_sql);" in z
    assert (ROOT / "scripts/pipeline-runner.sh.local").read_text(encoding="utf-8") == SCRIPT


def _run_helpers(body, *names):
    source = '\n'.join(_fn(name) for name in names)
    result = subprocess.run(['bash', '-euc', source + '\n' + body],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_slot_wait_logs_events_and_limits_each_job_for_five_minutes():
    output = _run_helpers(r'''
declare -A _runner_slot_wait_at=()
MAX_CONCURRENT_SERVER=3
RUNNER_ENGINE_MODE=general
runner_pool_host_scope_sql() { echo "AND r.runner_host='here'"; }
db_exec() {
    [[ "$1" == *"dep.status='done'"* ]]
    [[ "$1" == *"h.running >= h.lim"* ]]
    printf 'runner-aa\036GO100\0362\0363\0363\036runner-cc:66,runner-dd:30\n'
    printf 'runner-bb\036GO100\0362\0363\0363\036runner-cc:66,runner-dd:30\n'
}
record_runner_event() { printf 'EVENT %s %s %s\n' "$1" "$2" "${9}"; }
log() { echo "$*"; }
SECONDS=0
runner_record_slot_wait ''
SECONDS=299
runner_record_slot_wait ''
SECONDS=300
runner_record_slot_wait ''
''', 'runner_record_slot_wait')
    assert output.count('SLOT_WAIT project=GO100 queued=2 running=3/3') == 4
    assert output.count('EVENT runner-aa queue_wait') == 2
    assert output.count('EVENT runner-bb queue_wait') == 2
    assert 'holders=runner-cc:66,runner-dd:30' in output
    assert '"reason":"slot_full"' in output
    assert '"holders":["runner-cc:66","runner-dd:30"]' in output


def test_dead_worker_recovery_is_fenced_and_notifies_only_updated_jobs():
    output = _run_helpers(r'''
RUNNER_HOSTNAME=here
sql_escape() { printf "'%s'" "$1"; }
db_exec() {
    if [[ "$1" == SELECT* ]]; then
        [[ "$1" == *"runner_host='here'"* ]]
        [[ "$1" == *"INTERVAL '10 minutes'"* ]]
        printf 'runner-aa\036999991\036here\036GO100\036session\036running\036false\n'
        printf 'runner-bb\036999992\036here\036GO100\036session\036claimed\036true\n'
        printf 'runner-cc\036999993\036here\036GO100\036session\036claimed\036true\n'
        printf 'runner-ac\036999993\036here\036GO100\036session\036running\036false\n'
        printf 'runner-dd\036\036here\036GO100\036session\036running\036false\n'
        printf 'runner-ee\036999991\036peer\036GO100\036session\036running\036false\n'
        printf 'runner-ff\036unknown\036here\036GO100\036session\036claimed\036true\n'
        printf 'runner-ab\036999991\036here\036GO100\036session\036running\036false\n'
    else
        [[ "$1" == *"error_detail='stale_recovered'"* ]]
        [[ "$1" == *"[Runner 워커 소멸 복구]"* ]]
        [[ "$1" == *"runner_host='here' AND runner_pid="* ]]
        [[ "$1" == *"job_id='runner-ab'"* ]] || echo changed
    fi
}
# No process probes or signals are executed by this test.
runner_worker_dead() { [[ "$1" != 999993 ]]; }
log() { echo "$*"; }
post_to_chat() { echo CHAT; }
record_runner_event() { echo "EVENT $1 ${9}"; }
_notify_ai() { echo "NOTIFY $1"; }
promote_next_queued() { echo "PROMOTE $1"; }
runner_recover_dead_workers ''
''', 'runner_recover_dead_workers')
    assert output.count('RECOVERED dead worker') == 2
    assert 'reason=worker_pid_dead' in output
    assert 'reason=claimed_unstarted_10m' in output
    assert output.count('PROMOTE GO100') == 2
    assert output.count('NOTIFY ') == 2
    for job in ('runner-cc', 'runner-ac', 'runner-dd', 'runner-ee', 'runner-ff', 'runner-ab'):
        assert job not in output


def test_worker_probe_is_fail_safe_without_executing_signals():
    output = _run_helpers(r'''
# Override the Bash builtin: these probes never reach the OS.
kill() { return 0; }
if runner_worker_dead 999999999; then echo bad; else echo alive; fi
kill() { return 1; }
if runner_worker_dead unknown; then echo bad; else echo unknown; fi
if runner_worker_dead 999999999; then echo dead; fi
if runner_worker_dead 1; then echo bad; else echo proc_present; fi
''', 'runner_worker_dead')
    assert output == 'alive\nunknown\ndead\nproc_present\n'


def test_early_recovery_runs_before_capacity_throttle():
    main = _fn('main')
    loop = main[main.index('while true; do'):]
    assert loop.index('runner_recover_dead_workers') < loop.index('if [[ -z "${MAX_CONCURRENT_SERVER:-}"')
    assert loop.index('runner_record_slot_wait') < loop.index('if [[ -z "${MAX_CONCURRENT_SERVER:-}"')
    assert "AND updated_at < NOW() - INTERVAL '60 minutes'" in SCRIPT
    assert "deploy_timeout" in SCRIPT


def test_no_slot_wait_on_empty_result_or_database_failure():
    assert _run_helpers(r'''
declare -A _runner_slot_wait_at=()
MAX_CONCURRENT_SERVER=3
runner_pool_host_scope_sql() { :; }
record_runner_event() { echo unexpected; }
log() { echo unexpected; }
db_exec() { :; }
runner_record_slot_wait ''
db_exec() { return 1; }
runner_record_slot_wait ''
''', 'runner_record_slot_wait') == ''


def test_slot_wait_legacy_host_and_global_scopes_match_throttle():
    for scope in ('host', 'global'):
        output = _run_helpers(r'''
declare -A _runner_slot_wait_at=()
RUNNER_HOST_NAME=here
unset MAX_CONCURRENT_SERVER
runner_pool_host_scope_sql() { :; }
sql_escape() { printf "'%s'" "$1"; }
db_exec() { printf '%s' "$1" >&2; }
record_runner_event() { :; }
log() { :; }
''' + f'RUNNER_THROTTLE_SCOPE={scope}\n' + r'''
# Query output captured separately because this case only checks SQL generation.
db_exec() { printf '%s' "$1" > "$query"; }
query=$(mktemp)
runner_record_slot_wait ''
cat "$query"
rm "$query"
''', 'runner_record_slot_wait')
        assert ("AND r.runner_host='here'" in output) == (scope == 'host')
        assert "r.project=p.project" in output


def test_slot_wait_filters_unsafe_holders_without_jq():
    output = _run_helpers(r'''
declare -A _runner_slot_wait_at=()
MAX_CONCURRENT_SERVER=3
runner_pool_host_scope_sql() { :; }
# Any accidental jq dependency must fail even on hosts where it is installed.
jq() { echo unexpected-jq >&2; return 127; }
db_exec() {
    printf 'runner-aa\036GO100\0361\0363\0363\036ok_job-1:02,bad"job:3,job:-1,job:1:2,,space job:2,job:4\\x,valid:0\n'
}
record_runner_event() { printf '%s\n' "${9}"; }
log() { :; }
runner_record_slot_wait ''
''', 'runner_record_slot_wait')
    assert output == '{"reason":"slot_full","holders":["ok_job-1:02","valid:0"]}\n'
