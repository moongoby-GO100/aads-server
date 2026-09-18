"""instruction_forbids_deploy() 가 표현 변형을 정규식으로 잡는지 고정한다.

2026-09-18 사고(오류사전 runner.deploy_forbid_pattern_miss): runner-6d17facb
지시서에 "빌드·배포 실행 금지"가 명시돼 있었는데, 게이트는 고정 문자열 13개의
부분일치만 봐서 트리거 단어("배포")와 금지어("금지") 사이에 "실행" 같은 말이
낀 표현을 잡지 못했다. push→빌드→배포가 그대로 실행됐고 review_feedback 에
"[v2.1][배포완료]" 가 남았다.

같은 파일의 워커 프롬프트(H7)에는 또 다른 결함이 있었다: 지시서 완료기준에
"npm run build exit 0" 이 들어 있어도 워커는 그 명령을 절대 실행하지 말라는
규칙만 있어서, 완료기준을 채우려면 규칙을 어겨야 하는 모순이 있었다. 이 파일은
그 안내 문장이 실제로 존재하는지도 고정한다.
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


# ── 판정 동작 ───────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def gate_fn(tmp_path_factory):
    if shutil.which("bash") is None:
        pytest.skip("bash 미설치 환경")
    fn_file = tmp_path_factory.mktemp("gate_fn") / "fn.sh"
    fn_file.write_text(
        "set -eo pipefail\n" + _extract_function(_read_script(), "instruction_forbids_deploy"),
        encoding="utf-8",
    )
    return fn_file


def _decide(fn_file: Path, text: str) -> str:
    proc = subprocess.run(
        ["bash", "-c", f'source "{fn_file}"; if instruction_forbids_deploy "$1"; '
                       f"then echo FORBID; else echo ALLOW; fi", "_", text],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


@pytest.mark.parametrize(
    "text",
    [
        "배포 금지",
        "배포금지",
        "빌드·배포 실행 금지",
        "배포를 실행하지 마",
        "배포·재기동 절대 금지",
        "재기동 금지",
        "커밋까지만",
        "push 까지만",
        "Do not deploy",
        "no deploy",
    ],
)
def test_forbidding_expressions_are_detected(gate_fn, text):
    """runner-6d17facb 가 놓친 표현 변형 — 트리거와 금지어 사이에 다른 말이 껴도 잡아야 한다."""
    assert _decide(gate_fn, text) == "FORBID"


@pytest.mark.parametrize(
    "text",
    [
        "배포 후 검증해라",
        "deployment notes",
        "대시보드 Mermaid 렌더러 도입 — 배포 후 화면 캡처로 검증하라.",
        "",
    ],
)
def test_non_forbidding_expressions_pass_no_false_positive(gate_fn, text):
    assert _decide(gate_fn, text) == "ALLOW"


# ── 정적 계약 ───────────────────────────────────────────────────────────


def test_worker_prompt_documents_build_completion_criteria_handoff():
    """완료기준에 빌드 명령이 있어도 워커는 실행하지 말고 Runner 위임을 RESULT 에 적어야 한다."""
    for script_name in SCRIPTS:
        script = _read_script(script_name)
        assert "승인 후 Runner 빌드 검증 대상" in script
        assert "npm run build / next build / docker build" in script
        # 기존 금지 목록은 지우거나 완화하지 않는다.
        assert "npm run build, npm start, next build" in script
        assert "docker build, docker compose, docker restart" in script


def test_scripts_are_byte_identical():
    main_script = (ROOT / "scripts" / "pipeline-runner.sh").read_bytes()
    local_script = (ROOT / "scripts" / "pipeline-runner.sh.local").read_bytes()
    assert main_script == local_script
