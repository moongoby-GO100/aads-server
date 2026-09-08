# AADS/OHVIS Android Agent 전체 기능·통제 범위 감사 보고서

- 작성일: 2026-09-09 KST
- 대상: `android_agent/` 소스, 운영 배포 APK v1.1.1, AADS 디바이스 API·MCP 실행 경로
- 패키지: `kr.newtalk.aads.agent`
- 운영 APK SHA-256: `8cc57ec110a59c822208a8a437bdd183591c09129bffece8c1acd2299e85c877`
- 판정 기준: **코드에 존재한다는 사실과 현재 설치 APK에서 실제 실행 가능하다는 사실을 분리**한다.

## 1. 결론

Android Agent 소스에는 **62개 명령명, 41개 고유 핸들러**가 구현되어 있다. 배터리, 음성 안내, 진동, 볼륨, 제한 셸, 앱 조회·실행, 화면 탭·스와이프·텍스트 입력, 화면 읽기·캡처, 카메라, 위치, 문자, 연락처, 통화기록, 알림 읽기, 마이크 녹음, 기기 잠금·초기화까지 코드가 존재한다.

그러나 2026-09-09 운영 다운로드 APK의 release Manifest에는 네트워크·foreground service·알림·마이크·진동·부팅·배터리 최적화 관련 권한과 구성만 들어 있다. 접근성 서비스, 알림 리스너, 디바이스 관리자 receiver, 카메라·위치·SMS·연락처·미디어·Bluetooth·전체 앱 조회 권한은 release APK에 없다. 따라서 **현재 설치본에서 즉시 가능한 범위는 전체 소스 기능보다 크게 작다.**

특히 Agent는 연결 시 권한 상태와 관계없이 62개 명령을 모두 capability로 광고한다. 서버는 이를 “지원됨”으로 볼 수 있지만 실제 호출은 `permission denied`, `service not enabled`, 빈 결과 또는 Android 정책 차단으로 끝날 수 있다. 현재 가장 큰 문제는 기능 부족보다 **capability 광고와 실제 실행 가능 범위가 불일치하는 것**이다.

## 2. 실측 상태

| 항목 | 실측값 | 판정 |
|---|---:|---|
| 운영 버전 | 1.1.1 / versionCode 6 [운영 manifest·Gradle] | API manifest와 Gradle 일치 |
| 운영 APK 크기 | 1,084,798 bytes [운영 manifest·파일 stat] | 다운로드 가능 |
| 서버 파일과 운영 다운로드 SHA-256 | 동일 [SHA-256 실측] | 배포 파일 동일성 확인 |
| 등록 명령명 | 62개 [CommandDispatcher.java] | 별칭 포함 |
| 고유 핸들러 | 41개 [CommandDispatcher.java] | 실제 기능 단위 |
| 현재 연결 Android Agent | 0대 [device_list, 2026-09-09 06:17 KST] | 실기기 왕복 검증 불가 |
| Android pairing token | 총 31개 / 미폐기 21개 / 현재 미만료 2개 [DB 조회] | DB 실측 |
| 마지막 token 사용 | 2026-09-09 03:31:51 KST [DB 조회] | 설치·연결 이력은 존재 |
| release Manifest 권한 | 11개 [main Manifest] | 제한형 release |
| debug Manifest 권한 선언 | 22개(메인과 중복 제외 순증 19개) 및 고권한 컴포넌트 [debug Manifest] | 개발 빌드 전용 |

현재 연결 기기가 0대이므로 “폰에 실제 설치된 APK의 버전·권한 승인 상태·제조사 정책·배터리 제한·앱 목록”은 확인하지 못했다. 아래의 현재 가능 판정은 운영 APK, release 병합 보고서, 서버 라우팅 코드를 기준으로 한 정적·배포 파일 감사 결과다.

## 3. 설치·실행·연결 구조

### 3.1 앱 자체

- 앱 이름: 오비스
- 첫 화면: 앱 내부 WebView로 `https://aads.newtalk.kr/chat`을 연다.
- 세션 만료: 웹 로그인 화면을 거쳐 `/chat`으로 복귀한다.
- 설정: 일반 첫 화면을 막지 않고 `Show Settings` 안에 페어링·권한·연결 제어를 둔다.
- 최소 Android: API 26, Android 8.0 이상
- 대상 Android: API 35
- release 서명: 현재 `signingConfigs.debug`를 사용한다. 운영 전용 서명키가 아니다.
- cleartext HTTP: `usesCleartextTraffic=false`; 기본 서버는 WSS다.

### 3.2 설치·페어링

- 설치 페이지: `https://aads.newtalk.kr/api/v1/devices/android/install`
- 최신 APK: `https://aads.newtalk.kr/api/v1/devices/android/download-fresh`
- 표준 APK: `https://aads.newtalk.kr/api/v1/devices/android/download-standard`
- 수동 페어링: `aads-agent://pair?...` deep link 또는 JSON/전체 WebSocket URL 붙여넣기
- 최초 자동 등록: `POST /api/v1/devices/android/auto-register`
- 토큰: 서버 DB에는 SHA-256 해시, 폰에는 private `SharedPreferences`에 평문으로 저장한다.
- 페어링 만료: 첫 사용 전 만료시간을 적용하되, 한 번 성공하여 `last_used_at`이 생긴 토큰은 폐기 전까지 재연결에 계속 사용할 수 있다.

### 3.3 상시 연결과 복구

- WebSocket: `wss://aads.newtalk.kr/api/v1/devices/ws/{agent_id}?token=...&device_type=android`
- 앱 heartbeat: 25초마다 전송
- 서버 receive timeout: 50초
- 폰 watchdog: 마지막 heartbeat 120초 초과 시 재연결
- 재연결 backoff: 2, 5, 10, 20, 40, 60초
- foreground notification: 연결 상태, 실행 중 명령, 마지막 오류, voice wake 상태를 표시한다.
- 부팅 복구: `BOOT_COMPLETED`/`QUICKBOOT_POWERON` receiver가 service 시작을 시도한다.
- 네트워크 복구: 연결 복구 broadcast가 WebSocket 재접속을 유도한다.
- task 제거 복구: 2초 후 AlarmManager로 service 재시작을 예약한다.
- Android 12+/14+에서는 background foreground-service 시작과 카메라·위치·마이크 while-in-use 권한 제한 때문에 제조사/OS 상태에 따라 자동 복구가 차단될 수 있다.

## 4. 현재 운영 APK에서 실제 가능한 기능

상태 정의:

- **가능**: release Manifest와 코드상 즉시 사용 가능. 단, 사용자 승인이나 하드웨어가 필요한 경우 조건을 병기한다.
- **부분 가능**: Android 버전·foreground 상태·package visibility 등으로 결과가 제한된다.
- **현재 불가**: 소스 핸들러는 있으나 release Manifest/컴포넌트가 없어 설치본에서 권한을 받을 수 없다.
- **위험 기능**: 데이터 삭제·민감정보 접근 등 별도 승인·감사가 필요한 기능이다.

| 고유 기능 | 명령명/별칭 | 하는 일 | 현재 release 판정 | 필수 조건·한계 |
|---|---|---|---|---|
| 배터리 | `battery` | 잔량, 충전, 건강, 온도, 전압 | 가능 | 읽기 전용 |
| 권한 상태 | `permission_status`, `permissions` | 런타임·특수 권한 상태 | 가능 | 실제 manifest 미선언 권한은 false |
| 위치 | `location` | 최신 last-known 위치 | 현재 불가 | 위치 권한 미선언, 연속 추적 아님 |
| 카메라 | `camera`, `camera_photo` | 전/후면 JPEG 촬영 후 base64 반환 | 현재 불가 | CAMERA 및 camera FGS 미선언 |
| 알림 표시 | `notification`, `notification_send` | 폰 알림 생성 | 가능 | Android 13+ 사용자의 알림 승인 필요 |
| 클립보드 | `clipboard`, `clipboard_get`, `clipboard_set` | 클립보드 읽기/쓰기 | 부분 가능 | 최근 Android는 background 읽기를 제한; foreground/기본 IME가 아니면 빈 값 가능 |
| 진동 | `vibrate` | 1~5,000ms 진동 | 가능 | 진동 하드웨어 필요 |
| 음성 안내 | `tts`, `tts_speak` | 지정 문구 TTS 재생 | 가능 | 설치된 TTS 엔진·언어 필요 |
| 음량 | `volume`, `volume_set` | music/alarm/ring/notification/system/voice_call 조회·설정 | 가능 | UI를 표시하며 OEM 정책 영향 가능 |
| Wi-Fi 정보/스캔 | `wifi`, `wifi_info`, `wifi_scan` | SSID/BSSID/RSSI/주변 AP | 현재 불가 또는 오류 | release에 ACCESS_WIFI_STATE·위치·NEARBY_WIFI 없음 |
| 제한 셸 | `shell_limited`, `shell` | 허용된 진단 명령 실행 | 가능 | 임의 셸 아님; 출력 12,000 bytes, timeout 30초 제한 |
| 문자 발송 | `sms_send` | 지정 번호로 SMS 직접 발송 | 현재 불가·위험 | SEND_SMS 미선언; 활성화 시 별도 승인 필수 |
| 전화 걸기 화면 | `call_dial`, `call` | 전화번호가 채워진 dialer 열기 | 가능 | 실제 통화 버튼은 사용자가 누름; 직접 발신 아님 |
| 연락처 목록 | `contacts_list`, `contacts` | 이름·전화번호 조회 | 현재 불가·민감 | READ_CONTACTS 미선언 |
| 연락처 검색 | `contacts_search` | 이름 부분검색 | 현재 불가·민감 | READ_CONTACTS 미선언 |
| 통화기록 | `call_log` | 번호·수발신유형·시각·통화시간 | 현재 불가·민감 | READ_CALL_LOG 미선언 |
| 받은 문자 | `sms_inbox` | 발신번호·본문·시각 | 현재 불가·민감 | READ_SMS 미선언 |
| 사진 목록 | `photo_gallery`, `photos` | 사진 ID/경로/일시/크기 메타데이터 | 현재 불가·민감 | 미디어 권한 미선언; 이미지 bytes 다운로드 기능은 아님 |
| 앱 목록 | `app_list`, `apps` | package/label/version 목록 | 부분 가능 | QUERY_ALL_PACKAGES 미선언으로 Android 11+에서 보이는 앱만 반환 |
| 앱 실행 | `app_launch` | package의 launcher activity 실행 | 부분 가능 | **설치 앱 실행 코드 존재**. package visibility와 background activity start 제한 때문에 알려진 앱도 실패할 수 있음 |
| Bluetooth | `bluetooth_status`, `bluetooth` | on/off, 페어링 기기 조회 | 현재 불가 | BLUETOOTH_CONNECT/구버전 Bluetooth 권한 미선언; scan/connect 기능은 없음 |
| 화면 탭 | `screen_tap`, `tap` | 좌표 탭 | 현재 불가 | 접근성 서비스가 release Manifest에 없음 |
| 화면 스와이프 | `screen_swipe`, `swipe` | 좌표간 스와이프 | 현재 불가 | 접근성 서비스 필요 |
| 길게 누르기 | `screen_long_press`, `long_press` | 좌표 long press | 현재 불가 | 접근성 서비스 필요 |
| 화면 문자/UI 트리 | `screen_text` | 현재 창 텍스트, description, view ID, class, clickable | 현재 불가·민감 | 접근성 서비스 필요; 최대 1,000 node |
| 텍스트 입력 | `key_input` | 현재 focus input에 replace/append | 현재 불가 | 접근성 서비스 필요; 범용 키보드 이벤트가 아니라 focus node setText |
| 시스템 전역 동작 | `global_action` | 뒤로/홈/최근앱/알림창/빠른설정/전원대화상자/잠금/스크린샷 | 현재 불가 | 접근성 서비스 필요; Android 버전별 지원 차이 |
| 텍스트·ID 클릭 | `find_and_click` | 화면 text/view ID 검색 후 클릭 | 현재 불가 | 접근성 서비스 필요 |
| 화면 스크롤 | `screen_scroll` | 상하좌우 scroll action/gesture | 현재 불가 | 접근성 서비스 필요 |
| 센서 | `sensor_data`, `sensors` | 가속도·자이로·조도 1회 표본 | 가능 | 기기에 센서가 있어야 함; 연속 스트림 아님 |
| 다른 앱 알림 읽기 | `notification_read`, `notifications_read` | 활성/최근 알림 제목·본문·패키지 | 현재 불가·민감 | notification listener가 release Manifest에 없음 |
| 화면 캡처 | `screenshot` | 현재 화면 PNG base64 | 현재 불가·민감 | Android 11+ 접근성 screenshot 권한/서비스 필요 |
| 즉시 잠금 | `device_lock` | lock screen 전환 | 현재 불가·위험 | device admin receiver가 release에 없음 |
| 공장 초기화 | `device_wipe` | `WIPE_CONFIRMED` 후 wipeData | 현재 불가·최고위험 | receiver 미선언. MCP `device_command`는 명시 차단하지만 일반 execute 경로는 별도 차단 필요 |
| 관리자 상태 | `device_admin_status` | admin 활성 여부 | 가능 | 현재는 false가 정상 예상 |
| 화면 밝기 | `screen_brightness` | 밝기 읽기/0~255 쓰기 | 읽기만 가능 예상 | 쓰기는 WRITE_SETTINGS 미선언/특수 승인 없음 |
| 화면 꺼짐 시간 | `screen_timeout` | timeout 읽기/5초~30분 쓰기 | 읽기만 가능 예상 | 쓰기는 WRITE_SETTINGS 필요 |
| 음성 녹음 | `audio_record` | 1~60초 AAC/M4A 녹음 후 base64 반환 | 조건부 가능·민감 | 마이크 승인, foreground/while-in-use 정책 충족 필요; 임시파일은 반환 후 삭제 |
| 음성 깨우기 시작 | `voice_wake_start` | `오비스/ohvis/obis/aads` 인식 대기 | 조건부 가능 | 마이크 승인, foreground notification, 배터리 예외 권장 |
| 음성 깨우기 중지 | `voice_wake_stop` | 로컬 listener 중지 | 가능 | service 상태 영향 |
| 음성 깨우기 상태 | `voice_wake_status`, `wake_status` | 상태·마지막 인식·오류 | 가능 | 읽기 전용 |

## 5. 설치된 앱을 실행하고 조작할 수 있는가

### 답

**코드상 실행은 가능하다.** `app_launch`에 package name을 주면 Android launcher intent를 찾아 앱을 연다. 예를 들어 패키지 visibility가 허용된 앱이라면 `com.kakao.talk` 같은 package를 대상으로 실행을 시도할 수 있다.

하지만 현재 운영 APK에서는 다음 두 이유로 “모든 설치 앱을 안정적으로 열고 조작”할 수 없다.

1. `QUERY_ALL_PACKAGES`와 개별 `<queries>`가 release Manifest에 없어 Android 11+에서 앱 목록이 필터링된다. 목록에 안 보이는 앱은 launcher intent 조회도 실패할 수 있다.
2. 접근성 서비스가 release Manifest에 없어 앱을 연 뒤 화면 읽기, 버튼 찾기, 탭, 스와이프, 텍스트 입력을 할 수 없다.

또한 Android의 background activity start 정책 때문에 Agent가 background에 있을 때 앱을 전면으로 띄우는 동작이 차단될 수 있다. 사용자에게 알림을 보여 누르게 하는 복구 UX가 필요하다.

### 앱 실행 후 가능한 자동화 범위(접근성 보강 시)

- 현재 화면의 노출된 텍스트와 content description 읽기
- view ID 또는 텍스트로 버튼 찾고 클릭
- 좌표 탭, 길게 누르기, 스와이프, 상하좌우 스크롤
- focus된 입력창에 텍스트 입력/추가
- 뒤로, 홈, 최근 앱, 알림창, 빠른 설정 열기
- 현재 화면 캡처 후 원격 분석
- 화면 변화에 따라 여러 명령을 순차 실행하여 앱 workflow 자동화

### 그래도 자동화가 어려운 화면

- `FLAG_SECURE`가 적용된 금융·인증·DRM 화면
- 별도 보안 키패드, 생체인증, 인증서 PIN 입력 화면
- Canvas/OpenGL로 그려져 접근성 node가 없는 UI
- CAPTCHA, 기기 무결성/Play Integrity, 보안프로그램이 자동화를 탐지하는 화면
- 잠금화면에서 사용자 인증이 필요한 작업
- background 실행을 OEM이 강제 종료한 상태

## 6. 화면 전체를 어디까지 제어할 수 있는가

접근성 서비스가 정상 선언되고 사용자가 설정에서 켜면, Android가 허용하는 일반 앱 화면 전반에 대해 다음 제어가 가능하다.

- 픽셀 좌표 기반: tap, long press, swipe
- 의미 기반: visible text, content description, view ID로 탐색·클릭
- 입력 기반: 현재 focus input에 문자열 쓰기
- navigation 기반: back/home/recents/notification shade/quick settings/power dialog
- 시각 기반: Android 11+ 화면 screenshot
- 구조 기반: 최대 1,000 UI node의 text/class/id/clickable 읽기

현재 `onAccessibilityEvent()`는 비어 있어 모든 터치·타이핑 이벤트를 지속적으로 수집하는 keylogger 형태는 구현되어 있지 않다. 호출 시점의 현재 화면을 읽고 동작하는 command-response 구조다.

## 7. 폰 하드웨어·데이터별 통제 범위

| 영역 | 소스에 구현된 범위 | 구현되지 않은 범위 |
|---|---|---|
| 화면 | tap/swipe/long press/read/input/click/scroll/screenshot/global action | 실시간 영상 stream, 멀티터치 조합, 회전·해상도 강제 변경 |
| 카메라 | 1장 JPEG 촬영, 기본 최대 640×480 | 동영상 촬영, 연속 camera stream, 줌·초점 정밀 제어 |
| 마이크 | 최대 60초 AAC/M4A 녹음, wake phrase, TTS | 실시간 양방향 음성 stream, 통화 녹음 |
| 위치 | last-known 위치 | 실시간 지속 추적, geofence, background location 이력 |
| 센서 | 가속도·자이로·조도 단발 측정 | GPS 외 연속 센서 stream, 건강/신체 센서 |
| 전화 | dialer 열기, 통화기록 읽기 코드 | 자동 발신 확정, 통화 수신/거절/종료, 통화음성 캡처 |
| SMS | 전송 및 inbox 읽기 코드 | MMS/RCS, 삭제/수정, 기본 SMS 앱 역할 |
| 연락처 | 목록·이름검색 | 생성·수정·삭제 |
| 사진 | 최근 사진 메타데이터 | 사진 파일 다운로드·업로드·삭제·촬영앨범 관리 |
| 알림 | 로컬 알림 표시, 다른 앱 알림 내용 읽기 코드 | 알림 action reply/click/dismiss 제어 |
| 앱 | 필터된 목록, known package 실행 | 설치/삭제/업데이트(Agent WS), 강제종료, 앱 데이터 읽기 |
| Wi-Fi | 현재 연결 정보·scan 결과 코드 | Wi-Fi on/off, AP 연결/비밀번호 설정 |
| Bluetooth | 상태와 이미 paired 기기 조회 | scan, pair, connect, 파일전송, on/off |
| 시스템 설정 | 볼륨, 밝기, 화면 timeout | arbitrary secure settings, 개발자옵션, VPN, 계정 관리 |
| 기기 정책 | 잠금·초기화 코드 | password reset은 XML 후보에만 있고 handler 없음; kiosk/앱허용목록 없음 |
| 셸 | 매우 좁은 읽기 allowlist | 임의 명령, 파이프/리다이렉션, 파일 쓰기, root, 패키지 조작 |

## 8. 제한 셸에서 정확히 허용되는 명령

`shell_limited`는 `/bin/sh -c`를 사용하지 않고 `ProcessBuilder(tokens)`로 실행한다. 세미콜론, 파이프, ampersand, backtick, `$`, `<`, `>`, 줄바꿈을 거부한다.

허용 목록:

- `getprop [property]`
- `settings get secure|system|global KEY`
- `dumpsys battery`
- `dumpsys wifi`
- `dumpsys connectivity`
- `dumpsys power`
- `pm list packages`
- `pm list packages -3`
- `id`
- `uname`
- `uname -a`

즉, APK 설치·삭제, 파일 읽기/쓰기, `am start`, `su`, 네트워크 도구, 임의 셸 script는 실행할 수 없다.

## 9. 서버 도구별 실제 노출 범위

### `device_command` 안전 래퍼

허용: screenshot, install_apk, list_apps, tap, swipe, input_text, get_device_info, voice_wake_start/stop/status.

- 연결 Agent가 있으면 WebSocket 명령으로 변환한다.
- Agent가 없으면 일부 명령은 ADB fallback을 시도한다.
- `device_wipe`, `factory_reset`, `root`, recovery/bootloader reboot는 blacklist다.
- 현재 AADS 서버에는 `adb` binary가 없고 연결 기기도 없어 ADB fallback은 사용할 수 없다.

### `device_execute`/REST `/devices/execute`

연결된 Android Agent에 `command_type`을 직접 전달할 수 있어 dispatcher에 등록된 62개 이름을 호출할 수 있다. 이 경로는 기능상 강력하지만, 민감·파괴 명령에 대한 command별 승인·RBAC·이중확인 정책을 별도로 강제해야 한다.

## 10. debug/full-permission 빌드에서 추가로 가능한 범위

debug Manifest에는 다음이 추가되어 있다.

- CAMERA, 위치, 근처 Wi-Fi, Wi-Fi 상태/변경
- SEND_SMS, READ_SMS, READ_CONTACTS, READ_CALL_LOG
- READ_EXTERNAL_STORAGE/READ_MEDIA_IMAGES
- BLUETOOTH/ADMIN/CONNECT
- QUERY_ALL_PACKAGES
- WRITE_SETTINGS
- camera/location/microphone foreground-service type
- AccessibilityService
- NotificationListenerService
- DeviceAdminReceiver(force-lock, wipe-data)

이 구성을 release에 안전하게 이식하고 사용자가 런타임·특수 권한을 승인하면 41개 핸들러 대부분을 사용할 수 있다. 단, Android 정책상 접근성, 알림 접근, device admin, 시스템 설정 쓰기, battery optimization 예외는 일반 runtime permission dialog만으로 자동 승인할 수 없고 사용자가 Settings 화면에서 직접 켜야 한다.

## 11. Device Owner·ADB·root까지 포함한 최대 확장 가능성

이 절은 “현재 구현됨”이 아니라 적법하게 소유·관리하는 기기에서 선택할 수 있는 확장 tier다.

### Tier C — Android Enterprise Device Owner/DPC

기기를 초기 프로비저닝하여 Agent를 Device Policy Controller로 만들면 일반 device admin보다 넓게 다음을 구현할 수 있다.

- kiosk/lock task mode, 허용 앱 목록
- 일부 런타임 권한 정책적 grant/deny
- managed configuration, 업무 계정·인증서·VPN·네트워크 정책
- 앱 배포/업데이트/차단 정책
- 원격 잠금·wipe 정책 강화
- 보안정책·암호정책·감사 이벤트

현재 APK는 DPC/Device Owner provisioning 코드가 없으므로 별도 제품 모드가 필요하다.

### Tier D — 사용자 승인 ADB

AADS 서버 코드에는 ADB backend로 다음 7개가 구현되어 있다.

- APK 설치/교체(`adb install -r`, 선택적으로 `-g`)
- screenshot
- 앱 package 목록
- tap, swipe, text input
- 제조사·모델·Android 버전·SDK·Android ID·kernel·화면 크기/밀도 조회

ADB는 USB 디버깅 또는 무선 디버깅에서 해당 PC/server RSA 키를 사용자가 승인해야 한다. 현재 서버는 `adb` 미설치라 즉시 사용할 수 없다.

### Tier E — root/system app/OEM Knox 등

root, platform signing, privileged app, Samsung Knox/EMM API가 별도 확보되면 일반 sandbox 밖 파일·설정·프로세스·패키지·input 제어 범위를 크게 늘릴 수 있다. 그러나 현재 AADS APK·서버에는 이러한 권한 획득·관리·감사 체계가 구현되어 있지 않다.

root가 있어도 하드웨어-backed Keystore/StrongBox, 잠금 전 credential-encrypted data, 생체인증 비밀, 서버측 은행 인증, DRM/TEE 데이터가 자동으로 해독되는 것은 아니다. “폰 root = 모든 비밀을 평문으로 읽음”은 성립하지 않는다.

## 12. 현재 구현되지 않았거나 구조적으로 제한되는 기능

- 임의 파일 탐색/업로드/다운로드
- 다른 앱 private data(`/data/data/...`) 읽기
- 저장된 비밀번호·생체정보·인증서 private key 추출
- PIN/패턴/지문/얼굴 인증 우회
- 자동 통화 수신·거절·종료·통화 녹음
- 원격 video recording/live camera stream/live screen stream
- 알림 reply/click/dismiss
- 앱 강제종료, cache/data 삭제, uninstall/update
- Wi-Fi/Bluetooth 자동 연결과 credential 관리
- VPN/계정/인증서/MDM 정책 관리
- 전원 끄기, 정상 reboot, bootloader/recovery 진입
- 상시 keylogging 또는 모든 accessibility event 영구 저장
- offline 명령 queue·재실행, command idempotency ledger
- screenshot/audio/contact/SMS 응답의 field-level 암호화
- 여러 사용자의 승인 workflow와 위험 명령 2인 승인

## 13. 보안·프라이버시 감사

| 등급 | 문제 | 영향 | 현재 근거 |
|---|---|---|---|
| P0 | capability 62개 과대 광고 | 서버가 실행 가능으로 오판 | dispatcher 목록을 그대로 register |
| P0 | `device_wipe`가 일반 dispatcher/execute 경로에 존재 | full manifest 활성화 후 원격 공장초기화 위험 | 문자열 확인 1회만 요구 |
| P0 | 자동 등록 API가 인증 없이 token 발급 | 임의 기기 등록·미소유 token 생성 가능 | endpoint에 auth dependency 없음 |
| P0 | auto-register token에 user/tenant owner 없음 | 사용자 격리와 device ownership 약화 | DB insert에 owner 미기록 |
| P1 | release가 debug key로 서명 | 공급망·업데이트 신뢰성 저하 | Gradle `signingConfigs.debug` |
| P1 | token을 폰 private prefs에 평문 저장 | root/backup/debug 유출 시 재접속 가능 | SharedPreferences 직접 저장 |
| P1 | token이 WebSocket query string에 포함 | proxy/access log 노출 가능성 | URL query auth |
| P1 | token 첫 성공 후 사실상 장기 유효 | 탈취 token의 지속 위험 | `last_used_at IS NOT NULL`이면 expiry 통과 |
| P1 | application-level E2E 암호화 없음 | TLS 종단 서버는 민감 데이터 열람 가능 | JSON/base64 WebSocket 응답 |
| P1 | 앱 package visibility 정책 미정 | 앱 목록/실행 불안정 또는 과도권한 유혹 | QUERY_ALL_PACKAGES release 누락 |
| P1 | release와 debug Manifest가 크게 다름 | 테스트 통과가 운영 capability를 보증하지 못함 | 고권한 항목이 debug에만 존재 |
| P2 | no certificate pinning | 신뢰 저장소가 손상된 기기에서 MITM 위험 증가 | OkHttp 기본 TLS만 사용 |
| P2 | 실행 명령의 폰측 영구 감사원장 없음 | 사후 누가 무엇을 했는지 확인 곤란 | command-response 후 로컬 기록 없음 |

보호 요소도 있다.

- cleartext traffic 비활성화, 기본 WSS 사용
- token은 서버 DB에 해시로 저장
- agent_id/device_type/token을 함께 검증
- 폐기된 token 거부
- 사용자 owner scope 검사와 internal admin 구분
- 위험한 `device_command` 래퍼에서 wipe/root/recovery 명령 차단
- shell metacharacter 차단 및 엄격 allowlist
- audio 임시파일 삭제
- 앱 backup 비활성화(`allowBackup=false`)

## 14. 기능을 안전하게 완성하는 권장 설계

### P0 — 실제 가능 capability만 광고

register payload를 다음처럼 분리해야 한다.

- `implemented_capabilities`: APK 코드에 있는 기능
- `declared_capabilities`: 현재 Manifest에 선언된 기능
- `granted_capabilities`: runtime/special permission까지 준비된 기능
- `temporarily_available`: foreground, unlock, network, battery 상태까지 현재 실행 가능한 기능
- `blocked_reasons`: permission/service/OS/OEM/foreground 사유

서버는 `granted_capabilities`만 자동 실행하고, 나머지는 사용자의 다음 행동이 있는 복구 흐름으로 연결해야 한다.

### P0 — 위험 명령 별도 control plane

- `device_wipe`는 일반 dispatcher와 REST execute에서 제거하거나 서버 feature flag 기본 OFF
- CEO 재인증 + 대상 device 확인 + 2단계 challenge + 2인 승인
- 최소 5분 취소 유예와 별도 out-of-band 알림
- command allowlist를 role/user/device별로 관리
- SMS, 연락처, 통화기록, 알림, screenshot/audio도 민감 등급 적용

### P0 — 등록·소유권 강화

- auto-register를 로그인된 one-time enrollment QR 또는 admin-issued ticket로 제한
- token에 user_id, tenant_id, device hardware binding, scopes, expiry를 필수화
- token rotation·즉시 revoke·last IP/device fingerprint·재인증
- query token 대신 짧은 bootstrap 후 Authorization/subprotocol 기반 session token 검토

### P1 — release Manifest/빌드 계약 수정

- `src/debug`에만 있는 기능을 운영 정책에 맞는 `src/fullControl` productFlavor로 분리
- 일반 사용자용 `standard`와 관리기기용 `managed` APK를 분리
- release 전용 signing key/keystore와 서명 fingerprint 검증
- 빌드 테스트가 **merged release Manifest**와 APK를 직접 검사하도록 변경
- 권한 없는 명령을 capability에서 제거하는 instrumentation test 추가

### P1 — 앱 실행·UI 자동화 안정화

- 무제한 QUERY_ALL_PACKAGES 대신 자동화 대상 package allowlist와 `<queries>` 우선
- background app launch 실패 시 고정 알림의 “앱 열기” action으로 복구
- 화면 캡처 + UI tree + action 결과를 한 transaction ID로 묶기
- action 전후 화면 hash와 expected condition을 검증하여 오동작 방지
- 금융/인증 앱은 허용목록이 아니라 기본 차단목록에서 시작

### P1 — 감사·보호

- command_id, actor, device, command, 승인, 시작/완료, 결과 hash, 민감도, 실패 사유를 append-only 저장
- screenshot/audio/SMS/contact는 원문을 기본 로그에서 제거하고 TTL 적용
- Android Keystore 기반 token 암호화
- 앱 내 “현재 원격 제어 중” 표시와 즉시 연결 끊기/권한 철회 버튼
- 서버가 offline이면 마지막 command를 자동 재실행하지 않도록 nonce/idempotency 계약

### P2 — 고급 기능은 별도 모듈

- live screen/camera/audio는 command JSON base64가 아니라 WebRTC 또는 별도 encrypted media channel
- MDM/Device Owner는 일반 개인폰 APK와 분리
- Samsung 기기는 Knox Managed mode를 별도 adapter로 제공
- 장시간 작업은 WorkManager/foreground service 정책과 사용자-visible 상태를 결합

## 15. 권장 제품 모드

| 모드 | 대상 | 권한 | 주요 기능 | 금지 |
|---|---|---|---|---|
| Standard | 일반 개인폰 | 알림·마이크·진동·네트워크 | OHVIS Chat, TTS, wake, 상태 확인 | 화면/개인데이터/잠금/wipe |
| Assisted Control | 사용자가 화면을 보고 승인 | 접근성·선별 package visibility | 앱 열기, 화면 읽기, 탭/입력, screenshot | 무인 금융·위험 명령 |
| Managed Device | 회사 소유 전용폰 | Device Owner/DPC | kiosk, 앱배포, 정책, 원격지원 | 개인 데이터 혼용 |
| Lab/ADB | 개발·QA 기기 | USB/무선 디버깅 | 설치, screenshot, 입력, 회귀테스트 | 운영 개인정보 처리 |

## 16. 실기기 검증 체크리스트

현재 연결 기기가 생기면 아래를 순서대로 확인해야 “실제 사용 가능”으로 판정할 수 있다.

1. `device_list(android)`에서 agent 1대와 version 1.1.1, 25~50초 내 heartbeat 확인
2. `permission_status` 저장: runtime/special permission별 실제 결과
3. 안전한 읽기 명령: battery, sensor_data, voice_wake_status
4. 사용자-visible 명령: notification, vibrate, tts, volume
5. 앱 목록에서 기대 package 존재 여부 및 `app_launch` 전면 전환 확인
6. 접근성 활성화 가능 여부, screen_text → screenshot → tap → input → back 순 검증
7. 카메라/위치/사진/연락처/SMS/통화기록은 별도 consent 후 최소 샘플로 검증
8. 화면 잠금은 전용 테스트 기기에서만 검증
9. `device_wipe`는 운영·개인폰에서 절대 테스트하지 않음
10. network off/on, 앱 swipe-away, 재부팅 후 자동 재연결과 foreground notification 확인
11. Samsung battery optimization/절전 앱 예외 확인
12. 모든 테스트 command의 서버 감사로그와 폰 표시가 일치하는지 확인

## 17. 완료 기준

Android Agent를 “폰 전반 제어 가능”으로 보고하려면 최소한 다음이 모두 충족되어야 한다.

- 운영 merged Manifest와 APK 서명·hash가 승인된 release 계약과 일치
- 실제 연결 기기의 granted capability가 서버에 동적으로 광고
- 앱 실행, 화면 읽기, screenshot, tap/input의 실제 왕복 E2E 성공
- 위험 명령은 일반 실행 경로에서 차단되고 별도 승인 경로만 존재
- enrollment가 로그인 사용자/tenant/device에 귀속
- Standard/Assisted/Managed 모드가 APK 및 서버 정책으로 분리
- 재부팅·네트워크 복구·세션 만료·권한 철회 시 사용자 복구 경로 확인
- command 감사원장과 민감 데이터 TTL/마스킹 확인

## 18. 근거 파일

- `android_agent/app/src/main/java/kr/newtalk/aads/agent/CommandDispatcher.java`
- `android_agent/app/src/main/java/kr/newtalk/aads/agent/AndroidCommandHandlers.java`
- `android_agent/app/src/main/java/kr/newtalk/aads/agent/AadsAccessibilityService.java`
- `android_agent/app/src/main/java/kr/newtalk/aads/agent/AadsNotificationListener.java`
- `android_agent/app/src/main/java/kr/newtalk/aads/agent/AadsForegroundService.java`
- `android_agent/app/src/main/java/kr/newtalk/aads/agent/AadsWebSocketClient.java`
- `android_agent/app/src/main/java/kr/newtalk/aads/agent/AgentPrefs.java`
- `android_agent/app/src/main/java/kr/newtalk/aads/agent/AutoRegisterClient.java`
- `android_agent/app/src/main/AndroidManifest.xml`
- `android_agent/app/src/debug/AndroidManifest.xml`
- `android_agent/app/build/outputs/logs/manifest-merger-release-report.txt`
- `app/api/device.py`
- `app/services/device_manager.py`
- `app/services/pc_agent_manager.py`
- `app/services/tool_executor.py`

Android 플랫폼 제약 근거(2026-09-09 KST 재확인, 공식 문서의 표시 갱신일 2026-09-01 UTC):

- AccessibilityService screenshot/gesture/UI tree: <https://developer.android.com/reference/android/accessibilityservice/AccessibilityService>
- 접근성 서비스 선언과 gesture 요건: <https://developer.android.com/guide/topics/ui/accessibility/service>
- Android 11+ package visibility: <https://developer.android.com/training/package-visibility/declaring>
- background foreground-service/while-in-use 제한: <https://developer.android.com/develop/background-work/services/fgs/restrictions-bg-start>
- DevicePolicyManager lock/wipe: <https://developer.android.com/reference/android/app/admin/DevicePolicyManager>

## 19. 디스크 정리 결과

Android 분석과 별도로 AADS 서버 디스크를 안전 정리했다.

| 항목 | 정리 전 | 정리 후 |
|---|---:|---:|
| 루트 사용량 | 176GB / 193GB [df] | 164GB / 193GB [df] |
| 여유 공간 | 17GB [df] | 30GB [df] |
| 사용률 | 92% [df] | 85% [df] |

2026-09-09 06:19:06 KST 재확인값은 160GB 사용, 33GB 여유, 사용률 84%다 [df]. 정리 직후보다 4GB가 더 줄어든 것은 후속 이미지/빌드 상태 변화가 반영된 현재 파일시스템 실측이며, 이번 정리로 삭제했다고 산정한 회수량에는 포함하지 않는다.

삭제한 것은 실행 중 컨테이너가 참조하지 않는 구형 AADS release 이미지 4개뿐이다.

- API: `aads-server:91acd716ab47`, `aads-server:69d5bce9b603`, `aads-server:eec9fdc43f78`
- Dashboard: `aads-dashboard:4c9f0b1f00fa`

보존:

- 현재 API `aads-server:582fb94fcfe1` 양 슬롯
- 직전 API rollback image `aads-server:0d34bdd23bfa`
- 현재 Dashboard `aads-dashboard:ca94ced3bf1c` 양 슬롯
- 직전 Dashboard `aads-dashboard:ca94ced`/`local`
- 실행 중 test/service 이미지, 모든 container, DB/Redis volume
- 최근 24시간 build cache

BuildKit 23.66GB는 35.76MB만 reclaimable로 표시되어 prune 결과 0B였다. 이미지 삭제로 약 12GB의 실제 파일시스템 공간을 회수했다. 삭제 이미지는 해당 Git SHA에서 재빌드할 수 있다.
