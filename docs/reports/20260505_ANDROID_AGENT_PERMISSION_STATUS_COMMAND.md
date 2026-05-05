# Android Agent Permission Status Command

_작성: 2026-05-05 10:46 KST_

## 목적

Galaxy Z Fold6 Android Agent에서 이미 승인된 권한이 실제로 유지되고 있는지 서버 명령으로 확인할 수 있도록 `permission_status` 명령을 추가했다.

## 명령

```json
{
  "type": "permission_status",
  "params": {}
}
```

별칭:

```json
{
  "type": "permissions",
  "params": {}
}
```

## 응답 필드

- `runtime_permissions`: Android 런타임 권한별 승인 상태 배열
- `special_permissions`: 설정 화면에서 직접 켜야 하는 특수 권한 상태
- `all_runtime_granted`: 런타임 권한 전체 승인 여부
- `all_special_ready`: 특수 권한 전체 준비 여부
- `sdk_int`: 단말 Android SDK 버전
- `package`: 에이전트 패키지명

## 확인 대상

런타임 권한:

- Camera
- SMS 발송/읽기
- 연락처 읽기
- 통화기록 읽기
- 마이크 녹음
- 위치
- Android 13 이상 알림/근처 Wi-Fi/미디어 이미지
- Android 12 이상 Bluetooth connect

특수 권한:

- 접근성 서비스 활성화
- 접근성 서비스 런타임 연결 상태
- 알림 접근 권한
- 디바이스 관리자 활성화
- 시스템 설정 쓰기(`WRITE_SETTINGS`)
- 배터리 최적화 예외

## 검증

- `./build_debug_apk.sh` 성공
- `android_agent/dist/aads-agent-debug.apk`: 1,410,347 bytes, 2026-05-05 10:49 KST 생성
- `CommandDispatcher` 등록 수: 58개

## 운영 메모

일반 런타임 권한은 앱 삭제/재설치, 사용자의 권한 취소, OS 자동 권한 회수, 신규 권한 추가가 없으면 매번 다시 승인할 필요가 없다. 접근성, 알림 접근, 디바이스 관리자, `WRITE_SETTINGS`, 배터리 최적화 예외는 Android 정책상 설치 시 자동 전체 승인 대상이 아니며, 본 명령으로 현재 상태를 원격 확인한다.
