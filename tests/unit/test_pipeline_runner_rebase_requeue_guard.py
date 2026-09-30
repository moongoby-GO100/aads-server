"""stale_base 자동 rebase 재큐잉 구간의 오진 3건 교정 계약 테스트.

배경(2026-09-30 실측):
  A) runner-e3b882c2 — 재큐잉이 started_at 을 그대로 둬서 승인 대기 69분까지
     MAX_RUNTIME(7200s) 에 합산, 재큐잉 40초 만에 zombie_killed (실행시간 7710s).
  B) runner-7f5feba1 — 잘리지 않은 rebase diff(180,545B)를 `jq --arg` 로 넘겨
     MAX_ARG_STRLEN(131072B) 초과 → jq exit 126 → set -e 로 분리 워커 무성 사망
     → 워치독이 process_died 로 확정.
  C) 분리 워커가 비정상 종료해도 DB 정리·로그가 남지 않았다.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ("pipeline-runner.sh", "pipeline-runner.sh.local")


def _read_script(name: str = "pipeline-runner.sh") -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def _extract_function(script: str, name: str) -> str:
    start = script.index(f"{name}() {{")
    end = script.index("\n}\n", start) + len("\n}\n")
    return script[start:end]


def _requeue_block(script: str) -> str:
    start = script.index("새 diff 를 AI 재검수하고 CEO 재승인을 받는다.")
    end = script.index("deploy_stale_rebased_requeued", start)
    return script[start:end]


# ── 원인 A: 재큐잉 시 실행시간 시계 재시작 + 좀비 수거기 최근 진행 가드 ──────


def test_requeue_update_resets_started_at():
    for name in SCRIPTS:
        block = _requeue_block(_read_script(name))
        assert "status='running', phase='ai_review'" in block, name
        assert "started_at=NOW()" in block, name


def test_zombie_reaper_skips_recently_updated_jobs():
    for name in SCRIPTS:
        body = _extract_function(_read_script(name), "_recover_stuck_jobs")
        start = body.index("zombie_rows=$(db_exec")
        query = body[start:body.index(";\"", start)]
        assert "started_at < NOW() - INTERVAL '${MAX_RUNTIME} seconds'" in query, name
        assert "updated_at < NOW() - INTERVAL '10 minutes'" in query, name


# ── 원인 B: 재검수 본문은 argv 가 아니라 파일로 ──────────────────────────


def test_rebase_review_body_is_built_from_files_not_argv():
    for name in SCRIPTS:
        body = _extract_function(_read_script(name), "review_rebased_aads_sha")
        assert "--arg diff" not in body, name
        assert '--rawfile diff "$diff_file"' in body, name
        assert '--data-binary "@${body_file}"' in body, name
        assert '-d "$body"' not in body, name
        # jq 실패는 set -e 즉사가 아니라 처리된 실패여야 한다
        assert "if ! jq -n" in body, name
        assert "deploy_rebase_review_body_failed" in body, name
        assert "deploy_rebase_review_tmp_failed" in body, name
        assert 'rm -f "$diff_file" "$body_file"' in body, name


# ── 원인 C: 분리 워커 무성 사망 → review_hold 회수 ─────────────────────────


def test_detached_review_worker_has_exit_trap_finalizer():
    for name in SCRIPTS:
        script = _read_script(name)
        helper = _extract_function(script, "_finalize_rebase_review_worker")
        assert "status='review_hold'" in helper, name
        assert "rebase_review_worker_died" in helper, name
        assert "AND status='running'" in helper, name
        assert "runner_pid=NULL" in helper, name
        assert "status='error'" not in helper, name

        # 헬퍼는 호출부보다 먼저 정의돼야 한다
        assert script.index("_finalize_rebase_review_worker() {") < script.index("review_rebased_aads_sha() {")

        launch = script.index('review_rebased_aads_sha "$job_id" "$session_id" "$worktree_dir" "$rebased_sha"')
        window = script[launch - 400:launch + 200]
        assert "trap '_rc=$?; _finalize_rebase_review_worker" in window, name
        assert "EXIT" in window, name
        assert ") 9>&- </dev/null >/dev/null 2>&1 &" in window, name
        # 옛 형태(함수 직접 분리 실행)는 남아 있으면 안 된다
        assert '"$rebased_sha" 9>&- </dev/null' not in script, name


def test_runner_script_copies_are_byte_identical():
    a = (ROOT / "scripts" / SCRIPTS[0]).read_bytes()
    b = (ROOT / "scripts" / SCRIPTS[1]).read_bytes()
    assert a == b


# ── 동작 검증 ─────────────────────────────────────────────────────────


def test_exit_trap_recovers_silent_death_under_set_e(tmp_path):
    """set -e 로 즉사한 워커도 EXIT trap 이 finalizer 를 부른다."""
    helper = _extract_function(_read_script(), "_finalize_rebase_review_worker")
    log_file = tmp_path / "calls.log"
    harness = f"""
set -eo pipefail
CALLS={log_file}
get_job_status() {{ cat "$CALLS.status" 2>/dev/null || echo running; }}
log() {{ echo "log $*" >> "$CALLS"; }}
db_update() {{ echo "db_update $*" >> "$CALLS"; }}
record_runner_event() {{ echo "event $*" >> "$CALLS"; }}
_notify_ai() {{ echo "notify $1" >> "$CALLS"; }}
promote_next_queued() {{ echo "promote $1" >> "$CALLS"; }}
{helper}
review_rebased_aads_sha() {{
    false   # jq exit 126 과 같은 처리되지 않은 실패
    echo "unreachable" >> "$CALLS"
}}
job_id=runner-test1234 session_id=sess
(
    trap '_rc=$?; _finalize_rebase_review_worker "'"$job_id"'" "'"$session_id"'" "$_rc"; exit $_rc' EXIT
    review_rebased_aads_sha "$job_id" "$session_id" /nowhere deadbeef
) </dev/null >/dev/null 2>&1 &
wait $! || true
"""
    subprocess.run(["bash", "-c", harness], check=True, timeout=30)
    calls = log_file.read_text(encoding="utf-8")
    assert "unreachable" not in calls
    assert "REBASE_REVIEW_WORKER_DIED job=runner-test1234 rc=1" in calls
    assert "status='review_hold'" in calls
    assert "rebase_review_worker_died: rc=1" in calls
    assert "notify runner-test1234" in calls
    assert "promote AADS" in calls


def test_exit_trap_is_noop_for_handled_failure(tmp_path):
    """함수가 스스로 상태를 바꾼 실패(running 아님)는 건드리지 않는다."""
    helper = _extract_function(_read_script(), "_finalize_rebase_review_worker")
    log_file = tmp_path / "calls.log"
    (tmp_path / "calls.log.status").write_text("error\n", encoding="utf-8")
    harness = f"""
set -eo pipefail
CALLS={log_file}
get_job_status() {{ cat "$CALLS.status"; }}
log() {{ echo "log $*" >> "$CALLS"; }}
db_update() {{ echo "db_update $*" >> "$CALLS"; }}
record_runner_event() {{ :; }}
_notify_ai() {{ :; }}
promote_next_queued() {{ :; }}
{helper}
review_rebased_aads_sha() {{ return 1; }}
job_id=runner-test1234 session_id=sess
(
    trap '_rc=$?; _finalize_rebase_review_worker "'"$job_id"'" "'"$session_id"'" "$_rc"; exit $_rc' EXIT
    review_rebased_aads_sha "$job_id" "$session_id" /nowhere deadbeef
) </dev/null >/dev/null 2>&1 &
wait $! || true
"""
    subprocess.run(["bash", "-c", harness], check=True, timeout=30)
    assert not log_file.exists() or "db_update" not in log_file.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq 미설치")
def test_e2big_regression_arg_fails_rawfile_succeeds(tmp_path):
    payload = "d" * (140 * 1024)  # MAX_ARG_STRLEN(131072B) 초과
    diff_file = tmp_path / "diff"
    diff_file.write_text(payload, encoding="utf-8")
    env = dict(os.environ)

    try:
        arg = subprocess.run(
            ["jq", "-n", "--arg", "diff", payload, "{diff:$diff}"],
            capture_output=True, timeout=30, env=env,
        )
        arg_failed = arg.returncode != 0
    except OSError:  # E2BIG — exec 자체가 거부됨(셸에서는 exit 126)
        arg_failed = True
    assert arg_failed

    raw = subprocess.run(
        ["jq", "-n", "--rawfile", "diff", str(diff_file), "{diff:$diff}|.diff|length"],
        capture_output=True, timeout=30, env=env, text=True,
    )
    assert raw.returncode == 0, raw.stderr
    assert int(raw.stdout.strip()) == len(payload)
