# ACCT-LAYOUT-004 — 멀티 사업자 관리 PRD

- 작성: 2026-09-18
- 지시: CEO — "사용자가 여러 사업자를 관리할 때 각각 로그인해서 관리하는 건 불편하다. SaaS 사업자 추가도 고려해 기획에 반영하라"
- 목업(아티팩트 열람 가능): https://aads.newtalk.kr/reports/20260918_ACCT-LAYOUT-004_멀티사업자_관리.html
- 연관: ACCT-LAYOUT-001(매입자료), ACCT-LAYOUT-003(회원가입)

## 1. 배경 — 실측

| 항목 | 실측값 | 출처 |
|---|---|---|
| SaaS 사용자 | 88명 | AADS DB `saas_users` |
| 테넌트 | 88개 | AADS DB `tenants` |
| 멤버십 | 123행(active 84) | AADS DB `tenant_memberships` |
| **2개 이상 사업자 소속 사용자** | **35명** | `GROUP BY user_id HAVING COUNT(DISTINCT tenant_id)>1` |
| ACCT 사업자 마스터 | `company` 4건, `tenant_company` 연결 존재 | ACCT DB |
| ACCT 사업자 단위 권한 | `membership` 스키마 존재, **데이터 0건** | ACCT DB(tenant 9 스코프) |

이미 배포된 인증 API 실호출 결과(2026-09-18):

| 엔드포인트 | 결과 |
|---|---|
| `POST /api/v1/auth/register` | 200 — tenant 자동 생성 + owner membership |
| `POST /api/v1/auth/tenants` | 201 — 두 번째 사업자 생성 + 새 토큰 반환 |
| `GET /api/v1/auth/tenants` | 200 — `{current_tenant_id, tenants[]}` (role, membership_status 포함) |
| `POST /api/v1/auth/tenants/{id}/switch` | 200 — `{tenant, membership, token}` **새 JWT 재발급** |

**결론: 재로그인은 기술적으로 이미 불필요하다.** 없는 것은 이 API를 쓰는 화면과, 사업자(company) 축의 권한·스코프다.

## 2. 문제 정의

1. 화면이 "사용자 1명 = 사업자 1개"를 전제로 만들어져 전환 수단이 없다 → 로그아웃/재로그인이 유일한 경로.
2. 사업자별 할 일(미처리 전표·부족 데이터)을 한눈에 볼 방법이 없다 → 사업자 수만큼 반복 점검.
3. 사업자 단위 권한(`membership.company_id`)이 데이터로 존재하지 않아, 외부 회계사에게 특정 사업자만 열어줄 수 없다.
4. SaaS tenant와 ACCT company의 매핑 규칙이 없어, 가입으로 만들어진 tenant가 회계 장부와 연결되지 않는다.

## 3. 설계

### 3.1 2층 모델

- **tenant** — 계약·구독·결제·초대 단위(요금제, 사용량).
- **company** — 사업자등록번호 1개 = 장부 1벌(마감·시산표·부가세).
- **membership(user, tenant, company, role)** — `company_id IS NULL` 이면 tenant 전체 범위, 값이 있으면 해당 사업자만.

한 tenant 안에 여러 company 를 둔다(권장). tenant 를 사업자마다 쪼개면 통합 손익 합산이 불가능해진다.

### 3.2 사업자별 DB 분리 여부

분리하지 않는다. 단일 DB + `FORCE ROW LEVEL SECURITY` 유지(ACCT 37개 테이블 중 20개가 이미 FORCE RLS).
DB/스키마 분리는 마이그레이션·백업·커넥션풀이 사업자 수만큼 배수로 늘고, 통합 조회가 애플리케이션 조인으로 밀려난다.

### 3.3 스코프 전달

```
JWT.tenant_id + X-Company-Id 헤더
  → FastAPI 의존성에서 membership 권한 검증(없으면 403)
  → set_config('acct.tenant_id', …), set_config('acct.company_id', …)
  → FORCE RLS 가 DB 단에서 재차 차단
```

### 3.4 쓰기 제한 (오귀속 방지)

- 조회·요약: 전 사업자 합산(`scope=all`) 허용.
- 쓰기(전표·마감·급여 확정): **현재 단일 사업자 스코프에서만** 허용. 통합 화면에는 쓰기 버튼을 노출하지 않는다.
- 근거: ACCT `source_file` 17,776건이 귀속 근거 없이 남아 있는 상태(2026-09-17 확인). 쓰기 경로의 사업자 확정은 타협 대상이 아니다.

## 4. 화면

| # | 화면 | 핵심 |
|---|---|---|
| 1 | 사업자 스위처(전 화면 고정) | 최근 사용순, 검색(⌘K), 미처리 배지, 전환 후 같은 메뉴 유지 |
| 2 | 통합 대시보드 | 합산 매출·매입·미처리 + 사업자 카드(클릭 시 전환). 쓰기 버튼 없음 |
| 3 | 사업자 추가 마법사 | 사업자번호 → 진위확인 → company/tenant_company/membership 생성 → 수집 연동(선택) → 자동 전환 |
| 4 | 권한·멤버 관리 | 역할 × 사업자 범위 지정, 초대·해지 |

권한 매트릭스: owner(tenant 전체/결제 포함), admin(tenant 전체), accountant(지정 사업자/마감 가능), staff(지정 사업자/당월 입력), viewer(조회만).

## 5. API

기존(구현·검증 완료): `GET /auth/tenants`, `POST /auth/tenants`, `POST /auth/tenants/{id}/switch`, `POST /auth/tenants/{id}/invites`, `GET /auth/tenants/{id}/members`.

신규:

| 엔드포인트 | 용도 |
|---|---|
| `GET /api/v1/acct/companies` | 현재 tenant 의 사업자 목록 + 배지 수치 |
| `POST /api/v1/acct/companies` | 사업자 추가(company + tenant_company + membership 트랜잭션) |
| `GET /api/v1/acct/overview?scope=all\|company` | 통합/단일 대시보드 집계 |
| 공통 헤더 `X-Company-Id` | company 스코프 전달 |

플랜 한도 초과는 500 이 아니라 **402 + 업그레이드 안내**로 응답한다.

## 6. 단계 계획

| 단계 | 범위 | 완료기준 | 규모 |
|---|---|---|---|
| P1 | 스위처 UI + `GET /auth/tenants` 연동 + 전환 후 화면 유지 | 재로그인 없이 전환, 메뉴 컨텍스트 보존 | S |
| P2 | company 축 도입 — `X-Company-Id`, membership 시딩(현재 0건) | 회계사 계정이 지정 사업자만 조회, 타 사업자 403 | M |
| P3 | 통합 대시보드 `scope=all` + 배지 | 합산값 = 사업자별 합, 쓰기 버튼 미노출 | M |
| P4 | 사업자 추가 마법사 + 플랜 한도(402) | 사업자번호 입력→장부 생성→자동 전환 1화면 | S |

## 7. CEO 결정 필요

| # | 결정 | 권장 | 근거 |
|---|---|---|---|
| 1 | tenant 당 사업자 다수 vs 사업자마다 tenant | **tenant 1개 안에 company N개**, 과금은 사업자 수 기준 | 다점포·프랜차이즈의 목적이 통합 손익이다 |
| 2 | free 플랜 사업자 한도 | **1개**(2개째부터 유료) | 사업자 수 = 장부 수 = 수집·저장 비용 |

## 8. 미결·리스크

- ACCT `membership` 0건 — P2 에서 시딩하지 않으면 사업자 단위 권한은 계속 명목상으로만 존재한다.
- SaaS tenant ↔ ACCT company 매핑 컬럼이 없다. P2 에서 `tenant_company` 를 정본으로 삼고 SaaS tenant 에 ACCT tenant id 를 보존한다.
- 언니냉면 사업자번호 미등록 — 사업자 추가 마법사의 진위확인 단계에서 걸린다(현재 company 4건 중 1건 biz_no 미상).
