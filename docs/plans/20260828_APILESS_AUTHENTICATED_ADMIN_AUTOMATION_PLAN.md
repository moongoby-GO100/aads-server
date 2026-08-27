# API-less Authenticated Admin Automation Plan

문서 ID: AADS-APILESS-AUTH-ADMIN-AUTOMATION-PLAN-20260828  
작성 기준: 2026-08-28 08:17:48 KST  
대상: AADS / OHVIS / 매장비서 / API가 제공되지 않는 외부 관리자 사이트  
상태: 기획서 우선 작성, 구현 전 실행 기준 문서

## 1. 요약

API가 제공되지 않는 웹사이트의 로그인 어드민 자동화는 단순 스크래핑이 아니라 **인증된 브라우저 작업 실행 플랫폼**으로 설계한다. AADS/OHVIS는 PC Agent, Browser Bridge, Browser Task, Agent Vault, 승인 토큰, 감사 로그 기반을 이미 보유하므로, 다음 단계는 사이트별 반복 업무를 `BrowserRecipe`로 표준화하고 승인 범위 안에서 수집, 파싱, 업로드, 제출, 검증까지 자동 수행하는 구조를 완성하는 것이다.

핵심 원칙은 다음과 같다.

1. API가 없으면 로그인된 브라우저 화면, 다운로드 파일, 네트워크 응답, DOM 테이블을 합법적 사용자 권한 범위 안에서 수집한다.
2. OTP/CAPTCHA/인증서/결제/게시/삭제/업로드는 무조건 차단이 아니라, CEO 또는 권한자가 승인한 페이지, 업무, 횟수, 만료시간, 액션 범위 안에서 자동화한다.
3. CAPTCHA 우회 금지는 승인 없는 비밀 판독, 외부 solver, 회피성 접근을 금지한다는 뜻이다. 승인된 특정 페이지의 숫자 CAPTCHA를 LLM/비전 모델로 판독해 입력하는 것은 승인 로그와 범위 제한이 있으면 자동화 대상이다.
4. 비밀번호, OTP 값, CAPTCHA 정답, 인증서 비밀번호는 로그와 DB에 저장하지 않고 transient 값으로만 사용한다.
5. 모든 자동화는 누가, 언제, 어떤 사이트, 어떤 페이지, 어떤 업무, 어떤 파일, 어떤 승인 범위로 실행했는지 감사 로그를 남긴다.

## 2. 이전 대화 반영 사항

| 주제 | 반영 내용 | 현재 문서에서의 위치 |
|---|---|---|
| Browserbase 유료 대안 | 외부 관리형 브라우저 대신 OHVIS 자체 PC Agent/Browser Bridge 기반을 우선한다 | 4장, 12장 |
| Aside 벤치마킹 | 로그인된 실제 브라우저, 작업 모드, Vault, 권한 게이트, 로컬 메모리, 루틴을 참고한다 | 4장 |
| API 없는 어드민 | 브라우저 세션, DOM/다운로드/네트워크 응답, 파일 업로드까지 표준화한다 | 5장, 6장 |
| 위험 액션 자동화 | 승인 토큰 기반으로 결제/게시/삭제/업로드/CAPTCHA를 자동화한다 | 7장 |
| OTP/CAPTCHA 정책 정정 | 승인 없는 우회는 금지하되 승인된 범위 안의 모델 판독/입력은 허용한다 | 8장 |
| 땡겨요 숫자 CAPTCHA | 승인 scope 안에서 비전 모델 판독과 자동입력을 허용하고 로그를 남긴다 | 8장, 11장 |
| 로컬 PC 의존 | P0는 로컬 PC Agent, P1은 자체 서버형 브라우저 풀, P2는 외부 샌드박스 선택지로 둔다 | 12장 |

## 3. 목표와 비목표

### 3.1 목표

| 목표 | 설명 | 성공 기준 |
|---|---|---|
| 로그인 자동화 | Vault에 저장된 계정으로 어드민 로그인 | 비밀번호 로그 노출 0건 |
| 세션 유지 | work_key별 브라우저 프로필과 storage state 재사용 | 동일 사이트 반복 로그인 빈도 감소 |
| 자료 수집 | DOM 테이블, 다운로드 CSV/XLSX/PDF, 화면 텍스트, 네트워크 응답 수집 | 원장 row와 원본 증빙 매핑 |
| 자료 업로드 | 파일 선택, 폼 입력, 제출, 결과 확인 | 업로드 대상 파일 hash와 결과 URL/번호 저장 |
| 승인형 위험 액션 | CAPTCHA/OTP/인증서/게시/결제/삭제/업로드를 승인 범위 안에서 자동 수행 | 승인 범위 초과 실행 0건 |
| 감사 추적 | 승인, 실행, 입력, 제출, 다운로드, 오류 이벤트 기록 | 사후에 누가/언제/무엇을 했는지 재구성 가능 |
| 재시도/복구 | 세션 만료, UI 변경, 네트워크 실패, 챌린지 발생 시 중단/재개 | 같은 작업 재개 가능 |

### 3.2 비목표

| 비목표 | 이유 |
|---|---|
| 사이트 보안장치 무단 우회 | 법적/계약상 리스크가 크고 CEO 지시와 충돌 |
| 외부 CAPTCHA solver 기본 연동 | 승인/감사/출처 통제가 어렵고 우회성으로 해석될 수 있음 |
| 전체 사이트 범용 무인 자동화 | UI 변경과 인증 정책이 사이트마다 달라 레시피/검증 기반이 필요 |
| 비밀번호/OTP/CAPTCHA 정답 영구 저장 | 민감값 노출 리스크 |

## 4. 현재 AADS/OHVIS 보유 기반

| 기반 | 현재 역할 | 활용 방향 |
|---|---|---|
| PC Agent | 사용자 PC의 브라우저, 파일, PowerShell/CMD, 스크린샷 제어 | 로그인된 실제 세션 작업 |
| Browser Bridge | work_key별 브라우저 세션 연결과 화면 제어 | 업무별 세션 격리 |
| Browser Task API | 작업 생성, 상태, 이벤트, 승인 대기 흐름 | 자동화 작업 큐 |
| Agent Vault | 계정/비밀번호 보관과 자동입력 기반 | LLM 비노출 로그인 |
| Permission Policy | 위험 액션 ask/deny/allow 분류 | 승인 게이트 |
| Approval Token | 승인 scope, 실행 횟수, 만료, origin 제한 | 반복 자동화 승인 |
| Browser Task Events | 작업 이벤트 감사 로그 | 운영 추적과 장애 분석 |
| Yeoljeong 수집기 | 배민/쿠팡이츠/요기요/땡겨요 수집 경로 | 첫 실전 적용 도메인 |

## 5. 전체 아키텍처

```text
CEO Chat / OHVIS UI
        |
        v
Browser Automation Gateway
 - 업무 분류
 - 사이트/레시피 선택
 - 권한 모드 결정
 - 승인 필요 액션 감지
        |
        +----------------------+----------------------+
        |                      |                      |
        v                      v                      v
BrowserRecipe Registry   Agent Vault Service    Approval Service
 - 사이트별 단계          - 계정 매칭             - 요청/승인/반려
 - selector 후보          - write-only autofill   - token scope
 - 파서/업로드 규칙        - 비밀값 masking        - 감사 로그
        |                      |                      |
        +----------------------+----------------------+
                               |
                               v
Browser Runtime
 - PC Agent Browser Bridge
 - 향후 서버형 Playwright pool
 - 향후 외부 샌드박스 adapter
                               |
                               v
Collectors / Uploaders / Verifiers
 - DOM parser
 - file parser
 - network response capture
 - upload result verifier
 - DB writer
```

## 6. 실행 플로우

### 6.1 수집 플로우

| 단계 | 동작 | 실패 처리 |
|---|---|---|
| 1. Task 생성 | 사이트, 사업자, 지점, 기간, 수집 범위 입력 | 필수값 누락 시 생성 거부 |
| 2. 세션 확보 | work_key 기준 기존 브라우저 세션 재사용 또는 새 세션 생성 | PC Agent offline이면 대기 |
| 3. 로그인 | Vault autofill, 로그인 버튼 클릭, 성공 URL/DOM 확인 | MFA/CAPTCHA 감지 시 승인/입력 플로우 |
| 4. 메뉴 이동 | 레시피에 정의된 메뉴 탐색, 검색 기간 입력 | selector 실패 시 fallback selector/LLM 화면 분석 |
| 5. 자료 확보 | DOM 표, 다운로드 파일, 네트워크 응답 중 우선순위 적용 | 원본 증빙 저장 실패 시 수집 중단 |
| 6. 파싱 | 사이트별 parser로 표준 row 변환 | schema mismatch면 검수 대기 |
| 7. 저장 | 원본 artifact hash와 표준 row를 DB에 저장 | 중복키 충돌 시 upsert 정책 적용 |
| 8. 검증 | row 수, 금액 합계, 기간, 지점 매핑 검증 | 불일치 시 partial 상태와 diff 저장 |

### 6.2 업로드 플로우

| 단계 | 동작 | 실패 처리 |
|---|---|---|
| 1. 업로드 후보 생성 | 파일 경로, hash, 문서 종류, 대상 사이트/폼 결정 | hash 미확정 파일은 승인 불가 |
| 2. Preview | 업로드할 파일명, 크기, hash, 대상 URL, 예상 버튼 표시 | preview 불가 시 실행 불가 |
| 3. 승인 | CEO/권한자가 origin, selector, file_hash, max_executions 승인 | 만료/반려 시 task failed |
| 4. 입력/첨부 | 승인된 selector에 파일 첨부, 폼 값 입력 | selector 불일치 시 중단 |
| 5. 제출 | 승인된 버튼만 클릭 | 제출 직전 화면 snapshot 저장 |
| 6. 결과 확인 | 접수번호, 완료 메시지, 다운로드 영수증, URL 확인 | 결과 미확정 시 manual review |
| 7. 감사 로그 | 승인자, 파일 hash, 제출 URL, 결과 저장 | 민감값 제외 |

## 7. BrowserRecipe 설계

`BrowserRecipe`는 사이트별 자동화 절차를 코드에 흩뿌리지 않고 데이터로 관리하는 단위다.

| 필드 | 설명 |
|---|---|
| `recipe_id` | 예: `delivery.ddangyo.sales_collect.v1` |
| `service` | 사이트/서비스 키 |
| `allowed_origins` | 허용 도메인 목록 |
| `work_key_template` | 업무별 세션 격리 키 |
| `login_steps` | 로그인 폼 탐색, Vault 입력, 제출, 성공 판정 |
| `challenge_policy` | CAPTCHA/OTP/인증서 감지와 승인 흐름 |
| `navigation_steps` | 메뉴 이동, 기간 입력, 조회 |
| `capture_rules` | DOM, file download, network response 우선순위 |
| `parser_id` | 정규화 parser |
| `upload_rules` | 파일 입력 selector, 제출 버튼, 결과 판정 |
| `risk_actions` | approval token이 필요한 액션 목록 |
| `verifier` | 완료 기준 |
| `fallbacks` | selector 후보, 화면분석, 사용자 승인 요청 |
| `version_hash` | 승인 토큰과 연결되는 레시피 해시 |

## 8. 승인형 위험 액션 정책

| 액션 | 승인 전 가능 | 승인 후 가능 | 금지선 |
|---|---|---|---|
| CAPTCHA | 감지, 화면 저장, 승인 요청 | 승인된 origin/page/task 안에서 모델 판독, 입력, 제출, 재시도 | 승인 없는 판독, 외부 solver, 회피성 접근 |
| OTP | 입력칸 감지, 사용자 입력 요청, 푸시 승인 대기 | 사용자가 제공한 OTP 주입, 공식 TOTP/API, 푸시 승인 완료 감지 | 문자/앱 무단 열람, LLM 임의 생성 |
| 인증서 | 인증서 화면 감지, 별칭 preview | 승인된 인증서 선택, Vault write-only 비밀번호 입력 | 인증서 파일/비밀번호 노출 |
| 업로드 | 파일 hash/대상 폼 preview | 승인된 파일과 selector만 업로드 | 승인 후 파일 변경, 다른 사이트 업로드 |
| 게시/전송 | 본문/첨부/대상 preview | content hash가 같은 경우 게시 | 승인 후 본문 변조 |
| 결제/이체 | 금액/수취인/건수 preview | 한도/대상 내 제출 | 한도 초과, 대상 변경 |
| 삭제/취소 | 대상 ID와 영향 preview | 승인된 ID만 처리 | wildcard, 전체 삭제, rollback 없는 대량 삭제 |

## 9. OTP/CAPTCHA 상세 정책

### 9.1 CAPTCHA

CAPTCHA는 자동화 차단 대상이 아니라 **승인 범위 제어 대상**이다.

| 상태 | 처리 |
|---|---|
| 승인 없음 | 모델 판독, 입력, 제출 금지. 승인 요청 생성 |
| 승인 있음 | 승인된 origin, page_url, work_key, task_id, challenge_kind가 일치하면 비전 모델 판독과 자동입력 허용 |
| 판독 실패 | 제한 횟수 내 재시도, 이후 사용자 확인 요청 |
| 값 저장 | 정답 값은 저장 금지. 로그에는 `captcha_value=***MASKED***`만 기록 |
| 감사 | 승인자, 승인시각, 페이지, 스크린샷 hash, 모델명, 성공/실패 상태 기록 |

### 9.2 OTP

OTP는 무단 읽기나 LLM 생성이 금지된다. 자동화 가능한 범위는 다음이다.

| 방식 | 자동화 가능 여부 | 조건 |
|---|---|---|
| 사용자가 입력한 OTP | 가능 | transient로 1회 사용, 저장 금지 |
| 앱 푸시 승인 | 가능 | 사용자가 앱에서 승인한 결과를 polling/화면 감지 |
| 공식 TOTP provider | 가능 | 사용자가 등록/승인한 계정과 scope 필요 |
| SMS/메일함 무단 열람 | 금지 | 별도 명시 승인과 공식 접근권한 없이는 불가 |
| LLM 임의 추정 | 금지 | 인증값 생성 불가 |

## 10. 파싱 전략

| 소스 | 장점 | 리스크 | 우선순위 |
|---|---|---|---|
| 다운로드 CSV/XLSX | 구조 안정적, 증빙성 높음 | 다운로드 메뉴 필요 | 1 |
| 네트워크 응답 JSON/CSV | API 문서가 없어도 화면 내부 데이터 활용 가능 | 토큰/권한 취급 주의 | 2 |
| DOM 테이블 | 가장 범용적 | UI 변경에 취약 | 3 |
| PDF/이미지 OCR | API/표가 없을 때 fallback | 정확도 검증 필요 | 4 |
| LLM 화면 파싱 | 비정형 화면 대응 | 비용/오판 리스크 | 5 |

파싱 결과는 반드시 원본 artifact와 연결한다.

| 저장 항목 | 설명 |
|---|---|
| `artifact_hash` | 원본 파일/HTML/screenshot hash |
| `source_url` | 수집 당시 URL |
| `captured_at` | 수집 시각 |
| `parser_id` | 사용 parser |
| `parser_version` | parser 버전 |
| `normalized_rows` | 표준화 결과 |
| `validation_summary` | row 수, 합계, 기간, 누락 |

## 11. 첫 적용 후보

| 우선 | 사이트/업무 | 이유 | 완료 기준 |
|---|---|---|---|
| P0 | 땡겨요 매출/정산/리뷰 수집 | 숫자 CAPTCHA와 승인형 자동입력 요구가 명확함 | 승인 로그 후 CAPTCHA 판독/입력, 수집 row 저장 |
| P0 | 배민/쿠팡이츠/요기요 정산 수집 | 매장비서 핵심 업무 | 로그인 세션 재사용, 파일/DOM 파싱 성공 |
| P1 | 은행 거래내역 조회/파일 다운로드 | 재무 자동화 핵심 | OTP/인증서 승인 후 거래내역 원장화 |
| P1 | 홈택스 세금계산서/증빙 수집 | 세무 자동화 핵심 | 인증서/간편인증 승인 후 증빙 다운로드 |
| P1 | 매입처 포털 발주서/거래명세서 업로드 | 반복 업로드 수요 | 승인된 파일 hash만 업로드 |
| P2 | 게시/전송 업무 | 마케팅/고객 안내 자동화 | content hash 승인 후 게시 URL 저장 |

## 12. 로컬 PC 의존 해소 로드맵

| 단계 | 방식 | 장점 | 단점 | 판정 |
|---|---|---|---|---|
| P0 | CEO PC Agent + Browser Bridge | 로그인/인증/로컬 인증서 처리에 강함 | PC online 필요 | 즉시 적용 |
| P1 | 자체 서버형 Playwright Worker Pool | 반복 수집 안정화, 비용 통제 | 금융/인증서/로컬 세션 한계 | 추가 구현 |
| P1 | 사내 원격 브라우저 VM | 세션 지속성과 격리 우수 | 운영 비용/보안 관리 필요 | 검토 |
| P2 | Browserbase/Browserless류 외부 샌드박스 | 빠른 확장, 관측성 제공 | 유료, 데이터/인증 외부 위탁 | 보조 옵션 |
| P2 | Firecrawl류 크롤러 | 공개 페이지 수집에 강함 | 로그인/업로드/승인형 액션에는 한계 | 보조 옵션 |

권장안은 P0 로컬 PC Agent를 제품화하고, P1에서 자체 Playwright Worker Pool을 붙여 로컬 PC 없이도 가능한 사이트를 분리 처리하는 것이다. 금융인증, 공동인증서, CEO PC에만 있는 로그인 세션은 계속 PC Agent 경로를 우선한다.

## 13. 멀티 작업 동시성 및 리소스 배분 설계

API 없는 로그인 어드민 자동화는 사이트별 세션, 파일 다운로드, 업로드, 결제/게시/삭제 승인, CAPTCHA/OTP 입력이 서로 얽힌다. 따라서 단순히 브라우저를 여러 개 띄우는 방식이 아니라, `BrowserRecipe`마다 동시성 정책과 리소스 예산을 고정해 작업 충돌을 제어한다.

### 13.1 동시성 원칙

| 원칙 | 적용 방식 | 이유 |
|---|---|---|
| 같은 계정/같은 사이트 기본 1개 실행 | `conflict_keys=["work_key","origin"]`, `max_parallel_runs=1` | 로그인 세션 강제 로그아웃, CAPTCHA 재발, 중복 제출 방지 |
| 조회성 수집은 제한 병렬 허용 | 서비스가 허용하면 `max_parallel_runs=2~3` | 여러 지점/기간 수집 처리량 확보 |
| 업로드/게시/삭제/결제는 직렬 처리 | risky action이 있는 recipe는 기본 `max_parallel_runs=1` | 승인 scope와 실제 제출 대상 불일치 방지 |
| 큐 전략을 레시피별 지정 | `fifo`, `priority`, `latest_only`, `reject_on_conflict` | 정산 수집은 누적, 상태조회는 최신 작업만 필요 |
| 승인 토큰은 recipe_hash에 묶음 | 승인 당시 버전과 실행 버전 불일치 시 중단 | 승인 후 selector/대상 변경 방지 |

### 13.2 런타임 배분

| 런타임 | 사용 조건 | 동시성 기본값 | 리소스 기준 |
|---|---|---:|---|
| `pc_agent` | 로컬 인증서, CEO PC 로그인 세션, 강한 MFA 필요 | 1 | PC 화면/브라우저 독점 가능성 고려 |
| `self_hosted_playwright` | ID/PW 로그인, 파일 다운로드, 공개/준공개 어드민 | 3 | 컨텍스트별 메모리 512~1,024MB |
| `external_sandbox` | 비민감 테스트, 확장성 비교 PoC | 5 | 비용/개인정보 정책 검토 후 제한 |
| `auto` | 레시피가 런타임을 선택 | 정책 기반 | 실패 시 PC Agent 또는 self-hosted로 fallback |

### 13.3 리소스 정책 필드

| 필드 | 의미 | 기본값 |
|---|---|---:|
| `runtime` | `pc_agent`, `self_hosted_playwright`, `external_sandbox`, `auto` | `auto` |
| `max_browser_contexts` | 레시피 실행당 브라우저 컨텍스트 상한 | 1 |
| `max_memory_mb` | 작업별 메모리 예산 | 1,024 |
| `max_runtime_seconds` | 작업 최대 실행 시간 | 900 |
| `artifact_budget_mb` | 다운로드/HTML/screenshot 원본 저장 예산 | 256 |

### 13.4 큐와 충돌 처리

| 상황 | 처리 |
|---|---|
| 같은 `work_key+origin` 수집 작업 2개 | 앞 작업 완료 후 FIFO 실행 |
| 같은 사이트 상태조회 2개 | `latest_only`면 이전 queued 작업 취소 가능 |
| 업로드/게시/삭제/결제 작업 중복 | `reject_on_conflict` 또는 승인 재요청 |
| 승인 대기 중 같은 대상 재실행 | 기존 승인 요청을 재사용하거나 중복 요청 차단 |
| self-hosted pool 리소스 초과 | queued 유지, 작업 콘솔에 대기 사유 기록 |

### 13.5 구현 반영

이번 P0 구현은 `browser_recipes`에 `concurrency_policy`, `resource_policy`, `runtime_policy`, `version_hash`를 저장한다. `dry-run`은 실제 브라우저를 띄우기 전에 승인 필요 액션, 차단 액션, 런타임, 리소스 예산, 병렬 실행 정책을 계산해 작업 충돌과 비용을 사전 확인한다.

저장된 레시피는 `run-plan` 단계에서 현재 `browser_recipe_runs`의 활성 실행 수를 기준으로 시작/대기/거부를 판정한다. 판정 결과는 `concurrency_key`, `active_runs`, `max_parallel_runs`, `queue_strategy`, `resource_claim`을 포함한다. 실행 생성 API는 승인된 슬롯이면 `running`, 한도 초과면 `queued`, `reject_on_conflict` 정책이면 `409 conflict`로 응답한다. `latest_only` 정책은 같은 concurrency key의 기존 queued 실행을 `superseded` 처리하고 최신 요청만 유지한다.

## 14. DB/API 설계 초안

### 14.1 주요 테이블

| 테이블 | 목적 |
|---|---|
| `browser_recipes` | 사이트별 자동화 레시피와 버전 hash |
| `browser_recipe_runs` | 레시피 실행 이력 |
| `browser_tasks` | 브라우저 작업 상태 |
| `browser_task_events` | 감사 이벤트 |
| `agent_permission_requests` | 승인 요청 |
| `browser_approval_tokens` | 승인 scope와 실행 제한 |
| `browser_artifacts` | 다운로드/HTML/screenshot/PDF 원본 증빙 |
| `browser_parse_results` | parser 결과와 검증 요약 |
| `browser_upload_results` | 업로드 결과, 접수번호, URL |

### 14.2 주요 API

| API | 목적 |
|---|---|
| `POST /api/v1/browser-recipes` | 레시피 등록/버전 생성 |
| `GET /api/v1/browser-recipes` | 레시피 목록 조회 |
| `GET /api/v1/browser-recipes/{id}/versions/{version}` | 특정 버전 조회 |
| `POST /api/v1/browser-tasks` | 작업 생성 |
| `GET /api/v1/browser-tasks/{id}` | 작업 상태/이벤트 조회 |
| `POST /api/v1/browser-tasks/{id}/approve` | 승인 scope 발급 |
| `POST /api/v1/browser-tasks/{id}/resume` | 승인 후 재개 |
| `POST /api/v1/browser-tasks/{id}/cancel` | 중단 |
| `GET /api/v1/browser-artifacts/{id}` | 원본 증빙 다운로드/보기 |
| `POST /api/v1/browser-recipes/dry-run` | 저장 전 레시피 동시성/리소스/승인 정책 검증 |
| `POST /api/v1/browser-recipes/{id}/versions/{version}/run-plan` | 저장된 레시피 기준 현재 실행 슬롯/자원 배분 판정 |
| `POST /api/v1/browser-recipes/{id}/versions/{version}/runs` | 슬롯 판정 후 `browser_recipe_runs` 실행 레코드 생성 |

## 15. 운영 화면 설계

| 화면 | 기능 |
|---|---|
| Browser Task Console | 현재 URL, 단계, 스크린샷, 로그, 승인 대기 사유 표시 |
| Approval Inbox | CAPTCHA/OTP/업로드/결제/게시/삭제 승인 카드 |
| Recipe Manager | 사이트별 레시피 버전, selector, parser, verifier 관리 |
| Artifact Viewer | 다운로드 파일, HTML snapshot, screenshot, parser 결과 비교 |
| Session Manager | work_key별 브라우저 세션, 로그인 상태, 만료, 재시작 |
| Audit Log | 승인자, 실행자, page_url, action, 결과 추적 |

## 16. 구현 우선순위

| 우선 | 과제 | 산출물 | 검증 |
|---|---|---|---|
| P0-1 | BrowserRecipe 스키마/registry | DB migration, service, API, dry-run | recipe CRUD/dry-run 테스트 |
| P0-1a | 동시성/리소스 정책 | `concurrency_policy`, `resource_policy`, `version_hash` | 병렬 한도/런타임 예산 정규화 테스트 |
| P0-2 | 승인형 CAPTCHA/업로드 공통 게이트 | approval token consume 강제 | 승인 없음 차단, 승인 있음 실행 |
| P0-3 | 땡겨요 레시피 v1 | 로그인, CAPTCHA, 매출/정산/리뷰 수집 | 실제/모의 세션 테스트 |
| P0-4 | Artifact 저장소 | 원본 HTML/파일/screenshot hash 저장 | 원본-파싱 row 연결 |
| P0-5 | Task Console 최소 UI | 상태, screenshot, 승인 버튼 | 수동 승인 후 resume |
| P1-1 | 파일 업로드 레시피 | file_hash allowlist, upload verifier | 승인 파일만 업로드 |
| P1-2 | 은행/홈택스 protected recipe | 인증서/OTP 승인 흐름 | action_required/retry |
| P1-3 | 자체 Playwright Worker Pool | 로컬 PC 불필요 사이트 분리 | 병렬 작업 3개 안정 실행 |
| P2-1 | 관리형 브라우저 adapter 비교 | Browserbase/Browserless/자체 VM adapter | 비용/성공률 비교 |

## 17. 검증 기준

| 검증 항목 | 성공 기준 |
|---|---|
| 로그인 | Vault autofill 후 비밀번호 로그 노출 0건 |
| 승인 | 승인 없는 risky action 실행 0건 |
| CAPTCHA | 승인 없으면 차단, 승인 있으면 모델 판독/입력/재판정 이벤트 기록 |
| OTP | 저장/무단 읽기 없이 transient 입력 또는 공식 승인 감지만 사용 |
| 파싱 | 원본 artifact와 parser result가 1:1 연결 |
| 업로드 | 승인된 file_hash만 제출 |
| 감사 로그 | 승인자, 승인시각, origin, page_url, action, result 기록 |
| 재현성 | 같은 recipe_hash로 dry-run과 실제 실행 비교 가능 |
| 실패 복구 | 세션 만료/selector 실패/챌린지 발생 시 resume 가능 |
| 동시성 | 같은 `work_key+origin` 위험 작업 중복 실행 0건 |
| 리소스 | recipe별 runtime/memory/time/artifact 예산 초과 시 queued/failed로 기록 |

## 18. 리스크와 대응

| 리스크 | 영향 | 대응 |
|---|---|---|
| 사이트 약관 위반 | 계정 차단/법적 리스크 | 사이트별 허용 범위 검토, 사용자 권한 내 작업만 수행 |
| UI 변경 | 수집 실패 | selector 후보, LLM 화면분석 fallback, recipe versioning |
| 인증 만료 | 작업 중단 | 승인 요청, push 알림, 같은 세션 resume |
| 민감값 노출 | 보안 사고 | transient input, masking, secret scanner |
| 잘못된 업로드/게시 | 업무 사고 | preview, content/file hash, 승인 토큰, verifier |
| 외부 샌드박스 데이터 노출 | 개인정보/계정 리스크 | P0는 로컬 우선, 외부는 비민감 사이트부터 |
| 과도한 병렬 브라우저 실행 | 서버/PC 메모리 고갈 | recipe별 `max_browser_contexts`, `max_memory_mb`, queue backpressure |
| 같은 계정 중복 로그인 | 세션 만료/차단 | conflict key로 work_key+origin 직렬화 |

## 19. 즉시 실행 지시서 초안

```text
TASK_ID: AADS-APILESS-AUTH-AUTOMATION-P0-20260828
TITLE: API 없는 로그인 어드민 자동화 P0 - BrowserRecipe/승인게이트/Artifact 기반 구축
PRIORITY: P0
SIZE: L
MODE: code_modify_verify

DESCRIPTION:
1. browser_recipes, browser_artifacts, browser_parse_results, browser_upload_results additive migration을 작성한다.
2. BrowserRecipe registry service와 API를 추가한다.
3. Browser Task 실행 전에 risky action approval token consume을 강제한다.
4. CAPTCHA는 승인 없는 모델 판독을 차단하고, 승인된 scope에서는 모델 판독/자동입력을 허용한다.
5. 파일 업로드는 file_hash, origin, selector, max_executions 승인 scope를 강제한다.
6. 땡겨요 수집 레시피 v1을 등록하고 기존 Yeoljeong 수집 경로와 연결한다.
7. browser_task_events에 승인자/승인시각/page_url/action/result를 남기고 민감값은 마스킹한다.
8. 단위 테스트와 py_compile을 통과시킨다.

VERIFY:
- pytest tests/unit/test_browser_task_policy.py
- pytest tests/unit/test_yeoljeong_finance_service.py -k "captcha or ddangyo"
- python3 -m py_compile app/api/browser_tasks.py app/api/browser_recipes.py app/services/browser_task_gateway.py app/services/browser_permission_policy.py app/services/browser_recipe_registry.py
```

## 20. 완료 판정

이 기획의 P0 완료는 다음 조건을 모두 만족해야 한다.

1. 레시피가 DB에 저장되고 특정 사이트/업무/버전으로 실행된다.
2. 로그인 후 수집/업로드 작업이 Browser Task 이벤트로 추적된다.
3. CAPTCHA/업로드/게시/삭제/결제는 승인 토큰 없이는 실행되지 않는다.
4. 승인된 CAPTCHA는 모델 판독과 자동입력이 가능하다.
5. 원본 artifact와 정규화 row 또는 업로드 결과가 연결된다.
6. 운영자가 작업 콘솔에서 현재 단계, 승인 대기 사유, 결과를 확인할 수 있다.
7. 민감값이 로그/DB/스크린샷 OCR 결과에 남지 않는다.
