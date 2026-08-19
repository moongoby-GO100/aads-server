# OHVIS Aside 벤치마크 기반 자체 브라우저/PC Agent 강화 상세 기획서

> 문서 ID: AADS-OHVIS-ASIDE-BROWSER-AGENT-PLAN-20260819  
> 작성 기준: 2026-08-19 19:27 KST 실측, Aside 공식 사이트/문서 확인, AADS 현재 코드 구조 확인  
> 대상 프로젝트: AADS / OHVIS  
> 상태: 구현 전 상세 기획서  

## 1. 결론

OHVIS가 Aside에서 가져와야 할 핵심은 "AI 브라우저"라는 이름이 아니라, **로그인된 실제 웹사이트를 안전하게 조작하는 로컬 우선 작업 실행 환경**이다. 현재 AADS에는 이미 PC Agent, Browser Bridge, Credential Vault, Web Push, 채팅/Runner 계층이 있으므로 완전 신규 제품을 만들기보다 아래 순서가 가장 현실적이다.

1. P0: 기존 PC Agent + Browser Bridge를 "OHVIS Managed Browser Session"으로 제품화한다.
2. P0: 비밀번호 값을 AI에게 노출하지 않는 Agent Vault Autofill과 승인 게이트를 만든다.
3. P1: 루틴, 작업 메모리, 사이트별 작업 레시피, 알림/재개 흐름을 묶는다.
4. P2: 독립 데스크톱 앱 형태의 OHVIS Browser Shell을 만든다.
5. P3: Chromium fork는 마지막 선택지로 남긴다. 유지보수 비용이 크고 MVP 검증에 불필요하다.

## 2. 근거

### 2.1 Aside 공식 기능 요약

| 영역 | Aside에서 확인한 기능 | OHVIS 반영 판정 | 근거 |
|---|---|---|---|
| 브라우저 기반 에이전트 | 사용자가 로그인한 사이트와 계정에서 브라우저 작업 수행 | P0 반영 | https://aside.com |
| 작업 실행 컨트롤 | 모드, 권한, 작업 폴더, 모델 선택 | P0 반영 | https://docs.aside.com/help/tasks |
| 권한 모드 | Read only, Guard, Full access | P0 반영 | https://docs.aside.com/help/security |
| 비밀번호 관리자 | 에이전트 자동입력, 비밀번호 값 비노출, 사이트별 접근 제어, 감사로그 | P0 반영 | https://aside.com/features/password-manager, https://docs.aside.com/help/password-manager |
| 로컬 메모리 | 브라우징 이력과 작업 결과를 로컬 메모리로 저장하고 출처 확인 | P1 반영 | https://aside.com/features/memory, https://docs.aside.com/help/memory |
| 루틴 | Cron routine, Heartbeat routine, 중복 실행 방지 | P1 반영 | https://docs.aside.com/help/automation |
| 개발자 도구 | CLI, MCP, REPL로 브라우저 자동화 연결 | P1 반영 | https://docs.aside.com/help/developers |
| 가격/운영 모델 | Free/Pro/Max, BYO subscription, Enterprise 공유 프로필/볼트 | 참고 | https://aside.com/pricing |

### 2.2 AADS 현재 대응 자산

| OHVIS 보유 자산 | 현재 상태 | 활용 방향 |
|---|---|---|
| `app/api/pc_agent.py` | PC Agent WebSocket/REST, online status, route execute | 브라우저/파일/OS 작업 실행 게이트웨이 |
| `app/services/pc_agent_manager.py` | capability 기반 라우팅, busy/queue/lease 처리 | 작업 큐, 권한 모드, 세션 락의 중심 |
| `app/browser_bridge/service.py` | work_key별 세션, PC Agent local browser command, recovery | 자체 브라우저 세션 관리자 |
| `app/browser_bridge/storage_state.py` | 브라우저 storage state 저장 | 로그인 상태 복구/세션 이식 기반 |
| `app/core/credential_vault.py` | tenant scoped Fernet 암호화 credential 저장 | Agent Vault v1 기반 |
| `app/api/notifications.py` / `app/services/push_notifications.py` | 웹 푸시 구독, 테스트, 채팅 완료 알림 | 장기 작업 완료/승인 대기 알림 |
| `docs/plans/AADS-PC-AGENT-MULTI-SERVICE.md` | work_key별 CDP 격리 계획 | OHVIS Browser profile isolation으로 확장 |

2026-08-19 19:27 KST 로컬 API 기준 PC Agent는 `online_count=1`이고, 연결 Agent `2e9379a1-fed`는 `chrome_cdp`, `interactive_browser`, `pc_control` capability와 `shell`, `powershell`, `notification` command type을 보유한다. 즉, 기능 자체는 구현 가능하고 제품화/보안/UX 정리가 남은 상태다.

## 3. 제품 목표

### 3.1 한 문장 목표

OHVIS 채팅에서 "배민 정산 확인해", "Genspark에서 이미지 만들고 내려받아", "GitHub 이슈 보고 수정 PR 만들어" 같은 요청을 내리면, OHVIS가 전용 브라우저 세션을 열고 로그인/탐색/입력/다운로드/보고까지 수행하되, 비밀번호와 민감 행위는 사용자의 통제 아래 둔다.

### 3.2 성공 기준

| 기준 | 목표 |
|---|---|
| 로그인 자동화 | 저장된 계정으로 사이트 로그인 폼 자동입력, 비밀번호 값은 LLM/로그에 노출 0건 |
| 권한 안전성 | 결제, 게시, 메시지 발송, 파일 삭제, 권한 변경 전 human approval 100% |
| 세션 안정성 | work_key별 독립 Chrome profile, 동시 4개 업무 세션 상호 간섭 0건 |
| 작업 재개 | 브라우저/PC Agent 재시작 후 최근 작업 상태 복구 |
| 감사 추적 | credential 사용, 승인, 거부, 다운로드, 파일쓰기, 외부 전송 이벤트 기록 |
| 알림 | 장기 작업 완료/승인 대기/인증 필요 시 앱 푸시 알림 발송 및 클릭 시 해당 세션 이동 |

## 4. OHVIS에 반영할 기능 전체 목록

| 우선 | 기능 | 세부 기능 | 구현 위치 | 비고 |
|---|---|---|---|---|
| P0 | Managed Browser Session | work_key별 프로필, 탭, 다운로드 폴더, 세션 복구 | `browser_bridge`, `pc_agent/commands/browser_auto.py` | 현재 구조 확장 |
| P0 | Agent Vault Autofill | 사이트별 credential 매칭, username/password 자동입력, 값 비노출 | `credential_vault`, 신규 `agent_vault` | 기존 Vault 보강 |
| P0 | Approval Gate | 결제/게시/전송/삭제/권한변경 전 확인 카드 | 신규 `approval_policy`, chat UI | 운영 안전 핵심 |
| P0 | Browser Task Console | 현재 URL, 단계, 스크린샷, 대기 사유, 승인 버튼 | dashboard | CEO가 작업을 감시/개입 |
| P0 | MFA/인증 처리 | OTP/문자/이메일/앱 인증 대기 상태, 입력 요청, 제한시간 | chat + push | 자동 우회 금지 |
| P0 | Audit Log | credential access, form fill, click, download, approval | DB 신규 테이블 | 보안 사고 추적 |
| P1 | Site Recipe Memory | 사이트별 성공 경로, selector, 실패 원인 저장 | memory + pgvector | 반복 자동화 성공률 향상 |
| P1 | Routine Automation | cron/heartbeat, 중복 실행 방지, 실패 알림 | 기존 schedule_task/DB 확장 | Aside 루틴 대응 |
| P1 | Browser File Workspace | 다운로드/생성 파일 목록, 미리보기, 서버 저장 | pc_agent + artifacts | Genspark/보고서 작업에 필요 |
| P1 | Command Palette | 열려있는 세션/북마크/도구/루틴 검색 | dashboard | 자체 브라우저 UX |
| P1 | Local Memory Viewer | MEMORY.md 유사 편집 가능한 작업 메모리 | dashboard + DB | provenance 필수 |
| P1 | Side Chat / Steer | 실행 중 작업에 새 지시 주입, queue/steer 선택 | chat_service | 장기 작업 제어 |
| P2 | OHVIS Browser Shell | 데스크톱 앱, 탭/사이드패널/알림/볼트 UI | Tauri/WebView2 또는 Electron | 제품화 단계 |
| P2 | Team Shared Vault | tenant/team vault, shared profile, seat 권한 | DB + RBAC | SaaS/직원용 |
| P2 | MCP Server Mode | 외부 코딩 에이전트가 OHVIS 브라우저 도구 사용 | MCP adapter | AADS 생태계 확장 |
| P3 | Chromium Fork | 자체 프로토콜/권한 엔진 내장 | 별도 repo | 비용 큼, 최후순위 |

## 5. 권장 아키텍처

### 5.1 전체 구조

```text
OHVIS Chat / Dashboard
        |
        v
Browser Task Gateway
 - 요청 분류
 - 권한 모드 결정
 - work_key/session_id 생성
 - 승인 필요 액션 판정
        |
        +--------------------+
        |                    |
        v                    v
Agent Vault Service     Browser Session Service
 - credential lookup    - work_key profile
 - autofill token       - CDP/Playwright facade
 - policy check         - screenshots/snapshot
 - audit log            - download/artifact tracking
        |                    |
        +---------+----------+
                  |
                  v
PC Agent Router / Lease Queue
 - capability match
 - busy queue
 - command timeout
 - route fallback
                  |
                  v
CEO PC Agent
 - Chrome CDP
 - OS notification
 - file system
 - PowerShell/CMD where allowed
```

### 5.2 컴포넌트별 책임

| 컴포넌트 | 책임 | 신규/수정 |
|---|---|---|
| `app/services/browser_task_gateway.py` | 채팅 요청을 브라우저 작업으로 접수하고 work_key, permission_mode, target_url을 결정 | 신규 |
| `app/services/agent_vault_service.py` | credential 검색, 사이트 매칭, autofill payload 생성, 비밀값 masking | 신규 |
| `app/services/browser_permission_policy.py` | Allow/Ask/Deny, 위험 액션 분류, 승인 필요 여부 판단 | 신규 |
| `app/services/browser_task_audit.py` | 브라우저 작업/credential/승인/파일 이벤트 기록 | 신규 |
| `app/api/browser_tasks.py` | 작업 생성/상태/승인/중단/재개 API | 신규 |
| `app/browser_bridge/service.py` | ensure_work_session, recovery, storage_state, protected work_key 강화 | 수정 |
| `app/services/pc_agent_manager.py` | permission_mode, lease metadata, approval wait 상태 추가 | 수정 |
| `pc_agent/commands/browser_auto.py` | profile isolation, autofill bridge, DOM action classifier | 수정 |
| `aads-dashboard` | 브라우저 작업 콘솔, Vault 관리, 승인 카드, 루틴 관리 | 수정 |

### 5.3 세션 모델

| 세션 종류 | 사용처 | 격리 수준 |
|---|---|---|
| `general` | 일반 탐색, 검색 | 낮음 |
| `ohvis-{workspace}-{service}` | 반복 업무 자동화 | 높음, profile 분리 |
| `protected:{service}` | 금융/세무/인사/메시지 발송 | 최고, CEO 승인 필수 |
| `incognito:{task_id}` | 일회성 검증/외부 계정 | 저장 안 함 |

work_key는 `browser_bridge` 기존 규칙을 따른다. 예: `ohvis-aads-genspark`, `ohvis-food-baemin`, `protected:hometax`.

## 6. 비밀번호 관리자 / 인증 처리 설계

### 6.1 Agent Vault 원칙

1. LLM에게 username/password/passkey/OTP 원문을 전달하지 않는다.
2. Vault는 target origin, form fingerprint, credential scope를 확인한 뒤 PC Agent에만 1회성 autofill token을 보낸다.
3. PC Agent는 토큰으로 복호화 값을 받아 DOM 입력에만 사용하고, 로그/응답/스크린샷 OCR에는 값을 남기지 않는다.
4. credential 사용 전후로 audit log를 남긴다.
5. MFA, 금융이체, 결제, 게시, 고객 메시지 발송은 항상 사용자 확인을 요구한다.

### 6.2 Autofill 흐름

```text
1. Chat task: "홈택스 로그인해서 전자세금계산서 확인"
2. Browser Task Gateway: service=hometax, permission_mode=guard
3. Agent Vault: tenant/project/service 기준 credential 후보 조회
4. Policy: target origin이 credential allowlist와 일치하는지 확인
5. Vault: 60초 TTL autofill token 발급
6. PC Agent: browser_fill_secret(token, selector=password)
7. Browser: 실제 입력, 값은 agent transcript에 저장하지 않음
8. Audit: credential_used, target_origin, task_id, user_id, result 저장
```

### 6.3 MFA 처리

| 인증 유형 | 처리 |
|---|---|
| SMS/이메일 OTP | CEO에게 입력 요청 카드 + 앱 푸시, 자동 읽기 금지 |
| 인증 앱 OTP | CEO 직접 입력 또는 승인된 로컬 OTP provider 연결 |
| Passkey/생체 인증 | PC Agent가 OS prompt를 띄우고 CEO 확인 대기 |
| 공동/금융인증서 | 인증서 선택/비밀번호 입력은 `protected` 세션에서 사용자 승인 후 진행 |
| CAPTCHA | 자동 우회 금지, 사용자 해결 요청 |

### 6.4 위험 액션 분류

| 액션 | 기본 정책 |
|---|---|
| 페이지 읽기, 스크린샷, 다운로드 | Guard에서 허용 가능 |
| 로그인 자동입력 | 사이트 allowlist 일치 시 허용, 감사로그 필수 |
| 파일 업로드 | Ask |
| 게시글/댓글/메시지 발송 | Ask |
| 결제/이체/환불/계정 권한 변경 | Ask + 2단계 확인 |
| 파일 삭제/대량 수정 | Ask 또는 Deny |
| 시크릿 표시/복사 | Deny |

## 7. 데이터 모델 초안

```sql
CREATE TABLE IF NOT EXISTS browser_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    session_id UUID,
    user_id TEXT NOT NULL,
    project_key TEXT NOT NULL DEFAULT 'AADS',
    work_key TEXT NOT NULL,
    target_url TEXT,
    instruction TEXT NOT NULL,
    permission_mode TEXT NOT NULL DEFAULT 'guard',
    status TEXT NOT NULL DEFAULT 'queued',
    current_step TEXT DEFAULT '',
    requires_approval BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS browser_task_events (
    id BIGSERIAL PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES browser_tasks(id),
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS agent_vault_policies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    credential_id UUID NOT NULL,
    service TEXT NOT NULL,
    allowed_origins JSONB NOT NULL DEFAULT '[]'::jsonb,
    allowed_work_keys JSONB NOT NULL DEFAULT '[]'::jsonb,
    default_action TEXT NOT NULL DEFAULT 'ask',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS agent_vault_access_logs (
    id BIGSERIAL PRIMARY KEY,
    tenant_id UUID NOT NULL,
    credential_id UUID,
    task_id UUID,
    user_id TEXT NOT NULL,
    target_origin TEXT NOT NULL,
    action TEXT NOT NULL,
    result TEXT NOT NULL,
    reason TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS browser_routines (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    project_key TEXT NOT NULL,
    work_key TEXT NOT NULL,
    routine_type TEXT NOT NULL,
    schedule_expr TEXT,
    instruction TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    last_run_at TIMESTAMPTZ,
    next_run_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

## 8. API 초안

| API | 용도 |
|---|---|
| `POST /api/v1/browser-tasks` | 브라우저 작업 생성 |
| `GET /api/v1/browser-tasks/{task_id}` | 작업 상태/현재 단계 조회 |
| `POST /api/v1/browser-tasks/{task_id}/approve` | 위험 액션 승인 |
| `POST /api/v1/browser-tasks/{task_id}/reject` | 위험 액션 거부 및 피드백 |
| `POST /api/v1/browser-tasks/{task_id}/steer` | 실행 중 작업에 추가 지시 |
| `POST /api/v1/browser-tasks/{task_id}/pause` | 일시정지 |
| `POST /api/v1/browser-tasks/{task_id}/resume` | 재개 |
| `POST /api/v1/agent-vault/policies` | 사이트별 credential 사용 정책 등록 |
| `POST /api/v1/agent-vault/autofill-token` | PC Agent용 1회성 autofill token 발급 |
| `GET /api/v1/browser-routines` | 루틴 목록 |
| `POST /api/v1/browser-routines` | 루틴 생성 |

## 9. 자체 브라우저 구축 전략

### 9.1 권장 선택지

| 방식 | 장점 | 단점 | 판정 |
|---|---|---|---|
| 기존 Chrome + PC Agent 관리 프로필 | 가장 빠름, 현재 코드 재사용, Chrome 호환성 최고 | 완전한 브랜드 브라우저 느낌은 약함 | P0 채택 |
| Electron + Chromium | 자체 앱/탭/사이드패널 구현 쉬움 | 앱 크기/업데이트/보안 패치 부담 | P2 후보 |
| Tauri/WebView2 | 가볍고 Windows 친화적 | Chromium CDP 제어와 자동화 확장 설계 필요 | P2 후보 |
| Chromium fork | 완전한 통제권 | 유지보수 비용 과다, 보안 업데이트 부담 | P3 보류 |

### 9.2 단계별 제품화

#### Phase 0: 현 기능 안정화

- PC Agent online/offline 상태와 command_types를 대시보드에서 명확히 표시한다.
- `work_key`별 profile dir, download dir, last_url, lease owner를 시각화한다.
- PC Agent C 드라이브 용량, Chrome profile 오류, CDP handshake 오류를 경고로 노출한다.

#### Phase 1: OHVIS Managed Browser MVP

- 채팅에서 브라우저 작업을 만들면 `BrowserTaskGateway`가 `browser_bridge.ensure_work_session()`을 호출한다.
- 작업 콘솔에 현재 URL, 단계, 스크린샷, 로그, 승인 대기를 표시한다.
- 결과 파일은 chat artifact와 연결한다.
- 완료/인증필요/승인대기 알림은 기존 push notification 경로를 재사용한다.

#### Phase 2: Agent Vault MVP

- 기존 `e2e_credentials`를 유지하되 `agent_vault_policies`를 추가한다.
- 비밀번호 원문을 LLM/채팅 응답/도구 결과에 노출하지 않는 secret fill command를 추가한다.
- target origin과 work_key가 맞지 않으면 Deny한다.
- credential 사용 이력은 `agent_vault_access_logs`에 기록한다.

#### Phase 3: 루틴/메모리/레시피

- 반복 작업을 `browser_routines`로 관리한다.
- 작업 성공 시 사이트별 selector, navigation path, 실패 복구법을 memory fact로 저장한다.
- 다음 실행에서 사이트 레시피를 먼저 적용하고 실패하면 시각/DOM 기반 탐색으로 전환한다.

#### Phase 4: Desktop Browser Shell

- 탭 UI, 사이드패널, Vault unlock, 알림, 승인 카드, 작업 목록을 데스크톱 앱으로 제공한다.
- 내부 구현은 기존 `pc_agent`와 `browser_bridge` API를 그대로 사용한다.
- 브라우저 엔진은 Windows 우선이면 WebView2/Tauri, 빠른 크로스플랫폼이면 Electron을 비교 PoC한다.

#### Phase 5: Team/Enterprise 모드

- tenant별 shared browser profile, shared vault, seat 권한, 승인자 그룹을 추가한다.
- 회사 공용 계정 접근은 task_id, user_id, approval_id와 묶어 감사 가능하게 만든다.

## 10. UI/UX 화면 기획

| 화면 | 구성 |
|---|---|
| Browser Task Console | 작업 상태, 현재 단계, URL, 스크린샷, 이벤트 타임라인, 중단/재개/조향 버튼 |
| Vault Manager | 서비스, 계정, 허용 origin, work_key, 마지막 사용, 실패 이력 |
| Approval Inbox | 위험 액션 카드, diff/스크린샷/대상 URL, 승인/거부/수정지시 |
| Routine Center | 루틴 목록, cron/heartbeat, 마지막 실행, 다음 실행, 실패 알림 |
| Memory/Recipe Viewer | 사이트별 성공 경로, selector, 주의사항, provenance, 수동 수정 |
| PC Agent Health | 연결 상태, capability, heartbeat, disk, Chrome profile, CDP 상태 |

## 11. 구현 작업 분해

### P0 Sprint 1

| 작업 | 산출물 | 검증 |
|---|---|---|
| Browser Task DB/API | `browser_tasks`, `browser_task_events`, `/browser-tasks/*` | API unit test, auth/RBAC test |
| Browser Task Gateway | 채팅 요청에서 작업 생성/상태 이벤트 기록 | fake PC Agent test |
| Task Console | dashboard 작업 콘솔 | eslint/build, Playwright smoke |
| Push 연동 | 승인대기/완료/인증필요 알림 | payload URL/session 이동 검증 |

### P0 Sprint 2

| 작업 | 산출물 | 검증 |
|---|---|---|
| Agent Vault Policy | `agent_vault_policies`, access logs | origin mismatch deny test |
| Secret Autofill Command | `browser_fill_secret` 또는 기존 fill 확장 | 로그에 secret 미노출 test |
| Approval Gate | policy classifier + approval API | 결제/게시/메시지 ask test |
| MFA Card | OTP/passkey/captcha 상태 카드 | manual E2E |

### P1 Sprint 3

| 작업 | 산출물 | 검증 |
|---|---|---|
| Site Recipe Memory | memory fact 저장/조회 | selector regression test |
| Routines | cron/heartbeat + overlap skip | scheduler test |
| Steer/Queue | 실행 중 작업 추가 지시 | long task test |
| File Workspace | 다운로드/생성 파일 artifact 연결 | download smoke |

### P2 Sprint 4

| 작업 | 산출물 | 검증 |
|---|---|---|
| Desktop Shell PoC | Tauri/WebView2 또는 Electron app | Windows install/run smoke |
| Vault Unlock UI | 로컬 unlock, biometric/passkey 후보 | 보안 리뷰 |
| Team Profile | shared profile/vault 권한 | tenant isolation test |

## 12. 보안/운영 리스크와 대응

| 리스크 | 영향 | 대응 |
|---|---|---|
| 비밀번호 로그 노출 | 치명적 보안 사고 | secret command, masking test, log sanitizer |
| 잘못된 사이트에 credential 입력 | 계정 탈취 위험 | origin allowlist, form fingerprint, work_key scope |
| 결제/게시 자동 실행 | 금전/평판 피해 | human approval mandatory |
| 오래된 Chrome profile 충돌 | 작업 실패/세션 오염 | work_key 격리, profile health check |
| PC Agent offline | 작업 중단 | push 알림, reconnect guide, queue resume |
| CAPTCHA/MFA 자동화 오해 | 정책/보안 문제 | 자동 우회 금지, 사용자 입력 대기 |
| 브라우저 앱 보안 패치 부담 | 운영비 증가 | MVP는 managed Chrome, fork 보류 |

## 13. 검증 기준

| 코드 | 시나리오 | 완료 기준 |
|---|---|---|
| T-01 | `ohvis-aads-genspark`와 `ohvis-food-baemin` 동시 실행 | 서로 다른 URL/탭/profile 유지 |
| T-02 | Vault credential 자동입력 | secret이 로그/응답/DB event payload에 없음 |
| T-03 | origin mismatch | credential 사용 차단, audit result=denied |
| T-04 | 메시지 발송 버튼 클릭 전 | approval required 상태로 멈춤 |
| T-05 | 승인 알림 클릭 | 해당 chat session/task console로 이동 |
| T-06 | PC Agent 재시작 | 작업 상태가 failed 또는 resumable로 명확히 전이 |
| T-07 | 루틴 중복 실행 | 기존 실행 중이면 새 실행 skip |
| T-08 | 다운로드 파일 | artifact로 연결되고 경로/메타데이터 표시 |

## 14. Runner 지시서 초안

```text
TASK_ID: AADS-186
TITLE: OHVIS Managed Browser + Agent Vault P0
PRIORITY: P0
SIZE: L
MODEL: default runner model
DESCRIPTION:
Aside 벤치마크 기반으로 OHVIS 자체 브라우저/PC Agent 강화 P0을 구현한다.

Scope:
1. Add browser task DB/API/service layer.
2. Add browser task console in dashboard.
3. Add Agent Vault policy and audit logs.
4. Add secret autofill route/PC Agent command without exposing secrets to LLM/logs.
5. Add approval gate for risky actions.
6. Reuse existing push notification path for task completed/auth required/approval required.

Constraints:
- Do not expose raw credentials in logs, tool results, SSE, DB event payloads, or chat messages.
- Do not deploy or restart without approval stage.
- Preserve existing PC Agent and Browser Bridge behavior.
- Use work_key profile isolation.

Verification:
- py_compile changed backend files.
- unit tests for origin policy, secret masking, approval classifier, browser task API.
- dashboard eslint/build for changed pages.
- API fallback validation if browser E2E login is unavailable.
```

## 15. 최종 권장안

즉시 착수할 기능은 Chromium fork가 아니라 **OHVIS Managed Browser + Agent Vault + Approval Gate**다. 이 조합이 Aside의 핵심 효용을 가장 빠르게 AADS에 흡수하면서도, 현재 PC Agent/Browser Bridge/Push/Chat 구조를 그대로 활용한다. 자체 데스크톱 브라우저는 P0/P1에서 사용성이 검증된 뒤 별도 Shell로 감싸는 것이 비용과 리스크가 가장 낮다.
