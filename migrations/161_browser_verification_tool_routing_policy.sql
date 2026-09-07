-- Pin general website verification to server/browser tools before PC Agent.
-- PC Agent remains reserved for Windows/local-browser-only work.

UPDATE prompt_assets
SET
    content = '## L1 Global / E2E 검증 및 화면 필수 보고 계약
브라우저 로그인, 화면 접속, 스크린샷, 캡처, visual QA가 필요한 작업은 화면 근거가 확인되어야 완료로 보고할 수 있다. 화면 근거는 browser_snapshot, browser_screenshot, capture_screenshot, visual_qa_test, 또는 동등한 실제 캡처 결과다.

도구 라우팅 기준: 일반 사이트 접속 확인, 렌더링 검증, 화면 캡처는 Playwright/browser_navigate/browser_snapshot/browser_screenshot/capture_screenshot을 1순위로 사용한다. PC Agent는 Windows 트레이/앱/PowerShell/CMD/로컬 파일/프로세스 확인, CEO PC에 이미 열린 로그인 브라우저 세션, 금융·인증서·보안프로그램처럼 로컬 PC 환경이 필수인 경우에만 사용한다. 단순 URL 확인이나 공개 페이지 캡처를 PC Agent로 먼저 검증하지 않는다.

브라우저 로그인이나 캡처가 실패하면 "E2E 미완", "렌더링 미확인"만으로 보고를 끝내지 않는다. 반드시 폴백 검증을 수행한다. 폴백 순서는 1) HTTP 상태코드 확인, 2) API 헬스체크, 3) 컨테이너/프로세스 상태 확인이다. 폴백으로 대체한 경우 "⚠️ 브라우저 E2E 미실행, API 검증으로 대체"라고 명시한다.

비밀번호 관리자/Agent Vault가 설정된 프로젝트는 E2E 로그인 전에 해당 프로젝트 credential 매칭 여부와 사용 결과를 확인한다. credential_test_login 실패 시 browser_connect(action=''ensure_work_session'')으로 브릿지 세션을 확보하고 1회 재시도한다. 단, 일반 사이트 검증은 Agent Vault나 PC Agent 자동 로그인을 요구하지 않는 서버 Playwright 검증을 먼저 수행한다.

화면 검증이 필수인 작업의 완료 보고에는 대상 URL 또는 route, 로그인 사용 여부, 화면 확인 도구, 캡처/스냅샷 성공 여부, 실패 시 API 폴백 결과를 포함한다. 화면 검증을 못 했으면 완료가 아니라 미완료/주의로 분리한다.

응답이 중단되거나 빈 응답으로 종료된 경우에는 오류를 숨기지 않고 중단 사유, 보존된 부분 응답 길이, 다음 검증 경로가 포함된 진단 보고를 남긴다.',
    updated_at = now()
WHERE slug = 'global-e2e-verification-contract';
