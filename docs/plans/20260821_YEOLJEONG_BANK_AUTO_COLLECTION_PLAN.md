# 은행데이터 자동수집 기획서

- 작성 시각: 2026-08-21 14:27 KST
- 최종 수정: 2026-08-21 15:33 KST
- 대상: FOOD/열정국밥 매장비서 `yeoljeong-finance`
- 목적: 은행 입출금 데이터를 자동 수집해 배달앱 정산, 카드/PG, 수기 매출, 지출 증빙과 대사 가능한 운영 원장으로 만든다.
- 현재 판정: P0 코어와 PC Agent 은행 기업페이지 자동 오픈, 비밀번호관리자 focus fallback, 자동수집 루프 편입은 구현되어 있다. 완전 자동화 목표는 "공식 연동/API/조회전용 계정/브라우저 신뢰 세션/암호화 Vault/상태 머신/소형 모델 검증"을 조합해 사람 반복입력을 없애는 것이다. OTP/CAPTCHA/본인인증은 무조건 수동으로 멈추지 않고 자동 가능 경로를 먼저 수행하되, CAPTCHA 자동 판독·보안장치 우회·허가 없는 OTP 대리 생성처럼 계정 차단이나 약관 위반 위험이 큰 동작은 정책 게이트에서 차단하고 운영 승인 대상으로 남긴다.

## 1. 목표

은행 자동수집의 목표는 단순 거래내역 저장이 아니라 매장 운영에 필요한 현금흐름 대사 체계를 만드는 것이다. 매출 채널별 정산 예정액과 실제 은행 입금액을 일자/금액/입금자 기준으로 연결하고, 미입금·과소입금·수수료 차이·미분류 지출을 대표자가 바로 확인할 수 있게 한다.

핵심 완료 기준은 다음이다.

| 구분 | 완료 기준 |
|---|---|
| 데이터 수집 | 등록된 은행계좌별 거래내역을 날짜 범위 기준으로 수집한다. |
| 멱등성 | 같은 거래내역을 반복 수집해도 중복 원장 row가 생기지 않는다. |
| 보안 | 원본 계좌번호, 은행 비밀번호, OTP, 인증서 비밀번호, 원본 HTML을 저장하지 않는다. |
| 자동 로그인 | PC Agent 브라우저에서 저장된 비밀번호관리자 또는 암호화 Vault 값을 이용해 ID/PW/계좌비밀번호/인증서 비밀번호 입력을 보조한다. OTP/CAPTCHA/본인인증은 자동처리 가능 여부를 먼저 판정하고 불가능한 최소 단계만 CEO 승인으로 요청한다. |
| 대사 | 배달앱 정산/기존 거래 원장과 입금 거래를 자동 매칭하고 미매칭을 표시한다. |
| 운영성 | PC Agent 세션 필요, 인증 필요, 파서 실패, 거래 없음, 수집 성공을 구분해 상태를 남긴다. |

## 2. 현재 구현 현황

| 영역 | 현재 상태 | 근거 |
|---|---|---|
| 은행 계좌 등록부 | 구현됨. `bank_accounts.json` 파일 원장, 0600 권한 저장, 민감 필드 화이트리스트/차단 목록 적용. | `app/services/yeoljeong_finance_service.py` |
| 은행 거래 원장 | 구현됨. `bank_transactions.json` 파일 원장, `source_hash` 기반 멱등 중복 제거. | `app/services/yeoljeong_finance_service.py` |
| API | 구현됨. `/bank-accounts`, `/bank-accounts/{id}/collect`, `/bank-transactions`, `/bank-transactions/import`, `/bank-summary`. | `app/api/yeoljeong_finance.py` |
| CSV/수동/목업 반영 | 구현됨. 한국어 은행 CSV 헤더를 정규화해 원장 반영. | `app/services/yeoljeong_finance_service.py` |
| PC Agent 브라우저 수집 | 코드 구현됨. 신한/IBK 빠른조회 페이지를 Browser Bridge 세션으로 열고 HTML 테이블을 파싱한다. | `app/services/yeoljeong_bank_browser_connector.py` |
| 자동 로그인 보조 | 부분 구현됨. 거래 테이블이 없으면 로그인 입력란에 focus/input event를 보내 비밀번호관리자 자동완성을 유도한다. | `app/services/yeoljeong_bank_browser_connector.py` |
| 자동수집 루프 통합 | 구현됨. `scripts/yeoljeong_auto_collect.py`가 활성/auto_sync 은행 계좌를 함께 수집한다. | `scripts/yeoljeong_auto_collect.py` |
| 계좌비밀번호 자동입력 | 설계 필요. 현재 은행 계좌 원장에는 민감값을 저장하지 않는 보안 정책이므로, 별도 암호화 Vault 또는 CEO PC 비밀번호관리자 연동으로만 처리해야 한다. | `app/services/yeoljeong_finance_service.py` |
| 매크로/AI 보조 | 운영 설계 반영됨. DOM selector 기반 매크로를 1순위로 쓰고, 소형 모델은 화면 상태 분류·검증 판단·안전한 수정 후보 추천·파서 실패 원인 분류에 제한한다. | 이 문서 4-2, 8-1 |
| 운영 은행 실계정 E2E | 미완료. 기존 WRAP 문서 기준 Shinhan/IBK 실포털 검증은 수행되지 않았다. | `docs/AADS-WRAP-FOOD-BROWSER-P0_20260820.md` |
| DB 승격 | 미완료. 현재는 파일 기반 원장이다. | `docs/handover-notes/2026-08-20_yeoljeong_bank_sync_phase1.md` |

## 3. 수집 대상 데이터

| 데이터 | 필수 필드 | 선택 필드 | 활용 |
|---|---|---|---|
| 계좌 등록 | 사업자, 지점, 은행, 마스킹 계좌번호, 계좌 별칭, 연동 방식, 상태 | 예금주, 기관 코드, 자동수집 여부 | 수집 범위와 권한 통제 |
| 은행 거래 | 거래일시, 입출금 방향, 금액, 은행계좌 ID | 잔액, 거래처, 적요, 원문 적요, 카테고리, source_hash | 입금/출금 원장 |
| 수집 상태 | 계좌 ID, 상태, connector_status, error_code, 수집 건수, 중복 건수, 마지막 수집시각 | diagnostics, browser_work_key | 운영 모니터링 |
| 대사 결과 | 은행 거래 ID, 정산/거래 원장 ID, 매칭 기준, 차이 금액 | 수수료/보류 사유 | 미입금·차액 추적 |

은행별 1차 대상은 신한은행 기업 빠른조회와 IBK기업은행 빠른조회다. 추가 은행은 같은 인터페이스에 `BANK_PORTAL_URLS`, 은행 코드 alias, 파서 fixture, E2E 체크리스트를 붙이는 방식으로 확장한다.

## 4. 권장 아키텍처

```text
계좌 등록/연동관리 UI
  -> /api/v1/yeoljeong-finance/bank-accounts
  -> bank_accounts 원장
  -> 자동수집 루프 또는 수동 수집 버튼
  -> PC Agent / Browser Bridge work session
  -> 은행 빠른조회 페이지에서 비밀번호관리자/Vault/매크로로 로그인 입력 보조
  -> OTP/CAPTCHA/본인인증/인증서 비밀번호 화면은 자동처리 가능 항목 우선 처리
  -> 자동처리 불가 항목만 최소 CEO 승인 요청
  -> 거래내역 HTML table 파싱
  -> record_bank_transactions()
  -> bank_transactions 원장
  -> bank_summary / settlement matching
  -> 입금대사 화면, 알림, 리포트
```

PC Agent 브라우저 방식은 서버 headless 로그인보다 우선한다. 시스템은 업무 전용 브라우저 세션을 열고, 로그인 화면에서는 브라우저 비밀번호관리자 자동완성을 유도하거나 암호화 Vault에 저장된 write-only 값을 PC Agent 세션 안에서만 입력한다. OTP/CAPTCHA/인증서 선택/본인인증 화면이 나오면 즉시 `operator_action_required`로 멈추지 않고, 저장된 인증서 별칭 선택, 인증서 비밀번호 write-only 입력, 앱 푸시 승인 대기, 이미 열린 승인 팝업 확인, CEO가 입력한 OTP 감지 후 자동 재개처럼 합법적이고 허용된 자동처리를 먼저 시도한다. 그래도 자동처리가 불가능한 CAPTCHA 판독, 외부 OTP 기기 조작, 추가 본인확인 같은 단계만 `operator_action_required`로 남기고, 승인 완료 후 같은 세션에서 자동 재수집한다. 세션이 없거나 최종 승인이 필요한 경우 `action_required`로 남기고 CSV 업로드를 대체 경로로 제공한다.

## 4-1. 은행 자동로그인 완전 자동화 설계

완전 자동화는 "모든 보안 절차를 우회한다"는 뜻이 아니라, 사람이 반복 입력하던 정형 입력과 화면 이동을 PC Agent가 대신 수행하고 법적·보안상 사람이 승인해야 하는 단계만 멈추는 구조로 정의한다.

| 단계 | 자동화 수준 | 구현/조치 | 상태 |
|---|---|---|---|
| L1 | 은행 기업페이지 자동 오픈 | `auto_open_browser=true`이면 `yeoljeong-bank-browser-{hash}` work session을 생성하고 신한/IBK 빠른조회 URL을 연다. | 구현됨 |
| L2 | 비밀번호관리자 자동완성 유도 | 테이블이 없고 로그인 화면으로 보이면 ID/PW input에 focus/input event를 보내 브라우저 저장 비밀번호 선택을 유도한다. | 구현됨 |
| L3 | 계좌비밀번호 자동입력 | 계좌비밀번호/조회비밀번호를 AADS 파일 원장에 저장하지 않고 Agent Vault 또는 CEO PC 비밀번호관리자에서 write-only로 가져와 PC Agent 세션의 지정 selector에만 주입한다. | P0 추가 구현 |
| L4 | 매크로 화면 이동 | 로그인 완료 후 "조회", "거래내역", "기간조회", "엑셀/표시" 같은 버튼을 DOM selector 우선, 좌표 매크로 fallback으로 클릭한다. | P0 추가 구현 |
| L5 | 인증 챌린지 자동처리 | OTP/CAPTCHA/본인인증/인증서 비밀번호 화면을 `auto_resumable`, `operator_input_needed`, `blocked_by_policy`로 분류하고 자동 가능한 단계만 수행한다. | P0 추가 구현 |
| L6 | 소형 모델 실시간 보조 | 빠른 모델로 화면 상태를 `login_required`, `otp_required`, `captcha_required`, `identity_check_required`, `certificate_password_required`, `transaction_table`, `no_records`, `parse_failed`로 분류하고 다음 안전 액션 후보를 낸다. | P0 추가 구현 |
| L7 | 운영 승인 정책 | 계좌/포털별 `auto`, `approval_required`, `manual_only` 모드를 두고 정책상 차단되는 보안 우회는 실행하지 않는다. | P0 추가 구현 |

자동로그인 보강 API 계약:

| 입력 | 설명 | 저장 정책 |
|---|---|---|
| `bank_login_id` | 은행 빠른조회 ID 또는 기업뱅킹 ID | Agent Vault 암호화, API 응답 미노출 |
| `bank_login_password` | 로그인 비밀번호 | Agent Vault 암호화, API 응답 미노출 |
| `account_password` | 계좌 조회 비밀번호/간편조회 비밀번호 | Agent Vault 암호화, API 응답 미노출 |
| `certificate_profile` | 인증서 별칭 또는 선택 힌트 | 민감값 없이 별칭만 저장 |
| `certificate_password` | 공동/금융인증서 비밀번호 | 계좌별 opt-in 시 Agent Vault/CEO PC keychain write-only, API 응답 미노출 |
| `auto_login_enabled` | 자동입력 허용 여부 | 계좌별 opt-in |
| `auth_challenge_policy` | OTP/CAPTCHA/본인인증/인증서 비밀번호 처리 정책 | `auto_if_possible`, `ask_once_then_resume`, `manual_only` 중 선택 |
| `operator_required_steps` | 자동처리 불가로 CEO 승인이 필요한 단계 | 상태·사유·화면 종류만 저장, 값 저장 금지 |
| `automation_mode` | 계좌별 운영 모드 | `auto`, `approval_required`, `manual_only` 중 선택 |
| `approved_auth_methods` | 자동 처리 허용 인증수단 | 공식 API, 조회전용 토큰, 인증서 별칭, Vault write-only 값, 푸시 승인 polling 등 allowlist만 저장 |

자동화 실행 순서:

1. 계좌별 work key로 PC Agent 브라우저 세션을 확보한다.
2. 은행 기업/빠른조회 URL을 열고 현재 화면 상태를 DOM 규칙으로 판정한다.
3. 로그인 폼이면 비밀번호관리자 focus fallback을 먼저 실행한다.
4. 비밀번호관리자 자동완성이 되지 않으면 Agent Vault write-only 자격증명으로 ID/PW를 입력한다.
5. 계좌비밀번호 입력 폼이면 `account_password`를 selector allowlist에만 입력한다.
6. 인증서 선택 화면이면 저장된 `certificate_profile` alias와 allowlist selector로 인증서를 선택한다.
7. 인증서 비밀번호 화면이면 계좌별 opt-in 된 `certificate_password`를 write-only로 입력하고, 저장값이 없거나 실패하면 CEO 1회 입력만 요청한다.
8. OTP/본인인증/앱 푸시 화면이면 브라우저 팝업·푸시 대기·CEO 입력 감지·승인 완료 후 자동 재개를 먼저 수행한다.
9. CAPTCHA처럼 자동 해독/우회가 금지된 단계, 외부 OTP 기기 조작, 추가 본인확인처럼 시스템이 직접 처리할 수 없는 단계만 `operator_action_required`로 남긴다.
10. CEO 승인 또는 입력이 완료되면 동일 work session에서 자동으로 상태를 재판정하고 거래내역 화면까지 계속 진행한다.
11. 거래내역 화면이면 기간을 설정하고 조회 버튼을 누른 뒤 HTML table 또는 다운로드 가능한 CSV/Excel을 파싱한다.
12. `source_hash`로 중복 제거 후 `bank_transactions` 원장에 저장하고 수집 run 상태를 갱신한다.

### 4-1-1. OTP/CAPTCHA 완전 자동화 재기획

CEO 목표는 "운영자가 매번 브라우저를 보고 멈춘 지점을 처리하는 방식"을 없애는 것이다. 따라서 완전 자동화는 아래 3단계로 재정의한다.

| 단계 | 목표 | 허용 동작 | 차단 동작 |
|---|---|---|---|
| A. 발생 억제 | OTP/CAPTCHA가 나오지 않도록 합법적 경로를 우선 사용 | 공식 API/오픈뱅킹/포털 export, 조회전용 계정, 기기 신뢰 등록, 업무 전용 PC Agent 프로필, 세션 keep-open, 적정 수집 주기 | 포털 보안회피, IP/기기 위장, 대량 병렬 로그인 |
| B. 자동 처리 | 허용된 인증수단은 시스템이 끝까지 진행 | 인증서 별칭 선택, 인증서 비밀번호 Vault write-only 입력, 앱 푸시 승인 완료 polling, CEO가 이미 입력한 OTP 감지 후 제출 결과 판정, 공식 TOTP/API 토큰이 제공되는 경우의 조회전용 인증 | CAPTCHA 정답 자동 판독, 허가 없는 OTP 생성/대리 입력, 휴대폰 본인확인 대행 |
| C. 승인 게이트 | 자동 처리가 막힌 최소 단계만 운영 승인으로 전환 | 화면 종류, 사유, 입력 위치, 재개 버튼, 같은 work key 유지 | 민감값 저장, 원본 화면/HTML 장기 보관, 실패 무한 반복 |

소형 모델은 B 단계의 "정답 생성기"가 아니라 A/B/C 분기 판단기다. 모델은 `captcha_visible`, `otp_visible`, `push_waiting`, `trusted_device_prompt`, `certificate_password_required`, `transaction_table_ready` 같은 상태를 빠르게 분류하고, 상태 머신이 허용한 액션만 실행한다.

운영 모드:

| 모드 | 설명 | 사용처 |
|---|---|---|
| `auto` | 공식/허용 인증수단과 Vault allowlist로 자동 진행하고, 정책 차단 단계만 멈춘다. | 조회전용 API, 인증서/Vault 입력, 신뢰된 PC 세션 |
| `approval_required` | 자동 액션 전후에 CEO 승인 이벤트를 남기고 진행한다. | 신규 은행/신규 포털, 처음 1회 검증 |
| `manual_only` | 화면 감지와 재개만 자동화하고 인증 입력은 운영자가 한다. | CAPTCHA 반복, 약관 변경, 계정 잠금 위험 상태 |

P0 구현 단위:

| 모듈 | 역할 | 완료 기준 |
|---|---|---|
| `AuthChallengeOrchestrator` | OTP/CAPTCHA/본인인증/인증서 화면을 상태 머신으로 분류하고 정책을 적용한다. | `auto_resumable`, `approval_required`, `blocked_by_policy` 전이가 run에 기록됨 |
| `SafeScreenJudge` | DOM/스크린샷 요약에서 인증 화면 종류와 신뢰도를 판정한다. | 민감값 제거, JSON schema 검증, confidence 부족 시 자동 실행 차단 |
| `CredentialActionGate` | Vault write-only 값과 selector allowlist를 검증한 뒤 입력/클릭을 실행한다. | 허용 selector 외 입력 0건, 민감값 로그 0건 |
| `SessionContinuityManager` | 같은 work key에서 인증 후 수집을 재개하고 실패 시 세션 재생성을 결정한다. | 인증 완료 후 새 창 폭증 없이 동일 세션 재수집 |
| `OperatorApprovalLedger` | 수동/자동 승인 이벤트, 사유, 재개 결과를 남긴다. | 누가/언제/어떤 모드로 승인했는지 감사 가능 |

인증 챌린지 처리 원칙:

| 화면 | 자동처리 우선순위 | CEO 승인 필요 조건 |
|---|---|---|
| 인증서 선택 | 저장된 인증서 별칭과 selector allowlist로 선택 | 별칭 불일치, 인증서 만료, 새 인증서 등록 |
| 인증서 비밀번호 | 계좌별 opt-in 된 Vault/keychain write-only 입력 | 저장값 없음, 실패 횟수 초과, 비밀번호 변경 필요 |
| OTP 입력 | CEO가 같은 PC/브라우저에 입력하면 감지 후 자동 재개, 푸시형은 승인 완료 상태 polling | OTP 생성·대리 입력 불가, 외부 기기 조작 필요 |
| CAPTCHA | 화면 감지, 스크린샷/상태 기록, 입력칸 focus, CEO 입력 후 자동 재개 | CAPTCHA 판독·우회는 항상 CEO 처리 |
| 본인인증 | 팝업/앱푸시/이미 완료된 승인 상태 자동 감지 후 재개 | 휴대폰 본인확인, 추가 약관동의, 신분 확인 |

금지선:
- CAPTCHA/OTP를 모델로 해독하거나 우회하지 않는다.
- 원본 계좌번호, 원본 HTML, 비밀번호, 인증서 비밀번호, OTP 값을 로그/diagnostics/API 응답에 남기지 않는다.
- `operator_action_required`는 첫 감지 상태가 아니라 자동처리 시도 후 남은 최소 승인 상태에만 사용한다.
- 서버 headless 브라우저에서 은행 로그인을 반복 시도하지 않는다. 은행은 PC Agent 실사용 브라우저 세션을 정본으로 한다.

## 4-2. 소형 모델 기반 실시간 검증·판단·수정 루프

CEO 제안대로 소형 AI 모델을 자동수집 루프에 붙이면 효율은 올라간다. 다만 모델이 은행 거래를 "판단해서 생성"하면 안 되고, DOM 규칙과 파서가 실패했을 때 화면 상태를 빠르게 분류하고 다음 안전 행동 후보를 좁히는 보조 역할로 제한해야 한다.

권장 루프:

```text
PC Agent 화면/DOM 수집
  -> 규칙 기반 상태 판정
  -> 판정 성공 시 상태 머신 실행
  -> 판정 불명확/파서 실패 시 안전 요약 생성
  -> 소형 모델 classify/suggest
  -> allowlist selector와 보안 정책으로 후보 검증
  -> 1회 자동 수정 시도
  -> 성공/실패/CEO 승인 필요 상태를 collection run에 기록
```

소형 모델이 직접 받는 입력은 민감값 제거 후 아래로 제한한다.

| 입력 | 허용 | 금지 |
|---|---|---|
| DOM 요약 | tag, role, label 일부, button/input 후보, table header 일부 | value, password, OTP, 계좌번호 원문 |
| 화면 텍스트 | 로그인/인증/조회/거래내역 같은 상태 판정용 단어 | 고객명, 계좌번호, 잔액, 거래 적요 원문 |
| 파서 diagnostics | table count, header count, error_code, selector miss | 원본 HTML, 전체 스크린샷 OCR, 다운로드 원본 |
| 직전 상태 | 이전 error_code, 재시도 횟수, work key hash | 세션 쿠키, storage state, credential |

판단 출력 계약:

```json
{
  "state": "login_required",
  "confidence": 0.86,
  "suggested_action": "focus_password_manager",
  "selector_candidates": ["input[type=password]", "#password"],
  "requires_operator": false,
  "reason_code": "LOGIN_FORM_VISIBLE"
}
```

실행 게이트:

| 모델 제안 | 자동 실행 조건 | 차단 조건 |
|---|---|---|
| 로그인 input focus | selector allowlist에 있고 값 입력이 아닌 focus/click이면 실행 | selector가 숨김 필드/외부 iframe/unknown이면 차단 |
| Vault write-only 입력 | 계좌별 opt-in, selector allowlist, credential key 매핑이 모두 맞으면 실행 | 모델이 값 자체를 요구하거나 selector가 불명확하면 차단 |
| 조회/기간 버튼 클릭 | 은행별 상태 머신에서 허용된 다음 단계와 일치하면 실행 | 순서가 맞지 않거나 반복 실패 2회 이상이면 차단 |
| 파서 보정 | header alias 후보만 기록하고 코드 수정 후보로 남김 | 모델 출력만으로 거래 row 생성 금지 |
| OTP/CAPTCHA | 입력칸 focus, CEO 입력 감지 후 재개만 허용 | 해독, 대리 입력, 우회 제안은 차단 |

운영 효율 기대효과:

| 병목 | 기존 처리 | 소형 모델 보강 후 |
|---|---|---|
| 로그인 화면과 거래 없음 화면 혼동 | `PORTAL_TABLE_NOT_FOUND`로 뭉뚱그림 | `login_required`, `no_records`, `parse_failed`로 분리 |
| 은행 UI selector 변경 | 수동 코드 확인 필요 | 후보 selector를 diagnostics에 남겨 파서 보강 시간을 단축 |
| 인증 챌린지 반복 | 매번 수동 확인 | 자동처리 가능/불가를 분류해 CEO 개입을 최소화 |
| 파서 실패 원인 분석 | 원본 HTML 확인 필요 | 안전 header 요약 기반으로 실패 원인 bucket 분류 |
| 자동수집 루프 재시도 | 같은 실패 반복 가능 | 상태 변화가 없으면 재시도 중단, 바뀌면 자동 재판정 |

구현 우선순위:

| 우선순위 | 작업 | 완료 기준 |
|---|---|---|
| P0 | `bank_screen_state_classifier` 추가 | DOM 요약만으로 8개 상태를 분류하고 민감값이 로그에 남지 않음 |
| P0 | 자동수집 루프에 `classify -> gated action -> recheck` 1회 재시도 연결 | 동일 error에서 무한 반복하지 않고 상태 전이를 기록 |
| P0 | 모델 출력 JSON schema 검증 | schema 불일치, confidence 부족, 금지 action은 자동 차단 |
| P1 | selector 후보 diagnostics 누적 | 은행별 실패 selector 후보가 run 기록에 남음 |
| P1 | 대사 불명확 항목 설명 보조 | 규칙 매칭 실패 row에만 사유 후보를 붙임 |

권장 모델 라우팅:
- 1순위: 현재 AADS LiteLLM 경유 빠른 소형 모델.
- 2순위: 로컬/저비용 모델이 준비되면 DOM 상태 분류만 이전.
- 고성능 모델은 은행별 파서 구조 변경, 보안 정책 변경, 대사 규칙 설계 같은 고위험 판단에만 사용한다.

## 5. 구현 단계

### Phase 0. 현재 P0 코어 정리

이미 완료된 계좌 등록, 거래 원장, CSV 반영, 브라우저 커넥터, 자동수집 루프를 운영 기준으로 문서화한다. 지금 남은 핵심은 "코드는 있음"과 "실은행 E2E는 아직 안 됨"을 분리해 관리하는 것이다.

완료 조건:
- 은행 API/서비스 테스트 통과.
- 문서상 지원 방식과 미지원 방식을 명확히 표시.
- 운영 화면에서 인증 필요/수집 성공/거래 없음/파서 실패 상태가 구분됨.

### Phase 1. 운영 실은행 E2E

신한은행 기업, IBK기업은행 기업 각각에 대해 PC Agent 세션으로 빠른조회 페이지를 열고 비밀번호관리자/Vault/계좌비밀번호/인증서 비밀번호 자동입력을 먼저 수행한다. OTP/CAPTCHA/본인인증이 나오면 자동처리 가능 여부를 먼저 시도하고, 불가능한 단계만 운영자가 승인한다. 이후 거래내역 화면에서 수집 버튼을 눌러 `bank_transactions`에 row가 저장되는지 확인한다.

E2E 체크리스트:

| 순서 | 검증 | 성공 기준 |
|---|---|---|
| 1 | `connection_type=browser`, `status=active`, `auto_sync=true` 계좌 등록 | 원본 계좌번호 미저장, 마스킹 계좌만 표시 |
| 2 | PC Agent 브라우저 세션 없이 수집 | `PC_AGENT_LOGIN_REQUIRED` 또는 `ACTION_REQUIRED` 반환 |
| 3 | PC Agent 세션 자동 열기 | 은행 빠른조회 URL이 work session에 열린다 |
| 4 | 비밀번호관리자/Vault 자동입력 | ID/PW/계좌비밀번호 입력이 완료되거나 입력 불가 사유가 진단된다 |
| 5 | OTP/CAPTCHA/본인인증 자동처리 또는 최소 승인 후 재수집 | `connector_status=CONFIGURED`, `collected_rows > 0` 또는 정상 `no_records` |
| 6 | 동일 기간 재수집 | 중복은 `duplicate_rows`로 잡히고 원장 중복 증가 없음 |
| 7 | UI 확인 | 입금·계좌 화면과 요약에 수집 결과 표시 |

### Phase 1-1. 자동수집 루프 상세

현재 자동수집 루프의 실행 정본은 `scripts/yeoljeong_auto_collect.py`다. 판매채널 수집 결과를 만든 뒤, `skip_financial_accounts`가 false이면 은행 수집을 추가 실행한다.

| 단계 | 함수/옵션 | 동작 |
|---|---|---|
| 입력 구성 | `_payload()` | 날짜 범위, 사업자/지점, `browser_session_id`, `force_recreate_portal_sessions`, `auto_open_bank_browser`, `browser_agent_id`, `browser_preferred_port`를 구성한다. |
| 은행 계좌 선별 | `_bank_accounts_for_payload()` | active 상태와 `auto_sync=true` 계좌만 수집 대상으로 고른다. |
| 은행 수집 호출 | `_collect_bank_accounts()` | 계좌별 `collect_bank_account_transactions()`를 호출하고 브라우저 자동 오픈/세션 재생성 옵션을 전달한다. |
| 기존 금융 계정 호환 | `_collect_platform_financial_accounts()` | 과거 `platform_accounts`에 남은 은행/카드류 계정도 수집 결과에 합친다. |
| 완료 판정 | `_completion_state()` | 은행/판매채널 결과를 합쳐 완료, retryable, blocking 상태를 판정한다. |
| 세션 재생성 | `_should_force_recreate_portal_sessions()` | `PC_AGENT_SESSION_REQUIRED`, `PC_AGENT_SESSION_NOT_FOUND`, `PC_AGENT_WRONG_PORTAL_SESSION`이면 다음 시도에서 세션을 강제 재생성한다. |
| child 재시도 | `_child_collect_argv()` | 동일 CLI를 child process로 재호출하며 브라우저/은행 옵션을 보존한다. |

루프 운영 기준:
- 기본은 빠른 1회 수집이 아니라 `until-complete` 방식으로 계좌/채널별 완료 상태를 모은다.
- blocking은 사람 승인이 필요한 상태로 분리하고, retryable은 자동 재시도/세션 재생성 대상으로 분리한다.
- 은행 수집은 판매채널 수집 실패와 독립적으로 결과를 남긴다.
- 자동 오픈 세션은 계좌별 hashed work key를 사용해 다른 포털 세션과 섞이지 않게 한다.

### Phase 1-2. 소형 모델 실시간 판단 루프 구현

Phase 1 E2E와 병행해 소형 모델 판단 루프를 P0 보조 기능으로 붙인다. 핵심은 "모델이 자동수집을 대신한다"가 아니라 "규칙 기반 자동화가 멈춘 이유를 빠르게 분류하고, 허용된 다음 동작 후보만 제안한다"이다.

| 단계 | 작업 | 완료 기준 |
|---|---|---|
| 1 | `BankScreenStateJudge` 인터페이스 정의 | 규칙 기반 classifier와 모델 기반 classifier를 같은 출력 schema로 교체 가능 |
| 2 | `safe_dom_summary` 생성 | 계좌번호 원문, 비밀번호, OTP, 인증서 비밀번호, 원본 HTML이 모델 입력/로그에 없음 |
| 3 | 소형 모델 JSON schema 고정 | `state`, `confidence`, `suggested_action`, `selector_candidates`, `requires_operator`, `reason_code` 외 출력 폐기 |
| 4 | 정책 gate 연결 | CAPTCHA/OTP 해독, 거래 row 생성, 금액 수정, 금지 selector 클릭은 자동 차단 |
| 5 | 자동수집 루프 재판정 연결 | `parse_failed` 또는 불명확 상태에서 최대 1회 `judge -> gated action -> recheck` 수행 |
| 6 | 판단 캐시 | 같은 화면 fingerprint/error_code는 TTL 내 모델 재호출 없이 이전 판단 사용 |
| 7 | diagnostics 기록 | `ai_judgement`, `confidence`, `applied_action`, `blocked_reason`을 민감값 없이 collection run에 저장 |

완료 후 운영 판정은 아래처럼 바뀐다.

| 기존 상태 | 개선 상태 | 효과 |
|---|---|---|
| `PORTAL_TABLE_NOT_FOUND` | `login_required`, `no_records`, `parse_failed`, `portal_maintenance` | 원인별 재시도/수동조치 분리 |
| `ACTION_REQUIRED` | `auto_resumable`, `operator_input_needed`, `blocked_by_policy` | CEO 개입 단계를 최소화 |
| 단순 retry | `retryable_with_new_session`, `retryable_after_operator`, `stop_same_fingerprint` | 무한 재시도와 세션 낭비 방지 |

### Phase 2. 대사 엔진 고도화

현재 `_annotate_bank_matches()`는 기존 거래 원장과 날짜/금액/텍스트 기반으로 약식 매칭한다. 운영 단계에서는 정산 원장과 직접 매칭하는 `match_bank_to_settlements()`를 수집 완료 후 실행하고, 미매칭 bucket을 별도 화면으로 노출해야 한다.

우선순위:

| 우선순위 | 작업 | 기대효과 |
|---|---|---|
| P0 | 배달 정산 원장과 은행 입금액 날짜+금액 매칭 | 입금 누락/차액 즉시 확인 |
| P1 | 입금자명/적요 synonym 규칙 | 배민/쿠팡/요기요/카드PG 명칭 흔들림 대응 |
| P1 | 수수료/보류/분할입금 허용 오차 | 실제 정산 차이 오탐 감소 |
| P2 | 수동 매칭/해제 감사 로그 | 회계 검토 이력 보존 |

### Phase 3. 파일 원장 DB 승격

무중단 배포와 다중 인스턴스 운영을 고려하면 파일 원장은 PostgreSQL로 승격해야 한다. 승격 시 테이블은 `yeoljeong_bank_accounts`, `yeoljeong_bank_transactions`, `yeoljeong_bank_collection_runs`, `yeoljeong_bank_matches`로 분리한다.

DB 승격 원칙:
- 기존 JSON 파일을 migration seed로 1회 import한다.
- `source_hash`에 unique index를 둔다.
- 민감 필드는 DB에도 만들지 않는다.
- collection run 단위로 수집 시작/종료/상태/오류를 남긴다.
- 롤백을 위해 일정 기간 JSON export를 병행한다.

## 6. 보안 정책

| 항목 | 정책 |
|---|---|
| 계좌번호 | 원본은 write-only 입력 후 즉시 마스킹, 저장 금지 |
| 은행 로그인 비밀번호 | AADS 은행 파일 원장 저장 금지. Agent Vault 또는 CEO PC 비밀번호관리자에 암호화/로컬 저장하고 API 응답에는 마스킹만 제공 |
| 계좌비밀번호 | 자동화를 위해 필요하면 계좌별 opt-in 후 Agent Vault write-only 항목으로 저장한다. 파일 원장, 로그, diagnostics에는 절대 남기지 않는다 |
| OTP/CAPTCHA/본인인증 | 첫 감지 즉시 중단 금지. 승인 팝업 감지, 푸시 승인 polling, CEO 입력 감지 후 자동 재개를 우선하고, CAPTCHA 판독·외부 OTP 기기 조작·휴대폰 본인확인처럼 시스템이 처리할 수 없는 최소 단계만 CEO/운영자가 처리 |
| 인증서 비밀번호 | 계좌별 opt-in이 있을 때만 Agent Vault/CEO PC keychain에서 write-only 입력. 파일 원장, 로그, diagnostics, API 응답 저장 금지 |
| 원본 HTML | 저장 금지. 파서 diagnostics는 테이블 수/헤더 일부 같은 안전 정보만 저장 |
| 브라우저 세션 | 업무별 opaque work key 사용. 계좌명/지점명/계좌번호가 key에 드러나지 않게 hash 사용 |
| 권한 | 은행 계좌/거래 조회·등록은 관리자 권한만 허용 |

## 7. 실패 처리와 대체 경로

| 실패 유형 | 상태/error_code | 처리 |
|---|---|---|
| PC Agent 미연결/브라우저 로그인 필요 | `PC_AGENT_LOGIN_REQUIRED`, `ACTION_REQUIRED` | PC Agent 자동 재연결, 비밀번호관리자 focus fallback, Vault 자동입력 후 재수집, 또는 CSV 업로드 |
| 세션 만료 | `BANK_BROWSER_SESSION_NOT_FOUND` | work session 재생성 |
| 은행 로그인/OTP 필요 | `BANK_BROWSER_AUTH_CHALLENGE_DETECTED`, `BANK_BROWSER_OPERATOR_ACTION_REQUIRED` | 비밀번호관리자/Vault/푸시 승인 polling/CEO 입력 감지 후 자동 재개를 먼저 시도하고, 불가 단계만 운영자가 브라우저에서 인증 완료 후 재수집 |
| CAPTCHA/본인인증 필요 | `BANK_BROWSER_AUTH_CHALLENGE_DETECTED`, `BANK_BROWSER_OPERATOR_ACTION_REQUIRED` | CAPTCHA 판독·본인확인 대행은 금지. 입력칸 focus, 상태 알림, CEO 처리 감지 후 자동 재개 |
| 인증서 비밀번호 필요 | `BANK_CERTIFICATE_PASSWORD_REQUIRED`, `BANK_BROWSER_OPERATOR_ACTION_REQUIRED` | opt-in Vault/keychain 값으로 write-only 자동입력, 없거나 실패하면 CEO 1회 입력 후 재수집 |
| 계좌비밀번호 필요 | `BANK_ACCOUNT_PASSWORD_REQUIRED` | Agent Vault 계좌비밀번호 등록 또는 운영자 1회 입력 후 재수집 |
| 다른 포털/죽은 세션 | `PC_AGENT_WRONG_PORTAL_SESSION`, `CDP_NOT_READY` | work key별 세션 retire 후 강제 재생성, preferred port 재선택 |
| 포털 테이블 없음 | `PORTAL_TABLE_NOT_FOUND` 계열 | 페이지 로드/로그인 상태 확인, CSV 대체 |
| 파서 실패 | `parse_failure=True` | 실HTML fixture 확보 후 파서 보강 |
| 오픈뱅킹 미연결 | `BANK_CONNECTOR_NOT_CONFIGURED` | 공식 API 제공자 선정 전 CSV/브라우저 방식 유지 |

## 7-1. PC Agent 안정화 반영안

은행 자동수집의 병목은 파서보다 PC Agent 세션 안정성이다. 운영 기준은 다음과 같이 고정한다.

| 리스크 | 개선안 | 완료 기준 |
|---|---|---|
| Agent 0대 오인식 | Browser Bridge가 로컬 메모리 registry만 보지 않고 active API 경유로 online Agent를 재조회한다. | 8100/8102 슬롯 중 online Agent가 있으면 세션 생성 가능 |
| 죽은 Chrome/CDP 재사용 | `CDP_NOT_READY`, session not found, wrong portal이면 work key 세션을 retire하고 강제 재생성한다. | 재시도 1회 안에 새 세션 ID 발급 |
| 포털 세션 혼선 | 은행은 `yeoljeong-bank-browser-{hash}`, 배달은 `yeoljeong-delivery-*` work key로 분리한다. | 은행 수집이 배달 포털 페이지를 읽지 않음 |
| 장시간 수집 중단 | child process 단위 timeout과 `ATTEMPT_TIMEOUT` 상태 기록을 사용한다. | timeout 후 다음 루프에서 재시도 가능 |
| PC Agent busy | routed command lease/queue를 사용하고 command timeout을 명시한다. | busy 상태가 실패가 아니라 대기/재시도로 기록 |
| 브라우저 자동 종료 | 기본은 세션 유지, 명시 옵션에서만 close tabs/browser를 실행한다. | 인증 완료 세션이 다음 재수집에 재사용됨 |

운영 권장값:

```bash
python3 scripts/yeoljeong_auto_collect.py \
  --business-id all \
  --branch 전체 \
  --force-recreate-sessions \
  --keep-browser-open
```

은행 자동화에서는 `--keep-browser-open`을 기본 운영값으로 둔다. 은행 인증은 세션 쿠키/비밀번호관리자 상태가 중요하므로, 성공 직후 브라우저를 닫으면 다음 루프 안정성이 떨어진다.

## 8. 테스트 계획

| 테스트 | 범위 | 완료 기준 |
|---|---|---|
| 단위 테스트 | 은행 계좌/원장/CSV/API/브라우저 파서 | 기존 은행 관련 테스트 전부 통과 |
| 멱등 테스트 | 같은 거래 반복 반영 | imported 1회, 이후 duplicate 처리 |
| 보안 테스트 | 민감 필드 입력 | API 422 또는 저장소 미포함 |
| PC Agent mock 테스트 | 세션 없음/세션 만료/테이블 있음/거래 없음 | 상태코드와 메시지 구분 |
| 자동로그인 보조 테스트 | password manager fallback, Vault 입력 selector, 계좌비밀번호/인증서 비밀번호 입력 폼 | 민감값 미노출, 허용 selector에만 입력 |
| 인증 챌린지 테스트 | OTP/CAPTCHA/본인인증/인증서 비밀번호 화면 | 자동처리 가능한 단계는 재개, 불가능한 단계만 `operator_action_required` |
| 소형 모델 판단 테스트 | safe DOM 요약, JSON schema, confidence gate, 금지 action 차단 | 민감값 미노출, 낮은 confidence/금지 action 자동 폐기 |
| 자동수집 루프 테스트 | bank auto-open 전달, force recreate, blocking/retryable 분류 | `bank_collections`, `bank_totals`, `completion_state` 정상 |
| 운영 E2E | 신한/IBK 실브라우저 | 실제 거래 row 저장 또는 정상 no_records 판정 |
| UI 검증 | 입금·계좌 화면 | 요약/계좌/거래/인증필요 뱃지 표시 |

실행 후보:

```bash
pytest tests/unit/test_yeoljeong_finance_service.py tests/unit/test_yeoljeong_finance_api.py tests/unit/test_bank_browser_connector.py tests/unit/test_yeoljeong_bank_browser_connector.py -q
pytest tests/unit/test_yeoljeong_auto_collect.py tests/unit/test_pc_agent_routing_leases.py -q
pytest tests/unit/test_bank_screen_state_judge.py -q
python3 -m py_compile app/api/yeoljeong_finance.py app/services/yeoljeong_finance_service.py app/services/yeoljeong_bank_browser_connector.py scripts/yeoljeong_auto_collect.py
```

## 8-1. 소형 모델 적용 기준

데이터 수집 자체는 모델이 아니라 DOM/HTTP/파서/멱등 원장으로 처리해야 빠르고 안정적이다. 모델은 화면 이해 보조에만 제한하면 작은 모델로 충분하다.

| 사용처 | 권장 모델 급 | 이유 |
|---|---|---|
| 화면 상태 분류 | 빠른 소형 모델 또는 규칙 기반 우선 | `로그인 필요/OTP 필요/CAPTCHA 필요/본인인증 필요/인증서 비밀번호 필요/거래표 있음/거래 없음` 분류는 짧은 텍스트·DOM 요약이면 충분 |
| selector 후보 추천 | 빠른 소형 모델 | 실패한 DOM 요약에서 다음 클릭 후보를 고르는 용도 |
| 파서 실패 원인 요약 | 빠른 소형 모델 | diagnostics와 header 목록 기반으로 원인 분류 |
| 금액/거래 정규화 | 모델 미사용 | 정규식/테이블 파서가 더 빠르고 재현 가능 |
| OTP/CAPTCHA 해독 | 사용 금지 | 보안·약관·법적 리스크. 단, CEO가 입력한 값의 제출 후 성공/실패 감지와 자동 재개는 허용 |
| 최종 대사 판단 | 규칙 우선, 불명확 항목만 소형 모델 보조 | 금액/날짜 일치는 결정론이 필요 |

권장 운영 방식:
- 1차: DOM selector/정규식/테이블 파서로 처리한다.
- 2차: 실패한 화면의 안전 요약만 작은 모델에 보내 상태를 분류한다.
- 3차: 모델 출력은 실행 명령이 아니라 후보로만 쓰고, 실제 클릭/입력은 allowlist selector와 상태 머신이 수행한다.
- 모델 호출은 계좌별 수집 루프마다 반복하지 않고, 실패 상태가 바뀐 경우에만 캐시 키를 갱신해 비용과 지연을 줄인다.

## 9. 운영 적용 순서

1. 운영 계좌 등록: 사업자/지점/은행/마스킹 계좌/연동 방식/browser/auto_sync를 등록한다.
2. 자동로그인 opt-in: 비밀번호관리자 사용 여부, Agent Vault ID/PW, 계좌비밀번호, 인증서 별칭을 계좌별로 등록한다.
3. PC Agent 확인: CEO PC 또는 운영 PC의 Agent가 online인지 확인한다.
4. 자동 오픈/자동입력 dry-run: 은행 기업페이지 오픈, ID/PW/계좌비번/인증서 비밀번호 입력 가능 여부, OTP/CAPTCHA/본인인증 자동처리 가능 여부를 확인한다.
5. 은행별 인증 챌린지 처리: 자동처리 가능한 항목은 PC Agent가 계속 진행하고, CAPTCHA 판독·외부 OTP 기기 조작·휴대폰 본인확인처럼 불가능한 최소 단계만 운영자가 처리한다.
6. 단건 수집: 한 계좌/하루 범위로 먼저 수집해 원장과 UI를 확인한다.
7. 반복 수집: 같은 기간 재수집으로 중복 방지를 확인한다.
8. 자동 루프 편입: `yeoljeong_auto_collect.py`의 은행 수집을 주기 실행에 포함한다.
9. 대사 확인: 배달 정산 원장과 은행 입금 거래의 matched/unmatched를 확인한다.
10. 알림 연결: 미입금, 차액, 인증 필요, 파서 실패를 텔레그램/대시보드 알림으로 연결한다.

## 10. 미결정 사항

| 항목 | 선택지 | 권장 |
|---|---|---|
| 공식 오픈뱅킹 | 직접 계약/API 제공자/보류 | 운영 초기에는 PC Agent+CSV, 이후 API 제공자 검토 |
| DB 승격 시점 | E2E 전/후 | 실은행 E2E 1회 성공 후 승격 |
| 은행 추가 | 신한/IBK 우선/전은행 확장 | 신한/IBK 안정화 후 거래량 기준 추가 |
| 자동 로그인 | 비밀번호관리자/Vault 자동입력/자동처리 가능 인증/최소 수동 승인 | ID/PW/계좌비번/인증서 비밀번호는 opt-in 자동입력, OTP/CAPTCHA/본인인증은 자동처리 가능 항목 우선 후 불가 항목만 수동 승인 |
| 알림 범위 | 실패만/차액 포함 | 실패, 인증 필요, 미입금, 차액 모두 알림 |

## 11. 다음 액션

| 우선순위 | 작업 | 산출물 |
|---|---|---|
| P0 | 신한은행 기업 실계정 PC Agent E2E | 수집 로그, 원장 row, 화면 캡처 |
| P0 | IBK기업은행 기업 실계정 PC Agent E2E | 수집 로그, 원장 row, 화면 캡처 |
| P0 | 수집 결과 UI 인증필요/성공/no_records 표시 검증 | 체크리스트 완료표 |
| P0 | 계좌비밀번호/은행 로그인/인증서 비밀번호 Vault write-only 저장과 자동입력 selector 구현 | 민감값 미노출 테스트, 실은행 dry-run 로그 |
| P0 | OTP/CAPTCHA/본인인증 챌린지 자동처리 상태 머신 구현 | `auth_challenge_detected -> auto_resumed/operator_action_required` 전이 로그 |
| P0 | 은행별 매크로 상태 머신 구현 | 신한/IBK 로그인→조회→거래내역 경로별 상태 전이표 |
| P0 | 소형 모델 기반 `BankScreenStateJudge` 구현 | safe DOM 요약, JSON schema, confidence gate, 금지 action 차단 테스트 |
| P0 | PC Agent 세션 강제 재생성/keep-open 운영값 배포 | `CDP_NOT_READY` 재발 시 자동 회복 로그 |
| P1 | `bank_collection_runs` DB 테이블 설계 | migration 초안 |
| P1 | 은행 입금 vs 배달 정산 자동대사 배치 | matched/unmatched 리포트 |
| P2 | 은행별 실HTML fixture 저장 없이 파서 fixture화 | 테스트 fixture와 파서 회귀 테스트 |

## 12. 결론

현재 은행 자동수집은 "원장/API/CSV/PC Agent 브라우저 커넥터/자동수집 루프/비밀번호관리자 focus fallback"까지 P0 코드가 준비되어 있다. CEO 지시 기준의 완전 자동화는 여기에 "Agent Vault write-only 은행 ID/PW/계좌비밀번호 자동입력, 은행별 매크로 상태 머신, PC Agent 세션 안정화, 소형 모델 기반 화면 상태 분류"를 추가하는 방향이 맞다.

따라서 즉시 다음 단계는 두 갈래다. 첫째, 이미 있는 PC Agent 세션 자동 오픈으로 신한/IBK 실계정 E2E를 진행한다. 둘째, 계좌비밀번호와 은행 로그인, 인증서 비밀번호 자동입력은 파일 원장이 아니라 Agent Vault/비밀번호관리자 기반으로 P0 구현해, 이후 자동수집 루프가 사람 개입 없이 거래내역 화면까지 최대한 도달하게 만든다. OTP/CAPTCHA/본인인증/인증서 비밀번호 화면은 첫 감지에서 멈추지 말고 자동처리 가능한 항목을 우선 처리하며, CAPTCHA 판독·외부 OTP 조작·휴대폰 본인확인처럼 불가능한 최소 단계만 CEO 승인으로 넘긴다.
