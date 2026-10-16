"""AADS-RUNNER-TEST-CONTAINER-REAP-20261016 (R-BG, R-ERRBOOK 3항)

러너가 job 을 종결하며 프로세스 트리를 죽이면 `docker run --rm` 클라이언트만 죽고 pytest 컨테이너는 남았다
(dockerd 소유라 클라이언트 SIGKILL 과 무관하게 계속 돈다). 규칙이 아니라 코드로 막는다:
  - run_unit_tests.sh: 이름·라벨·--init·trap·컨테이너 내부 timeout
  - pipeline-runner.sh: 종결 경로마다 라벨/worktree mount 로만 회수

실제 docker 를 쓰지 않는다. PATH 앞에 가짜 docker 실행파일을 둔다.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUN_UT = ROOT / "scripts" / "run_unit_tests.sh"
RUNNER = ROOT / "scripts" / "pipeline-runner.sh"

OPS_NAMES = ("aads-server", "aads-dashboard", "aads-postgres", "postgres", "litellm", "aads-nginx", "aads-redis")


def _code_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def _extract_function(script: str, name: str) -> str:
    m = re.search(rf"^{re.escape(name)}\(\) \{{\n", script, re.M)
    assert m, f"{name} 함수가 없다"
    end = script.index("\n}\n", m.start())
    return script[m.start() : end + 3]


# ── (a) run_unit_tests.sh 정적 검사 ──────────────────────────────────

def test_every_docker_run_has_label_name_and_in_container_timeout():
    code = _code_lines(RUN_UT.read_text(encoding="utf-8"))
    run_lines = [ln for ln in code if re.search(r"\bdocker run\b", ln)]
    assert run_lines, "docker run 줄이 사라졌다 — 헬퍼 구조가 바뀌었다면 이 테스트도 갱신"
    for ln in run_lines:
        assert "--label aads.unit_test=1" in ln, ln
        assert "--name " in ln, ln
        assert "--rm" in ln, ln
        assert "--init" in ln, ln
    assert any("aads.worktree=" in ln for ln in run_lines)
    assert any("aads.runner_job=" in ln for ln in code), "러너 job id 라벨이 없다"

    script = "\n".join(code)
    # 헬퍼 밖에서 docker run 을 직접 부르는 곳이 없다 (라벨 없는 컨테이너가 다시 생기는 경로 차단)
    assert len(run_lines) == 1
    calls = [
        ln for ln in code
        if ln.lstrip().startswith(("ut_docker_run", "if ! ut_docker_run")) and not ln.rstrip().endswith("() {")
    ]
    assert len(calls) == 3, "pytest 확인·설치·본 실행 3곳이 모두 헬퍼를 써야 한다"

    # 컨테이너 안에서 시간 상한 (R-BG 1항)
    assert 'UT_TIMEOUT="${UNIT_TEST_TIMEOUT:-1200}"' in script
    assert 'timeout -k 10 "$UT_TIMEOUT" python3 -m pytest' in script
    assert "timeout 60 python3 -c" in script
    assert "timeout 600 pip install" in script


def test_run_unit_tests_traps_cleanup_on_all_signals_and_keeps_exit_contract():
    script = RUN_UT.read_text(encoding="utf-8")
    for sig in ("EXIT", "TERM", "INT", "HUP"):
        assert re.search(rf"^trap '[^']*ut_cleanup[^']*' {sig}$", script, re.M), sig
    cleanup = _extract_function(script, "ut_cleanup")
    assert "docker rm -f" in cleanup
    # 클라이언트를 포그라운드로 두면 신호가 와도 bash 가 자식 종료까지 trap 을 미룬다 — 백그라운드 + wait 여야 한다
    helper = _extract_function(script, "ut_docker_run")
    assert '"$@" &' in helper and 'wait "$UT_CLIENT_PID"' in helper
    # 종료코드 계약 0/1/2 와 pytest 5→2 변환 유지
    assert re.search(r"\[ \$rc -eq 5 \]", script)
    assert script.rstrip().endswith("exit $rc")
    assert script.count("exit 2") >= 4


# ── (b) pipeline-runner.sh 정적 검사 ─────────────────────────────────

def test_runner_reaps_test_containers_at_every_worktree_reclaim_and_terminal_path():
    text = RUNNER.read_text(encoding="utf-8")
    lines = text.splitlines()

    # worktree 를 지우기 직전의 임시 PG 회수(_reap_job_temp_pg "$job_id") 지점은 전부 짝으로 호출한다.
    pg_sites = [i for i, ln in enumerate(lines) if ln.strip() == '_reap_job_temp_pg "$job_id" || true']
    assert len(pg_sites) >= 8
    for i in pg_sites:
        assert lines[i + 1].strip() == '_reap_job_test_containers "$job_id" || true', f"line {i + 1}"

    # 그 밖의 종결 경로: 빠른 실패, 러너 종료 마감, 종료 job 즉시 회수(취소·종료 상태 전이 포함), 주기 스윕
    for fn, needle in (
        ("_fail_job", '_reap_job_test_containers "$job_id"'),
        ("_shutdown_finalize_job", '_reap_job_test_containers "$_jid"'),
        ("_reclaim_finished_worktree", '_reap_job_test_containers "$job_id"'),
        ("_cleanup_old_artifacts", "_reap_orphan_test_containers"),
    ):
        assert needle in _extract_function(text, fn), fn

    # _reclaim_finished_worktree 는 _abort_job_terminal_before_work 와 _reap_bg_jobs 가 부른다
    assert "_reclaim_finished_worktree" in _extract_function(text, "_abort_job_terminal_before_work")
    assert "_reclaim_finished_worktree" in _extract_function(text, "_reap_bg_jobs")


def test_runner_reap_filters_are_limited_to_labels_and_worktree_path():
    text = RUNNER.read_text(encoding="utf-8")
    for fn in ("_reap_job_test_containers", "_reap_orphan_test_containers"):
        body = "\n".join(_code_lines(_extract_function(text, fn)))
        for name in OPS_NAMES:
            assert name not in body, f"{fn} 가 운영 컨테이너 이름({name})을 언급한다"
        assert "--filter name=" not in body and "--filter \"name=" not in body
    body = "\n".join(_code_lines(_extract_function(text, "_reap_job_test_containers")))
    assert 'label=aads.runner_job=${job_id}' in body
    assert 'label=aads.worktree=${wt}' in body
    assert 'wt="/tmp/aads-wt-${job_id}"' in body
    assert "runner-[0-9a-zA-Z_-]+" in body  # job id 형식 검증
    # best effort: 어떤 경우에도 0 을 돌려준다
    assert body.rstrip().endswith("return 0\n}")
    assert "|| true" in body


# ── 가짜 docker ───────────────────────────────────────────────────────

FAKE_DOCKER = r'''#!__PY__
import json, os, sys, time

state_file = os.environ["FAKE_DOCKER_STATE"]
log_file = os.environ["FAKE_DOCKER_LOG"]
args = sys.argv[1:]
with open(log_file, "a") as f:
    f.write(json.dumps(args) + "\n")
if os.environ.get("FAKE_DOCKER_FAIL") == "1":
    sys.exit(1)
try:
    state = json.load(open(state_file))
except Exception:
    state = []

def save():
    json.dump(state, open(state_file, "w"))

def parse_filters(a):
    out = []
    for i, x in enumerate(a):
        if x == "--filter":
            out.append(a[i + 1])
    return out

def match(c, flt):
    if flt.startswith("label="):
        kv = flt[len("label="):]
        k, _, v = kv.partition("=")
        if k not in c.get("labels", {}):
            return False
        return (not _) or c["labels"][k] == v
    return True

cmd = args[0] if args else ""
if cmd == "ps":
    filters = parse_filters(args)
    sel = [c for c in state if all(match(c, f) for f in filters)]
    if "--format" in args:
        fmt = args[args.index("--format") + 1]
        for c in sel:
            if "{{.Label" in fmt:
                print(c["id"] + "\t" + c.get("labels", {}).get("aads.worktree", ""))
            else:
                print(c["id"])
    else:
        for c in sel:
            print(c["id"])
elif cmd == "inspect":
    fmt = args[args.index("-f") + 1] if "-f" in args else ""
    if "Config.Image" in fmt:
        print("fakeimage:1")
    else:
        ids = [a for a in args[1:] if a not in ("-f", fmt)]
        for i in ids:
            for c in state:
                if c["id"] == i:
                    print(i + "".join(" " + m for m in c.get("mounts", [])))
elif cmd == "rm":
    ids = [a for a in args[1:] if not a.startswith("-")]
    for i in ids:
        state[:] = [c for c in state if c["id"] != i and c.get("name") != i]
    save()
elif cmd == "run":
    joined = " ".join(args)
    if "import pytest" in joined:
        sys.exit(0)
    if "-m pytest" in joined:
        time.sleep(float(os.environ.get("FAKE_RUN_SLEEP", "0")))
        sys.exit(int(os.environ.get("FAKE_RUN_RC", "0")))
    sys.exit(0)
elif cmd == "images":
    pass
sys.exit(0)
'''


@pytest.fixture()
def fake(tmp_path: Path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER.replace("__PY__", sys.executable))
    docker.chmod(0o755)
    state = tmp_path / "state.json"
    log = tmp_path / "docker.log"
    state.write_text("[]")
    log.write_text("")

    class F:
        pass

    f = F()
    f.tmp = tmp_path
    f.bindir = bindir
    f.state = state
    f.log = log
    f.env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "FAKE_DOCKER_STATE": str(state),
        "FAKE_DOCKER_LOG": str(log),
    }
    f.env.pop("AADS_HEAVY_JOB_ID", None)
    f.set_containers = lambda cs: state.write_text(json.dumps(cs))
    f.containers = lambda: json.loads(state.read_text())
    f.calls = lambda: [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]
    return f


def _cid(n: int) -> str:
    return f"{n:02d}" + "ab" * 31


def _extract_reap_funcs() -> str:
    text = RUNNER.read_text(encoding="utf-8")
    return _extract_function(text, "_reap_job_test_containers") + "\n" + _extract_function(
        text, "_reap_orphan_test_containers"
    )


def _run_reap(fake, snippet: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    script = f"log() {{ echo \"LOG $*\"; }}\n{_extract_reap_funcs()}\n{snippet}\n"
    env = {**fake.env, **(env_extra or {})}
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)


# ── 회수 함수 셸 동작 ─────────────────────────────────────────────────

def test_reap_job_test_containers_removes_only_labeled_or_worktree_mounted(fake):
    job = f"runner-{uuid.uuid4().hex[:8]}"
    other = "runner-deadbeef"
    wt = f"/tmp/aads-wt-{job}"
    fake.set_containers([
        {"id": _cid(1), "name": "ut_a", "labels": {"aads.unit_test": "1", "aads.runner_job": job, "aads.worktree": wt}},
        {"id": _cid(2), "name": "ut_b", "labels": {"aads.unit_test": "1", "aads.worktree": wt}},
        {"id": _cid(3), "name": "legacy_c", "labels": {}, "mounts": ["/root/.ssh", wt, "/root/aads/.testdeps"]},
        {"id": _cid(4), "name": "legacy_d", "labels": {}, "mounts": [f"{wt}/tests"]},
        {"id": _cid(5), "name": "other_job", "labels": {"aads.unit_test": "1", "aads.runner_job": other,
                                                         "aads.worktree": f"/tmp/aads-wt-{other}"}},
        {"id": _cid(6), "name": "aads-server", "labels": {}, "mounts": ["/root/aads/aads-server", "/root/.ssh"]},
        {"id": _cid(7), "name": "aads-postgres", "labels": {"com.docker.compose.service": "postgres"},
         "mounts": ["/var/lib/docker/volumes/pgdata/_data"]},
        {"id": _cid(8), "name": "prefix_clash", "labels": {}, "mounts": [f"{wt}-other"]},
        {"id": _cid(9), "name": "other_wt_mount", "labels": {}, "mounts": [f"/tmp/aads-wt-{other}"]},
    ])
    r = _run_reap(fake, f'_reap_job_test_containers {job}; echo "RC=$?"')
    assert r.returncode == 0, r.stderr
    assert "RC=0" in r.stdout
    left = {c["name"] for c in fake.containers()}
    assert left == {"other_job", "aads-server", "aads-postgres", "prefix_clash", "other_wt_mount"}
    assert f"TEST_CONTAINER_REAP job={job} removed=4 failed=0" in r.stdout
    # 삭제 호출 대상은 full id 뿐이고 운영 컨테이너 id 는 한 번도 지정되지 않았다
    rm_ids = [c for call in fake.calls() if call[0] == "rm" for c in call[1:] if not c.startswith("-")]
    assert sorted(rm_ids) == sorted(_cid(i) for i in (1, 2, 3, 4))
    for call in fake.calls():
        if call[0] == "rm":
            assert call[:2] == ["rm", "-f"]


def test_reap_job_test_containers_is_best_effort_and_validates_job_id(fake):
    job = f"runner-{uuid.uuid4().hex[:8]}"
    fake.set_containers([{"id": _cid(1), "name": "ut", "labels": {"aads.runner_job": job}}])

    # docker 가 실패해도 0 을 돌려준다 (job 종결을 막지 않는다)
    r = _run_reap(fake, f'_reap_job_test_containers {job}; echo "RC=$?"', {"FAKE_DOCKER_FAIL": "1"})
    assert "RC=0" in r.stdout and r.returncode == 0
    assert len(fake.containers()) == 1

    # job id 형식이 이상하면 docker 를 부르지 않는다 (경로 주입 방지)
    fake.log.write_text("")
    for bad in ("", "nojob", "runner-x;rm -rf /", "../runner-1", "runner-a b"):
        r = _run_reap(fake, f'_reap_job_test_containers "{bad}"; echo "RC=$?"')
        assert "RC=0" in r.stdout, bad
    assert fake.calls() == []

    # 끄는 스위치
    r = _run_reap(fake, f'_reap_job_test_containers {job}; echo "RC=$?"', {"RUNNER_TEST_CONTAINER_REAP": "0"})
    assert "RC=0" in r.stdout
    assert fake.calls() == [] and len(fake.containers()) == 1

    # docker 실행파일이 PATH 에 없어도 0
    empty = fake.tmp / "emptybin"
    empty.mkdir()
    r = subprocess.run(
        ["/bin/bash", "-c", f"log() {{ :; }}\n{_extract_reap_funcs()}\n_reap_job_test_containers {job}; echo RC=$?"],
        env={"PATH": str(empty)},
        capture_output=True, text=True, timeout=30,
    )
    assert "RC=0" in r.stdout, r.stderr


def test_reap_orphan_test_containers_only_when_worktree_is_gone(fake):
    gone = f"/tmp/aads-wt-runner-gone{uuid.uuid4().hex[:6]}"
    alive = Path(f"/tmp/aads-wt-runner-alive{uuid.uuid4().hex[:6]}")
    alive.mkdir()
    try:
        fake.set_containers([
            {"id": _cid(1), "name": "orphan", "labels": {"aads.unit_test": "1", "aads.worktree": gone}},
            {"id": _cid(2), "name": "live_wt", "labels": {"aads.unit_test": "1", "aads.worktree": str(alive)}},
            {"id": _cid(3), "name": "main_checkout", "labels": {"aads.unit_test": "1",
                                                                 "aads.worktree": "/root/aads/aads-server"}},
            {"id": _cid(4), "name": "unlabeled", "labels": {}, "mounts": [gone]},
            {"id": _cid(5), "name": "aads-server", "labels": {}},
        ])
        r = _run_reap(fake, '_reap_orphan_test_containers; echo "RC=$?"')
        assert "RC=0" in r.stdout, r.stderr
        left = {c["name"] for c in fake.containers()}
        assert left == {"live_wt", "main_checkout", "unlabeled", "aads-server"}
        assert "TEST_CONTAINER_SWEEP removed=1" in r.stdout
    finally:
        alive.rmdir()


# ── run_unit_tests.sh 셸 동작 (가짜 docker) ───────────────────────────

def _ut_env(fake, **extra):
    d = fake.tmp
    env = {
        **fake.env,
        "AADS_TEST_IMAGE_SOURCE": "fakesrc",
        "AADS_ACTIVE_CONTAINER_FILE": str(d / "nope"),
        "AADS_TESTDEPS_DIR": str(d / "testdeps"),
        "AADS_DASHBOARD_ROOT": str(d),
    }
    env.update(extra)
    return env


def _main_run_call(fake):
    runs = [c for c in fake.calls() if c[0] == "run" and "-m" in c and "pytest" in c]
    return runs[0] if runs else None


def test_run_unit_tests_runs_with_labels_name_and_timeout_and_keeps_exit_codes(fake):
    job = f"runner-{uuid.uuid4().hex[:8]}"
    for rc_in, rc_out in ((0, 0), (1, 1), (5, 2), (124, 1), (137, 1)):
        fake.log.write_text("")
        r = subprocess.run(
            ["bash", str(RUN_UT), "tests/unit/x"],
            env=_ut_env(fake, FAKE_RUN_RC=str(rc_in), AADS_HEAVY_JOB_ID=job),
            capture_output=True, text=True, timeout=60,
        )
        assert r.returncode == rc_out, (rc_in, r.returncode, r.stderr)
        call = _main_run_call(fake)
        assert call is not None
        name = call[call.index("--name") + 1]
        assert re.fullmatch(r"aads-ut-\d+-\d+", name), name
        assert "--rm" in call and "--init" in call
        labels = [call[i + 1] for i, a in enumerate(call) if a == "--label"]
        assert "aads.unit_test=1" in labels
        assert f"aads.worktree={ROOT}" in labels
        assert f"aads.runner_job={job}" in labels
        i = call.index("timeout")
        assert call[i : i + 4] == ["timeout", "-k", "10", "1200"]
        # 정상 종료(rc<128)에서는 남은 컨테이너를 지우려 하지 않는다(--rm 이 처리).
        # 클라이언트가 신호로 끝난 rc>=128 이면 컨테이너가 살아 있을 수 있어 이름으로 지운다.
        removed = [c for c in fake.calls() if c[0] == "rm"]
        assert removed == ([["rm", "-f", name]] if rc_in >= 128 else []), (rc_in, removed)


def test_run_unit_tests_unit_test_timeout_env_and_label_without_job(fake):
    r = subprocess.run(
        ["bash", str(RUN_UT), "tests/unit/x"],
        env=_ut_env(fake, UNIT_TEST_TIMEOUT="77"),
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    call = _main_run_call(fake)
    i = call.index("timeout")
    assert call[i : i + 4] == ["timeout", "-k", "10", "77"]
    labels = [call[i + 1] for i, a in enumerate(call) if a == "--label"]
    if re.fullmatch(r"/tmp/aads-wt-runner-[0-9A-Za-z_-]+", str(ROOT)):
        assert any(x.startswith("aads.runner_job=runner-") for x in labels)
    else:
        assert not any(x.startswith("aads.runner_job=") for x in labels)


@pytest.mark.parametrize("sig,code", [(signal.SIGTERM, 143), (signal.SIGINT, 130), (signal.SIGHUP, 129)])
def test_run_unit_tests_signal_removes_its_container(fake, sig, code):
    p = subprocess.Popen(
        ["bash", str(RUN_UT), "tests/unit/x"],
        env=_ut_env(fake, FAKE_RUN_SLEEP="30"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
    )
    try:
        deadline = time.time() + 30
        while time.time() < deadline and _main_run_call(fake) is None:
            time.sleep(0.1)
        call = _main_run_call(fake)
        assert call is not None, "본 실행 docker run 이 시작되지 않았다"
        name = call[call.index("--name") + 1]
        time.sleep(0.3)
        os.kill(p.pid, sig)
        out, err = p.communicate(timeout=20)
    finally:
        if p.poll() is None:
            os.killpg(p.pid, signal.SIGKILL)
    assert p.returncode == code, (p.returncode, err)
    removed = [c for c in fake.calls() if c[0] == "rm"]
    assert ["rm", "-f", name] in removed, removed
