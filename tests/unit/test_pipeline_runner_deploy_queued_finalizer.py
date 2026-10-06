"""AADS-RUNNER-DEPLOY-QUEUED-FINALIZER: deploy_queued 로 남은 러너 잡을 주기 훅에서 마무리한다.

aads_deploy_live_gate 는 미반영 + deploy_runs 진행 중이면 status='deploying',
phase='deploy_queued' 로 두고 끝난다. 그 뒤 done/error 로 닫는 주체가 없어 done 을
조건으로 건 예약 작업이 WAITING 에 머물렀다.

aads_finalize_deploy_queued_jobs 를 격리 실행해
  (a) 운영 이미지에 반영 → done + '✅ 배포 반영 확인'
  (b) 아직 queued → 아무것도 쓰지 않음
  (c) deploy_runs 가 blocked 인데 미반영 → error / deploy_not_live
  (d) queued 인 채 2시간 초과 → error / deploy_queued_timeout
  (e) 같은 잡을 두 번 돌려도 알림 1회
를 고정한다. docker·DB 는 stub 이고 git 은 임시 저장소를 쓴다.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.test_pipeline_runner_live_slot_safety import helper_source, install_case

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (ROOT / "scripts" / "pipeline-runner.sh").read_text(encoding="utf-8")

JOB = "runner-abc12345"
SESSION = "11111111-2222-3333-4444-555555555555"
RS = "\x1e"


def _function(name: str) -> str:
    start = SCRIPT.index(f"{name}() {{")
    return SCRIPT[start : SCRIPT.index("\n}\n", start) + 3]


def _max_sec_line() -> str:
    m = re.search(r"^AADS_DEPLOY_QUEUED_MAX_SEC=.*$", SCRIPT, re.M)
    assert m, "AADS_DEPLOY_QUEUED_MAX_SEC 정의가 없다"
    return m.group(0)


HARNESS = r"""
set -uo pipefail
log() { echo "LOG $*" >&2; }
declare -A PROJECT_WORKDIR=(["AADS"]="$REPO")
db_update() { printf '%s\n---\n' "$1" >> "$WORK/db.log"; }
record_runner_event() { echo "EVENT $*" >> "$WORK/event.log"; }
post_to_chat() { echo "CHAT $*" >> "$WORK/chat.log"; }
_notify_ai() { echo "NOTIFY $*" >> "$WORK/notify.log"; }
docker() {
    case "$2" in
        aads-server) [[ -n "${IMG_BLUE:-}" ]] && echo "$IMG_BLUE" || return 1 ;;
        aads-server-green) [[ -n "${IMG_GREEN:-}" ]] && echo "$IMG_GREEN" || return 1 ;;
    esac
}
# 실제 DB 의 경합 방지(WHERE status='deploying' AND phase='deploy_queued')를 흉내낸다:
# 같은 잡의 UPDATE 는 처음 한 번만 행을 돌려주고, SELECT 는 (경합 최악 가정) 계속 그 잡을 보여준다.
db_exec() {
    printf '%s\n---\n' "$1" >> "$WORK/sql.log"
    case "$1" in
        *"FROM deploy_runs"*) echo "${FAKE_RUN_ROW:-}" ;;
        *"UPDATE pipeline_jobs"*)
            printf '%s\n---\n' "$1" >> "$WORK/update.log"
            local jid
            jid=$(printf '%s' "$1" | grep -o "job_id='[^']*'" | head -1 | cut -d"'" -f2)
            if [[ -e "$WORK/claimed_$jid" ]]; then return 0; fi
            : > "$WORK/claimed_$jid"
            echo "$jid" ;;
        *"FROM pipeline_jobs"*) printf '%s\n' "${FAKE_ROWS:-}" ;;
    esac
}
"""


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    if shutil.which("bash") is None or shutil.which("git") is None:
        pytest.skip("bash/git 미설치 환경")
    base = tmp_path_factory.mktemp("finalizer")
    repo = base / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    git("init", "-q")
    shas = []
    for i in range(3):
        (repo / "f").write_text(str(i))
        git("add", "f")
        git("commit", "-q", "-m", f"c{i}")
        shas.append(git("rev-parse", "HEAD"))
    fn = base / "fn.sh"
    fn.write_text(
        HARNESS
        + _max_sec_line() + "\n"
        + helper_source()
        + _function("aads_release_live_verdict")
        + _function("aads_finalize_deploy_queued_jobs")
    )
    return fn, repo, shas  # shas: 오래된 것 → 최신


def _run(env, tmp_path, *, age, blue="", green="", run_row="", runs=1, projects=None):
    fn, repo, shas = env
    work = tmp_path / "work"
    work.mkdir()
    for f in ("db.log", "event.log", "chat.log", "sql.log", "update.log", "notify.log"):
        (work / f).write_text("")
    rows = f"{JOB}{RS}{shas[1]}{RS}{SESSION}{RS}{age}"
    probe_env = install_case(work, blue=blue, green=green)
    e = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "WORK": str(work),
        "REPO": str(repo),
        "IMG_BLUE": blue,
        "IMG_GREEN": green,
        "FAKE_RUN_ROW": run_row,
        "FAKE_ROWS": rows,
        **probe_env,
    }
    if projects is not None:
        e["RUNNER_PROJECTS"] = projects
    calls = "; ".join(["aads_finalize_deploy_queued_jobs"] * runs)
    proc = subprocess.run(
        ["bash", "-c", f'source "{fn}"; {calls}; echo "RC=$?"'],
        capture_output=True, text=True, env=e,
    )
    assert proc.stdout.strip().splitlines()[-1] == "RC=0", proc.stderr
    names = ("update.log", "chat.log", "event.log", "notify.log", "sql.log")
    return proc, {f.split(".")[0]: (work / f).read_text() for f in names}


def test_a_live_image_finalizes_done(env, tmp_path):
    _, _, shas = env
    proc, out = _run(
        env, tmp_path, age=600,
        blue=f"aads-server:{shas[0][:8]}", green=f"aads-server-green:{shas[2][:8]}",
    )
    up = out["update"]
    assert "status='done'" in up and "phase='done'" in up and "completed_at=NOW()" in up
    assert "status='error'" not in up
    assert out["chat"].count("✅ [Pipeline Runner] 배포 반영 확인") == 1
    assert "aads-server-green" in out["chat"] and JOB in out["chat"]
    assert out["event"].count("job_terminal") == 1
    assert out["notify"].count(JOB) == 1


def test_b_still_queued_writes_nothing(env, tmp_path):
    _, _, shas = env
    proc, out = _run(
        env, tmp_path, age=600, run_row="5601|queued",
        blue=f"aads-server:{shas[0][:8]}", green=f"aads-server-green:{shas[0][:8]}",
    )
    assert out["update"] == "" and out["chat"] == "" and out["event"] == "" and out["notify"] == ""


@pytest.mark.parametrize("status", ["running", "verifying", "syncing_standby"])
def test_b_in_progress_statuses_also_stay(env, tmp_path, status):
    _, _, shas = env
    _, out = _run(
        env, tmp_path, age=3600, run_row=f"5602|{status}",
        blue=f"aads-server:{shas[0][:8]}",
    )
    assert out["update"] == "" and out["chat"] == ""


def test_c_blocked_not_live_becomes_error(env, tmp_path):
    _, _, shas = env
    proc, out = _run(
        env, tmp_path, age=600, run_row="5599|blocked",
        blue=f"aads-server:{shas[0][:8]}", green=f"aads-server-green:{shas[0][:8]}",
    )
    up = out["update"]
    assert "status='error'" in up and "phase='deploy_not_live'" in up
    assert "error_detail='deploy_not_live:5599:blocked'" in up
    assert "status='done'" not in up
    assert out["chat"].count("🔴 [Pipeline Runner] 배포 미반영") == 1
    assert "deploy_not_live:5599:blocked" in out["chat"]
    assert out["notify"].count(JOB) == 1


def test_c_no_deploy_run_row_is_error(env, tmp_path):
    _, out = _run(env, tmp_path, age=600, blue="aads-server:deadbeef")
    assert "error_detail='deploy_not_live:none:none'" in out["update"]


def test_d_queued_over_two_hours_times_out(env, tmp_path):
    _, _, shas = env
    proc, out = _run(
        env, tmp_path, age=7201, run_row="5601|queued",
        blue=f"aads-server:{shas[0][:8]}", green=f"aads-server-green:{shas[0][:8]}",
    )
    up = out["update"]
    assert "status='error'" in up and "phase='deploy_queued_timeout'" in up
    assert "error_detail='deploy_queued_timeout:5601:queued'" in up
    assert "deploy_queued_timeout:5601:queued" in out["chat"]


def test_d_exactly_two_hours_does_not_time_out(env, tmp_path):
    _, _, shas = env
    _, out = _run(
        env, tmp_path, age=7200, run_row="5601|queued",
        blue=f"aads-server:{shas[0][:8]}",
    )
    assert out["update"] == ""


def test_d_live_wins_even_after_two_hours(env, tmp_path):
    _, _, shas = env
    _, out = _run(env, tmp_path, age=99999, blue=f"aads-server:{shas[2][:8]}")
    assert "status='done'" in out["update"] and "status='error'" not in out["update"]


@pytest.mark.parametrize(
    "kwargs, marker",
    [
        (dict(blue="@LIVE@"), "배포 반영 확인"),
        (dict(run_row="5599|blocked", blue="aads-server:deadbeef"), "배포 미반영"),
        (dict(run_row="5601|queued", blue="aads-server:deadbeef", age=9000), "배포 미반영"),
    ],
)
def test_e_second_run_does_not_renotify(env, tmp_path, kwargs, marker):
    _, _, shas = env
    kwargs = dict(kwargs)
    kwargs.setdefault("age", 600)
    if kwargs.get("blue") == "@LIVE@":
        kwargs["blue"] = f"aads-server:{shas[2][:8]}"
    _, out = _run(env, tmp_path, runs=2, **kwargs)
    assert out["chat"].count(marker) == 1
    assert out["event"].count("job_terminal") == 1
    assert out["notify"].count(JOB) == 1
    # 두 번째 주기에도 UPDATE 는 시도된다 — 막는 것은 WHERE 조건이다.
    assert out["update"].count("UPDATE pipeline_jobs") == 2


def test_every_update_is_guarded_by_deploying_phase(env, tmp_path):
    _, _, shas = env
    for kw in (dict(blue=f"aads-server:{shas[2][:8]}"), dict(run_row="1|failed", blue="aads-server:deadbeef")):
        sub = tmp_path / kw.get("run_row", "live").replace("|", "_")
        sub.mkdir()
        _, out = _run(env, sub, age=600, **kw)
        updates = [u for u in out["update"].split("\n---\n") if u.strip()]
        assert updates
        for u in updates:
            assert "AND status='deploying' AND phase='deploy_queued'" in u
            assert "RETURNING job_id" in u


def test_non_aads_runner_does_not_touch_db(env, tmp_path):
    _, out = _run(env, tmp_path, age=99999, projects="KIS,GO100", blue="aads-server:deadbeef")
    assert out["sql"] == ""


def test_aads_runner_projects_filter_runs(env, tmp_path):
    _, _, shas = env
    _, out = _run(env, tmp_path, age=600, projects="KIS,AADS", blue=f"aads-server:{shas[2][:8]}")
    assert "status='done'" in out["update"]


def test_wiring_in_recover_stuck_jobs_before_bug7_timeout():
    body = _function("_recover_stuck_jobs")
    assert "aads_finalize_deploy_queued_jobs" in body
    # 마무리가 deploying 20분 타임아웃보다 먼저 돈다
    assert body.index("aads_finalize_deploy_queued_jobs") < body.index("error_detail='deploy_timeout'")
    # 20분 타임아웃이 deploy_queued 를 가로채지 않는다(2시간 상한은 마무리 쪽이 가진다)
    timeout_sql = body[body.index("error_detail='deploy_timeout'") :]
    timeout_sql = timeout_sql[: timeout_sql.index("RETURNING job_id")]
    assert "phase IS DISTINCT FROM 'deploy_queued'" in timeout_sql


def test_live_gate_rules_unchanged():
    gate = _function("aads_deploy_live_gate")
    assert "phase='deploy_queued'" in gate and "phase='deploy_not_live'" in gate


def test_local_copy_is_byte_identical():
    assert (ROOT / "scripts" / "pipeline-runner.sh").read_bytes() == (
        ROOT / "scripts" / "pipeline-runner.sh.local"
    ).read_bytes()
