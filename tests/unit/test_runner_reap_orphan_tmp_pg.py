"""AADS-RUNNER-REAP-ORPHAN-TMP-PG-20261015 (R-BG)

2026-10-15 contabo14: /tmp 에 `postgres -D /tmp/...` 고아 클러스터 10개가 ppid=1 로 최장 13일 남아 있었다.
러너가 worktree 를 회수하면서 그 안에서 띄운 임시 PG 를 정지하지 않은 탓이다.

실제 postgres 를 띄우지 않는다. /proc 루트·ps·runuser 를 환경변수로 가짜 주입하고,
pipeline-runner.sh 의 temp-pg-reap 블록을 잘라 실행한다. 데이터 디렉터리는 /tmp 하위 실제 디렉터리다.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts" / "pipeline-runner.sh"
RECLAIM = ROOT / "scripts" / "reclaim_runner_worktrees.sh"

BG_CHILDREN = [
    "checkpointer",
    "background writer",
    "walwriter",
    "autovacuum launcher",
    "logical replication launcher",
    "archiver",
    "io worker 0",
    "walsummarizer",
]


def _new_job() -> str:
    return f"runner-{uuid.uuid4().hex[:8]}"


class Env:
    """가짜 /proc + ps + runuser 와 /tmp 데이터 디렉터리를 가진 시험 환경."""

    def __init__(self, base: Path):
        self.base = base
        self.proc = base / "proc"
        self.proc.mkdir()
        self.bin = base / "bin"
        self.bin.mkdir()
        self.pgbin = base / "pgbin"
        self.pgbin.mkdir()
        (self.pgbin / "postgres").write_text("#!/bin/sh\n")
        for n in ("pg_ctl",):
            (self.pgbin / n).write_text("#!/bin/sh\nexit 0\n")
            (self.pgbin / n).chmod(0o755)
        self.ps_file = base / "ps.txt"
        self.ps_file.write_text("")
        self.runuser_log = base / "runuser.log"
        ps = self.bin / "ps"
        ps.write_text(f'#!/bin/sh\ncat "{self.ps_file}"\n')
        ps.chmod(0o755)
        ru = self.bin / "runuser"
        ru.write_text(f'#!/bin/sh\necho "$@" >> "{self.runuser_log}"\nexit "${{FAKE_RUNUSER_RC:-0}}"\n')
        ru.chmod(0o755)
        self.ps_lines: list[str] = []
        self.tmp_dirs: list[Path] = []
        self.extra_paths: list[Path] = []

    def mkdtmp(self) -> Path:
        d = Path(tempfile.mkdtemp(prefix="tpgreap-", dir="/tmp"))
        self.tmp_dirs.append(d)
        return d

    def add_pg(
        self,
        pid: int,
        data_dir: str,
        job: str | None,
        *,
        port: int | None = 55432,
        sock: str | None = None,
        children: list[str] | None = None,
        environ_readable: bool = True,
    ) -> None:
        args = f"{self.pgbin}/postgres -D {data_dir}"
        if port is not None:
            args += f" -p {port}"
        if sock:
            args += f" -k {sock}"
        self.ps_lines.append(f"{pid} 1 postgres {args}")
        for i, child in enumerate(children or []):
            self.ps_lines.append(f"{pid + 1 + i} {pid} postgres postgres: {child}")
        pdir = self.proc / str(pid)
        pdir.mkdir()
        (pdir / "exe").symlink_to(self.pgbin / "postgres")
        if environ_readable:
            wt = f"/tmp/aads-wt-{job}" if job else "/nowhere"
            (pdir / "environ").write_bytes(
                f"PATH=/usr/bin\0PWD={wt}/tests\0HOME=/root\0".encode()
            )
        self.ps_file.write_text("\n".join(self.ps_lines) + "\n")

    def run(self, func: str, *fargs: str, rc: int = 0, extra: dict | None = None) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "TEMP_PG_PROC_ROOT": str(self.proc),
            "TEMP_PG_PS": str(self.bin / "ps"),
            "TEMP_PG_RUNUSER": str(self.bin / "runuser"),
            "FAKE_RUNUSER_RC": str(rc),
            **(extra or {}),
        }
        script = (
            f"sed -n '/^# >>> temp-pg-reap/,/^# <<< temp-pg-reap/p' '{RUNNER}' > '{self.base}/block.sh'\n"
            f"source '{self.base}/block.sh'\n{func} {' '.join(fargs)}\n"
        )
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)

    def runuser_calls(self) -> list[str]:
        return self.runuser_log.read_text().splitlines() if self.runuser_log.exists() else []

    def cleanup(self) -> None:
        for d in self.tmp_dirs + self.extra_paths:
            shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.cleanup()


def _cluster(env: Env, extra_in_parent: str | None = None) -> tuple[Path, Path]:
    """/tmp/<parent>/{data,log,port} 를 만든다. (parent, data) 반환."""
    parent = env.mkdtmp()
    data = parent / "data"
    data.mkdir()
    (data / "PG_VERSION").write_text("15\n")
    (parent / "log").mkdir()
    (parent / "initdb.log").write_text("x\n")
    (parent / "port").write_text("55432\n")
    if extra_in_parent:
        (parent / extra_in_parent).write_text("keep\n")
    return parent, data


def test_matching_job_is_stopped_and_parent_removed(env):
    job = _new_job()
    parent, data = _cluster(env)
    env.add_pg(4001, str(data), job, children=BG_CHILDREN)
    user = subprocess.run(["stat", "-c", "%U", str(data)], capture_output=True, text=True).stdout.strip()
    r = env.run("_reap_job_temp_pg", job)
    assert r.returncode == 0, r.stderr
    calls = env.runuser_calls()
    assert len(calls) == 1
    assert calls[0] == f"-u {user} -- {env.pgbin}/pg_ctl stop -D {data} -m fast -t 30"
    assert f"job={job} pid=4001 dir={data} action=stopped" in r.stdout
    assert f"job={job} pid=4001 dir={data} action=removed" in r.stdout
    assert not parent.exists()


def test_other_job_is_skipped_silently(env):
    job = _new_job()
    parent, data = _cluster(env)
    env.add_pg(4002, str(data), _new_job())
    r = env.run("_reap_job_temp_pg", job)
    assert r.returncode == 0
    assert env.runuser_calls() == []
    assert data.is_dir()


def test_job_id_prefix_does_not_match_longer_id(env):
    parent, data = _cluster(env)
    env.add_pg(4003, str(data), "runner-abcdef012")
    r = env.run("_reap_job_temp_pg", "runner-abcdef01")
    assert r.returncode == 0
    assert env.runuser_calls() == []
    assert data.is_dir()


def test_unreadable_environ_is_skipped(env):
    job = _new_job()
    parent, data = _cluster(env)
    env.add_pg(4004, str(data), job, environ_readable=False)
    r = env.run("_reap_job_temp_pg", job)
    assert r.returncode == 0
    assert env.runuser_calls() == []
    assert data.is_dir()


def test_client_backend_blocks_reap(env):
    job = _new_job()
    parent, data = _cluster(env)
    env.add_pg(4005, str(data), job, children=["checkpointer", "app appdb [local] idle"])
    r = env.run("_reap_job_temp_pg", job)
    assert r.returncode == 0
    assert "action=skipped:clients=1" in r.stdout
    assert env.runuser_calls() == []
    assert data.is_dir()


@pytest.mark.parametrize(
    "data_dir,port,reason",
    [
        ("/var/lib/postgresql/15/main", 55432, "skipped:data_dir_outside_tmp"),
        ("/tmp", 55432, "skipped:data_dir_is_tmp"),
        ("/tmp/", 55432, "skipped:data_dir_is_tmp"),
        ("/tmp/x/../etc", 55432, "skipped:data_dir_outside_tmp"),
        ("{tmp}", 5432, "skipped:port_5432"),
        ("{tmp}", None, "skipped:port_5432"),
    ],
)
def test_protected_clusters_are_never_touched(env, data_dir, port, reason):
    job = _new_job()
    parent, data = _cluster(env)
    target = data_dir.format(tmp=data)
    env.add_pg(4006, target, job, port=port)
    r = env.run("_reap_job_temp_pg", job)
    assert r.returncode == 0
    assert env.runuser_calls() == []
    assert data.is_dir() and (data / "PG_VERSION").exists()
    assert f"action={reason}" in r.stdout


def test_port_falls_back_to_postmaster_pid_line4(env):
    job = _new_job()
    parent, data = _cluster(env)
    (data / "postmaster.pid").write_text("4007\n/x\n1\n5432\n")
    env.add_pg(4007, str(data), job, port=None)
    r = env.run("_reap_job_temp_pg", job)
    assert "action=skipped:port_5432" in r.stdout
    assert env.runuser_calls() == []
    assert data.is_dir()


def test_symlinked_data_dir_is_skipped(env):
    job = _new_job()
    parent, data = _cluster(env)
    link = env.mkdtmp() / "link"
    link.symlink_to(data)
    env.add_pg(4008, str(link), job)
    r = env.run("_reap_job_temp_pg", job)
    assert "action=skipped:data_dir_symlink_or_missing" in r.stdout
    assert env.runuser_calls() == []
    assert data.is_dir()


def test_pg_ctl_failure_keeps_directory(env):
    job = _new_job()
    parent, data = _cluster(env)
    env.add_pg(4009, str(data), job)
    r = env.run("_reap_job_temp_pg", job, rc=1)
    assert r.returncode == 0
    assert len(env.runuser_calls()) == 1
    assert "action=skipped:pg_ctl_stop_failed(rc=1)" in r.stdout
    assert "action=removed" not in r.stdout
    assert parent.is_dir() and data.is_dir()


def test_foreign_file_in_parent_removes_data_dir_only(env):
    job = _new_job()
    parent, data = _cluster(env, extra_in_parent="notes.txt")
    env.add_pg(4010, str(data), job)
    r = env.run("_reap_job_temp_pg", job)
    assert r.returncode == 0
    assert f"action=removed target={data}" in r.stdout
    assert not data.exists()
    assert parent.is_dir() and (parent / "notes.txt").read_text() == "keep\n"


def test_data_dir_directly_under_tmp_removes_data_dir_only(env):
    job = _new_job()
    data = env.mkdtmp()
    (data / "PG_VERSION").write_text("15\n")
    env.add_pg(4011, str(data), job)
    r = env.run("_reap_job_temp_pg", job)
    assert f"action=removed target={data}" in r.stdout
    assert not data.exists()
    assert Path("/tmp").is_dir()


def test_empty_socket_dir_is_removed_nonempty_is_kept(env):
    job = _new_job()
    parent, data = _cluster(env, extra_in_parent="notes.txt")
    sock_empty = env.mkdtmp()
    env.add_pg(4012, str(data), job, sock=str(sock_empty))
    env.run("_reap_job_temp_pg", job)
    assert not sock_empty.exists()

    job2 = _new_job()
    parent2, data2 = _cluster(env, extra_in_parent="notes.txt")
    sock_busy = env.mkdtmp()
    (sock_busy / ".s.PGSQL.55432.lock").write_text("x")
    env.add_pg(4020, str(data2), job2, sock=str(sock_busy))
    env.run("_reap_job_temp_pg", job2)
    assert sock_busy.is_dir()


def test_multiple_clusters_of_one_job_are_all_reaped(env):
    job = _new_job()
    parents = []
    for i in range(3):
        parent, data = _cluster(env)
        parents.append(parent)
        env.add_pg(4100 + i * 10, str(data), job)
    env.run("_reap_job_temp_pg", job)
    assert len(env.runuser_calls()) == 3
    assert all(not p.exists() for p in parents)


def test_invalid_job_id_and_opt_out_do_nothing(env):
    job = _new_job()
    parent, data = _cluster(env)
    env.add_pg(4030, str(data), job)
    assert env.run("_reap_job_temp_pg", "'../x'").returncode == 0
    assert env.run("_reap_job_temp_pg", job, extra={"RUNNER_TEMP_PG_REAP": "0"}).returncode == 0
    assert env.runuser_calls() == []
    assert data.is_dir()


def test_orphan_sweep_reaps_only_when_worktree_is_gone(env):
    gone_job, alive_job = _new_job(), _new_job()
    p1, d1 = _cluster(env)
    p2, d2 = _cluster(env)
    env.add_pg(4040, str(d1), gone_job)
    env.add_pg(4050, str(d2), alive_job)
    alive_wt = Path(f"/tmp/aads-wt-{alive_job}")
    alive_wt.mkdir()
    env.extra_paths.append(alive_wt)
    r = env.run("_reap_orphan_temp_pg")
    assert r.returncode == 0, r.stderr
    assert not p1.exists()
    assert d2.is_dir()
    assert len(env.runuser_calls()) == 1


def test_orphan_sweep_respects_clients_and_limit(env):
    p_client, d_client = _cluster(env)
    env.add_pg(4060, str(d_client), _new_job(), children=["app db [local] idle"])
    parents = []
    for i in range(3):
        parent, data = _cluster(env)
        parents.append(parent)
        env.add_pg(4200 + i * 10, str(data), _new_job())
    r = env.run("_reap_orphan_temp_pg")
    assert d_client.is_dir()
    assert len(env.runuser_calls()) == 2
    assert sum(p.exists() for p in parents) == 1
    assert "skipped:sweep_limit" in r.stdout


def test_reclaim_script_runs_sweep_without_touching_unknown_worktrees(env, tmp_path):
    job = _new_job()
    parent, data = _cluster(env)
    env.add_pg(4070, str(data), job)
    for name in ("docker", "psql"):
        f = env.bin / name
        f.write_text("#!/bin/sh\nexit 1\n")
        f.chmod(0o755)
    repo = tmp_path / "repo"
    repo.mkdir()
    run_env = {
        **os.environ,
        "PATH": f"{env.bin}:{os.environ['PATH']}",
        "TEMP_PG_PROC_ROOT": str(env.proc),
        "TEMP_PG_PS": str(env.bin / "ps"),
        "TEMP_PG_RUNUSER": str(env.bin / "runuser"),
        "RUNNER_WT_REPO": str(repo),
        "RUNNER_WT_ENV_FILE": str(tmp_path / "runner.env"),
        "RUNNER_WT_ARCHIVE_DIR": str(tmp_path / "archive"),
        "RUNNER_WT_RETENTION_HOURS": "24",
        "DRY_RUN": "0",
    }
    r = subprocess.run(["bash", str(RECLAIM)], env=run_env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert f"job={job} pid=4070 dir={data} action=removed" in r.stdout
    assert not parent.exists()


def test_reclaim_script_dry_run_does_not_stop(env, tmp_path):
    job = _new_job()
    parent, data = _cluster(env)
    env.add_pg(4080, str(data), job)
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in ("docker", "psql"):
        f = env.bin / name
        f.write_text("#!/bin/sh\nexit 1\n")
        f.chmod(0o755)
    run_env = {
        **os.environ,
        "PATH": f"{env.bin}:{os.environ['PATH']}",
        "TEMP_PG_PROC_ROOT": str(env.proc),
        "TEMP_PG_PS": str(env.bin / "ps"),
        "TEMP_PG_RUNUSER": str(env.bin / "runuser"),
        "RUNNER_WT_REPO": str(repo),
        "RUNNER_WT_ENV_FILE": str(tmp_path / "runner.env"),
        "RUNNER_WT_ARCHIVE_DIR": str(tmp_path / "archive"),
        "DRY_RUN": "1",
    }
    r = subprocess.run(["bash", str(RECLAIM)], env=run_env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert "action=skipped:dry_run" in r.stdout
    assert env.runuser_calls() == []
    assert data.is_dir()


def test_runner_calls_reap_before_every_worktree_reclaim_site():
    text = RUNNER.read_text()
    lines = text.splitlines()
    sites = [
        i for i, ln in enumerate(lines)
        if "worktree remove" in ln and ('"$worktree_dir"' in ln or '"/tmp/aads-wt-${job_id}"' in ln)
    ]
    assert len(sites) >= 8
    for i in sites:
        window = "\n".join(lines[max(0, i - 6): i + 1])
        assert '_reap_job_temp_pg "$job_id"' in window, f"line {i + 1}: 회수 직전 _reap_job_temp_pg 호출 없음"
    assert "_reap_job_temp_pg" in text.split("_reclaim_finished_worktree() {", 1)[1].split("\n}\n", 1)[0]


def test_scripts_syntax_ok():
    for f in (RUNNER, RECLAIM):
        assert subprocess.run(["bash", "-n", str(f)], capture_output=True).returncode == 0
