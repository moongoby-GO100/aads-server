"""DEPLOY_ONLY 지시서가 no_changes 게이트를 우회하는지 고정한다.

배포만을 목적으로 하는 잡(코드 변경 없이 재배포/재기동 트리거만 수행)은
git diff 가 항상 비어 있다. 기존 no_changes 게이트는 diff 가 비면 무조건
cancelled 로 종결시키므로, 이런 잡은 승인 단계까지 도달하지 못하고 매번
반려됐다. 지시서 첫 20줄에 "DEPLOY_ONLY: true" 마커가 있으면 이 게이트를
우회하고 정상 진행(승인 대기)하도록 러너 스크립트와 승인 API 양쪽에
예외를 둔다.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ("pipeline-runner.sh", "pipeline-runner.sh.local")


def _read_script(name: str = "pipeline-runner.sh") -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def _extract_function(script: str, name: str) -> str:
    start = script.index(f"{name}() {{")
    end = script.index("\n}\n", start) + len("\n}\n")
    return script[start:end]


# ── 스크립트 계약 ───────────────────────────────────────────────────────


def test_is_deploy_only_instruction_exists_in_both_runner_scripts():
    for script_name in SCRIPTS:
        script = _read_script(script_name)

        assert "is_deploy_only_instruction() {" in script
        assert "DEPLOY_ONLY: true" in script


def test_no_changes_gate_bypassed_for_deploy_only_jobs():
    for script_name in SCRIPTS:
        script = _read_script(script_name)

        no_changes = script.index("NO_CHANGES job=$job_id")
        bypass = script.index("DEPLOY_ONLY_BYPASS job=$job_id")
        # 우회 분기는 cancelled 처리보다 먼저 판정돼야 정상 진행으로 빠질 수 있다.
        assert bypass < no_changes
        assert "if is_deploy_only_instruction \"$instruction\"; then" in script


def test_deploy_only_bypass_does_not_cancel_the_job():
    for script_name in SCRIPTS:
        script = _read_script(script_name)

        cancel_start = script.index("NO_CHANGES job=$job_id")
        # commit_job_worktree_for_approval() 에도 별도의
        # "if is_deploy_only_instruction" 분기가 있으므로, 첫 occurrence 가
        # 아니라 no_changes 게이트 바로 앞의 occurrence 를 찾는다.
        bypass_start = script.rfind("if is_deploy_only_instruction", 0, cancel_start)
        assert bypass_start != -1
        bypass_block = script[bypass_start:cancel_start]

        assert "status='cancelled'" not in bypass_block
        assert "return 1" not in bypass_block


def test_local_pipeline_runner_template_matches_deploy_only_wiring():
    """두 스크립트 파일의 DEPLOY_ONLY 관련 블록이 동일해야 한다."""
    primary = _extract_function(_read_script("pipeline-runner.sh"), "is_deploy_only_instruction")
    local_template = _extract_function(_read_script("pipeline-runner.sh.local"), "is_deploy_only_instruction")

    assert primary == local_template


# ── 판정 동작 ───────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def deploy_only_fn(tmp_path_factory):
    if shutil.which("bash") is None:
        pytest.skip("bash 미설치 환경")
    fn_file = tmp_path_factory.mktemp("deploy_only_fn") / "fn.sh"
    fn_file.write_text(
        "set -eo pipefail\n"
        + _extract_function(_read_script(), "is_deploy_only_instruction"),
        encoding="utf-8",
    )
    return fn_file


def _decide(fn_file: Path, text: str) -> str:
    proc = subprocess.run(
        ["bash", "-c", f'source "{fn_file}"; if is_deploy_only_instruction "$1"; '
                       f"then echo YES; else echo NO; fi", "_", text],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


@pytest.mark.parametrize(
    "text",
    [
        "DEPLOY_ONLY: true\nTASK_ID: AADS-XYZ\nTITLE: 재배포 트리거",
        "TASK_ID: AADS-XYZ\nDEPLOY_ONLY: true\nTITLE: 재배포 트리거",
    ],
)
def test_deploy_only_marker_detected_within_first_20_lines(deploy_only_fn, text):
    assert _decide(deploy_only_fn, text) == "YES"


@pytest.mark.parametrize(
    "text",
    [
        "TASK_ID: AADS-XYZ\nTITLE: 일반 코드 수정 작업",
        "",
        "DEPLOY_ONLY: false\nTASK_ID: AADS-XYZ",
        "deploy_only: true (소문자는 매칭하지 않는다)",
    ],
)
def test_non_deploy_only_instructions_are_not_bypassed(deploy_only_fn, text):
    assert _decide(deploy_only_fn, text) == "NO"


def test_deploy_only_marker_outside_first_20_lines_is_ignored(deploy_only_fn):
    text = "\n".join([f"line {i}" for i in range(20)]) + "\nDEPLOY_ONLY: true"
    assert _decide(deploy_only_fn, text) == "NO"


# ── 승인 API 계약 ───────────────────────────────────────────────────────


def test_approve_endpoint_bypasses_diff_gates_for_deploy_only_jobs():
    api = (ROOT / "app" / "api" / "pipeline_runner.py").read_text(encoding="utf-8")

    assert "def _is_deploy_only_instruction(instruction: str) -> bool:" in api
    assert '"DEPLOY_ONLY: true" in "\\n".join(lines)' in api
    assert "SELECT job_id, project, status, phase, git_diff, instruction," in api

    approve_fn_start = api.index("async def approve_or_reject(")
    approve_fn = api[approve_fn_start:approve_fn_start + 4000]

    assert "deploy_only = _is_deploy_only_instruction(row[\"instruction\"])" in approve_fn
    assert 'if not deploy_only and "diff --git " not in git_diff:' in approve_fn
    assert "if not deploy_only and not re.match(r\"^[0-9a-f]{40}$\", commit_hash):" in approve_fn
    assert "if not deploy_only and not changed_files:" in approve_fn
    assert "if not deploy_only:" in approve_fn
