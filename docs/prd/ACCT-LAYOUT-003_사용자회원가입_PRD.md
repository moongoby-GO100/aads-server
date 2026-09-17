# ACCT-LAYOUT-003 — 사용자(사업자) 회원가입 PRD

- 작성: 2026-09-18 KST
- 범위: 통합 경영관리시스템(ACCT 회계원장 + 매장비서) SaaS **신규 사업자 가입**
- 목업: `/static/preview/signup-flow-mockup.html`
- 선행 문서: `docs/SAAS_USER_ACCESS_AND_BRIEFING_POLICY.md`(접근·브리핑 정책)

---

## 1. 왜 쓰는가 — 지금 없는 것

대표님 지적대로 **가입 기획 문서가 없습니다.** 저장소 전체(`docs/prd/` 19건 포함)를
검색한 결과 회원가입을 다룬 문서는 `SAAS_USER_ACCESS_AND_BRIEFING_POLICY.md` 하나이고,
그 문서는 "가입 후 어떤 화면을 보여줄지"(테넌트 격리·브리핑 범위)만 정합니다.
**어떻게 가입시킬지**는 코드에만 있고 설계 근거가 없습니다.

구현된 것은 사실상 **직원 초대(팀 멤버 추가)** 쪽입니다 — `tenant_invites` 테이블과
초대·수락 API가 있습니다. 정작 **사업자가 스스로 들어오는 경로**는 이메일/비밀번호/이름
3개만 받는 최소 폼이고, 사업자등록번호도 받지 않습니다.

### 실측 현황 [DB 조회, 2026-09-18]

| 항목 | 값 | 판정 |
|---|---:|---|
| 가입 사용자(`saas_users`) | 88명 | — |
| 이메일 인증 완료(`email_verified_at`) | **3명 (3.4%)** | ⚠️ 컬럼만 있고 인증 강제 없음 |
| 마지막 로그인 기록(`last_login_at`) | **0건** | ❌ 갱신 코드 없음 → 휴면 판별 불가 |
| 테넌트(`tenants`) | 88 (customer 87) | 가입 1건당 1테넌트 자동 생성 |
| 직원 초대(`tenant_invites`) | **0건** | ⚠️ API는 있으나 실사용 0 |
| 사업자(`yeoljeong_businesses`) | 4 | 가입 흐름과 **연결 없음**(수기 등록) |

---

## 2. AS-IS 가입 흐름 (코드 실측: `app/api/auth.py:140`)

```
POST /api/v1/auth/register  { email, password, name, organization_name?, team_invites[] }
   → saas_users INSERT (plan=free, is_active=true)
   → create_tenant_for_user(kind=customer, plan=free)   # 테넌트 자동 생성
   → team_invites 있으면 tenant_invites INSERT
   → JWT 발급, onboarding_required = organization_name 없음
```

즉 **이메일 인증 없이 즉시 로그인 가능**하고, 사업자 정보는 한 글자도 받지 않습니다.

### 빠져 있는 것 (P0 순)

| # | 결함 | 사용자/사업 영향 |
|---|---|---|
| G-1 | 사업자 정보 미수집 | 회계원장(ACCT `company.biz_no`)과 매장비서(`yeoljeong_businesses`)에 연결할 키가 없어, 가입해도 **자기 회계 데이터를 볼 수 없음** |
| G-2 | 이메일 인증 미강제 | 오타·타인 메일 가입 방치, 비밀번호 재설정 경로 신뢰 불가 (실측 3/88) |
| G-3 | 약관·개인정보 동의 이력 미저장 | 동의 받은 기록이 DB에 없음 — 분쟁·점검 시 입증 불가 |
| G-4 | 비밀번호 재설정 없음 | 잊으면 복구 경로가 없음(운영자 수기 개입) |
| G-5 | `last_login_at` 미갱신 | 휴면 계정 분리·파기 정책 실행 불가 |
| G-6 | 플랜 선택·결제 연결 없음 | 전원 free 고정, 유료 전환 경로 부재 |
| G-7 | 탈퇴 흐름 없음 | `deleted_at` 컬럼만 존재 |

---

## 3. TO-BE — 가입은 3단계, 사업자 확인은 그 다음

원칙: **첫 화면에서 요구하는 입력을 최소화**하고, 사업자 정보는 가입 직후 온보딩에서
받습니다. 사업자번호를 처음부터 요구하면 이탈합니다. 대신 **사업자 정보 없이는 회계
데이터 화면에 들어갈 수 없게** 막고, 그 이유를 화면에서 설명합니다.

```
[1] 계정 만들기      이메일 · 비밀번호 · 이름 · 약관동의        → saas_users
      ↓
[2] 이메일 인증      6자리 코드 (10분, 재발송 60초 쿨다운)      → email_verified_at
      ↓
[3] 사업자 등록      사업자번호 · 상호 · 대표자 · 과세유형       → businesses + company 매핑
                     · 업태/업종 · 개업일 · 주소
      ↓
[4] (선택) 팀 초대   직원 이메일 + 역할(관리자/입력자/열람자)   → tenant_invites
      ↓
    대시보드 (매입자료 화면 등)
```

3단계를 건너뛴 사용자는 대시보드에 들어오되 **"사업자 등록을 마쳐야 회계 데이터가
연결됩니다"** 배너와 이어하기 버튼을 봅니다(막다른 화면 금지).

### 역할 정의

| 역할 | 가입 경로 | 권한 |
|---|---|---|
| 대표(owner) | 본인 가입(위 4단계) | 사업자 정보·결제·팀 관리 전부 |
| 관리자(admin) | 초대 수락 | 전표 확정·마감, 팀 초대 |
| 입력자(member) | 초대 수락 | 전표 입력·수정, 확정 불가 |
| 열람자(viewer) | 초대 수락 | 조회·내보내기만 |

---

## 4. 데이터 모델 (추가분)

기존 `saas_users` / `tenants` / `tenant_invites` 는 유지하고 3개 테이블을 더합니다.

```sql
-- 약관·개인정보 동의 이력 (G-3)
CREATE TABLE saas_user_consents (
  id            BIGSERIAL PRIMARY KEY,
  user_id       TEXT NOT NULL,
  consent_key   TEXT NOT NULL,     -- terms / privacy / marketing / age14
  version       TEXT NOT NULL,     -- 약관 버전 (문구 변경 추적)
  agreed        BOOLEAN NOT NULL,
  agreed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  ip            INET,
  user_agent    TEXT
);

-- 이메일 인증 (G-2)
CREATE TABLE saas_email_verifications (
  id          BIGSERIAL PRIMARY KEY,
  user_id     TEXT NOT NULL,
  code_hash   TEXT NOT NULL,       -- 평문 저장 금지
  expires_at  TIMESTAMPTZ NOT NULL,
  attempts    INT NOT NULL DEFAULT 0,
  verified_at TIMESTAMPTZ
);

-- 비밀번호 재설정 (G-4)
CREATE TABLE saas_password_resets (
  id          BIGSERIAL PRIMARY KEY,
  user_id     TEXT NOT NULL,
  token_hash  TEXT NOT NULL,
  expires_at  TIMESTAMPTZ NOT NULL,
  used_at     TIMESTAMPTZ
);
```

기존 테이블 변경:

```sql
ALTER TABLE yeoljeong_businesses ADD COLUMN tenant_id UUID;      -- 테넌트 귀속
ALTER TABLE yeoljeong_businesses ADD COLUMN acct_company_id BIGINT; -- ACCT company 매핑
```

**`acct_company_id` 가 이 기획의 핵심입니다.** 이 값이 채워져야 가입한 사업자가
매입자료 화면에서 자기 전표를 봅니다. 값이 없으면 화면은 "연결 대기"로 표시합니다.

---

## 5. API 명세 (신규 8종)

| # | 메서드·경로 | 입력 | 비고 |
|---|---|---|---|
| 1 | `POST /auth/register` (개편) | email, password, name, consents[] | 동의 이력 저장 + 인증코드 발송, **미인증 상태로 생성** |
| 2 | `POST /auth/email/verify` | code | 성공 시 `email_verified_at` 기록 |
| 3 | `POST /auth/email/resend` | — | 60초 쿨다운, 시간당 5회 제한 |
| 4 | `POST /auth/business` | biz_no, name, representative, tax_type, biz_cond, opened_at, address | `yeoljeong_businesses` + ACCT 매핑 시도 |
| 5 | `GET /auth/business/check?biz_no=` | 사업자번호 | 형식검증(체크섬) + 중복 가입 확인 |
| 6 | `POST /auth/password/forgot` | email | 토큰 메일 발송(존재 여부 노출 금지 — 항상 200) |
| 7 | `POST /auth/password/reset` | token, new_password | 사용 즉시 토큰 폐기 |
| 8 | `DELETE /auth/me` | password 재확인 | 탈퇴 — `deleted_at` 기록, 30일 후 파기 |

### 검증 규칙

- 비밀번호: 10자 이상 + 영문·숫자·기호 중 2종 이상. 자주 쓰이는 비밀번호 목록 차단.
- 사업자번호: 10자리 국세청 **체크섬 검증**을 먼저 하고, 외부 진위확인 API 연동은
  별건으로 둡니다(현재 키 미보유 — [미검증]).
- 가입 시도 제한: 동일 IP 시간당 10회, 동일 이메일 5회.
- 이메일 존재 여부를 응답으로 구분하지 않습니다(계정 열거 방지). 단 가입 단계에서는
  중복 이메일을 409로 알려야 사용자가 진행할 수 있으므로 여기만 예외로 둡니다.

---

## 6. 화면 명세 (목업 5화면)

`/static/preview/signup-flow-mockup.html`

| # | 화면 | 핵심 요소 | 실패 복구 |
|---|---|---|---|
| 1 | 계정 만들기 | 이메일·비밀번호(강도 표시)·이름, 약관 4종(필수 3/선택 1) | 중복 이메일 → 로그인 링크 제시 |
| 2 | 이메일 인증 | 6자리 코드, 남은 시간, 재발송 | 메일 미수신 → 주소 변경·재발송 |
| 3 | 사업자 등록 | 사업자번호(자동 하이픈·체크섬), 상호·대표자·과세유형·업태·개업일·주소 | 이미 등록된 사업자 → 관리자에게 초대 요청 |
| 4 | 팀 초대(선택) | 이메일 + 역할 선택, 여러 줄 추가, 건너뛰기 | 나중에 설정에서 가능함을 명시 |
| 5 | 완료·연결 상태 | 회계 데이터 연결 상태, 다음 할 일 3가지 | 연결 실패 → 사유·재시도 |

모바일 기준: 한 손 조작, 터치 영역 44px 이상, 단계 이탈 시 진행 상태 보존.

---

## 7. 개인정보·보안

- 수집 항목: 이메일, 이름, 비밀번호(해시), 사업자 정보, 접속 IP/UA(동의 이력용).
- 보관 기간: 탈퇴 후 30일(분쟁 대비) → 파기. 동의 이력은 법정 보관기간 준수.
- 1년 미접속 계정은 휴면 전환 — **`last_login_at` 갱신(G-5)이 선행 조건**입니다.
- 비밀번호·인증코드·재설정 토큰은 전부 해시 저장. 평문 로그 금지.
- 마케팅 수신은 기본 해제(옵트인).

---

## 8. 단계별 실행 계획

| 단계 | 내용 | 규모 | 완료기준 |
|---|---|---|---|
| S1 | `last_login_at` 갱신 + 동의 이력 테이블·저장 | XS | 로그인 시 값 갱신, 동의 row 생성 |
| S2 | 이메일 인증 강제(발송·검증·재발송) | S | 미인증 계정 회계 화면 차단 |
| S3 | 사업자 등록 단계 + ACCT `company` 매핑 | M | `acct_company_id` 채워진 사업자가 자기 전표 조회 |
| S4 | 비밀번호 재설정·탈퇴 | S | 메일 링크로 재설정 성공 |
| S5 | 팀 초대 메일 실발송 검증 | XS | 초대 수락 1건 이상(현재 0건) |
| S6 | 플랜 선택·결제 연동 | L | 별도 기획 |

---

## 9. 완료 판정

- [ ] 가입 → 인증 → 사업자 등록 → 대시보드 진입 E2E 1회 통과(화면 캡처)
- [ ] 동의 이력 row 생성 확인(DB)
- [ ] 미인증 계정이 회계 데이터 API에서 403 받는지 확인
- [ ] `acct_company_id` 매핑된 계정이 매입자료 화면에서 자기 전표만 보는지 확인
- [ ] 이메일 인증율 목표 90% 이상(현재 3.4%)

## 10. 미검증 항목

- 국세청 사업자 진위확인 API 키 보유 여부 — 미확인. 없으면 체크섬 검증까지만.
- 메일 발송 채널(SMTP/외부 서비스) 실동작 — `tenant_invites` 0건이라 실발송 경로가
  한 번도 검증되지 않았습니다. S2 착수 전에 발송 테스트가 선행돼야 합니다.
