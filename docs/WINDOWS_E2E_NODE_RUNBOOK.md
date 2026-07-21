# AADS Windows E2E Node Runbook

## 목적

CEO 개인 PC의 전원·로그온 상태와 무관하게 Windows 전용 E2E를 수행하는 독립 노드를 구성합니다. 일반 웹 검증은 서버 Headless를 우선 폴백으로 사용하고, Windows 앱·PowerShell·로컬 Chrome 검증만 `windows_e2e` capability 노드로 라우팅합니다.

## 준비 조건

- 별도 Windows 11 VM 또는 물리 PC 1대
- 상시 전원, 자동 로그인 또는 사용자 로그온 상태에서 실행 가능한 작업 스케줄러 정책
- AADS 대시보드에서 발급한 PC Agent 토큰
- 최신 `kakaobot-setup.exe`

## 설치

관리자 PowerShell에서 실행합니다. 토큰은 명령행 인자로 받지 않고 보안 프롬프트에서 입력합니다.

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install_windows_e2e_node.ps1 -LauncherPath C:\AADS\kakaobot-setup.exe
```

설치 스크립트는 `%LOCALAPPDATA%\KakaoBot\config.json`에 `node_role=windows_e2e`를 설정하고 `AADSWindowsE2ENode` 시작 작업을 등록합니다.

## 검증

```text
GET /api/v1/pc-agent/e2e-node/status
GET /api/v1/pc-agent/diagnostics
```

완료 기준은 `ready=true`, `online_count>=1`, 해당 노드의 `capabilities`에 `windows_e2e`, `interactive_browser`, `pc_control`이 모두 표시되는 것입니다. `diagnostics`의 최신 launcher 상태에서 `watchdog_task.registered=true`, `startup_registration.registered=true`, `worker_connected=true`도 확인합니다.

## 라우팅 계약

Windows 전용 작업은 `required_capabilities=["windows_e2e"]`를 지정합니다. 공개 URL·일반 로그인 검증은 PC 노드가 없어도 `PC Bridge → Headless → HTTP/API health` 폴백으로 계속 수행합니다.

## 롤백

```powershell
schtasks.exe /End /TN AADSWindowsE2ENode
schtasks.exe /Delete /TN AADSWindowsE2ENode /F
```

설정 파일에는 토큰이 있으므로 노드 폐기 시 Windows 자격 저장소 정책에 따라 안전하게 제거하고, AADS 대시보드에서 해당 토큰을 폐기합니다.
