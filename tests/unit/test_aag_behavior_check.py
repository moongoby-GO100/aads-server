"""AAG L3 행위 점검기 단위테스트.

2026-09-16 실제 사고 3건을 합성 데이터로 고정한다.
이 테스트가 깨지면 "사고가 났는데 점검기가 조용한" 상태가 된다 — 0건 가드와 같은 이유로
가장 위험한 실패 모드다.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import importlib.util

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "aag_behavior_check", ROOT / "tools" / "aag" / "behavior_check.py"
)
bc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bc)


def _at(minute: int) -> datetime:
    return datetime(2026, 9, 16, 1, minute, tzinfo=UTC)


# ── psql 파싱 (\x1e 함정 회귀) ──────────────────────────────────────────


def test_record_separator_is_not_treated_as_a_line_break():
    """실측 버그: splitlines() 는 \\x1e 를 줄바꿈으로 봐서 4행 2열을 8행 1열로 만든다."""
    text = "contabo14\x1e2026-09-16 11:21:10+09\nrfree-0009\x1e2026-09-16 11:25:43+09\n"

    rows = bc.parse_psql_output(text)

    assert len(rows) == 2
    assert rows[0] == ["contabo14", "2026-09-16 11:21:10+09"]
    assert rows[1][0] == "rfree-0009"


def test_parse_skips_blank_lines():
    assert bc.parse_psql_output("\n\n  \n") == []


# ── 규칙 1: 재시작으로 버려진 작업 ──────────────────────────────────────


def test_requeue_after_start_reports_wasted_time():
    """GO100 runner-1791da41: 10:22:35 시작 → 10:28:51 requeue = 6분 16초 폐기."""
    events = [
        {"job_id": "runner-1791da41", "event_type": "job_started", "observed_at": _at(22), "project": "GO100"},
        {"job_id": "runner-1791da41", "event_type": "job_requeued", "observed_at": _at(28), "project": "GO100"},
    ]

    findings = bc.find_requeue_waste(events)

    assert len(findings) == 1
    assert findings[0]["rule"] == "JOB_REQUEUED_BY_RESTART"
    assert findings[0]["severity"] == "P1"
    assert findings[0]["wasted_seconds"] == 360
    assert findings[0]["project"] == "GO100"


def test_requeue_without_preceding_start_is_not_counted():
    """시작 기록이 없으면 폐기 시간을 셀 수 없다 — 추측하지 않는다."""
    events = [
        {"job_id": "j1", "event_type": "job_requeued", "observed_at": _at(28), "project": "AADS"},
    ]

    assert bc.find_requeue_waste(events) == []


def test_normal_start_without_requeue_is_silent():
    events = [
        {"job_id": "j1", "event_type": "job_started", "observed_at": _at(10), "project": "AADS"},
    ]

    assert bc.find_requeue_waste(events) == []


def test_second_requeue_needs_its_own_start():
    """start→requeue→requeue 에서 두 번째 requeue 를 중복으로 세지 않는다."""
    events = [
        {"job_id": "j1", "event_type": "job_started", "observed_at": _at(10), "project": "GO100"},
        {"job_id": "j1", "event_type": "job_requeued", "observed_at": _at(16), "project": "GO100"},
        {"job_id": "j1", "event_type": "job_requeued", "observed_at": _at(17), "project": "GO100"},
    ]

    findings = bc.find_requeue_waste(events)

    assert len(findings) == 1


# ── 규칙 2: 사람이 봐야 하는 종료 상태 ──────────────────────────────────


def test_health_check_rollback_is_p0():
    """배포가 되돌아갔다는 것은 '고쳤다고 보고된 것이 실제로는 없다'는 뜻이다."""
    jobs = [
        {"job_id": "runner-1791da41", "project": "GO100", "status": "error",
         "phase": "health_check_fail_rollback", "runner_host": "contabo14", "updated_at": _at(9)},
    ]

    findings = bc.find_terminal_failures(jobs)

    assert len(findings) == 1
    assert findings[0]["severity"] == "P0"
    assert findings[0]["rule"] == "HEALTH_CHECK_FAIL_ROLLBACK"


def test_push_failures_are_reported_with_distinct_rules():
    jobs = [
        {"job_id": "a", "project": "NTV2", "status": "error", "phase": "push_fail",
         "runner_host": "rfree-0009", "updated_at": _at(10)},
        {"job_id": "b", "project": "NTV2", "status": "error", "phase": "push_stale_base",
         "runner_host": "rfree-0009", "updated_at": _at(11)},
    ]

    rules = {f["rule"] for f in bc.find_terminal_failures(jobs)}

    assert rules == {"PUSH_FAIL", "PUSH_STALE_BASE"}


def test_successful_jobs_are_not_reported():
    jobs = [
        {"job_id": "ok", "project": "AADS", "status": "done", "phase": "completed",
         "runner_host": "vmi3267555", "updated_at": _at(10)},
    ]

    assert bc.find_terminal_failures(jobs) == []


# ── 규칙 3: 좀비 작업 ───────────────────────────────────────────────────


def _now() -> datetime:
    return datetime(2026, 9, 16, 2, 0, tzinfo=UTC)


def test_job_on_dead_runner_is_flagged_as_zombie():
    jobs = [{"job_id": "z", "project": "GO100", "status": "running", "phase": "claude_code_work",
             "runner_host": "contabo14", "updated_at": _at(10)}]
    hosts = [{"host": "contabo14", "last_seen_at": _now() - timedelta(minutes=40)}]

    findings = bc.find_zombie_jobs(jobs, hosts, _now())

    assert len(findings) == 1
    assert findings[0]["rule"] == "ZOMBIE_INFLIGHT_JOB"


def test_job_on_live_runner_is_not_flagged():
    """정상 작업을 좀비로 오판하면 그 작업이 죽는다 — 이쪽 오판이 더 비싸다."""
    jobs = [{"job_id": "ok", "project": "GO100", "status": "running", "phase": "claude_code_work",
             "runner_host": "contabo14", "updated_at": _at(10)}]
    hosts = [{"host": "contabo14", "last_seen_at": _now() - timedelta(minutes=2)}]

    assert bc.find_zombie_jobs(jobs, hosts, _now()) == []


def test_unknown_host_heartbeat_is_flagged():
    jobs = [{"job_id": "z", "project": "SF", "status": "claimed", "phase": "queued",
             "runner_host": "ghost-host", "updated_at": _at(10)}]

    findings = bc.find_zombie_jobs(jobs, [], _now())

    assert len(findings) == 1
    assert "하트비트 기록 없음" in findings[0]["detail"]


def test_job_without_runner_host_is_skipped():
    jobs = [{"job_id": "x", "project": "SF", "status": "running", "phase": "queued",
             "runner_host": "", "updated_at": _at(10)}]

    assert bc.find_zombie_jobs(jobs, [], _now()) == []


# ── 출력 ────────────────────────────────────────────────────────────────


def test_markdown_reports_zero_findings_explicitly():
    out = bc.render_markdown([], 24)

    assert "지적 0건" in out
    assert "지적 없음" in out


def test_behavior_check_runs_on_a_timer_not_only_by_hand():
    """수동 실행으로만 두면 사람이 기억할 때만 돈다 — R-ERRBOOK 의 '규칙으로만 기록' 함정."""
    service = (ROOT / "scripts" / "aads-aag-behavior-check.service").read_text(encoding="utf-8")
    timer = (ROOT / "scripts" / "aads-aag-behavior-check.timer").read_text(encoding="utf-8")

    assert "tools/aag/behavior_check.py" in service
    # 지적 있음(1)은 실패가 아니고, 실행 불가(2)만 실패여야 한다
    assert "SuccessExitStatus=0 1" in service
    assert "OnUnitActiveSec=30min" in timer
    assert "Persistent=true" in timer
    assert "Unit=aads-aag-behavior-check.service" in timer


def test_markdown_renders_table_for_findings():
    findings = [{"rule": "PUSH_FAIL", "severity": "P1", "job_id": "a", "project": "NTV2",
                 "at": _at(10), "detail": "테스트"}]

    out = bc.render_markdown(findings, 6)

    assert "| P1 | `PUSH_FAIL` | `a` | NTV2 |" in out
