"""러너가 root 로 돌아도 파일을 쓸 수 있어야 한다 (AADS-RUNNER-ROOT-WRITE).

2026-09-16 실측(Claude Code 2.1.273, contabo116 root):
  --dangerously-skip-permissions 만  → "cannot be used with root/sudo privileges" 로 거부
  플래그 없이 -p                      → Write/Edit 이 전량 승인 대기로 막힘
  IS_SANDBOX=1 + 플래그               → 정상, probe.txt 생성 확인

이 조합이 깨지면 AADS 러너 작업이 **산출물 0건으로 조용히 성공 처리**된다.
실패로 보이지 않기 때문에 가장 늦게 발견된다 — 오늘 세 건(runner-57ef70e9,
runner-dc19afb7, runner-c2055401)이 그렇게 끝났다.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ("pipeline-runner.sh", "pipeline-runner.sh.local")


def _read(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def test_permission_flag_is_unconditional():
    """root 여부로 플래그를 빼면 쓰기가 막힌다 — 조건부로 되돌리지 마라."""
    for name in SCRIPTS:
        script = _read(name)

        assert "claude_args+=(--dangerously-skip-permissions)" in script
        # 과거 결함: root 일 때만 플래그를 빼던 분기
        assert 'if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then\n                claude_args+=(--dangerously-skip-permissions)' not in script


def test_root_gets_is_sandbox_env():
    for name in SCRIPTS:
        script = _read(name)

        assert 'if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then' in script
        assert "claude_root_env=(env IS_SANDBOX=1)" in script


def test_is_sandbox_is_scoped_to_the_child_process():
    """러너 셸 전체에 IS_SANDBOX 를 걸면 다른 도구 동작까지 바뀐다."""
    script = _read("pipeline-runner.sh")

    assert "export IS_SANDBOX" not in script
    assert "\nIS_SANDBOX=1\n" not in script


def test_both_cli_invocations_use_the_root_env_prefix():
    """슬롯 자격증명 경로와 폴백 경로 둘 다 적용돼야 한다 — 한쪽만 고치면 절반이 막힌다."""
    script = _read("pipeline-runner.sh")

    assert script.count('${claude_root_env[@]+"${claude_root_env[@]}"}') == 2


def test_fallback_invocation_closes_stdin():
    """stdin 을 닫지 않으면 CLI 가 'no stdin data received in 3s' 로 3초를 버린다."""
    script = _read("pipeline-runner.sh")

    invocation = script[script.index("claude_root_env=(env IS_SANDBOX=1)"):]
    assert invocation.count("< /dev/null") >= 2
