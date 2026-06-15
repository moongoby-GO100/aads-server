from __future__ import annotations

from datetime import timedelta

from app.services.device_manager import DeviceManager


class _DummyWebSocket:
    async def send_json(self, _payload):  # noqa: ANN001
        return None


def test_android_device_status_reports_guidance_and_last_seen() -> None:
    manager = DeviceManager()
    manager.register_device(
        "android-1",
        _DummyWebSocket(),  # type: ignore[arg-type]
        "android",
        {
            "hostname": "Pixel 9",
            "os_info": "Android 15",
            "capabilities": ["app_list", "shell_limited", "permission_status"],
        },
    )

    status = manager.get_device_status("android-1")

    assert status is not None
    assert status["status"] == "online"
    assert status["heartbeat_age_seconds"] >= 0
    assert status["last_seen"]
    assert status["capabilities"] == ["app_list", "shell_limited", "permission_status"]


def test_android_device_status_goes_offline_when_heartbeat_is_stale() -> None:
    manager = DeviceManager()
    manager.register_device(
        "android-1",
        _DummyWebSocket(),  # type: ignore[arg-type]
        "android",
        {
            "hostname": "Pixel 9",
            "os_info": "Android 15",
            "capabilities": ["app_list"],
        },
    )
    conn = manager._devices["android-1"]  # type: ignore[attr-defined]
    conn.info.last_heartbeat = manager._now() - timedelta(seconds=300)  # type: ignore[attr-defined]

    status = manager.get_device_status("android-1")

    assert status is not None
    assert status["status"] == "offline"
    assert "foreground service" in status["reconnect_guidance"]
