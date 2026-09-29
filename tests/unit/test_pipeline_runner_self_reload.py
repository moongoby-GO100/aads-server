"""러너가 유휴 시점에 바뀐 자기 스크립트를 새 코드로 갈아끼운다.

배경 (2026-09-29). 배포 게이트 연쇄 차단 교정이 main 에 들어간 뒤에도 승인
4건이 같은 `deploy_isolated_stale_approval` 로 죽었다. 원인은 코드가 아니라
프로세스였다 — bash 는 기동 시 함수 정의를 전부 파싱하므로 08:00 KST 에 뜬
러너 데몬은 12:10 KST 에 갈린 파일을 읽지 않았다. 고친 파일과 돌고 있는
코드가 달랐고, 그 차이는 어느 로그에도 남지 않았다.
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts/pipeline-runner.sh"
SCRIPT = SCRIPT_PATH.read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = SCRIPT.index(f"{name}() {{")
    return SCRIPT[start:SCRIPT.index("\n}\n", start) + 3]


def _harness(tmp_path: Path, *, target_body: str, busy: bool,
             fingerprint: str = "stale0000000000") -> subprocess.CompletedProcess:
    """추출한 두 함수만 스텁 환경에서 돌린다."""
    target = tmp_path / "target.sh"
    target.write_text(target_body, encoding="utf-8")
    harness = tmp_path / "harness.sh"
    harness.write_text(
        "set -eo pipefail\n"
        'log() { echo "$@" >&2; }\n'
        "declare -A _bg_jobs\n"
        + ('_bg_jobs[4242]="runner-aaaaaaaa|sess"\n' if busy else "")
        + '_current_job_id=""\n'
        f'RUNNER_SELF_PATH="{target}"\n'
        "RUNNER_SELF_ARGV=()\n"
        f'RUNNER_SELF_FINGERPRINT="{fingerprint}"\n'
        + _function("_runner_self_fingerprint")
        + _function("maybe_reexec_on_self_change")
        + "maybe_reexec_on_self_change\n"
        'echo "NOT_REEXECED"\n',
        encoding="utf-8",
    )
    return subprocess.run(
        ["bash", str(harness)], capture_output=True, text=True, timeout=60,
    )


def test_reexecs_when_script_changed_and_runner_is_idle(tmp_path):
    result = _harness(tmp_path, target_body='echo "REEXECED"\n', busy=False)
    assert result.returncode == 0, result.stderr
    assert "REEXECED" in result.stdout
    assert "NOT_REEXECED" not in result.stdout
    assert "SELF_RELOAD:" in result.stderr


def test_does_not_reexec_while_a_job_is_running(tmp_path):
    """작업 중에 갈아끼우면 실행 중인 자식이 고아가 된다."""
    result = _harness(tmp_path, target_body='echo "REEXECED"\n', busy=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "NOT_REEXECED"


def test_does_not_reexec_into_a_syntactically_broken_script(tmp_path):
    """쓰다 만 파일로 자살하지 않는다 — 현재 코드를 유지한다."""
    result = _harness(tmp_path, target_body='if [[ 1 == 1 ]]; then\necho "half"\n', busy=False)
    assert result.returncode == 0, result.stderr
    assert "NOT_REEXECED" in result.stdout
    assert "SELF_RELOAD_SKIP:" in result.stderr


def test_no_reexec_when_fingerprint_matches(tmp_path):
    target_body = 'echo "REEXECED"\n'
    target = tmp_path / "probe.sh"
    target.write_text(target_body, encoding="utf-8")
    digest = subprocess.run(
        ["sha256sum", str(target)], capture_output=True, text=True, check=True,
    ).stdout.split()[0]
    result = _harness(tmp_path, target_body=target_body, busy=False, fingerprint=digest)
    assert result.returncode == 0, result.stderr
    assert "NOT_REEXECED" in result.stdout
    assert "SELF_RELOAD" not in result.stderr


def test_guard_closes_singleton_lock_fd_before_exec():
    """flock 은 같은 프로세스의 다른 FD 에도 걸린다 — 닫지 않으면 자기 락에 막힌다."""
    body = _function("maybe_reexec_on_self_change")
    assert "exec 9>&-" in body
    assert body.index("exec 9>&-") < body.index('exec bash "$RUNNER_SELF_PATH"')
    assert 'exec 9>"$RUNNER_LOCK_FILE"' in SCRIPT


def test_guard_is_called_from_the_poll_loop_and_can_be_disabled():
    assert "\n        maybe_reexec_on_self_change\n" in SCRIPT
    assert '[[ "${RUNNER_SELF_RELOAD:-1}" == "1" ]] || return 0' in SCRIPT
    # 기동 시 지문을 기록하지 않으면 첫 사이클에서 무조건 재적용된다.
    assert "RUNNER_SELF_FINGERPRINT=$(_runner_self_fingerprint)" in SCRIPT


def test_local_mirror_stays_in_sync():
    """`.local` 사본만 낡으면 어느 쪽이 도는지 알 수 없다."""
    mirror = ROOT / "scripts/pipeline-runner.sh.local"
    assert mirror.read_text(encoding="utf-8") == SCRIPT
