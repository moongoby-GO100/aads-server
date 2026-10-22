"""deploy.sh 릴리스 출처 가드 (AADS-DEPLOY-RELEASE-SHA-ORIGIN-ANCESTOR-GUARD-20261022).

2026-10-22 deploy_runs#5849·#5850: 로컬 main 70fe937d(origin/main 대비 ahead 18 / behind 466)가
릴리스로 선택돼 구동본 커밋 458개가 빠진 이미지가 컷오버 직전까지 갔다.
deploy.sh 의 실제 함수를 잘라 임시 git 저장소 위에서 실행한다. docker·원격 호출은 스텁이다.
"""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SH = (ROOT / "deploy.sh").read_text()
AUTOHEAL_SH = ROOT / "scripts" / "deploy_autoheal.sh"


def _fn(name: str) -> str:
    body = DEPLOY_SH.split("\n" + name + "() {", 1)[1].split("\n}\n", 1)[0]
    return name + "() {" + body + "\n}\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / "f").write_text(name)
    _git(repo, "add", "f")
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def repo(tmp_path):
    """main: c0 <- c1 <- c2 (origin/main = c2). HEAD 는 c2."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    full = [_commit(repo, f"c{i}") for i in range(3)]
    _git(repo, "update-ref", "refs/remotes/origin/main", full[2])
    return repo, full


PRELUDE = '''set -Eeuo pipefail
MODE=bluegreen
DEPLOY_RUN_ID="${DEPLOY_RUN_ID:-501}"
DEPLOY_CURRENT_PHASE=initializing
DEPLOY_START_EPOCH=$(date +%s)
DEPLOY_PHASE_START_EPOCH=$DEPLOY_START_EPOCH
audit_control() { printf 'AUDIT %s\\n' "$*" >> "$LOG"; }
record_deploy() { printf 'RECORD %s\\n' "$*" >> "$LOG"; }
stop_deploy_heartbeat() { :; }
deploy_observe_init() { :; }
deploy_estimated_remaining_ms() { echo NULL; }
sql_escape() { printf '%s' "${1:-}" | sed "s/'/''/g"; }
deploy_db_available() { return 0; }
docker() { printf 'DOCKER %s\\n' "$*" >> "$LOG"; return 1; }
deploy_db_exec() { printf 'SQL %s\\n' "$1" >> "$LOG"; }
'''

FUNCS = "".join(
    _fn(n)
    for n in (
        "deploy_observe_update",
        "deploy_phase_end",
        "reject_unpushed_release",
        "enforce_release_origin_guard",
    )
)

FLOW = '\nenforce_release_origin_guard\necho GUARD_PASSED\nbuild_candidate_image_stub\n'


def _run(tmp_path, repo, release, **env):
    log = tmp_path / "log"
    log.write_text("")
    script = (
        PRELUDE
        + FUNCS
        + 'build_candidate_image_stub() { printf "BUILD_CALLED\\n" >> "$LOG"; }\n'
        + FLOW
    )
    full_env = dict(os.environ, LOG=str(log), COMPOSE_DIR=str(repo), AADS_RELEASE_SHA=release)
    full_env.pop("AADS_DEPLOY_ALLOW_UNPUSHED_RELEASE", None)
    full_env.update(env)
    result = subprocess.run(
        ["bash", "-c", script], env=full_env, capture_output=True, text=True, timeout=60
    )
    return result, log.read_text()


def test_diverged_local_commit_is_blocked_before_build(tmp_path, repo):
    # (a) origin/main 에 없는 로컬 커밋 — Chat-Finalize 로컬 커밋과 같은 모양.
    repo_path, full = repo
    local = _commit(repo_path, "local-only")
    result, log = _run(tmp_path, repo_path, local[:12])

    assert result.returncode == 2, result.stdout + result.stderr
    assert "GUARD_PASSED" not in result.stdout
    assert "BUILD_CALLED" not in log
    assert "DOCKER build" not in log
    assert f"release={local[:12]}" in result.stdout
    assert f"origin/main={full[2][:12]}" in result.stdout
    assert "ahead=1 behind=0" in result.stdout
    assert "AUDIT release-origin-guard aads-server:" in log and "blocked" in log
    assert "RECORD blocked bluegreen deploy.release_sha_diverged_local_head:" in log
    assert "status='blocked'" in log


def test_diverged_with_origin_ahead_reports_both_counts(tmp_path, repo):
    repo_path, full = repo
    _git(repo_path, "reset", "-q", "--hard", full[0])
    local = _commit(repo_path, "local-only")  # origin/main(c2) 과 갈라졌다
    result, _ = _run(tmp_path, repo_path, local[:12])

    assert result.returncode == 2
    assert "ahead=1 behind=2" in result.stdout


def test_commit_reachable_from_origin_main_passes(tmp_path, repo):
    # (b) origin/main 의 조상(여기서는 tip 자체)이면 통과한다.
    repo_path, full = repo
    result, log = _run(tmp_path, repo_path, full[2][:12])

    assert result.returncode == 0, result.stderr
    assert "GUARD_PASSED" in result.stdout
    assert "blocked" not in log
    assert "AUDIT" not in log


def test_older_ancestor_of_origin_main_passes(tmp_path, repo):
    repo_path, full = repo
    _git(repo_path, "checkout", "-q", "--detach", full[1])
    result, _ = _run(tmp_path, repo_path, full[1][:12])

    assert result.returncode == 0, result.stderr
    assert "GUARD_PASSED" in result.stdout


def test_release_sha_must_match_compose_dir_head(tmp_path, repo):
    # (c) 태그는 origin/main 의 조상이지만 아카이브될 트리(HEAD)는 다르다.
    repo_path, full = repo
    result, log = _run(tmp_path, repo_path, full[0][:12])

    assert result.returncode == 2
    assert "GUARD_PASSED" not in result.stdout
    assert "BUILD_CALLED" not in log
    assert "deploy.release_sha_head_mismatch" in log
    assert "RECORD blocked bluegreen deploy.release_sha_head_mismatch:" in log


@pytest.mark.parametrize("release", ["unknown", "", "zzzzzzzz", "abc"])
def test_unresolvable_or_malformed_release_sha_is_blocked(tmp_path, repo, release):
    repo_path, _ = repo
    result, _ = _run(tmp_path, repo_path, release)

    assert result.returncode == 2
    assert "GUARD_PASSED" not in result.stdout


def test_head_mismatch_cannot_be_bypassed(tmp_path, repo):
    repo_path, full = repo
    result, _ = _run(
        tmp_path, repo_path, full[0][:12], AADS_DEPLOY_ALLOW_UNPUSHED_RELEASE="1"
    )

    assert result.returncode == 2
    assert "GUARD_PASSED" not in result.stdout


def test_missing_origin_main_ref_fails_closed(tmp_path, repo):
    # (d) origin/main ref 가 없으면 증명할 수 없으므로 차단한다. fetch 도 하지 않는다.
    repo_path, full = repo
    _git(repo_path, "update-ref", "-d", "refs/remotes/origin/main")
    result, log = _run(tmp_path, repo_path, full[2][:12])

    assert result.returncode == 2
    assert "GUARD_PASSED" not in result.stdout
    assert "BUILD_CALLED" not in log
    assert "origin/main ref not found locally" in result.stdout
    assert "deploy.release_sha_diverged_local_head" in log


def test_operator_override_passes_with_warning_and_audit(tmp_path, repo):
    # (e) 우회 변수: 통과하되 경고 로그와 audit 를 남긴다.
    repo_path, _ = repo
    local = _commit(repo_path, "local-only")
    result, log = _run(
        tmp_path, repo_path, local[:12], AADS_DEPLOY_ALLOW_UNPUSHED_RELEASE="1"
    )

    assert result.returncode == 0, result.stderr
    assert "GUARD_PASSED" in result.stdout
    assert "bypassed by AADS_DEPLOY_ALLOW_UNPUSHED_RELEASE=1" in result.stdout
    assert "AUDIT release-origin-guard aads-server:" in log and "bypassed" in log
    assert "RECORD blocked" not in log


def test_override_other_values_do_not_bypass(tmp_path, repo):
    repo_path, _ = repo
    local = _commit(repo_path, "local-only")
    result, _ = _run(
        tmp_path, repo_path, local[:12], AADS_DEPLOY_ALLOW_UNPUSHED_RELEASE="true"
    )

    assert result.returncode == 2


def test_guard_never_fetches():
    body = _fn("reject_unpushed_release") + _fn("enforce_release_origin_guard")
    assert "fetch" not in body
    assert "docker" not in body.replace("deploy_", "")


def test_guard_sits_after_claim_and_before_inclusion_and_preflight():
    claim = DEPLOY_SH.index("\nclaim_latest_queued_deploy_request\n")
    ancestor = DEPLOY_SH.index("\nenforce_ancestor_release_guard\n")
    origin = DEPLOY_SH.index("\nenforce_release_origin_guard\n")
    include = DEPLOY_SH.index("\ninclude_queued_ancestors_in_direct_release\n")
    preflight = DEPLOY_SH.index('\ndeploy_phase_start "preflight" "running"')
    build = DEPLOY_SH.index("build_candidate_image", preflight)
    assert claim < ancestor < origin < include < preflight < build
    assert DEPLOY_SH.count("\nenforce_release_origin_guard\n") == 1


def test_existing_guards_are_kept():
    for name in (
        "reject_ancestor_release",
        "enforce_ancestor_release_guard",
        "prepare_release_context",
        "enforce_release_worktree_gate",
    ):
        assert "\n" + name + "() {" in DEPLOY_SH


def _autoheal(func: str, *args: str) -> str:
    quoted = " ".join(f"'{a}'" for a in args)
    script = f'set -uo pipefail\nexport AADS_PROVENANCE_SWEEP=0\nsource "{AUTOHEAL_SH}"\n{func} {quoted}\n'
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.mark.parametrize(
    "err",
    [
        "deploy.release_sha_diverged_local_head: release=70fe937d origin/main=51c6586d ahead=18 behind=466",
        "deploy.release_sha_head_mismatch: release=abc source_head=def",
        # 본문에 다른 원인의 문구가 섞여 있어도 출처 가드가 우선한다.
        "deploy.release_sha_diverged_local_head: dirty worktree under /root/aads/aads-server",
    ],
)
def test_autoheal_classifies_origin_guard_as_non_retryable(err):
    cause = _autoheal("classify_deploy_failure", "preflight", err)
    assert cause == "release_origin_blocked"
    assert _autoheal("autoheal_policy", cause, "preflight") == "manual"


def test_autoheal_dirty_worktree_remediation_escalates_on_guarded_release(tmp_path, repo):
    repo_path, _ = repo
    local = _commit(repo_path, "local-only")
    script = (
        "set -uo pipefail\n"
        "export AADS_PROVENANCE_SWEEP=0 AADS_DEPLOY_AUTOHEAL_DRYRUN=1\n"
        f'source "{AUTOHEAL_SH}"\n'
        "audit_control() { :; }\n"
        f"COMPOSE_DIR='{repo_path}'\n"
        f"AADS_RELEASE_SHA='{local[:12]}'\n"
        f"{_fn('reject_unpushed_release')}"
        "remediate_deploy_failure dirty_worktree; echo \"rc=$?\"\n"
    )
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert "rc=1" in proc.stdout, proc.stdout + proc.stderr
    assert "route_clean_worktree" not in proc.stdout
