"""자동 rebase 결과가 patch-id 동일이면 이전 CEO 승인을 상속한다.

배경: 2026-09-30 runner-e3b882c2 는 patch-id 가 같은 SHA 4개로 승인을 세 번 받다가
MAX_RUNTIME 7200s 에 zombie_killed 됐다. stale_base 분기가 SHA 변경만 보고 무조건
AI 재검수 + CEO 재승인으로 되돌렸기 때문이다.

실제 git 저장소와 deploy_job 의 stale_base 분기 원문을 그대로 떼어 실행한다.
DB·이벤트·재검수 워커만 스텁이다.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (ROOT / "scripts" / "pipeline-runner.sh").read_text(encoding="utf-8")

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git 필요")


def _function(name: str) -> str:
    start = SCRIPT.index(f"{name}() {{")
    return SCRIPT[start:SCRIPT.index("\n}\n", start) + 3]


def _stale_base_branch() -> str:
    """deploy_job 의 rebase 성공 분기(if ... fi)를 원문 그대로 떼어낸다."""
    start = SCRIPT.index('        local rebased_sha=""\n')
    end_marker = (
        '        else\n            if [[ "$project" == "AADS" ]]; then\n'
        '                _fail_job "$job_id" "$session_id" "deploy_isolated_push_state"'
    )
    end = SCRIPT.index(end_marker, start)
    return SCRIPT[start:end] + "        fi\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    ).stdout.strip()


def _commit(repo: Path, name: str, body: str, msg: str) -> str:
    (repo / name).write_text(body, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def repo(tmp_path):
    """B0 ─ OLD(job.txt)   ·   B0 ─ B1(other.txt)=origin/main ─ NEW(job.txt, 같은 내용) / DIFF(다른 내용)."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    b0 = _commit(r, "base.txt", "base\n", "base")
    old = _commit(r, "job.txt", "job change\n", "job")
    _git(r, "checkout", "-q", "--detach", b0)
    b1 = _commit(r, "other.txt", "other\n", "other")
    _git(r, "update-ref", "refs/remotes/origin/main", b1)
    _git(r, "cherry-pick", old)
    new = _git(r, "rev-parse", "HEAD")
    _git(r, "checkout", "-q", "--detach", b1)
    diff = _commit(r, "job.txt", "job change EDITED\n", "job")
    _git(r, "checkout", "-q", "--detach", b1)
    root = _git(r, "commit-tree", _git(r, "rev-parse", f"{old}^{{tree}}"), "-m", "orphan")
    return {"path": r, "b0": b0, "b1": b1, "old": old, "new": new, "diff": diff, "root": root}


STUBS = r'''
log() { :; }
sql_escape() { printf "'%s'" "$1"; }
db_update() { printf '%s\n---\n' "$1" >> "$DB_LOG"; }
db_exec() {
    case "$1" in
        *approval_requested*) printf '%s\n' "$APPROVED_SHA" ;;
        *"COUNT(*)"*) printf '%s\n' "$INHERIT_COUNT" ;;
        *"COALESCE(commit_hash,'')"*) printf '%s\n' "$REBASED" ;;
    esac
}
record_runner_event() { printf '%s %s %s %s\n' "$2" "$3" "$4" "$9" >> "$EV_LOG"; }
get_job_status() { echo running; }
attempt_stale_base_rebase() { printf '%s' "$REBASED"; }
review_rebased_aads_sha() { echo "review $4" >> "$EV_LOG"; }
_release_deploy_lock() { :; }
_fail_job() { echo "fail $3" >> "$EV_LOG"; return 1; }
'''


def _run_branch(tmp_path, repo, *, rebased, approved, count="0", env_extra=None):
    body = (
        STUBS + _function("commit_patch_id") + _function("inherit_approval_decision")
        + "harness() {\n"
        + "    local job_id=runner-t session_id=s project=AADS\n"
        + "    local worktree_dir=\"$REPO\" current_sha=\"$CURRENT\" expected_sha=\"$CURRENT\"\n"
        + "    local push_state=stale_base _pre_sha=\"\"\n"
        + _stale_base_branch()
        + "    echo \"STATE current=$current_sha expected=$expected_sha push=$push_state\"\n"
        + "}\n"
    )
    path = tmp_path / "branch.sh"
    path.write_text(body, encoding="utf-8")
    db_log, ev_log = tmp_path / "db.log", tmp_path / "ev.log"
    db_log.write_text("")
    ev_log.write_text("")
    env = {**os.environ, "REPO": str(repo["path"]), "CURRENT": repo["old"], "REBASED": rebased,
           "APPROVED_SHA": approved, "INHERIT_COUNT": count,
           "DB_LOG": str(db_log), "EV_LOG": str(ev_log), **(env_extra or {})}
    res = subprocess.run(["bash", "-c", f'source "{path}"; harness'],
                         env=env, capture_output=True, text=True, check=False)
    return res, db_log.read_text(), ev_log.read_text()


def _assert_reapproval(res, db, ev, reason):
    assert res.returncode == 0, res.stderr
    assert "STATE" not in res.stdout  # 배포(push)로 진행하지 않고 return 0
    assert "phase='ai_review'" in db and "review_verdict=NULL" in db
    assert "deploy_stale_rebased_requeued" in ev
    assert "review " in ev
    assert "approval_inherited_same_patch_id" not in ev
    assert f'"reason":"{reason}' in ev


# 1) patch-id 동일 → 상속, 배포 진행, review_verdict 보존
def test_same_patch_id_inherits_approval_and_continues_deploy(tmp_path, repo):
    res, db, ev = _run_branch(tmp_path, repo, rebased=repo["new"], approved=repo["old"])
    assert res.returncode == 0, res.stderr
    assert f"STATE current={repo['new']} expected={repo['new']} push=fast_forward" in res.stdout
    assert "approval_inherited_same_patch_id info approved" in ev
    assert f'"from":"{repo["old"]}","to":"{repo["new"]}"' in ev
    assert "deploy_stale_rebased_requeued" not in ev and "review " not in ev
    assert f"commit_hash='{repo['new']}'" in db
    assert "[승인상속] patch-id 동일(" in db
    assert "review_verdict" not in db and "review_request_id" not in db
    assert "ai_review" not in db


# 2) patch-id 다름 → 기존대로 재검수 + 재승인
def test_changed_patch_id_requires_review_and_reapproval(tmp_path, repo):
    res, db, ev = _run_branch(tmp_path, repo, rebased=repo["diff"], approved=repo["old"])
    _assert_reapproval(res, db, ev, "patch_id_changed")


# 3) patch-id 계산 실패 → 보수적으로 재승인
def test_patch_id_failure_is_conservative(tmp_path, repo):
    # git patch-id 만 망가뜨린다
    fake = tmp_path / "fakebin"
    fake.mkdir()
    real_git = shutil.which("git")
    (fake / "git").write_text(
        f'#!/bin/bash\nif [[ " $* " == *" patch-id "* ]]; then cat >/dev/null; echo garbage; exit 0; fi\nexec "{real_git}" "$@"\n'
    )
    (fake / "git").chmod(0o755)
    res, db, ev = _run_branch(tmp_path, repo, rebased=repo["new"], approved=repo["old"],
                              env_extra={"PATH": f"{fake}:{os.environ['PATH']}"})
    _assert_reapproval(res, db, ev, "patch_id_unavailable")


# 4) 부모 없는 커밋 / <sha>^ 없음 → 재승인, 예외 전파 없음
def test_root_or_merge_commit_has_no_patch_id_and_requires_reapproval(tmp_path, repo):
    fn = _function("commit_patch_id")
    r = repo["path"]
    _git(r, "checkout", "-q", "--detach", repo["b1"])
    _git(r, "merge", "-q", "--no-ff", "-m", "merge", repo["old"])
    merge = _git(r, "rev-parse", "HEAD")
    for sha, want_empty in ((repo["root"], True), (merge, True), ("not-a-sha", True), (repo["new"], False)):
        out = subprocess.run(["bash", "-c", fn + f'commit_patch_id "{r}" "{sha}"; echo "rc=$?"'],
                             capture_output=True, text=True, check=False)
        assert out.returncode == 0 and out.stdout.endswith("rc=0\n"), out
        assert (out.stdout == "rc=0\n") == want_empty, (sha, out.stdout)

    res, db, ev = _run_branch(tmp_path, repo, rebased=repo["new"], approved=repo["root"])
    _assert_reapproval(res, db, ev, "not_single_commit")


# 5) 상속 상한 초과 → 재승인 + 사유 기록
def test_inherit_limit_exceeded_requires_reapproval_with_reason(tmp_path, repo):
    res, db, ev = _run_branch(tmp_path, repo, rebased=repo["new"], approved=repo["old"], count="5")
    _assert_reapproval(res, db, ev, "inherit_limit_exceeded(5/5)")
    assert "[승인상속 중단] inherit_limit_exceeded(5/5)" in db

    res, db, ev = _run_branch(tmp_path, repo, rebased=repo["new"], approved=repo["old"], count="1",
                              env_extra={"AADS_APPROVAL_INHERIT_MAX": "1"})
    _assert_reapproval(res, db, ev, "inherit_limit_exceeded(1/1)")


# 6) 이전 승인 SHA 를 찾을 수 없음 → 상속하지 않음
def test_missing_approved_sha_does_not_inherit(tmp_path, repo):
    res, db, ev = _run_branch(tmp_path, repo, rebased=repo["new"], approved="")
    _assert_reapproval(res, db, ev, "no_approved_sha")


def test_approved_sha_source_is_latest_request_covered_by_latest_approval():
    fn = _function("inherit_approval_decision")
    assert "event_type='approval_requested'" in fn
    assert "metadata->>'commit_hash'" in fn
    assert "dec.status='approved' AND dec.id > req.id" in fn
    assert (ROOT / "scripts" / "pipeline-runner.sh.local").read_text(encoding="utf-8") == SCRIPT
