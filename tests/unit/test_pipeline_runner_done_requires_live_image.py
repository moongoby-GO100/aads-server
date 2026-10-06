"""AADS-RUNNER-DONE-REQUIRES-LIVE-IMAGE: 운영 이미지에 커밋이 없으면 done 으로 쓰지 않는다.

2026-10-06 runner-a5761224(20a97a9c) 는 deploy_runs #5599 가 target_drain_busy 로
blocked 인데도 health 만 보고 status=done · '배포 완료' 를 올렸다. 운영은 옛 이미지
(aads-server:d13525b0 / aads-server-green:6a3de230)였고 20a97a9c 는 #5601 에서야 반영됐다.

aads_release_live_verdict / aads_deploy_live_gate 를 격리 실행해
  (a) 슬롯 이미지가 커밋을 포함 → 통과(done 진행)
  (b) 미반영 + blocked → error / deploy_not_live
  (c) 미반영 + queued → deploying / deploy_queued
를 고정한다.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (ROOT / "scripts" / "pipeline-runner.sh").read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = SCRIPT.index(f"{name}() {{")
    return SCRIPT[start : SCRIPT.index("\n}\n", start) + 3]


HARNESS = r"""
set -uo pipefail
log() { echo "LOG $*" >&2; }
db_update() { printf '%s\n---\n' "$1" >> "$WORK/db.log"; }
record_runner_event() { echo "EVENT $*" >> "$WORK/event.log"; }
post_to_chat() { echo "CHAT $*" >> "$WORK/chat.log"; }
docker() {
    # docker inspect <container> --format '{{.Config.Image}}'
    case "$2" in
        aads-server) [[ -n "${IMG_BLUE:-}" ]] && echo "$IMG_BLUE" || return 1 ;;
        aads-server-green) [[ -n "${IMG_GREEN:-}" ]] && echo "$IMG_GREEN" || return 1 ;;
    esac
}
db_exec() { printf '%s\n---\n' "$1" >> "$WORK/sql.log"; echo "${FAKE_RUN_ROW:-}"; }
"""


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    if shutil.which("bash") is None or shutil.which("git") is None:
        pytest.skip("bash/git 미설치 환경")
    base = tmp_path_factory.mktemp("live")
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
        + _function("aads_release_live_verdict")
        + _function("aads_deploy_live_gate")
    )
    return fn, repo, shas  # shas: 오래된 것 → 최신


def _run(env, tmp_path, call, *, blue="", green="", row=""):
    fn, repo, shas = env
    work = tmp_path / "work"
    work.mkdir()
    for f in ("db.log", "event.log", "chat.log", "sql.log"):
        (work / f).write_text("")
    proc = subprocess.run(
        ["bash", "-c", f'source "{fn}"; {call.format(repo=repo, sha=shas[1])}; echo "RC=$?"'],
        capture_output=True, text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "WORK": str(work),
            "IMG_BLUE": blue,
            "IMG_GREEN": green,
            "FAKE_RUN_ROW": row,
        },
    )
    rc = int(proc.stdout.strip().splitlines()[-1].split("=")[1])
    return proc, rc, {f: (work / f).read_text() for f in ("db.log", "chat.log", "event.log")}


def test_a_image_containing_commit_passes_gate(env, tmp_path):
    _, _, shas = env
    # blue 는 옛 이미지, green 이 job 커밋의 후손 → 하나만 포함해도 반영
    proc, rc, out = _run(
        env, tmp_path,
        'aads_deploy_live_gate job-1 sess {sha} "{repo}"',
        blue=f"aads-server:{shas[0][:8]}", green=f"aads-server-green:{shas[2][:8]}",
    )
    assert rc == 0, proc.stderr
    assert out["db.log"] == "" and out["chat.log"] == ""


def test_a_exact_tag_match_passes(env, tmp_path):
    _, _, shas = env
    proc, rc, _ = _run(
        env, tmp_path,
        'aads_release_live_verdict {sha} "{repo}"',
        blue=f"aads-server:{shas[1][:12]}",
    )
    assert rc == 0
    assert proc.stdout.splitlines()[0] == "live:aads-server"


def test_b_not_live_and_blocked_becomes_error(env, tmp_path):
    _, _, shas = env
    proc, rc, out = _run(
        env, tmp_path,
        'aads_deploy_live_gate job-2 sess {sha} "{repo}"',
        blue=f"aads-server:{shas[0][:8]}", green=f"aads-server-green:{shas[0][:8]}",
        row="5599|blocked",
    )
    assert rc == 1
    db = out["db.log"]
    assert "status='error'" in db and "phase='deploy_not_live'" in db
    assert "error_detail='deploy_not_live:5599:blocked'" in db
    assert "status='done'" not in db
    assert "🔴 [Pipeline Runner] 배포 미반영" in out["chat.log"]
    assert "#5599" in out["chat.log"] and "blocked" in out["chat.log"]


def test_b_no_deploy_run_row_is_still_error(env, tmp_path):
    proc, rc, out = _run(
        env, tmp_path, 'aads_deploy_live_gate job-3 sess {sha} "{repo}"',
        blue="aads-server:deadbeef",
    )
    assert rc == 1
    assert "error_detail='deploy_not_live:none:none'" in out["db.log"]


def test_c_not_live_but_queued_becomes_deploying(env, tmp_path):
    _, _, shas = env
    proc, rc, out = _run(
        env, tmp_path,
        'aads_deploy_live_gate job-4 sess {sha} "{repo}"',
        blue=f"aads-server:{shas[0][:8]}", green=f"aads-server-green:{shas[0][:8]}",
        row="5601|queued",
    )
    assert rc == 1
    db = out["db.log"]
    assert "status='deploying'" in db and "phase='deploy_queued'" in db
    assert "status='error'" not in db and "completed_at" not in db
    assert "#5601" in out["chat.log"]


def test_unrelated_branch_commit_is_not_live(env, tmp_path):
    """이미지 sha 가 job 커밋의 조상(옛 것)이면 반영이 아니다 — 방향을 뒤집으면 #5599 가 통과한다."""
    _, _, shas = env
    proc, rc, _ = _run(
        env, tmp_path, 'aads_release_live_verdict {sha} "{repo}"',
        green=f"aads-server-green:{shas[0][:8]}",
    )
    assert rc == 1


def test_runner_wires_gate_only_for_aads_backend_release():
    """게이트는 AADS backend 릴리스를 시도한 경로에서만, 빌드 실패가 없을 때만 돈다."""
    assert '_aads_live_gate="required"' in SCRIPT
    assert '[[ "$_aads_live_gate" == "required" && -z "$_build_fail" ]]' in SCRIPT
    # 게이트가 막은 잡은 done 기록 경로로 떨어지지 않는다
    assert 'if [[ "$_aads_live_blocked" == "true" ]]; then' in SCRIPT
    local = (ROOT / "scripts" / "pipeline-runner.sh.local").read_text(encoding="utf-8")
    assert "aads_deploy_live_gate()" in local
