# 오비서 급여시트 3개 지점 → 급여내역서(초안) 반영 결과

TASK_ID: ACCT-OBYS-PAYROLL-SHEET-IMPORT-3STORES-20261006 (자동 재작업 라운드 1/2, 원 시도 runner-8b6d03e3)
실행: 2026-10-06 KST, 운영 원본 = 카페24 114 `acct-pg` / DB `obys` (SSH 포트 **7916**).

## 결과 요약

| 항목 | 전 | 후 |
|---|---:|---:|
| `yeoljeong_payroll_statements` 전체 행 | 0 | **53** (전부 `draft`, `confirmed` 0건) |
| `biz-mia` 가입요청 상태 | approved 5 | approved 1 + **rejected 4** (E2E 4건) |
| 앱 코드 변경 | - | 없음 (이 README 외 변경 파일 없음) |

- 급여내역서 53행 / gross 합계 **155,446,735원**. 시트 본표(규칙 ② 제외 반영)를 같은 스크립트로 다시 계산한 값과 **건수·gross 모두 일치**(아래 대조표).
- 반영 방식: 정식 API(`save_payroll`)가 아니라 **트랜잭션 INSERT**. 이유: API 는 `net_pay` 를 `gross−공제` 로 재계산하고 `max(0, …)` 로 자르므로(`yeoljeong_finance_service.py:5823` 부근) 시트의 실 입금액·음수 정산 행을 보존할 수 없고, 운영 테넌트 관리자 로그인은 CEO 승인(policy=ask) 대상이라 쓰지 않았다.
- 상태: `draft`, `confirmed_by=''`, `confirmed_at=NULL`, `created_by=system:ACCT-OBYS-PAYROLL-SHEET-IMPORT-3STORES-20261006`.
- tenant_id: 세 사업자 모두 `yeoljeong_business_tenant_mapping` 과 가입요청의 `15055cac-…` (FK 조건 `WHERE EXISTS` 로 확인 후 INSERT).
- 공제: 시트는 차감액 한 칸뿐이라 세금/4대보험 구분 불가 → 코드 규칙대로 전액 `other_deduction`(tax/insurance = 0). 차감액 칸이 빈 이용훈 2026-05~08 은 `gross−net` 으로 채우고 payload `deduction_derived_from_gross_minus_net=true`.
- 금액은 원 단위 반올림(0.5 올림), 원값은 payload `raw_gross/raw_net/raw_deduction` 에 보존.
- payload: `import_task`, `source_sheet_id`, `source_tab`, `source_row`, `source_notes`(원문 메모, 10자리 이상 숫자열은 생략), 9월 "확인 필요" 행은 `source_flag='확인 필요'`(3행). 계좌·전화는 payload 에 넣지 않았고, 이메일은 컬럼 외에는 마스킹본만 있다.
- 멱등: id = `uuid5(사업자|월|이름|시트 행|탭)`, `ON CONFLICT (id) DO NOTHING`. **같은 INSERT 를 한 번 더 실행해 `INSERT 0 0`, 건수 53 유지**를 확인했다.

## STEP 0 — 기존 구현 분류

| 대상 | 분류 | 비고 |
|---|---|---|
| `save_payroll`(:5823), `_db_upsert_ledger` payroll 분기(:1125) | 유지 | 컬럼·상태 기본값(`draft`)·payload 구조를 그대로 따라 INSERT |
| `review_join_request`(:2785) | 유지 | 이것이 하는 필드 변경(`status/review_memo/reviewed_by/reviewed_at/updated_at` + payload)을 SQL 로 동일하게 적용 |
| `_db_row_to_record` | 유지 | `status` 를 **payload 우선**으로 읽으므로 E2E 비활성은 컬럼과 payload 둘 다 바꿨다 |
| 신규 / 수정 / 삭제 | 없음 | 코드·스키마 변경 없음 |

## 규칙 ① 이메일 NULL — 실DB 실측

운영 DB 에서 `BEGIN; INSERT … employee_email=NULL …; ROLLBACK;` 을 실행했다. 결과:

```
ERROR:  null value in column "employee_email" of relation "yeoljeong_payroll_statements" violates not-null constraint
```

롤백 뒤 payroll 건수 0 유지. 따라서 규칙 ① 의 조건("NULL 을 거부하면 그 직원은 넣지 말고 목록·사유 보고")에 해당한다. 이메일을 지어내거나 빈 문자열을 넣지 않았다. 이메일이 시트의 **같은 이름·같은 사업자 다른 월 탭**에 정확히 하나만 있을 때만 그 이메일을 가져왔다(소창영·김기훈·이승호 2026-09 3행, payload `employee_email_source=same_name_other_tab`). 후속 작업 `acct-obys-payroll-email-nullable-20261006` 이 핸드오버에 이미 있다.

## 본표 215행 분류 (상호배타 — 아래 우선순위로 첫 일치만 적용)

우선순위: ①소계 행은 사람 행이 아니라 제외(215에 포함 안 함) → ②이름 빈 행 → ③제외(규칙 ②) → ④CEO 지정 이름이지만 행 표기가 제외 기준에 안 걸림 → ⑤오병재·오병용 사업소득(미아·중화) → ⑥이메일 없음 → ⑦금액 0 → ⑧반영. 별칭(괄호) 여부는 분류가 아니라 **표시**이므로 겹쳐도 한 번만 센다.

| 분류 | 성신여대 | 미아 | 중화 | 합계 | 처리 |
|---|---:|---:|---:|---:|---|
| 반영(draft INSERT) | 16 | 37 | 0 | **53** | 완료 |
| 제외(규칙 ②) | 35 | 12 | 8 | 55 | 급여대장 미반영 |
| 이메일 없음 | 17 | 34 | 14 | 65 | 규칙 ① 미반영 |
| 확인 필요 — 오병재·오병용 사업소득 | 0 | 21 | 8 | 29 | 미반영 |
| 확인 필요 — CEO 지정 이름·표기 불일치 | 10 | 0 | 0 | 10 | 미반영 |
| 확인 필요 — 이름 칸 빈 행 | 0 | 0 | 2 | 2 | 미반영 |
| 금액 0 | 1 | 0 | 0 | 1 | 미반영(0원 초안 방지) |
| **합계** | 79 | 104 | 32 | **215** | |

(지점별 소계는 반영 16+35+17+10+1=79 / 37+12+34+21=104 / 0+8+14+8+2=32. 합이 215.)

분류 재현: 월 탭(`YYYY년MM월급여_…`/`YY년MM월분…`)에서 A열 `이름`·B열 `계좌번호` 헤더 행부터 `합계` 행 전까지를 본표로 읽고, 헤더 이름으로 열을 찾았다(`총 급여`, `실 입금 급여`, `차감액(세금,4대보험)`, `이메일`/`이메일주소`, `특이사항`·`비고`·`급여신고`·`소득신고(종류)`). 제외 기준 문구는 `신고안함`, `대표`(대표남편·대표가지급금처리 포함), `사업자에서 지급`, `언니냉면`. 스크립트(`parse_sheets.py`, `classify.py`)는 contabo116 `/root/backup/obys-payroll-import-20261006/` 에 있다(개인정보 없음, 시트 사본은 삭제됐으므로 재실행하려면 다시 내려받는다).

## 대조표 — 지점×월 (반영분, gross 원)

시트 값 = 분류 스크립트의 `반영` 행 합계, DB 값 = 운영 `SELECT business_id, payroll_month, count(*), sum(gross_pay)`. 모든 칸 일치.

| 지점 | 월 | 건수 | gross 합계 |
|---|---|---:|---:|
| 성신여대 | 2025-10 | 2 | 4,177,425 |
| 성신여대 | 2025-11 / 12 / 2026-01 | 각 2 | 각 7,000,000 |
| 성신여대 | 2026-02 / 03 | 각 1 | 각 3,500,000 |
| 성신여대 | 2026-04 | 1 | 3,220,430 |
| 성신여대 | 2026-05 ~ 09 | 각 1 | 각 3,000,000 |
| 미아 | 2025-11 | 4 | 11,750,000 |
| 미아 | 2025-12 | 4 | 12,600,000 |
| 미아 | 2026-01 | 4 | 13,250,000 |
| 미아 | 2026-02 | 4 | 12,700,000 |
| 미아 | 2026-03 | 4 | 9,796,780 |
| 미아 | 2026-04 | 3 | 7,650,740 |
| 미아 | 2026-05 | 3 | 7,640,250 |
| 미아 | 2026-06 | 3 | 7,689,790 |
| 미아 | 2026-07 | 3 | 7,739,320 |
| 미아 | 2026-08 | 3 | 7,632,000 |
| 미아 | 2026-09 | 2 | 6,600,000 |
| 중화 | 전체 | 0 | 0 |
| **합계** | | **53** | **155,446,735** |

- 반영 인원: 성신여대 소창영 9행 + 김진영(주찬) 4행(별칭 이름 그대로), 미아 김기훈·이승호 각 11행, 이용훈 9행, 조근석 4행(상세는 월별 건수로 위 표에 포함).
- 중화는 시트에 이메일 칸이 전혀 없어 0행이다(전원 이메일 없음).
- 값이 눈에 띄는 행: 미아 조근석 2026-03(gross 96,780 < net 166,220, 차감액 −69,440 — 정산 행, 시트 그대로 `other_deduction=-69440`, `net−(gross−공제)=0` 이라 불일치는 아님). 미아 김기훈 2026-03 차감 1,006,260.

## 제외 목록 (규칙 ②, 급여대장 미반영) — 55행

| 지점 | 이름 | 행 수 | 사유(시트 표기) |
|---|---|---:|---|
| 성신여대 | 김영주 | 9 | 비고/신고 `대표`·`신고안함` |
| 성신여대 | 오병재(김영주) | 11 | `대표남편`·`신고안함` 또는 오병재 신고안함 지점 |
| 성신여대 | 김영주(오병재) | 1 | 위와 같은 사람의 표기 뒤집힘(2025-10) |
| 성신여대 | 오병용 | 12 | `신고안함` |
| 성신여대 | 홍경애 | 1 | 2026-08 비고 `언니냉면사업자에서 지급함` |
| 성신여대 | 홍경애(고민수) | 1 | 2026-09 `언니냉면에서 지급` |
| 미아 | 최미미 | 11 | `대표`(CEO 지정, 메모 없는 달 포함) |
| 미아 | 오병용 | 1 | 2026-01 `신고안함` |
| 중화 | 오윤희 | 5 | `대표`·`대표가지급금처리`(CEO 지정, 메모 없는 3행 포함) |
| 중화 | 하티꾸인 느(…) | 3 | `대표가지급금처리`(외국인) |

## 확인 필요 목록 (미반영)

**성신여대 — CEO 지정 이름인데 행 표기가 제외 기준과 다름 (10행).** 원 시도는 이 중 김영주 두 줄을 "둘 다 제외"로 추측했다. 이번에는 **행 표기와 이메일로 구분**했다.

- 성신여대 `김영주`(2026-03·04) 사업소득 줄: `신고안함·대표` 줄과 별개이고, **이메일이 `김진영(주찬)`·`홍경애` 행과 동일**하다(해시 비교: 세 이름이 한 이메일 그룹, 소창영만 다른 그룹). 대표 본인 줄(신고안함)은 제외했고, 이메일이 붙은 사업소득 줄은 야간 근무자(김진영(주찬)) 계열로 보이나 CEO 가 이름을 직접 지정해 자동 반영하지 않았다.
- `홍경애` 2026-05·06·07·09, `홍경애(고민수)` 2026-08, `고민수` 2026-07, `김영주` 2025-11·12(금액 소액, 메모 없음).
- 결정 요청: 위 이메일 공유 그룹을 같은 사람으로 보고 반영할지. 반영하려면 이 10행을 같은 스크립트로 재실행하면 된다(멱등).

**오병재·오병용 사업소득 (미아 21행, 중화 8행).** 미아 오병재는 gross 5,170,630.816959… 가 매달 고정된 값이고, 중화 오병재 2026-06 은 net 이 비어 있다. 사업주 가족의 사업소득이라 판정이 애매해 넣지 않았다.

**중화 이름 칸이 빈 행 2행(2026-07·08, 4,000,000원).** 2026-09 탭의 같은 위치가 `오윤희 / 대표가지급금처리` 라 같은 성격으로 추정되나 시트에 이름이 없어 단정하지 않았다.

**9월분 이메일 승계.** 소창영·김기훈·이승호 2026-09 는 이메일 칸이 비어 있어 이전 달 같은 이름의 이메일을 가져왔다(payload 표시). 승계가 맞는지 확인이 필요하다(3행, `source_flag='확인 필요'`).

## 이메일 없음 — 미반영 (규칙 ①, 65행, 행 기준)

- 성신여대: 칸다 9, 김세현 3, 이동규 3, 양재혁 1, 최석 1 (+ 9월 외 없음) — 합 17
- 미아: 칸다 10, 김정환 7, 주찬(김진영) 6, 김환희 3, 김진영 2, 이동규 2, 강세원 1, 남민정 1, 신재민(김환희) 1, 양재혁 1 — 합 34
- 중화: 홍석빈 4, 하영훈 4, 유선명(김수진) 3, 김민우 2, 양재혁 1 — 합 14

참고(CEO 결정용): 운영 가입요청 DB 에는 중화 홍석빈·하영훈·김민우·양재혁(승인), 미아·성신여대 양재혁(승인)이 이미 이메일과 함께 있다. 동명이라는 이유만으로 이메일을 붙이면 본인 확인 없이 급여가 계정에 연결되므로 **붙이지 않았다**. 연결하려면 이름 일치 + 본인 확인을 CEO 가 승인해야 한다.

## 동일인 확인 필요 (규칙 ④, 합치지 않고 시트 원문 이름 그대로)

- 성신여대 `김진영(주찬)`(반영 4행) / 미아 `주찬(김진영)`(6행, 이메일 없음) / 미아 `김진영`(2행)
- 성신여대 `김진영(주찬)` = `김영주`(사업소득 줄) = `홍경애` — 이메일 동일(위 참조)
- 미아 `신재민(김환희)` / 미아 `김환희`
- 중화 `유선명(김수진)`, 중화 `하티꾸인 느(HÀ THỊ QUỲNH NHƯ)`
- 성신여대 `오병재(김영주)` / `김영주(오병재)` 표기 뒤집힘, `홍경애(고민수)` / `고민수`

## 미아 `2025년 성과급여 현금지급분` (급여내역서로 넣지 않음)

전원 비고 `미신고`, 시트 메모 "25년 12월 신고시 반영부탁드립니다".

| 이름 | 추석상여금 | 성과급 | 총 성과급 현금 |
|---|---:|---:|---:|
| 강세원 | 300,000 | 100,000 | 400,000 |
| 김기훈 | 100,000 | 1,500,000 | 1,600,000 |
| 이승호 | 100,000 | 1,500,000 | 1,600,000 |
| 이용훈 | 100,000 | 0 | 100,000 |
| 김환희 | 100,000 | 0 | 100,000 |
| 합계 | 700,000 | 3,100,000 | 3,800,000 |

통장인출 3,550,000 + 현금매출 지급 250,000 = 3,800,000(합계와 일치).

## 규칙 ⑤ 미아 E2E 시험 가입요청 4건 — 비활성(rejected)

대상 4건(전부 `biz-mia`, approved, 이름이 `E2E ` 로 시작): `E2E 단기알바`, `E2E 외국인 정규직`, `E2E 3.3프리랜서 20260716100721`, `E2E A4근로 20260716100721`. 같은 사업자의 실직원 `양재혁` 은 approved 로 그대로다.

- 코드의 정식 전이 `review_join_request(action="rejected")` 와 같은 필드를 SQL 로 갱신했다: `status='rejected'`, `review_memo`, `reviewed_by='system:<TASK_ID>'`, `reviewed_at`, `updated_at`, 그리고 **payload 의 같은 키**(읽기 경로가 payload 우선).
- 정식 API(`review_join_request_with_membership`)를 쓰지 않은 이유: 운영 관리자 로그인이 필요하고, 이 경로의 부가 효과인 멤버십 회수는 대상이 없다 — 4건의 이메일은 AADS 인증 DB `saas_users` 에 **0건**이다(건수만 조회, 이메일 미출력). `saas_users` 는 건드리지 않았다. 삭제(deleted_at)도 하지 않았다.
- 서비스 읽기 경로 확인: 운영 컨테이너에서 `_read_hr("employee_join_requests")` → 미아 `rejected 4 / approved 1`, `list_approved_employees(biz-mia)` → 1명.

## 검증 증빙

운영 SQL(읽기 전용 포함):

```sql
select business_id, payroll_month, count(*), sum(gross_pay), sum(net_pay), sum(other_deduction) from yeoljeong_payroll_statements where deleted_at is null group by 1,2 order by 1,2;   -- 23개 (지점×월) 행, 위 대조표와 일치
select count(*), sum(gross_pay), count(*) filter (where status<>'draft'), count(*) filter (where statement_payload->>'source_flag'='확인 필요'), count(*) filter (where tenant_id<>'15055cac-71b0-45ec-b714-7093dde189ff') from yeoljeong_payroll_statements;   -- 53, 155446735, 0, 3, 0
```

- 개인정보 스캔: payload 에서 전화(`01x-…`)·9자리 이상 숫자열 패턴을 찾았더니 3행이 걸렸으나 **전부 행 `id`(UUID) 안의 숫자 우연 일치**였다. `statement_payload - 'id'` 로 다시 스캔하면 0건.
- **화면 캡처는 하지 못했다.** `/static/apps/obys/index.html` 급여 화면은 운영 테넌트 관리자 로그인이 필요한데, Vault 의 `fb.newtalk.kr` 자격은 `owner=CEO / policy=ask`(사용마다 CEO 승인)이고 `obys-e2e` Vault 계정은 시험 회사(A/B) 소유자라 이 테넌트의 급여를 볼 수 없다. 대체로 **운영 앱 컨테이너(`acct-app-candidate-r12`)에서 서비스 읽기 함수 `list_payroll` 을 읽기 전용으로 호출**했다: 53행, `draft` 53(미아 37 / 성신여대 16), `payroll_validation_errors` 0, 이메일은 마스킹본(`yo******@naver.com` 형태)으로만 노출. 임시 스크립트 `/tmp/readpath.py` 가 그 컨테이너 `/tmp` 에 남았다(권한 문제로 삭제 실패, 개인정보·시크릿 없음 — 다음 후보 컨테이너 교체 시 사라진다).

## 백업·되돌리기

- 백업: contabo116 `/root/backup/obys-payroll-import-20261006/obys_two_tables_before_20261006.sql` (pg_dump `-t yeoljeong_payroll_statements -t yeoljeong_employee_join_requests`, 권한 600, sha256 `eb9a330bef00c0a83afb238eb0509653c2d7da274e09d3006dd116c21707953f`). 가입요청 이메일·전화가 있어 이 저장소에는 올리지 않는다.
- 이번에 넣은 행만 되돌리기(소프트 삭제):

```sql
UPDATE yeoljeong_payroll_statements
   SET deleted_at = NOW()
 WHERE statement_payload->>'import_task' = 'ACCT-OBYS-PAYROLL-SHEET-IMPORT-3STORES-20261006'
   AND deleted_at IS NULL;
```

- E2E 4건 되돌리기: 위 백업 파일에서 해당 4개 id 의 원래 `reviewed_by`·`review_memo`·`request_payload` 로 복원한다(되돌릴 이유가 없다면 하지 않는다). 최소한의 상태 복원:

```sql
UPDATE yeoljeong_employee_join_requests
   SET status='approved', updated_at=NOW(),
       request_payload = request_payload || '{"status":"approved"}'::jsonb
 WHERE business_id='biz-mia' AND status='rejected'
   AND reviewed_by='system:ACCT-OBYS-PAYROLL-SHEET-IMPORT-3STORES-20261006';
```

## 정리

- 내려받은 xlsx 사본과 `/tmp/obys_payroll_sheets/` 는 삭제했다(계좌·전화 포함). 삭제 후 `ls -d /tmp/obys_payroll_sheets` → `No such file or directory`, `find / -xdev -name '{mia,junghwa,sungshin}.xlsx'` → 0건. (재확인을 위해 두 번 더 내려받았고 그때마다 삭제했다.) 114 서버 `/tmp` 에 올렸던 SQL 파일도 삭제했다.
- 계좌번호·전화번호·주민번호·이메일 원문은 이 문서·커밋·로그 어디에도 적지 않았다.
- 금지 사항 준수: 기존 실데이터 수정·삭제 없음(수정은 E2E 시험 4건 한정), 서명본·계약 생성 없음, DROP/TRUNCATE 없음, aads-server/postgres/litellm 재시작·이미지 재빌드·배포 없음, 진아서버 미사용, `--no-verify` 미사용.

## CEO 결정이 필요한 것

1. 이메일 없는 65행: `employee_email` NULL 허용(후속 작업 `acct-obys-payroll-email-nullable-20261006`) 후 재적재할지, 또는 가입요청 이메일과 동명 연결을 승인할지.
2. 성신여대 `김영주`(사업소득)·`홍경애`·`고민수` 10행: 이메일이 김진영(주찬)과 같아 한 사람으로 보이는데 반영할지.
3. 오병재·오병용 사업소득(미아·중화 29행) 처리 기준, 중화 이름 빈 2행.
4. 9월분 3행(소창영·김기훈·이승호)의 이메일 승계가 맞는지.
