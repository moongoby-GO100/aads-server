# GO100 HANDOVER — 2026-04-21

## 최근 완료 작업 (05/04 20:02 KST)

### 1. React 스크리너 저장조건/상세 패널 + 독립 차트 페이지 최신화
- **파일**: `frontend/src/go100/pages/ScreenerPage.tsx`, `frontend/src/app/(protected)/stock/[code]/page.tsx`.
- **스크리너**: 저장 조건(localStorage), 마지막 검색 복원, 선택 종목 상세 지표 패널, 독립 차트(`/stock/{code}`) 이동을 추가. 기존 프리셋/기간/제외조건/CSV/전략카드 조건검색 흐름은 유지.
- **차트**: 기존 모달/리다이렉트 구조를 V4 차트 API 기반 독립 페이지로 교체. 일/주/분봉 전환, MA/Bollinger/RSI 토글, 체결/전략 시그널 오버레이, 외국인/기관/개인 수급, 호가, 재무, 체결강도 패널을 한 화면에 배치.
- **종목 표기**: 스크리너/차트 모두 `formatStock()` 기준으로 표시.
- **검증**: 변경 파일 ESLint 통과. `npm run build` 성공. 기존 React Hook warning 4건은 변경 파일 밖 기존 경고.
- **운영 주의**: 코드 반영만 완료. `go100-frontend` 재시작은 CEO 승인 전 미실행. 작업트리에 manager 스냅샷 산출물 미커밋 변경이 별도로 존재하므로 커밋 시 위 2개 파일과 HANDOVER만 분리 필요.

## 최근 완료 작업 (05/04 18:24 KST)

### 1. React `/go100/screener` 고급 조건검색 이식
- **파일**: `frontend/src/go100/pages/ScreenerPage.tsx`, `frontend/src/go100/api/screenerApi.ts`.
- **내용**: 정적 `stock-search.html/js`의 핵심 고급 기능을 React 스크리너로 이식. v4 스크리너 메타/검색 API 연동, 프리셋 그룹, 동적 조건 빌더, 기준일/기간 모드, 제외 필터, 정렬, 페이지네이션, CSV 내보내기를 추가. 전략카드 조건검색 모드는 유지.
- **종목 표기**: 일반/전략카드 결과와 CSV에서 `formatStock()`을 사용하도록 정리.
- **검증**: `pnpm lint` 통과, `pnpm build` 성공, `git diff --check` 통과. Public domain 기준 `/api/v4/stock-screener/meta`, `/api/v4/stock-screener/search` HTTP 200 확인. 기존 React Hook warning 4건은 기존 파일 경고.
- **운영 주의**: `go100-frontend` 재시작은 CEO 승인 전 미실행. 현재 작업트리에 별도 실시간 스캘핑 관련 미커밋 변경이 있어 커밋 시 스크리너 2개 파일만 분리 필요.

## 최근 완료 작업 (05/04 10:08 KST)

### 1. GO100 P0 라우터/XSS/조건검색 보강 + 프론트 무중단 배포 안전 점검 복구
- **라우터**: `backend/app/main.py`, `backend/app/routers/go100/__init__.py`에 정의만 되어 있던 `go100_strategy_approval_router`, `go100_signal_router` 등록. import 기반 route 검증에서 `/api/go100/strategies/{strategy_id}/approve`, `/api/v1/go100/signals/check`, `/api/v1/go100/signals/history` 확인.
- **XSS**: `frontend/src/go100/components/command-center/ChatMessage.tsx`, `frontend/src/go100/components/ChatMessage.tsx`의 `ReactMarkdown`에 `skipHtml` 명시. 기존 링크 allowlist와 이미지 차단 유지.
- **조건검색 IDOR 방지**: `frontend/src/go100/components/command-center/ConditionsTab.tsx`에서 `/api/go100/conditions*` 호출 시 `user_id` query 전달 제거. 백엔드는 `get_current_user` + `get_effective_uid()` 기준 유지.
- **배포 안전**: 직접 `.next` 삭제/직접 빌드/즉시 재시작하던 프론트 배포 진입점을 `scripts/deploy_frontend_only.sh`로 위임. `scripts/CUR-GO100-EMERGENCY-FULL-CHECK.sh`, `scripts/go100/install_manager_snapshot.sh`, `scripts/t173_root_ops.sh`의 위험 구간도 안전 배포 위임으로 변경.
- **검증**: `python3 -m py_compile` 통과, 운영 venv import 기반 route 확인 통과, 변경 프론트 3파일 ESLint 통과, `bash -n` 통과, `scripts/check_go100_frontend_deploy_safety.sh` 결과 PASS 22 / WARN 0 / FAIL 0. `go100`, `go100-frontend`는 active. 빌드/재시작/배포는 CEO 승인 없이 실행하지 않음.

## 최근 완료 작업 (05/04 09:04 KST)

### 1. Command Center 네비게이션 적용 안정화
- **파일**: `frontend/src/go100/components/command-center/ContextPanel.tsx`, `frontend/src/go100/components/command-center/MobileNav.tsx`, `frontend/src/go100/components/command-center/NavBar.tsx`.
- **내용**: Next.js `usePathname()`이 hydration/초기 렌더 구간에서 null을 반환할 때 command-center 탭 링크 생성이 깨지지 않도록 기본 경로 `/go100/command-center`를 적용.
- **검증**: `npm run build` 성공. 기존 React Hook warning 4건은 기존 파일 경고. `go100-frontend` 재시작 완료, `/go100/command-center` HTTP 307 인증 리다이렉트 확인.


## 최근 완료 작업 (05/04 08:58 KST)

### 1. Command Center 내부 도구 진행 로그 사용자 노출 차단
- **파일**: `frontend/src/go100/hooks/useChat.ts`, `frontend/src/go100/components/command-center/ChatArea.tsx`, `frontend/src/go100/components/command-center/ChatMessage.tsx`, `frontend/src/go100/components/command-center/chat-area.css`.
- **내용**: SSE `progress` 이벤트의 내부 도구명/실행 로그를 채팅 본문에 그대로 표시하지 않고 `백억이가 자료를 확인하고 있습니다.` 상태 문구로 치환. 최종 `content` delta가 도착하면 진행 상태를 제거하고 Markdown 답변만 남기도록 변경.
- **UI**: 진행 문구는 일반 답변 버블과 분리된 작은 상태줄(`msg-progress-note`)로 표시해 사용자가 내부 실행 과정을 본문으로 오해하지 않도록 함.
- **검증**: `pnpm lint` 통과, `pnpm build` 성공. `go100-frontend` 재시작 후 `/auth/login` HTTP 200, `/go100/command-center` 307(인증 리다이렉트 정상), `.next/BUILD_ID`와 `prerender-manifest.json` 존재 확인.

## 최근 완료 작업 (04/30 18:58 KST)

### 1. GO100 화면 P0 직접 복구: 로그인 복귀 + 종목분석 카드 fallback
- **파일**: `backend/app/api/v1/social_auth_router.py`, `backend/app/routers/go100/ai_router.py`, `frontend/src/middleware.ts`, `frontend/src/app/auth/login/page.tsx`, `frontend/src/app/auth/callback/page.tsx`.
- **내용**: 보호 페이지 로그인 리다이렉트에서 query string 포함 원래 경로를 `from`으로 보존하고, 일반/소셜 로그인 완료 후 `return_to` 기준으로 원래 GO100 화면으로 복귀하도록 수정.
- **카드 복구**: `ai_router.py`의 중복 카드 빌더 블록을 제거해 `_build_cards_for_intent`/market/stock/portfolio 정의를 1개로 정리. `stock_analysis` alias도 카드 생성 경로로 허용해 종목분석 응답이 텍스트만 나열되는 상황을 줄임.
- **검증**: Python `py_compile` 통과, 프론트 변경 파일 ESLint 통과, 카드 빌더 중복 제거 grep 확인.
- **운영 주의**: 코드 커밋 기준 반영. 실제 화면 적용에는 `go100`/`go100-frontend` 빌드 및 재시작이 필요하며 GO100 운영 규칙상 CEO 승인 후 실행.

## 최근 완료 작업 (04/29 11:03 KST)

### 1. LLM 인증 문서 확인 + GO100 DB 우선 인증 고정
- **확인 문서**: `docs/technical/LLM_AUTH_ARCHITECTURE_v2.1.md`, `docs/technical/GO100_CLI_RELAY_ARCHITECTURE.md`.
- **문서 기준**: Claude는 OAuth 토큰 우선, Codex는 CLI/OAuth JSON 또는 OpenAI API key, 키 관리는 프로젝트별 DB 우선 + 기존 파일/env 폴백. AADS DB를 GO100이 직접 참조하지 않고 GO100 자체 `go100_llm_api_keys`에 암호화 저장한다.
- **DB 이관**: `/root/.claude/current.env`의 `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_AUTH_TOKEN_2`와 `/root/.codex/auth.json`의 Codex OAuth JSON을 `go100_llm_api_keys`에 암호화 등록. 평문은 로그/응답에 출력하지 않음.
- **운영 API**: `backend/app/routers/go100/llm_registry_router.py`에 `GET /api/go100/llm-registry/admin/auth-status`, `POST /api/go100/llm-registry/admin/reload-auth` 추가. 인증 소스(DB/current.env/process env/root auth), relay 상태, 선택모델 fallback 정책, MCP 도구 정책을 마스킹 상태로 확인 가능.
- **Relay**: `scripts/go100_relay_server.py`의 `/health`에 Claude/Codex 인증 active source 표시 추가, `/reload-auth`로 쿨다운/인증 상태 즉시 재조회 가능. Claude/Codex 모두 DB 키 우선, 기존 파일 폴백 유지.
- **정책**: 사용자가 직접 선택한 모델은 다른 모델로 폴백하지 않고 동일 모델 재시도만 수행. `auto` 모델만 인텐트 기반 fallback 유지.
- **검증**: Python 3 `py_compile` 통과. 배포 후 `/health`와 admin auth-status에서 `anthropic`/`codex` DB 키 존재와 relay active source를 확인할 것.

## 최근 완료 작업 (04/29 08:20 KST)

### 1. Command Center 모델 선택/응답 장애 긴급 복구
- **파일**: `frontend/src/go100/hooks/useChat.ts`, `backend/app/services/go100/llm_registry_service.py`, `backend/app/services/go100/model_routing_service.py`, `backend/app/core/llm_cost_tracker.py`, `backend/app/services/go100/ai/agent_core.py`, `backend/app/services/go100/ai/agent_tools.py`
- **내용**: Codex CLI에서 확인되지 않는 `gpt-5.5-pro`를 프론트 선택 목록, 하드코딩 fallback 목록, DB seed, 비용 테이블에서 제거. 운영 DB의 `go100_llm_models`에서도 `gpt-5.5-pro`를 `is_active=false`, `is_selectable=false`, `is_executable=false`로 비활성화.
- **Gemini 복구**: `agent_tools.py`의 `l2_desk_hint` JSON schema list type을 string으로 수정하고, `agent_core._get_tool_declarations()`에 Gemini function declaration schema 정규화를 추가해 `function_declarations.*.parameters.properties.*.type` 오류를 방지.
- **Codex UX 복구**: Codex CLI가 첫 이벤트 없이 장시간 대기할 때 화면이 멈춰 보이지 않도록 `GO100_CODEX_FIRST_EVENT_TIMEOUT` 기본 25초를 추가. timeout 발생 시 다음 fallback 모델로 즉시 전환.
- **Fallback 순서**: GPT/Codex 선택 실패 시 검증 완료된 `gemini-2.5-flash`를 1순위 fallback으로 변경해 사용자 응답성을 우선 확보.
- **운영 가드**: Codex CLI/API가 헤더 단계에서 장기 대기하는 현상이 남아 있어, command-center에서 GPT 계열 override가 들어오면 현재는 `gemini-2.5-flash`로 즉시 우회한다. Codex 인증/쿼터 정상화 후 해제 대상.
- **검증**: Python `py_compile` 통과, `frontend/src/go100/hooks/useChat.ts` ESLint 통과, `npm run build` 성공. 기존 React Hook warning 4건은 기존 파일 경고.
- **운영 주의**: 라이브 적용에는 `go100` graceful reload와 `go100-frontend` 재시작 필요. 적용 후 `/api/go100/llm-registry/selectable-models`에서 `gpt-5.5-pro` 미노출과 Gemini 스트림 응답을 확인할 것.

## 최근 완료 작업 (04/28 17:36 KST)

### 1. Command Center LLM 실행 경로 DB 키 우선 적용
- **파일**: `backend/app/services/go100/ai/agent_core.py`, `backend/app/core/oauth_loader.py`, `scripts/go100_relay_server.py`, `backend/app/services/go100/model_routing_service.py`
- **내용**: command-center가 직접 사용하는 Agent 실행 경로에 `go100_llm_api_keys` 우선 조회를 적용. Gemini/Google, LiteLLM, Claude OAuth, Claude CLI relay, Codex CLI relay가 DB 키를 먼저 보고 기존 env/current.env/root auth 파일로 폴백.
- **Codex CLI**: `codex` provider에 `CODEX_AUTH_JSON` 저장 시 임시 HOME의 `.codex/auth.json`으로 사용. 없으면 기존 `/root/.codex/auth.json`을 사용하고, 그마저 없으면 `codex/openai OPENAI_API_KEY`를 사용.
- **Claude CLI**: `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_AUTH_TOKEN_2`, `ANTHROPIC_API_KEY_FALLBACK` 순서로 DB 조회 후 기존 current.env 폴백. `ANTHROPIC_API_KEY` 직접 fallback은 제거.
- **검증**: Python 3.12 `py_compile` 통과. DB 실측 기준 현재 활성 키는 `google` 2개, `litellm` 1개, `openai` 1개이며 `anthropic`/`codex` 전용 키는 아직 미등록.
- **테스트 보정**: DB 기본 모델에 맞춰 fallback 모델 상수도 `codex`/`litellm` provider와 `deepseek-reasoner`까지 동기화.
- **운영 주의**: 코드 반영은 완료. 라이브 적용에는 `go100`와 `go100-relay` reload/restart가 필요하며 GO100 운영 규칙상 CEO 승인 후 실행.

## 최근 완료 작업 (04/28 16:45 KST)

### 1. GO100 LLM API 키/모델 DB 레지스트리 + 어드민 노출
- **파일**: `backend/app/services/go100/llm_registry_service.py`, `backend/app/routers/go100/llm_registry_router.py`, `backend/app/migrations/028_go100_llm_registry.sql`, `backend/app/core/llm_gateway.py`, `backend/app/services/go100/model_routing_service.py`, `backend/app/routers/go100/ai_router.py`, `frontend/src/app/(protected)/admin/llm-registry/page.tsx`, `frontend/src/go100/hooks/useChat.ts`, `frontend/src/go100/components/command-center/ChatArea.tsx`, `frontend/src/go100/components/command-center/SettingsTab.tsx`, `frontend/src/components/admin/AdminSidebar.tsx`, `frontend/src/lib/api/admin.ts`
- **내용**: `go100_llm_api_keys`, `go100_llm_models`, `go100_llm_key_audit_logs` 테이블 추가. API 키는 기존 `CryptoService`로 암호화 저장하고 어드민에는 마스킹만 노출. 모델은 DB에서 selectable/executable/display_order를 관리하며 command-center 모델 선택과 모델 라우팅 목록이 DB 레지스트리를 우선 사용.
- **Gateway**: `LLMGateway.initialize()`가 DB 키를 우선 조회하고 키가 없으면 기존 env로 폴백. Anthropic은 `ANTHROPIC_AUTH_TOKEN` → `ANTHROPIC_API_KEY_FALLBACK` → 기존 OAuth loader 순서를 유지.
- **DB 반영**: 기본 모델 15개 seed 완료. 운영 env에서 `GOOGLE_AI_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`, `LITELLM_MASTER_KEY` 4개를 평문 출력 없이 암호화 이관. 현재 활성 키 4개, 모델 15개, 선택 노출 15개.
- **검증**: Python 3.12 `py_compile` 통과, FastAPI route import 확인, 변경 파일 ESLint 통과, `npm run build` 성공. 기존 React Hook warning 4건은 기존 파일 경고.
- **운영 주의**: 라이브 적용에는 `go100`/`go100-frontend` 재시작 또는 무중단 배포 필요. 키 값 자체는 DB에 암호화되며 응답/로그에는 평문 미노출.

## 2026-04-29 13:12 KST - GO100 서비스 전 내부 한도 무제한 전환
- `backend/app/core/rate_limiter.py`: `GO100_UNLIMITED_MODE` 기본값을 enabled로 추가해 `/api/go100/*`, GO100 화면이 쓰는 `/api/v1/auth/*`, `/api/v1/llm/*`, 대시보드/알림/마켓/전략카드 경로의 내부 429를 우회. `/api/v4/kis/*`, `/api/v1/kis/*`는 계속 보호.
- `backend/app/core/llm_rate_limiter.py`: LLM 채널별 일일 사용 제한을 pre-launch unlimited mode에서 1,000,000,000으로 반환하고 사용량 증가를 no-op 처리.
- `backend/app/services/tier_limit_service.py`: FREE/PRO/PREMIUM 모두 계좌/카드 수 제한 없음, 실거래 허용으로 임시 전환.
- 운영 조치: Redis `rate_limit:*`, `rl:api:*` 기존 카운터 삭제. `go100_llm_api_keys.rate_limited_until`은 실측상 현재 대상 0건.
- 복구 방법: 정식 오픈 시 `GO100_UNLIMITED_MODE=false`로 배포하거나 티어 설정을 정책값으로 되돌릴 것.

## 최근 완료 작업 (04/28 14:31 KST)

### 1. Command Center Claude CLI / Codex CLI 최신 모델 우선 반영
- **파일**: `frontend/src/go100/hooks/useChat.ts`, `backend/app/routers/go100/ai_router.py`, `backend/app/services/go100/model_routing_service.py`, `backend/app/services/go100/ai/agent_core.py`, `backend/app/services/go100/ai/ai_client.py`, `scripts/go100_relay_server.py`
- **내용**: Command Center 모델 선택/허용 목록에 `claude-opus-4-7`, `gpt-5.5`, `gpt-5.5-pro`, `gpt-5.4-nano` 추가. Codex CLI 기본값을 `gpt-5.5`로 상향하고, `gpt-5` alias도 `gpt-5.5`로 매핑. Claude `claude-opus` alias는 `claude-opus-4-7`로 매핑.
- **표시 보강**: Codex Relay `done` 이벤트에 실제 실행 모델을 포함해 프론트가 완료 시 실행 모델 라벨을 재확인할 수 있게 함.
- **검증**: 서버 Python 3.12.3/가상환경 `py_compile` 통과, `frontend` ESLint 단일 파일 검사 통과, `pytest -q backend/tests/test_model_routing.py tests/unit/test_ai_router.py` 17개 통과, `git diff --check` 통과.
- **운영 주의**: 코드 반영만 완료. 라이브 적용에는 `go100`, `go100-frontend`, `go100-relay` 재시작이 필요하며 재시작 전 CEO 승인이 필요함.

## 최근 완료 작업 (04/21 15:00~16:00 KST)

### 1. Command Center 대화 삭제 API (runner-b3697b3a)
- **커밋**: `60ed1ef8`
- **파일**: `backend/app/routers/go100/chat_router.py`
- **내용**: `DELETE /api/go100/chat/sessions/{session_id}` 엔드포인트 추가
- **검증**: 인증(get_current_user) + 소유권(get_effective_uid) + 404 처리 확인

### 2. PC3 Sidebar/MetricCard/Grid 개선 (runner-ac99f3fe)
- **커밋**: `11af0b72` (runner-4940eb4e 커밋에 흡수)
- **파일**: Go100Sidebar.tsx, MetricCard.tsx, DashboardPage.tsx
- **내용**: Sidebar `lg:w-64`, MetricCard `lg:text-3xl font-bold`, Grid `lg:gap-6`

### 3. P1: KIS 잔고조회 TR_ID 실전/모의 분기 (runner-6d393ab9)
- **커밋**: `ca2b89e1`
- **파일**: `backend/app/services/go100/kis_order_gateway.py`
- **내용**: `get_account_balance`에서 is_production 기반 TR_ID/URL/토큰 자동 분기
- **버그 수정**: 실전 URL + 모의 TR_ID 조합으로 "실전투자 TR이 아닙니다" 에러 발생 → 해결

### 4. P0: 시스템 프롬프트 사용자 정보 주입 + get_my_info 도구 (runner-3fcbe276)
- **커밋**: `4b61fb89` (runner-4940eb4e 커밋에 흡수)
- **파일**: prompts.py, agent_core.py, agent_tools.py, tool_executors.py
- **내용**: 백억이 시스템 프롬프트에 로그인 사용자의 이름/등급/계좌 정보 자동 주입, `get_my_info` 도구 추가

## 이전 deploy_timeout 실패 → 재투입 이력
| 실패 Job | 재투입 Job | 사유 |
|----------|-----------|------|
| runner-146bbdb2 | runner-b3697b3a | deploy_timeout → 코드 미반영 확인 후 재투입 |
| runner-e5ef922e | runner-ac99f3fe | deploy_timeout → revert(c369fcc0) 확인 후 재투입 |

## 현재 서비스 상태
- go100 (백엔드 8002): ✅ active
- go100-frontend (프론트 3000): ✅ active
- Git: local = origin/main (푸시 완료, ahead/behind 0)

## 미완료 / 후속 작업
- [ ] 백억이 "내 계좌현황" / "내 정보 알려줘" 실측 테스트 (P0+P1 배포 후 검증)
- [ ] `get_my_info` 도구가 실제 AI 응답에서 정상 작동하는지 E2E 확인
- [ ] runner-4940eb4e (KIS 레거시 엔진 중지) — 별도 세션에서 처리 필요

## GitHub 브라우저 경로
- https://github.com/moongoby-GO100/kis-autotrade-v4/commits/main

## 2026-04-28 18:28 KST - GO100 Codex CLI relay auth hotfix
- Applied systemd drop-in: `/etc/systemd/system/go100-relay.service.d/env.conf` with `EnvironmentFile=/root/kis-autotrade-v4/.env` so relay can decrypt GO100 LLM registry keys.
- Updated `scripts/go100_relay_server.py` so DB-backed OpenAI API keys create a temporary `codex login --with-api-key` cache before `codex exec`.
- Verified `go100` health OK and `go100-relay` health OK after restart.
- 2026-04-29 08:20 KST 후속 수정: 터미널 기준 `gpt-5.5-pro`는 유효 모델이 아닌 것으로 확인되어 선택/실행 목록에서 제거됨.
- 2026-04-29 08:55 KST 후속 고정: `GO100_ALLOWED_MODEL_OVERRIDES`에서 `gpt-5.5-pro` 잔여 허용값 제거. DB `go100_llm_models`에서도 `is_active/is_selectable/is_executable=false`로 고정.
- 2026-04-29 09:05 KST 응답성 보강: Gemini SDK 호출에 `GO100_GEMINI_REQUEST_TIMEOUT` 기본 30초 제한을 추가해, LLM 지연 시 gunicorn worker timeout/SIGABRT로 번지지 않도록 방어.
- 2026-04-29 09:05 KST stale override 방어: 브라우저/직접 호출에서 비활성 모델 override가 들어오면 LLM 라우팅 전 즉시 SSE 안내+done으로 종료하도록 조기 차단.
## 2026-04-29 09:50 KST - GO100 GPT/Codex 선택 경로 복구 및 전체 인증 테스트
- `gpt-5.5-pro`는 터미널 모델 목록에 없어 비활성 상태 유지. 활성 GPT/Codex 테스트 대상은 `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.4-nano`, `gpt-5.3-codex`.
- `backend/app/routers/go100/ai_router.py`: GPT/Codex override를 `gemini-2.5-flash`로 강제 변환하던 임시 응답성 우회 제거. 활성 GPT/Codex 5개를 하드 허용 목록에도 추가해 DB 조회 실패 시 invalid override로 튕기지 않도록 고정.
- `scripts/go100_relay_server.py`: Codex CLI stdout 첫 이벤트 대기 300초를 첫 이벤트 25초/이후 180초로 변경해 Codex 인증 장애 시 command-center가 장시간 무응답에 빠지지 않게 보강.
- 실측 테스트: `moongoby@naver.com` 로그인 후 `/api/go100/ai/chat/stream`에서 GPT/Codex 5개 모두 HTTP 200, 3.3~7.7초 내 `gemini-2.5-flash` fallback으로 `OK` 응답 확인.
- 남은 차단 원인: Codex CLI 직접 실행에서 ChatGPT OAuth `refresh_token_reused`/`token_expired`/WebSocket 401 확인. Codex 직접 성공 복구에는 `/root/.codex/auth.json` 재로그인 또는 어드민 `codex` 키 등록이 필요.

## 2026-04-29 10:14 KST - 선택 모델 fallback 금지 + Codex MCP 주입 복구
- `backend/app/routers/go100/ai_router.py`: command-center에서 사용자가 모델을 직접 선택하면 `fallback_models=[]`를 agent_core에 전달하도록 고정. 자동 라우팅은 기존 intent fallback 유지.
- `backend/app/services/go100/ai/agent_core.py`: `model_override` 경로는 다른 모델로 전환하지 않고 동일 모델만 기본 2회 재시도(`GO100_SELECTED_MODEL_RETRY_ATTEMPTS`)하도록 변경. 자동/provider 기본 경로의 fallback은 유지.
- `scripts/go100_relay_server.py`: 설치된 `codex-cli 0.125.0`에 없는 `codex exec --mcp-config` 사용을 제거하고, 임시 `CODEX_HOME/.codex/config.toml`에 `mcp_servers.go100-tools`를 생성해 Codex CLI도 GO100 MCP 도구를 로드하도록 변경.
- Claude CLI는 기존 `--mcp-config scripts/go100_mcp_config.json --allowedTools mcp__go100-tools__*` 경로 유지. Gemini/Anthropic SDK/LiteLLM 경로는 동일 `AGENT_TOOLS`/`execute_tool` 도구 레지스트리를 계속 사용.

## 2026-04-29 11:48 KST - Claude Sonnet 4.6 / Opus 4.7 선택 모델 반영
- 원인: `backend/app/services/go100/llm_registry_service.py`의 기본 모델 seed에서 `claude-sonnet-4-6`, `claude-opus-4-7`이 `is_selectable=false`, `is_executable=false`, `disabled_actual_model_mismatch`로 고정되어 command-center 선택 목록에서 빠짐.
- 조치: 두 모델을 `is_selectable=true`, `is_executable=true`, `verification_status=enabled_cli_model_verified`로 변경하고, seed UPSERT가 기존 DB row의 `is_active`도 갱신하도록 보강.
- DB 조치: `go100_llm_models`에서 두 모델을 `is_active/is_selectable/is_executable=true`, `supports_tools/supports_coding=true`로 갱신. `claude-opus-4-6`은 비활성 유지.
- 표기 버그 수정: Claude CLI result의 `modelUsage`에 Haiku와 선택 모델이 함께 올 때 첫 key(Haiku)를 완료 모델로 표시하던 문제를 고쳐, 요청 모델을 우선 완료 이벤트에 표시.
- 검증: `moongoby@naver.com` 로그인 후 `/api/go100/ai/chat/stream`에서 `claude-sonnet-4-6`, `claude-opus-4-7` 각각 HTTP 200, `OK` 응답, `done.model` 선택 모델 일치 확인.

## 2026-04-30 16:23 KST - KIS stock screener API route restored for GO100
- Restored backend registration for existing router `backend/app/routers/v4_stock_screener.py` in `backend/app/main.py`.
- Verified `/stock-search.html` page returns HTTP 200 on frontend and public GO100 domain.
- Verified `/api/v4/stock-screener/meta` and `/api/v4/stock-screener/search` return HTTP 200 after `go100` restart.
- Impact: route registration only; no KIS order/trading logic changed.

## 2026-05-04 08:48 KST - GO100 chat bubble Markdown rendering hotfix
- Files: `frontend/src/go100/components/command-center/ChatArea.tsx`, `ChatMessage.tsx`, `chat-area.css`.
- Cause: assistant messages containing inline tokens such as `[종목:005930]` were passed as `children` from `ChatArea`, bypassing `ReactMarkdown` and rendering long GO100 reports as plain inline text.
- Fix: token-only assistant messages keep the inline-card parser, but assistant messages with Markdown structure now always use `ReactMarkdown`. Added h1/ordered-list/blockquote/paragraph/inline-code styling for report bubbles.
- Deploy: `scripts/deploy_frontend_only.sh` completed staging build, swap, and `go100-frontend` restart successfully at 08:48:09 KST.
- Verification: `go100` and `go100-frontend` active; `/auth/login` local HTTP 200 in 0.030s and public `https://go100.newtalk.kr/auth/login` HTTP 200 in 0.079s; `.next/BUILD_ID` and `prerender-manifest.json` present.

## 2026-05-04 19:50 KST - GO100 screener/chart frontend upgrade
- Files: `frontend/src/go100/pages/ScreenerPage.tsx`, `frontend/src/go100/pages/CompanyAnalysisPage.tsx`, `frontend/src/app/(protected)/stock/[code]/page.tsx`.
- Screener: added saved-condition presets, last-search restore, result detail modal, and explicit chart action while preserving advanced presets/date range/exclude filters/CSV/strategy-card mode.
- Chart: upgraded company chart tab to daily/weekly/minute frames, interval selector, indicator set selector, MA/RSI/Bollinger support, investor flow, fundamentals, spread summary, trade/signal overlays. `/stock/[code]` now routes into `/go100/company?code=...&tab=chart`.
- Verification: `pnpm lint` passed; `pnpm build` passed with existing unrelated lint warnings in `ai/hypothesis`, `SettingsRiskSection`, and `StrategyCardDetail`.
- Deploy: source and build artifact updated in working tree only; blue/green restart or push requires CEO approval.
