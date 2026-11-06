"""중단·검토 요청은 실패 대신 경고 아이콘으로 저장한다."""
import pytest

from tests.unit.test_auto_rework_no_progress_alert import JOB, alert  # noqa: F401


@pytest.mark.asyncio
async def test_no_progress_alert_uses_warning_icon(alert):
    await alert.notify()
    content = alert.messages[f"{JOB}:no_progress"]
    assert content.startswith("⚠️ **[세션 자동보고]** 자동 재작업 중단 · 검토 요청")
    assert "❌" not in content.splitlines()[0]
    assert alert.reports[0]["status"] == "warning"
