# RESULT — AADS-CLAUDE-SLOT4-RELOGIN-REVIEW-FIX-20260928

## STEP 0 기존 구현 분류

- 유지: `llm_keys.py`의 키 CRUD, overview 바인딩 및 relay 로그인 세션/code/cancel endpoint, `scripts/account_login.py`의 pty 기반 Codex/Claude 로그인, Claude token keeper의 주기적인 토큰 DB 동기화, 기존 snapshot 적재 형식.
- 수정: `_reconcile_successful_account_login`에서 슬롯1~3 기존 rate limit 해제 및 snapshot 기록을 복구하고 슬롯4의 계정/지문/만료 검증 후 활성화하도록 유지. `account_login.py`는 신규 토큰 freshness와 Claude CLI 실호출 검증을 유지하고, 슬롯4 성공 시 keeper DB 동기화를 즉시 호출한다. `claude_token_keeper.sh`에는 호출 범위를 슬롯4로 한정하는 선택 필터를 추가했다.
- 신규: 슬롯4 토큰/호출 증명 및 격리 테스트를 두 기존 unit test 파일에 추가했다.
- 삭제: 없음. 삭제 대상·호출처 영향·롤백 필요 없음.
- DB 접점: 슬롯1~3은 기존 OAuth record 조회, `llm_api_keys` rate limit 해제, `claude_max_usage_snapshot` 중복 방지 삽입을 유지한다. 슬롯4는 행 잠금 후 OAuth access token 지문과 5분 초과 만료, 바인딩 이메일을 비교하고 같은 트랜잭션에서만 활성화/격리 해제 및 snapshot 기록을 한다. keeper는 슬롯4 로그인 검증 직후 `llm_api_keys.encrypted_value`와 `oauth_expires_at`을 동기화한다. 다른 슬롯은 keeper 필터로 건드리지 않는다.
- 대시보드: 파일/패치 변경 없음. 새 diff에는 서버 파일만 포함.

## 변경 파일

- `app/api/llm_keys.py`
- `scripts/account_login.py`
- `scripts/claude_token_keeper.sh`
- `tests/unit/test_account_login_bindings.py`
- `tests/unit/test_llm_keys_slot_mapping.py`
- 본 결과 파일

## 검증

- `JWT_SECRET_KEY=test-only-secret-for-unit-tests /root/aads/aads-server/.venv/bin/pytest -q tests/unit/test_account_login_bindings.py tests/unit/test_llm_keys_slot_mapping.py`: **13 passed**. 실제 CLI/DB는 모킹했다. 미래 만료 credentials, 실호출 함수 성공, keeper 슬롯4 전용 호출, 잘못된 계정/지문/5분 미만 만료/DB 행 없음 격리, 올바른 계정/지문/만료의 DB 활성화와 snapshot 적재를 확인했다.
- `python3 -m compileall -q app/api/llm_keys.py scripts/account_login.py tests/unit/test_account_login_bindings.py tests/unit/test_llm_keys_slot_mapping.py`: **통과**.
- `bash -n scripts/claude_token_keeper.sh`: **통과**.
- `python3 scripts/dup_guard.py`: **통과**.
- `git diff --check`: **통과**.
- `ruff check app/api/llm_keys.py scripts/account_login.py tests/unit/test_account_login_bindings.py tests/unit/test_llm_keys_slot_mapping.py`: **실패, 전체 25건**. 대부분 `llm_keys.py` 기존 코드의 import 정렬/기존 예외 처리/UTC alias 경고이며, 신규 테스트 import 정렬 및 UTC alias 지적도 포함. 자동 수정은 기존 파일 전반을 불필요하게 변경하므로 적용하지 않았다.
- `bash scripts/run_unit_tests.sh`: **실행 실패**. 러너가 필요한 `container:aads-server` 기준 이미지를 찾지 못했고 `docker ps` 결과에도 컨테이너가 없었다.
- DB error-book `python3 scripts/error_book.py match -`: **실패**, 호스트에 `psql` 실행 파일이 없어 조회 불가. DB handover 경로도 현재 세션 도구에 없어 기록하지 못했다.
- HTTP/API 및 브라우저 인증 세션 E2E와 실제 Claude 계정/CLI 호출, 운영 DB 반영: **미검증**. targeted 테스트에서 외부 CLI와 DB는 모킹했다.
- `npm run build`/`next build`/`docker build`: **승인 후 Runner 빌드 검증 대상**. 실행하지 않음.

## 보존 및 기록

- 슬롯4 실호출/계정/만료/DB 경로가 실패하면 `verification_pending` 또는 false를 반환해 기존 2100 hold와 비활성 상태를 유지한다. 슬롯4 외 Claude 계정은 기존 reconciliation만 수행한다.
- 토큰 원문은 API 응답 및 새 로그에 출력하지 않는다. keeper는 기존 stdin 경로를 사용하고 릴레이에서 stdout/stderr를 폐기한다.
- 커밋, 푸시, 배포, 운영 DB 변경을 수행하지 않았다.
