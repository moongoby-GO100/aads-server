# FOOD Browser Collection Audit Plan

작성 시각: 2026-09-03 08:11 KST

## 요약

신한은행 자동수집은 각 페이지/단계별 성공 여부, 성공 조건, 실패 원인, 타임아웃, 소요시간을 `diagnostics.shinhan_stage_logs`로 남기도록 보강했다. 배민/배달 포털 공통 브라우저 수집 경로도 같은 스키마의 `diagnostics.browser_stage_logs`를 사용하게 해 사이트 접속 자동화 전체에서 실패 단계를 추적할 수 있게 했다.

운영 완료 판정은 단순 `status=succeeded`가 아니라 `browser_session -> site_access -> auth_state -> query/data_collection` 단계 로그와 실제 저장 건수 또는 명시적 `no_records` 결과가 함께 확인될 때만 완료로 본다.

## 적용 스키마

공통 모듈: `app/services/browser_collection_audit.py`

| 필드 | 의미 | 비고 |
| --- | --- | --- |
| `stage` | 수집 단계명 | 예: `shinhan_login_success`, `baemin_order_history_data_collection` |
| `status` | `success` / `failed` / `pending` | 다음 단계 진행 가능 여부 |
| `recorded_at` | UTC 기록 시각 | 서버/PC 시각 차이 추적용 |
| `elapsed_ms` | 단계 소요시간 | `time.monotonic()` 기준 |
| `error_code` | 기계 판정 오류 코드 | 실패/대기 시 필수 |
| `reason` | 사람이 보는 원인 | 예: `rows_parsed`, `page_goto_failed` |
| `attempt_index` | 반복 시도 번호 | 신한 state machine 재시도 구분 |
| `success_condition` | 성공 판정 근거 | 예: `post_login_text_or_url_marker_observed` |
| `failure_condition` | 실패 판정 근거 | 예: `post_login_marker_not_observed_before_timeout` |
| `timeout_ms` | 단계 제한시간 | 페이지 이동/섹션 조회 타임아웃 추적 |

비밀값 차단: `password`, `secret`, `token`, `cookie`, `credential`, `account_no`, `registration_no`, `raw_html` 계열 키는 공통 모듈에서 `[REDACTED]` 처리한다.

## 신한은행 단계 체크리스트

| 단계 | 성공 조건 | 실패 시 기록 |
| --- | --- | --- |
| 브라우저 세션 | PC Agent/Browser Bridge session id 확보 | `PC_AGENT_LOGIN_REQUIRED`, `PC_AGENT_UNAVAILABLE`, `CDP_NOT_READY` |
| 사이트 접속 | 신한 간편조회 URL 로드 또는 기존 탭 재사용 | `PORTAL_NAVIGATION_FAILED` |
| 간편조회 페이지 | `bank.shinhan.com/rib/easy/index.jsp` 계열 URL 확인 | `unknown` 상태와 현재 URL |
| ID/PW 입력 | ID와 비밀번호/보안키패드 입력 플래그 확인 | `IDPW_INPUT_NOT_CONFIRMED` |
| 로그인 제출 | 로그인 버튼/웹스퀘어 submit 트리거 확인 | `LOGIN_SUBMIT_NOT_CONFIRMED` |
| 로그인 성공 | 로그아웃/조회기간/거래내역/잔액 등 post-login 마커 확인 | `LOGIN_SUCCESS_NOT_OBSERVED`와 소요시간 |
| 계좌조회 화면 | 공지 확인 또는 계좌조회 페이지 이동 확인 | `ACCOUNT_QUERY_PAGE_NOT_CONFIRMED` |
| 계좌/비밀번호/기간 | 계좌 선택 또는 직접입력, 계좌 비밀번호, 조회기간 입력 확인 | 각 단계별 미확인 코드 |
| 조회 제출 | 조회 버튼/웹스퀘어 이벤트 트리거 확인 | `QUERY_SUBMIT_NOT_CONFIRMED` |
| 데이터 수집 | 거래 테이블 파싱 또는 다운로드 파싱 성공 | `TRANSACTION_ROWS_NOT_OBSERVED` 또는 화면 상태 코드 |

## 글로벌 사이트 접속 적용 범위

| 경로 | 적용 상태 | 기록 위치 |
| --- | --- | --- |
| 신한은행 브라우저 수집 | 적용 | `diagnostics.shinhan_stage_logs` |
| 일반 배달 포털 브라우저 수집 | 적용 | `diagnostics.browser_stage_logs` |
| 배민 전용 주문/리뷰/광고 수집 | 적용 | `diagnostics.browser_stage_logs` |
| 향후 매입처/홈택스/카드PG 포털 | 적용 기준 정의 | 같은 공통 모듈 호출 |

## 실패 방지 운영안

| 우선순위 | 조치 | 완료 기준 |
| --- | --- | --- |
| P0 | 은행 큐 claim 직후 collectable 계좌 재검증 | 불완전 신한/IBK 계좌는 브라우저 접속 전 `BANK_ACCOUNT_NOT_COLLECTABLE`로 cancel |
| P0 | PC Agent work key별 세션 재사용/재생성 기록 | `browser_session` 단계에 `session_ready` 또는 재생성 실패 코드 기록 |
| P0 | 페이지 이동과 인증 상태 분리 | `site_access` 성공이어도 `auth_state=pending/login/challenge/blocked`를 별도 기록 |
| P1 | 각 수집 섹션별 데이터 건수 기록 | `*_data_collection` 단계별 `row_count`와 실패 코드 확인 |
| P1 | 타임아웃 값을 로그에 포함 | 실패 원인이 느린 로딩인지 로그인/권한 문제인지 분리 |
| P2 | 대시보드에 단계 로그 리스트 노출 | 운영자가 실패 단계만 필터링해 재시도/인증/CSV 대체 판단 |

## 검증 방법

1. 코드 검증: `python -m py_compile`로 공통 모듈, 신한 커넥터, 재무 서비스, 자동수집 스크립트 컴파일을 확인한다.
2. 단위 검증: 신한 단계 로그, 비밀값 마스킹, 은행 계좌 선별/큐 취소 테스트를 통과해야 한다.
3. 운영 검증: 배포 후 미아점 신한 수집을 실행하고 `shinhan_stage_logs`에서 로그인/조회/데이터 단계가 순서대로 기록되는지 확인한다.
4. 결과 검증: `bank_collections[].imported_rows > 0` 또는 정상 `no_records`와 `data_collection status=success row_count=0`이 확인되어야 한다.
5. 실패 검증: 중화점 등 필수 신한 quick-service 값이 없는 범위는 브라우저가 열리지 않고 `browser_attempted=false`로 종료되어야 한다.

## 남은 운영 조건

현재 변경은 코드/테스트/문서 단계까지 완료했다. 실제 은행 자료 수집은 운영 배포 후 실행해야 하며, 배포는 AADS blue/green 규칙상 깨끗한 release SHA, 후보 슬롯 헬스체크, 라우팅 헬스체크, standby 동일 digest, 5분 P0/P1 모니터링을 통과해야 완료로 보고할 수 있다.
