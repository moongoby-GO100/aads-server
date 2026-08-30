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

    assert 'android:label="@string/app_name"' in manifest
    assert 'android:icon="@drawable/ic_ohvis_launcher"' in manifest
    assert 'android:roundIcon="@drawable/ic_ohvis_launcher"' in manifest
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
    assert "ERROR_NO_MATCH" in controller
    assert "ERROR_SPEECH_TIMEOUT" in controller
    assert "resetRecognizer()" in controller


def test_android_agent_embeds_ohvis_web_app() -> None:
    activity = (
        ANDROID
        / "java"
        / "kr"
        / "newtalk"
        / "aads"
        / "agent"
        / "MainActivity.java"
    ).read_text(encoding="utf-8")

    assert "android.webkit.WebView" in activity
    assert 'OHVIS_CHAT_URL = OHVIS_HOME_URL + "/chat"' in activity
    assert 'openOhvisWeb(OHVIS_CHAT_URL, "launch")' in activity
    assert 'button("Open Chat"' in activity
    assert 'button("Show Settings"' in activity
    assert "openOhvisWeb(resolveOhvisUrl(dataUri)" in activity
    assert "setJavaScriptEnabled(true)" in activity
    assert "setDomStorageEnabled(true)" in activity


def test_backend_device_routing_allows_voice_wake_commands() -> None:
    manager = (ROOT / "app" / "services" / "pc_agent_manager.py").read_text(encoding="utf-8")

    assert '"voice_wake_start",' in manager
    assert '"voice_wake_stop",' in manager
    assert '"voice_wake_status",' in manager
    assert '"voice_wake_start": "voice_wake_start"' in manager
    assert '"voice_wake_stop": "voice_wake_stop"' in manager
    assert '"voice_wake_status": "voice_wake_status"' in manager


def test_android_manifest_api_reads_version_from_gradle() -> None:
    device_api = (ROOT / "app" / "api" / "device.py").read_text(encoding="utf-8")
    gradle = (ROOT / "android_agent" / "app" / "build.gradle").read_text(encoding="utf-8")

    assert "def _android_build_metadata()" in device_api
    assert '"version": build_metadata["version"]' in device_api
    assert '"version_code": build_metadata["version_code"]' in device_api
    assert '"name": "오비스"' in device_api
    assert '"voice_wake_deep_links": ["ohvis://wake", "aads-agent://wake"]' in device_api
    assert '"ohvis_web_url": "https://aads.newtalk.kr/chat"' in device_api
    assert '"embedded_ohvis_webview": True' in device_api
    assert '"launch_route": "/chat"' in device_api
    assert '"admin_settings_route": "/ops/mobile-agent"' in device_api
    assert '"bixby_quick_command": "Open OHVIS with ohvis://wake"' in device_api
    assert '"voice_wake_capabilities": [' in device_api
    assert 'versionName "0.1.4"' in gradle
    assert "versionCode 5" in gradle
