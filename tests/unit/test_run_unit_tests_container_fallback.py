import os
import stat
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read_script() -> str:
    return (ROOT / "scripts" / "run_unit_tests.sh").read_text(encoding="utf-8")


def _extract_function(script: str, name: str) -> str:
    start = script.index(f"{name}() {{")
    return script[start : script.index("\n}\n", start) + len("\n}\n")]


def test_run_unit_tests_keeps_container_fallback_chain_and_retry_guards():
    """조각 문자열 검사 — 폴백 사슬과 재시도 루프가 스크립트에 그대로 있는지."""
    script = _read_script()

    assert "resolve_test_image_candidates()" in script
    assert 'ACTIVE_CONTAINER_FILE="${AADS_ACTIVE_CONTAINER_FILE:-/root/aads/aads-server/.active_container}"' in script
    assert 'candidates+=("$AADS_TEST_IMAGE_SOURCE")' in script
    assert 'candidates+=("aads-server")' in script
    assert 'docker ps --filter "name=aads-server-" --filter "health=healthy" --format' in script

    # exit 2 의미는 완화되지 않고, 실패 시 시도한 후보를 stderr 에 남긴다.
    assert "시도한 후보: ${RUNTIME_CANDIDATES[*]}" in script
    assert script.count("exit 2") >= 4

    # 이미지 조회는 5초 간격으로 3회까지 재시도한 뒤에 포기한다.
    retry_block = script[script.index("IMAGE=\"\"") : script.index("if [ -z \"$IMAGE\" ]")]
    assert "for attempt in 1 2 3; do" in retry_block
    assert 'sleep 5' in retry_block


def _run_resolve_candidates(env_overrides, active_container_content=None, docker_ps_names=()):
    script = _read_script()
    fn_body = _extract_function(script, "resolve_test_image_candidates")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        active_file = tmp_path / "active_container"
        if active_container_content is not None:
            active_file.write_text(active_container_content, encoding="utf-8")

        # docker ps 만 흉내내는 가짜 docker — 실제 호스트 상태에 의존하지 않는다.
        # 이름을 별도 파일에 적고 cat 으로 읽어 셸 이스케이핑 문제를 피한다.
        fake_docker = tmp_path / "docker"
        names_file = tmp_path / "ps_names.txt"
        names_content = "\n".join(docker_ps_names)
        if names_content:
            names_content += "\n"
        names_file.write_text(names_content, encoding="utf-8")
        fake_docker.write_text(
            "#!/bin/bash\n"
            "if [ \"$1\" = 'ps' ]; then\n"
            f"  cat {names_file}\n"
            "fi\n",
            encoding="utf-8",
        )
        fake_docker.chmod(fake_docker.stat().st_mode | stat.S_IEXEC)

        runner = tmp_path / "runner.sh"
        runner.write_text(
            "set -uo pipefail\n"
            f"ACTIVE_CONTAINER_FILE={str(active_file)!r}\n"
            + fn_body
            + "\nresolve_test_image_candidates\n",
            encoding="utf-8",
        )

        env = dict(os.environ)
        env["PATH"] = f"{tmp_path}:{env['PATH']}"
        env.update(env_overrides)
        for key in ("AADS_TEST_IMAGE_SOURCE",):
            if key not in env_overrides:
                env.pop(key, None)

        proc = subprocess.run(
            ["bash", str(runner)], capture_output=True, text=True, env=env, timeout=30
        )
        assert proc.returncode == 0, proc.stderr
        return [line for line in proc.stdout.splitlines() if line]


def test_candidate_order_prefers_explicit_override_first():
    names = _run_resolve_candidates(
        {"AADS_TEST_IMAGE_SOURCE": "my-custom-container"},
        active_container_content="aads-server-blue\n",
        docker_ps_names=["aads-server-green"],
    )
    assert names == ["my-custom-container", "aads-server", "aads-server-blue", "aads-server-green"]


def test_candidate_order_falls_back_through_active_file_then_docker_ps_when_alias_missing():
    names = _run_resolve_candidates(
        {},
        active_container_content="aads-server-green\n",
        docker_ps_names=["aads-server-green", "aads-server-blue"],
    )
    # "aads-server" 는 컷오버 창에 사라질 수 있는 별칭이라 여전히 2순위 후보로 시도되고,
    # 그다음 .active_container 값, 그다음 docker ps 로 찾은 healthy 컨테이너 순이다.
    assert names == ["aads-server", "aads-server-green", "aads-server-blue"]


def test_candidate_list_dedupes_while_preserving_first_occurrence_order():
    names = _run_resolve_candidates(
        {},
        active_container_content="aads-server\n",
        docker_ps_names=["aads-server", "aads-server-blue"],
    )
    assert names == ["aads-server", "aads-server-blue"]


def test_candidate_list_empty_when_nothing_resolves():
    names = _run_resolve_candidates({}, active_container_content=None, docker_ps_names=[])
    assert names == ["aads-server"]
