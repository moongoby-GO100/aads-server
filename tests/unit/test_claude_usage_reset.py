from datetime import UTC, datetime, timedelta

from app.services.oauth_usage_tracker import _effective_usage_window


def test_elapsed_exhausted_window_recovers_to_full_headroom():
    now = datetime(2026, 9, 23, 0, 0, tzinfo=UTC)
    reset_at = now - timedelta(hours=1)

    window = _effective_usage_window(100, reset_at, 10080, now=now)

    assert window == {
        "used_percent": 0.0,
        "window_minutes": 10080,
        "resets_at": None,
        "last_reset_at": reset_at.isoformat(),
        "reset_elapsed": True,
        "estimated_after_reset": True,
    }


def test_future_reset_keeps_measured_usage():
    now = datetime(2026, 9, 23, 0, 0, tzinfo=UTC)
    reset_at = now + timedelta(hours=2)

    window = _effective_usage_window(84, reset_at, 10080, now=now)

    assert window["used_percent"] == 84.0
    assert window["resets_at"] == reset_at.isoformat()
    assert window["reset_elapsed"] is False
    assert window["estimated_after_reset"] is False


def test_missing_reset_does_not_invent_recovery():
    window = _effective_usage_window(100, None, 10080)

    assert window["used_percent"] == 100.0
    assert window["resets_at"] is None
    assert window["reset_elapsed"] is False
