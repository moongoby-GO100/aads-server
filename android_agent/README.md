# AADS Android Agent

Native Android agent for the AADS device WebSocket protocol. The existing
`mobile_agent/` Termux prototype remains unchanged; this project is a separate
APK-oriented implementation under `android_agent/`.

## Project

- Package: `kr.newtalk.aads.agent`
- App label: `오비스`
- Min SDK: 26
- Target SDK: 35
- Language: Java
- WebSocket client: OkHttp

## Pairing

Server-side install helpers are exposed by AADS:

- Install page: `https://aads.newtalk.kr/api/v1/devices/android/install`
- APK download: `https://aads.newtalk.kr/api/v1/devices/android/download`
- Source ZIP fallback: `https://aads.newtalk.kr/api/v1/devices/android/source.zip`
- Manifest: `https://aads.newtalk.kr/api/v1/devices/android/manifest`
- Auto-register fallback: `https://aads.newtalk.kr/api/v1/devices/android/auto-register`

Create a per-device pairing payload from an authenticated admin session:

```bash
curl -X POST https://aads.newtalk.kr/api/v1/devices/android/pairing \
  -H "Authorization: Bearer $AADS_ADMIN_JWT" \
  -H "Content-Type: application/json" \
  -d '{"label":"CEO phone","expires_hours":24}'
```

The response contains a one-time-visible `pairing_payload`, `full_ws_url`, and
the dashboard can open an `aads-agent://pair?payload=...` deep link. On Android
devices with the APK installed, that link saves the pairing values and starts
the foreground service automatically. Paste values manually only when the device
blocks the deep link.

## Voice Wake and Bixby Entry

The APK exposes a P1 voice wake path for Samsung/Android devices:

- Bixby Quick Command target: `ohvis://wake`
- Compatibility deep link: `aads-agent://wake`
- Static Android shortcut ID: `ohvis_wake`
- Remote command capabilities: `voice_wake_start`, `voice_wake_stop`,
  `voice_wake_status`

Use Bixby Quick Command to launch the `ohvis://wake` link or open the `오비스`
app. The deep link starts the foreground service and enables the voice wake
listener when microphone permission is already granted. The local listener uses
Android `SpeechRecognizer` inside the existing foreground service and watches
for `오비스`, `ohvis`, `obis`, or `aads`. When a wake phrase is detected, it
brings `MainActivity` to the foreground through `ohvis://wake?source=voice`.

This is not an OS-level hotword replacement for Bixby. Android background
microphone rules still apply: the user must grant microphone permission, keep
the foreground notification visible, and allow battery optimization exemption
when long-running listening is required.

On first launch without a deep link:

1. Confirm or edit the server URL. Default:
   `wss://aads.newtalk.kr/api/v1/devices/ws`
2. Use the generated `agent_id` or paste the server-issued value.
3. Enter the pairing token manually or paste the full pairing payload.
4. Optionally paste a QR payload into the QR input hook.
5. Save pairing and start the foreground service.

Accepted QR/manual hook formats:

```json
{"server_url":"wss://aads.newtalk.kr/api/v1/devices/ws","agent_id":"android001","token":"..."}
```

or a full WebSocket URL:

```text
wss://aads.newtalk.kr/api/v1/devices/ws/android001?token=...&device_type=android
```

The token is not hardcoded. It is saved in app private SharedPreferences as the
fallback storage path for devices where Jetpack Security is not included. Pairing
expiration limits first use; after a successful WebSocket registration, reconnect
uses the same agent-bound token so normal network or reboot recovery does not
break when the original pairing window has passed.

## Protocol

The service connects to:

```text
wss://aads.newtalk.kr/api/v1/devices/ws/{agent_id}?token=...&device_type=android
```

Initial message:

```json
{"type":"register","id":"...","payload":{"agent_id":"...","device_type":"android","capabilities":[]}}
```

Runtime messages:

- `heartbeat`: sent every 25 seconds; server heartbeat replies update the UI.
- `command`: routed by `payload.command_type`.
- `result`: returned with the same message `id`.

## Commands

Primary Android handlers:

- `battery`
- `location`
- `camera`
- `notification`
- `clipboard`
- `vibrate`
- `tts`
- `volume`
- `wifi`
- `shell_limited`
- `sms_send`
- `call_dial`
- `voice_wake_start`
- `voice_wake_stop`
- `voice_wake_status`

Compatibility aliases are also exposed for several Termux-style command names:
`camera_photo`, `notification_send`, `clipboard_get`, `clipboard_set`,
`tts_speak`, `volume_set`, `wifi_info`, `wifi_scan`, `shell`, and `call`.

`shell_limited` never uses a full shell. It runs only allowlisted commands
through `ProcessBuilder`: `getprop`, `settings get`, selected `dumpsys`
targets, `pm list packages`, `id`, and `uname`.

## Permissions

The app requests sensitive permissions at runtime from the main screen. Command
handlers check permission again before execution and return an error result when
permission is missing.

- Android 13+: press `Notifications` before using notification commands or the
  foreground notification permission prompt.
- Location commands require fine or coarse location permission.
- Wi-Fi scans require location and, on Android 13+, nearby Wi-Fi permission.
- Camera capture requires camera permission.
- Voice wake and `audio_record` require microphone permission. Release builds
  declare `RECORD_AUDIO` and `FOREGROUND_SERVICE_MICROPHONE`; debug builds keep
  the same service type through manifest merge.
- SMS sending requires SMS permission.
- `call_dial` opens the system dialer and leaves the final call action visible
  to the user.

## Battery Optimization

For long-running background connectivity, open `Battery Settings` in the app and
allow an optimization exception for the AADS Agent package when the device policy
permits it.

## Local Verification

From this directory, the expected debug APK command is:

```bash
./build_debug_apk.sh
```

If no Gradle wrapper is available, use an installed Gradle compatible with
Android Gradle Plugin 8.6.1.

The build script copies the debug APK to:

```text
android_agent/dist/aads-agent-debug.apk
```
