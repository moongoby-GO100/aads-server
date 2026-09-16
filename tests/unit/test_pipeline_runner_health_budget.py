"""헬스체크가 앱 준비 시간보다 먼저 포기하지 않아야 한다 (AADS-RUNNER-HEALTH-BUDGET).

2026-09-16 실측 (contabo14, go100.service):
  11:06:34 systemctl restart go100
  11:06:37 gunicorn "Listening at http://127.0.0.1:8002"   ← 소켓 3초
  11:07:53 uvicorn "Application startup complete"           ← 앱 79초
  헬스체크 11:06:56 / 11:07:16 / 11:07:37 → 전부 실패
  11:07:43 ROLLBACK_START (backend=FAIL) → git revert

즉 **준비 완료 10초 전에 포기**하고 승인된 GO100 P0 청산 안전장치를 되돌렸다.
소켓이 열린 시점과 앱이 준비된 시점은 다르다. 이 테스트는 예산이 다시 줄어드는 것을 막는다.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ("pipeline-runner.sh", "pipeline-runner.sh.local")

# 실측된 최악 기동 시간(79초)에 여유를 둔 하한. 이보다 짧으면 GO100 배포는 결정적으로 롤백된다.
MIN_BUDGET_SEC = 120


def _read(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def test_health_budget_exceeds_measured_startup_time():
    for name in SCRIPTS:
        script = _read(name)

        m = re.search(r'health_budget_sec="\$\{HEALTH_CHECK_BUDGET_SEC:-(\d+)\}"', script)
        assert m, f"{name}: 헬스체크 예산 설정을 찾을 수 없다"
        assert int(m.group(1)) >= MIN_BUDGET_SEC, (
            f"{name}: 예산 {m.group(1)}초는 실측 기동 79초를 견디지 못한다"
        )


def test_old_three_retry_loop_is_gone():
    """sleep 10 × 3(=63초) 루프로 되돌리면 같은 사고가 그대로 재현된다."""
    script = _read("pipeline-runner.sh")

    assert "for _retry in 1 2 3; do\n            sleep 10" not in script
    assert "헬스체크 재시도 ${_retry}/3" not in script


def test_polling_interval_is_shorter_than_before():
    """예산을 늘리되 폴링은 짧게 — 빠른 서비스는 오히려 더 빨리 통과해야 한다."""
    script = _read("pipeline-runner.sh")

    m = re.search(r'health_interval_sec="\$\{HEALTH_CHECK_INTERVAL_SEC:-(\d+)\}"', script)
    assert m, "폴링 간격 설정을 찾을 수 없다"
    assert int(m.group(1)) <= 10


def test_budget_and_interval_are_overridable():
    """서비스마다 기동 시간이 다르다 — 코드를 고치지 않고 조정할 수 있어야 한다."""
    script = _read("pipeline-runner.sh")

    assert "HEALTH_CHECK_BUDGET_SEC" in script
    assert "HEALTH_CHECK_INTERVAL_SEC" in script


def test_failure_is_logged_with_budget_for_diagnosis():
    """실패했을 때 '얼마를 기다리다 포기했는지'가 없으면 원인 추적이 또 로그 뒤지기가 된다."""
    script = _read("pipeline-runner.sh")

    assert "HEALTH_FAIL job=$job_id budget=${health_budget_sec}s" in script
    assert "HEALTH_OK job=$job_id after=${_waited}s" in script
