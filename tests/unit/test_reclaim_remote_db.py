"""AADS-RUNNER-WT-RECLAIM-REMOTE-DB-20261012

contabo14 에는 aads-postgres 컨테이너가 없고 러너의 PGHOST 등은 export 되지 않은 셸 변수라, 주기 회수
스크립트의 psql 이 로컬 소켓의 다른 DB 로 붙어 모든 job 이 unknown(168h 보존)이 됐다.

실제 git 워크트리 + 모의 docker/psql 로 회수 스크립트를, 러너에서 잘라 낸 함수로 호출부를 검증한다.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RECLAIM = ROOT / "scripts" / "reclaim_runner_worktrees.sh"
RUNNER = ROOT / "scripts" / "pipeline-runner.sh"
RUNNER_LOCAL = ROOT / "scripts" / "pipeline-runner.sh.local"

SECRET = "s3cr3t-pw-for-test"
PG_ENV = {
    "PGHOST": "db.example.internal",
    "PGPORT": "6543",
    "PGUSER": "runner_user",
    "PGDATABASE": "runner_db",
    "PGPASSWORD": SECRET,
}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def old_wt(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README").write_text("test\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-qm", "initial")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    job_id = f"runner-{uuid.uuid4().hex[:8]}"
    path = Path(f"/tmp/aads-wt-{job_id}")
    _git(repo, "worktree", "add", "--detach", str(path), "HEAD")
    old = time.time() - 30 * 3600
    for root, dirs, files in os.walk(path):
        for name in files:
            os.utime(Path(root) / name, (old, old))
        for name in dirs:
            os.utime(Path(root) / name, (old, old))
    os.utime(path, (old, old))
    try:
        yield repo, path, job_id, tmp_path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _stubs(tmp_path: Path, *, docker_ok: bool, psql_ok: bool, job_id: str, status: str = "done",
           docker_status: str | None = None) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name in ("git", "sed", "tail", "date", "find", "timeout", "du", "awk", "mktemp", "rm", "cat",
                 "grep", "mv", "rmdir", "mkdir", "env", "bash", "dirname", "printf", "tr", "hostname"):
        target = bin_dir / name
        if not target.exists() and shutil.which(name):
            target.symlink_to(shutil.which(name))
    (bin_dir / "lsof").write_text("#!/bin/sh\nexit 1\n")
    (bin_dir / "lsof").chmod(0o755)
    rows = f"{job_id}|{status}"
    docker_rows = f"{job_id}|{docker_status or status}"
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{tmp_path}/docker.args"\n'
        + (f"cat <<'ROWS'\n{docker_rows}\nROWS\n" if docker_ok else "exit 1\n")
    )
    docker.chmod(0o755)
    psql = bin_dir / "psql"
    psql.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{tmp_path}/psql.args"\n'
        f'echo "PGPASSWORD=$PGPASSWORD" >> "{tmp_path}/psql.env"\n'
        + (f"cat <<'ROWS'\n{rows}\nROWS\n" if psql_ok else "exit 1\n")
    )
    psql.chmod(0o755)
    return bin_dir


def _run_reclaim(old_wt, *, docker_ok=False, psql_ok=True, env=None, status="done", docker_status=None):
    repo, path, job_id, tmp_path = old_wt
    bin_dir = _stubs(tmp_path, docker_ok=docker_ok, psql_ok=psql_ok, job_id=job_id, status=status,
                       docker_status=docker_status)
    base = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    run_env = base | {
        "PATH": str(bin_dir),
        "RUNNER_WT_REPO": str(repo),
        "RUNNER_WT_RETENTION_HOURS": "24",
        "RUNNER_WT_MAX_KEEP": "0",
        "RUNNER_WT_ENV_FILE": str(tmp_path / "runner.env"),
        "RUNNER_WT_ARCHIVE_DIR": str(tmp_path / "archive"),
    } | (env or {})
    return subprocess.run(["/bin/bash", str(RECLAIM)], env=run_env, text=True, capture_output=True,
                          timeout=30, check=False)


def _read(tmp_path: Path, name: str) -> str:
    f = tmp_path / name
    return f.read_text() if f.exists() else ""


def test_pg_env_reads_status_through_psql_without_docker(old_wt):
    _, path, _, tmp_path = old_wt
    result = _run_reclaim(old_wt, docker_ok=False, psql_ok=True, env=PG_ENV)
    assert result.returncode == 0, result.stderr
    assert not path.exists()
    assert "DB unavailable" not in result.stderr
    args = _read(tmp_path, "psql.args")
    assert "-h db.example.internal" in args and "-p 6543" in args
    assert "-U runner_user" in args and "-d runner_db" in args


def test_password_only_travels_by_environment_never_argv(old_wt):
    _, _, _, tmp_path = old_wt
    result = _run_reclaim(old_wt, docker_ok=False, psql_ok=True, env=PG_ENV)
    assert f"PGPASSWORD={SECRET}" in _read(tmp_path, "psql.env")
    assert SECRET not in _read(tmp_path, "psql.args")
    assert SECRET not in _read(tmp_path, "docker.args")
    assert SECRET not in result.stdout + result.stderr


def test_docker_path_unchanged_and_uses_pg_container_name(old_wt):
    _, path, _, tmp_path = old_wt
    result = _run_reclaim(old_wt, docker_ok=True, psql_ok=False, env={"PG_CONTAINER": "custom-pg"})
    assert result.returncode == 0, result.stderr
    assert not path.exists()
    assert _read(tmp_path, "docker.args").startswith("exec custom-pg psql -U aads -d aads")
    assert _read(tmp_path, "psql.args") == ""


def test_empty_pg_container_skips_local_docker_and_reads_remote_db(old_wt):
    # DB_MODE=psql 러너는 PG_CONTAINER="" 를 넘긴다. 로컬 aads-postgres 가 있어도(오래된 복제본) 쓰면 안 된다.
    _, path, _, tmp_path = old_wt
    result = _run_reclaim(old_wt, docker_ok=True, psql_ok=True, env=PG_ENV | {"PG_CONTAINER": ""},
                          status="awaiting_approval", docker_status="done")
    assert result.returncode == 0, result.stderr
    assert path.is_dir()
    assert _read(tmp_path, "docker.args") == ""
    assert "-h db.example.internal" in _read(tmp_path, "psql.args")


def test_unset_pg_container_still_prefers_docker_over_psql(old_wt):
    _, path, _, tmp_path = old_wt
    _run_reclaim(old_wt, docker_ok=True, psql_ok=True, env=PG_ENV, status="awaiting_approval",
                 docker_status="done")
    assert not path.exists()
    assert _read(tmp_path, "docker.args").startswith("exec aads-postgres psql")


def test_empty_pg_container_and_psql_failure_preserves_unknown(old_wt):
    _, path, _, tmp_path = old_wt
    result = _run_reclaim(old_wt, docker_ok=True, psql_ok=False, env=PG_ENV | {"PG_CONTAINER": ""})
    assert path.is_dir()
    assert _read(tmp_path, "docker.args") == ""
    assert "docker=none" in result.stderr


def test_default_container_name_is_aads_postgres(old_wt):
    _, path, _, tmp_path = old_wt
    _run_reclaim(old_wt, docker_ok=True, psql_ok=False)
    assert not path.exists()
    assert _read(tmp_path, "docker.args").startswith("exec aads-postgres psql -U aads -d aads")


def test_no_pg_env_keeps_bare_psql_call(old_wt):
    _, path, _, tmp_path = old_wt
    _run_reclaim(old_wt, docker_ok=False, psql_ok=True)
    assert not path.exists()
    args = _read(tmp_path, "psql.args").strip()
    assert args.startswith("-X -qAtc") and "-h" not in args.split()[:1]


def test_both_paths_failing_still_preserves_unknown_jobs(old_wt):
    _, path, _, _ = old_wt
    result = _run_reclaim(old_wt, docker_ok=False, psql_ok=False, env=PG_ENV)
    assert result.returncode == 0, result.stderr
    assert path.is_dir()
    assert "DB unavailable; preserving unknown jobs by default" in result.stderr
    assert "db.example.internal:6543" in result.stderr
    assert SECRET not in result.stderr
    assert "skipped_unknown=1" in result.stdout and "reclaimed=0" in result.stdout


def test_nonterminal_status_read_via_psql_is_preserved(old_wt):
    _, path, _, _ = old_wt
    _run_reclaim(old_wt, docker_ok=False, psql_ok=True, env=PG_ENV, status="awaiting_approval")
    assert path.is_dir()


# ── 러너 호출부 ─────────────────────────────────────────────────────────


def _extract(name: str, text: str) -> str:
    start = text.index(f"\n{name}() {{\n") + 1
    end = text.index("\n}\n", start) + 3
    return text[start:end]


def _run_cleanup(tmp_path: Path, *, db_mode: str, reclaim_body: str, post: str = "", flag: str = "db.flag"):
    run_dir = tmp_path / f"runner_{db_mode}_{uuid.uuid4().hex[:6]}"
    run_dir.mkdir()
    text = RUNNER.read_text(encoding="utf-8")
    funcs = "\n".join(_extract(n, text) for n in ("_cleanup_old_artifacts", "_warn_reclaimer_db_unavailable"))
    (run_dir / "pipeline-runner.sh").write_text(funcs + "\n")
    fake = run_dir / "reclaim_runner_worktrees.sh"
    fake.write_text("#!/usr/bin/env bash\n" + reclaim_body + "\n")
    fake.chmod(0o755)
    logf = tmp_path / "sweep.log"
    script = f"""
LOGF={logf}
log() {{ echo "LOG $*" >> "$LOGF"; }}
lookup_error_book() {{ echo "ERRBOOK $2 :: $(cat "$1")" >> "$LOGF"; }}
curl() {{ echo "CURL $*" >> "$LOGF"; }}
export TELEGRAM_BOT_TOKEN=tok TELEGRAM_CHAT_ID=chat RUNNER_HOST_NAME=testhost
export RECLAIMER_DB_FLAG={tmp_path}/{flag}
ARTIFACT_DIR={tmp_path}/artifacts
ARTIFACT_MAX_AGE_HOURS=24
PGHOST=remote-db PGPORT=6543 PGUSER=ru PGDATABASE=rdb PGPASSWORD={SECRET}
DB_MODE={db_mode}
PG_CONTAINER=my-pg
source {run_dir}/pipeline-runner.sh
_cleanup_old_artifacts
{post}
"""
    proc = subprocess.run(["bash", "-c", script], env={k: v for k, v in os.environ.items() if not k.startswith("PG")},
                          text=True, capture_output=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    return logf.read_text() if logf.exists() else ""


DUMP_ENV = (
    'echo "PGHOST=$PGHOST PGPORT=$PGPORT PGUSER=$PGUSER PGDATABASE=$PGDATABASE '
    'PGPASSWORD=$PGPASSWORD PG_CONTAINER=$PG_CONTAINER ARGC=$#"\n'
    'echo "[reclaim-wt] reclaimed=3 freed=10MB archived=0 skipped_active=0 skipped_recent=0 skipped_unknown=0 '
    'skipped_unknown_repo=0 skipped_archive_failed=0 reclaimed_over_cap=0"'
)


def test_cleanup_passes_runner_db_settings_to_the_reclaimer_only(tmp_path):
    out = _run_cleanup(tmp_path, db_mode="psql", reclaim_body=DUMP_ENV)
    assert f"PGHOST=remote-db PGPORT=6543 PGUSER=ru PGDATABASE=rdb PGPASSWORD={SECRET}" in out
    # psql 모드에서는 빈 값을 명시해 스크립트의 docker 조회를 건너뛰게 한다
    assert "PG_CONTAINER= ARGC=0" in out
    assert "WORKTREE_SWEEP [reclaim-wt] reclaimed=3" in out


def test_cleanup_passes_container_name_in_docker_mode(tmp_path):
    out = _run_cleanup(tmp_path, db_mode="docker", reclaim_body=DUMP_ENV)
    assert "PG_CONTAINER=my-pg ARGC=0" in out


def test_cleanup_does_not_export_pg_settings_to_other_children(tmp_path):
    post = "bash -c 'echo CHILD_SEES=[$PGHOST$PGPASSWORD$PG_CONTAINER]' >> \"$LOGF\""
    out = _run_cleanup(tmp_path, db_mode="docker", reclaim_body="exit 0", post=post)
    assert "CHILD_SEES=[]" in out
    body = _extract("_cleanup_old_artifacts", RUNNER.read_text(encoding="utf-8"))
    assert "export PG" not in body
    assert "--password" not in body and " -W" not in body


def test_cleanup_logs_every_sweep_line_and_warns_once_on_db_unavailable(tmp_path):
    body = (
        'echo "[reclaim-wt] DB unavailable; preserving unknown jobs by default" >&2\n'
        'echo "[reclaim-wt] reclaimed=0 freed=0MB archived=0 skipped_unknown=5"'
    )
    out = _run_cleanup(tmp_path, db_mode="psql", reclaim_body=body)
    assert "WORKTREE_SWEEP [reclaim-wt] DB unavailable" in out
    assert "WORKTREE_SWEEP [reclaim-wt] reclaimed=0" in out
    assert out.count("WARN STALE_WORKTREE_CLEANUP: reclaimer DB unavailable") == 1
    assert out.count("CURL ") == 1 and "chat_id=chat" in out and "testhost" in out
    assert out.count("ERRBOOK reclaimer-db") == 1
    assert SECRET not in out

    # 같은 플래그가 24h 이내이므로 두 번째 주기에는 요약 줄만 늘고 경고·텔레그램·오류사전은 그대로다(로그는 누적).
    again = _run_cleanup(tmp_path, db_mode="psql", reclaim_body=body)
    assert again.count("WORKTREE_SWEEP [reclaim-wt] DB unavailable") == 2
    assert again.count("WARN STALE_WORKTREE_CLEANUP: reclaimer DB unavailable") == 1
    assert again.count("CURL ") == 1
    assert again.count("ERRBOOK reclaimer-db") == 1


def test_cleanup_warns_again_after_interval(tmp_path):
    body = 'echo "[reclaim-wt] DB unavailable; preserving unknown jobs by default" >&2'
    _run_cleanup(tmp_path, db_mode="psql", reclaim_body=body)
    flag = tmp_path / "db.flag"
    old = time.time() - 3 * 86400
    os.utime(flag, (old, old))
    again = _run_cleanup(tmp_path, db_mode="psql", reclaim_body=body)
    assert again.count("WARN STALE_WORKTREE_CLEANUP: reclaimer DB unavailable") == 2


def test_cleanup_logs_when_reclaimer_prints_nothing(tmp_path):
    out = _run_cleanup(tmp_path, db_mode="psql", reclaim_body="exit 0")
    assert "WORKTREE_SWEEP no output" in out


def test_cleanup_does_not_warn_when_db_is_reachable(tmp_path):
    out = _run_cleanup(tmp_path, db_mode="psql", reclaim_body=DUMP_ENV)
    assert "reclaimer DB unavailable" not in out
    assert "CURL" not in out


def test_runner_and_local_copy_are_identical():
    assert RUNNER.read_bytes() == RUNNER_LOCAL.read_bytes()
