# AADS Android Agent

Native Android agent for the AADS device WebSocket protocol. The existing
`mobile_agent/` Termux prototype remains unchanged; this project is a separate
APK-oriented implementation under `android_agent/`.

## Project

- Package: `kr.newtalk.aads.agent`
- Min SDK: 26
- Target SDK: 35
- Language: Java
- WebSocket client: OkHttp

## Pairing

On first launch:

1. Confirm or edit the server URL. Default:
   `wss://aads.newtalk.kr/api/v1/devices/ws`
2. Use the generated `agent_id` or regenerate it.
3. Enter the pairing token manually.
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
fallback storage path for devices where Jetpack Security is not included.

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
./gradlew :app:assembleDebug
```

If no Gradle wrapper is available, use an installed Gradle compatible with
Android Gradle Plugin 8.6.1.
