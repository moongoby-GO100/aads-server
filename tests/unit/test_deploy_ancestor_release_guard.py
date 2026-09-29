"""deploy.sh 조상 릴리스 가드 (AADS-DEPLOY-ANCESTOR-RELEASE-GUARD-20260929).

2026-09-29 이미 main 에 흡수된 낡은 SHA(6d20a6eb·71bf61c2·f6aa0d74)가 큐 워커/autoheal
경로로 반복 재시도됐다. 성공했다면 그 뒤 커밋이 운영에서 사라지는 회귀 배포다.
가드는 deploy.sh 의 실제 함수를 잘라 임시 git 저장소 위에서 실행해 검증한다.
"""

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SH = (ROOT / "deploy.sh").read_text()


def _fn(name: str) -> str:
    body = DEPLOY_SH.split("\n" + name + "() {", 1)[1].split("\n}\n", 1)[0]
    return name + "() {" + body + "\n}\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    shas = []
    for i in range(4):
        (repo / "f").write_text(str(i))
        _git(repo, "add", "f")
        env_date = f"2026-09-29T0{i}:00:00+09:00"
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "-m", f"c{i}"],
            check=True,
            env=dict(os.environ, GIT_AUTHOR_DATE=env_date, GIT_COMMITTER_DATE=env_date),
        )
        shas.append(_git(repo, "rev-parse", "--short=12", "HEAD"))
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo, shas


PRELUDE = '''set -Eeuo pipefail
MODE=bluegreen
DEPLOY_RUN_ID="${DEPLOY_RUN_ID:-}"
DEPLOY_CURRENT_PHASE=initializing
DEPLOY_START_EPOCH=$(date +%s)
DEPLOY_PHASE_START_EPOCH=$DEPLOY_START_EPOCH
ACTIVE_CONTAINER=aads-server-green
audit_control() { printf 'AUDIT %s\\n' "$*" >> "$LOG"; }
record_deploy() { printf 'RECORD %s\\n' "$*" >> "$LOG"; }
stop_deploy_heartbeat() { :; }
deploy_observe_init() { :; }
deploy_estimated_remaining_ms() { echo NULL; }
sql_escape() { printf '%s' "${1:-}" | sed "s/'/''/g"; }
deploy_db_available() { return 0; }
docker() {
    case "$*" in
        *image.revision*) printf '%s' "${RUNNING_LABEL:-}" ;;
        *Config.Image*) printf '%s' "${RUNNING_IMAGE:-}" ;;
        *) return 1 ;;
    esac
}
deploy_db_exec() {
    printf 'SQL %s\\n' "$1" >> "$LOG"
    case "$1" in
        *"SELECT COUNT(*)::int"*"ancestor_release_skipped"*) echo "${PRIOR_SKIPS:-0}" ;;
        *"SELECT release_sha"*) echo "$AADS_RELEASE_SHA" ;;
        *"RETURNING id"*) echo "${CLAIM_RUN_ID:-}" ;;
    esac
}
'''

GUARD_FUNCS = "".join(
    _fn(n)
    for n in (
        "deploy_observe_update",
        "deploy_phase_end",
        "running_release_sha",
        "resolve_release_commit",
        "reject_ancestor_release",
        "enforce_ancestor_release_guard",
        "claim_latest_queued_deploy_request",
        "include_queued_ancestors_in_direct_release",
    )
)


def _main_flow() -> str:
    # deploy.sh 본문의 실제 호출 순서를 그대로 잘라 쓴다.
    start = DEPLOY_SH.index("\nclaim_latest_queued_deploy_request\n")
    end = DEPLOY_SH.index('\ndeploy_phase_start "preflight" "running"', start)
    return DEPLOY_SH[start:end] + "\necho PREFLIGHT_CONTINUED\n"


def _run(tmp_path, repo, release, running, **env):
    log = tmp_path / "log"
    log.write_text("")
    script = PRELUDE + GUARD_FUNCS + _main_flow()
    full_env = dict(
        os.environ,
        LOG=str(log),
        COMPOSE_DIR=str(repo),
        AADS_RELEASE_SHA=release,
        RUNNING_LABEL=running,
        AADS_DEPLOY_ANCESTOR_GUARD_FETCH="0",
        DEPLOY_RUN_ID=env.pop("DEPLOY_RUN_ID", "501"),
    )
    full_env.pop("AADS_DEPLOY_QUEUE_WORKER", None)
    full_env.pop("AADS_DEPLOY_ALLOW_ANCESTOR_RELEASE", None)
    full_env.update(env)
    result = subprocess.run(
        ["bash", "-c", script], env=full_env, capture_output=True, text=True, timeout=60
    )
    return result, log.read_text()


def test_ancestor_of_running_release_is_superseded_before_deploy(tmp_path, repo):
    repo_path, shas = repo
    result, log = _run(tmp_path, repo_path, shas[0], shas[3], PRIOR_SKIPS="2")

    assert result.returncode == 0, result.stderr
    assert "PREFLIGHT_CONTINUED" not in result.stdout
    assert "status='superseded'" in log
    assert "status='failed'" not in log
    summary = (
        f"ancestor_release_skipped: release={shas[0]} running={shas[3]} behind=3 reentry=3"
    )
    assert summary in log
    assert "error_summary=NULLIF('" + summary in log
    assert "RECORD skipped bluegreen " + summary in log
    assert "AUDIT ancestor-release-guard" in log


def test_descendant_of_running_release_proceeds(tmp_path, repo):
    repo_path, shas = repo
    result, log = _run(tmp_path, repo_path, shas[3], shas[1])

    assert result.returncode == 0, result.stderr
    assert "PREFLIGHT_CONTINUED" in result.stdout
    assert "superseded" not in log
    assert "ancestor_release_skipped" not in log


def test_same_release_is_left_to_duplicate_live_guard(tmp_path, repo):
    # 한쪽 슬롯만 올라간 같은 릴리스 재배포는 standby 복구다.
    repo_path, shas = repo
    result, log = _run(tmp_path, repo_path, shas[3], shas[3])

    assert result.returncode == 0, result.stderr
    assert "PREFLIGHT_CONTINUED" in result.stdout
    assert "ancestor_release_skipped" not in log


@pytest.mark.parametrize(
    "release,running",
    [
        ("deadbeefdead", None),  # 릴리스 SHA 가 로컬에 없다
        (None, "deadbeefdead"),  # 구동본 SHA 가 로컬에 없다
        (None, ""),  # 구동본 SHA 를 알 수 없다
    ],
)
def test_undeterminable_ancestry_warns_without_blocking(tmp_path, repo, release, running):
    repo_path, shas = repo
    release = shas[0] if release is None else release
    running = shas[3] if running is None else running
    # fetch 도 시도하게 둔다 — origin 이 없으므로 실패하고, 실패해도 막지 않아야 한다.
    result, log = _run(
        tmp_path, repo_path, release, running, AADS_DEPLOY_ANCESTOR_GUARD_FETCH="1"
    )

    assert result.returncode == 0, result.stderr
    assert "PREFLIGHT_CONTINUED" in result.stdout
    assert "superseded" not in log
    assert "not blocking" in result.stdout
    assert "AUDIT ancestor-release-guard" in log and "warning" in log


def test_running_release_falls_back_to_image_tag(tmp_path, repo):
    repo_path, shas = repo
    result, log = _run(
        tmp_path, repo_path, shas[1], "", RUNNING_IMAGE=f"aads-server:{shas[2]}"
    )

    assert result.returncode == 0, result.stderr
    assert "PREFLIGHT_CONTINUED" not in result.stdout
    assert f"release={shas[1]} running={shas[2]} behind=1" in log


def test_guard_runs_in_queue_worker_mode(tmp_path, repo):
    # 오늘 재시도는 전부 큐 워커/autoheal 경로였다. 워커가 claim 한 run 을 닫아야 한다.
    repo_path, shas = repo
    result, log = _run(
        tmp_path,
        repo_path,
        shas[1],
        shas[3],
        AADS_DEPLOY_QUEUE_WORKER="true",
        DEPLOY_RUN_ID="",
        CLAIM_RUN_ID="5250",
    )

    assert result.returncode == 0, result.stderr
    assert "claimed queued deploy_run_id=5250" in result.stdout
    assert "PREFLIGHT_CONTINUED" not in result.stdout
    assert "WHERE id=5250" in log
    assert "status='superseded'" in log
    assert f"ancestor_release_skipped: release={shas[1]} running={shas[3]} behind=2" in log


def test_operator_override_allows_intentional_rollback(tmp_path, repo):
    repo_path, shas = repo
    result, log = _run(
        tmp_path, repo_path, shas[0], shas[3], AADS_DEPLOY_ALLOW_ANCESTOR_RELEASE="1"
    )

    assert result.returncode == 0, result.stderr
    assert "PREFLIGHT_CONTINUED" in result.stdout
    assert "superseded" not in log


def test_guard_sits_before_direct_inclusion_and_preflight():
    claim = DEPLOY_SH.index("\nclaim_latest_queued_deploy_request\n")
    guard = DEPLOY_SH.index("\nenforce_ancestor_release_guard\n")
    include = DEPLOY_SH.index("\ninclude_queued_ancestors_in_direct_release\n")
    preflight = DEPLOY_SH.index('\ndeploy_phase_start "preflight" "running"')
    assert claim < guard < include < preflight
    # 큐 워커 조건문 안에 들어가 있지 않아야 한다.
    assert "AADS_DEPLOY_QUEUE_WORKER" not in _fn("reject_ancestor_release")
    assert "AADS_DEPLOY_QUEUE_WORKER" not in _fn("enforce_ancestor_release_guard")


def test_guard_coexists_with_release_migrations_and_provenance():
    """R2(2026-09-29): 가드를 새 main 에 얹으며 그 사이 들어온 두 변경을 지우지 않았다."""
    # 389882dd — 릴리스 migrations 전량 적용 단계.
    migrations = _fn("run_release_migrations")
    assert 'scripts/apply_release_migrations.sh" --root "$migration_root"' in migrations
    apply_call = DEPLOY_SH.index('apply_release_schema_migrations "schema_migrations"')
    # 0543b4a9 — 성공 후 release provenance 기록 훅.
    provenance = DEPLOY_SH.index('"${COMPOSE_DIR}/scripts/record-release-provenance.sh" \\')
    # 가드는 preflight 에서, 즉 migrations·provenance 보다 앞에서 돈다.
    guard = DEPLOY_SH.index("\nenforce_ancestor_release_guard\n")
    assert guard < apply_call < provenance
    assert DEPLOY_SH.count("\nenforce_ancestor_release_guard\n") == 1
