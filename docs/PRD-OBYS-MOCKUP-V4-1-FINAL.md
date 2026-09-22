# 오비서 V4.1 메뉴 통합·업무별 상세 워크벤치 PRD

- 문서 ID: `AADS-OBYS-V4-1-FINAL-20260922`
- 제품 버전: V4.1
- 목업: `/static/apps/obys/mockup-v4-1.html`
- 상태: 최종 구현 기준

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

목업 `mockup-v4-1.html` 의 `configs[route].source` 와 1:1로 대응한다. 목업은 이 표에 적힌
경로만 호출하고, 응답이 없거나 막히면 그 상태를 그대로 화면에 표시한다. 샘플 행과 고정 KPI는 두지 않는다.

실측 기준: 2026-09-22 12:5x KST, 라일론 = `obys.yeoljeong_businesses.id='biz-lylon-e2e'`,
tenant `d1695f15-6b68-4929-bc8d-646827363ff9`(ACCT tenant id 11).
상태는 `실데이터`(1건 이상) · `0건`(연결되나 자료 없음) · `차단(403)`(테넌트 게이트) · `미연동`(원천 API 없음) 넷이다.

| 페이지(route) | 현행 원천 API | 저장소 | 라일론 실측 | 상태 |
|---|---|---|---|---|
| `home` | `GET /yeoljeong-dashboard/kpis` | obys 집계(dashboard.get_kpis) | 게이트 차단 | 차단(403) |
| `tasks` | `GET /yeoljeong-dashboard/tasks` | obys 집계(dashboard.get_tasks) | 게이트 차단 | 차단(403) |
| `reports` | 없음 | 리포트 저장소 미정 | — | 미연동 |
| `documents` | `GET /yeoljeong-finance/onboarding/documents` | `obys.yeoljeong_onboarding_documents` | 0건(전체 41건) | 차단(403) |
| `sales` | `GET /yeoljeong-finance/sales` | `obys.yeoljeong_delivery_sales` | 0건(전체 3,813건) | 차단(403) |
| `orders` | 없음 | 주문 원문 저장소 미정 | — | 미연동 |
| `cards` | `GET /yeoljeong-finance/card-transactions` | `obys.yeoljeong_card_transactions` | 1건 | 실데이터 |
| `settlements` | `GET /yeoljeong-finance/settlements` | `obys.yeoljeong_delivery_settlements` | 0건(전체 3,551건) | 차단(403) |
| `accounts` | `GET /yeoljeong-finance/ledger-bank-transactions` | `obys.yeoljeong_manual_bank_transactions` | 1건 | 실데이터 |
| `receivables` | 없음 | 미수 저장소 미정 | — | 미연동 |
| `unmatched` | 없음 | 매칭 결과 저장소 미정 | — | 미연동 |
| `purchases` | `GET /yeoljeong-finance/ledger-entries?category=purchase` | `obys.yeoljeong_manual_ledger_entries` | 2건 | 실데이터 |
| `suppliers` | 없음 | 거래처 마스터 미정 | — | 미연동 |
| `inventory` | `GET /yeoljeong-inventory/items` | `obys.yeoljeong_inventory_items` | 0건 | 0건 |
| `purchase-orders` | `GET /yeoljeong-inventory/orders` | `obys.yeoljeong_purchase_orders` | 0건 | 0건 |
| `employees` | `GET /yeoljeong-finance/employees/approved` | `obys.yeoljeong_employee_join_requests` | 0건(전체 17건) | 차단(403) |
| `attendance` | 없음 | `obys.yeoljeong_attendance_records` | 0건 | 미연동 |
| `payroll` | `GET /yeoljeong-finance/payroll` | `obys.yeoljeong_payroll_statements` | 0건(전체 2건) | 차단(403) |
| `hr-docs` | `GET /yeoljeong-finance/contracts` | `obys.yeoljeong_contracts` | 0건(전체 29건) | 차단(403) |
| `tax-evidence` | `GET /yeoljeong-finance/integration-evidence` | obys 증빙 파일 저장소 | 0건 | 차단(403) |
| `tax-gap` | `GET /yeoljeong-finance/uploaded-ledger?category=purchase` | `obys.yeoljeong_uploaded_ledger_rows` | 3건 | 실데이터 |
| `journals` | `GET /yeoljeong-finance/journals` | `obys.yeoljeong_journal_vouchers` | 2건 | 실데이터 |
| `tax-returns` | `GET /yeoljeong-accounting/tax-reports` | `obys.yeoljeong_tax_reports` | 0건 | 차단(403) |
| `approvals` | `GET /yeoljeong-ops/approvals` | `obys.yeoljeong_approvals` | 0건 | 차단(403) |
| `audit-history` | `GET /yeoljeong-ops/audit-logs` | `obys.yeoljeong_audit_logs` | 0건 | 차단(403) |
| `alerts` | `GET /yeoljeong-ops/notifications` | `obys.yeoljeong_notifications` | 0건 | 차단(403) |
| `errors` | `GET /yeoljeong-finance/collection-status` | `obys.yeoljeong_delivery_collection_status` | 0건(전체 4,967건) | 차단(403) |
| `businesses` | `GET /yeoljeong-finance/tenant-registry/businesses` | `obys.yeoljeong_businesses` | 1건 | 실데이터 |
| `branches` | 없음 | `obys.yeoljeong_branches`(전체 5건) | 0건 | 미연동 |
| `margins` | 없음 | 원가 분석 저장소 미정 | — | 미연동 |
| `menu-costs` | 없음 | 레시피 마스터 미정 | — | 미연동 |
| `integrations` | `GET /yeoljeong-finance/automation` | obys 자동화 상태 집계 | 게이트 차단 | 차단(403) |
| `sales-connect` | `GET /yeoljeong-finance/completion-matrix` | `obys.yeoljeong_delivery_collection_status` 집계 | 게이트 차단 | 차단(403) |
| `bank-connect` | `GET /yeoljeong-finance/bank-accounts` | `obys.yeoljeong_bank_accounts` | 0건(전체 6건) | 차단(403) |
| `tax-connect` | 없음 | 세무 연동 상태 저장소 미정 | — | 미연동 |
| `access-log` | `GET /yeoljeong-ops/audit-logs` | `obys.yeoljeong_audit_logs` | 0건 | 차단(403) |

합계 36개 = 실데이터 6 · 0건 2 · 차단(403) 18 · 미연동 10.

`차단(403)` 은 `app/core/obys_tenant.py` 의 레거시 허용목록(`15055cac-…` 1개) 때문이다.
라일론 테넌트는 허용목록에 없고, 목록에 없는 테넌트가 통과할 수 있는 경로는
`/session` `/tenant-registry` `/uploads` `/uploaded-ledger` `/ledger-entries`
`/card-transactions` `/card-uploads` `/ledger-bank-transactions` `/journals` 뿐이다.
그래서 라일론이 실제 자료를 볼 수 있는 화면은 6개이고, 나머지 18개는 자료 유무와 무관하게 403이다.

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

## 12. 라일론 DB 연동 실측 (2026-09-22)

목업을 실데이터 계약으로 바꾸면서 라일론 사업자로 원천을 실측했다. 결론은 **오비서 화면에서 라일론이
볼 수 있는 실자료는 6개 메뉴, 합계 10행**이고, 라일론의 회계 자료 대부분은 오비서가 아니라 ACCT 원장에만 있다는 것이다.

| 구분 | 실측값 | 근거 |
|---|---|---|
| obys DB 라일론 자료 | card_transactions 1 · journal_vouchers 2 · manual_ledger_entries 2 · manual_bank_transactions 1 · uploaded_ledger_rows 3 · uploads 3 · businesses 1 | `obys` DB, `business_id='biz-lylon-e2e'` |
| obys DB 라일론 0건 | delivery_sales · delivery_settlements · bank_accounts · branches · contracts · onboarding_documents · platform_accounts · inventory_items · purchase_orders | 같은 조회 |
| ACCT DB 라일론 자료 | `journal_entry` 13,623건(2026-01-01~2026-08-24) · `journal_line` 27,416건 · company 1 | ACCT DB tenant_id=11 |
| ACCT DB 라일론 0건 | canonical_transaction · canonical_document · raw_document · import_job · source_connection · audit_event · period_lock | 같은 조회 |
| 연동 상태 | `GET /yeoljeong-finance/journals` 응답의 `integration.acct = "not_connected"` | `app/api/obys_finance.py` |
| 테넌트 게이트 | 라일론 tenant는 레거시 허용목록에 없어 18개 메뉴가 403 | `app/core/obys_tenant.py` |

따라서 "오비서 V4.1에서 라일론 실데이터를 본다"는 두 가지를 먼저 풀어야 성립한다.

1. **테넌트 게이트 해제 조건** — `yeoljeong-*` 라우터의 WHERE 절에 테넌트 조건을 넣어야 허용목록을 지울 수 있다.
   지금 허용목록을 늘리면 라일론이 열정국밥 자료를 보게 된다(게이트가 막고 있던 바로 그 사고다).
2. **ACCT 원장 13,623건 연결** — 오비서의 `journals` 는 `obys.yeoljeong_journal_vouchers`(2건)를 보고 있고
   ACCT `journal_entry`(13,623건)와 이어져 있지 않다. 세무·회계 4개 메뉴의 실데이터는 이 연결 없이는 나오지 않는다.

이 두 가지가 끝나기 전까지 목업은 `차단(403)` 과 `미연동` 을 숨기지 않고 그대로 표시한다.
화면에 수치가 보이지 않는 것은 목업의 결함이 아니라 원천의 현재 상태다.

## 13. M1 완료 판정 (목업·PRD의 실데이터 계약 전환)

- 36개 메뉴 계약 = 36개 `configs` 항목, 중복 route 0개. (파일 실측)
- 샘플 행 생성기(`rows()`·`sampleValue()`)와 고정 KPI 문자열 제거 — 하드코딩 금액·건수·비율 0건.
- 모든 값은 §8.2 의 원천 API 응답에서만 나오며, 없으면 미연동·0건·차단·로그인 필요를 표시한다.
- HTML 파서 오류 0건, 자바스크립트 구문 오류 0건, 외부 URL HTTP 200.
- 남은 항목: §9 의 `summary`·`records`·`import-sessions` API 미구현, 건별 처리 API 미구현, §12 의 두 선행 조건.
