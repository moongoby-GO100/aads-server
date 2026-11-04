"""scripts/run_unit_tests.sh 동시 실행 상한(flock 슬롯) 회귀 테스트.

UT_SLOT_BEGIN~UT_SLOT_END 블록만 떼어 bash 로 실행한다. 도커 컨테이너는 띄우지 않는다.
"""
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (ROOT / "scripts" / "run_unit_tests.sh").read_text(encoding="utf-8")


def _block() -> str:
    a = SCRIPT.index("# ── UT_SLOT_BEGIN ──")
    b = SCRIPT.index("# ── UT_SLOT_END ──")
    return SCRIPT[a:b]


def _run(body: str, env: dict | None = None, timeout: int = 30):
    e = {k: v for k, v in os.environ.items() if not k.startswith("AADS_UT_")}
    e.update(env or {})
    return subprocess.run(
        ["bash", "-c", _block() + "\n" + body],
        env=e, capture_output=True, text=True, timeout=timeout,
    )


def _dir(tmp: str) -> str:
    return os.path.join(tmp, "slots")


def test_script_wires_slot_before_docker_work():
    assert SCRIPT.index("ut_acquire_slot || exit 2") < SCRIPT.index("mapfile -t RUNTIME_CANDIDATES")
    assert "aads-ut-$$-${RANDOM}${RANDOM}" in SCRIPT
    assert "AADS_UT_MAX_PARALLEL" in SCRIPT and "flock" in SCRIPT
    assert "/tmp/aads-ut-slots" in SCRIPT


def test_positive_int_octal_and_invalid():
    cases = {"": "7", "00": "7", "0": "7", "08": "8", "09": "9", "010": "10",
             "abc": "7", "-1": "7", "1000000": "7", "3": "3"}
    for raw, want in cases.items():
        r = _run(f'ut_positive_int "{raw}" 7')
        assert r.returncode == 0, (raw, r.stderr)
        assert r.stdout == want, (raw, r.stdout, r.stderr)


def test_octal_env_values_do_not_error():
    with tempfile.TemporaryDirectory() as t:
        for mp, wait in (("00", "08"), ("08", "09")):
            r = _run('ut_acquire_slot; echo rc=$?',
                     {"AADS_UT_LOCK_DIR": _dir(t), "AADS_UT_MAX_PARALLEL": mp, "AADS_UT_SLOT_WAIT": wait})
            assert "rc=0" in r.stdout, (mp, wait, r.stdout, r.stderr)
            assert "value too great" not in r.stderr and "산술" not in r.stderr


def test_cap_blocks_third_and_times_out_with_exit_2():
    with tempfile.TemporaryDirectory() as t:
        env = {"AADS_UT_LOCK_DIR": _dir(t), "AADS_UT_MAX_PARALLEL": "2", "AADS_UT_SLOT_WAIT": "2"}
        holder = (
            'ut_acquire_slot || exit 9; echo held; sleep 15'
        )
        procs = []
        for _ in range(2):
            p = subprocess.Popen(["bash", "-c", _block() + "\n" + holder],
                                 env={**os.environ, **env}, stdout=subprocess.PIPE, text=True)
            assert p.stdout.readline().strip() == "held"
            procs.append(p)
        try:
            t0 = time.time()
            r = _run('ut_acquire_slot; exit $?', env)
            assert r.returncode == 2, (r.stdout, r.stderr)
            assert time.time() - t0 < 10
        finally:
            for p in procs:
                p.kill()
                p.wait()
        # 홀더가 죽으면 슬롯은 자동 해제된다.
        r = _run('ut_acquire_slot; exit $?', env)
        assert r.returncode == 0, r.stderr


def test_symlinked_lock_dir_and_slot_file_are_refused_and_not_truncated():
    with tempfile.TemporaryDirectory() as t:
        victim = Path(t) / "victim.txt"
        victim.write_text("keep me")
        # 슬롯 파일이 심볼릭 링크
        d = Path(_dir(t))
        d.mkdir(mode=0o700)
        os.symlink(victim, d / "slot-0.lock")
        r = _run('ut_acquire_slot; exit $?', {"AADS_UT_LOCK_DIR": str(d), "AADS_UT_SLOT_WAIT": "1"})
        assert r.returncode == 2
        assert victim.read_text() == "keep me"
        # 디렉터리가 심볼릭 링크
        real = Path(t) / "real"
        real.mkdir(mode=0o700)
        link = Path(t) / "link"
        os.symlink(real, link)
        r = _run('ut_acquire_slot; exit $?', {"AADS_UT_LOCK_DIR": str(link), "AADS_UT_SLOT_WAIT": "1"})
        assert r.returncode == 2
        assert not list(real.iterdir())


def test_world_writable_lock_dir_refused():
    with tempfile.TemporaryDirectory() as t:
        d = Path(_dir(t))
        d.mkdir()
        d.chmod(0o777)
        r = _run('ut_acquire_slot; exit $?', {"AADS_UT_LOCK_DIR": str(d), "AADS_UT_SLOT_WAIT": "1"})
        assert r.returncode == 2


def test_default_slot_wait_is_below_commit_gate_timeout():
    m = re.search(r"^GROUP_TIMEOUT_SEC\s*=\s*(\d+)", (ROOT / "scripts" / "pre_commit_test_map.py").read_text(encoding="utf-8"), re.M)
    assert m, "GROUP_TIMEOUT_SEC 를 찾지 못했습니다"
    gate = int(m.group(1))
    d = re.search(r'ut_positive_int "\$\{AADS_UT_SLOT_WAIT:-\}" (\d+)', SCRIPT)
    assert d, "AADS_UT_SLOT_WAIT 기본값을 찾지 못했습니다"
    assert int(d.group(1)) == 300
    assert int(d.group(1)) < gate
    assert "기본 900" not in SCRIPT


def test_waiting_prints_progress_line_to_stderr():
    with tempfile.TemporaryDirectory() as t:
        env = {"AADS_UT_LOCK_DIR": _dir(t), "AADS_UT_MAX_PARALLEL": "1",
               "AADS_UT_SLOT_WAIT": "4", "AADS_UT_WAIT_NOTICE": "1"}
        p = subprocess.Popen(["bash", "-c", _block() + "\nut_acquire_slot || exit 9; echo held; sleep 15"],
                             env={**os.environ, **env}, stdout=subprocess.PIPE, text=True)
        assert p.stdout.readline().strip() == "held"
        try:
            r = _run('ut_acquire_slot; exit $?', env)
        finally:
            p.kill()
            p.wait()
        assert r.returncode == 2
        assert re.search(r"슬롯 대기 중 \(경과 [1-9]\d*초, 상한 1개\)", r.stderr), r.stderr
        assert "슬롯 대기 중" not in r.stdout


def test_no_progress_line_when_slot_is_free():
    with tempfile.TemporaryDirectory() as t:
        r = _run('ut_acquire_slot; exit $?', {"AADS_UT_LOCK_DIR": _dir(t)})
        assert r.returncode == 0
        assert "슬롯 대기 중" not in r.stderr
