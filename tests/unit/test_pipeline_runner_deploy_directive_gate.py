"""지시서의 '배포 금지' 제약을 러너가 실제로 지키는지 고정한다.

2026-09-16 사고: GO100 runner-1791da41 지시서에 "커밋까지만, 배포·재기동 절대 금지"가
명시돼 있었는데 승인 즉시 push→빌드→배포가 돌았고, 그 배포가 헬스체크에 실패해
11:09 에 P0 청산 안전장치(validate_exit_price)를 자동 revert 시켰다.
장중에 안전장치가 사라졌다. 사람이 쓴 제약은 코드가 막지 않으면 지켜지지 않는다.
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


# ── 계약 ────────────────────────────────────────────────────────────────


def test_gate_exists_in_both_runner_scripts():
    for script_name in SCRIPTS:
        script = _read_script(script_name)

        assert "instruction_forbids_deploy() {" in script
        assert "DEPLOY_SKIPPED_BY_DIRECTIVE" in script
        assert "phase='push_only_by_directive'" in script


def test_gate_runs_after_push_and_before_build():
    """push 는 해야 한다 — 막는 것은 빌드·배포뿐이다."""
    script = _read_script()

    push_ok = script.index("GIT_PUSH_OK job=$job_id")
    gate = script.index("if instruction_forbids_deploy")
    build = script.index("무중단 배포 v3.0")

    assert push_ok < gate < build


def test_skipped_deploy_is_success_not_error():
    """지시서를 지킨 것은 실패가 아니다. error 로 끝내면 사람이 원인을 오해한다."""
    script = _read_script()

    gate = script.index("if instruction_forbids_deploy")
    tail = script[gate:gate + 2000]

    assert "status='done'" in tail
    assert "_release_deploy_lock" in tail
    assert "promote_next_queued" in tail
    assert "return 0" in tail


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
        "배포·systemctl 재기동·릴리스 슬롯 전환 절대 금지. 커밋까지만. push 도 하지 않는다.",
        "TITLE: 청산 게이트 (커밋까지만, 배포·재기동 절대 금지)",
        "이 작업은 배포 금지 대상이다",
        "Do not deploy — commit only.",
        "빌드까지만 하고 NO DEPLOY",
    ],
)
def test_forbidding_instructions_are_detected(gate_fn, text):
    assert _decide(gate_fn, text) == "FORBID"


@pytest.mark.parametrize(
    "text",
    [
        "TASK_ID: AADS-AAG-001 추출기를 만들고 단위테스트를 통과시켜라.",
        "대시보드 Mermaid 렌더러 도입 — 배포 후 화면 캡처로 검증하라.",
        "",
    ],
)
def test_normal_instructions_still_deploy(gate_fn, text):
    assert _decide(gate_fn, text) == "ALLOW"


def test_real_incident_instruction_would_have_been_blocked(gate_fn):
    """runner-1791da41 지시서 원문 발췌 — 이 게이트가 있었으면 배포가 없었다."""
    instruction = (
        "TASK_ID: GO100-C310-EXIT-PRICE-SANITY-R2\n"
        "TITLE: #310 청산 가격 무결성 게이트 — 유령 시세(+3%)로 TP가 발사된 사고 재발 방지 "
        "(커밋까지만, 배포·재기동 절대 금지)\n"
        "PRIORITY: P0-CRITICAL\n"
    )

    assert _decide(gate_fn, instruction) == "FORBID"


# ── 릴리스 잡 오탐 보정 (AADS-RUNNER-DEPLOY-DIRECTIVE-GATE-FALSE-POSITIVE, 2026-10-22) ──
# runner-c9211761·db0342f8 은 DEPLOY_ONLY 릴리스 잡인데 본문 산문이 배포 금지로 오인돼 배포가 건너뛰어졌다.
# 판정은 로캘(C / C.UTF-8)에 따라 갈리면 안 되므로 모든 케이스를 두 로캘로 돌린다.

LOCALES = ("C", "C.UTF-8")
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _decide_in_locale(fn_file: Path, text: str, locale: str) -> str:
    import os

    env = {**os.environ, "LC_ALL": locale, "LANG": locale}
    proc = subprocess.run(
        ["bash", "-c", f'source "{fn_file}"; if instruction_forbids_deploy "$1"; '
                       f"then echo FORBID; else echo ALLOW; fi", "_", text],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return proc.stdout.strip()


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize(
    "fixture",
    ["runner_c9211761_release_instruction.txt", "runner_db0342f8_release_instruction.txt"],
)
def test_release_job_prose_is_not_a_deploy_forbid(gate_fn, fixture, locale):
    text = (FIXTURES / fixture).read_text(encoding="utf-8")
    assert _decide_in_locale(gate_fn, text, locale) == "ALLOW"


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize(
    "text",
    [
        "DEPLOY_ONLY: true\nPUSH_ONLY: true\nTARGET: /root/aads/aads-server",
        "DEPLOY_ONLY: true\nPUSH_ONLY:true\n본문",
        "DEPLOY_ONLY: true\nDEPLOY_POLICY: bluegreen\nPUSH_ONLY: true",
        "DEPLOY_ONLY: true\nDEPLOY_POLICY: push_only\n본문",
        "DEPLOY_ONLY: true\nDEPLOY_POLICY: bluegreen\nDEPLOY_POLICY: push_only",
        "DEPLOY_ONLY: true\nDEPLOY_POLICY: none",
        "DEPLOY_ONLY: true\nPUSH_ONLY",
    ],
)
def test_release_job_with_push_only_declaration_is_forbidden(gate_fn, text, locale):
    assert _decide_in_locale(gate_fn, text, locale) == "FORBID"


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize(
    "text",
    [
        "DEPLOY_ONLY: true\nDEPLOY_POLICY: bluegreen\n이번 작업에서는 배포 금지로 오인했다.",
        "DEPLOY_ONLY :  true\n  Deploy_Policy:  BlueGreen\n배포하지 않고 중단 보고한다.",
        "DEPLOY_ONLY: true\n배포하지 않고 중단 보고한다. 운영 nginx 설정 수정은 금지다.",
        "DEPLOY_ONLY: true\nDEPLOY_POLICY: bluegreen\nPUSH_ONLY: false",
    ],
)
def test_release_job_exception_skips_prose_scan(gate_fn, text, locale):
    assert _decide_in_locale(gate_fn, text, locale) == "ALLOW"


@pytest.mark.parametrize("locale", LOCALES)
def test_deploy_only_outside_header_window_is_not_release_job(gate_fn, locale):
    text = "TASK_ID: X\n" * 25 + "DEPLOY_ONLY: true\n배포 금지"
    assert _decide_in_locale(gate_fn, text, locale) == "FORBID"


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize(
    "text",
    [
        "PUSH_ONLY:true\nTARGET: /root/aads/aads-server",
        "TARGET: /root/aads/aads-server\nDEPLOY_POLICY: push_only",
        "PUSH_ONLY:true DEPLOY_POLICY:push_only TARGET: /root/aads/aads-server",
        "DEPLOY_POLICY: bluegreen\nPUSH_ONLY: true",
    ],
)
def test_general_job_policy_declaration_lines_forbid(gate_fn, text, locale):
    assert _decide_in_locale(gate_fn, text, locale) == "FORBID"


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize(
    "text",
    [
        "phase=push_only_by_directive 로 끝난 잡을 분석한다.",
        "TASK_ID: X\n선행 잡이 phase=push_only_by_directive 로 끝났다. 원인을 고친다.",
        "DEPLOY_POLICY: bluegreen\n선행 잡이 phase=push_only 로 끝났다. 원인을 고친다.",
    ],
)
def test_push_only_mentioned_only_in_prose_is_not_a_signal(gate_fn, text, locale):
    assert _decide_in_locale(gate_fn, text, locale) == "ALLOW"


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize(
    "text",
    [
        "커밋까지만, 배포·재기동 절대 금지",
        "빌드·배포 실행 금지",
        "do not deploy",
        "TASK_ID: X\n본문\n배포하지 않는다.",
    ],
)
def test_general_job_existing_prose_forbids_are_kept(gate_fn, text, locale):
    assert _decide_in_locale(gate_fn, text, locale) == "FORBID"


@pytest.mark.parametrize("locale", LOCALES)
def test_decision_does_not_depend_on_shell_locale(gate_fn, locale):
    """'배포' 와 '금지' 사이가 한글 15자(바이트 45) — LC_ALL=C 에서 바이트로 세면 40 을 넘어 놓쳤다."""
    text = "배포는 이번 작업에서 아무 관련 없는 항목이라서 금지"
    assert _decide_in_locale(gate_fn, text, locale) == "FORBID"
