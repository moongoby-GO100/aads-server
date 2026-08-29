from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ANDROID = ROOT / "android_agent" / "app" / "src" / "main"


def test_release_manifest_allows_voice_wake_foreground_service() -> None:
    manifest = (ANDROID / "AndroidManifest.xml").read_text(encoding="utf-8")

    assert "android.permission.RECORD_AUDIO" in manifest
    assert "android.permission.FOREGROUND_SERVICE_MICROPHONE" in manifest
    assert 'android:foregroundServiceType="dataSync|microphone"' in manifest


def test_release_manifest_exposes_ohvis_wake_deep_links_and_shortcut() -> None:
    manifest = (ANDROID / "AndroidManifest.xml").read_text(encoding="utf-8")
    shortcuts = (ANDROID / "res" / "xml" / "shortcuts.xml").read_text(encoding="utf-8")

    assert 'android:scheme="ohvis" android:host="wake"' in manifest
    assert 'android:scheme="aads-agent" android:host="wake"' in manifest
    assert 'android:name="android.app.shortcuts"' in manifest
    assert 'android:shortcutId="ohvis_wake"' in shortcuts
    assert 'android:data="ohvis://wake"' in shortcuts


def test_android_agent_exposes_voice_wake_commands() -> None:
    dispatcher = (
        ANDROID
        / "java"
        / "kr"
        / "newtalk"
        / "aads"
        / "agent"
        / "CommandDispatcher.java"
    ).read_text(encoding="utf-8")
    controller = (
        ANDROID
        / "java"
        / "kr"
        / "newtalk"
        / "aads"
        / "agent"
        / "VoiceWakeController.java"
    ).read_text(encoding="utf-8")

    assert 'dispatcher.register("voice_wake_start"' in dispatcher
    assert 'dispatcher.register("voice_wake_stop"' in dispatcher
    assert 'dispatcher.register("voice_wake_status"' in dispatcher
    assert 'SpeechRecognizer.createSpeechRecognizer' in controller
    assert '"오비스"' in controller


def test_backend_device_routing_allows_voice_wake_commands() -> None:
    manager = (ROOT / "app" / "services" / "pc_agent_manager.py").read_text(encoding="utf-8")

    assert '"voice_wake_start",' in manager
    assert '"voice_wake_stop",' in manager
    assert '"voice_wake_status",' in manager
    assert '"voice_wake_start": "voice_wake_start"' in manager
    assert '"voice_wake_stop": "voice_wake_stop"' in manager
    assert '"voice_wake_status": "voice_wake_status"' in manager
