"""AADS-LLM-M6-DEPLOY-REGRESSION-GUARD-RETRY: 러너가 autoheal 인계를 실패로 읽지 않는다.

2026-09-30 runner-a5f21866(da6b42d1) 은 deploy.sh 가 target_drain_busy 로 rc=1 을
내자 01:25 에 bluegreen_failed 로 종결됐다. 그런데 deploy.sh 는 EXIT 트랩에서 같은
릴리스의 successor 를 띄우고 나간 것이었고, #5290→#5291→#5292→#5293 사슬이
01:46 에 success_partial 로 배포를 끝냈다. 산출물은 운영에 나갔는데 작업만
실패로 남았고, 목표 마일스톤이 재작업 예산을 소진해 blocked 가 됐다.

scripts/pipeline-runner.sh 의 follow_autoheal_successor() 를 격리 실행해
  - 인계 흔적이 없으면 기존 실패 판정을 그대로 유지하고(즉시 1, 대기 없음)
  - 인계가 있으면 사슬을 끝까지 따라가 success/success_partial 을 성공으로 본다
는 계약을 고정한다.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (ROOT / "scripts" / "pipeline-runner.sh").read_text(encoding="utf-8")
SHA = "da6b42d1d9b869edd6e611977d7fa79c8e10dd4a"


def _function(name: str) -> str:
    start = SCRIPT.index(f"{name}() {{")
    return SCRIPT[start : SCRIPT.index("\n}\n", start) + 3]


def _config_lines() -> str:
    return "\n".join(
        line
        for line in SCRIPT.splitlines()
        if line.startswith(("AADS_AUTOHEAL_FOLLOW_", "AUTOHEAL_FOLLOW_TAG="))
    )


# rows 파일의 각 줄이 폴링 한 번의 "최신 deploy_runs 행" 이다. 마지막 줄은 계속 반복된다.
HARNESS = r"""
set -eo pipefail
log() { echo "LOG $*" >&2; }
db_update() { printf '%s\n---\n' "$1" >> "$WORK/db.log"; }
sleep() { echo "$1" >> "$WORK/sleep.log"; }
approved_sha_is_live() { echo "LIVE $*" >> "$WORK/live.log"; [[ "$FAKE_LIVE" == "1" ]]; }
_renew_deploy_lock() {
    echo "RENEW $*" >> "$WORK/renew.log"
    local n
    n=$(wc -l < "$WORK/renew.log")
    case "$FAKE_RENEW" in
        ok) echo '{"renewed":true,"holder":"runner-x","ttl":600,"reason":"ok"}' ;;
        lost:*) echo "{\"renewed\":false,\"holder\":null,\"ttl\":0,\"reason\":\"${FAKE_RENEW#lost:}\"}" ;;
        down) return 0 ;;
        down_first:*)
            if (( n <= ${FAKE_RENEW#down_first:} )); then return 0; fi
            echo '{"renewed":true,"holder":"runner-x","ttl":600,"reason":"ok"}' ;;
    esac
}
db_exec() {
    printf '%s\n---\n' "$1" >> "$WORK/sql.log"
    if [[ "$1" == *"count(*)"* ]]; then
        echo "$FAKE_HANDOFF"
        return 0
    fi
    local n total
    n=$(( $(cat "$WORK/poll" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$WORK/poll"
    total=$(wc -l < "$WORK/rows")
    (( n > total )) && n=$total
    sed -n "${n}p" "$WORK/rows"
}
"""


@pytest.fixture(scope="module")
def fn_file(tmp_path_factory):
    if shutil.which("bash") is None:
        pytest.skip("bash 미설치 환경")
    path = tmp_path_factory.mktemp("ah") / "fn.sh"
    path.write_text(
        HARNESS + "\n" + _config_lines() + "\n" + _function("follow_autoheal_successor"),
        encoding="utf-8",
    )
    return path


def _run(fn_file: Path, work: Path, rows: list[str], handoff: int = 1, live: bool = False,
         max_wait: str = "300", poll: str = "30", renew: str = "ok", extra_env: dict | None = None):
    work.mkdir(parents=True, exist_ok=True)
    (work / "rows").write_text("\n".join(rows) + "\n", encoding="utf-8")
    env = {
        "PATH": "/usr/bin:/bin",
        "WORK": str(work),
        "FAKE_HANDOFF": str(handoff),
        "FAKE_LIVE": "1" if live else "0",
        "FAKE_RENEW": renew,
        "AADS_AUTOHEAL_FOLLOW_MAX_SEC": max_wait,
        "AADS_AUTOHEAL_FOLLOW_POLL_SEC": poll,
        **(extra_env or {}),
    }
    # set -e 아래에서 호출부와 같은 형태(if out=$(f); then)로 부른다.
    cmd = (
        f'source "{fn_file}"; '
        f'if out=$(follow_autoheal_successor runner-x {SHA} 1790698700 /repo /state AADS); '
        'then rc=0; else rc=$?; fi; echo "$out" | tail -1; echo "RC=$rc"'
    )
    proc = subprocess.run(["bash", "-c", cmd], env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.strip().splitlines()
    rc = int(lines[-1].removeprefix("RC="))
    summary = lines[-2] if len(lines) > 1 else ""
    slept = sum(int(x) for x in (work / "sleep.log").read_text().split()) if (work / "sleep.log").exists() else 0
    return rc, summary, slept


def test_no_handoff_keeps_failure_without_waiting(fn_file, tmp_path):
    rc, summary, slept = _run(fn_file, tmp_path, ["5300|blocked|target_slot_drain|500"], handoff=0)
    assert rc == 1
    assert "no_handoff" in summary
    assert slept == 0
    assert not (tmp_path / "poll").exists(), "인계가 없으면 run 폴링도 하지 않는다"


def test_real_incident_chain_ends_success_partial(fn_file, tmp_path):
    # runner-a5f21866 실측 사슬: 5290 superseded → 5291·5292 running/superseded → 5293 success_partial
    rows = [
        "5291|running|target_slot_drain|3",
        "5291|superseded|superseded_by_autoheal_drain_retry|1",
        "5292|running|target_slot_drain|5",
        "5293|running|build_candidate_image|2",
        "5293|success_partial|completed|0",
    ]
    rc, summary, slept = _run(fn_file, tmp_path, rows, max_wait="2700")
    assert rc == 0
    assert "success run=#5293 status=success_partial" in summary
    assert slept == 4 * 30
    heartbeats = (tmp_path / "db.log").read_text()
    assert "status='deploying'" in heartbeats, "대기 중 작업 heartbeat 를 갱신해야 한다"


def test_blocked_inside_settle_window_is_not_final(fn_file, tmp_path):
    # successor 가 blocked 로 바뀐 직후엔 다음 successor 가 아직 안 보일 수 있다.
    rows = [
        "5292|blocked|target_slot_drain|10",
        "5293|queued|queued_for_deploy|2",
        "5293|success|completed|0",
    ]
    rc, summary, _ = _run(fn_file, tmp_path, rows)
    assert rc == 0
    assert "run=#5293" in summary


def test_settled_failure_ends_chain_as_failure(fn_file, tmp_path):
    rows = ["5293|running|candidate_health|5", "5293|failed|candidate_health|120"]
    rc, summary, _ = _run(fn_file, tmp_path, rows)
    assert rc == 1
    assert "failed run=#5293 status=failed" in summary


def test_timeout_is_failure_and_bounded(fn_file, tmp_path):
    rc, summary, slept = _run(fn_file, tmp_path, ["5293|running|target_slot_drain|1"], max_wait="120")
    assert rc == 1
    assert "timeout" in summary
    assert slept == 120


def test_absorbed_into_other_release_uses_live_check(fn_file, tmp_path):
    rows = ["5294|superseded|included_in_release_batch|40"]
    rc, summary, _ = _run(fn_file, tmp_path, rows, live=True)
    assert rc == 0
    assert "live=contained" in summary
    assert f"LIVE /repo {SHA} /state" in (tmp_path / "live.log").read_text()


def test_absorbed_but_not_live_waits_then_fails(fn_file, tmp_path):
    rows = ["5294|superseded|included_in_release_batch|40"]
    rc, summary, slept = _run(fn_file, tmp_path, rows, live=False, max_wait="60")
    assert rc == 1
    assert "timeout" in summary
    assert slept == 60


def test_autoheal_superseded_row_never_uses_live_shortcut(fn_file, tmp_path):
    rows = ["5290|superseded|superseded_by_autoheal_drain_retry|0"]
    rc, _, _ = _run(fn_file, tmp_path, rows, live=True, max_wait="30")
    assert rc == 1
    assert not (tmp_path / "live.log").exists()


def test_query_scope_is_same_release_api_production_since_deploy_start(fn_file, tmp_path):
    _run(fn_file, tmp_path, ["5293|success|completed|0"])
    sql = (tmp_path / "sql.log").read_text()
    assert f"'{SHA}' LIKE release_sha || '%'" in sql
    assert "length(release_sha) >= 12" in sql
    assert "created_at >= to_timestamp(1790698700)" in sql
    assert "'api')='api'" in sql and "'production')='production'" in sql
    assert "phase LIKE 'superseded_by_autoheal%'" in sql


def test_invalid_sha_is_rejected_without_db(fn_file, tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "rows").write_text("x\n", encoding="utf-8")
    cmd = (
        f'source "{fn_file}"; if out=$(follow_autoheal_successor j "abc;drop" 1 /r /s); '
        'then echo RC=0; else echo RC=$?; fi'
    )
    proc = subprocess.run(
        ["bash", "-c", cmd],
        env={"PATH": "/usr/bin:/bin", "WORK": str(tmp_path), "FAKE_HANDOFF": "1", "FAKE_LIVE": "0",
             "FAKE_RENEW": "ok"},
        capture_output=True, text=True, timeout=30,
    )
    assert proc.stdout.strip().endswith("RC=1")
    assert not (tmp_path / "sql.log").exists()


def test_deploy_job_consults_successor_before_declaring_bluegreen_failed():
    body = _function("deploy_job")
    deploy_call = body.index('bash "$worktree_dir/deploy.sh" bluegreen')
    since = body.index("_aads_deploy_since=$(( $(date +%s) - 5 ))")
    follow = body.index("follow_autoheal_successor \"$job_id\" \"$current_sha\"")
    failed = body.index("aads-server:bluegreen_failed")
    assert since < deploy_call < follow < failed
    # set -e 환경에서 rc=1 이 스크립트를 죽이지 않도록 조건문 안에서 호출해야 한다.
    assert "elif _aads_follow_out=$(follow_autoheal_successor" in body


# ── 배포 락 갱신 (검수 확정 결함: 락 TTL 600s < 추적 상한 2700s) ──────────────────


def _renew_calls(work: Path) -> list[str]:
    path = work / "renew.log"
    return path.read_text().splitlines() if path.exists() else []


def test_lock_is_renewed_on_every_poll_with_job_id_as_owner(fn_file, tmp_path):
    rows = [
        "5291|running|target_slot_drain|3",
        "5292|running|target_slot_drain|3",
        "5293|running|build_candidate_image|2",
        "5293|success_partial|completed|0",
    ]
    rc, _, slept = _run(fn_file, tmp_path, rows, max_wait="2700")
    assert rc == 0
    calls = _renew_calls(tmp_path)
    # 터미널 행을 본 마지막 poll 에서는 갱신하지 않는다 — 대기한 poll 수와 같다.
    assert len(calls) == slept // 30 == 3
    assert all(c == "RENEW AADS runner-x" for c in calls)


def test_renewal_outlasts_lock_ttl(fn_file, tmp_path):
    # 락 TTL 600s 를 넘겨 추적해도(1,080s 사슬) 갱신이 끊기지 않는다.
    rows = ["5291|running|target_slot_drain|3"] * 40 + ["5293|success|completed|0"]
    rc, _, slept = _run(fn_file, tmp_path, rows, max_wait="2700")
    assert rc == 0
    assert slept == 40 * 30 > 600
    assert len(_renew_calls(tmp_path)) == 40


@pytest.mark.parametrize("reason", ["not_owner", "expired_or_released", "redis_error"])
def test_renewal_refused_stops_following_fail_closed(fn_file, tmp_path, reason):
    rows = ["5291|running|target_slot_drain|3"]
    rc, summary, slept = _run(fn_file, tmp_path, rows, max_wait="2700", renew=f"lost:{reason}")
    assert rc == 1
    assert f"deploy_lock_lost run=#5291 reason={reason}" in summary
    assert slept == 0, "락을 잃으면 더 기다리지 않는다"
    assert len(_renew_calls(tmp_path)) == 1


def test_chain_already_succeeded_wins_over_lock_state(fn_file, tmp_path):
    # 사슬이 끝났다면 갱신 거부와 무관하게 결과를 따른다(갱신은 터미널 판정 뒤에 있다).
    rc, summary, _ = _run(fn_file, tmp_path, ["5293|success|completed|0"], renew="lost:not_owner")
    assert rc == 0
    assert "success run=#5293" in summary
    assert _renew_calls(tmp_path) == []


def test_renew_api_unreachable_tolerates_short_blip_then_recovers(fn_file, tmp_path):
    rows = ["5291|running|target_slot_drain|3"] * 4 + ["5293|success|completed|0"]
    rc, summary, _ = _run(fn_file, tmp_path, rows, renew="down_first:2")
    assert rc == 0
    assert "success run=#5293" in summary


def test_renew_api_unreachable_beyond_limit_is_fail_closed(fn_file, tmp_path):
    rows = ["5291|running|target_slot_drain|3"]
    rc, summary, slept = _run(fn_file, tmp_path, rows, max_wait="2700", renew="down")
    assert rc == 1
    assert "deploy_lock_unreadable" in summary and "fails=3" in summary
    assert slept == 2 * 30
    assert len(_renew_calls(tmp_path)) == 3


def test_unreadable_renew_closes_immediately_when_tolerance_is_one(fn_file, tmp_path):
    rows = ["5291|running|target_slot_drain|3"]
    rc, summary, slept = _run(fn_file, tmp_path, rows, max_wait="2700", renew="down",
                              extra_env={"AADS_AUTOHEAL_FOLLOW_RENEW_MAX_FAIL": "1"})
    assert rc == 1
    assert "deploy_lock_unreadable run=#5291 fails=1" in summary
    assert slept == 0
    assert len(_renew_calls(tmp_path)) == 1


def test_lock_abandon_reasons_are_distinct_from_each_other_and_from_timeout(fn_file, tmp_path):
    rows = ["5291|running|target_slot_drain|3"]
    _, lost, _ = _run(fn_file, tmp_path / "a", rows, max_wait="2700", renew="lost:not_owner")
    _, unreadable, _ = _run(fn_file, tmp_path / "b", rows, max_wait="2700", renew="down")
    _, timeout, _ = _run(fn_file, tmp_path / "c", rows, max_wait="60")
    assert "deploy_lock_lost" in lost and "deploy_lock_unreadable" not in lost
    assert "deploy_lock_unreadable" in unreadable and "deploy_lock_lost" not in unreadable
    assert "timeout" in timeout and "deploy_lock_" not in timeout


def test_continuous_renewal_follows_to_success_without_bluegreen_failed(fn_file, tmp_path):
    rows = ["5291|running|target_slot_drain|3"] * 5 + ["5293|success_partial|completed|0"]
    rc, summary, _ = _run(fn_file, tmp_path, rows, max_wait="2700")
    assert rc == 0 and "success run=#5293" in summary
    body = _function("deploy_job")
    success_branch = body[body.index("elif _aads_follow_out=$(follow_autoheal_successor"):]
    success_branch = success_branch[: success_branch.index("\n                    else\n")]
    assert "bluegreen_failed" not in success_branch


def test_deploy_job_records_lock_abandon_event_with_reason():
    body = _function("deploy_job")
    else_branch = body[body.index("AUTOHEAL_FOLLOW 결과:"):body.index("aads-server:bluegreen_failed")]
    assert "deploy_autoheal_follow_abandoned" in else_branch
    assert "deploy_lock_lost" in else_branch and "deploy_lock_unreadable" in else_branch


def test_follow_start_marks_holder_for_waiters(fn_file, tmp_path):
    _run(fn_file, tmp_path, ["5293|success|completed|0"])
    db = (tmp_path / "db.log").read_text()
    assert "[배포추적]" in db and "review_feedback" in db


def test_time_values_relation_is_documented_and_ordered():
    def default(name: str) -> int:
        line = next(ln for ln in SCRIPT.splitlines() if ln.startswith(f"{name}="))
        return int(line.split(":-")[1].split("}")[0])

    follow = default("AADS_AUTOHEAL_FOLLOW_MAX_SEC")
    wait = default("DEPLOY_LOCK_MAX_WAIT_SEC")
    assert wait < follow
    body = _function("follow_autoheal_successor")
    assert "_renew_deploy_lock" in body
    assert "deploy_lock:" not in body
    assert "락 TTL 600s" in SCRIPT and "DEPLOY_LOCK_MAX_WAIT_SEC 900s" in SCRIPT
    assert "AADS_AUTOHEAL_FOLLOW_MAX_SEC 2700s" in SCRIPT
    assert "/ops/locks/deploy/renew?" in _function("_renew_deploy_lock")


def test_deploy_job_passes_project_to_follow_for_lock_renewal():
    body = _function("deploy_job")
    call = body.index('follow_autoheal_successor "$job_id" "$current_sha"')
    assert '"$main_workdir" "$project")' in body[call : call + 200]


def _waiter_file(tmp_path_factory_dir: Path) -> Path:
    cfg = "\n".join(
        line for line in SCRIPT.splitlines()
        if line.startswith(("DEPLOY_LOCK_", "AUTOHEAL_FOLLOW_TAG="))
    )
    harness = r"""
set -eo pipefail
AADS_API_URL="http://fake-api"
log() { echo "LOG $*" >> "$WORK/log.txt"; }
db_update() { printf '%s\n---\n' "$1" >> "$WORK/db.log"; }
record_runner_event() { echo "EVENT $*" >> "$WORK/events.log"; }
post_to_chat() { :; }
_release_deploy_lock() { :; }
_notify_ai() { :; }
promote_next_queued() { :; }
sleep() { :; }
db_exec() { echo "$FAKE_FOLLOWING"; }
curl() { echo '{"acquired":false,"holder":"runner-holder","wait_seconds":300}'; }
"""
    path = tmp_path_factory_dir / "waiter.sh"
    path.write_text(
        harness + cfg + "\n" + _function("_deploy_holder_follow_tag") + "\n"
        + _function("acquire_deploy_lock_with_requeue"),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("following,expect", [("1", True), ("0", False)])
def test_waiter_requeue_message_shows_holder_following_successor(tmp_path, following, expect):
    if shutil.which("bash") is None:
        pytest.skip("bash 미설치 환경")
    fn = _waiter_file(tmp_path)
    work = tmp_path / "w"
    work.mkdir()
    subprocess.run(
        ["bash", "-c", f'source "{fn}"; acquire_deploy_lock_with_requeue job-w AADS sess || true'],
        env={"PATH": "/usr/bin:/bin", "WORK": str(work), "FAKE_FOLLOWING": following,
             "DEPLOY_LOCK_MAX_WAIT_SEC": "200"},
        capture_output=True, text=True, timeout=30,
    )
    db = (work / "db.log").read_text()
    events = (work / "events.log").read_text()
    assert ("holder=runner-holder successor-추적중" in db) is expect
    assert ('"holder_following_successor":true' in events) is expect
