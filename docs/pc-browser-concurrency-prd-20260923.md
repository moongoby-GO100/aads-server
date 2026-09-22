# 채팅별 PC 브라우저 동시 사용 PRD / 설계

상태: 코드 구현 및 로컬 검증 완료. 운영 API·대시보드·Windows PC 배포 전이며 이 문서는 배포 완료 증거가 아니다.

## 목표와 대상 사용자
같은 PC를 사용하는 AADS 운영자가 서로 다른 채팅의 브라우저 화면을 동시에 보고 클릭·입력한다. PC 전체 화면이나 OS 마우스·클립보드를 공유하지 않는다.

## 사용자 흐름
- 첫 실행: 채팅 → 아티팩트 → 브라우저 → PC 브라우저 → PC 선택 → 주소 열기.
- 반복 실행: 동일 채팅의 업무 키·전용 Chrome 프로필·탭을 재사용하며 로그인 상태를 보존한다.
- 오류 복구: 오프라인, 업데이트 필요, 탭 종료, 중복 연결을 구분해 표시한다. 끊김 즉시 화면·입력을 비활성화하고 재연결한다. 다른 탭이나 데스크톱으로 자동 전환하지 않는다.
- 설정/페어링은 기존 PC 관리 화면에 유지한다. 일반 화면은 주소·실시간 상태·클릭·입력·재연결만 제공한다. 모바일 버튼은 44px 이상이다.

## 구조와 계약
1. 서버가 인증된 tenant + chat UUID + PC 식별자로 업무 키를 유도한다. 클라이언트는 CDP 포트·임의 target을 지정하지 못한다.
2. Browser Bridge ensure_work_session으로 독립 Chrome 프로필을 확보한다. 전역 active_session은 변경하지 않는다.
3. PC Agent의 전용 browser_tab 명령이 명시적 CDP page target에 연결한다. 화면은 Page.captureScreenshot JPEG를 최대 초당 3회 요청해 같은 WS로 전송한다. 이는 탭 단위 연속 캡처 스트리밍이며 데스크톱 스트림과 구분한다. 실제 FPS/지연은 실측한다.
4. 클릭/텍스트/키/스크롤은 같은 target의 CDP Input에 전달한다. OS 입력과 Target.activateTarget을 사용하지 않는다. viewport CSS 좌표와 캡처 크기를 함께 보낸다.
5. 같은 업무의 스트림은 PostgreSQL advisory lock으로 한 연결만 소유한다. 다른 업무는 PC Agent에서 독립 연결·잠금으로 병렬 실행한다. 종료/오류 시 잠금을 해제하고 CDP 연결만 닫으며 Chrome과 로그인 상태는 보존한다.
6. 탭 식별자를 찾지 못하면 실패하며 다른 탭을 선택하지 않는다. reconnect는 보존된 tab을 확인한다. API 초기 단계는 관리자만 허용하고 채팅 tenant 소속도 검증한다.
7. 입력값·비밀번호·프레임을 DB/로그에 저장하지 않는다. PC 레시피 자동 학습은 이번 범위에 포함하지 않는다. 기존 서버 레시피 학습은 유지한다.
8. PC Agent에 새 명령이 없으면 업데이트 필요로 종료한다. PC Agent ZIP 정상 업데이트 경로를 사용한다.

## 검증 및 승인 기준
- 서로 다른 채팅/프로필 A·B 동시 프레임 수신과 각각 다른 입력·클릭 결과를 실제 화면에서 검증.
- A 입력이 B에 전달되지 않음, OS 포커스를 바꾸지 않음, 같은 업무 중복 소유 거절.
- 잘못된 토큰·다른 tenant·닫힌 target·오프라인·프레임 지연 시 입력 거절.
- reconnect 후 로그인/페이지 보존, 기존 서버 스트림 회귀검사, 모바일 화면 캡처.
- backend/dashboard 커밋·푸시 후 정식 Blue/Green. 단일 이미지 빌드·동일 digest standby·헬스·5분 P0/P1 감시 통과 후 배포 완료 보고.

## 위험과 롤백
PC 리소스/네트워크에 따라 FPS가 낮아질 수 있다. 자동화와 사람이 같은 업무를 조작하는 범위는 별도 운영 주의가 필요하다. API/대시보드는 이전 이미지로 라우팅 복귀한다. PC 새 명령은 기존 명령과 독립되어 이전 API와 호환되며 Chrome 프로필은 삭제하지 않는다.

## 구현 경로와 운영 조건
- API: `/api/v1/browser-bridge/chat/{chat_id}/pc-live`. 관리자 JWT와 채팅/PC tenant 일치를 확인한다. 클라이언트는 PC와 최초 URL만 보내며 프로필·포트·target은 서버가 정한다.
- 서버: `app/browser_bridge/live_api.py`, `app/api/browser_bridge.py`, `app/services/pc_agent_manager.py`. 채팅별 DB advisory lock, 매 조작 전 DB 연결/JWT 검증, 연결 종료 시 잠금 해제. 프레임은 일반 명령 결과 캐시에서 즉시 제거한다.
- PC: `pc_agent/commands/browser_tab.py`, 명령 등록 및 VERSION 1.0.74. 명시적 target/CDP 입력, 5초 이내 프레임 ID와 페이지 epoch/URL/viewport 일치 검사, 60초 유휴 토큰 만료. 브라우저를 종료하지 않고 CDP만 분리한다.
- 대시보드: `LiveBrowserStage.tsx`, `BrowserArtifactView.tsx`, `ohvis/page.tsx`. PC 모드는 채팅 ID 필수, 전체 데스크톱 경로 자동 대체 금지, 입력 확인 응답 대기, 화면 지연/끊김 시 입력 비활성화, 재연결/스크롤/비밀값 입력.
- 실행 조건: PC Agent가 연결된 API 슬롯에서만 신규 스트림을 연다. 슬롯 전환 중 PC가 아직 다른 슬롯에 연결됐으면 오프라인 안내 후 재연결한다. 다른 슬롯을 통한 프레임 프록시는 이 버전에 포함하지 않는다.
- Chrome 프로필은 기존 포트 할당 용량과 PC 자원 범위에서 사용한다. 새 프로필은 별도 로그인이 필요하다. 기존 개인 Chrome의 로그인 쿠키를 복제하지 않는다.
- 캡처·입력 경로는 OS 포커스 활성화 명령을 사용하지 않는다. 최초 Chrome 실행의 Windows 포커스 동작과 금융 보안프로그램 호환성은 운영 PC 검증 대상이다.

## 검증 근거
- Python 회귀: `test_pc_browser_tabs.py`, `test_pc_browser_live_api.py`, `test_browser_live_control.py`, `test_pc_agent_manager_connection_guard.py` — 27 passed.
- PC 배포 패키지 회귀: `test_pc_agent_release_guards.py`, `test_pc_agent_download.py` — 20 passed.
- 실제 Chromium: `PYTHONPATH=. python tests/integration/pc_tab_cdp_smoke.py` — 2개 프로필의 동시 입력·클릭 분리, 재연결 후 값 보존, 닫힌 target의 대체 탭 선택 거절 통과. 증거 `docs/reports/pc-browser-concurrency-20260923/`.
- 프론트: `npm run typecheck`, 대상 컴포넌트 ESLint, `python3 tests/live-browser/test_stage.py` 통과. 화면 테스트는 실제 컴포넌트 + 모의 WS/API 응답이며 운영 인증 E2E와 구분한다.
- 미검증: 운영 PC에서 서로 다른 채팅 두 개의 동시 조작, 로그인 복원, Windows 포커스, 실제 네트워크 FPS/지연, Blue/Green 및 5분 운영 관측. 성능 수치는 주장하지 않는다.

## 릴리스와 롤백
검증된 브랜치를 최신 main에 선별 통합한 후 API → PC 정상 ZIP 업데이트 → 대시보드 순으로 배포하고 실제 PC 두 채팅 검증을 수행한다. 각 API/대시보드 배포는 저장소 Blue/Green 계약을 따른다. 실패하면 API/대시보드 이전 digest로 라우팅 복귀하며 새 PC 명령은 이전 API와 공존한다. 프로필/쿠키를 삭제하지 않는다. 푸시·운영 배포는 CEO 명시 승인 후 실행한다.
