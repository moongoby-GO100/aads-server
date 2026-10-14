"""deploy.sh Phase 7 실패 처리 — 실제 운영 슬롯과 기록·알림 일치 (2026-10-14 deploy_runs #5740).

사고: Phase 7 감시가 구 슬롯에도 있던 소음(llm_first_response_timeout)에 걸려
deploy_runs=failed 가 됐지만 새 릴리스는 이미 운영 중이었고, 러너는 "기존 서비스 유지"
라고 보고했다. 정책 B(롤백 안 함 + ACTIVE=<slot>/<sha> 명시) + 기준선 비교.
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = (ROOT / "deploy.sh").read_text(encoding="utf-8")
RUNNER = (ROOT / "scripts" / "pipeline-runner.sh").read_text(encoding="utf-8")

PATTERN = re.search(
    r'MONITOR_PATTERN="\$\{AADS_DEPLOY_MONITOR_PATTERN:-(.+)\}"', DEPLOY
).group(1)
EXCLUDE = re.search(
    r'MONITOR_EXCLUDE_PATTERN="\$\{AADS_DEPLOY_MONITOR_EXCLUDE_PATTERN:-(.+)\}"', DEPLOY
).group(1)


def _fn(script: str, name: str) -> str:
    start = script.index(f"{name}() {{")
    return script[start : script.index("\n}\n", start) + 3]


def _const(name: str) -> str:
    return re.search(rf"^{name}=.*$", DEPLOY, re.M).group(0)


DEPLOY_FUNCS = "\n".join(
    [
        _const("P0P1_BASELINE_FACTOR"),
        _const("P0P1_BASELINE_SLACK"),
        _fn(DEPLOY, "p0p1_event_counts"),
        _fn(DEPLOY, "p0p1_classify_hits"),
        _fn(DEPLOY, "p0p1_active_descriptor"),
        _fn(DEPLOY, "p0p1_monitor_fail"),
    ]
)
RUNNER_FUNCS = "\n".join(
    _fn(RUNNER, n)
    for n in ("aads_active_slot_state", "_aads_sha_match", "aads_deploy_failure_message")
)


def _bash(script: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", script],
        env={"PATH": "/usr/bin:/bin", **(env or {})},
        text=True,
        capture_output=True,
        check=False,
    )


def _counts(logs: str) -> dict[str, int]:
    r = _bash(
        DEPLOY_FUNCS + '\nprintf "%s\\n" "$LOGS" | p0p1_event_counts "$P" "$X"',
        {"LOGS": logs, "P": PATTERN, "X": EXCLUDE},
    )
    assert r.returncode == 0, r.stderr
    return {k: int(v) for k, v in (line.split() for line in r.stdout.splitlines())}


def _classify(baseline: str, observed: str, elapsed: int = 300):
    r = _bash(
        DEPLOY_FUNCS + '\np0p1_classify_hits "$B" "$O" "$E"; echo "rc=$?"',
        {"B": baseline, "O": observed, "E": str(elapsed)},
    )
    assert r.returncode == 0, r.stderr
    *lines, rc = r.stdout.strip().splitlines()
    return [ln for ln in lines if ln], rc


def _ev(event: str, level: str = "error") -> str:
    return f'{{"event": "{event}", "level": "{level}"}}'


# ── 기준선 분류 ──────────────────────────────────────────────────────────

def test_a_baseline_noise_only_passes_with_warning():
    """(a) 구 슬롯과 같은 timeout 소음만 → 실패 아님(NOISE)."""
    logs = "\n".join(
        _ev(f"llm_first_response_timeout session={i} elapsed=203.1s") for i in range(5)
    )
    observed = "\n".join(f"{k} {n}" for k, n in _counts(logs).items())
    assert observed == "llm_first_response_timeout 5"
    # 구 슬롯: 15분 30건 = 2건/분
    lines, rc = _classify("B llm_first_response_timeout 30 900", observed)
    assert rc == "rc=0"
    assert lines[0].startswith("NOISE llm_first_response_timeout obs=5")


def test_b_new_event_key_fails():
    """(b) 구 슬롯에 없던 이벤트 키 → 실패."""
    logs = "\n".join(
        [_ev("llm_first_response_timeout session=1"), _ev("bg_producer_error boom")]
    )
    observed = "\n".join(f"{k} {n}" for k, n in _counts(logs).items())
    lines, rc = _classify("B llm_first_response_timeout 30 900", observed)
    assert rc == "rc=1"
    assert any(ln.startswith("FAIL_NEW bg_producer_error") for ln in lines)
    assert any(ln.startswith("NOISE llm_first_response_timeout") for ln in lines)


def test_known_key_with_clear_increase_fails():
    # 기준선 2건/분 → 5분 허용 = 2*5*3+3 = 33. 34건이면 실패, 33건은 소음.
    _, rc_ok = _classify("B k 30 900", "k 33")
    lines, rc_bad = _classify("B k 30 900", "k 34")
    assert rc_ok == "rc=0"
    assert rc_bad == "rc=1" and lines[0].startswith("FAIL_INCREASE k obs=34")


def test_empty_baseline_treats_every_event_as_new():
    lines, rc = _classify("", "llm_first_response_timeout 1")
    assert rc == "rc=1" and lines[0].startswith("FAIL_NEW")


def test_baseline_uses_max_rate_of_windows():
    # 정상 상태 창 0.1건/분 + 기동 직후 창 2건/분 → 큰 쪽(2건/분)으로 판정
    baseline = "B k 1 900\nB k 30 900"
    _, rc = _classify(baseline, "k 20")
    assert rc == "rc=0"


def test_traceback_is_merged_with_preceding_error_event():
    logs = "\n".join(
        [
            _ev("bg_producer_error boom"),
            "Traceback (most recent call last):",
            '  File "x.py", line 1',
            _ev("ok", "info"),
            _ev("llm_first_response_timeout session=1"),
        ]
    )
    assert _counts(logs) == {"bg_producer_error": 1, "llm_first_response_timeout": 1}


def test_lone_traceback_after_info_event_counts_under_that_event():
    logs = "\n".join([_ev("resume_session", "info"), "Traceback (most recent call last):"])
    assert _counts(logs) == {"resume_session": 1}
    assert _counts("Traceback (most recent call last):") == {"traceback": 1}


def test_user_sql_error_and_payload_text_are_still_excluded():
    logs = "\n".join(
        [
            _ev("query_project_database: USER_SQL_ERROR | x"),
            _ev("tool failed for PRIORITY: P0-CRITICAL job", "warning"),
        ]
    )
    assert _counts(logs) == {}


def test_text_format_event_key():
    assert _counts("2026-10-14 level=error event=foo_bar a=1") == {"foo_bar": 1}


# ── 실패 기록: ACTIVE=<slot>/<sha> ───────────────────────────────────────

FAKE_ENV = r"""
UPSTREAM_CONF="$WORK/aads-upstream.conf"
printf '# Active API slot: green (8102). Blue (8100) is kept as rollback backup.\n' > "$UPSTREAM_CONF"
docker() { [[ "$1" == inspect && "$2" == aads-server-green ]] && echo "aads-server:5562aed6049c"; }
notify() { echo "NOTIFY $*" >> "$WORK/calls.log"; }
deploy_phase_end() { echo "PHASE_END $*" >> "$WORK/calls.log"; }
record_deploy() { echo "RECORD $*" >> "$WORK/calls.log"; }
MODE=bluegreen
AADS_RELEASE_SHA=5562aed6049c9999
"""


def test_monitor_fail_records_actual_active_slot_and_exits_nonzero(tmp_path):
    r = _bash(
        FAKE_ENV
        + DEPLOY_FUNCS
        + '\n( p0p1_monitor_fail "P0/P1 monitor regression" "FAIL_NEW x obs=1" ); echo "rc=$?"\n'
        + 'cat "$WORK/calls.log"',
        {"WORK": str(tmp_path)},
    )
    assert "rc=1" in r.stdout
    assert "ACTIVE=green/5562aed6049c; release=5562aed6049c9999; switched_but_monitor_failed" in r.stdout
    for tag in ("PHASE_END p0p1_monitoring failed ACTIVE=green/", "RECORD failed bluegreen ACTIVE=green/"):
        assert tag in r.stdout


def test_active_descriptor_unknown_when_conf_unreadable(tmp_path):
    r = _bash(
        'UPSTREAM_CONF="$WORK/missing"\n' + DEPLOY_FUNCS + "\np0p1_active_descriptor",
        {"WORK": str(tmp_path)},
    )
    assert r.stdout.strip() == "unknown/unknown"


# ── 러너 알림 문구 ───────────────────────────────────────────────────────

def _message(release, slot, sha, pre, cutover, tail="monitor hit") -> str:
    r = subprocess.run(
        ["bash", "-c", RUNNER_FUNCS + '\naads_deploy_failure_message "$@"', "_",
         release, slot, sha, pre, cutover, tail],
        env={"PATH": "/usr/bin:/bin"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_c_active_is_new_release_says_new_version_running():
    """(c) 운영 SHA = 신규 + 실패 → '새 버전 운영 중', '기존 서비스 유지' 금지."""
    msg = _message("5562aed6049c8d1e00000000000000000000aaaa", "green", "5562aed6049c", "2559dfb1a0b1", "true")
    assert "새 버전 운영 중" in msg
    assert "운영 중 = green(5562aed6049c)" in msg
    assert "기존 서비스 유지" not in msg and "되돌림" not in msg


def test_d_rolled_back_says_reverted_to_previous_version():
    """(d) 컷오버 후 롤백 → '이전 버전으로 되돌림'."""
    msg = _message("5562aed6049c8d1e00000000000000000000aaaa", "blue", "2559dfb1a0b1", "2559dfb1a0b1", "true")
    assert "이전 버전으로 되돌림" in msg
    assert "새 버전 운영 중" not in msg and "기존 서비스 유지" not in msg


def test_pre_cutover_failure_keeps_existing_service_wording():
    msg = _message("5562aed6049c8d1e00000000000000000000aaaa", "blue", "2559dfb1a0b1", "2559dfb1a0b1", "false")
    assert "기존 서비스 유지" in msg and "운영 중 = blue(2559dfb1a0b1)" in msg


def test_unreadable_state_never_claims_either_outcome():
    msg = _message("5562aed6049c8d1e", "", "", "", "false")
    assert "확인 불가" in msg
    assert "기존 서비스 유지" not in msg and "새 버전 운영 중" not in msg


def test_switched_to_third_release_is_flagged_not_called_rollback():
    msg = _message("5562aed6049c8d1e", "blue", "aaaaaaaaaaaa", "2559dfb1a0b1", "true")
    assert "되돌림" not in msg and "수동 확인" in msg


def test_active_slot_state_reads_nginx_comment_and_image_tag(tmp_path):
    conf = tmp_path / "aads-upstream.conf"
    conf.write_text("# Active API slot: green (8102). Blue (8100) is kept as rollback backup.\n")
    r = _bash(
        'docker() { [[ "$2" == aads-server-green ]] && echo "aads-server:5562aed6049c"; }\n'
        + RUNNER_FUNCS
        + "\naads_active_slot_state",
        {"AADS_UPSTREAM_CONF": str(conf)},
    )
    assert r.stdout.strip() == "green 5562aed6049c"

    conf.write_text("# no marker\n")
    r = _bash(RUNNER_FUNCS + "\naads_active_slot_state; echo rc=$?", {"AADS_UPSTREAM_CONF": str(conf)})
    assert "rc=1" in r.stdout


# ── 배선(구조) 가드 ──────────────────────────────────────────────────────

def test_phase7_wiring_keeps_floor_and_uses_baseline_and_live_slot():
    phase7 = DEPLOY[DEPLOY.index("# ── Phase 7: P0/P1 모니터링 게이트 ──") :]
    assert "if (( MONITOR_SECONDS < 300 )); then" in phase7
    assert 'MONITOR_CONTAINER="$NEW_CONTAINER"' in phase7
    assert 'docker logs "$MONITOR_CONTAINER" --since "$MONITOR_SINCE"' in phase7
    assert "p0p1_classify_hits" in phase7 and "p0p1_monitor_fail" in phase7
    # 실패 경로가 deploy_runs 를 직접 failed 로 쓰지 않고 ACTIVE 명시 함수로만 간다
    assert 'record_deploy "failed" "$MODE" "Phase 7' not in phase7


def test_baseline_is_captured_before_standby_sync_recreates_old_slot():
    assert DEPLOY.index('p0p1_capture_baseline "$OLD_CONTAINER"') < DEPLOY.index(
        'deploy_phase_start "standby_same_digest_sync"'
    )
    assert "P0P1_CUTOVER_EPOCH" in DEPLOY


def test_runner_uses_state_message_not_hardcoded_wording():
    assert "AADS bluegreen 배포 실패 — 기존 서비스 유지: " not in RUNNER
    assert 'post_to_chat "$session_id" "$_aads_fail_msg"' in RUNNER
