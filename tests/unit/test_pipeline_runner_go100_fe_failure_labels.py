"""GO100 프론트 BG 배포 실패 사유 분기 (AADS-RUNNER-GO100-FE-FAILURE-LABELS-20260930).

2026-09-30 runner-06e8ddf5 가 go100-frontend:build_failed 로 보고됐지만 npm run build 는
돈 적이 없었다 — Deploy Gate 의 dirty worktree 차단이었다. BG 스크립트는 모든 실패를
exit 1 로 내고, 락 경합이면 큐에 넣고 exit 0 하므로 종료코드로는 구분할 수 없다.
분류는 캡처한 전체 출력의 고정 문자열로 한다. 분류 함수만 떼어 bash 로 실행해 검증한다.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "pipeline-runner.sh"

GATE = "[BG-DEPLOY] ❌ Deploy Gate 차단 — 커밋 후 재시도"
LOCK = "[BG-DEPLOY] ❌ 동시 배포 차단: 다른 배포가 이미 진행 중입니다."
WT_BUSY = "[BG-DEPLOY] ❌ release worktree 사용 중: 백엔드 배포/인증 또는 다른 프론트 배포가 진행 중입니다."
QUEUED = "[BG-DEPLOY-QUEUE] queued_for_deploy release=abc1234 holder_lock=/tmp/x.lock holder_pid=99 queue=/tmp/q"
BUILD = "[BG-DEPLOY]   ❌ 빌드 실패 — active 서비스 무영향"
STANDBY = "[BG-DEPLOY]   ❌ standby health 실패: HTTP 502"
EXTERNAL = "[BG-DEPLOY]   ❌ 외부 프론트 헬스 확인 실패: HTTP 502"
INACTIVE = "[BG-DEPLOY]   ❌ inactive 서비스 health 실패 — active 서비스 유지"


def _extract_function(script: str, name: str) -> str:
    start = script.index(f"{name}() {{")
    end = script.index("\n}\n", start) + len("\n}\n")
    return script[start:end]


@pytest.fixture(scope="module")
def fn_file(tmp_path_factory):
    if shutil.which("bash") is None or shutil.which("grep") is None:
        pytest.skip("bash/grep 미설치 환경")
    script = SCRIPT.read_text(encoding="utf-8")
    body = "set -eo pipefail\n"
    for name in (
        "go100_fe_failure_label",
        "_go100_fe_out_has",
        "go100_fe_bg_classify",
        "go100_fe_failure_chat_text",
    ):
        body += _extract_function(script, name)
    path = tmp_path_factory.mktemp("fe_labels") / "fn.sh"
    path.write_text(body, encoding="utf-8")
    return path


def _run(fn_file, call: str) -> str:
    r = subprocess.run(
        ["bash", "-c", f'source "{fn_file}"; {call}'],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _classify(fn_file, tmp_path, rc: int, output: str) -> str:
    out = tmp_path / "bg.out"
    out.write_text(output, encoding="utf-8")
    return _run(fn_file, f'go100_fe_bg_classify {rc} "{out}"')


# ── 검수자 표의 7개 케이스 ────────────────────────────────────────────


@pytest.mark.parametrize(
    "rc,output,expected",
    [
        (1, GATE + "\n", "go100-frontend:deploy_gate_blocked"),
        (1, LOCK + "\n확인: lsof /tmp/x.lock\n", "go100-frontend:deploy_lock_busy"),
        (1, WT_BUSY + "\n", "go100-frontend:deploy_lock_busy"),
        (0, QUEUED + "\n", "go100-frontend:deploy_queued_not_applied"),
        (1, "1/7 build...\n" + BUILD + "\n", "go100-frontend:build_failed"),
        (1, "6/7 ...\n" + STANDBY + "\n", "go100-frontend:health_failed"),
        (1, "7/7 ...\n" + EXTERNAL + "\n", "go100-frontend:health_failed"),
        (1, "뭔가 알 수 없는 실패\n", "go100-frontend:bg_failed_unclassified"),
    ],
    ids=[
        "gate", "lock", "release_wt_busy", "queued_rc0", "build",
        "health_standby", "health_external", "unclassified",
    ],
)
def test_failure_table(fn_file, tmp_path, rc, output, expected):
    assert _classify(fn_file, tmp_path, rc, output) == expected


def test_queued_with_rc0_is_not_success(fn_file, tmp_path):
    label = _classify(fn_file, tmp_path, 0, "[BG-DEPLOY-QUEUE] worker started pid=1\n" + QUEUED + "\n")
    assert label == "go100-frontend:deploy_queued_not_applied"


def test_rc0_without_queue_is_success(fn_file, tmp_path):
    assert _classify(fn_file, tmp_path, 0, "7/7 완료\n✅ 배포 성공\n") == ""


def test_inactive_health_failure_is_health(fn_file, tmp_path):
    assert _classify(fn_file, tmp_path, 1, INACTIVE + "\n") == "go100-frontend:health_failed"


def test_empty_or_missing_output_is_unclassified_not_build_failed(fn_file, tmp_path):
    assert _classify(fn_file, tmp_path, 1, "") == "go100-frontend:bg_failed_unclassified"
    missing = tmp_path / "nope.out"
    assert _run(fn_file, f'go100_fe_bg_classify 1 "{missing}"') == "go100-frontend:bg_failed_unclassified"


def test_gate_message_beats_later_build_words(fn_file, tmp_path):
    out = GATE + "\n" + BUILD + "\n"
    assert _classify(fn_file, tmp_path, 1, out) == "go100-frontend:deploy_gate_blocked"


# ── tail -30 절단 회귀 ────────────────────────────────────────────────


def test_gate_message_far_beyond_tail_30_still_classified(fn_file, tmp_path):
    noise = "\n".join(f"[BG-DEPLOY] 진행 로그 {i}" for i in range(500))
    out = tmp_path / "long.out"
    out.write_text(GATE + "\n" + noise + "\n", encoding="utf-8")

    # 옛 방식(tail -30 뒤에서 분류)은 Deploy Gate 줄이 이미 사라져 있다
    tail = subprocess.run(["tail", "-30", str(out)], capture_output=True, text=True).stdout
    assert "Deploy Gate 차단" not in tail
    assert _run(fn_file, f'go100_fe_bg_classify 1 "{out}"') == "go100-frontend:deploy_gate_blocked"
    # 전체 파일을 분류하면 잡히고, runner.log 용 tail 은 30줄이다
    assert len(tail.splitlines()) == 30


# ── 라벨 표 / 채팅 문구 ───────────────────────────────────────────────


def test_label_table_includes_worktree_and_script_missing(fn_file):
    assert _run(fn_file, "go100_fe_failure_label release_worktree_failed") == "go100-frontend:release_worktree_failed"
    assert _run(fn_file, "go100_fe_failure_label bluegreen_script_missing") == "go100-frontend:bluegreen_script_missing"
    assert _run(fn_file, "go100_fe_failure_label 뭐든") == "go100-frontend:bg_failed_unclassified"


def test_chat_text_differs_by_label(fn_file):
    gate = _run(fn_file, "go100_fe_failure_chat_text go100-frontend:deploy_gate_blocked 1")
    queued = _run(fn_file, "go100_fe_failure_chat_text go100-frontend:deploy_queued_not_applied 0")
    build = _run(fn_file, "go100_fe_failure_chat_text go100-frontend:build_failed 1")
    assert "Deploy Gate" in gate and "빌드는 시작되지 않음" in gate
    assert "큐" in queued and "미반영" in queued
    assert "빌드" not in queued
    assert build.startswith("GO100 프론트엔드 blue-green 배포 실패")


# ── 러너 통합 계약 ────────────────────────────────────────────────────


def _go100_block() -> str:
    s = SCRIPT.read_text(encoding="utf-8")
    start = s.index("        GO100)\n            # GO100 API: systemd")
    return s[start:s.index("        SF)\n", start)]


def test_runner_captures_full_output_then_tails_and_cleans_up():
    block = _go100_block()
    run = block.index('bash "$_fe_bg_script" --apply >"$_fe_bg_outf" 2>&1')
    tail = block.index('tail -30 "$_fe_bg_outf"', run)
    classify = block.index('go100_fe_bg_classify "$_fe_bg_rc" "$_fe_bg_outf"', run)
    cleanup = block.index('rm -f "$_fe_bg_outf"', classify)
    assert run < tail < classify < cleanup
    # 옛 방식(파이프 앞단 tail) 회귀 금지
    assert '--apply 2>&1 | tail -30' not in block


def test_runner_uses_label_from_classifier_not_hardcoded_build_failed():
    block = _go100_block()
    assert '${_build_fail:+${_build_fail};}${_fe_bg_label}' in block
    assert '${_build_fail:+${_build_fail};}go100-frontend:build_failed' not in block


def test_freshness_gate_still_skips_for_every_go100_label():
    block = _go100_block()
    assert '[[ "$_build_fail" != *"go100-frontend:"* ]]' in block


def test_final_partial_fail_wording_unchanged():
    script = SCRIPT.read_text(encoding="utf-8")
    assert "배포 부분 실패 — 빌드 실패 감지: ${_build_fail}" in script
    assert 'if [[ -n "$_build_fail" ]]; then' in script
