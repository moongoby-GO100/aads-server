---
name: sales-channel-collector
description: PC Agent 기반 판매채널 자동수집을 실행, 진단, 확장, 다른 프로젝트로 이식할 때 사용합니다.
allowed-tools: Bash, Read, Glob, Grep, Edit, Write
---
# 판매채널 자동수집 스킬

## 목적

FOOD/열정국밥 판매채널 자동수집은 PC Agent 브라우저 세션, 포털별 collector, 표준 원장 writer, 상태 ledger가 결합된 기능이다. 이 스킬은 배민, 쿠팡이츠, 요기요, 땡겨요 같은 판매채널 수집을 실행/진단/확장하거나 다른 프로젝트로 이식할 때 사용한다.

정본 기획서: `docs/plans/20260821_SALES_CHANNEL_PC_AGENT_COLLECTION_ARCHITECTURE.md`

## 먼저 읽을 파일

- `docs/plans/20260821_SALES_CHANNEL_PC_AGENT_COLLECTION_ARCHITECTURE.md`
- `app/services/yeoljeong_delivery_collectors.py`
- `app/services/yeoljeong_finance_service.py`
- `scripts/yeoljeong_auto_collect.py`
- 관련 테스트: `tests/unit/test_yeoljeong_delivery_collectors.py`, `tests/unit/test_yeoljeong_finance_service.py`

## 불변 원칙

- CAPTCHA, OTP, 본인인증, 기기 인증, 약관 동의는 우회하지 않는다. 단, 권한 있는 운영자가 특정 페이지/업무/세션/실행횟수를 승인한 경우 그 범위 안에서 모델 분석, 자동입력, 자동제출, 결과 재판정까지 자동화할 수 있다.
- 완전 자동화 목표는 공식 연동, 조회전용 계정, 신뢰된 PC Agent 세션, Vault write-only 입력, 푸시 승인 polling, 같은 work key 재개로 사람 반복 작업을 줄이는 것이다.
- 소형 모델은 승인 없는 CAPTCHA/OTP 정답 생성기로 사용하지 않는다. 승인된 CAPTCHA 자동화 범위에서는 같은 브라우저 세션의 해당 페이지에 한해 CAPTCHA 판독과 입력을 수행할 수 있으며, OTP 무단 생성/외부 기기 조작/승인 없는 값 읽기는 금지한다.
- 일반 Chrome을 닫지 않는다. PC Agent 관리형 work session만 대상으로 한다.
- 평문 비밀번호, 포털 원본 HTML, 다운로드 파일, storage state를 문서나 커밋에 남기지 않는다.
- `browser-automation` 계정은 PC Agent 전용 세션을 우선한다. 서버 headless fallback은 명시 허용이 있을 때만 사용한다.
- 상태 보고는 `delivery_collection_status`의 `status`, `error_code`, `counts`, `diagnostics` 기준으로 한다.

## 실행

단발 수집:

```bash
python3 scripts/yeoljeong_auto_collect.py --services baemin,coupangeats,yogiyo,ddangyo --business-id all --branch 전체 --skip-financial-accounts
```

PC Agent 세션 재생성 포함:

```bash
python3 scripts/yeoljeong_auto_collect.py --services baemin,coupangeats,yogiyo,ddangyo --business-id all --branch 전체 --force-recreate-sessions --skip-financial-accounts
```

반복 수집:

```bash
python3 scripts/yeoljeong_auto_collect.py --services baemin,coupangeats,yogiyo,ddangyo --business-id all --branch 전체 --until-complete --max-attempts 3 --exit-zero-on-blocked
```

## 진단

1. PC Agent 연결과 브라우저 work session이 살아 있는지 확인한다.
2. `delivery_collection_status`에서 최신 row의 `error_code`를 먼저 본다.
3. `PC_AGENT_SESSION_REQUIRED`, `PC_AGENT_SESSION_NOT_FOUND`, `PC_AGENT_WRONG_PORTAL_SESSION`, `PC_AGENT_COLLECTOR_TIMEOUTERROR`는 세션 재생성 후보로 본다.
4. `PORTAL_AUTH_CHALLENGE`, `DDANGYO_NUMERIC_CAPTCHA_REQUIRED`, `PORTAL_BLOCKED`, `CSV_UPLOAD_REQUIRED`, `MISSING_CREDENTIALS`는 먼저 `auto_resumable`, `approval_required`, `manual_only`, `blocked_by_policy`로 재분류한다.
5. CAPTCHA/OTP/기기 인증은 자동 가능 경로를 먼저 시도하되, 승인 없는 CAPTCHA 판독, 허가 없는 OTP 생성/대리 입력, 외부 기기 조작, 권한 변경 동의는 정책 차단으로 남긴다.
6. 땡겨요 숫자 CAPTCHA는 `operator_approved=true`와 1회성 `approved_input`이 들어온 실행 또는 `captcha_auto_approved=true`와 승인 scope가 들어온 실행에서만 같은 work key 세션에 입력한다. 계정 원장에 CAPTCHA 값을 저장하거나 재사용하지 않는다.
7. 수집 성공 판정은 `status=succeeded` 또는 해당 scope의 `counts`만 보지 말고, 계정 x 서비스 x `sales/settlements/reviews/ads` 완료 매트릭스로 판단한다.

## 안정화 체크

- PC Agent 재연결 이력이 있으면 `pc_agent_connection_events`에서 대상 agent_id의 `disconnected` reason을 먼저 확인한다.
- `keepalive ping timeout`, `server_restart:fast_reconnect`, `WebSocketDisconnect 1012` 이후에는 기존 work key를 그대로 신뢰하지 말고 health check 후 재사용/재생성을 결정한다.
- `ATTEMPT_TIMEOUT`, `BACKGROUND_SYNC_STALE`, `COLLECTION_ALREADY_RUNNING`이 남아 있으면 stale lock, 살아 있는 collector 프로세스, 워커 컨테이너 command를 함께 확인한다.
- 완료 판정은 row 존재만으로 하지 않는다. 등록 계정별 `sales`, `settlements`, `reviews`, `ads` 네 종류가 각각 성공, 실제 0건, 또는 action_required로 확정되어야 한다.
- `PORTAL_TABLE_NOT_FOUND`, `AUTHENTICATED_NO_ROWS`, `NO_PARSEABLE_ROWS`는 포털 DOM 변경, 조회기간 0건, 로그인 후 빈 대시보드를 분리해 진단한다.
- 수집 종료 또는 오류 후에는 PC Agent 관리형 work session만 닫혔는지 확인한다. 일반 Chrome 탭/프로필을 닫으면 안 된다.
- 인증 챌린지는 `PortalAuthChallengeOrchestrator` 기준으로 자동 가능/승인 필요/수동 전용/정책 차단 상태를 남기고, 승인 또는 입력이 끝나면 같은 work key에서 재개한다.
- 소형 모델 판단은 민감값 제거 DOM/화면 요약만 입력하고, JSON schema와 confidence gate를 통과한 후보만 상태 머신에 넘긴다.

## 새 채널 추가

1. `PORTAL_CONFIG`에 service key, label, login_url, sections를 추가한다.
2. 로그인 selector가 일반 fallback으로 부족하면 `LOGIN_SELECTOR_CONFIG`에만 보강한다.
3. URL marker, challenge term, dismiss selector가 필요하면 포털별 상수에 추가한다.
4. `normalize_record()`가 읽는 컬럼 alias만 최소 보강한다.
5. 테스트는 포털 등록, deterministic id, PC Agent 우선, auth challenge 상태 매핑, 완료 매트릭스를 포함한다.

## 이식 기준

다른 프로젝트로 옮길 때는 아래 인터페이스만 맞춘다.

- Account Registry: service, username, business_id, branch, collection_mode, secret reference
- Browser Provider: PC Agent 또는 동등한 authenticated browser session
- Collector Adapter: portal URL, selectors, section labels, export parser
- Ledger Writer: canonical record upsert
- Status Ledger: job_id, run_id, status, error_code, counts, diagnostics

## 검증

수정 후 최소 검증:

```bash
python3 -m py_compile app/services/yeoljeong_delivery_collectors.py app/services/yeoljeong_finance_service.py scripts/yeoljeong_auto_collect.py
pytest tests/unit/test_yeoljeong_delivery_collectors.py tests/unit/test_yeoljeong_finance_service.py -q
```

문서/스킬만 바꾼 경우에는 `git diff --check`를 실행하고, 코드 테스트는 미실행 사유를 보고한다.
