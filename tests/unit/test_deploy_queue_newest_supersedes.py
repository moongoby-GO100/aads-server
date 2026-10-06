"""deploy.sh 큐 워커: 같은 계열의 최신 queued 릴리스가 조상 릴리스를 대체한다.

2026-10-06 #5600(6a3de230)이 자기 후손 #5601(20a97a9c)보다 먼저 ready head 로 선택돼
최신 수정이 약 20분 늦게 배포됐다. deploy.sh 의 실제 함수를 잘라 임시 git 저장소 위에서 실행한다.
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


def _git(repo: Path, *args: str, env=None) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def _commit(repo: Path, fname: str, content: str, msg: str) -> str:
    (repo / fname).write_text(content)
    _git(repo, "add", fname)
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "--short=12", "HEAD")


@pytest.fixture()
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    base = _commit(repo, "f", "0", "base")
    main_branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    anc = _commit(repo, "f", "1", "ancestor")
    desc = _commit(repo, "f", "2", "descendant")
    _git(repo, "checkout", "-q", "-b", "side", base)
    fork = _commit(repo, "g", "x", "forked")
    _git(repo, "checkout", "-q", main_branch)
    return repo, {"base": base, "anc": anc, "desc": desc, "fork": fork}


# deploy_db_exec 는 상태 파일(QUEUE: "id|sha" 를 created_at 순으로) 위에서 동작하는 가짜다.
PRELUDE = r'''set -Eeuo pipefail
deploy_db_available() { return 0; }
sql_escape() { printf '%s' "${1:-}" | sed "s/'/''/g"; }
deploy_db_exec() {
    printf 'SQL %s\n' "$1" >> "$LOG"
    case "$1" in
        *"SELECT id::text"*) cat "$QUEUE" ;;
        *"superseded_by_newer_release"*)
            local rid
            rid="$(printf '%s' "$1" | sed -n 's/.*WHERE id=\([0-9]*\).*/\1/p' | head -1)"
            grep -v "^${rid}|" "$QUEUE" > "$QUEUE.tmp" || true
            mv "$QUEUE.tmp" "$QUEUE"
            ;;
        *"SELECT release_sha"*) head -1 "$QUEUE" | cut -d'|' -f2 ;;
        *"RETURNING id"*) head -1 "$QUEUE" | cut -d'|' -f1 ;;
    esac
}
'''

FUNCS = "".join(_fn(n) for n in ("supersede_queued_ancestor_releases", "claim_latest_queued_deploy_request"))


def _run(tmp_path, repo, rows, release):
    log = tmp_path / "log"
    log.write_text("")
    queue = tmp_path / "queue"
    queue.write_text("".join(f"{rid}|{sha}\n" for rid, sha in rows))
    script = PRELUDE + FUNCS + "\nclaim_latest_queued_deploy_request\necho \"CLAIMED=${DEPLOY_RUN_ID:-none}\"\n"
    env = dict(
        os.environ,
        LOG=str(log),
        QUEUE=str(queue),
        COMPOSE_DIR=str(repo),
        AADS_RELEASE_SHA=release,
        AADS_DEPLOY_QUEUE_WORKER="true",
    )
    result = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)
    return result, log.read_text(), queue.read_text()


def test_ancestor_is_superseded_and_descendant_wins(tmp_path, repo):
    # 실측 재현: 조상(#5600)이 created_at 상 앞서 있어도 후손(#5601)이 선택된다.
    repo_path, s = repo
    result, log, queue = _run(tmp_path, repo_path, [(5600, s["anc"]), (5601, s["desc"])], s["desc"])

    assert result.returncode == 0, result.stderr
    assert "standing down" not in result.stdout
    assert "CLAIMED=5601" in result.stdout
    assert queue.strip() == f"5601|{s['desc']}"
    assert "status='superseded'" in log
    assert "phase='superseded_by_newer_release'" in log
    assert f"superseded_by_newer_release: release={s['desc']} run=5601" in log
    assert "WHERE id=5600" in log
    assert "status='failed'" not in log


def test_ancestor_worker_stands_down_for_descendant(tmp_path, repo):
    repo_path, s = repo
    result, _log, queue = _run(tmp_path, repo_path, [(5600, s["anc"]), (5601, s["desc"])], s["anc"])

    assert result.returncode == 0, result.stderr
    assert f"standing down: ready head={s['desc']}" in result.stdout
    assert "CLAIMED" not in result.stdout
    assert queue.strip() == f"5601|{s['desc']}"


def test_diverged_releases_keep_created_at_order(tmp_path, repo):
    repo_path, s = repo
    result, log, queue = _run(tmp_path, repo_path, [(5700, s["fork"]), (5701, s["desc"])], s["fork"])

    assert result.returncode == 0, result.stderr
    assert "CLAIMED=5700" in result.stdout
    assert "superseded" not in log
    assert queue.splitlines() == [f"5700|{s['fork']}", f"5701|{s['desc']}"]


def test_git_failure_falls_back_to_created_at_with_warning(tmp_path, repo):
    repo_path, s = repo
    missing = "deadbeefdead"
    result, log, queue = _run(tmp_path, repo_path, [(5800, s["anc"]), (5801, missing)], s["anc"])

    assert result.returncode == 0, result.stderr
    assert "CLAIMED=5800" in result.stdout
    assert "superseded" not in log
    assert f"commit unresolved for run=5801 sha={missing}" in result.stdout
    assert "⚠️" in result.stdout
    assert queue.splitlines() == [f"5800|{s['anc']}", f"5801|{missing}"]


def test_same_sha_duplicates_are_not_superseded(tmp_path, repo):
    repo_path, s = repo
    result, log, queue = _run(tmp_path, repo_path, [(5900, s["desc"]), (5901, s["desc"])], s["desc"])

    assert result.returncode == 0, result.stderr
    assert "CLAIMED=5900" in result.stdout
    assert "superseded" not in log


def test_supersede_is_called_before_head_selection_and_keeps_predicates():
    claim = _fn("claim_latest_queued_deploy_request")
    assert claim.index("supersede_queued_ancestor_releases") < claim.index("SELECT release_sha")
    sup = _fn("supersede_queued_ancestor_releases")
    for predicate in (
        "component='api'",
        "target_env='production'",
        "status='queued'",
        "phase='queued_for_deploy'",
    ):
        assert predicate in sup
