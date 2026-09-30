# 오비서 서명 계약 → 직원 고용조건 → 급여 기준값 운영 문서

TASK: AADS-OBYS-CONTRACT-TO-EMPLOYMENT-PAYROLL-20260930 · 대상: 오비서(열정국밥) 진아서버 인스턴스

## 1. 원칙

- **원본은 서명 완료 계약서 하나뿐이다.** 고용조건을 따로 편집하는 화면·테이블은 없다.
  조건을 바꾸려면 정정 계약서를 새로 작성해 서명받는다.
- 직원 레코드(`yeoljeong_employee_join_requests.request_payload`)의 `current_employment*` 는
  최신 서명 근로·용역 계약을 가리키는 **파생 스냅샷**이다. 계약서에서 언제든 다시 만든다(재동기화).
- 이력은 지우지 않는다. 서명 계약서 체인이 이력이고, 스냅샷 생성·교체·재동기화는 `yeoljeong_audit_logs` 에 남는다.
- 급여는 **기본값만** 채운다. 관리자가 보낸 값이 항상 우선이다. 시급·일급제 월 총액은 추정하지 않는다.
- 적용 범위: **이 배포 이후 서명분부터**. 기존 데이터는 건드리지 않는다(백필은 5절, 수동).

## 2. 흐름

| 단계 | 동작 | 실패 시 |
|---|---|---|
| `POST /api/v1/yeoljeong-finance/contracts/signing` | 서명 봉인 → 서명본 PDF → **고용조건 스냅샷 갱신** → 알림 (`sign_contract_and_deliver`) | 서명은 유지. 직원 레코드 `current_employment_error` 에 사유, 응답 `employment.status=failed` |
| `GET /api/v1/yeoljeong-finance/employees/approved` | 기존 필드 + `current_employment`, `current_employment_contract_id`, `needs_employment_sync` | — |
| `POST /api/v1/yeoljeong-finance/employees/approved/{request_id}/resync-employment` (신규) | 관리자만. 계약서에서 스냅샷을 다시 만든다. 바뀐 게 없으면 쓰지 않는다 | 비관리자·타 테넌트 403 |
| `GET /api/v1/yeoljeong-finance/employees/approved/{request_id}/employment-history` (신규) | 관리자·본인. 서명 계약서 체인 + 스냅샷 감사로그를 시간순(`timeline`) | 제3자·타 테넌트 403 |
| `GET /api/v1/yeoljeong-finance/payroll/defaults?employee_email=&payroll_month=` (신규) | 관리자만. 기본값 + 근거(`source_contract_id`, `wage_type`, `basis`) | 비관리자·타 테넌트 403, 월 형식 오류 400 |
| `POST /api/v1/yeoljeong-finance/payroll` | 빈 칸만 계약 기본값으로 채움. `source_contract_id`, `contract_defaults_applied`, `contract_deviation` 기록 | 기존 검증 3종 그대로 |

응답 호환: 서명 응답의 기존 `contract`·`signed_pdf`·`notify` 키는 그대로이고 `employment` 키가 추가됐다.
`contract_count`·`needs_contract` 등 기존 직원 목록 필드도 그대로다.

- 고용조건 원본이 되는 계약 유형: `regular`, `part_time`, `manager`, `freelancer`.
  **`confidentiality`(비밀유지 서약)는 원본이 아니다** — 서명해도 스냅샷이 바뀌지 않는다.
- 여러 건이면 `status='signed'`, 삭제되지 않은 것 중 `signed_at` 최신 1건. 값은 계약서의 봉인 스냅샷(`signed_snapshot`)에서 읽는다.
- 직원 매칭: 계약서 `employee_request_id` 가 있으면 그것, 없으면 `employee_email`.

## 3. 파생 규칙표

| 계약서 payload | 고용조건 `current_employment` | 급여 기본값 (`payroll/defaults`) |
|---|---|---|
| `contract_type` | `contract_type` | `freelancer` → `employment_tax_type=freelancer_33` 만, `gross_pay` 없음 |
| `employment_tax_type` | `employment_tax_type` | `employment_tax_type` (프리랜서 외) |
| `wage` | `wage` (정수) | 월급제: `gross_pay = wage` / 시급·일급제: `hourly_wage`·`daily_wage` 로만 돌려줌 |
| `wage_type` | `wage_type` | 근거 `wage_type`. `hourly`/`daily` → `estimated=false`, `reason="시급제는 근태 확정 후 산정"`(일급은 "일급제는 …") |
| `non_tax_meal_allowance` | `non_tax_meal_allowance` (정수) | 월급제: `non_tax_meal_allowance`, `taxable_pay = wage − 비과세 식대` |
| `meal_provision` | `meal_provision` | `meal_provision` (식사제공이면 현금식대 비과세 불가 검증이 그대로 적용) |
| `base_salary`, `taxable_allowance` | 같은 이름 (정수) | — (근거 표시용) |
| `start_date`, `end_date` | 같은 이름 | 급여 월이 기간 밖이면 `period_note` |
| `work_days`, `work_time`, `rest_time`, `weekly_hours` | 같은 이름 | `contract_terms` 로 표시만 |
| `pay_date`, `pay_method`, `workplace`, `business_id`, `branch` | 같은 이름 | `pay_date` 는 `contract_terms` 로 표시 |
| (계약서 id) | `current_employment_contract_id` | `source_contract_id` |
| `start_date` 또는 `signed_at` | `current_employment_effective_from` | — |
| (동기화 시각) | `current_employment_synced_at` | — |

### save_payroll 채움 규칙

- payload 에 값이 **없거나 빈 문자열**일 때만 채운다. 보낸 값은 덮어쓰지 않는다.
- 과세/비과세 분할(`taxable_pay`·`non_tax_meal_allowance`)은 **둘 다 비었고 총지급액이 계약 임금과 같을 때만** 채운다.
  관리자가 총액을 바꿨는데 계약 분할을 끼우면 "과세+비과세=총지급" 검증이 깨지기 때문이다.
- 계약과 다른 값이 들어오면 `contract_deviation = {필드: {"contract": 계약값, "input": 입력값}}`.
- 감사로그: `payroll.contract_defaults_applied`, `payroll.contract_deviation` (resource_type=`payroll_statement`).

## 4. 재동기화 절차

`needs_employment_sync=true` 인 직원(서명 계약은 있는데 스냅샷이 없거나, 다른 계약을 가리키거나, `current_employment_error` 가 있음):

1. 직원 목록에서 `current_employment_error` 사유를 확인한다(DB 연결 실패, 직원 레코드 없음 등).
2. 원인을 해소한 뒤 관리자 계정으로
   `POST /api/v1/yeoljeong-finance/employees/approved/{request_id}/resync-employment`.
3. 응답 `result.status` — `updated`(감사로그 `employment.snapshot_resynced`), `unchanged`(이미 최신), `no_source`(서명 근로·용역 계약 없음, 기존 스냅샷은 지우지 않음).
4. `employment-history` 로 스냅샷이 최신 서명 계약을 가리키는지 확인한다.

## 5. 마이그레이션 · 백필 (수동, 오비서 업무 DB 전용)

- `migrations/20260930_obys_employment_audit_lookup.sql` — 감사로그 조회 인덱스 1개만. 컬럼 추가 없음
  (스냅샷·급여 근거는 기존 jsonb 에 들어간다). AADS DB 자동 적용 제외(`scripts/migrations_auto_apply_baseline.txt`).
  ```
  psql "$OBYS_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/20260930_obys_employment_audit_lookup.sql
  ```
  롤백: `migrations/rollback/20260930_obys_employment_audit_lookup.down.sql` (인덱스만 제거).
- 백필: 훅 이전에 서명된 계약이 있는 직원만. **기본 `--dry-run`**, 한 번에 한 테넌트.
  ```
  OBYS_DATABASE_URL=... python3 scripts/backfill_employment_snapshots.py --tenant <uuid>          # 계획만
  OBYS_DATABASE_URL=... python3 scripts/backfill_employment_snapshots.py --tenant <uuid> --apply  # 실제 쓰기
  ```
  쓰기는 서비스의 `_sync_employee_employment` 를 그대로 거치므로 감사로그에 `trigger=backfill` 로 남는다.
  2026-09-30 실측으로 운영 tenant 15055cac… 는 서명 완료 계약 0건이라 현재 대상이 없다.

## 6. 조회 쿼리

```sql
-- (a) 급여-계약 이탈: 관리자가 계약과 다른 값을 넣은 급여명세
SELECT id, employee_email_masked, payroll_month, gross_pay,
       statement_payload->>'source_contract_id' AS source_contract_id,
       statement_payload->'contract_deviation'  AS deviation
  FROM yeoljeong_payroll_statements
 WHERE deleted_at IS NULL
   AND tenant_id = $1::uuid
   AND statement_payload->'contract_deviation' IS NOT NULL
   AND statement_payload->'contract_deviation' <> '{}'::jsonb
 ORDER BY payroll_month DESC;

-- (b) 계약 근거 없이 작성된 급여명세(서명 계약 없는 직원 또는 이 기능 이전 작성분)
SELECT id, employee_email_masked, payroll_month, gross_pay
  FROM yeoljeong_payroll_statements
 WHERE deleted_at IS NULL
   AND tenant_id = $1::uuid
   AND COALESCE(statement_payload->>'source_contract_id', '') = ''
 ORDER BY payroll_month DESC;

-- (c) 스냅샷 동기화 실패가 남아 있는 직원
SELECT id, employee_email_masked,
       request_payload->>'current_employment_error'    AS error,
       request_payload->>'current_employment_error_at' AS error_at
  FROM yeoljeong_employee_join_requests
 WHERE deleted_at IS NULL
   AND tenant_id = $1::uuid
   AND COALESCE(request_payload->>'current_employment_error', '') <> '';

-- (d) 직원 한 명의 스냅샷 교체 이력
SELECT created_at, actor, action,
       details->>'previous_contract_id' AS previous_contract_id,
       details->>'contract_id'          AS contract_id,
       details->'changes'               AS changes
  FROM yeoljeong_audit_logs
 WHERE business_id = $1
   AND resource_type = 'employee_employment'
   AND resource_id = $2
   AND details->>'tenant_id' = $3
 ORDER BY created_at;
```

감사로그 action 목록: `employment.snapshot_created`, `employment.snapshot_replaced`, `employment.snapshot_resynced`,
`payroll.contract_defaults_applied`, `payroll.contract_deviation`.
