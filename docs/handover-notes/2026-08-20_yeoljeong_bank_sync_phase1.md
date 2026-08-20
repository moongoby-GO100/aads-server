# 매장비서 은행자동연동 1단계 (P0 코어) — Handover

- 날짜: 2026-08-20
- 범위: 열정국밥 finance(매장비서) 앱에 사업자별 **은행계좌 등록 / 연동상태 / 은행거래 원장** API 추가
- 배포 상태: **미배포** (코드 + 테스트만). 재시작/푸시/빌드는 Runner/CEO 승인 경로에서 수행.

## 무엇을 구현했나

기존 구조(`platform_accounts.json`, `transactions.json`, 배달/정산 원장)를 **전혀 건드리지 않고**,
은행 전용 저장소·서비스 메서드·API를 별도로 추가했다. 기존 `/transactions`, `/accounts`,
`/settlements` 등은 그대로 backward compatible.

### 저장소 (파일 기반, DB 미결합)
- `app/data/yeoljeong_finance/bank_accounts.json` — 은행계좌 등록부
- `app/data/yeoljeong_finance/bank_transactions.json` — 은행거래 원장
- 두 파일 모두 **0600 권한**(`_write_secure_file_rows`)으로 기록. 소유자만 읽기/쓰기.
- DB(`DB_LEDGER_TABLE_BY_NAME`)에 등록하지 않음 → 마이그레이션 불필요, 파일 저장소 패턴 유지.

### 은행계좌 모델 (`bank_accounts`)
필드: `id, business_id, branch_id(optional), bank_code, bank_name, account_number_masked,
account_holder, account_alias, connection_type, status, institution_code, auto_sync, memo,
last_synced_at, created_at, updated_at`
- `connection_type` ∈ `open_banking | csv | manual | mock`
- `status` ∈ `active | paused | error | needs_auth`
- **민감정보 제외 원칙**: 원본 계좌번호는 `account_number`(write-only)로만 받아 `_masked_digits`로
  마스킹 후 폐기. 화이트리스트(`_BANK_ACCOUNT_PUBLIC_FIELDS`) + 금지키 셋
  (`_BANK_ACCOUNT_FORBIDDEN_FIELDS`: password/otp/pin/certificate/secret 등)으로 이중 차단.
  Pydantic 모델은 `extra="forbid"` → 비밀번호류 필드는 422로 거부.

### 은행거래 원장 모델 (`bank_transactions`)
필드: `id, business_id, branch_id(optional), bank_account_id, occurred_at, posted_at(optional),
direction(in|out), amount, balance(optional), counterparty, memo, raw_memo, category,
platform_match(optional), settlement_match(optional), source, source_hash, imported_at`
- **멱등 중복 제거**: `source_hash` 기준. 미제공 시 (business_id, bank_account_id, occurred_at,
  direction, amount, counterparty, raw_memo) SHA256으로 산출. 재기록해도 중복은 skip.
- 금액은 `_amount`로 정수화(절대값), 방향은 `in/out`으로 정규화(입금/출금/deposit/withdrawal 등 허용).
- 미분류 카테고리는 기존 `_transaction_category` 규칙 재사용.

### API (`app/api/yeoljeong_finance.py`, prefix `/yeoljeong-finance`)
- `GET  /bank-accounts?business_id&branch_id&status` — 목록(관리자 전용)
- `POST /bank-accounts` — 등록
- `PATCH /bank-accounts/{account_id}` — 상태/별칭/마스킹 등 부분 수정
- `GET  /bank-transactions?business_id&bank_account_id&direction&date_from&date_to` — 조회
- `POST /bank-transactions` — 수동/CSV/목업 거래 멱등 반영 (`bank_account_id` 필수)
- `GET  /bank-summary?business_id&bank_account_id&date_from&date_to` — 입출금 집계 + 계좌별 상태 요약

집계: `totals{total_in,total_out,net,transaction_count,account_count}`,
`account_status_counts`, 계좌별 `{transaction_count,total_in,total_out,net,status,last_synced_at}`.
사업자/지점 스코프는 `_normalize_bank_scope`로 CANONICAL 사업자·지점 정합성 검증.

## 검증
- `python3 -m compileall app/api/yeoljeong_finance.py app/services/yeoljeong_finance_service.py` → OK
- 신규 서비스 단위테스트 18건 + API 테스트 2건 전부 통과.
  - `tests/unit/test_yeoljeong_finance_service.py` (은행 섹션)
  - `tests/unit/test_yeoljeong_finance_api.py::test_bank_account_and_ledger_http_flow`,
    `::test_bank_account_rejects_extra_sensitive_field`
- 기존 finance 서비스 테스트(네트워크/PC Agent 미의존분) 63건 통과, 회귀 없음.
  ※ delivery/pc_agent/ddangyo 계열 테스트는 이 샌드박스에 `structlog`/실DB/실네트워크가 없어
    사전부터 실패/행업하는 환경 이슈이며, 은행 코드와 무관.

## 이번 단계에서 **구현하지 않은** 범위 (다음 단계)
- 외부 은행 **실연동**: 오픈뱅킹 API, 은행 포털 스크래핑/PC Agent 로그인, 실시간 잔액/거래 수집.
  현재는 수동/CSV/목업 입력만 원장에 반영한다.
- 은행 CSV 파서(은행별 컬럼 매핑) — 현재는 정규화된 JSON 배열(`transactions`)만 수신.
  향후 `import_file`류 CSV 파서를 은행 원장 전용으로 확장 필요.
- 배달 플랫폼/정산 자동 매칭(`platform_match`, `settlement_match`)은 필드만 두고 로직 미구현.
- DB 영속화(현재 파일 전용). 다중 인스턴스/무중단 배포 시 DB 원장 승격 검토.
- 프런트엔드(`app/static/apps/yeoljeong-finance`) 은행 탭 UI — 이번엔 API/서비스 계층만.

## 주의 (보안 절대 규칙)
- 은행 실계정/비밀번호/OTP/공동인증서는 **저장 기능 자체를 만들지 않았다.** credential은
  추상 상태(`status`)와 마스킹 정보(`account_number_masked`)만 유지.
- 신규 DB destructive 연산 없음(DROP/TRUNCATE/DELETE 미사용). 계좌 삭제 API는 이번 범위에서 제외.
