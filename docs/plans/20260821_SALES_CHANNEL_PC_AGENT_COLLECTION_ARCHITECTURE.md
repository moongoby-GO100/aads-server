# PC Agent 판매채널 자동수집 기획 및 아키텍처

작성: 2026-08-21 14:23 KST<br>
범위: FOOD/열정국밥 판매채널 자동수집을 AADS 재사용 모듈로 정리

## 1. 결론

전용 기획서는 없었고, 구현은 이미 `app/services/yeoljeong_finance_service.py`, `app/services/yeoljeong_delivery_collectors.py`, `scripts/yeoljeong_auto_collect.py`, 정적 매장비서 화면에 분산되어 있었다. 이 문서는 현재 구현을 정본 아키텍처로 묶고, 이후 다른 매장/프로젝트에서도 같은 수집 기능을 재사용하기 위한 확장 기준을 정의한다.

핵심 방향은 "포털별 DOM/인증 차이는 collector adapter에 격리하고, 계정/범위/상태/원장 반영은 공통 orchestration layer에서 처리한다"이다. PC Agent는 사람 인증, IP/기기 신뢰, 포털 세션 유지가 필요한 채널의 기본 실행 경로이며, 서버 headless는 명시 허용 시에만 폴백한다.

## 2. 기존 자산 확인

| 구분 | 현재 자산 | 확인 내용 |
|------|-----------|-----------|
| 포털 collector | `app/services/yeoljeong_delivery_collectors.py` | 배민, 쿠팡이츠, 요기요, 땡겨요 `PORTAL_CONFIG`와 `sales/settlements/reviews/ads` 파서 보유 |
| orchestration | `app/services/yeoljeong_finance_service.py` | 계정 선택, 권한 체크, PC Agent work session, lock, 상태 원장, 원장 upsert 담당 |
| 자동 실행 CLI | `scripts/yeoljeong_auto_collect.py` | 전체 지점/서비스 루프, 타임아웃, retry, blocked 상태 분류, 은행/금융 계정 동시 수집 옵션 보유 |
| UI | `app/static/apps/yeoljeong-finance/index.html` | 판매채널 등록, 준비상태, 수집 실행, 업로드 폴백 화면 존재 |
| 테스트 | `tests/unit/test_yeoljeong_delivery_collectors.py`, `tests/unit/test_yeoljeong_finance_service.py` | 4개 포털 구성, 결정적 정규화, PC Agent 세션 우선, all scope 수집, stale session 재생성 검증 |

## 3. 목표

1. 판매채널 자료를 지점/사업자별로 반복 수집한다.
2. 수집 결과를 `sales`, `settlements`, `reviews`, `ads` 원장에 중복 없이 병합한다.
3. CAPTCHA, OTP, 기기 인증, 포털 보안 차단은 자동 가능 경로를 먼저 시도한 뒤, 정책상 허용되지 않는 우회만 `action_required` 또는 `blocked_by_policy`로 남긴다.
4. PC Agent 전용 브라우저 세션을 work key 단위로 분리해 일반 Chrome을 건드리지 않는다.
5. 매장비서 외 다른 프로젝트에서도 "계정 registry + collector adapter + canonical ledger writer" 조합으로 재사용한다.

## 4. 비목표 및 안전 원칙

- 포털 CAPTCHA, OTP, 기기 인증, 약관 동의 자동 우회는 하지 않는다. 완전 자동화 목표는 공식 API, 조회전용 계정, 신뢰된 PC Agent 세션, Vault write-only 입력, 푸시 승인 polling, 같은 work key 재개로 사람 반복 작업을 줄이는 것이다.
- 포털 원본 HTML, 다운로드 파일, 평문 비밀번호, 브라우저 storage state를 장기 저장하지 않는다.
- 일반 사용자 Chrome 탭/프로필을 자동 종료하지 않는다.
- 대량 병렬 수집으로 포털 계정/IP 차단 위험을 만들지 않는다.
- 실매출/정산 데이터 보정은 자동 추정하지 않고 원천 row 기준으로만 적재한다.

## 5. 대상 채널과 데이터

| 채널 | 서비스 키 | 기본 인증 경로 | 수집 레코드 | 주요 리스크 |
|------|-----------|----------------|-------------|-------------|
| 배달의민족 | `baemin` | PC Agent 또는 storage state | 매출, 정산, 리뷰, 광고 | SPA 화면 구조 변경, 로그인 상태 만료 |
| 쿠팡이츠 | `coupangeats` | PC Agent 전용 session | 매출, 정산, 리뷰, 광고 | 보안 차단, 기기 신뢰 |
| 요기요 | `yogiyo` | PC Agent 전용 session | 매출, 정산, 리뷰, 광고 | 조회기간 0건과 인증 실패 구분 |
| 땡겨요 | `ddangyo` | PC Agent 전용 session | 매출, 정산, 리뷰, 광고 | 숫자 CAPTCHA, 추가 인증 |

## 6. 아키텍처

```text
CEO/매장비서 UI
  -> finance API / CLI
  -> sync_delivery / queue_delivery_sync
  -> account registry(platform_accounts) + Agent Vault secret hydration
  -> PC Agent Browser Bridge work session
  -> portal collector adapter
  -> canonical record normalizer
  -> delivery ledger writer + status ledger
  -> UI readiness/status/ledger views
```

### 6.1 계층별 책임

| 계층 | 책임 | 대표 파일 |
|------|------|-----------|
| UI/Command | 수집 범위, 채널, 기간, 수동 업로드 입력 | `app/static/apps/yeoljeong-finance/index.html`, `scripts/yeoljeong_auto_collect.py` |
| Orchestrator | 권한, 지점 scope, 계정 선택, lock, retry/status, 원장 병합 | `app/services/yeoljeong_finance_service.py` |
| Browser Session | PC Agent work key 생성, session 재사용/재생성, 일반 Chrome 보호 | `app/services/yeoljeong_finance_service.py`, `app/services/pc_agent_manager.py`, `pc_agent/commands/browser_auto.py` |
| Collector Adapter | 포털별 URL, selector, export/copy/table 파싱 | `app/services/yeoljeong_delivery_collectors.py` |
| Canonical Ledger | 서비스별 원본 row를 표준 원장 row로 정규화 | `normalize_record()` |
| Observability | `delivery_collection_status`에 run_id/job_id/status/error_code/counts/diagnostics 기록 | `queue_delivery_sync()`, `sync_delivery()` |

### 6.2 실행 흐름

1. UI 또는 CLI가 `services`, `business_id`, `branch`, `date_from`, `date_to`를 전달한다.
2. `queue_delivery_sync()`가 선택 scope별 queued row를 만들 수 있다.
3. `sync_delivery()`는 단일 lock으로 중복 실행을 차단한다.
4. 계정 registry에서 서비스/지점에 맞는 계정을 고르고 Agent Vault에서 비밀번호를 보강한다.
5. `browser-automation` 계정은 PC Agent work session을 우선 확보한다.
6. 포털별 collector가 인증된 화면에서 표/다운로드/복사 데이터를 수집한다.
7. `normalize_record()`가 deterministic id, source_id, amount/date 필드를 만든다.
8. 원장별 id upsert로 중복 없이 병합하고 `delivery_collection_status`를 갱신한다.
9. CAPTCHA/OTP/보안 차단은 `AuthChallengeOrchestrator`가 `auto_resumable`, `approval_required`, `manual_only`, `blocked_by_policy`로 분류한다.
10. 자동 가능 항목은 같은 work key에서 처리하고, 정책상 차단되는 항목만 운영 승인 또는 수동 입력으로 넘긴 뒤 동일 세션에서 재개한다.

## 7. 공통 인터페이스

### 7.1 수집 payload

```json
{
  "services": ["baemin", "coupangeats", "yogiyo", "ddangyo"],
  "business_id": "all",
  "branch": "전체",
  "date_from": "2026-08-01",
  "date_to": "2026-08-21",
  "prefer_pc_agent": true,
  "force_recreate_portal_sessions": false,
  "close_portal_browser_on_complete": false,
  "skip_financial_accounts": true
}
```

### 7.2 collector result

```json
{
  "status": "succeeded",
  "error_code": "",
  "records": {
    "sales": [],
    "settlements": [],
    "reviews": [],
    "ads": []
  },
  "diagnostics": {
    "auth_mode": "pc_agent_browser",
    "browser_work_key": "yeoljeong-delivery-baemin-biz-mia-..."
  }
}
```

### 7.3 상태 코드 정책

| 상태 | 의미 | 운영 판단 |
|------|------|-----------|
| `queued` | 백그라운드 수집 대기 | runner/CLI 실행 필요 |
| `running` | 수집 진행 중 | 15분 이상 갱신 없으면 stale 정리 대상 |
| `succeeded` | 요청 scope 성공 또는 row 적재 | 완료 |
| `partial` | 일부 row 또는 일부 섹션만 성공 | 재시도 또는 수동 보강 |
| `action_required` | 인증/보안/수동 업로드 필요 | CEO/운영자 개입 |
| `approval_required` | 자동 처리 전후 운영 승인 필요 | 승인 후 같은 work key 재개 |
| `blocked_by_policy` | 보안/약관/계정잠금 위험으로 자동 실행 차단 | 공식 연동 또는 수동 처리 |
| `failed` | 타임아웃/파서 실패/예외 | 로그와 diagnostics 기준 수정 |

대표 error_code:

| error_code | 의미 | 다음 조치 |
|------------|------|-----------|
| `PC_AGENT_SESSION_REQUIRED` | 전용 브라우저 세션 필요 | PC Agent online, work session 재생성 |
| `MISSING_CREDENTIALS` | 계정 비밀번호/secret 누락 | Agent Vault 또는 계정 등록 보강 |
| `PORTAL_AUTH_CHALLENGE` | OTP/본인인증/약관 등 인증 챌린지 | 푸시 승인 polling, 신뢰기기 확인, CEO 입력 감지 후 같은 세션 재개. 불가 시 운영 승인 |
| `DDANGYO_NUMERIC_CAPTCHA_REQUIRED` | 땡겨요 숫자 CAPTCHA 필요 | CAPTCHA 감지, 입력 위치 focus, `operator_approved=true`와 1회성 `approved_input` 수신 후 같은 work key 재개. 자동 판독/우회는 차단 |
| `PORTAL_BLOCKED` | 포털 보안 차단 | PC/IP/기기 신뢰 상태 확인 |
| `CSV_UPLOAD_REQUIRED` | 업로드형 계정 | 포털 CSV/엑셀 업로드 |
| `ATTEMPT_TIMEOUT` | 단일 시도 제한 초과 | 세션 재생성 후 재시도 |
| `COLLECTION_ALREADY_RUNNING` | lock으로 중복 실행 차단 | 현재 작업 완료 후 재실행 |

## 8. 모듈화 기준

새 판매채널을 추가할 때는 아래 4개만 건드리는 구조를 유지한다.

1. `PORTAL_CONFIG`에 `service`, `label`, `login_url`, `sections`, optional dismiss selector를 추가한다.
2. 필요한 경우 `LOGIN_SELECTOR_CONFIG`와 service URL marker를 추가한다.
3. 기존 `normalize_record()`가 읽을 수 없는 컬럼만 최소 보강한다.
4. 테스트에 "포털 등록", "정규화", "PC Agent 우선", "action_required 매핑" 케이스를 추가한다.

다른 프로젝트로 옮길 때는 아래 인터페이스만 맞추면 된다.

| 추상 모듈 | 현재 구현 | 다른 프로젝트에서 필요한 어댑터 |
|-----------|-----------|----------------------------------|
| Account Registry | `platform_accounts` | 서비스/지점/tenant 계정 목록과 secret reference |
| Secret Provider | Agent Vault hydration | Vault, 환경변수, 외부 secret manager |
| Browser Provider | PC Agent Browser Bridge | 로컬 PC Agent, 원격 브라우저, E2E 세션 |
| Collector Adapter | `yeoljeong_delivery_collectors.py` | 채널별 URL/selector/parser |
| Ledger Writer | `_write()`, `_db_upsert_ledger()` | 파일, PostgreSQL, Google Sheets, 외부 ERP |
| Status Ledger | `delivery_collection_status` | job/run 상태 저장소 |

## 9. 운영 실행서

단발 전체 수집:

```bash
python3 scripts/yeoljeong_auto_collect.py \
  --services baemin,coupangeats,yogiyo,ddangyo \
  --business-id all \
  --branch 전체 \
  --skip-financial-accounts
```

PC Agent 세션 재생성 포함 단발 수집:

```bash
python3 scripts/yeoljeong_auto_collect.py \
  --services baemin,coupangeats,yogiyo,ddangyo \
  --business-id all \
  --branch 전체 \
  --force-recreate-sessions \
  --skip-financial-accounts
```

완료 또는 수동 차단 상태까지 반복:

```bash
python3 scripts/yeoljeong_auto_collect.py \
  --services baemin,coupangeats,yogiyo,ddangyo \
  --business-id all \
  --branch 전체 \
  --until-complete \
  --max-attempts 3 \
  --exit-zero-on-blocked
```

운영 체크:

```bash
pytest tests/unit/test_yeoljeong_delivery_collectors.py tests/unit/test_yeoljeong_finance_service.py -q
```

대시보드/정적 앱에서 확인할 데이터 파일:

| 파일 | 의미 |
|------|------|
| `app/data/yeoljeong_finance/platform_accounts.json` | 판매채널/은행/매입처 계정 registry |
| `app/data/yeoljeong_finance/delivery_collection_status.json` | run 상태와 error_code |
| `app/data/yeoljeong_finance/delivery_sales.json` | 매출 원장 |
| `app/data/yeoljeong_finance/delivery_settlements.json` | 정산 원장 |
| `app/data/yeoljeong_finance/delivery_reviews.json` | 리뷰 원장 |
| `app/data/yeoljeong_finance/delivery_ads.json` | 광고 원장 |

## 10. 수용 기준

| 우선순위 | 완료 기준 | 검증 |
|----------|-----------|------|
| P0 | 4개 채널 계정이 지점별 readiness에 표시된다 | UI readiness rows 또는 `list_accounts()` |
| P0 | PC Agent 세션이 서비스별 work key로 분리된다 | status diagnostics의 `browser_work_key` |
| P0 | CAPTCHA/OTP/보안 차단을 자동 가능/승인 필요/정책 차단으로 분리한다 | `delivery_collection_status.error_code`, `approval_required`, `blocked_by_policy` |
| P0 | 원장 row id가 deterministic해서 재수집 중복이 없다 | 동일 source 재수집 후 row count 불변 |
| P1 | all scope가 등록 계정 기준으로 다지점 수집한다 | `business_id=all`, `branch=전체` 결과 |
| P1 | 포털별 DOM 변경 시 collector adapter만 수정한다 | finance service 변경 없이 테스트 통과 |
| P2 | Google Sheets/외부 ERP writer를 교체 가능하다 | Ledger Writer 인터페이스 분리 |

## 11. 남은 개선안

| 우선순위 | 개선안 | 기대효과 |
|----------|--------|----------|
| P0 | `delivery_collection_status` DB/파일 이중 쓰기 검증 리포트 추가 | 화면/DB 불일치 조기 탐지 |
| P0 | 포털별 challenge screenshot 링크를 UI 상태 카드에 노출 | CEO/운영자 수동 인증 속도 개선 |
| P1 | collector adapter registry 클래스로 분리 | 다른 프로젝트 이식 비용 감소 |
| P1 | Ledger Writer를 protocol로 분리 | 파일/DB/Sheets/ERP 대상 교체 가능 |
| P1 | scheduled task 템플릿 추가 | 매일/매주 자동수집 운영 표준화 |
| P2 | parser fixture library 구축 | 포털 DOM 변경 대응 시간 단축 |

## 12. 안정화 리스크와 보강 백로그

2026-08-21 15:04 KST 점검 기준, 기능 골격은 존재하지만 장기간 무인 수집 완료 기준에는 아래 보강이 필요하다.

| 우선순위 | 문제 영역 | 현재 증상/근거 | 개선 방향 | 완료 기준 |
|----------|-----------|----------------|-----------|-----------|
| P0 | PC Agent 재연결 | `pc_agent_connection_events`에 `keepalive ping timeout`, `server_restart:fast_reconnect`, `WebSocketDisconnect 1012` 이력이 반복된다. | 대상 PC `oby-ceo`의 agent_id/hostname 고정을 수집 payload, 계정 registry, compose env에서 단일 정본으로 관리하고 재연결 후 work session health check를 자동 실행한다. | agent 재연결 후 1분 내 동일 work key 재사용 또는 재생성 성공, `PC_AGENT_SESSION_REQUIRED` 신규 발생 0건 |
| P0 | 브라우저 무한 창/세션 혼선 | timeout/완료 cleanup 코드는 있으나 운영 원장에는 `ATTEMPT_TIMEOUT`, `BACKGROUND_SYNC_STALE`, `COLLECTION_ALREADY_RUNNING`이 남아 있다. | `close_on_complete`, `close_browser_on_timeout`, `work_key`별 active session count를 상태 원장에 기록하고 stale lock/process 정리 명령을 운영 실행서에 추가한다. | 단일 수집 run 종료 후 관리형 브라우저 세션 0개 또는 의도한 keep-open 세션 1개 이하 |
| P0 | 완료 판정 과소정의 | 자동 루프는 row가 1건이라도 있으면 해당 item을 완료로 볼 수 있다. 광고 원장 0건 상태에서 "전체 완료"로 오판할 수 있다. | 완료 기준을 `계정 x 서비스 x record_type` 매트릭스로 바꾸고 `ads`를 포함한 누락 타입을 별도 `partial`로 남긴다. | 4개 서비스 x 등록 지점 x sales/settlements/reviews/ads 상태표가 모두 `succeeded/no_records/action_required` 중 하나로 확정 |
| P0 | 포털 페이지 오류/DOM 변경 | `PORTAL_TABLE_NOT_FOUND`, `AUTHENTICATED_NO_ROWS`, `NO_PARSEABLE_ROWS`가 모두 파서/조회조건/실제 0건을 섞어 표현할 수 있다. | 포털별 fixture HTML/텍스트를 저장하지 않는 안전 샘플로 구축하고, section별 selector 실패와 실제 0건을 분리한다. | DOM fixture 테스트가 배민/쿠팡/요기요/땡겨요 x 4개 record type을 모두 통과 |
| P1 | action_required 운영 UX | challenge screenshot은 파일로 생성되지만 UI/보고서에서 즉시 확인하는 링크와 다음 입력 경로가 약하다. | 상태 카드에 screenshot path, captcha 입력 payload 예시, 재실행 버튼을 노출한다. | 땡겨요 CAPTCHA 발생 시 화면에서 스크린샷 확인 후 같은 work key로 재시도 가능 |
| P1 | DB/JSON 원장 불일치 | 파일 원장과 PostgreSQL 원장을 둘 다 쓰지만 자동 대조 리포트가 없다. | 수집 종료 후 DB row count와 JSON row count, 최신 run_id별 counts를 비교해 mismatch를 `failed`로 승격한다. | 수집 run마다 `ledger_consistency=ok` 또는 mismatch 상세가 상태 원장에 기록 |
| P1 | 장기 스케줄 운영 | 워커 컨테이너가 항상 떠 있는지, 단발 실행인지, 반복 실행인지가 운영 시점마다 달라질 수 있다. | 단발 수집, 반복 수집, 예약 수집을 분리하고 supervisor/docker command를 문서화한다. | `docker ps`, scheduled task, lock 상태만으로 현재 실행 모드를 판정 가능 |

## 13. OTP/CAPTCHA 자동화 운영 설계

판매채널 자동수집의 완전 자동화는 "CAPTCHA/OTP를 모델로 풀어 보안장치를 우회"하는 방식이 아니다. 운영자가 반복하던 판단과 세션 재개를 시스템화하고, 자동 처리 가능한 인증 경로를 먼저 쓰는 방식이다.

| 영역 | 자동화 전략 | 정책 게이트 |
|------|-------------|-------------|
| CAPTCHA 발생 억제 | 업무 전용 `oby-ceo` PC Agent 프로필, 포털별 work key, keep-open 세션, 과도한 재로그인 방지 | IP/기기 위장, CAPTCHA 자동 판독 차단 |
| OTP/2차 인증 | 푸시 승인 완료 polling, 신뢰기기 등록 상태 확인, CEO가 입력한 OTP 제출 결과 감지 후 자동 재개 | OTP 생성/대리 입력/외부 기기 조작 차단 |
| 기기 인증 | 기존 신뢰 브라우저 재사용, 기기등록 안내 화면 감지, 승인 후 동일 세션 재개 | 신규 기기 무한 등록 시도 차단 |
| 약관/공지 팝업 | 허용된 닫기/확인 selector만 클릭 | 결제/권한 변경/약관 동의는 approval 필요 |
| 소형 모델 | 화면 상태 분류, 버튼/입력칸 후보 추천, 파서 실패 원인 bucket 분류 | 정답 생성, 민감값 요구, 금액/거래 row 생성 차단 |

P0 추가 모듈:

| 모듈 | 책임 | 적용 범위 |
|------|------|-----------|
| `PortalAuthChallengeOrchestrator` | 포털 인증 화면을 `auto_resumable`, `approval_required`, `manual_only`, `blocked_by_policy`로 분류 | 배민/쿠팡이츠/요기요/땡겨요 공통 |
| `SafePortalScreenJudge` | DOM/스크린샷 안전 요약으로 CAPTCHA/OTP/로그인/거래표/오류 페이지를 판정 | 소형 모델 또는 규칙 기반 교체 가능 |
| `PortalActionGate` | selector allowlist와 정책에 맞는 클릭/focus/Vault 입력만 실행 | 포털별 adapter 뒤 공통 |
| `CollectionCompletionMatrix` | 계정 x 서비스 x `sales/settlements/reviews/ads` 완료 상태를 확정 | "row 일부 존재" 오판 방지 |

완료 기준은 `전체 자동수집 성공` 하나가 아니라 다음 4개가 모두 충족되는 상태다.

| 완료 항목 | 판정 기준 |
|----------|-----------|
| 인증 자동화 | 자동 가능 항목은 `auto_resumable`로 재개되고, 차단 항목은 `blocked_by_policy` 또는 `approval_required`로 명확히 남음 |
| 세션 안정성 | 한 run 안에서 관리형 브라우저 세션이 work key별 1개 이하로 유지됨 |
| 데이터 커버리지 | 등록 계정별 `sales/settlements/reviews/ads`가 `succeeded/no_records/action_required/blocked_by_policy` 중 하나로 확정 |
| 운영 승인 | 자동/수동 모드 변경과 승인 이벤트가 상태 ledger에 남음 |

## 14. 재사용 스킬

반복 운영과 타 프로젝트 이식을 위해 `.claude/skills/sales-channel-collector/SKILL.md`를 추가했다. 이 스킬은 다음 요청에 사용한다.

- 판매채널 자동수집 실행/재시도
- 새 판매채널 collector 추가
- PC Agent 브라우저 세션 문제 진단
- 수집 원장/상태/업로드 폴백 검수
- FOOD 외 프로젝트로 collector 패턴 이전
