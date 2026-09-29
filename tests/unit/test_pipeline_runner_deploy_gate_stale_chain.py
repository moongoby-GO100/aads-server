"""AADS stale approval recovery keeps the review and approval gates intact."""

import asyncio
import subprocess
from pathlib import Path

import pytest

from app.services import pipeline_auto_rework as rework
from tests.unit.test_pipeline_auto_rework import ORIGINAL, FakeConn, _inserts, _row
from tests.unit.test_pipeline_runner_deploy_preflight_ffonly_scoped import (
    _advance_origin,
    _call_isolated,
    _git,
)

pytest_plugins = ["tests.unit.test_pipeline_runner_deploy_preflight_ffonly_scoped"]


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (ROOT / "scripts/pipeline-runner.sh").read_text(encoding="utf-8")


def _function(name):
    start = SCRIPT.index(f"{name}() {{")
    return SCRIPT[start:SCRIPT.index("\n}\n", start) + 3]


def test_preflight_classifies_already_present_stale_and_fetch_failure():
    body = _function("deploy_isolated_git_preflight")
    assert 'push_state=$(classify_push_state "$worktree_root" "$approved_sha")' in body
    assert 'DEPLOY_PREFLIGHT_ALREADY_PRESENT: job=$job_id sha=$approved_sha' in body
    assert '"deploy_already_present" "info"' in body
    assert 'fast_forward|stale_base) ;;' in body
    assert '"deploy_origin_missing"' in body


def test_preflight_already_present_ancestor_passes(isolated_case, isolated_fn_file, tmp_path):
    remote, main, worktree, job_id, sha = isolated_case
    _git(worktree, "push", "origin", "HEAD:main")
    _advance_origin(remote, tmp_path, "next.txt", "next\n")
    failure = tmp_path / "failure.txt"
    result = _call_isolated(isolated_fn_file, main, worktree, job_id, sha, failure)
    assert result.returncode == 0, result.stderr
    assert "DEPLOY_PREFLIGHT_ALREADY_PRESENT" in result.stderr
    assert not failure.exists()


def test_preflight_fetch_failure_stays_closed(isolated_case, isolated_fn_file, tmp_path):
    _remote, main, worktree, job_id, sha = isolated_case
    _git(main, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    failure = tmp_path / "failure.txt"
    result = _call_isolated(isolated_fn_file, main, worktree, job_id, sha, failure)
    assert result.returncode == 1
    assert failure.read_text().strip() == "deploy_fetch_failed"


def test_stale_overlap_stops_and_disjoint_sha_returns_to_review():
    rebase = _function("attempt_stale_base_rebase")
    deploy = _function("deploy_job")
    assert 'overlap=$(comm -12' in rebase
    assert 'if [[ -n "$overlap" ]]; then' in rebase
    assert 'rebased_sha=$(attempt_stale_base_rebase "$worktree_dir" "$current_sha" "$job_id")' in deploy
    assert '"deploy_isolated_push_state" "승인 SHA push 사전판별 실패: stale_base' in deploy
    assert "status='running', phase='ai_review'" in deploy
    assert "commit_hash='${rebased_sha}'" in deploy
    assert '"deploy_stale_rebased_requeued"' in deploy
    assert 'review_rebased_aads_sha "$job_id" "$session_id" "$worktree_dir" "$rebased_sha"' in deploy
    assert deploy.index('review_rebased_aads_sha "$job_id"') < deploy.index('git -C "$worktree_dir" push origin')
    assert 'return 0' in deploy[deploy.index('review_rebased_aads_sha "$job_id"'):deploy.index('git -C "$worktree_dir" push origin')]
    review = _function("review_rebased_aads_sha")
    assert 'status=\'awaiting_approval\', phase=\'awaiting_approval\'' in review
    assert '/api/v1/review/code-diff/requests' in review


@pytest.mark.parametrize("phase,detail", [
    ("push_stale_base", "push_stale_base: old base"),
    ("deploy_isolated_push_state", "stale_base"),
    ("deploy_isolated_stale_approval", "stale approval"),
    ("error", "deploy_isolated_push_state: stale_base"),
])
def test_deploy_gate_rework_has_bounded_rounds(monkeypatch, phase, detail):
    monkeypatch.delenv("PIPELINE_AUTO_REWORK", raising=False)
    conn = FakeConn(_row(phase=phase, error_detail=detail))
    result = asyncio.run(rework.maybe_submit_auto_rework(conn, "runner-aaaaaaaa"))
    assert result["round"] == 1
    assert len(_inserts(conn)) == 1

    capped = rework.build_rework_instruction(
        original=ORIGINAL, parent_job_id="runner-previous", round_no=2,
        rounds_max=2, issues=["x"],
    )
    conn = FakeConn(_row(phase=phase, error_detail=detail, instruction=capped))
    result = asyncio.run(rework.maybe_submit_auto_rework(conn, "runner-aaaaaaaa"))
    assert result["skipped"] == "cap_reached"
    assert not _inserts(conn)


@pytest.mark.parametrize("instruction,enabled", [
    (ORIGINAL + "NO_AUTO_REWORK\n", "1"),
    (ORIGINAL, "0"),
])
def test_deploy_gate_respects_opt_out(monkeypatch, instruction, enabled):
    monkeypatch.setenv("PIPELINE_AUTO_REWORK", enabled)
    conn = FakeConn(_row(phase="push_stale_base", error_detail="old base", instruction=instruction))
    asyncio.run(rework.maybe_submit_auto_rework(conn, "runner-aaaaaaaa"))
    assert not _inserts(conn)


def test_api_notify_routes_deploy_gate_to_bounded_service():
    source = (ROOT / "app/api/pipeline_runner.py").read_text(encoding="utf-8")
    assert 'is_request_changes_failure(status, row["phase"] or "", row["error_detail"] or "")' in source
    assert "maybe_submit_auto_rework(conn, job_id)" in source


@pytest.mark.parametrize("phase,detail", [
    ("deploy_isolated_push_state", "승인 SHA push 사전판별 실패: fetch_fail"),
    ("error", "deploy_isolated_push_state: fetch_fail"),
])
def test_fetch_failure_does_not_submit_rework(phase, detail):
    assert not rework.is_request_changes_failure("error", phase, detail)


def test_legacy_ambiguous_push_failure_does_not_submit_rework():
    assert not rework.is_request_changes_failure("error", "error", "deploy_isolated_push_state")


def test_review_exit_paths_and_synchronous_response():
    review = _function("review_rebased_aads_sha")
    assert 'response=$(printf \'%s\\n\' "$response" | sed \'$d\')' in review
    assert 'if [[ "$http_code" == "200" ]]; then' in review
    assert 'request_status="completed"' in review
    assert "error_detail='review_infra_failed: rebased SHA review unavailable'" in review
    assert "runner_pid=NULL," in review
    for marker in ("deploy_rebase_review_request_persist_failed", "status='review_hold'", "phase='review_failed'"):
        branch = review[review.index(marker):]
        assert '_notify_ai "$job_id"' in branch
        assert 'promote_next_queued "AADS"' in branch


def test_already_present_requires_live_certified_release():
    deploy = _function("deploy_job")
    guard = _function("approved_sha_is_live")
    assert 'approved_sha_is_live "$worktree_dir" "$expected_sha"' in deploy
    assert '"deploy_already_present_unverified"' in deploy
    assert "status='success' AND phase='completed'" in guard
    assert 'merge-base --is-ancestor "$approved_sha" "$resolved_sha"' in guard
    assert 'standby_digest=image_digest' in guard
    assert "component='api'" in guard
    assert "target_env='production'" in guard


def test_rebased_worktree_survives_review_and_approval_wait():
    deploy = _function("deploy_job")
    cleanup = _function("_cleanup_artifacts")
    old_cleanup = _function("_cleanup_old_artifacts")
    review_branch = deploy[deploy.index('review_rebased_aads_sha "$job_id"'):deploy.index('git -C "$worktree_dir" push origin')]
    assert "runner_pid=${review_worker_pid}" in review_branch
    assert "return 0" in review_branch
    assert "worktree remove" not in review_branch
    assert "worktree remove" not in cleanup
    assert "running|awaiting_approval|deploying|review_hold) continue" in old_cleanup


# 러너는 호스트에서 돌고 호스트에는 jq 가 있다(/usr/bin/jq). 그러나 이 테스트를
# 실행하는 운영 이미지 컨테이너에는 jq 가 없어 추출한 셸 함수가 exit 127 로 죽었다.
# 함수 흐름을 그대로 검증하기 위해 필요한 만큼만 흉내내는 스텁을 넣는다.
JQ_STUB = r"""
jq() {
    local filter="" want_null=0 skip=0 a
    for a in "$@"; do
        if (( skip > 0 )); then skip=$(( skip - 1 )); continue; fi
        case "$a" in
            -n|--null-input) want_null=1 ;;
            -r|--raw-output) ;;
            --arg) skip=2 ;;
            .*) [[ -z "$filter" ]] && filter="$a" ;;
        esac
    done
    if (( want_null )); then printf '{}\n'; return 0; fi
    [[ -n "$filter" ]] || { printf '\n'; return 0; }
    local key="${filter#.}"
    key="${key%%[^A-Za-z0-9_]*}"
    local input value
    input=$(command cat)
    value=$(printf '%s' "$input" | grep -o "\"${key}\"[[:space:]]*:[[:space:]]*[^,}]*" | head -1 | sed 's/^[^:]*:[[:space:]]*//; s/^"//; s/"$//')
    printf '%s\n' "$value"
}
"""


def test_synchronous_review_200_reaches_approval(isolated_case, tmp_path):
    _remote, _main, worktree, job_id, sha = isolated_case
    body = r'''
set -eo pipefail
sql_escape() { printf "'%s'" "$1"; }
json_array_from_lines() { echo '[]'; }
db_update() { printf '%s\n' "$1" >> "$DB_LOG"; }
db_exec() { echo "$RID"; }
cat() { if [[ "$1" == /proc/sys/kernel/random/uuid ]]; then echo "$RID"; else command cat "$@"; fi; }
curl() { printf '{"verdict":"APPROVE","score":0.9}\n200\n'; }
get_job_status() { echo awaiting_approval; }
get_job_instruction() { echo test; }
looks_like_git_diff() { return 0; }
record_runner_event() { :; }
post_to_chat() { :; }
_notify_ai() { :; }
promote_next_queued() { :; }
_fail_job() { return 1; }
''' + JQ_STUB + _function("review_rebased_aads_sha")
    path = tmp_path / "review_fn.sh"
    path.write_text(body, encoding="utf-8")
    log = tmp_path / "db.log"
    import os
    env = {**os.environ, "DB_LOG": str(log), "RID": "11111111-1111-4111-8111-111111111111", "AADS_API_URL": "http://invalid"}
    result = subprocess.run(["bash", "-c", f'source "{path}"; review_rebased_aads_sha "{job_id}" sess "{worktree}" "{sha}"'],
                            env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "status='awaiting_approval'" in log.read_text()


def test_live_release_check_requires_api_certificate_matching_active_digest(isolated_case, tmp_path):
    remote, main, worktree, _job_id, sha = isolated_case
    _git(worktree, "push", "origin", "HEAD:main")
    _advance_origin(remote, tmp_path, "next.txt", "next\n")
    _git(worktree, "fetch", "origin")
    release = _git(worktree, "rev-parse", "origin/main")
    state = tmp_path / "state"
    state.mkdir()
    (state / ".active_port").write_text("8100\n")
    (state / ".active_container").write_text("aads-server\n")
    digest = "sha256:" + "a" * 64
    body = r'''
db_exec() { echo "$RELEASE_ROW"; }
docker() { [[ "$1" == inspect ]] && echo "$ACTIVE_DIGEST"; }
curl() { [[ "$HEALTH_OK" == 1 ]]; }
''' + _function("approved_sha_is_live")
    path = tmp_path / "live_check.sh"
    path.write_text(body, encoding="utf-8")
    import os
    env = {**os.environ, "RELEASE_ROW": f"{release[:12]}|{digest}|8100", "ACTIVE_DIGEST": digest,
           "HEALTH_OK": "1", "AADS_API_URL": "http://invalid"}
    cmd = ["bash", "-c", f'source "{path}"; approved_sha_is_live "{worktree}" "{sha}" "{state}"']
    assert subprocess.run(cmd, env=env, check=False).returncode == 0
    for key, value in [("ACTIVE_DIGEST", "sha256:" + "b" * 64), ("RELEASE_ROW", f"{release[:12]}|{digest}|8102"),
                       ("RELEASE_ROW", ""), ("HEALTH_OK", "0")]:
        bad = {**env, key: value}
        assert subprocess.run(cmd, env=bad, check=False).returncode != 0
    env["RELEASE_ROW"] = f"{_git(main, 'rev-parse', 'HEAD')[:12]}|{digest}|8100"
    assert subprocess.run(cmd, env=env, check=False).returncode != 0


def test_rebased_review_infrastructure_flag_stays_held(isolated_case, tmp_path):
    _remote, _main, worktree, job_id, sha = isolated_case
    body = r'''
set -eo pipefail
sql_escape() { printf "'%s'" "$1"; }
json_array_from_lines() { echo '[]'; }
db_update() { printf '%s\n' "$1" >> "$DB_LOG"; }
db_exec() { echo "$RID"; }
cat() { if [[ "$1" == /proc/sys/kernel/random/uuid ]]; then echo "$RID"; else command cat "$@"; fi; }
curl() { printf '{"verdict":"FLAG","score":0,"flag_category":"REVIEW_MODEL_NO_RESPONSE"}\n200\n'; }
get_job_status() { echo running; }
get_job_instruction() { echo test; }
looks_like_git_diff() { return 0; }
record_runner_event() { :; }
post_to_chat() { :; }
_notify_ai() { :; }
promote_next_queued() { :; }
_fail_job() { return 1; }
''' + JQ_STUB + _function("review_rebased_aads_sha")
    path = tmp_path / "review_flag_fn.sh"
    path.write_text(body, encoding="utf-8")
    log = tmp_path / "db.log"
    import os
    env = {**os.environ, "DB_LOG": str(log), "RID": "11111111-1111-4111-8111-111111111111", "AADS_API_URL": "http://invalid"}
    result = subprocess.run(["bash", "-c", f'source "{path}"; review_rebased_aads_sha "{job_id}" sess "{worktree}" "{sha}"'],
                            env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 1, result.stderr
    updates = log.read_text()
    assert "status='review_hold', phase='review_hold'" in updates
    assert "review_needs_retry=TRUE" in updates
    assert "status='error', phase='review_failed'" not in updates


def test_deploy_job_already_present_skips_actual_push(isolated_case, tmp_path):
    remote, main, worktree, job_id, sha = isolated_case
    _git(worktree, "push", "origin", "HEAD:main")
    _advance_origin(remote, tmp_path, "next.txt", "next\n")
    # deploy_job 은 락 대기를 acquire_deploy_lock_with_requeue 에 위임한다(M6).
    script = "\n".join((_function("classify_push_state"), _function("acquire_deploy_lock_with_requeue"),
                         _function("deploy_job")))
    stub = r'''
set -eo pipefail
log() { echo "$*" >&2; }
git() { if [[ " $* " == *" push "* ]]; then echo push > "$PUSH_MARK"; return 1; fi; command git "$@"; }
curl() { echo '{"acquired":true}'; }
get_job_instruction() { :; }
resolve_project_workdir() { echo "$MAIN"; }
is_aads_dashboard_instruction() { return 1; }
db_exec() { if [[ "$1" == *"commit_hash"* ]]; then echo "$SHA"; fi; }
db_update() { :; }
post_to_chat() { :; }
deploy_isolated_git_preflight() { :; }
record_git_diagnostics() { :; }
approved_sha_is_live() { :; }
get_job_status() { echo done; }
record_runner_event() { :; }
_release_deploy_lock() { :; }
_notify_ai() { :; }
promote_next_queued() { :; }
'''
    path = tmp_path / "deploy_fn.sh"
    path.write_text(stub + script, encoding="utf-8")
    marker = tmp_path / "push_called"
    import os
    env = {**os.environ, "MAIN": str(main), "SHA": sha, "PUSH_MARK": str(marker), "AADS_API_URL": "http://invalid"}
    result = subprocess.run(["bash", "-c", f'source "{path}"; deploy_job "{job_id}" "AADS" "sess"'],
                            env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "PUSH_ALREADY_PRESENT" in result.stderr
    assert not marker.exists()


def test_stale_rebase_overlap_is_blocked_and_disjoint_changes_get_new_sha(isolated_case, tmp_path):
    remote, _main, worktree, job_id, sha = isolated_case
    shell = "\n".join((_function("classify_push_state"), _function("attempt_stale_base_rebase")))
    script = tmp_path / "rebase_fn.sh"
    script.write_text('log() { :; }\n' + shell, encoding="utf-8")

    _advance_origin(remote, tmp_path, "release.txt", "conflict\n")
    overlap = subprocess.run(
        ["bash", "-c", f'source "{script}"; attempt_stale_base_rebase "{worktree}" "{sha}" "{job_id}"'],
        capture_output=True, text=True, check=False,
    )
    assert overlap.returncode != 0
    assert _git(worktree, "rev-parse", "HEAD") == sha

    # A fresh independent origin branch has no shared changed file.
    _git(remote, "update-ref", "refs/heads/main", _git(worktree, "rev-parse", "HEAD^"))
    _advance_origin(remote, tmp_path, "independent.txt", "remote\n")
    disjoint = subprocess.run(
        ["bash", "-c", f'source "{script}"; attempt_stale_base_rebase "{worktree}" "{sha}" "{job_id}"'],
        capture_output=True, text=True, check=False,
    )
    assert disjoint.returncode == 0, disjoint.stderr
    new_sha = disjoint.stdout.strip()
    assert len(new_sha) == 40 and new_sha != sha
    assert _git(worktree, "rev-parse", "HEAD") == new_sha
    assert _git(worktree, "merge-base", "--is-ancestor", _git(worktree, "rev-parse", "origin/main"), new_sha) == ""
