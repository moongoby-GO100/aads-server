"""
scripts/run_unit_tests.sh 의 후보 해석 + 재시도 루프 전체 흐름 테스트.

test_run_unit_tests_container_fallback.py 는 resolve_test_image_candidates() 만
떼어서 후보 "목록"을 검사한다. 여기서는 후보 목록 + 이미지 조회 재시도 루프 +
실패 메시지까지 이어서 실행해, 컷오버 창에서 컨테이너/이미지가 어느 상태로
있어도 실제로 이미지가 뽑히는지(또는 exit 2 로 명확히 실패하는지)를 검사한다.

`bash -c 'source scripts/run_unit_tests.sh ...'` 는 스크립트 본문(pytest 실행부)까지
실행해버리므로 쓰지 않는다. 대신 함수 블록 + 후보 해석~재시도 블록만 sed 없이
문자열 슬라이스로 떼어내 임시 러너에 넣고, PATH 맨 앞의 가짜 docker 로 시나리오를
만든다.
"""
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


def _extract_resolution_block(script: str) -> str:
    start = script.index("mapfile -t RUNTIME_CANDIDATES")
    end = script.index('mkdir -p "$TESTDEPS_DIR"')
    block = script[start:end]
    # 테스트에서는 재시도 대기를 0 초로 줄인다 — 실제 재시도 "횟수"(6회)는
    # 그대로 실행되므로 로직은 검증되지만 sleep 5 를 6번 기다리지 않는다.
    assert "RETRY_INTERVAL_SECONDS=5" in block
    return block.replace("RETRY_INTERVAL_SECONDS=5", "RETRY_INTERVAL_SECONDS=0")


def _fake_docker_script(
    ps_healthy_file: Path,
    ps_any_file: Path,
    ps_summary_file: Path,
    images_file: Path,
    inspect_ok_file: Path,
    image_map_file: Path,
) -> str:
    return f"""#!/bin/bash
ARGS="$*"
case "$1" in
    ps)
        if [[ "$ARGS" == *"health=healthy"* ]]; then
            cat {ps_healthy_file} 2>/dev/null
        elif [[ "$ARGS" == *"name=aads-server-"* ]]; then
            cat {ps_any_file} 2>/dev/null
        else
            cat {ps_summary_file} 2>/dev/null
        fi
        ;;
    images)
        cat {images_file} 2>/dev/null
        ;;
    inspect)
        if [ "$2" = "-f" ]; then
            name="$4"
            awk -F'\\t' -v n="$name" '$1==n {{print $2; found=1}} END {{exit !found}}' {image_map_file}
        else
            name="$2"
            grep -qxF "$name" {inspect_ok_file} 2>/dev/null
        fi
        ;;
    *)
        exit 1
        ;;
esac
"""


def _run_resolution(
    ps_healthy=(),
    ps_any=(),
    ps_summary=("aads-server  (no container found)",),
    images=(),
    inspect_ok=(),
    image_map=None,
    active_container_content=None,
    env_overrides=None,
):
    script = _read_script()
    fn_body = _extract_function(script, "resolve_test_image_candidates")
    resolution_block = _extract_resolution_block(script)
    image_map = image_map or {}

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        active_file = tmp_path / "active_container"
        if active_container_content is not None:
            active_file.write_text(active_container_content, encoding="utf-8")

        ps_healthy_file = tmp_path / "ps_healthy.txt"
        ps_any_file = tmp_path / "ps_any.txt"
        ps_summary_file = tmp_path / "ps_summary.txt"
        images_file = tmp_path / "images.txt"
        inspect_ok_file = tmp_path / "inspect_ok.txt"
        image_map_file = tmp_path / "image_map.txt"

        ps_healthy_file.write_text("\n".join(ps_healthy) + ("\n" if ps_healthy else ""), encoding="utf-8")
        ps_any_file.write_text("\n".join(ps_any) + ("\n" if ps_any else ""), encoding="utf-8")
        ps_summary_file.write_text("\n".join(ps_summary) + ("\n" if ps_summary else ""), encoding="utf-8")
        images_file.write_text("\n".join(images) + ("\n" if images else ""), encoding="utf-8")
        inspect_ok_file.write_text("\n".join(inspect_ok) + ("\n" if inspect_ok else ""), encoding="utf-8")
        image_map_file.write_text(
            "".join(f"{name}\t{image}\n" for name, image in image_map.items()), encoding="utf-8"
        )

        fake_docker = tmp_path / "docker"
        fake_docker.write_text(
            _fake_docker_script(
                ps_healthy_file, ps_any_file, ps_summary_file, images_file, inspect_ok_file, image_map_file
            ),
            encoding="utf-8",
        )
        fake_docker.chmod(fake_docker.stat().st_mode | stat.S_IEXEC)

        runner = tmp_path / "runner.sh"
        runner.write_text(
            "set -uo pipefail\n"
            f"ACTIVE_CONTAINER_FILE={str(active_file)!r}\n"
            + fn_body
            + "\n"
            + resolution_block
            + '\necho "RESOLVED_IMAGE=$IMAGE"\nexit 0\n',
            encoding="utf-8",
        )

        env = dict(os.environ)
        env["PATH"] = f"{tmp_path}:{env['PATH']}"
        for key in ("AADS_TEST_IMAGE_SOURCE",):
            env.pop(key, None)
        if env_overrides:
            env.update(env_overrides)

        return subprocess.run(
            ["bash", str(runner)], capture_output=True, text=True, env=env, timeout=60
        )


def test_alias_aads_server_alive_is_used_first():
    """① 별칭 aads-server 가 살아 있으면 그것이 1순위로 뽑힌다."""
    proc = _run_resolution(image_map={"aads-server": "aads-server:abc123"})
    assert proc.returncode == 0, proc.stderr
    assert "RESOLVED_IMAGE=aads-server:abc123" in proc.stdout


def test_healthy_green_used_when_alias_missing():
    """② 별칭이 없고 green 이 healthy 면 green 이 뽑힌다."""
    proc = _run_resolution(
        ps_healthy=["aads-server-green"],
        image_map={"aads-server-green": "aads-server:green123"},
    )
    assert proc.returncode == 0, proc.stderr
    assert "RESOLVED_IMAGE=aads-server:green123" in proc.stdout


def test_starting_container_used_when_nothing_is_healthy_yet():
    """③ 별칭 없음 + 전부 starting → health 필터 없는 후보에서 뽑힌다."""
    proc = _run_resolution(
        ps_healthy=[],
        ps_any=["aads-server-blue"],
        image_map={"aads-server-blue": "aads-server:blue123"},
    )
    assert proc.returncode == 0, proc.stderr
    assert "RESOLVED_IMAGE=aads-server:blue123" in proc.stdout


def test_image_tag_fallback_used_when_no_container_exists():
    """④ 컨테이너가 하나도 없고 이미지만 있으면 image: 후보로 이미지 태그가 뽑힌다."""
    proc = _run_resolution(
        ps_healthy=[],
        ps_any=[],
        images=["aads-server:f3fc0a717e11"],
    )
    assert proc.returncode == 0, proc.stderr
    assert "RESOLVED_IMAGE=aads-server:f3fc0a717e11" in proc.stdout


def test_exit_2_with_diagnostic_reasons_when_nothing_resolves():
    """⑤ 컨테이너도 이미지도 없으면 exit 2 이고, 메시지에 시도한 후보와 실패 사유가 들어 있다."""
    proc = _run_resolution(ps_healthy=[], ps_any=[], images=[])
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "시도한 후보와 실패 사유" in proc.stderr
    assert "container:aads-server -> no such container" in proc.stderr
    assert "docker ps --filter name=aads-server 요약" in proc.stderr
