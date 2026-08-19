# OHVIS Agent Vault Account Registration UI Plan

작성 시각: 2026-08-20 05:14 KST  
요청: 전용 계정 등록 UI를 `/browser-tasks`에서 분리하고, Chrome 비밀번호 관리자 및 최신 AI 브라우저/패스워드 매니저/브라우저 에이전트 서비스를 벤치마킹해 구현 기획으로 정리한다.

## 1. 결론

OHVIS의 계정 등록 UI는 `Managed Browser` 작업 화면 안의 보조 폼이 아니라, `/agent-vault` 전용 보안 콘솔로 분리해야 한다. 방향은 "관리자는 계정을 등록하고 정책을 정하며, 에이전트는 원문 비밀번호를 보지 못하고 1회성 autofill 토큰만 받아 로그인한다"이다.

P0 구현 권장안은 다음 5개다.

1. `/agent-vault` 전용 페이지 신설: 계정 등록, 저장 계정 목록, 사용 정책, 최근 접근 로그를 한 화면에 배치한다.
2. 등록 플로우를 Chrome/Apple/1Password식 UX로 개선: 사이트 URL 입력 → origin 자동 정규화 → ID/PW 입력 → 라벨/워크스페이스 선택 → 저장 후 목록 반영.
3. AI 에이전트용 보안 모델 적용: raw password 표시/복사/내보내기는 금지하고, 승인된 작업에만 60초 이하 1회성 autofill 토큰을 발급한다.
4. passkey/MFA/OTP는 저장 대상이 아니라 "사람 takeover/승인 필요" 상태로 분리한다.
5. 감사로그와 권한 게이트를 기본 화면에 노출한다: 누가, 언제, 어떤 work_key/task/origin에서 자격증명을 요청했는지 확인 가능해야 한다.

## 2. 현재 OHVIS 실측 상태

확인 파일:

- Backend API: `app/routers/agent_vault.py`
- Browser task API: `app/api/browser_tasks.py`
- Vault service: `app/services/agent_vault_service.py`
- Permission policy: `app/services/browser_permission_policy.py`
- Migration: `migrations/122_ohvis_managed_browser_agent_vault.sql`
- Dashboard MVP: `/root/aads/aads-dashboard/src/app/browser-tasks/page.tsx`
- API client: `/root/aads/aads-dashboard/src/lib/api.ts`

현재 가능한 것:

| 항목 | 상태 | 근거 |
|---|---:|---|
| 자격증명 저장 API | 구현 | `POST /api/v1/agent-vault/credentials` |
| 자격증명 목록 API | 구현 | `GET /api/v1/agent-vault/credentials` |
| 비활성화 API | 구현 | `DELETE /api/v1/agent-vault/credentials/{credential_id}` |
| 1회성 autofill token | 구현 | `POST /api/v1/agent-vault/autofill-token`, TTL 1~60초 |
| token redeem | 구현 | `POST /api/v1/agent-vault/autofill-redeem` |
| 접근 로그 API | 구현 | `GET /api/v1/agent-vault/access-logs` |
| 권한 분류 | 구현 | `show/reveal/copy/export password`, OTP/MFA 입력은 deny |
| 현재 UI | MVP | `/browser-tasks` 화면에 Agent Vault 폼이 섞여 있음 |

현재 결함:

| 결함 | 영향 | P0 조치 |
|---|---|---|
| 계정 등록 UI가 브라우저 작업 생성 화면 안에 섞임 | CEO/운영자가 "계정은 어디서 등록하나" 혼란 | `/agent-vault` 전용 화면으로 분리 |
| 저장 계정 목록에 `password` 필드가 표시됨 | 값은 마스킹이지만 보안 UI 원칙상 노출 컬럼 자체가 부적절 | 화면에서 비밀번호 컬럼 제거 |
| 등록 폼이 `origin`, `label`, `username`, `password`만 받음 | 서비스명, 소유자, 공유 범위, 사용 정책이 없음 | 등록 Wizard/고급 설정 추가 |
| passkey/MFA/OTP 처리 정책이 UI에 없음 | 자동화 실패 시 원인 파악 어려움 | 인증 상태와 사람 개입 필요 상태 표시 |
| 접근 로그 화면 부재 | 에이전트가 언제 어떤 계정을 썼는지 추적 어려움 | 최근 접근 로그와 필터 추가 |

## 3. 벤치마킹 근거

| 구분 | 서비스/기술 | 확인된 기능 | OHVIS 반영 |
|---|---|---|---|
| 브라우저 기본 비밀번호 관리자 | Google Password Manager / Chrome | Chrome/Android/웹에서 비밀번호와 passkey 저장·조회·동기화, password checkup, import/export 제공 | 저장 위치를 명확히 보여주는 Vault 홈, 보안 점검 상태, CSV import는 P1 |
| 플랫폼 기본 저장소 | Apple Passwords | Passwords 앱에서 비밀번호/passkey/Wi-Fi/인증 정보를 관리하고 AutoFill 및 iCloud Keychain 동기화 제공 | "로컬/디바이스 승인 기반 unlock" UX, OS credential store 연동은 P2 |
| 브라우저 기본 저장소 | Microsoft Password Manager / Edge | Edge 내장 password generator, monitor, health, passkey 저장/동기화, Microsoft 계정 PIN 보호 | 계정 상태 점수, 취약/오래된 계정 표시, PIN/재인증 패턴 |
| 엔터프라이즈 비밀번호 관리자 | 1Password | 브라우저 확장 autofill, 2FA/카드 autofill, Extended Access Management, Device Trust, Credential Broker | 관리자 정책, device trust, work_key별 접근권한, 확장/PC Agent 연결 점검 |
| 오픈소스/기업 패스워드 매니저 | Bitwarden | password/passkey 생성·저장·autofill, 중앙 관리, breach alerts, Secrets Manager는 개발/인프라 secret 별도 제품 | 사람 계정 Vault와 시스템 secret을 분리하고, 조직 정책·감사로그 강화 |
| 기업 credential 보안 | Dashlane | AI-powered autofill, secure sharing, RBAC, SSO 연동 | 공유는 원문 공유가 아니라 권한 공유로 설계, role 기반 폼 노출 |
| PAM/원격접속형 Vault | Keeper | passkey/biometric vault login, zero-trust/zero-knowledge PAM, secrets/connection management | 고권한 계정은 "Privileged" 배지와 추가 승인 요구 |
| AI 브라우저 | Aside Password Manager | 에이전트가 비밀번호를 보지 않고 autofill, site-level access control, audit log, hardware-backed encryption, import 지원 | OHVIS Agent Vault의 직접 모델: hidden secret, scoped autofill, every-use log |
| AI 브라우저 권한 | Aside security docs | Read only/Guard/Full access 모드, password access policy와 target URL 확인 후 payload 생성 | task-level permission mode를 Vault 사용 정책에 연결 |
| 브라우저 에이전트 인프라 | Browserbase Contexts | 쿠키/local storage/session state를 저장해 반복 로그인 제거, session replay로 관측 | P1에서 "로그인 프로필"과 "Vault credential"을 별도 저장소로 분리 |
| AI 웹 작업 안전 | OpenAI Operator / ChatGPT agent | 로그인/결제 등 민감 입력 시 사용자가 browser takeover, takeover 중 screenshot 미수집, 중요한 액션 전 확인 | MFA/passkey/payment/change password는 사람 takeover와 승인 게이트 |
| 표준 인증 | W3C WebAuthn Level 3 / FIDO passkeys | origin에 묶인 공개키 credential, user consent, synced/device-bound passkey | passkey는 raw secret 저장이 아니라 WebAuthn/OS/브라우저 위임으로 처리 |
| Credential API | W3C Credential Management | 웹사이트가 user agent에 credential 저장/요청을 돕는 API | 향후 자체 브라우저 또는 extension에서 form 감지/save prompt 구현 |

## 4. 제품 원칙

### 4.1 화면 분리 원칙

`/browser-tasks`는 "에이전트 작업 실행/승인" 화면으로 유지한다. `/agent-vault`는 "계정 등록/정책/로그" 전용 화면으로 분리한다.

이유:

- 계정 등록은 운영 보안 행위이고, 브라우저 작업 생성은 실행 행위다.
- 비밀번호 입력 폼이 작업 목록 옆에 있으면 실수 입력/노출 위험이 커진다.
- 접근 권한, 공유 범위, 감사 로그는 계정 단위로 관리해야 한다.

### 4.2 AI 에이전트 원칙

에이전트는 계정 원문을 보지 않는다.

- 금지: 비밀번호 보기, 복사, 내보내기, 로그 출력, LLM context 전달
- 허용: 승인된 origin/work_key/task에 한해 브라우저 폼으로 autofill
- 토큰: 1회성, TTL 60초 이하, origin/work_key/credential_id 바인딩
- 로그: 발급, 사용, 실패, 만료, 거부를 모두 기록

### 4.3 passkey/MFA/OTP 원칙

passkey, OTP, SMS, CAPTCHA, 생체인증은 저장·자동우회 대상이 아니다.

- passkey: OS/브라우저/WebAuthn 인증 UI로 위임
- OTP/MFA: 사용자 입력 요청 또는 PC Agent visible browser takeover
- CAPTCHA: 우회 금지, 스크린샷/상태 표시 후 사람 처리
- 결제/계정삭제/비밀번호변경: 항상 승인 필요

## 5. 전용 UI 정보구조

신규 라우트:

- `/agent-vault`
- Sidebar label: `Agent Vault`
- 관리자 전용 표시: `adminOnly: true`

화면 탭:

| 탭 | 목적 | 주요 컴포넌트 |
|---|---|---|
| 계정 | 등록된 계정 조회/검색/비활성화 | 검색, origin 필터, work_key 필터, 계정 테이블 |
| 새 계정 등록 | credential 생성/갱신 | wizard form, URL 검사, 비밀번호 생성, 저장 |
| 사용 정책 | work_key/origin별 allow/ask/deny | 정책 테이블, 위험도, 승인 규칙 |
| 접근 로그 | 감사/추적 | actor, task, origin, action, status, 시간 |
| 가져오기 | Chrome/1Password/Bitwarden 등 import 준비 | CSV/JSON 업로드, dry-run preview, 중복 병합 |

## 6. 계정 등록 UX

### 6.1 기본 등록 흐름

1. "새 계정" 버튼 클릭
2. 서비스 URL 입력
3. origin 자동 추출 및 favicon/title 조회
4. work_key 선택: 예) `aads-ceo-browser`, `food-delivery`, `finance-admin`
5. 계정 라벨 입력: 예) `대표 계정`, `정화점 배민`, `세무사 포털`
6. 사용자명/아이디 입력
7. 비밀번호 입력 또는 생성
8. 정책 선택: `작업 중 자동 로그인 허용`, `매번 승인`, `항상 차단`
9. 저장
10. 저장 후 raw password 제거, 목록에는 masked status만 표시

### 6.2 화면 레이아웃

첫 화면은 카드형 마케팅이 아니라 운영 콘솔이어야 한다.

상단:

- 제목: `Agent Vault`
- 상태 요약: 총 계정 수, 활성 계정 수, 승인대기, 최근 24시간 사용 횟수
- 우측 액션: `새 계정`, `가져오기`, `접근 로그`

좌측 필터:

- Work key
- Origin/domain
- 정책: allow/ask/deny
- 상태: active/disabled/needs-verification
- 소유자/프로젝트

메인 테이블:

| 컬럼 | 표시 |
|---|---|
| 서비스 | favicon + label + origin |
| 계정 | username masked or visible username only |
| Work key | badge |
| 정책 | 자동허용/승인필요/차단 |
| 마지막 사용 | timestamp |
| 상태 | active/disabled/needs MFA/passkey |
| 액션 | 정책변경, 테스트 로그인, 비활성화 |

우측 상세 패널:

- 계정 메타데이터
- 연결된 browser task/routine
- 최근 접근 로그 10건
- 위험 액션 정책
- 재인증 필요 알림

### 6.3 등록 폼 필드

필수:

- `target_url`: 사용자가 붙여넣는 로그인 페이지 URL
- `origin`: 서버가 정규화한 origin, 사용자는 읽기 전용 확인
- `work_key`: 에이전트/브라우저 프로필 범위
- `label`: 사람이 구분하는 이름
- `username`: ID/email/phone
- `password`: 저장 즉시 암호화, 화면 재표시 금지

선택:

- `owner`: 계정 책임자
- `project`: AADS/SF/KIS/GO100/NTV2/NAS/FOOD 등
- `auth_type`: password/passkey/mfa/captcha/manual
- `policy`: allow/ask/deny
- `notes`: 비보안 메모. 비밀번호/OTP/API key 입력 금지 안내
- `tags`: 업무 구분

## 7. API/DB 보강안

현재 API는 P0 저장/조회/토큰 발급이 가능하다. 전용 UI를 위해 다음 보강이 필요하다.

### 7.1 Credential metadata 표준화

`agent_vault_credentials.metadata`에 다음 키를 표준화한다.

```json
{
  "source": "agent-vault-ui",
  "service_name": "Baemin",
  "target_url": "https://...",
  "project": "FOOD",
  "owner": "CEO",
  "auth_type": "password",
  "policy": "ask",
  "tags": ["delivery", "store:junghwa"],
  "last_verified_at": null,
  "verification_status": "unverified"
}
```

### 7.2 API 추가

| API | 목적 | 우선순위 |
|---|---|---:|
| `POST /api/v1/agent-vault/credentials/preview-origin` | URL 입력 시 origin/title/favicon 후보 반환 | P0 |
| `PATCH /api/v1/agent-vault/credentials/{id}` | label/metadata/policy 수정 | P0 |
| `POST /api/v1/agent-vault/credentials/{id}/test-login` | PC Agent 브라우저로 로그인 가능 여부 테스트 | P1 |
| `POST /api/v1/agent-vault/import/preview` | CSV/JSON import dry-run | P1 |
| `POST /api/v1/agent-vault/import/commit` | import 확정 저장 | P1 |
| `GET /api/v1/agent-vault/security-summary` | 취약/오래됨/미검증 계정 요약 | P2 |

### 7.3 정책 테이블

P0는 metadata로 시작 가능하지만, P1부터는 정책 테이블을 분리한다.

```sql
CREATE TABLE agent_vault_policies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    credential_id UUID NULL REFERENCES agent_vault_credentials(id) ON DELETE CASCADE,
    work_key TEXT NOT NULL,
    origin TEXT NOT NULL,
    decision TEXT NOT NULL DEFAULT 'ask',
    allowed_actions JSONB NOT NULL DEFAULT '[]'::jsonb,
    denied_actions JSONB NOT NULL DEFAULT '[]'::jsonb,
    require_approval_for JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

## 8. 보안·권한 정책

| 행위 | 기본 정책 | 이유 |
|---|---|---|
| 로그인 폼 ID/PW autofill | ask 또는 allow | origin/work_key 일치 시만 가능 |
| 비밀번호 보기 | deny | AI/브라우저/화면 노출 금지 |
| 비밀번호 복사 | deny | clipboard 유출 위험 |
| 비밀번호 export | deny | 대량 유출 위험 |
| OTP/MFA 입력 | deny/manual | 2차 인증은 사람 처리 |
| passkey 승인 | manual takeover | WebAuthn user consent 필요 |
| 결제/송금/환불 | ask/high | 금전 행위 |
| 계정 삭제/비밀번호 변경 | ask/high 또는 deny | 복구 어려움 |
| 일반 조회/다운로드 | allow/low | 업무 자동화 기본 |

## 9. 구현 단계

### P0: 전용 UI 분리

목표: CEO가 "계정등록 어디서 하나"를 다시 묻지 않게 한다.

작업:

- Dashboard `src/app/agent-vault/page.tsx` 추가
- Sidebar에 `Agent Vault` 메뉴 추가
- `/browser-tasks`의 Agent Vault 등록 폼 제거 또는 "Agent Vault에서 관리" 링크로 대체
- 저장 계정 목록에서 password 컬럼 제거
- 기존 `api.ts` 메서드 재사용
- 접근 로그 목록 연결
- metadata 기반 정책 필드 저장

완료 기준:

- `/agent-vault` 진입 가능
- 계정 저장 성공
- 저장 후 비밀번호 입력값 초기화
- 목록에는 username/origin/label/policy만 표시
- raw password 표시/복사 UI 없음
- 접근 로그에 `credential_upsert` 기록 표시

### P1: 실사용 안정화

작업:

- URL 입력 시 origin 자동 미리보기
- 중복 계정 감지
- 비밀번호 생성기
- import preview
- 로그인 테스트 버튼
- PC Agent online/offline 표시

완료 기준:

- 같은 tenant/work_key/origin/label은 update로 처리
- 잘못된 URL 저장 방지
- PC Agent offline이면 테스트 로그인 버튼 disabled

### P2: 고급 보안

작업:

- passkey/WebAuthn 위임 설계
- OS credential store/Windows Hello/Secure Enclave 연동 검토
- Chrome/1Password/Bitwarden import
- 취약/오래된 비밀번호 경고
- routine별 credential binding

완료 기준:

- passkey는 raw secret 없이 browser/OS prompt로 처리
- 에이전트가 passkey/MFA를 우회하지 않음
- import 후 dry-run과 rollback 가능

## 10. 화면 문구

금지 문구:

- "비밀번호 보기"
- "복사"
- "AI에게 전달"
- "OTP 자동 입력"

권장 문구:

- `에이전트는 비밀번호 원문을 볼 수 없습니다. 승인된 사이트의 로그인 폼에만 1회성으로 주입됩니다.`
- `MFA, passkey, CAPTCHA는 직접 확인 후 계속 진행합니다.`
- `이 계정은 work_key와 origin이 일치하는 작업에서만 사용할 수 있습니다.`

## 11. 구현 지시서 초안

TASK_ID: AADS-187  
TITLE: Agent Vault 전용 계정 등록 UI 분리  
PRIORITY: P0  
SIZE: M  
MODEL: claude-sonnet-4-6  
DESCRIPTION:

1. `/root/aads/aads-dashboard/src/app/agent-vault/page.tsx`를 추가해 Agent Vault 전용 UI를 구현한다.
2. 기존 `/browser-tasks`의 Agent Vault 폼은 제거하거나 `/agent-vault` 링크로 대체한다.
3. `src/lib/api.ts`의 기존 Agent Vault API 메서드를 재사용하고, 필요 시 access logs 타입만 보강한다.
4. 목록에는 password 필드를 절대 표시하지 않는다.
5. 등록 폼은 target_url, origin, work_key, label, username, password, policy, project, owner, auth_type을 제공한다.
6. 저장 시 metadata에 source, target_url, project, owner, auth_type, policy, tags, verification_status를 넣는다.
7. 접근 로그를 같은 페이지 하단 또는 우측 패널에 표시한다.
8. `HANDOVER.md`에 변경·검증·미완료 항목을 기록한다.

검증:

- `npm run lint -- --file src/app/agent-vault/page.tsx`
- `npm run typecheck` 또는 프로젝트에서 가능한 타입 검사
- 운영 API 401 보호 확인
- 로그인 세션 또는 테스트 토큰으로 credential create/list/access-log 확인
- 브라우저 스크린샷으로 `/agent-vault` 렌더링 확인

## 12. 참고 출처

- Aside Password Manager: https://aside.com/features/password-manager
- Aside Security Help: https://docs.aside.com/help/security
- Aside Troubleshooting / Password autofill: https://docs.aside.com/help/troubleshooting
- Aside Changelog: https://docs.aside.com/changelog/components
- Google Password Manager: https://passwords.google.com/
- Google Password Manager guide: https://passwords.google/intl/en_sg/
- Chrome passkeys: https://support.google.com/chrome/answer/13168025
- Apple Passwords: https://support.apple.com/en-us/120758
- Microsoft Password Manager: https://explore.microsoft.com/en-us/edge/features/microsoft-password-manager
- Microsoft passkey saving in Edge: https://blogs.windows.com/msedgedev/2025/11/03/microsoft-edge-introduces-passkey-saving-and-syncing-with-microsoft-password-manager/
- 1Password Unified Access: https://1password.com/platform
- 1Password Browser Extension: https://1password.com/downloads/browser-extension
- Bitwarden Password Manager: https://bitwarden.com/
- Bitwarden Secrets Manager: https://bitwarden.com/help/secrets-manager-overview/
- Dashlane Business Password Manager: https://www.dashlane.com/business-password-manager
- Keeper PAM: https://www.keepersecurity.com/privileged-access-management/
- Browserbase Contexts: https://docs.browserbase.com/platform/browser/core-features/contexts
- Browserbase Authentication: https://docs.browserbase.com/platform/identity/authentication
- OpenAI Operator: https://openai.com/index/introducing-operator/
- ChatGPT agent help: https://help.openai.com/en/articles/11752874-chatgpt-agent
- W3C WebAuthn Level 3: https://www.w3.org/TR/webauthn-3/
- W3C Credential Management Level 1: https://www.w3.org/TR/credential-management-1/
- FIDO Passkeys: https://fidoalliance.org/passkeys/
