"""러너의 DB 쓰기 실패가 조용히 지나가지 않는지 지킨다.

2026-09-16, `runner-1bf4a718`(AADS-AAG-001-R2, 33분짜리 L 작업)이 이렇게 사라졌다.

    06:02:05  AI_REVIEW_HOLD job=runner-1bf4a718 status=review_hold ...
    06:03:02  WATCHDOG_DEAD_PROCESS: job=runner-1bf4a718 pid=1026069 — error로 전환

행을 열어보면 원인이 보인다. `review_feedback` 에 `[AI Reviewer]` 줄이 없고
watchdog 줄만 있으며 `result_output`·`git_diff` 가 0바이트다 — review_hold
UPDATE 가 통째로 실패했고, 행은 `status='running'` 인 채 남아 60초 뒤
watchdog 에 잡혔다.

실패가 보이지 않은 이유는 두 겹이었다.
1. `psql` 은 `ON_ERROR_STOP` 없이는 SQL 이 실패해도 종료코드 0 을 돌려준다.
2. `db_update()` 가 stdout·stderr 를 둘 다 `/dev/null` 로 버렸다.

원인을 고치는 것과 별개로, **다음에 또 실패하면 로그에 남아야 한다.**
"""

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ("pipeline-runner.sh", "pipeline-runner.sh.local")


def _read_script(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def test_db_update_stops_on_sql_error_and_logs_failure():
    for name in SCRIPTS:
        script = _read_script(name)
        start = script.index("db_update() {")
        end = script.index("\n}", start)
        body = script[start:end]

        # psql 이 SQL 오류에서 0 을 돌려주면 실패는 영원히 안 보인다.
        assert "-v ON_ERROR_STOP=1" in body, f"{name}: db_update 가 ON_ERROR_STOP 없이 psql 을 부른다"
        # stderr 를 버리지 않고 실패를 남긴다.
        assert "DB_UPDATE_FAILED" in body, f"{name}: db_update 가 실패를 로그에 남기지 않는다"
        assert ">/dev/null 2>&1" not in body, f"{name}: db_update 가 아직 출력을 통째로 버린다"


def test_db_update_never_fails_the_runner():
    """이 스크립트는 `set -e` 로 돈다. db_update 가 실패를 반환하면
    DB 한 줄 때문에 러너 전체가 죽는다. 보이게 하되 죽이지는 않는다."""
    for name in SCRIPTS:
        script = _read_script(name)
        assert "set -eo pipefail" in script, f"{name}: set -e 전제가 바뀌었다 — 이 테스트를 다시 보라"

        start = script.index("db_update() {")
        end = script.index("\n}", start)
        body = script[start:end]

        assert "return 0" in body, f"{name}: db_update 가 0 이 아닌 값을 반환할 수 있다"


def test_review_hold_verifies_the_write_landed():
    """review_hold 전환은 payload(result_output·git_diff)를 통째로 싣는다.
    그게 실패하면 watchdog 이 60초 안에 process_died 로 덮어쓴다.
    적용 여부를 읽어 확인하고, 아니면 payload 없이 한 번 더 걸어야 한다."""
    for name in SCRIPTS:
        script = _read_script(name)

        hold = script.index("AI_REVIEW_HOLD")
        event = script.index('record_runner_event "$job_id" "job_terminal" "$review_hold_status"')
        window = script[hold:event]

        assert "REVIEW_HOLD_WRITE_MISSED" in window, f"{name}: review_hold 쓰기 확인이 없다"
        assert "SELECT status FROM pipeline_jobs WHERE job_id=" in window, f"{name}: 상태 읽기 확인이 없다"
        # 재시도 문장에는 큰 payload 가 없어야 한다 — 그게 실패의 원인이었다.
        retry = window[window.index("REVIEW_HOLD_WRITE_MISSED"):]
        assert "result_output=" not in retry, f"{name}: 재시도가 다시 payload 를 싣는다"
        assert "git_diff=" not in retry, f"{name}: 재시도가 다시 diff 를 싣는다"


def test_sql_escape_drops_invalid_utf8_before_postgres():
    """head -c가 한글을 중간 절단해도 PostgreSQL UPDATE 전체가 실패하면 안 된다."""
    for name in SCRIPTS:
        script = _read_script(name)
        start = script.index("sql_escape() {")
        end = script.index("\n}", start)
        body = script[start:end]

        assert "iconv -f UTF-8 -t UTF-8 -c" in body, f"{name}: DB 경계 UTF-8 정제가 없다"
        completed = subprocess.run(
            ["bash", "-c", f"{body}\n}}\nbad=$(printf 'ok\\354')\nsql_escape \"$bad\""],
            check=True,
            capture_output=True,
        )
        assert completed.stdout == b"$esc$ok$esc$\n", f"{name}: 잘린 UTF-8 바이트가 제거되지 않았다"


def test_approval_transition_verifies_write_and_retries_without_payload():
    """승인 전환 UPDATE가 실패하면 event를 쓰거나 worker가 끝나기 전에 복구한다."""
    for name in SCRIPTS:
        script = _read_script(name)
        transition = script.index("SET phase='awaiting_approval'")
        event = script.index('record_runner_event "$job_id" "approval_requested"', transition)
        window = script[transition:event]

        assert "APPROVAL_WRITE_MISSED" in window, f"{name}: 승인 전환 쓰기 확인이 없다"
        assert "SELECT status FROM pipeline_jobs WHERE job_id=" in window
        assert "approval_state_persist_failed" in window
        assert "runner_pid=NULL" in window
        retry = window[window.index("APPROVAL_WRITE_MISSED"):]
        assert "result_output=" not in retry, f"{name}: 승인 전환 재시도가 output을 다시 싣는다"
        assert "git_diff=" not in retry, f"{name}: 승인 전환 재시도가 diff를 다시 싣는다"
