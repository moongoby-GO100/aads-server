"""git_fetch_origin_with_retry / prepare_clean_job_worktree 의 fetch 재시도 검증.

가짜 git 을 PATH 앞에 두어 실제 네트워크 없이 시도 횟수·종류·시간 제한·원문 비유출을 확인한다.
"""
import os
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNNERS = ("pipeline-runner.sh", "pipeline-runner.sh.local")
SECRET = "ghp_TEST_TOKEN"


def extract(source, name):
    start = source.index(f"{name}() {{")
    end = source.index("\n}\n", start) + 3
    return source[start:end]


@pytest.fixture(params=RUNNERS)
def harness(request, tmp_path):
    source = (ROOT / "scripts" / request.param).read_text()
    script = tmp_path / "fn.sh"
    script.write_text(
        extract(source, "git_fetch_origin_with_retry") + "\n"
        + extract(source, "prepare_clean_job_worktree")
        + r'''
log() { printf '%s\n' "$*" >> "$QA_LOG"; }
_fail_job() { printf '%s|%s\n' "$3" "$4" >> "$QA_FAILURE"; }
is_git_workdir() { return 0; }
worktree_busy_pids() { printf ''; }
''')
    bindir = tmp_path / "bin"
    bindir.mkdir()
    counter = tmp_path / "count"

    def fake_git(body):
        g = bindir / "git"
        g.write_text("#!/bin/bash\n"
                     f'echo x >> "{counter}"\n'
                     'if [[ "$3" == fetch ]]; then\n' + body + "\nfi\nexit 0\n")
        g.chmod(0o755)

    def run(func, *args, extra_env=None, timeout=60):
        env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
               "QA_LOG": str(tmp_path / "log"), "QA_FAILURE": str(tmp_path / "failure"),
               "RUNNER_GIT_FETCH_RETRY_SLEEPS": "0 0", **(extra_env or {})}
        return subprocess.run(["bash", "-c", 'source "$1"; shift; "$@"', "qa", str(script), func, *args],
                              env=env, cwd=tmp_path, text=True, capture_output=True, timeout=timeout)

    def read(name):
        p = tmp_path / name
        return p.read_text() if p.exists() else ""

    def calls():
        return len(read("count").splitlines())

    return type("H", (), dict(fake_git=staticmethod(fake_git), run=staticmethod(run),
                              read=staticmethod(read), calls=staticmethod(calls),
                              source=source, tmp=tmp_path))


def prepare(h, **env):
    return h.run("prepare_clean_job_worktree", "runner-abc", "AADS", "s", str(h.tmp), str(h.tmp / "wt"),
                 extra_env=env)


def test_one_failure_then_success(harness):
    harness.fake_git(f'if [[ ! -e "{harness.tmp}/once" ]]; then touch "{harness.tmp}/once"; '
                     'echo "fatal: Could not resolve host: x" >&2; exit 128; fi; exit 0')
    r = harness.run("git_fetch_origin_with_retry", str(harness.tmp))
    assert r.returncode == 0, r.stderr
    assert harness.calls() == 2
    assert "GIT_FETCH_RETRY attempt=1/3 rc=128 kind=network" in harness.read("log")


def test_three_failures_report_count_and_kind(harness):
    harness.fake_git('echo "git@github.com: Permission denied (publickey)." >&2; exit 128')
    prepare(harness)
    assert harness.calls() == 3
    failure = harness.read("failure")
    assert failure.startswith("git_fetch_failed|")
    assert "AADS (3회 시도, 마지막 종류: publickey)" in failure


def test_sigterm_ignoring_git_is_killed_within_limit(harness):
    harness.fake_git("trap '' TERM; while :; do sleep 1; done")
    start = time.monotonic()
    r = harness.run("git_fetch_origin_with_retry", str(harness.tmp),
                    extra_env={"RUNNER_GIT_FETCH_TIMEOUT_SEC": "1"})
    elapsed = time.monotonic() - start
    assert r.returncode == 1
    assert elapsed < 3 * (1 + 5) + 10
    assert "kind=timeout" in harness.read("log")


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "", "301"])
def test_invalid_timeout_falls_back_to_45(harness, bad):
    (harness.tmp / "tlog").write_text("")
    # timeout 래퍼를 가짜로 두어 받은 제한값을 기록한다.
    t = harness.tmp / "bin" / "timeout"
    t.write_text(f'#!/bin/bash\necho "$2" >> "{harness.tmp}/tlog"\nshift 2\nexec "$@"\n')
    t.chmod(0o755)
    harness.fake_git("exit 0")
    r = harness.run("git_fetch_origin_with_retry", str(harness.tmp),
                    extra_env={"RUNNER_GIT_FETCH_TIMEOUT_SEC": bad})
    assert r.returncode == 0, r.stderr
    assert (harness.tmp / "tlog").read_text().split() == ["45"]


def test_secret_in_stderr_never_leaks(harness):
    harness.fake_git(f'echo "fatal: could not read Password for http://{SECRET}@127.0.0.1:9" >&2; exit 128')
    r = prepare(harness)
    for text in (harness.read("failure"), harness.read("log"), r.stdout, r.stderr):
        assert "ghp_" not in text
    assert "마지막 종류: other" in harness.read("failure")


def test_two_runner_copies_identical():
    a, b = [(ROOT / "scripts" / n).read_text() for n in RUNNERS]
    assert extract(a, "git_fetch_origin_with_retry") == extract(b, "git_fetch_origin_with_retry")
    assert "git_fetch_origin_with_retry" in extract(a, "prepare_clean_job_worktree")
    assert "git_fetch_origin_with_retry" in extract(b, "prepare_clean_job_worktree")
