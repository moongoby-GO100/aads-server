# 오비서 V4.1 메뉴 통합·업무별 상세 워크벤치 PRD

- 문서 ID: `AADS-OBYS-V4-1-FINAL-20260922`
- 제품 버전: V4.1
- 목업: `/static/apps/obys/mockup-v4-1.html`
- 상태: 운영 오비서 정식 진입 통합 · DB 연동 구현 기준 · 2026-09-22 반영

## 1. 목적과 사용자

V4는 8개 업무군으로 메뉴 중복을 줄였지만, 상세 화면은 V3 iframe을 재사용하여 메뉴별 정보 구조가 달라지지 않았다. V4.1은 소상공인 대표, 지점장, 회계 담당자가 첫 화면에서 자기 업무의 집계와 예외를 보고, 상위 집계에서 원문 건별까지 내려가 처리하고, 자동연동이 실패하면 같은 화면에서 엑셀·직접 등록으로 복구하도록 한다.

핵심 원칙은 `공통 셸 + 메뉴별 전용 데이터 계약`이다. UI 컴포넌트는 재사용하되 조회 계층, 지표, 목록 컬럼, 건별 상세, 행동, 오류 복구는 메뉴마다 별도 정의한다.

## 2. 메뉴 소유권

| 업무군 | 소유 기능 | 중복 방지 원칙 |
|---|---|---|
| 경영 | 통합 요약·할 일·리포트·원본문서 | 거래 입력·연동 설정을 소유하지 않음 |
| 매출·입금 | 매출·주문·카드·정산·계좌·미수·매칭 | 설정 메뉴는 인증·매핑만 담당 |
| 매입·재고 | 매입·매입처·재고·발주 | 증빙 세무판정은 세무·회계로 연결 |
| 직원·급여 | 직원·근태·급여·인사문서 | 문서보관함은 원본 보관만 담당 |
| 세무·회계 | 증빙대장·누락등록·전표·신고납부 | 현금영수증·계산서·카드전표를 한 대장에서 구분 |
| 승인·알림 | 승인·감사·알림·오류 복구 | 원 업무를 복제하지 않고 근거 화면으로 딥링크 |
| 사업자·분석 | 사업자·지점·마진·메뉴원가 | 거래 조회 대신 기준정보·분석만 소유 |
| 설정 | 자동연동 인증·권한·매핑·수집상태 | 거래 목록·수동입력을 중복 제공하지 않음 |

V4.1은 8개 업무군, 36개 고유 메뉴 경로를 제공한다. 카드 승인·취소는 독립 메뉴로 승격한다.

## 3. 첫 실행·반복 사용·실패 복구

1. 첫 실행: 통합 홈에서 오늘 할 일, 승인, 미매칭, 7일 자금을 확인한다.
2. 반복 사용: 업무 메뉴 선택 → 계층형 드릴다운 → 기간·상태·검색 필터 → 건별 행 선택 → 원문·근거·처리 이력 확인 → 업무 행동 실행 순서다.
3. 실패 복구: 자동연동 실패 시 현재 업무 화면의 `엑셀·직접 등록`을 열어 양식 선택, 파일 업로드, 컬럼 매핑, 오류·중복 검증, 임시등록, 승인을 수행한다.
4. 세션·권한 실패: 입력값과 필터를 보존하고 재로그인 또는 권한 요청 후 같은 단계로 복귀한다.
5. 모바일: 메뉴는 드로어로 전환하고, 필터·드릴다운·건별 상세·등록 단계는 데스크톱과 같은 계약을 유지한다.

## 4. 핵심 업무별 상세 계층

| 업무 | 필수 탐색 계층 | 목록·건별 상세 핵심 |
|---|---|---|
| 매출 | 사업자 → 매출처/채널 → 지점 → 주문·취소 | 주문번호, 상품, 결제, 취소 사유, 수수료, 정산 연결 |
| 매입 | 사업자 → 매입처 → 품목 → 증빙·지급 | 매입번호, 단가, 수량, 세금계산서, 지급계좌, 미지급 |
| 입금·계좌 | 사업자 → 계좌 → 입금처 → 매칭·미매칭 | 은행 원문, 적요, 예정입금, 후보 점수, 수동 연결 이력 |
| 카드 | 사업자 → 카드사 → 카드번호 → 가맹점 → 승인·취소 | 승인번호, 원승인, 취소전표, 업무/개인 후보, 증빙 연결 |
| 세무·원장 | 사업자 → 거래처 → 계정과목 → 증빙 → 분개 | 증빙 종류, 과세구분, 공급가·세액, 차대변, 승인·확정·역분개 |

각 계층 선택은 하위 KPI와 목록을 다시 조회하는 API 파라미터가 된다. 건별 상세는 화면 제목만 바꾸는 공통 팝업이 아니라 해당 업무의 원문, 연결관계, 감사 이력을 반환하는 전용 DTO를 사용한다.

## 5. 세무 증빙

세무 증빙대장은 다음 구분을 조회·등록·수정할 수 있어야 한다.

- 전자세금계산서, 전자계산서, 종이세금계산서
- 현금영수증(매출·매입), 카드매출전표, 카드매입전표
- 간이영수증, 거래명세서, 기타증빙
- 과세, 면세, 영세, 불공제와 매출·매입 방향
- 공급가액, 세액, 합계, 발행·승인번호, 거래처 사업자번호
- 원본 파일, 전표 연결, 중복·귀속 검증, 처리 이력

검증 규칙은 `공급가액 + 세액 = 합계`, 면세 세액 0원, 원천번호 중복 금지, 사업자 귀속 일치다. 검증 전 자료는 임시 상태로 저장하고 전표 후보만 생성한다.

## 6. 전표관리

| 단계 | 기능 | 통제 |
|---|---|---|
| 조회 | 사업자·거래처·계정과목·증빙·분개 계층과 일자별 목록 | 원천자료까지 추적 가능 |
| 입력 | 전표일자·적요·차변·대변·금액·증빙 연결 | 차변합계=대변합계 |
| 검토 | 임시 → 회계검토 → 대표승인 → 확정 | 금액·역할별 권한 적용 |
| 수정 | 수정전표 또는 역분개 | 확정 원본 직접 덮어쓰기 금지 |
| 마감 | 월 잠금·해제 요청 | 처리자·사유·승인자 감사로그 |

## 7. 자동연동 실패 자료 등록

등록은 화면 이동만 하는 버튼이 아니라 다음 4단계 워크플로다.

1. 등록 방식: 엑셀·CSV 일괄등록 또는 건별 직접입력.
2. 원본 입력: 사업자와 기준일을 선택하고 xlsx/csv/pdf/jpg 원본을 첨부한다.
3. 매핑·검증: 원본 컬럼을 업무 필드에 매핑하고 필수값, 형식, 합계, 중복, 귀속을 검사한다.
4. 결과: 정상·오류 건수를 분리하고 정상 건만 임시등록한다. 오류 행은 내려받아 재등록한다.

원본 해시, 업로더, 등록시각, 컬럼 매핑, 검증결과, 승인자를 감사 이벤트로 남긴다. 동일 파일·동일 원천번호 재등록은 idempotency key로 차단한다.

## 8. 메뉴별 화면 계약

모든 36개 메뉴는 다음 필드를 가진 독립 계약을 정의한다.

- `title`, `description`, `kind`
- `hierarchy[]`: 메뉴별 드릴다운 단계
- `metrics[]`: 핵심 지표 4개
- `focus[]`: 사용자 판단 관점 3개
- `columns[]`: 일자별·건별 목록 컬럼
- `detailDto`: 원문·연결자료·처리이력
- `primaryAction`, `recoveryAction`
- `registrationPolicy`: 허용 파일·필드·검증·승인 정책

정의가 없는 경로는 공통 화면으로 대체하지 않고 빌드 검증에서 실패시킨다.

### 8.1 전체 36개 페이지 전용 계약

| 업무군 | 페이지(route) | 전용 탐색·목록 | 건별 처리 |
|---|---|---|---|
| 경영 | 통합 홈(`home`) | 사업자→오늘 할 일→위험·마감, 업무·담당자·일자 | 마감·승인·미매칭 원 업무로 이동 |
| 경영 | 할 일·마감(`tasks`) | 사업자→업무영역→담당자→마감상태 | 완료 근거 등록·담당자 재지정 |
| 경영 | 경영 리포트(`reports`) | 사업자→보고기간→보고서→버전 | 손익·현금흐름 산식과 공유 버전 확인 |
| 경영 | 문서보관함(`documents`) | 사업자→문서분류→소유자→보존상태 | 원본 해시·권한·열람·만료 이력 |
| 매출·입금 | 매출 현황(`sales`) | 사업자→매출처·채널→지점→주문·취소 | 주문 원문·결제·취소·수수료·정산 연결 |
| 매출·입금 | 주문·취소(`orders`) | 사업자→채널→지점→주문상태 | 상품·옵션·결제·취소사유·환불 처리 |
| 매출·입금 | 카드 승인·취소(`cards`) | 사업자→카드사→카드번호→가맹점→승인·취소 | 승인번호·원승인·취소전표·증빙 연결 |
| 매출·입금 | 정산 대사(`settlements`) | 사업자→정산처→정산주기→대사상태 | 주문액·수수료·정산액·입금 차이 확정 |
| 매출·입금 | 계좌 현황(`accounts`) | 사업자→계좌→거래유형→은행원문 | 잔액·입출금·적요·수집시각 원문 확인 |
| 매출·입금 | 입금 예정·미수(`receivables`) | 사업자→계좌→입금처→예정·연체 | 청구·예정일·연체구간·회수 이력 |
| 매출·입금 | 입금 미매칭(`unmatched`) | 사업자→계좌→입금처→매칭·미매칭 | 추천점수·분할매칭·수동 연결·해제 근거 |
| 매입·재고 | 매입 현황(`purchases`) | 사업자→매입처→품목→증빙·지급 | 단가·수량·공급가·세액·지급계좌·미지급 |
| 매입·재고 | 매입처 현황(`suppliers`) | 사업자→매입처→계약상태→지급상태 | 계약단가·품목·미지급·증빙 품질 |
| 매입·재고 | 재고 현황(`inventory`) | 사업자→창고·지점→품목→입출고 원장 | 가용수량·안전재고·유통기한·이동 이력 |
| 매입·재고 | 발주 추천(`purchase-orders`) | 사업자→지점→매입처→발주후보 | 판매예측·현재고·리드타임·승인 근거 |
| 직원·급여 | 직원 현황(`employees`) | 사업자→지점→재직상태→직원 | 계약·직무·권한·입퇴사 상태 |
| 직원·급여 | 근태 현황(`attendance`) | 사업자→지점→직원→근태상태 | 출퇴근 원문·휴게·연장·보정 승인 |
| 직원·급여 | 급여 현황(`payroll`) | 사업자→급여월→지점→직원 | 기본급·수당·공제·실수령·지급 연결 |
| 직원·급여 | 계약·인사증빙(`hr-docs`) | 사업자→지점→직원→문서종류 | 계약 버전·전자서명·만료·열람 권한 |
| 세무·회계 | 세무 증빙대장(`tax-evidence`) | 사업자→거래처→계정과목→증빙→분개 | 계산서·현금영수증·카드전표·과세구분·전표 |
| 세무·회계 | 증빙 등록·누락(`tax-gap`) | 사업자→증빙종류→매출·매입→누락원인 | 공급가+세액·중복·귀속 검증 후 전표 후보 |
| 세무·회계 | 전표관리(`journals`) | 사업자→거래처→계정과목→증빙→분개 | 차대변 일치·승인·확정·수정전표·역분개 |
| 세무·회계 | 신고·납부(`tax-returns`) | 사업자→세목→신고기간→제출·납부 | 산출근거·제출 버전·접수번호·납부 영수증 |
| 승인·알림 | 통합 승인함(`approvals`) | 사업자→업무유형→결재단계→요청자 | 변경 전후·근거·금액별 승인·반려 |
| 승인·알림 | 완료·감사 이력(`audit-history`) | 사업자→업무영역→사용자→행위 | 세션·IP·이전값·새값·승인 이벤트 |
| 승인·알림 | 중요 알림(`alerts`) | 사업자→중요도→업무영역→조치상태 | 영향·담당·마감·재발 횟수·조치 이력 |
| 승인·알림 | 시스템 오류(`errors`) | 서비스→오류유형→영향업무→복구상태 | 누락구간·영향건수·재인증·재수집·파일대체 |
| 사업자·분석 | 사업자 현황(`businesses`) | 사업자유형→사업자→세무상태→연결자원 | 기본정보·과세유형·지점·계좌·카드·직원 |
| 사업자·분석 | 지점 현황·등록(`branches`) | 사업자→지점→영업상태→등록검증 | 주소·영업시간·담당·POS·계좌·직원 귀속 |
| 사업자·분석 | 원가·마진(`margins`) | 사업자→지점→채널→비용요소 | 매출·재료비·수수료·인건비·마진 민감도 |
| 사업자·분석 | 메뉴 원가(`menu-costs`) | 사업자→메뉴→레시피→판매채널 | 재료수량·최근단가·포장·수수료·목표가격 |
| 설정 | 자동연동 관리(`integrations`) | 서비스유형→서비스→사업자→연동상태 | 인증범위·수집주기·마지막성공·누락구간 |
| 설정 | 판매 연동(`sales-connect`) | 사업자→판매채널→지점매핑→수집상태 | 주문·취소·정산 권한과 외부매장 매핑 |
| 설정 | 은행·카드 연동(`bank-connect`) | 사업자→기관→계좌·카드→수집상태 | 금융자산 귀속·권한만료·누락기간 재수집 |
| 설정 | 세무 연동(`tax-connect`) | 사업자→세무서비스→증빙종류→수집상태 | 인증만료·증빙범위·누락기간·증빙대장 연결 |
| 설정 | 권한·감사로그(`access-log`) | 사업자→사용자→권한영역→위험도 | 역할·활성세션·민감접근·부여·회수 근거 |

페이지는 같은 카드나 표 컴포넌트를 사용할 수 있지만, 위 탐색 계층·목록 컬럼·건별 처리 계약을 다른 페이지의 것으로 대체할 수 없다. 모든 페이지의 `현재 목록 내보내기`, 목록 행, 상세 처리, 자료 등록 가능 여부와 실패 복구 버튼은 실제 상태 변화 또는 파일 생성으로 이어져야 한다.

### 8.2 36개 메뉴 데이터 원천표

판정 근거는 `app/api/obys_workspaces.py`의 실제 실행 순서다. 화면은
`/api/v1/workspaces/{businessId}/{route}/summary`와 `/records`를 호출한다.
`configs.source`: 화면 메타데이터(미사용).

포함 사업자는 중화·성신·미아·언니냉면이다. 실측 열은 이 네 사업자 순서의 건수를
기록하는 자리이며, 이번에는 DB 조회 실패로 숫자를 비웠다. 상태는 실데이터(동일조건
1건 이상), 0건(동일조건 0건), 미연동(처리 경로 없음 또는 고정 빈 응답), 조회 실패
(실측 불가)로 구분한다. 경로가 존재한다는 이유로 실데이터나 0건으로 추정하지 않는다.
권한에 따른 403은 미연동과 구분한다.

공통 사업자 필터·조회 조건(L/A/B/C/H는 각 행의 조건 일부):

- 모든 경로는 `_business` → `list_businesses`에서 `yeoljeong_businesses`의
  `tenant_id=JWT tenant_id AND deleted_at IS NULL` 목록에 `id=business_id`가 있는지
  먼저 확인한다. GENERIC SQL 자체에는 tenant_id를 추가하지 않는다.
- L: 수기·업로드 모두 `tenant_id=$1 AND business_id=$2 AND category=$3`.
  수기만 `deleted_at IS NULL`; 업로드 행에는 이 조건이 없다.
  `occurred_on >= date_from`, `occurred_on <= date_to`는 지정 시 포함 경계로 적용한다.
  `_ledger_records`는 두 조회 모두 `limit=None`으로 읽어 합친다.
- B/C: 수기 계좌·카드 행은 `tenant_id=$1 AND business_id=$2 AND deleted_at IS NULL`.
  날짜는 `(occurred_at AT TIME ZONE 'Asia/Seoul')::date`의 포함 경계이며 지정 시 적용한다.
  업로드는 L의 업로드 조건으로 각각 `category=transaction`, `category=card`를 사용한다.
  `home`만 카드·계좌 업로드를 합치지 않는다. 로컬 조회는 `limit=None`.
- A: `_authorized_acct_scope`는 JWT 사업자 소유권을 검증한 뒤 사업자별 ACCT
  tenant/company 매핑으로 범위를 정한다. 파일/rec_idx로 pivot하고 category별 marker로
  후보를 정한 뒤 표의 전표 조건을 적용한다. `occurred_on ~ '^[0-9]{8}$'`,
  `occurred_on > '19000101'`, 지정한 YYYYMMDD 시작·종료일의 포함 경계를 적용한다.
  `DISTINCT ON (occurred_on, voucher_key)`로 최신 source_file_id를 남긴다.
  voucher_key는 seq, 없으면 source_file_id/rec_idx다. `_source_rows`는 최대 2000행과
  source_total_count를 읽고 로컬 원장을 더한다. `workspace_records`는 로컬 검색·상태
  필터와 페이지를 먼저 채우고 남은 한도만 ACCT에서 검색·상태·offset 조건으로 읽는다.
  매핑 403은 로컬만, 다른 조회 오류는 실패로 전파한다.
- H: 근태·급여는 현재 DB와 `_legacy_people_rows` 결과를 id로 병합(현재 DB 우선)한다.
  기존 소유 테넌트만 허용하고 canonical 사업자 id/등록번호(없으면 이름)가 유일하게
  일치하고 ACCT 매핑이 있어야 한다. 별도 기존 원장의 같은 테이블에
  `business_id=$1 AND (to_jsonb(h)->>'deleted_at') IS NULL` 및
  `((to_jsonb(h)->>'tenant_id') IS NULL OR to_jsonb(h)->>'tenant_id'=$2)`와
  `($5::text IS NULL OR lower(to_jsonb(h)->>'employee_email')=$5)`를 적용한다.
  급여는 left(payroll_month,7), 근태는 work_date::text의 포함 날짜 조건이다.
  summary 관리자 근태/급여 병합은 `limit=None`, member 근태 summary는 500행.
  근태 records는 현재·기존 각각 offset+limit까지 읽고 병합하여 페이지를 자른다.
  member는 본인 이메일만, 이메일 없는 member는 빈 응답, viewer는 403이다.
  `yeoljeong_work_schedules`·`yeoljeong_contracts`는 records 근무판정 보강 자료이며
  출퇴근 원장 행 수에 더하지 않는다. 급여 지급 차액도 행 수에 더하지 않는다.
- GENERIC은 표의 WHERE·정렬·LIMIT를 그대로 따른다. `_filter_source_dates`는
  원천 날짜를 포함 경계로 거르고 일반 records는 `_filter`의 검색·상태 조건 뒤
  offset/limit로 자른다. summary 원천 수와 페이지 count를 혼동하지 않는다.

| 페이지(route) | 서버 처리(경로·함수) | 저장소(테이블+조건) | 포함 사업자 실측(중화·성신·미아·언니냉면) | 상태 |
|---|---|---|---|---|
| `home` | 전용 · `_source_rows` | L: `yeoljeong_manual_ledger_entries` + `yeoljeong_uploaded_ledger_rows` (`category=sales`, `category=purchase`) + C(업로드 제외) + B(업로드 제외); 사업자·날짜 조건은 L/C/B | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `tasks` | GENERIC · `_source_rows / GENERIC_ROUTE_SQL` | `FROM yeoljeong_approvals WHERE business_id=$1 ORDER BY created_at DESC LIMIT 500` | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `reports` | GENERIC · `_source_rows / GENERIC_ROUTE_SQL` | `FROM yeoljeong_tax_reports WHERE business_id=$1 ORDER BY period_end DESC LIMIT 500` | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `documents` | GENERIC · `_source_rows / GENERIC_ROUTE_SQL` | `FROM yeoljeong_uploads WHERE business_id=$1 AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 500` | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `sales` | ACCT_SOURCE · `_source_rows / workspace_records / source_transactions / _local_source_rows` | A: `source_file` + `atom_record`; `sf.company_id=매핑 company_id`, `sf.is_current IS TRUE`, `sf.domain='wehago'`; `entry_type = '1'`; A 공통 날짜·중복제거 + L: `yeoljeong_manual_ledger_entries` + `yeoljeong_uploaded_ledger_rows` (`category=sales`) | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `orders` | LEDGER · `_source_rows / _ledger_records` | L: `yeoljeong_manual_ledger_entries` + `yeoljeong_uploaded_ledger_rows` (`category=sales`); L 공통 조건 | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `cards` | ACCT_SOURCE · `_source_rows / workspace_records / source_transactions / _local_source_rows` | A: `source_file` + `atom_record`; `sf.company_id=매핑 company_id`, `sf.is_current IS TRUE`, `sf.domain='wehago'`; `nullif(card_code, '') IS NOT NULL`; A 공통 날짜·중복제거 + C: `yeoljeong_card_transactions` + `yeoljeong_uploaded_ledger_rows` (`category=card`) | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `settlements` | LEDGER · `_source_rows / _ledger_records` | L: `yeoljeong_manual_ledger_entries` + `yeoljeong_uploaded_ledger_rows` (`category=sales`); L 공통 조건 | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `accounts` | ACCT_SOURCE · `_source_rows / workspace_records / source_transactions / _local_source_rows` | A: `source_file` + `atom_record`; `sf.company_id=매핑 company_id`, `sf.is_current IS TRUE`, `sf.domain='wehago'`; `seq IS NOT NULL AND seq NOT IN ('', '0') AND (coalesce(deposit_amount,0) <> 0 OR coalesce(withdraw_amount,0) <> 0)`; A 공통 날짜·중복제거 + B: `yeoljeong_manual_bank_transactions` + `yeoljeong_uploaded_ledger_rows` (`category=transaction`) | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `receivables` | BANK · `_source_rows / list_bank_transactions / list_ledger_rows` | B: `yeoljeong_manual_bank_transactions` + `yeoljeong_uploaded_ledger_rows` (`category=transaction`); B 공통 조건 | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `unmatched` | BANK · `_source_rows / list_bank_transactions / list_ledger_rows` | B: `yeoljeong_manual_bank_transactions` + `yeoljeong_uploaded_ledger_rows` (`category=transaction`); B 공통 조건 | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `purchases` | ACCT_SOURCE · `_source_rows / workspace_records / source_transactions / _local_source_rows` | A: `source_file` + `atom_record`; `sf.company_id=매핑 company_id`, `sf.is_current IS TRUE`, `sf.domain='wehago'`; `entry_type = '2'`; A 공통 날짜·중복제거 + L: `yeoljeong_manual_ledger_entries` + `yeoljeong_uploaded_ledger_rows` (`category=purchase`) | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `suppliers` | ACCT_SOURCE · `_source_rows / workspace_records / source_transactions / _local_source_rows` | A: `source_file` + `atom_record`; `sf.company_id=매핑 company_id`, `sf.is_current IS TRUE`, `sf.domain='wehago'`; `entry_type = '2'`; A 공통 날짜·중복제거 + L: `yeoljeong_manual_ledger_entries` + `yeoljeong_uploaded_ledger_rows` (`category=purchase`) | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `inventory` | GENERIC · `_source_rows / GENERIC_ROUTE_SQL` | `FROM yeoljeong_inventory_items WHERE business_id=$1 ORDER BY name LIMIT 500` | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `purchase-orders` | GENERIC · `_source_rows / GENERIC_ROUTE_SQL` | `FROM yeoljeong_purchase_orders WHERE business_id=$1 ORDER BY order_date DESC LIMIT 500` | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `employees` | GENERIC · `_source_rows / GENERIC_ROUTE_SQL` | `FROM yeoljeong_employee_join_requests WHERE business_id=$1 AND deleted_at IS NULL AND status <> 'archived' ORDER BY requested_at DESC LIMIT 500`; approved 필터 없음 (가입 요청 조회) | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `attendance` | 전용 · `list_attendance_records / _source_rows / _legacy_people_rows` | `yeoljeong_attendance_records` WHERE `business_id=$1 AND deleted_at IS NULL`; records: `($2::date IS NULL OR work_date>=$2) AND ($3::date IS NULL OR work_date<=$3) AND ($4::text IS NULL OR lower(employee_email)=$4)`; `ORDER BY work_date DESC, start_at DESC LIMIT $5`; summary 관리자: `ORDER BY work_date DESC LIMIT 500`; member: `lower(employee_email)=$2`; H 병합 | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `payroll` | 전용 · `_source_rows / _legacy_people_rows / _with_payroll_settlement_state` | `yeoljeong_payroll_statements` WHERE `business_id=$1 AND deleted_at IS NULL AND ($2::text IS NULL OR left(payroll_month,7)>=$2) AND ($3::text IS NULL OR left(payroll_month,7)<=$3)`; `ORDER BY payroll_month DESC,id DESC` (로컬 LIMIT 없음); H 병합 후 지급 차액 계산 | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `hr-docs` | GENERIC · `_source_rows / GENERIC_ROUTE_SQL` | `FROM yeoljeong_onboarding_documents WHERE business_id=$1 AND deleted_at IS NULL ORDER BY uploaded_at DESC LIMIT 500` | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `tax-evidence` | ACCT_SOURCE · `_source_rows / workspace_records / source_transactions / _local_source_rows` | A: `source_file` + `atom_record`; `sf.company_id=매핑 company_id`, `sf.is_current IS TRUE`, `sf.domain='wehago'`; `entry_type IN ('1','2') OR nullif(card_code,'') IS NOT NULL`; A 공통 날짜·중복제거 + L: `yeoljeong_manual_ledger_entries` + `yeoljeong_uploaded_ledger_rows` (`category=sales`, `category=purchase`) | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `tax-gap` | 전용 · `_source_rows` | L: `yeoljeong_manual_ledger_entries` + `yeoljeong_uploaded_ledger_rows` (`category=sales`, `category=purchase`) + C: `yeoljeong_card_transactions` + `yeoljeong_uploaded_ledger_rows` (`category=card`); L/C 조건 (LEDGER_ROUTES의 purchase 단독 분기보다 우선) | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `journals` | JOURNAL · `_source_rows / _acct_journal_records / list_journals` | ACCT 우선: `journal_entry e LEFT JOIN journal_line l ON l.entry_id=e.id` WHERE `e.company_id=매핑 company_id`; `e.entry_date >= date_from::date`, `e.entry_date <= date_to::date` (지정 시); 전표별 GROUP BY 집계, `LIMIT 500`; 매핑 없음/403일 때만 `yeoljeong_journal_vouchers v LEFT JOIN yeoljeong_journal_lines l ON l.voucher_id=v.id` WHERE `v.tenant_id=$1 AND v.business_id=$2 AND ($3::text IS NULL OR v.status=$3)` (status=None); ACCT 0건은 폴백하지 않음 | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `tax-returns` | GENERIC · `_source_rows / GENERIC_ROUTE_SQL` | `FROM yeoljeong_tax_reports WHERE business_id=$1 ORDER BY period_end DESC LIMIT 500` | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `approvals` | GENERIC · `_source_rows / GENERIC_ROUTE_SQL` | `FROM yeoljeong_approvals WHERE business_id=$1 ORDER BY created_at DESC LIMIT 500` | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `audit-history` | GENERIC · `_source_rows / GENERIC_ROUTE_SQL` | `FROM yeoljeong_audit_logs WHERE business_id=$1 ORDER BY created_at DESC LIMIT 500` | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `alerts` | GENERIC · `_source_rows / GENERIC_ROUTE_SQL` | `FROM yeoljeong_notifications WHERE business_id=$1 ORDER BY created_at DESC LIMIT 500` | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `errors` | GENERIC · `_source_rows / GENERIC_ROUTE_SQL` | `FROM yeoljeong_delivery_collection_status WHERE business_id=$1 AND deleted_at IS NULL ORDER BY updated_at DESC LIMIT 500` | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `businesses` | 전용 · `_source_rows` | `yeoljeong_businesses` WHERE `tenant_id=JWT tenant_id AND deleted_at IS NULL`; `_business`: 승인된 목록 중 `id=business_id` 1행 선택 | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `branches` | GENERIC · `_source_rows / GENERIC_ROUTE_SQL` | `FROM yeoljeong_branches WHERE business_id=$1 AND deleted_at IS NULL ORDER BY sort_order,id LIMIT 500` | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `margins` | GENERIC · `_source_rows / GENERIC_ROUTE_SQL` | `FROM yeoljeong_manual_ledger_entries WHERE business_id=$1 AND deleted_at IS NULL ORDER BY occurred_on DESC LIMIT 500` | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `menu-costs` | GENERIC · `_source_rows / GENERIC_ROUTE_SQL` | `FROM yeoljeong_inventory_items WHERE business_id=$1 ORDER BY name LIMIT 500` | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `integrations` | GENERIC · `_source_rows / GENERIC_ROUTE_SQL` | `FROM yeoljeong_bank_accounts WHERE business_id=$1 ORDER BY updated_at DESC LIMIT 500` | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `sales-connect` | GENERIC · `_source_rows / GENERIC_ROUTE_SQL` | `FROM yeoljeong_platform_accounts WHERE business_id=$1 AND deleted_at IS NULL ORDER BY updated_at DESC LIMIT 500` | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `bank-connect` | GENERIC · `_source_rows / GENERIC_ROUTE_SQL` | `FROM yeoljeong_bank_accounts WHERE business_id=$1 ORDER BY updated_at DESC LIMIT 500` | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `tax-connect` | GENERIC · `_source_rows / GENERIC_ROUTE_SQL` | `FROM yeoljeong_tax_reports WHERE business_id=$1 ORDER BY updated_at DESC LIMIT 500` | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |
| `access-log` | GENERIC · `_source_rows / GENERIC_ROUTE_SQL` | `FROM yeoljeong_audit_logs WHERE business_id=$1 ORDER BY created_at DESC LIMIT 500` | 서버 동일조건 재현 불가 — 코드 판정 | 조회 실패 |

실측 시도 시각: 2026-11-09 09:14:51 KST. 대상 DB: `obys`.
허용된 읽기 전용 접속으로 먼저 사업자 목록을 확인하려고 실행한 SQL:

```sql
SELECT * FROM yeoljeong_businesses;
```

결과: Docker 소켓 접근이 `operation not permitted`로 거부되어 SQL은 실행되지 않았다.
포함 사업자 id·JWT tenant 매핑과 제외 사업자 부재 확인도 미완료다. 후속 count 쿼리는
실행하지 않았으며 테이블 전체 count나 다른 사본을 사용하지 않았다.
모든 36행이 **서버 동일조건 재현 불가 — 코드 판정**이다. 사업자 식별·권한·기간 및
A/H의 다중 원천 조건을 확정하지 못했으므로 숫자가 없으며 위 조건은 코드 조사 결과다.
상태 합계: 실데이터 0행 / 0건 0행 / 미연동 0행 / 조회 실패 36행.
처리 경로 합계(우선 분기 반영): ACCT_SOURCE 6 / LEDGER 2 / BANK 2 / CARD 0 /
JOURNAL 1 / GENERIC 20 / 전용 5. cards·accounts는 ACCT_SOURCE가 먼저 처리하며,
tax-gap은 LEDGER 상수 소속이지만 전용 분기가 먼저 처리한다.

### 8.3 KPI·드릴다운 계산 규칙

메뉴마다 고정 수치를 적어 두지 않는다. 지표 4칸은 다음 규칙으로 조회 결과에서 계산한다.

| 칸 | 값 | 계산 |
|---|---|---|
| 1 | 메뉴별 건수 | 조회된 행 수 |
| 2 | 확인 필요 | 행 값에 확인 필요·누락·대기·오류·미매칭·연체 등이 있는 행 수 |
| 3 | 최근 일자 | 행 값에서 찾은 `YYYY-MM-DD` 중 최대값 |
| 4 | 원천 상태 | 실데이터 / 0건 / 미연동 / 차단 / 로그인 필요 / 조회 실패 |

`실데이터`·`0건` 이 아닌 상태에서는 1~3칸을 `—` 로 둔다. 추정값을 채우지 않는다.
드릴다운 단계도 같다 — 각 단계의 선택지는 조회된 행에서 뽑은 고유값뿐이고, 값이 없으면 단계를 선택할 수 없다.
확정 지표(총매출액, 마진율 등)는 §9 의 `summary` API를 붙인 뒤 이 계산값을 대체한다.

## 9. API·권한 계약

| API | 목적 | 필수 통제 |
|---|---|---|
| `GET /api/v1/workspaces/{businessId}/{route}/summary` | KPI·예외 조회 | 사업자·역할 스코프 |
| `GET /api/v1/workspaces/{businessId}/{route}/records` | 계층·기간·상태별 목록 | cursor·정렬·필터 |
| `GET /api/v1/workspaces/{businessId}/{route}/records/{id}` | 원문·연결·감사 상세 | 민감정보 마스킹 |
| `POST /api/v1/import-sessions` | 업로드 세션 생성 | 파일 해시·idempotency key |
| `POST /api/v1/import-sessions/{id}/validate` | 매핑·검증 | 오류 행과 코드 반환 |
| `POST /api/v1/import-sessions/{id}/commit` | 임시등록·승인요청 | 원본 저장·감사 이벤트 선행 |
| `POST /api/v1/journals/{id}/reverse` | 확정 전표 역분개 | 마감·권한·승인 검사 |

세션 만료 시 작성 중인 필터와 등록 세션 ID를 보존한다. 권한 부족은 막다른 403 화면 대신 필요한 역할과 권한 요청 버튼을 제공한다.

## 10. 구현 전환 순서

1. V4.1 목업의 36개 화면 계약을 프론트 route manifest로 옮긴다.
2. 공통 셸·필터·테이블·모달을 컴포넌트화하되 화면 계약을 필수 입력으로 받는다.
3. 매출, 매입, 입금·계좌, 카드, 세무·원장 순으로 API DTO와 연결한다.
4. import session API를 연결해 파일 선택·매핑·검증·임시등록을 실제화한다.
5. 승인·감사·권한·세션 복구를 붙인다.
6. V3 iframe 의존을 제거한 뒤 V4.1 route를 정식 앱 진입점으로 전환한다.

## 11. 완료·검증 기준

- 8개 업무군, 36개 메뉴, 36개 고유 route이며 중복 route가 0개다.
- 모든 route가 전용 계층, KPI, 판단 관점, 목록 컬럼, 행동을 가진다.
- 매출·매입·입금·카드·세무가 본 PRD의 필수 계층과 정확히 일치한다.
- 모든 목록 행에서 건별 상세가 열리고 원문·연결·처리 이력이 표시된다.
- 등록 허용 화면에서 파일 선택, 매핑, 검증, 임시등록 결과까지 조작된다.
- CSV 내보내기와 양식 다운로드가 실제 파일을 생성한다.
- 빈 결과, 오류, 재시도, 세션 만료, 권한 부족의 복구 경로가 있다.
- 데스크톱 1440px와 모바일 390px에서 메뉴·필터·상세·등록 흐름을 캡처한다.
- 자바스크립트 구문 오류와 브라우저 콘솔 오류가 0건이다.
- 외부 URL이 HTTP 200이며 배포 후 5분간 P0/P1 오류가 0건이다.

## 12. 실제 구현 계약

V4.1은 더 이상 화면 안의 샘플 배열을 데이터로 사용하지 않는다. 로그인한 사용자의 JWT 테넌트로 사업자 목록을 조회하고, 선택한 사업자가 해당 테넌트 소유인지 서버에서 재검증한 뒤 아래 API를 호출한다.

| 화면 동작 | 구현 API | 정본 |
|---|---|---|
| 사업자 선택 | `GET /api/v1/workspaces/businesses` | `yeoljeong_businesses` |
| KPI·원천 상태 | `GET /api/v1/workspaces/{businessId}/{route}/summary` | route별 기존 오비서 원장 |
| 일자별·건별 조회 | `GET /api/v1/workspaces/{businessId}/{route}/records` | 매출·매입·카드·은행·전표 및 업무별 기존 테이블 |
| 건별 상세 | `GET /api/v1/workspaces/{businessId}/{route}/records/{id}` | 같은 DB row의 마스킹된 원문 |
| 엑셀 사전검증 | `POST /api/v1/workspaces/{businessId}/{route}/imports/preview` | 무기록 파싱 + DB 중복 해시 조회 |
| 엑셀 확정등록 | `POST /api/v1/workspaces/{businessId}/{route}/imports/commit` | `yeoljeong_uploads`, `yeoljeong_uploaded_ledger_rows` |
| 건별 직접등록 | `POST /api/v1/workspaces/{businessId}/{route}/records` | 수기 원장·카드·은행 정본 테이블 |

36개 route는 모두 DB 원천을 명시한다. 해당 사업자의 row가 없으면 샘플 값을 보여주지 않고 `0건`을 표시한다. 매출·매입·계좌·카드·세무증빙·전표는 각각 기존 tenant-scoped 서비스 함수를 재사용한다. 직원·급여·재고·승인·감사·설정 화면은 사업자 소유권 검증 후 해당 업무 테이블을 읽는다.

### 12.1 라일론 검증 기준 — 폐기(라일론 제외 — 2026-11-05)

- 대상 사업자: `주식회사 라일론` (`biz-lylon-e2e`)
- 검증 시 테넌트는 로그인 JWT에서만 결정하며 HTML이나 쿼리 파라미터로 받지 않는다.
- 화면에 표시된 DB 건수와 동일 조건 SQL count가 일치해야 한다.
- 조회 시작일·종료일은 KST 현재 월을 기본값으로 사용하고, 업로드 원장을 포함한 모든 원천에 같은 날짜 조건을 적용해야 한다.
- 매출·매입·계좌·카드·세무증빙·전표에서 목록 행을 누르면 같은 row ID의 상세 원문이 열려야 한다.
- 상세 원문의 토큰·비밀번호·API 키·사업자번호·이메일·전화·비마스킹 계좌번호는 서버 응답 단계에서 마스킹해야 한다.
- 업로드 미리보기는 DB를 변경하지 않고, 확정 버튼을 눌렀을 때만 저장해야 한다.
- 다른 테넌트가 `biz-lylon-e2e`를 직접 요청하면 404로 차단되어야 한다.

실측 기준값(2026-09-22 14:34 KST)은 ACCT tenant/company `11/11`, 전표
13,623건, 전표라인 27,416건, 원천파일 604개, 차변·대변 불균형 전표 0건,
전표기간 2026-01-01~2026-08-24다. 이 값은 화면의 전표관리 원천이 테스트 원장
2건으로 되돌아가지 않았는지 확인하는 배포 검증 기준이며, 화면 필터에 따라 표시
건수는 달라질 수 있다.

### 12.2 포함 사업자 검증 기준

대상은 중화·성신·미아·언니냉면이다. 12.1의 폐기된 사업자·건수를 검증 기준으로
사용하지 않는다. 8.2의 서버 처리·사업자 권한·날짜 조건으로 조회한 건수와 화면을
대조한다. 동일조건을 재현하지 못한 행은 조회 실패로 남기며 0건으로 취급하지 않는다.
현재는 DB 접근 실패로 사업자 목록과 건수 실측이 미완료다.

- 테넌트는 로그인 JWT에서만 결정하고 사업자 소유권 검증을 먼저 수행한다.
- 날짜는 KST 현재 월을 기준으로 화면과 서버에 동일하게 적용한다.
- 목록과 상세는 같은 row ID로 대응하고 민감 원문은 서버에서 마스킹한다.
- 업로드 미리보기는 DB를 변경하지 않고 확정 시에만 저장한다.
- 다른 테넌트의 사업자 직접 요청은 404로 차단한다.

## 13. 운영 오비서 통합 계약

V4.1은 별도 검토 링크가 아니라 인증 후 오비서의 기본 업무 화면이다. 기존
`index.html`은 로그인·회원가입·초대·계약 링크 처리를 계속 소유하고, 유효한 인증
토큰이 확인되면 `mockup-v4-1.html#home`으로 전환한다. 로그인 성공 뒤에도 같은
전환을 수행한다.

- `?legacy=1`은 장애 복구와 아직 V4.1에 옮기지 않은 기존 관리 기능을 위한 명시적
  롤백 진입점이다.
- 레시피 redirect, 직원 가입·초대, 계약서 딥링크는 기존 흐름을 우선하고 V4.1로
  강제 전환하지 않는다.
- V4.1에서 여러 사업자를 조회할 수 있으면 사업자 선택기를 노출하며, 선택값은
  `business_id`로 API에 전달되고 서버가 JWT tenant 소유권을 다시 검증한다.
- 세션이 만료되면 입력·필터 화면을 막다른 오류로 끝내지 않고 다시 로그인 및
  재시도 버튼을 표시한다.
- 로그아웃은 서버 세션 종료를 시도한 뒤 브라우저 토큰·쿠키를 제거하고 기존 로그인
  화면으로 돌아간다.

운영 통합 완료 판정은 `index.html 로그인 → V4.1 자동 진입 → 포함 사업자 선택 → 동일조건
DB 조회 → 건별 상세 → 로그아웃/재로그인 복구`를 데스크톱과 모바일에서 확인한 때다.
