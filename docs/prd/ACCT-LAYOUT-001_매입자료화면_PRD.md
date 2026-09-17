# ACCT-LAYOUT-001 — 매입자료 화면 PRD / 설계서

- 작성: 2026-09-17 21:0x KST
- 프로젝트: ACCT(회계원장) + FOOD(오비서) 통합 경영관리시스템
- 상위 목표: `통합 경영관리시스템 구축 — ACCT 회계원장 + 오비서(FOOD) 단일 서비스`
- 마일스톤: M6 매입자료 화면
- 상태: 설계 확정 대기(CEO 승인 2건 — DB 분리 방식, Phase 1 착수)
- 목업: https://fb.newtalk.kr/static/preview/acct-purchase-mockup.html

---

## 0. 한 장 요약

오비서에서 **식자재·소모품 매입 전표를 등록/승인**하고, 승인된 전표가 **ACCT 회계원장의
분개 초안(`journal_draft`)으로 자동 전송**되는 화면과 파이프라인을 만든다.

지금은 매입 데이터를 넣을 **테이블만 있고(0건) API·화면이 전혀 없다**. 그 결과 매입은
회계원장 바깥에 남아 원가율·부가세 신고 근거가 비어 있다.

핵심 설계 결정 3가지:

| # | 결정 | 근거 |
|---|---|---|
| D1 | 오비서 DB를 AADS 본체 DB에서 **분리**한다 | CEO 지시 + 장애 전파·스키마 간섭 차단 |
| D2 | **회원별/사업자별 DB 분리는 하지 않는다.** 단일 DB + RLS 로 논리 격리한다 | ACCT 가 이미 같은 방식으로 20개 테이블 FORCE RLS 운영 중 [실측] |
| D3 | 매입→회계 연동은 직접 INSERT 가 아니라 **outbox_event → journal_draft** 비동기 경로 | 두 DB가 분리되므로 분산 트랜잭션 불가. ACCT 에 outbox 테이블이 이미 존재 [실측] |

---

## 1. 배경과 문제 정의 (전부 실측)

### 1.1 지금 있는 것

| 항목 | 실측값 | 출처 |
|---|---|---|
| `yeoljeong_purchase_orders` 테이블 | **존재, 14컬럼** | information_schema 조회 |
| 매입 데이터 | **0건** (`reltuples = -1`, 한 번도 analyze/insert 안 됨) | pg_class 조회 |
| 매입 API | **0종** | `/docs` 48개 업무 API 중 purchase 없음 |
| 매입 화면 | **없음** | 대시보드 라우트 부재 |
| 배달 매출 | 3,789건 가동 중 | pg_class 조회 |
| 사업자 / 지점 | 4 / 5 | `yeoljeong_businesses`, `yeoljeong_branches` |

### 1.2 그래서 생기는 손해

1. **원가율을 못 낸다.** 매출(배달 3,789건)은 쌓이는데 매입이 0건이라 매출원가 계산 불가.
2. **부가세 매입세액 근거가 없다.** 세금계산서·계산서 수취분이 시스템 밖(카톡·종이)에 있다.
3. **회계원장이 반쪽이다.** ACCT 에 `journal_draft`/`journal_entry` 구조는 다 있는데
   매입 쪽 유입 경로가 없어 원장이 매출·통장 거래만으로 채워진다.

### 1.3 기존 테이블의 부족분

현 14컬럼: `id, business_id, branch_id, order_date, supplier, supplier_type, status,
total_amount, items(jsonb), received_at, invoice_number, memo, created_at, updated_at`

부가세·회계 연동에 필요한 필드가 **전부 없다**:

| 없는 것 | 왜 필요한가 |
|---|---|
| 공급가액 / 부가세액 분리 | `total_amount` 하나로는 매입세액공제액을 못 뽑는다 |
| 과세구분(과세/면세/영세) | 면세 농축수산물은 의제매입세액 대상 — 분개가 다르다 |
| 증빙유형(세금계산서/계산서/카드/현금영수증/간이) | 공제 가능 여부가 갈린다 |
| 결제수단·결제조건(외상/즉시) | 대변 계정이 외상매입금 ↔ 현금/보통예금으로 갈린다 |
| 회계 전송 상태·연결 ID | 중복 전송 방지와 추적 |

---

## 2. 목표 / 비목표

### 목표 (M6 범위)
- G1. 매입 전표 **등록·수정·삭제·조회** (웹 + 모바일)
- G2. 품목 단위 입력 → **공급가/부가세 자동 계산**
- G3. 증빙(사진·PDF) 첨부와 미리보기
- G4. 승인 시 **ACCT 분개 초안 자동 생성**, 화면에서 차변/대변 미리보기
- G5. 월별 매입 집계 + 원가율(매입/매출) 표시
- G6. 마감월 전표 수정 차단 (`period_lock` 연동)

### 비목표 (M6 에서 하지 않음)
- 세금계산서 국세청 자동 수취(스크래핑) — M7 이후
- 재고 수불(`yeoljeong_stock_movements`) 자동 연동 — M7
- 발주(PO) → 입고(GR) 2단계 워크플로 — 지금은 **입고 실적 1단계**만
- OCR 자동 인식 — ACCT `atom_ocr` 재사용 검토는 M7

---

## 3. 사용자와 시나리오

| 사용자 | 빈도 | 주 사용 화면 | 기기 |
|---|---|---|---|
| 점장 | 매일 1~5회 | 매입 등록 | **모바일 (주)** |
| 사장 | 주 1~2회 | 매입 목록·집계 | 모바일/PC |
| 경리·세무 담당 | 월 1~2회 | 회계 전송 확인 | **PC (주)** |

### 첫 실행 경로 (점장, 모바일)
1. 앱 진입 → 하단 탭 **[매입]**
2. 우하단 **+ 매입 등록** (한 손 엄지 도달 범위)
3. 거래처 선택(최근 사용 순) → 금액 입력 → 증빙 사진 촬영 → 저장
4. 목표: **30초 이내 1건 등록**

### 반복 사용 경로
- 목록 첫 화면이 **이번 달 / 내 지점**으로 기본 필터. 설정 없이 바로 업무.
- 같은 거래처 재등록은 **[복사 등록]** 한 번으로 품목까지 프리필.

### 실패 복구 경로
| 실패 | 화면 처리 |
|---|---|
| 세션 만료 | 입력값을 로컬 보관 → 재로그인 후 **작성 중이던 전표로 복귀** |
| 네트워크 끊김 | "임시저장됨" 배지 + 상단에 [다시 전송] 버튼 |
| 회계 전송 실패 | 전표 상세에 실패 사유 + [재전송] 버튼 (막다른 화면 금지) |
| 마감월 수정 시도 | 차단 사유 + "마감 해제 요청" 경로 안내 |

---

## 4. 【핵심 결정】 DB 격리 설계 (ADR)

> CEO 질문: "DB는 분리하자. 그리고 SaaS 기준 **회원별·사업자별 DB를 별도 격리분리해야 하나?**"

### 4.1 답: 서비스 단위로는 분리한다. 회원·사업자 단위로는 분리하지 않는다.

### 4.2 세 가지 안 비교

| 안 | 구조 | 격리 강도 | 운영 비용 | 판정 |
|---|---|---|---|---|
| A. 사업자별 DB | 사업자 1곳 = DB 1개 | 최상 | 마이그레이션·백업·커넥션풀이 **사업자 수만큼 배수** | ❌ 비권장 |
| B. 사업자별 스키마 | DB 1개, 스키마 N개 | 상 | 스키마 N개에 DDL 반복. 커넥션 search_path 관리 | 대안 |
| C. **단일 DB + RLS** | DB 1개, 테이블에 `tenant_id` + FORCE RLS | 중상 | DDL 1회, 백업 1벌 | ✅ **권장** |

### 4.3 C안을 권하는 근거 — 이미 우리 시스템에서 돌고 있다

ACCT(jinah244 `acct` DB) 실측: **전체 37 테이블 중 20개가 `FORCE ROW LEVEL SECURITY`**.

```
audit_event, canonical_document, canonical_transaction, classification_feedback,
classification_result, classification_rule, classification_rule_version,
idempotency_key, import_job, journal_approval, journal_draft, journal_draft_line,
journal_entry, journal_line, master_data_issue, membership, outbox_event,
period_lock, raw_document, source_connection, tenant_company
```

정책은 GUC `acct.tenant_id` 를 읽는 `acct_current_tenant()` 가 강제하고,
**GUC 미설정 시 EXCEPTION** 을 던진다 — 스코프를 빼먹으면 조회가 실패하지, 남의 데이터가
새지 않는다. 오늘(2026-09-17) `tenant_id=9` 스코프로 조회했을 때 `tenant_company` 가
자기 1건만 반환되고 7·8·10 은 NULL 로 가려지는 것을 실측으로 확인했다.

즉 **테넌트 격리 문제는 이미 검증된 해법이 사내에 있다.** 매입 화면만 다른 방식을 쓸 이유가 없다.

### 4.4 A안(사업자별 DB)을 택하지 않는 이유

1. **규모가 안 맞는다.** 현재 사업자 4곳. SaaS 로 100곳만 가도 DB 100개 —
   마이그레이션 1건에 100회 적용, 실패 시 부분 적용 상태가 생긴다.
2. **커넥션 풀이 터진다.** PostgreSQL 커넥션은 DB 단위로 잡힌다. 사업자 N곳 × 앱 인스턴스 M개.
3. **교차 집계가 불가능해진다.** 사장이 여러 사업자를 소유하는 것이 이미 현실
   (열정국밥 3개점 + 언니냉면). 사업자별 DB면 "전체 원가율"을 앱에서 N번 조회해 합산해야 한다.
4. **RLS 로 얻는 격리로 충분하다.** 유출 경로는 대부분 애플리케이션 버그인데,
   RLS 는 DB 레벨에서 그것까지 막는다. DB 분리는 그보다 비싸고 더 낫지도 않다.

### 4.5 그럼 무엇을 분리하는가 — **서비스 단위 분리** (CEO 지시 채택)

현재 문제: 오비서 28개 테이블이 **AADS 본체와 같은 `aads` DB**에 들어 있다.

```
yeoljeong_* 27개 + saas_users  →  전부 contabo116 aads-postgres / aads
```

이건 실제로 위험하다:
- AADS 본체 마이그레이션·장애가 **오비서 서비스로 그대로 전파**된다.
- 테이블 이름이 `yeoljeong_` 접두사로만 구분돼 실수로 섞일 여지가 있다.
- 백업/복구 단위가 붙어 있어 "오비서만 되돌리기"가 불가능하다.

**권장: 같은 PostgreSQL 인스턴스 안에 별도 database `yeoljeong` 를 만든다.**

| 안 | 내용 | 장점 | 리스크 | 판정 |
|---|---|---|---|---|
| S1 | contabo116 **같은 인스턴스, 별도 database** | 장애·스키마 격리 확보. 이전 비용 최소 | 인스턴스 자원은 여전히 공유 | ✅ **권장** |
| S2 | contabo116 **별도 컨테이너/인스턴스** | 자원까지 격리 | 메모리 추가, 운영 대상 +1 | 대안(트래픽 증가 시) |
| S3 | jinah244 로 이전해 ACCT 와 합침 | 회계 연동이 같은 DB 트랜잭션 | 진아 서버 단일 장애점. 오비서 트래픽이 원장에 영향 | ❌ 비권장 |

S3 를 비권장하는 이유가 중요하다 — 매입 화면은 **오비서 쪽 업무 시스템**이고,
회계원장은 **기록의 정본**이다. 둘을 같은 DB 에 넣으면 업무 시스템의 쓰기 부하와 버그가
원장 무결성에 직접 닿는다. 분리하고 outbox 로 잇는 편이 맞다(D3).

### 4.6 최종 구조

```
contabo116                                   jinah244
┌─────────────────────────┐                 ┌──────────────────────────┐
│ aads-postgres           │                 │ acct DB                  │
│  ├ aads      (AADS 본체) │                 │  tenant / company        │
│  └ yeoljeong (오비서)  │  outbox 소비    │  journal_draft ←─────────┤
│     테이블에 tenant_id   │ ──────────────> │  journal_entry           │
│     + FORCE RLS          │   (SSH 터널)    │  period_lock             │
└─────────────────────────┘                 │  20개 테이블 FORCE RLS   │
                                            └──────────────────────────┘
       서비스 단위: 분리                          테넌트 단위: RLS 격리
```

### 4.7 예외 경로 (미래 대비)

대형 프랜차이즈가 "우리 데이터는 물리 분리" 를 계약 조건으로 요구할 수 있다.
그때를 위해 **격리 티어** 개념만 스키마에 남긴다 — `tenant.isolation_tier`
(`shared` | `dedicated`). 기본 전원 `shared`, 요구가 실제로 오면 그 테넌트만 S2 로 뺀다.
**지금 구현하지 않는다.** 필드 하나만 예약한다.

---

## 5. 데이터 모델

### 5.1 `purchase_order` (기존 14컬럼 + 11컬럼 추가)

```sql
-- 신규 DB yeoljeong 로 이전 후 적용
ALTER TABLE purchase_order
  ADD COLUMN tenant_id        bigint NOT NULL,   -- RLS 키 (ACCT tenant.id 와 동일 축)
  ADD COLUMN supply_amount    numeric(14,0),     -- 공급가액
  ADD COLUMN vat_amount       numeric(14,0),     -- 부가세액
  ADD COLUMN tax_type         text,              -- taxable | exempt | zero_rated
  ADD COLUMN evidence_type    text,              -- tax_invoice | invoice | card | cash_receipt | none
  ADD COLUMN payment_method   text,              -- credit(외상) | cash | card | transfer
  ADD COLUMN due_date         date,              -- 외상 지급 예정일
  ADD COLUMN evidence_files   jsonb DEFAULT '[]'::jsonb,
  ADD COLUMN acct_draft_id    bigint,            -- ACCT journal_draft.id (전송 성공 시)
  ADD COLUMN acct_sync_status text DEFAULT 'none', -- none|queued|sent|failed
  ADD COLUMN acct_sync_error  text;

ALTER TABLE purchase_order ENABLE ROW LEVEL SECURITY;
ALTER TABLE purchase_order FORCE ROW LEVEL SECURITY;
CREATE POLICY po_tenant_isolation ON purchase_order
  USING (tenant_id = yj_current_tenant());   -- ACCT acct_current_tenant() 와 같은 패턴

CREATE INDEX ix_po_tenant_date ON purchase_order (tenant_id, order_date DESC);
CREATE INDEX ix_po_sync        ON purchase_order (tenant_id, acct_sync_status)
  WHERE acct_sync_status IN ('queued','failed');
CREATE UNIQUE INDEX ux_po_invoice ON purchase_order (tenant_id, invoice_number)
  WHERE invoice_number IS NOT NULL;   -- 같은 세금계산서 중복 등록 차단
```

`items` jsonb 구조 (품목 라인):
```json
[{"name":"돼지고기 앞다리","qty":20,"unit":"kg","unit_price":8500,
  "supply":170000,"vat":17000,"tax_type":"taxable","account_hint":"원재료비"}]
```

> 품목을 별도 테이블로 빼지 않는 이유: 매입 품목은 **전표와 함께만 조회**되고
> 품목 단위 교차 집계는 M7(재고 연동) 전까지 요구가 없다. jsonb 로 두면
> 스키마 변경 없이 항목을 늘릴 수 있다. M7 에서 재고 수불이 붙을 때 정규화한다.

### 5.2 식별자 브리지 (M0 선행 — 이것 없이는 연동 불가)

**오늘 실측으로 매핑이 확정 가능함을 확인했다.** 이름이 1:1 로 붙는다.

| 오비서 `business_id` | 사업자명 | 사업자번호 | ACCT `tenant_id` |
|---|---|---|---|
| `biz-junghwa` | 열정국밥 중화점 | 710-86-04499 | **7** |
| `biz-mia` | 열정국밥_미아점 | 874-21-02160 | **8** |
| `biz-sungshin` | 열정국밥 성신여대점 | 기초등록 필요 | **9** |
| `biz-eonni-naengmyeon` | 언니냉면 | 기초등록 필요 | **10** |

```sql
CREATE TABLE tenant_business_map (
  tenant_id     bigint NOT NULL,          -- ACCT tenant.id
  business_id   text   NOT NULL,          -- 오비서 business_id
  company_id    bigint,                   -- ACCT company.id (없으면 생성 필요)
  registration_no text,
  verified_at   timestamptz,
  PRIMARY KEY (tenant_id, business_id)
);
```

⚠️ **주의 2건**
1. `company` 는 tenant 9 만 존재(`company_id=9`). 7·8·10 은 **company 생성이 선행**돼야
   분개를 붙일 수 있다. (RLS 로 가려진 것인지 실제 부재인지는 tenant별 재조회로 확정할 것 — [미검증])
2. 사업자번호가 2곳 "기초등록 필요". 매핑 검증 키로 쓰려면 먼저 채워야 한다.

### 5.3 계정과목

⚠️ **ACCT 에 계정과목 마스터 테이블이 없다** — `journal_line.account_code` 가 자유 text.
아래 코드는 **제안이며 회계 담당 확정이 필요하다 [미확정]**.

| 상황 | 차변 | 대변 |
|---|---|---|
| 과세 식자재, 외상 | 원재료비 / 부가세대급금 | 외상매입금 |
| 과세 식자재, 즉시결제 | 원재료비 / 부가세대급금 | 보통예금 or 현금 |
| 면세 농축수산물 | 원재료비 (전액) | 외상매입금 |
| 소모품·비품 | 소모품비 / 부가세대급금 | 외상매입금 |

---

## 6. API 명세 (7종)

기준: `/api/v1/purchases`, 인증 = 기존 SaaS JWT, 테넌트 = 토큰에서 도출(클라이언트 지정 금지)

| # | Method | Path | 설명 | 권한 |
|---|---|---|---|---|
| 1 | GET | `/purchases` | 목록 (기간·지점·거래처·상태·증빙유형 필터, 페이지네이션) | 점장+ |
| 2 | GET | `/purchases/{id}` | 상세 (품목·증빙·분개 미리보기 포함) | 점장+ |
| 3 | POST | `/purchases` | 등록 (`Idempotency-Key` 필수) | 점장+ |
| 4 | PATCH | `/purchases/{id}` | 수정 — **마감월이면 423 Locked** | 점장+ |
| 5 | DELETE | `/purchases/{id}` | 소프트 삭제 — 전송 완료분은 409 | 사장 |
| 6 | POST | `/purchases/{id}/submit` | 승인 → outbox 적재 → 회계 전송 | 사장/경리 |
| 7 | GET | `/purchases/summary` | 월별 집계 + 원가율 | 점장+ |

부가 1종: `POST /purchases/{id}/retry-sync` — 전송 실패 재시도.

### 6.1 등록 요청 예
```json
POST /api/v1/purchases
Idempotency-Key: <클라이언트가 만드는 UUID — 재전송 시 같은 값>
{
  "business_id": "biz-junghwa", "branch_id": "br-junghwa-1",
  "order_date": "2026-09-17", "supplier": "대성축산",
  "evidence_type": "tax_invoice", "invoice_number": "20260917-00412",
  "tax_type": "taxable", "payment_method": "credit", "due_date": "2026-10-10",
  "items": [{"name":"돼지고기 앞다리","qty":20,"unit":"kg","unit_price":8500}]
}
```
서버가 `supply_amount=170000`, `vat_amount=17000`, `total_amount=187000` 를 계산한다.
**클라이언트 계산값을 신뢰하지 않는다** — 받더라도 재계산해 불일치 시 400.

### 6.2 오류 계약
| 코드 | 상황 | 화면 처리 |
|---|---|---|
| 400 | 금액 불일치 / 필수값 누락 | 해당 필드에 인라인 오류 |
| 409 | 같은 세금계산서번호 중복 | "이미 등록된 세금계산서입니다" + 기존 전표 링크 |
| 423 | 마감월 전표 수정 | 차단 배너 + 마감 해제 요청 경로 |
| 502 | ACCT 전송 실패 | 전표는 유지, 상태 `failed` + [재전송] |

---

## 7. 화면 설계 (3화면)

목업: https://fb.newtalk.kr/static/preview/acct-purchase-mockup.html

### 화면 1 — 매입 목록 (기본 진입)
- 상단: 이번 달 매입 합계 / 부가세 / 원가율 3장 요약 타일
- 필터: 기간(이번달 기본) · 지점 · 거래처 · 상태 · 증빙유형
- 리스트: 일자 / 거래처 / 증빙 배지 / 금액 / 회계상태 배지
- 모바일: 표 대신 **카드 리스트**, 우하단 FAB `+`
- 회계 전송 실패 건은 **상단에 고정 배너**로 올린다 (묻히면 영영 안 본다)

### 화면 2 — 매입 등록 / 상세
- 좌: 기본정보(일자·지점·거래처·증빙유형·세금계산서번호·결제조건)
- 우: 증빙 미리보기 (사진/PDF, 드래그 업로드, 모바일은 카메라 즉시 촬영)
- 하: 품목 라인 테이블 — 수량×단가 입력 시 공급가/부가세 **실시간 자동 계산**
- 하단 고정 바: 공급가 / 부가세 / 합계 + [임시저장] [등록]
- 면세 선택 시 부가세 칸이 비활성화되고 "의제매입세액 대상" 안내가 뜬다

### 화면 3 — 회계 전송 확인
- 좌: 전표 요약
- 우: **분개 미리보기** — 차변/대변 T 계정, 차·대 합계 일치 표시
- 계정과목은 룰 자동 배정 + **수정 가능**(드롭다운), 수정 시 해당 거래처 다음 등록에 학습 반영
- 마감월이면 전송 버튼 비활성 + 사유 표시
- 전송 후: `journal_draft` 링크와 전송 시각 표시

### 7.1 용어 원칙 (L1 준수)
화면에는 업무 용어만 쓴다 — `tenant_id`, `business_id`, `journal_draft` 같은 내부 식별자는
API·로그에만 둔다. 화면에는 "사업장", "매장", "회계 전송" 으로 표시한다.

---

## 8. ACCT 연동 파이프라인

DB 가 분리되므로 **한 트랜잭션에 묶을 수 없다.** outbox 패턴을 쓴다.
ACCT 에 `outbox_event(topic, payload, dedupe_key, status, attempt, next_retry_at, last_error)`
가 **이미 있고 FORCE RLS 가 걸려 있다** [실측] — 그대로 재사용한다.

```
[1] 사용자 승인
     └ yeoljeong DB 트랜잭션:
         purchase_order.status='approved', acct_sync_status='queued'
         + yj_outbox(topic='purchase.approved', dedupe_key=po:{tenant}:{id}:{rev})
[2] 워커(60초 주기)가 yj_outbox 소비
     └ SSH 터널로 ACCT 접속, SET LOCAL acct.tenant_id
     └ journal_draft INSERT
          draft_key    = 'po:{tenant_id}:{po_id}'      ← 멱등 키
          external_ref = 'yeoljeong:purchase:{po_id}'  ← 역추적
          entry_date   = order_date
        + journal_draft_line 2~3행 (차변/대변)
[3] 성공 → purchase_order.acct_draft_id, acct_sync_status='sent'
    실패 → 'failed' + last_error, next_retry_at 지수백오프 (최대 5회)
[4] 경리가 ACCT 에서 journal_draft 를 검토·확정 → journal_entry 전기
```

**멱등성**: `journal_draft.draft_key` 에 유니크를 걸어 워커가 두 번 돌아도 초안이 하나만 생긴다.
ACCT 에 `idempotency_key` 테이블도 이미 있으므로 API 레벨 멱등도 같이 건다.

**마감 연동**: 전송 전 `period_lock(tenant_id, period_key)` 를 조회한다.
잠긴 월이면 전송하지 않고 `failed` + "마감된 월" 사유를 남긴다.
(진행 중인 M1 마감잠금 작업과 직접 연결 — M1 완료가 선행되는 것이 안전하다.)

---

## 9. 권한

| 역할 | 조회 | 등록/수정 | 승인·전송 | 삭제 |
|---|---|---|---|---|
| 직원 | 자기 지점 | ✕ | ✕ | ✕ |
| 점장 | 자기 지점 | ○ | ✕ | 본인 작성 미승인분만 |
| 사장 | 전 사업장 | ○ | ○ | ○ |
| 경리·세무 | 전 사업장 | ✕ | ○ | ✕ |

RLS 가 테넌트 경계를, 애플리케이션 권한이 역할 경계를 담당한다. **두 겹으로 둔다.**

---

## 10. 단계별 구현 계획

| Phase | 내용 | Size | 선행 | 검증 기준 |
|---|---|---|---|---|
| **M0** | 식별자 브리지 — `tenant_business_map` 생성·시드(4건), company 부재분 생성 | S | — | 4행 verified_at NOT NULL |
| **P0** | DB 분리 — `yeoljeong` database 생성, 28테이블 이전, 앱 DSN 전환 | M | CEO 승인 | 오비서 API 48종 전부 200, 데이터 건수 이전 전후 일치 |
| **P1** | 스키마 확장 + RLS + API 7종 | M | P0 | 단위테스트 통과, RLS 교차접근 차단 테스트 |
| **P2** | 화면 3종 (목록·등록·전송확인) | M | P1 | 화면 캡처 3장, 모바일 폭 375px 검증 |
| **P3** | outbox 워커 + 분개 룰 + 마감 연동 | M | P1, M1 마감잠금 | 전표 1건 → `journal_draft` 생성 E2E, 중복 실행 시 1건 유지 |
| **P4** | 집계·원가율 + 실패 재시도 UX | S | P2, P3 | 원가율 = 매입/매출 수치 대조 |

**권장 착수 순서: M0 → P0 → P1.** M0 를 먼저 하는 이유는 P3 에서 막히면
그때는 이미 화면까지 다 만든 뒤라 되돌리는 비용이 크기 때문이다.

---

## 11. 검증 기준 (완료 판정)

| # | 항목 | 판정 방법 |
|---|---|---|
| V1 | 매입 전표 등록 | `POST /purchases` 200 + DB row |
| V2 | 부가세 자동계산 | 과세/면세/영세 3케이스 단위테스트 |
| V3 | 테넌트 격리 | tenant A 토큰으로 tenant B 전표 조회 → **0건** |
| V4 | 중복 방지 | 같은 `Idempotency-Key` 2회 → row 1건 |
| V5 | 회계 전송 | `journal_draft` 생성 + 차대 합계 일치 |
| V6 | 워커 멱등 | 워커 2회 강제 실행 → draft 1건 유지 |
| V7 | 마감 차단 | 마감월 수정 → 423 |
| V8 | 모바일 | 375px 폭 캡처, 터치 타깃 ≥ 44px |
| V9 | DB 분리 무손실 | 이전 전후 28테이블 count 전부 일치 |

---

## 12. 리스크

| # | 리스크 | 영향 | 완화 |
|---|---|---|---|
| R1 | DB 분리 중 오비서 중단 | 서비스 정지 | 논리복제로 선복사 → 짧은 전환창만 사용. 롤백은 DSN 원복 |
| R2 | company 미생성(7·8·10) | 분개 전송 전부 실패 | M0 에서 선확인·생성 |
| R3 | 계정과목 마스터 부재 | 분개 코드 임의값 | 회계 담당 확정 필요 — **P3 착수 전 블로킹 항목** |
| R4 | 사업자번호 2곳 미등록 | 매핑 검증 키 부재 | M0 에서 등록 |
| R5 | SSH 터널 단절 | 전송 지연 | outbox 재시도 5회 + 실패 배너. 전표 자체는 손실 없음 |
| R6 | M1 마감잠금 미완 | 마감월 전표가 새어 들어감 | P3 를 M1 이후로 배치 |

---

## 13. 미확정 / CEO 결정 필요

1. **DB 분리 방식** — S1(같은 인스턴스 별도 DB, 권장) vs S2(별도 인스턴스)
2. **계정과목 체계** — 표준 계정과목 코드 확정 (R3)
3. **발주 단계 도입 여부** — 지금은 입고 1단계만. 발주 승인 흐름이 필요한지

---

## 부록 A. 실측 출처

| 사실 | 출처 |
|---|---|
| `yeoljeong_purchase_orders` 14컬럼 | information_schema.columns |
| 오비서 28테이블 / 배달매출 3,789 | pg_class |
| 사업자 4 / 지점 5 / 매핑 후보 | `yeoljeong_businesses` |
| ACCT 37테이블 중 20개 FORCE RLS | pg_class (acct DB) |
| `journal_draft`·`outbox_event`·`period_lock` 컬럼 | information_schema (acct DB) |
| tenant 7/8/9/10, company_id=9 | tenant ⋈ tenant_company ⋈ company |
| `/static` 공개 200 | curl |
