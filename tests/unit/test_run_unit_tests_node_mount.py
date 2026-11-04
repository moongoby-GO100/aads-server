"""run_unit_tests.sh 의 호스트 node 읽기 전용 마운트 검증.

가짜 docker 로 스크립트 전체를 돌려 `docker run` 인자를 기록한다 — 실제 docker·이미지에 의존하지 않는다.
"""
import os
import shutil
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "run_unit_tests.sh"
NODE_TARGET = "/usr/local/bin/node"

FAKE_DOCKER = """#!/bin/bash
case "$1" in
    inspect) echo "fake-image:test"; exit 0 ;;
    ps|images) exit 0 ;;
    rm) exit 0 ;;
    run)
        shift
        echo "$*" >> "$FAKE_DOCKER_LOG"
        case " $* " in
            *" --version "*)
                if [ "${FAKE_NODE_TRIAL:-ok}" = "ok" ]; then
                    echo "v20.20.2"; exit 0
                fi
                echo "node: error while loading shared libraries: libstdc++.so.6" >&2
                exit 127 ;;
        esac
        case " $* " in
            *" pytest "*) exit "${FAKE_PYTEST_RC:-0}" ;;
        esac
        exit 0 ;;
esac
exit 0
"""

TOOLS = [
    "bash", "dirname", "tr", "seq", "stat", "id", "flock", "mkdir", "mktemp", "sleep",
    "head", "cat", "rm", "readlink", "kill", "sed", "env", "wc", "printf",
]


def _make_env(tmp: Path, with_path_node: bool):
    bindir = tmp / "bin"
    bindir.mkdir()
    for name in TOOLS:
        found = shutil.which(name)
        if found:
            (bindir / name).symlink_to(found)
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER, encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)

    real_node = tmp / "real-node"
    real_node.write_text("#!/bin/bash\necho v20.20.2\n", encoding="utf-8")
    real_node.chmod(0o755)
    if with_path_node:
        (bindir / "node").symlink_to(real_node)

    log = tmp / "docker.log"
    env = {
        "PATH": str(bindir),
        "HOME": str(tmp),
        "FAKE_DOCKER_LOG": str(log),
        "AADS_TESTDEPS_DIR": str(tmp / "testdeps"),
        "AADS_ACTIVE_CONTAINER_FILE": str(tmp / "no-active"),
        "AADS_UT_LOCK_DIR": str(tmp / "locks"),
        "AADS_DASHBOARD_ROOT": str(tmp),
    }
    return env, log, real_node


def _run(tmp_path, with_path_node=True, extra_env=None):
    env, log, real_node = _make_env(tmp_path, with_path_node)
    env.update(extra_env or {})
    proc = subprocess.run(
        ["/bin/bash", str(SCRIPT), "tests/unit/x.py"],
        env=env, capture_output=True, text=True, timeout=120, cwd=ROOT,
    )
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    trial = [ln for ln in lines if "--version" in ln]
    final = [ln for ln in lines if " pytest " in f" {ln} " and "--version" not in ln and "import pytest" not in ln]
    return proc, trial, final, real_node


def test_node_on_host_and_trial_ok_is_mounted_read_only(tmp_path):
    proc, trial, final, real_node = _run(tmp_path)
    mount = f"-v {real_node.resolve()}:{NODE_TARGET}:ro"

    assert proc.returncode == 0, proc.stderr
    assert len(trial) == 1 and mount in trial[0]
    assert len(final) == 1 and mount in final[0]
    assert "[run_unit_tests] node: mounted v20.20.2" in proc.stdout
    assert "node 마운트 불가" not in proc.stderr


def test_symlinked_node_is_mounted_by_resolved_path(tmp_path):
    proc, _trial, final, real_node = _run(tmp_path)

    assert f"{real_node.resolve()}:{NODE_TARGET}:ro" in final[0]
    assert str(tmp_path / "bin" / "node") not in final[0]


def test_trial_failure_skips_mount_and_warns_with_unchanged_exit_code(tmp_path):
    proc, trial, final, _ = _run(tmp_path, extra_env={"FAKE_NODE_TRIAL": "fail"})

    assert proc.returncode == 0, proc.stderr
    assert len(trial) == 1
    assert len(final) == 1 and NODE_TARGET not in final[0]
    assert "node 마운트 불가 — node 테스트는 skip 됩니다(" in proc.stderr
    assert "libstdc++" in proc.stderr
    assert "[run_unit_tests] node: not mounted(" in proc.stdout


def test_no_host_node_does_not_mount_and_warns(tmp_path):
    proc, trial, final, _ = _run(tmp_path, with_path_node=False)

    assert proc.returncode == 0, proc.stderr
    assert trial == []
    assert len(final) == 1 and NODE_TARGET not in final[0]
    assert "node 마운트 불가 — node 테스트는 skip 됩니다(호스트에 node 없음)" in proc.stderr
    assert "node: not mounted(호스트에 node 없음)" in proc.stdout


def test_no_node_env_disables_mount_even_if_host_has_node(tmp_path):
    proc, trial, final, _ = _run(tmp_path, extra_env={"AADS_TEST_NO_NODE": "1"})

    assert proc.returncode == 0, proc.stderr
    assert trial == []
    assert len(final) == 1 and NODE_TARGET not in final[0]
    assert "not mounted(AADS_TEST_NO_NODE=1)" in proc.stdout


def test_node_bin_env_takes_precedence_over_host_path(tmp_path):
    other = tmp_path / "other-node"
    other.write_text("#!/bin/bash\necho v22.23.4\n", encoding="utf-8")
    other.chmod(0o755)

    proc, trial, final, real_node = _run(tmp_path, extra_env={"AADS_TEST_NODE_BIN": str(other)})

    assert proc.returncode == 0, proc.stderr
    assert f"{other.resolve()}:{NODE_TARGET}:ro" in trial[0]
    assert f"{other.resolve()}:{NODE_TARGET}:ro" in final[0]
    assert str(real_node.resolve()) not in final[0]


def test_node_bin_env_pointing_nowhere_warns_and_does_not_fall_back(tmp_path):
    proc, trial, final, _ = _run(
        tmp_path, extra_env={"AADS_TEST_NODE_BIN": str(tmp_path / "missing-node")}
    )

    assert proc.returncode == 0, proc.stderr
    assert trial == []
    assert NODE_TARGET not in final[0]
    assert "node 마운트 불가" in proc.stderr


def test_pytest_failure_exit_code_is_preserved_with_node_mounted(tmp_path):
    proc, _trial, final, _ = _run(tmp_path, extra_env={"FAKE_PYTEST_RC": "1"})

    assert proc.returncode == 1
    assert NODE_TARGET in final[0]
    assert "node: mounted v20.20.2" in proc.stdout
