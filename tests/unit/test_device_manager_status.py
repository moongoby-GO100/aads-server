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


def test_stale_socket_close_does_not_unregister_replacement_connection() -> None:
    manager = DeviceManager()
    old_socket = _DummyWebSocket()
    replacement_socket = _DummyWebSocket()
    manager.register_device("android-1", old_socket, "android")  # type: ignore[arg-type]
    manager.register_device("android-1", replacement_socket, "android")  # type: ignore[arg-type]

    manager.unregister_device("android-1", old_socket)  # type: ignore[arg-type]

    assert manager.get_device("android-1") is not None
    assert manager._devices["android-1"].websocket is replacement_socket  # type: ignore[attr-defined]


async def test_explicit_missing_device_does_not_route_to_another_device() -> None:
    manager = DeviceManager()
    manager.register_device("android-online", _DummyWebSocket(), "android")  # type: ignore[arg-type]

    result = await manager.send_command("android-offline", "battery", {})

    assert result.status == "error"
    assert "android-offline" in str(result.data)
