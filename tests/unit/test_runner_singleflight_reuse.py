"""무거운 검증 명령 single-flight 합류 + 동일 입력 결과 재사용.

(AADS-RUNNER-LR02-SINGLEFLIGHT-REUSE-20261010, PRD R2 LR02/LR05/LR06)
shim 은 pipeline-runner.sh 안에 내장되어 있다 — 설치본을 만들어 실제 bash 로 돌린다.
"""
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
ROOT = SCRIPTS.parent
SH = SCRIPTS / "pipeline-runner.sh"
SRC = SH.read_text()

FUNCS = (
    "runner_heavy_publish_policy",
    "runner_heavy_install_shims",
    "runner_heavy_job_env",
)

pytestmark = pytest.mark.skipif(
    not all(shutil.which(c) for c in ("git", "flock", "sha256sum", "mkfifo", "tail", "tee", "timeout", "find", "awk")),
    reason="shim 이 쓰는 도구가 없음",
)


def _function(name: str) -> str:
    lines = SRC.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(f"{name}() {{"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[start : end + 1])


def _bash(body: str, env: dict | None = None):
    script = "set -eo pipefail\nlog() { :; }\n" + "\n".join(_function(n) for n in FUNCS) + "\n" + body
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, env={**os.environ, **(env or {})})


# ── 픽스처 ───────────────────────────────────────────────────────────

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env={**os.environ, **GIT_ENV})
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _make_repo(root: Path, files: dict[str, str]) -> Path:
    repo = root / "aads-server"
    repo.mkdir()
    _git(repo, "init", "-q")
    for rel, content in files.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    _git(repo, "update-index", "--add", *files)
    tree = _git(repo, "write-tree")
    commit = _git(repo, "commit-tree", tree, "-m", "init")
    _git(repo, "update-ref", "HEAD", commit)
    return repo


class Env:
    def __init__(self, tmp_path: Path, reuse_marker: bool = True, files: dict | None = None):
        self.tmp = tmp_path
        self.lane = tmp_path / "lane"
        self.results = tmp_path / "results"
        self.count = tmp_path / "count"
        self.repo = _make_repo(tmp_path, files or {"app.py": "x = 1\n", "tests/test_a.py": "def test_a(): pass\n"})
        r = _bash(f"RUNNER_HEAVY_DIR={self.lane}\nrunner_heavy_install_shims\n")
        assert r.returncode == 0, r.stderr
        (self.lane / "policy").write_text("slots=4\nurgent=0\nrevision=1\n")
        if reuse_marker:
            (self.lane / "reuse").write_text("")
        self.real = tmp_path / "real"
        self.real.mkdir()
        for name in ("pytest", "npm"):
            f = self.real / name
            f.write_text(
                '#!/bin/sh\n'
                'echo run >> "$FAKE_COUNT"\n'
                'echo "$$" >> "$FAKE_COUNT.pids"\n'
                '[ -n "$FAKE_SLEEP" ] && sleep "$FAKE_SLEEP"\n'
                f'echo "OUT {name} $*"\n'
                f'echo "ERR {name}" >&2\n'
                'exit "${FAKE_RC:-0}"\n'
            )
            f.chmod(0o755)
        self.procs: list[subprocess.Popen] = []

    def env(self, **extra) -> dict:
        e = {
            **os.environ,
            "PATH": f"{self.lane}/bin:{self.real}:{os.environ['PATH']}",
            "AADS_HEAVY_LANE_DIR": str(self.lane),
            "AADS_HEAVY_POLL_SEC": "0.1",
            "AADS_RESULT_DIR": str(self.results),
            "AADS_HEAVY_JOB_ID": "runner-t1",
            "FAKE_COUNT": str(self.count),
            "HOME": str(self.tmp),
            **GIT_ENV,
        }
        e.pop("RUNNER_RESULT_REUSE", None)
        e.update(extra)
        return e

    def runs(self) -> int:
        return len(self.count.read_text().split()) if self.count.exists() else 0

    def popen(self, args=("tests/test_a.py",), cmd="pytest", **extra) -> subprocess.Popen:
        p = subprocess.Popen(
            [str(self.lane / "bin" / cmd), *args], cwd=self.repo, env=self.env(**extra),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        self.procs.append(p)
        return p

    def run(self, args=("tests/test_a.py",), cmd="pytest", timeout=30, **extra) -> subprocess.CompletedProcess:
        p = self.popen(args, cmd, **extra)
        out, err = p.communicate(timeout=timeout)
        return subprocess.CompletedProcess(p.args, p.returncode, out, err)

    def events(self, kind: str | None = None) -> list[dict]:
        f = self.lane / "events" / "runner-t1.jsonl"
        out = []
        if f.exists():
            for line in f.read_text().splitlines():
                ev, meta = line.split("\t", 1)
                assert ev == json.loads(meta)["event"]
                if kind is None or ev == kind:
                    out.append(json.loads(meta))
        return out

    def wait_lease(self, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.results.exists() and list(self.results.glob("S-*")) and self.runs() >= 1:
                return
            time.sleep(0.05)
        raise AssertionError("owner lease never appeared")

    def cleanup(self):
        for p in self.procs:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        pids = Path(str(self.count) + ".pids")
        if pids.exists():
            for pid in pids.read_text().split():
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except (ProcessLookupError, ValueError):
                    pass

    def stored(self) -> list[Path]:
        return sorted(self.results.glob("R-*.meta")) if self.results.exists() else []


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.cleanup()


# ── 스크립트 일반 ────────────────────────────────────────────────────

def test_script_valid_and_local_copy_in_sync():
    assert subprocess.run(["bash", "-n", str(SH)]).returncode == 0
    assert SRC == (SCRIPTS / "pipeline-runner.sh.local").read_text()


def test_shim_body_has_no_column_zero_brace():
    body = _function("runner_heavy_install_shims")
    assert sum(1 for l in body.splitlines() if l == "}") == 1


def test_shim_reuses_existing_shim_not_a_new_one():
    assert SRC.count("<<'EOF_AADS_HEAVY_SHIM'") == 1
    assert SRC.count("runner_heavy_install_shims()") == 1


# ── 동시 2요청: 실행 1회 ─────────────────────────────────────────────

def test_concurrent_requests_execute_once_then_hit(env):
    p1 = env.popen(FAKE_SLEEP="2")
    env.wait_lease()
    p2 = env.popen(FAKE_SLEEP="2")
    out1, err1 = p1.communicate(timeout=30)
    out2, err2 = p2.communicate(timeout=30)
    assert (p1.returncode, p2.returncode) == (0, 0)
    assert env.runs() == 1
    assert "OUT pytest tests/test_a.py" in out1 and "OUT pytest tests/test_a.py" in out2
    assert "ERR pytest" in err1 and "합류" in err2
    joins = env.events("heavy_lane_result_join")
    assert len(joins) == 1 and joins[0]["outcome"] == "shared" and joins[0]["rc"] == 0
    misses = env.events("heavy_lane_result_miss")
    assert len(misses) == 1 and misses[0]["miss_reason"] == "no_entry" and misses[0]["mode"] == "owner"

    r3 = env.run()
    assert r3.returncode == 0 and env.runs() == 1
    assert "OUT pytest tests/test_a.py" in r3.stdout and "재사용" in r3.stderr
    hits = env.events("heavy_lane_result_hit")
    assert len(hits) == 1 and hits[0]["ttl_s"] == 21600


def test_joiner_receives_same_nonzero_exit_and_failure_is_not_stored(env):
    p1 = env.popen(FAKE_SLEEP="2", FAKE_RC="3")
    env.wait_lease()
    p2 = env.popen(FAKE_RC="3")
    p1.communicate(timeout=30)
    out2, _ = p2.communicate(timeout=30)
    assert (p1.returncode, p2.returncode) == (3, 3)
    assert "OUT pytest" in out2 and env.runs() == 1
    assert env.stored() == [] and not list(env.results.glob("R-*"))
    nots = env.events("heavy_lane_result_not_stored")
    assert nots and nots[0]["reason"] == "nonzero_exit" and nots[0]["rc"] == 3
    # 동시 합류가 아닌 다음 요청은 실패 결과를 재사용하지 않고 다시 실행한다.
    r = env.run(FAKE_RC="3")
    assert r.returncode == 3 and env.runs() == 2


def test_failure_then_success_runs_again_and_stores_only_success(env):
    assert env.run(FAKE_RC="1").returncode == 1
    assert env.stored() == []
    assert env.run().returncode == 0 and env.runs() == 2
    assert len(env.stored()) == 1
    assert env.run().returncode == 0 and env.runs() == 2


# ── 트리 변경 → miss ─────────────────────────────────────────────────

def test_tree_change_misses_and_reverting_hits_again(env):
    assert env.run().returncode == 0 and env.runs() == 1
    assert env.run().returncode == 0 and env.runs() == 1
    index_before = (env.repo / ".git/index").read_bytes()

    (env.repo / "new_untracked.py").write_text("y = 2\n")
    assert env.run().returncode == 0 and env.runs() == 2
    (env.repo / "new_untracked.py").unlink()

    (env.repo / "app.py").write_text("x = 2\n")
    assert env.run().returncode == 0 and env.runs() == 3
    assert env.run().returncode == 0 and env.runs() == 3  # 변경된 트리도 한 번 돌았으면 재사용
    (env.repo / "app.py").write_text("x = 1\n")
    assert env.run().returncode == 0 and env.runs() == 3  # 원래 트리로 돌아오면 첫 결과

    assert (env.repo / ".git/index").read_bytes() == index_before  # 실제 index 불변
    assert _git(env.repo, "status", "--porcelain") == ""


def test_different_argv_is_a_different_key(env):
    assert env.run(("tests/test_a.py",)).returncode == 0
    assert env.run(("tests/test_b.py",)).returncode == 0
    assert env.runs() == 2


def test_lockfile_change_misses(tmp_path):
    e = Env(tmp_path, files={"app.py": "x\n", "requirements.txt": "a==1\n"})
    try:
        assert e.run().returncode == 0 and e.runs() == 1
        assert e.run().returncode == 0 and e.runs() == 1
        (e.repo / "requirements.txt").write_text("a==2\n")
        assert e.run().returncode == 0 and e.runs() == 2
    finally:
        e.cleanup()


def test_tree_change_during_run_is_not_stored(env):
    slow = env.real / "pytest"
    slow.write_text('#!/bin/sh\necho run >> "$FAKE_COUNT"\ntouch "$PWD/made_during_run.txt"\necho done\n')
    assert env.run().returncode == 0
    assert env.stored() == []
    nots = env.events("heavy_lane_result_not_stored")
    assert nots and nots[-1]["reason"] == "tree_changed_during_run"


# ── lease 소유자 사망 ────────────────────────────────────────────────

def test_owner_killed_then_next_request_reruns(env):
    p1 = env.popen(FAKE_SLEEP="20")
    env.wait_lease()
    os.kill(p1.pid, signal.SIGKILL)
    p1.wait(timeout=10)
    t0 = time.time()
    r = env.run()
    assert r.returncode == 0 and "OUT pytest" in r.stdout
    assert time.time() - t0 < 15
    assert env.runs() == 2
    assert env.stored() and not list(env.results.glob("S-*"))


def test_joiner_waiting_on_killed_owner_takes_over(env):
    p1 = env.popen(FAKE_SLEEP="20")
    env.wait_lease()
    p2 = env.popen()
    time.sleep(1)
    os.kill(p1.pid, signal.SIGKILL)
    out2, _ = p2.communicate(timeout=30)
    assert p2.returncode == 0 and "OUT pytest" in out2
    assert env.runs() == 2
    reasons = [m["miss_reason"] for m in env.events("heavy_lane_result_miss")]
    assert "join_owner_gone" in reasons


def test_stale_lease_while_lock_foreign_held_runs_directly(env):
    assert env.run(FAKE_RC="3").returncode == 3
    key = next(env.results.glob("L-*")).name[2:]
    (env.results / f"S-{key}").write_text("999999 1\n")
    holder = subprocess.Popen(["flock", "-x", str(env.results / f"L-{key}"), "sleep", "30"], start_new_session=True)
    env.procs.append(holder)
    time.sleep(0.3)
    t0 = time.time()
    r = env.run()
    assert r.returncode == 0 and env.runs() == 2
    assert time.time() - t0 < 15
    assert any(m["miss_reason"] == "lease_owner_dead" and m["mode"] == "direct" for m in env.events("heavy_lane_result_miss"))
    assert not (env.results / f"S-{key}").exists()


def test_lease_with_recycled_pid_is_treated_dead(env):
    assert env.run(FAKE_RC="3").returncode == 3
    key = next(env.results.glob("L-*")).name[2:]
    (env.results / f"S-{key}").write_text(f"{os.getpid()} 1\n")  # 살아 있는 pid + 어긋난 start_ticks
    holder = subprocess.Popen(["flock", "-x", str(env.results / f"L-{key}"), "sleep", "30"], start_new_session=True)
    env.procs.append(holder)
    time.sleep(0.3)
    r = env.run()
    assert r.returncode == 0
    assert any(m["miss_reason"] == "lease_owner_dead" for m in env.events("heavy_lane_result_miss"))


# ── 비활성 스위치 ────────────────────────────────────────────────────

def test_env_switch_disables_everything(env):
    for _ in range(2):
        assert env.run(RUNNER_RESULT_REUSE="0").returncode == 0
    assert env.runs() == 2
    assert not env.results.exists()
    assert env.events() == []


def test_without_lane_marker_nothing_happens(tmp_path):
    e = Env(tmp_path, reuse_marker=False)
    try:
        for _ in range(2):
            assert e.run().returncode == 0
        assert e.runs() == 2 and not e.results.exists() and e.events() == []
    finally:
        e.cleanup()


def test_install_and_build_commands_are_never_reused(env):
    for args in (("ci",), ("install",), ("run", "build")):
        assert env.run(args, cmd="npm").returncode == 0
        assert env.run(args, cmd="npm").returncode == 0
    assert env.runs() == 6
    assert not env.results.exists() or not list(env.results.glob("R-*"))
    assert env.run(("test",), cmd="npm").returncode == 0
    assert env.run(("test",), cmd="npm").returncode == 0
    assert env.runs() == 7  # npm test 는 검증 명령 — 두 번째는 재사용


def test_report_artifact_options_skip_reuse(env):
    for _ in range(2):
        assert env.run(("--junitxml=out.xml", "tests/test_a.py")).returncode == 0
    assert env.runs() == 2 and env.events() == []


def test_not_a_git_tree_falls_back_to_plain_exec(env, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    for _ in range(2):
        r = subprocess.run([str(env.lane / "bin/pytest"), "x"], cwd=plain, env=env.env(), capture_output=True, text=True, timeout=30)
        assert r.returncode == 0 and "OUT pytest x" in r.stdout
    assert env.runs() == 2
    assert [m["miss_reason"] for m in env.events("heavy_lane_result_miss")] == ["not_git_worktree"] * 2


# ── TTL · 크기 상한 · 출력 꼬리 ──────────────────────────────────────

def test_ttl_expiry_misses(env):
    assert env.run(AADS_RESULT_TTL_SEC="1").returncode == 0
    time.sleep(2.2)
    assert env.run(AADS_RESULT_TTL_SEC="1").returncode == 0
    assert env.runs() == 2


def test_size_cap_evicts_oldest_first(env):
    cap = 700
    for i in range(6):
        assert env.run((f"tests/t{i}.py",), AADS_RESULT_MAX_BYTES=str(cap)).returncode == 0
        time.sleep(0.05)
    files = [f for f in env.results.iterdir() if f.is_file() and f.name[:2] in ("R-", "F-")]
    assert 0 < sum(f.stat().st_size for f in files) <= cap
    assert env.runs() == 6
    assert env.run(("tests/t5.py",), AADS_RESULT_MAX_BYTES=str(cap)).returncode == 0
    assert env.runs() == 6  # 가장 최근 것은 남아 있다
    assert env.run(("tests/t0.py",), AADS_RESULT_MAX_BYTES=str(cap)).returncode == 0
    assert env.runs() == 7  # 가장 오래된 것은 정리됐다


def test_output_tail_is_capped_at_64kb(env):
    big = env.real / "pytest"
    big.write_text(
        '#!/bin/sh\necho run >> "$FAKE_COUNT"\n'
        'i=0; while [ $i -lt 4000 ]; do echo "line-$i-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"; i=$((i+1)); done\n'
        'echo FINAL-LINE\n'
    )
    r1 = env.run()
    assert r1.returncode == 0 and "line-0-" in r1.stdout and len(r1.stdout) > 200000  # 소유자는 전체 출력을 그대로 받는다
    r2 = env.run()
    assert env.runs() == 1
    assert len(r2.stdout.encode()) <= 65536 and "FINAL-LINE" in r2.stdout and "line-0-" not in r2.stdout


# ── 보고 · 테스트 계획 (LR06) ────────────────────────────────────────

def test_test_plan_event_uses_existing_map_and_keeps_full_validation(tmp_path):
    files = {
        "scripts/pre_commit_test_map.py": (SCRIPTS / "pre_commit_test_map.py").read_text(),
        "scripts/runner_live_update.sh": "echo 1\n",
        "tests/unit/test_runner_live_update_hold.py": "def test_a(): pass\n",
        "tests/unit/test_runner_sync_launcher.py": "def test_b(): pass\n",
    }
    e = Env(tmp_path, files=files)
    try:
        (e.repo / "scripts/runner_live_update.sh").write_text("echo 2\n")
        assert e.run(("tests/unit/test_runner_live_update_hold.py",)).returncode == 0
        plan = e.events("heavy_lane_test_plan")
        assert len(plan) == 1
        assert plan[0]["changed_files"] == 1 and plan[0]["mapped_tests"] == 2
        assert plan[0]["in_command"] == ["tests/unit/test_runner_live_update_hold.py"]
        assert plan[0]["deferred"] == ["tests/unit/test_runner_sync_launcher.py"]
        assert plan[0]["full_validation_at_release"] == "kept"
        assert e.run(("tests/unit/test_runner_live_update_hold.py",)).returncode == 0  # 같은 계획은 다시 기록하지 않는다
        assert len(e.events("heavy_lane_test_plan")) == 1
    finally:
        e.cleanup()


def test_events_are_flushable_names(env):
    env.run()
    env.run()
    for ev in env.events():
        assert ev["event"].startswith("heavy_lane_") and all(c.isalpha() or c == "_" for c in ev["event"])


# ── 러너 쪽 배선 ─────────────────────────────────────────────────────

def test_publish_policy_writes_and_removes_reuse_marker(tmp_path):
    lane = tmp_path / "lane"
    base = f"RUNNER_HEAVY_DIR={lane}\nAADS_RESULT_DIR={tmp_path}/res\nexport AADS_RESULT_DIR\n"
    assert _bash(base + "runner_heavy_publish_policy 2 0 5\n").returncode == 0
    assert (lane / "reuse").exists() and (lane / "policy").read_text() == "slots=2\nurgent=0\nrevision=5\n"
    assert (tmp_path / "res").is_dir()
    assert _bash(base + "RUNNER_RESULT_REUSE=0\nrunner_heavy_publish_policy 2 0 6\n").returncode == 0
    assert not (lane / "reuse").exists() and (lane / "policy").exists()
    assert _bash(base + "runner_heavy_publish_policy 2 0 7\nrunner_heavy_publish_policy 0 0 8\n").returncode == 0
    assert not (lane / "reuse").exists() and not (lane / "policy").exists()


def test_job_env_exports_project_only_when_lane_enabled(tmp_path):
    lane = tmp_path / "lane"
    base = f"RUNNER_HEAVY_DIR={lane}\nAADS_RESULT_DIR={tmp_path}/res\nexport AADS_RESULT_DIR\n"
    r = _bash(base + 'runner_heavy_job_env "PRIORITY: P2" runner-x AADS\necho "P=${AADS_HEAVY_PROJECT-unset}"\n')
    assert "P=unset" in r.stdout
    r = _bash(base + 'runner_heavy_publish_policy 2 0 1\nrunner_heavy_job_env "PRIORITY: P2" runner-x AADS\necho "P=${AADS_HEAVY_PROJECT-unset}"\n')
    assert "P=AADS" in r.stdout
    r = _bash(base + 'runner_heavy_publish_policy 2 0 1\nrunner_heavy_job_env "x" runner-x "bad name;rm"\necho "P=${AADS_HEAVY_PROJECT-unset}"\n')
    assert "P=unset" in r.stdout
    assert 'runner_heavy_job_env "$instruction" "$job_id" "${project:-}"' in _function("run_job")
