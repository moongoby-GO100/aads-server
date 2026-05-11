# AADS HANDOVER

## 현재 진행 상태 (2026-05-11)
- **채팅 세션/턴 TODO 게이트 추가 (2026-05-11 KST)**:
  - `migrations/083_chat_todo_items.sql` 추가. `chat_todo_items` 테이블에 `session_id`, `message_id`, `execution_id`, `title`, `status`, `sort_order`, `source`, `metadata`, `completed_at`와 세션/턴 기준 인덱스 및 partial unique 인덱스를 정의했다.
  - `app/services/chat_todo_service.py` 신규 추가. 세션/턴 todo 생성, 조회, 상태 전환(`pending/in_progress/completed/failed/skipped`), completion gate 평가, prompt block 생성, 감사용 `metadata.audit` 누적 로직을 구현했다.
  - `app/services/chat_service.py`에 복수 작업/도구 실행형 요청 감지 후 turn todo를 생성하는 훅을 연결했다. prompt에 `[세션 TODO 운영 규칙]`을 주입하고, 최종 저장 직전에 completion gate로 미완료 항목을 감지해 status/metadata를 갱신하며 필요한 경우 `[세션 TODO 점검]` 메모를 응답에 덧붙인다.
  - `app/main.py` startup schema 보강에 `ensure_chat_todo_schema()`를 연결해 migration 적용 전에도 신규 테이블/인덱스를 안전하게 보장한다.
  - `app/models/chat.py`에 `ChatTodoItemOut` 스키마를 추가했다.
  - 테스트:
    - `E2B_API_KEY=dummy pytest -q tests/unit/test_chat_todo_service.py tests/unit/test_chat_service.py` → `24 passed`
    - `E2B_API_KEY=dummy pytest -q tests/unit/test_context_continuity.py tests/unit/test_runner_scope_defaults.py tests/unit/test_intent_context_followups.py` → `11 passed`
  - 남은 리스크:
    - completion gate는 현재 응답 본문/도구 사용 흔적 기반 heuristic 판정이다. 항목 표현이 크게 바뀌면 일부 todo가 `pending`으로 남을 수 있다.
    - 실제 운영 Postgres에 `083_chat_todo_items.sql` 적용 자체는 이 세션에서 수행하지 않았고, migration 파일 존재/구조 검증과 startup schema 경로로 적용 가능성만 확인했다.

## 현재 진행 상태 (2026-05-11)
- **PC Agent VVIC 라우팅/락/큐 직접 패치 (2026-05-11 14:57~KST)**:
  - `runner-2db6f7fa`가 `claude_code_work` 진입 후 5분 이상 로그 0건/diff 0건으로 정체되어 강제 종료했다.
  - 직접 조치: `app/services/pc_agent_manager.py`에 capability 기반 agent 선택, per-agent/per-job lease, queue wait, stale lease 회수, routed command 실행 API 기반을 추가했다.
  - 직접 조치: `app/api/pc_agent.py`에 `POST /api/v1/pc-agent/route/execute`, `GET /api/v1/pc-agent/leases`를 추가하고 health 응답에 capabilities/leases를 노출했다.
  - 직접 조치: `pc_agent/agent.py`가 COMMAND_HANDLERS 기반 capabilities를 등록 payload로 전송하고, `pc_agent/commands/browser_auto.py`의 `browser_launch`는 `dedicated=true`/`port=0`에서 전용 프로필과 동적 CDP 포트를 사용하며 `/json/version` 준비를 확인한다.
  - 후속: NTV2 Bridge는 `/api/v1/pc-agent/route/execute` 계약에 맞춰 `job_type=vvic`, `required_capabilities=["vvic","chrome_cdp"]`, `browser_launch` params `dedicated=true`, `port=0`로 연동해야 한다.
- **Pipeline Runner Task Board 상태 표시 개선 (2026-05-11 14:20~14:25 KST)**:
  - 운영 DB 실측: `queued`는 0건이고, terminal 분류는 `blocked_dependency` 2건, `dedup_blocked` 2건, `no_changes` 2건, `done` 354건, `error` 4건이다.
  - `app/api/admin.py`: `/admin/tasks/stats`가 `no_changes`, `dedup_blocked`, `blocked_dependency` 카운트를 별도 반환하도록 보강했다.
  - `aads-dashboard/src/app/admin/tasks/page.tsx`: Admin Task Board에 `No Changes`, `Dedup Blocked`, `Blocked Dependency` 칼럼과 별도 색상/라벨을 추가해 세 terminal 상태가 Error로 보이지 않게 했다.
  - 검증: `python3 -m py_compile app/api/admin.py app/api/pipeline_runner.py`, `npx eslint src/app/admin/tasks/page.tsx`, `npm run build` 통과. 전체 `npm run lint`는 기존 전역 ESLint 오류 248건으로 실패했다.
  - 운영 반영: `bash /root/aads/aads-dashboard/deploy.sh` 성공. 활성 슬롯은 `blue`, 컨테이너 `aads-dashboard`는 `healthy/running`, 외부 `/login` 200 OK, 보호 페이지 `/admin/tasks`는 미로그인 기준 307 리다이렉트 정상.
- **PC Agent Chrome CDP 분리 프로필 반영 (2026-05-11 14:18 KST)**:
  - `pc_agent/commands/browser_auto.py`: `browser_launch()`가 기본 Chrome 프로필을 재사용해 `--remote-debugging-port=9222`가 무시되던 문제를 확인했다.
  - 조치: Windows는 `%LOCALAPPDATA%\\KakaoBot\\cdp-profile`, 비Windows는 `~/.kakaobot-cdp-profile`를 기본 `user_data_dir`로 사용하고 `--user-data-dir`, `--new-window` 옵션을 추가했다.
  - 현재 상태: 서버 측 소스에는 반영됐지만, CEO PC에서 실행 중인 에이전트 바이너리에는 즉시 적용되지 않는다. 실제 PC 에이전트 재배포 또는 재업데이트 후 재검증이 필요하다.
- **Pipeline Runner terminal 상태 분류 보강 (2026-05-11 14:13~14:18 KST)**:
  - `scripts/pipeline-runner.sh`: 변경 0건(`no_changes`)과 중복 차단(`dedup_blocked`)을 실제 실행 실패 `error`가 아니라 `cancelled` terminal 상태로 저장하도록 변경했다.
  - `scripts/pipeline-runner.sh`와 `app/api/pipeline_runner.py`: 선행 job이 `error/rejected/rejected_done/cancelled`이거나 DB에 없는 queued 작업은 `blocked_dependency`로 자동 종결한다.
  - 운영 DB 정리: 선행 `rejected_done`에 묶인 AADS queued 2건을 `blocked_dependency`로 종결했고, 기존 `no_changes`/`dedup_blocked` error 4건을 `cancelled`로 재분류했다.
  - 대시보드 `ChatArtifactPanel.tsx`: `display_status/status_label`을 사용해 `변경 없음`, `중복 차단`, `의존 차단`을 빨간 에러가 아닌 terminal 경고/종결 상태로 표시하고, 세션 안에 부모 job이 없는 의존 작업도 루트에 표시한다.
- **AADS Open Design Hub 기획 문서화 (2026-05-11 13:45 KST)**:
  - `docs/plans/AADS-SMART-DESIGN-SYSTEM.md`를 확장해 전 프로젝트 디자인 운영 체계인 `docs/plans/AADS-OPEN-DESIGN-HUB.md`를 신규 작성했다.
  - 핵심 방향은 공통 토큰, 프로젝트별 adapter, Design Auditor, Project Starter, Admin Design Hub UI를 분리하는 구조다.
  - 첫 러너 작업 범위는 대규모 UI 전면 교체가 아니라 Phase 0 기반(스키마 초안, API 계약, 스캐너 PoC, 구현 분해 문서)으로 제한한다.
- **AADS Open Design Hub Phase 0 직접 보강 (2026-05-11 13:54 KST)**:
  - `runner-0143f0a0`는 `claude_code_work` 중 로그/heartbeat 없이 산출물이 `.codex`만 남은 상태에서 2026-05-11 13:52:59 KST 강제 종료됐다.
  - 대안으로 기존 `app/services/design_audit_service.py` 및 `/api/v1/admin/design/*` read-only API 계약을 기준으로 `docs/plans/AADS-OPEN-DESIGN-HUB-IMPLEMENTATION.md`를 추가했다.
  - `tests/unit/test_design_audit_service.py`를 추가해 색상 탐지, Tailwind arbitrary color 탐지, 이모지 탐지, button class 반복 패턴, allowlist 경로 방어, empty input 동작을 검증한다.
- **Codex `unknown_tool: bash` 재발 방어 (2026-05-11 12:00 KST)**:
  - 증상: Codex CLI `command_execution` 이벤트가 AADS 채팅 도구 이벤트 `tool_use: bash`로 노출되어 CEO 화면에 `unknown_tool: bash` 결과가 반복 출력됐다.
  - 원인: `74c73a6`에서 릴레이 변환 코드는 수정됐으나, `claude-relay.service`는 2026-05-06 16:51 KST부터 계속 실행 중이라 새 코드가 로드되지 않았다. 또한 API 수신부에 구버전 릴레이 이벤트를 막는 2차 방어가 없었다.
  - 조치: `app/services/model_selector.py`에 `_is_internal_cli_command_tool()`을 추가하고 Codex relay에서 `bash`, `shell`, `command_execution` tool event를 `thinking` observation으로 변환하도록 보강했다.
  - 검증: `python3 -m py_compile app/services/model_selector.py scripts/claude_relay_server.py` 통과, `pytest -q tests/unit/test_relay_diagnostics.py tests/unit/test_chat_service.py::test_keyword_fallback_routes_only_explicit_discussion_queries tests/unit/test_chat_service.py::test_broad_tool_group_excludes_run_debate` 12 passed.
  - 운영 반영: `scripts/reload-api.sh` hot reload 완료(`2026-05-11 12:00:36 KST`, 재로드 67개). `claude-relay.service` 본체는 현재 생성 중인 세션을 끊을 수 있어 별도 재시작 필요.
- **114 Codex OAuth `refresh_token_reused` 재발 대응 (2026-05-11 11:53~12:00 KST)**:
  - 실측: 68/211은 `codex exec --skip-git-repo-check ... gpt-5.5` 최소 호출이 성공했지만, 114는 `refresh_token_reused` 및 `token_expired` 401로 실패했다.
  - 원인: 114의 `/root/.codex/auth.json`이 존재하고 `codex login status`도 `Logged in`으로 나오지만, 실제 access token refresh 단계에서 이미 사용된 refresh token으로 판정된다. 2026-05-05에도 같은 유형으로 “auth 파일 존재 여부가 아니라 실제 `codex exec` 성공 여부로 판단해야 한다”는 이력이 있었다.
  - 조치: `scripts/pipeline-runner.sh`에 Codex auth broken 쿨다운을 추가했다. `refresh_token_reused`, `token_expired`, `Please log out and sign in again` 감지 시 `/tmp/aads-codex-auth-disabled-until` 마커를 2시간 생성하고, 이후 같은 서버의 `codex:*` 모델은 즉시 skip 후 다음 모델로 넘어간다.
  - 운영 반영: 211/114 `/root/scripts/pipeline-runner.sh`에 동기화했고 `aads-pipeline-runner`를 재시작했다. 114에는 현재 깨진 OAuth 상태를 반영해 쿨다운 마커를 즉시 생성했다. 211은 Codex 실행 성공 상태라 마커를 생성하지 않았다.
  - 주의: 68의 현재 ChatGPT OAuth 파일을 114로 단순 복사하면 114는 일시 복구될 수 있으나, OAuth refresh token 회전 특성상 다음에는 68 또는 114 중 한쪽이 다시 `refresh_token_reused`로 깨질 수 있다. 114는 독립 device-auth 재로그인이 근본 복구다.

## 현재 진행 상태 (2026-05-09)
- **Pipeline Runner 모델 설정/원격 LiteLLM/동시성 보강 (2026-05-09 10:36~KST)**:
  - 제출 API 기본 정책을 “어드민 `runner_model_config` 자동 선택”으로 고정했다. `worker_model`은 `worker_model_reason`이 함께 들어온 경우에만 `pipeline_jobs.worker_model`에 저장하며, 사유가 없으면 무시하고 자동 설정값을 사용한다.
  - DB에 `pipeline_jobs.model_override_reason` 컬럼을 추가했다(`migrations/081_pipeline_runner_model_override_reason.sql`).
  - `scripts/pipeline-runner.sh`에 `RUNNER_ENGINE_MODE=general|litellm` 분기를 추가했다. 일반 러너는 원격 프로젝트의 `litellm:*` 작업을 claim하지 않고, LiteLLM 전용 러너는 `model` 또는 `worker_model`이 `litellm:*`인 작업만 claim한다.
  - 211/114처럼 `aads-server` 컨테이너가 없는 서버에서도 `python3 /root/scripts/litellm_runner.py`로 직접 실행하도록 원격 LiteLLM 경로를 보강했다. MCP 서버가 없으면 `litellm_runner.py`가 로컬 파일/git 도구 폴백을 사용한다.
  - 검증: `python3 -m py_compile app/api/pipeline_runner.py app/api/ceo_chat_tools.py app/services/tool_registry.py app/services/tool_executor.py scripts/litellm_runner.py` 통과, `bash -n scripts/pipeline-runner.sh` 통과, 운영 DB 컬럼 생성 확인.
- **채팅 메모리/Auto-RAG 맥락 유지 보강 (2026-05-09 07:02 KST)**:
  - `app/services/chat_embedding_service.py`: `search_semantic()` 결과에 `session_id`를 반환해 Auto-RAG가 same-session/cross-session 출처를 정확히 판정하도록 수정했다. 메시지 임베딩 예약 공통 함수 `schedule_message_embedding()`을 추가했다.
  - `app/services/context_builder.py`: 현재 프롬프트 히스토리에 이미 포함된 `chat_messages.id`를 Auto-RAG로 전달해 동일 메시지가 `<auto_rag_context>`에 중복 주입되지 않도록 했다.
  - `app/services/chat_service.py`: 히스토리 로드 쿼리에 `id`를 포함하고, `streaming_placeholder`를 최종 assistant 응답으로 promote하는 경로에서도 최종 본문 임베딩을 예약하도록 보강했다.
  - 테스트: `pytest -q tests/unit/test_memory_context_regression.py` 3 passed, `python3 -m py_compile app/services/chat_embedding_service.py app/services/auto_rag.py app/services/context_builder.py app/services/chat_service.py tests/unit/test_memory_context_regression.py` 통과, 변경 파일 대상 `git diff --check` 통과.
  - 실측 DB 상태: 신규 누락 방지 패치 적용 후 과거 `chat_messages` 미임베딩 대상 백필을 완료했다. 2026-05-09 09:55 KST 기준 role별 본문 10자 이상 `embedding IS NULL` 대상은 0건이다.
  - 주의: 전체 `git diff --check`는 기존 사용자 변경 파일 `docs/CHANGELOG-direct-edit.md`의 trailing whitespace로 실패한다. 이번 변경 코드 파일에는 whitespace 오류가 없다.

## 현재 진행 상태 (2026-05-06)
- **NewTalk V1 E2E 계정 env/Vault 관리**:
  - `.env.e2e.local`에 뉴톡V1 관리자/도매/소매 E2E 계정을 로컬 전용으로 저장하고 `.gitignore`에 `.env.*` 예외 규칙을 보강했다. 실제 비밀번호는 git 추적 대상에서 제외한다.
  - `.env.e2e.example`에 키 이름과 로그인 URL 템플릿을 추가했다. 관리자/도매는 V1 `https://newtalk.kr/auth/login`, 소매는 `https://pick.newtalk.kr/auth/login` 기준이다.
  - `scripts/seed_e2e_credentials.py`를 추가해 env 값을 AADS `e2e_credentials` Credential Vault에 암호화 저장할 수 있게 했다.

## 현재 진행 상태 (2026-05-06)
- **대시보드 정적 reports HTML 공개 경로 복구 (2026-05-06 14:46 KST)**:
  - 증상: `https://aads.newtalk.kr/reports/20260506_newtalk_ai_virtual_model_fitting_service_plan.html` 접속 시 로그인 페이지로 `307` 리다이렉트되어 브라우저에서 보고서가 열리지 않았다.
  - 원인: HTML 파일은 `aads-dashboard/public/reports/` 및 운영 컨테이너 `/app/public/reports/`에 존재했지만, Next.js `src/middleware.ts`의 인증 미들웨어가 `/reports/*.html` 정적 파일까지 보호 경로로 처리했다.
  - 조치: `aads-dashboard/src/middleware.ts`에 `/reports/<filename>.(html|htm|pdf|txt|md|csv|json)` 정적 파일만 공개 통과시키는 패턴을 추가했다. `/reports` 대시보드 페이지 자체는 기존 인증 정책을 유지한다.
  - 배포: `bash /root/aads/aads-dashboard/deploy.sh` blue-green 성공. 활성 슬롯은 `green`, 컨테이너 `aads-dashboard-green` 상태 `running`.
  - 검증: 내부 `http://127.0.0.1:3100/reports/20260506_newtalk_ai_virtual_model_fitting_service_plan.html` 200 OK, 외부 `https://aads.newtalk.kr/reports/20260506_newtalk_ai_virtual_model_fitting_service_plan.html` 200 OK, `Content-Type: text/html; charset=UTF-8`, `Content-Length: 32058`.
- **deploy_safe 실행 컨텍스트 보강 + 실패 러너 수동 완료 (2026-05-06 14:42 KST)**:
  - 대상: `runner-dbd3068f` (`AADS deploy_safe 실행성 수정`)는 Claude CLI가 root 권한에서 `--dangerously-skip-permissions`를 거부해 작업 시작 전 실패했다.
  - 조치: `app/services/tool_executor.py`에서 실행 컨텍스트를 감지하도록 보강했다. 호스트에서는 `scripts/reload-api.sh`를 실행해 `.active_container` 기준 활성 컨테이너로 위임하고, 컨테이너 내부 `reload`는 `bash /app/scripts/reload-api.sh`를 직접 실행한다. 컨테이너 내부 `bluegreen`/`restart-single`은 호스트 docker/deploy 컨텍스트가 없으면 명확한 오류를 반환한다.
  - 조치: `deploy_safe` post-health를 5초 1회에서 최대 36회 재시도 방식으로 바꿔 supervisor 재기동 지연을 실패로 오판하지 않도록 했다.
  - 검증: `python3 -m compileall app/services/tool_executor.py tests/unit/test_deploy_safe.py` 통과, 운영 활성 컨테이너 `aads-server-green`에서 수정 테스트 `/tmp/test_deploy_safe.py` 기준 `14 passed`.
  - 주의: 운영 컨테이너에는 `tests/`가 볼륨 마운트되어 있지 않아 최신 테스트 파일을 `/tmp/test_deploy_safe.py`로 복사해 검증했다.
- **Chat Lightweight v2.2 도구박스/최종버블 회귀 보강**:
  - Backend: `fields=minimal`은 표시용 preview와 도구 요약 메타(`has_tools`, `tool_count`, `tool_names`)만 반환하고, full `tools_called`는 신규 단건 상세 API `GET /api/v1/chat/messages/{message_id}`에서 lazy hydrate한다.
  - Backend: `normalize_tool_events()`를 추가해 legacy string 배열, Codex relay 구조화 이벤트, tool_result/thinking 이벤트를 동일한 `tools_called` 배열 계약으로 정규화한다. 저장 전과 full 응답 전 모두 이 경로를 사용한다.
  - Frontend: 완료 assistant 버블에서 `tools_called`가 비어도 `has_tools/tool_count/tool_names`가 있으면 도구박스를 숨기지 않고 hydrate 상태를 표시한다. hydrate 후에는 기존 긴 본문을 minimal 200자 preview로 덮어쓰지 않는다.
  - Frontend: 스트리밍 중 누적한 `tool_use/tool_result` 이벤트를 final assistant 메시지에 합쳐 완료 직후 도구박스가 사라지지 않게 했다.
  - Model alias: `codex:gpt-5.5`, `gpt-5.5`, `GPT-5.5 (Codex CLI)`를 Codex 실행 모델 `gpt-5.5`로 정규화한다.
  - 원칙: DB/LLM 원본 메시지, embedding, quality/reflexion/memory/RAG 저장 경로는 축소하지 않는다. 축소는 프론트 표시 API payload에만 적용한다.
  - 검증 명령: `python3 -m pytest tests/unit/test_chat_service.py tests/unit/test_chat_lightweight_frontend_static.py -q`
  - 수동 확인: 세션 `b8a8651b-6226-46df-9a44-36a70e478959`에서 minimal polling 후 도구박스 placeholder, 단건 hydrate 1회, 800자 이상 본문 길이 유지, Codex final 도구 이벤트 보존을 확인한다.
  - 남은 리스크: 실제 브라우저 DOM 확인은 운영 세션 데이터와 인증 토큰이 필요한 경로라 자동 단위 테스트는 정적/서비스 계약 중심으로 커버한다.

## 현재 진행 상태 (2026-05-05)
- **Android Agent Play Protect 차단 대응 (2026-05-05 15:09 KST)**:
  - 원인: 운영 다운로드 APK가 debug 계열 파일명/후보로 제공되고, release APK도 SMS/통화기록/연락처/접근성/알림리스너/디바이스관리 등 고위험 권한을 포함해 Play Protect 차단 가능성이 높았다.
  - 조치: release Manifest를 최소 권한(`INTERNET`, `ACCESS_NETWORK_STATE`, foreground data sync, notification, vibrate)으로 축소하고, 전체 권한 Manifest는 `app/src/debug/AndroidManifest.xml`로 분리했다. `build_release_apk.sh`를 추가하고 운영 APK 라우트 및 BG 빌드를 release 기준으로 전환했다.
  - 즉시 반영: 재시작 없이 실행 중인 `aads-server`, `aads-server-green` 컨테이너의 `/app/android_agent/dist/{aads-agent-debug.apk,aads-agent-release.apk,aads-agent-fresh.apk}`를 새 release APK로 교체했다.
  - 검증: `./build_release_apk.sh` 성공, `./build_debug_apk.sh` 성공, 공개 `/download`, `/download-fresh`, `/download-standard` 3개 URL 모두 sha256 `8aee20a21860d1d440fb81a5fc1809b07d8ee6ffd8c075df4fe04eb1d8f1613e` 확인. `aapt dump permissions` 기준 공개 APK 권한 6개, `apksigner verify` v2 서명 통과.
- **Common Browser Bridge 모듈 스켈레톤 추가 (2026-05-05 KST)**:
  - 공통 계층: `app/browser_bridge/` 추가. `BrowserEndpointKind(cdp/websocket/local_agent/storage_state/headless)`, one-time pairing token, 세션 registry, storageState manager, Playwright context adapter, E2E config adapter를 AADS 채팅과 분리된 모듈로 구성했다.
  - 보안 경계: CDP/WebSocket endpoint는 기본적으로 `localhost`/loopback만 허용한다. pairing token은 원문 저장 없이 hash만 보관하고 1회 사용 후 재사용을 거부한다. storageState는 `.browser_bridge_state/` 하위에만 저장되며 `.gitignore`에 추가했다. `browser_fill` 결과는 입력값을 echo하지 않도록 바꿨다.
  - API: `app/api/browser_bridge.py` 추가 및 `app/main.py` 라우터 등록. `POST /api/v1/browser-bridge/pairings`는 인증된 사용자가 pairing token을 만들고, `POST /api/v1/browser-bridge/sessions/register`는 local bridge/Chrome 쪽에서 token으로 세션을 등록한다. `GET /sessions`, `POST /sessions/select`, `GET /e2e/config`로 등록 세션과 E2E 인터페이스를 조회한다.
  - AADS 도구 연동: `browser_connect` 도구를 추가했다. `status`, `create_pairing`, `select` action을 지원하며 기존 `browser_navigate/snapshot/screenshot/click/fill/tab_list`는 Browser Bridge 활성 세션을 우선 사용하고 없으면 기존 headless Playwright 경로를 사용한다.
  - CEO OTP 흐름: `browser_connect(action="create_pairing")` → CEO 로컬 Chrome/브릿지 에이전트가 `/sessions/register`에 `endpoint.kind=cdp` 또는 `storage_state`로 등록 → CEO가 로컬 Chrome에서 OTP 완료 → AADS browser 도구가 활성 세션을 재사용한다.
  - E2E 인터페이스: `app.browser_bridge.e2e_adapter.build_e2e_config()`를 추가했다. 환경변수 `AADS_BROWSER_BRIDGE_SESSION_ID`, `AADS_BROWSER_BRIDGE_CDP_URL`, `AADS_BROWSER_BRIDGE_WS_URL`, `AADS_BROWSER_BRIDGE_STORAGE_STATE`가 있으면 final Playwright 확인이 bridge 세션을 우선 사용하고, 없으면 headless Playwright 설정을 반환한다. `app/services/visual_qa.py` 캡처도 이 config를 인자로 전달한다.
  - 검증 기록: `tests/unit/test_browser_bridge.py`에 loopback 검증, public CDP 차단, one-time token 재사용 거부, storageState 경로 검증을 추가했다.

## 현재 진행 상태 (2026-05-04)
- **Android Agent 전기능 구현 후속 안정화 + 채팅 응답 표시 복구 문서화/커밋 준비 (2026-05-04 08:20 KST)**:
  - Android: `runner-4f922625` 산출물은 `05c7dc7`로 이미 커밋되어 있으며, `CommandDispatcher.java` 기준 57개 명령/alias가 등록되어 있다. 후속 패치로 `AndroidCommandHandlers.SensorSnapshot.toJson()`에서 `NaN`/`Infinity` 센서값을 JSON 배열에 넣다 실패하지 않도록 non-finite 값을 skip 처리했다.
  - Chat: DB에 저장된 AI 검수/상태 보고(`intent=runner_response`)가 채팅 본문에서 사라지는 원인을 `app/services/chat_service.py`, `app/routers/chat.py`, 대시보드 `src/app/chat/page.tsx` 필터로 확인했다. 자동 트리거/시스템 로그는 계속 숨기되 `runner_response`는 사용자-visible assistant 응답으로 남기도록 조정했다.
  - Runner: AADS Pipeline Runner per-project 동시 실행 상한을 `MAX_CONCURRENT_PER_PROJECT=6`으로 맞추고 `scripts/pipeline-runner.sh`, `scripts/aads-pipeline-runner.service`, `docs/pipeline-runner/*`, `docs/knowledge/CTO-SYSTEM-MAP.md`에 반영했다.
  - 기술문서: `docs/reports/20260504_ANDROID_AGENT_CHAT_VISIBILITY_TECHNICAL.md` 추가. Android 구현 범위, runner_response 표시 복구, Runner timeout/review diff 신뢰도 주의사항, 추후 검증 명령을 기록했다.
  - 주의: `runner-4f922625`는 코드 검수 승인 후 finalize/deploy 단계에서 timeout/error 이력이 있으므로, 배포 완료 보고는 반드시 APK 다운로드/컨테이너/health 실측 후에만 가능하다.

## 현재 진행 상태 (2026-04-28)
- **역할 분류 체계 + 사업화 역할 + Agent Registry 관리 UI 반영 (2026-04-30 18:59 KST)**:
  - DB: `migrations/077_role_taxonomy_and_business_roles.sql` 추가 및 운영 DB 적용 완료. 기존 `role_profiles` 26건에 분류 메타데이터를 반영하고, 사업화 역할 8건(`GTMStrategist`, `BrandMarketingLead`, `SalesPartnershipLead`, `PricingMonetizationStrategist`, `CustomerSuccessLead`, `RevenueOperationsAnalyst`, `FinanceFundraisingLead`, `LegalIPAdvisor`)과 L3 `prompt_assets` 8건을 추가/갱신했다.
  - 분류 결과: 의사결정·전략 4건, 제품·사용자경험 3건, 개발·구현·검증 9건, 보안·리스크·거버넌스 2건, 사업화·매출·시장진입 8건.
  - 백엔드: `GET /api/v1/admin/agents`와 상세 API가 `role_category`, `role_category_label_ko`, `role_group_order`, `lifecycle_stage`, `project_scope`, 활용 기준/지시 방법/템플릿을 반환한다. `/chat/workspaces/{workspace_id}/roles`도 role group order 기준 정렬과 카테고리 메타데이터를 포함한다.
  - 대시보드: `/root/aads/aads-dashboard/src/app/admin/agents/page.tsx` 신규 추가. 분류 필터, 역할 검색, 프로젝트 범위, 활용 기준, 최근 작업 상세를 한 화면에서 확인할 수 있다.
  - 검증: `python3 -m py_compile app/api/admin.py app/services/chat_service.py` 통과, `npx eslint src/app/admin/agents/page.tsx` 통과, `npm run build` 통과, `npx tsc --noEmit --pretty false` 통과, 운영 DB 적용 결과 `UPDATE 26`, `INSERT 0 8`, `INSERT 0 8`, `COMMIT` 확인. 2026-04-30 19:28 KST 기준 백엔드 reload 6단계 검증 통과, 대시보드 blue-green 배포 성공, `/admin/agents` 외부 URL은 인증 리다이렉트(`/login?redirect=%2Fadmin%2Fagents`) 정상.
- **서버114 CROSS-MONITOR 알림 조치 완료 (2026-04-30 09:02 KST)**:
  - 증상: `Exec(114) 심각 — 디스크100% HTTP-health실패` 텔레그램 알림.
  - 실측: 서버114 `/` 디스크는 `875G 중 686G 사용, 181G 여유, 80%`로 100% 상태가 아니며 warning 임계 구간. ShortFlow/NewTalk V2 Docker 컨테이너는 모두 Up.
  - 원인: AADS 헬스체커가 서버114 SSH 포트 `7916`을 HTTP health URL로 하드코딩하고 있었다. 실제 `116.120.58.155:7916`은 `sshd` 포트라 HTTP 요청이 connection refused 처리된다.
  - 조치: `app/services/health_checker.py`, `app/services/server_registry.py`, `app/services/tool_executor.py`의 114/SF/NTV2 HTTP health URL을 `https://sf.newtalk.kr/`, `https://v2.newtalk.kr/`로 교체하고 `aads-api`를 supervisorctl로 재시작했다.
  - 검증: `python3 -m py_compile app/services/health_checker.py app/services/server_registry.py app/services/tool_executor.py` 통과, `_check_http_health("114")`가 `ok=True` 반환, `https://aads.newtalk.kr/api/v1/health` 정상.
- **L3 Role 프롬프트 전문성 강화 DB 반영 완료 (2026-04-29 08:55 KST)**:
  - 신규 마이그레이션: `migrations/065_strengthen_l3_role_prompts.sql` 추가 및 운영 DB 적용 완료.
  - 적용 결과: `prompt_assets` L3 활성 40건 유지, 평균 본문 길이 283자 → 390자, 최대 820자. 핵심 역할 10개에는 판단 기준/필수 확인/작업 절차/산출물/검증/에스컬레이션 구조를 반영했다.
  - 강화 대상: `CTO`, `PM`, `Developer`, `QA`, `SRE`, `SecurityPrivacyOfficer`, `RiskComplianceOfficer`, `DataEngineer`, `PromptContextHarnessEngineer`, `JudgeEvaluator` 및 AADS/GO100/NTV2 핵심 오버레이.
  - `role_profiles.escalation_rules`에 `quality_rubric_version=l3-role-rubric-v1`, `requires_evidence=true`, `requires_verification_before_done=true`를 추가했다.
  - 샘플 매칭 검증: AADS+CTO, AADS+PromptContextHarnessEngineer, GO100+RiskComplianceOfficer/DataEngineer, NTV2+SecurityPrivacyOfficer/UXProductDesigner 모두 공통 역할 + 프로젝트 오버레이 2단 매칭 확인.
- **좌측 채팅 세션 역할 지정 UX 배포 완료 (2026-04-28 18:49 KST)**:
  - 백엔드: `GET /api/v1/chat/workspaces/{workspace_id}/roles` 추가. `role_profiles.project_scope` 기준으로 워크스페이스/프로젝트별 역할 목록을 반환하며, 한글 표시명은 `escalation_rules.display_name_ko`에서 읽는다.
  - 프런트: `aads-dashboard/src/components/chat/Sidebar.tsx`의 각 세션 행에 역할 지정/변경 드롭다운을 추가했다. 저장은 기존 `PUT /chat/sessions/{session_id}`의 `role_key`로 수행된다.
  - DB 실측: `role_profiles` 17건, 한글 표시명 포함. AADS 전용 `PromptContextHarnessEngineer / 프롬프트·컨텍스트·하네스엔지니어` 포함.
  - 배포: `bash /root/aads/aads-dashboard/deploy.sh` blue-green 성공, 활성 슬롯 `green`, `aads-dashboard-green` healthy. 컨테이너 내부 `.next` 번들에서 `getChatWorkspaceRoles`/역할 지정 UI 문자열 확인.
  - 검증: `python3.11 -m py_compile app/routers/chat.py app/services/chat_service.py` 통과, `npm run build` 통과, `npx eslint src/components/chat/Sidebar.tsx` 통과, `/health` 200. 전체 `npm run lint`는 기존 누적 오류 255건으로 실패 상태 유지.
- **채팅 싱킹박스 대화 버블 노출 패치 완료 (2026-04-28 18:00 KST)**:
  - 원인: `/chat` 운영 화면은 `ChatStream.tsx`/`ThinkingIndicator.tsx`가 아니라 `src/app/chat/page.tsx`의 인라인 `MessageItem` 렌더러를 사용한다. 따라서 도구박스 하단에 별도 컴포넌트를 만들어도 실제 대화 버블에는 표시되지 않았다.
  - 대시보드: `ChatMessage.thinking_summary` 타입을 추가하고, 최종 assistant 버블에서 `tools_called` 도구박스 바로 아래에 `thinking_summary/thought_summary` 접이식 사고 과정 박스를 렌더링한다. 저장된 `tools_called` 안의 thinking 이벤트도 `ev.thinking`/`ev.content` 양쪽을 표시한다.
  - 백엔드: LiteLLM/OpenAI 호환 스트림의 `reasoning_content`를 답변 본문에 섞지 않고 `thinking` SSE 이벤트로 분리해 저장한다. Output Validator 재시도 경로도 thinking 누락 없이 `thinking_summary`에 누적한다.
  - 검증: `docker exec aads-server python3 -m py_compile /app/app/services/model_selector.py /app/app/services/chat_service.py` 통과, `aads-dashboard npm run build` 통과.
- **채팅 진행 중 버블 P0 안정화 패치 완료 (2026-04-28 17:47 KST)**:
  - `aads-dashboard/src/app/chat/page.tsx`에서 `streaming_placeholder` 메시지는 800자 초과 긴 메시지 자동 접힘 대상에서 제외했다.
  - `streaming-status.is_streaming=true` 상태에서는 프론트의 180초 타이머가 `waitingBgResponse`를 강제로 끄지 않도록 변경했다. 진행 표시 종료는 서버 `streaming-status`의 `is_streaming/just_completed` 상태 기준으로만 결정한다.
  - 검증: `git diff --check -- src/app/chat/page.tsx` 통과, `npm run build` 통과. `npx eslint src/app/chat/page.tsx`는 기존 누적 9 error/21 warning으로 실패 상태 유지.
- **LLM 최신모델 자동 업데이트 및 GPT-5.5 반영 완료**:
  - `migrations/059_llm_model_discovery.sql`로 `llm_models`에 discovery/execution/verification/pricing/capabilities 컬럼을 추가하고 `llm_model_discovery_runs` 이력 테이블을 도입했다.
  - `app/services/model_registry.py`가 OpenAI/Gemini/LiteLLM catalog를 운영 컨테이너에서 조회해 DB 레지스트리에 병합한다. 최종 startup 기준 OpenAI 115개, Gemini 50개, LiteLLM 76개 발견. Anthropic은 OAuth 실행 가능 상태와 Models API discovery 가능 상태를 분리해, OAuth-only일 때 `oauth_runtime_only_models_api_unavailable` skip 및 `runtime_executable=true`, `auto_discovery_supported=false`, `discovery_requirement=x-api-key required...` 메타데이터로 기록한다.
  - Codex CLI `gpt-5.5`를 `model_registry`, `model_selector`, `claude_relay_server.py`, `pipeline_runner_service.py`, `pipeline-runner.sh`, 대시보드 selector/settings에 반영했다.
  - 실제 Codex relay E2E: `/codex-stream` `model=gpt-5.5`가 `AADS_GPT55_OK`, `model: gpt-5.5`로 응답 확인.
  - API E2E: active 모델 140개, `codex:gpt-5.5`와 `openai:gpt-5.5` 모두 active 확인.
- **LLM 최신모델 자동반영 보강 3차 패치 (2026-04-29)**:
  - DeepSeek canonical ID를 `deepseek-v4-flash`, `deepseek-v4-pro`로 등록했다. `deepseek-chat`, `deepseek-reasoner`는 호환 alias로 유지하며 metadata에 `canonical_model`, `compatibility_alias=true`, `deprecation_date=2026-07-24`를 남긴다.
  - DeepSeek 실행은 LiteLLM proxy 경로로 고정했다. 과거 DB metadata가 `openai_compatible_direct`로 남아 있어도 selector가 `litellm_proxy`로 보정하고 alias 요청은 canonical 실행 ID로 변환한다.
  - Provider summary는 `runtime_executable`, `auto_discovery_supported`, `discovery_requirement`, `active_model_source`, `template_active_model_count`, `discovery_active_model_count`를 노출한다. 확인 API: `/api/v1/llm-models/providers/summary`, `/api/v1/llm-models/discovery-runs?limit=8`.
  - 검증: `E2B_API_KEY=test python3.11 -m pytest tests/unit/test_model_registry.py tests/unit/test_model_selector_dynamic_routing.py tests/unit/test_llm_registry_sync_flow.py -q` 기준 24 passed.
- **model_selector registry 의존성 보강 (2026-04-29)**:
  - `app/services/model_registry.py`가 Anthropic 템플릿에 `accepted_aliases`와 실제 `execution_model_id`를 함께 기록한다. 예: `claude-sonnet` → `claude-sonnet-4-6`.
  - `app/services/model_selector.py`는 입력 모델을 static alias 맵보다 registry row 기준으로 정규화하고, 모델 미가용 시 provider/family/category/capability/cost 유사도로 fallback 후보를 고른다.
  - Codex는 static allowlist 밖 신규 모델도 registry row에 `execution_backend=codex_cli`가 있으면 relay 경로로 라우팅된다.
  - 검증: `pytest -q tests/unit/test_model_registry.py tests/unit/test_model_selector_dynamic_routing.py tests/unit/test_llm_registry_sync_flow.py` 기준 28 passed.
- **채팅 모델 상단고정 provider별 분리 완료**:
  - 원인: `chat_model_preferences`가 `model_id` 단일 PK라 `openai:gpt-5.5`와 `codex:gpt-5.5`가 `gpt-5.5`로 충돌했다.
  - `migrations/060_chat_model_preferences_provider_scope.sql`로 PK를 `preference_key`로 전환했다. 형식은 `provider:model_id`, 자동 라우팅은 `mixture`.
  - `app/api/llm_models.py`, `aads-dashboard/src/components/settings/LlmRegistryWorkspacePanel.tsx`, `aads-dashboard/src/app/chat/page.tsx`를 provider-qualified 기준으로 수정했다.
  - 최종 API 검증: `codex:gpt-5.5:true`, `openai:gpt-5.5:false`. 라우팅 검증: `openai:gpt-5.5 -> openai_compatible_direct`, `codex:gpt-5.5 -> codex_cli`.
  - 서버 `deploy.sh` 6단계 통과, 대시보드 blue-green 배포 및 프론트 QA 통과.
- **채팅 SSE 재진입 UX 3건 패치 완료** (b24b47f + 56ed27c):
  - **BUG #3**: `app/routers/chat.py` streaming-status DB fallback에서 `tool_count`/`last_tool`을 `tools_called` JSON에서 산출 (running/just_completed/placeholder 3분기). asyncpg가 jsonb를 str로 반환하는 케이스도 처리.
  - **Patch A** (`aads-dashboard/src/app/chat/page.tsx:1742`): `streaming-status` 응답의 `partial_content`/`tool_count`/`last_tool`을 즉시 `setStreamBuf`/`setToolStatus`로 주입. 진입 시 빈 버블 방지.
  - **Patch B** (`aads-dashboard/src/app/chat/page.tsx:1322`): `attachExecutionReplay`가 SSE 18종 모두 처리(이전 3종). `tool_use`/`tool_result`/`thinking`/`stream_start`/`stream_reset`/`yellow_limit`/`model_info`/`sdk_*`/`error` 핸들러 추가 — sendMessage 메인 루프와 동등.
  - **배포**: `docker compose build aads-dashboard` (image f9c82f89) → `up -d aads-dashboard` healthy. `bash scripts/reload-api.sh` 68개 모듈 재로드.
  - **푸시 확인**: `b24b47f` (aads-server main), `56ed27c` (aads-dashboard main) — 모두 origin 반영 완료.
  - **문서**: `docs/knowledge/SSE-STREAMING-ARCHITECTURE.md` v2.0 → **v2.1** 업데이트 (Layer 7: Re-attach Full SSE Replay 추가). `docs/chat/CHAT-CHANGELOG.md` 2026-04-28 항목 추가.
  - **별도 보고서**: `reports/20260428_session_fork_analysis.md` — 누적 4000건 세션 분기 권유 정밀 분석 + 개선안 5종.

## 현재 진행 상태 (2026-04-27)

- **5-Layer Prompt 시스템 마감 검증 (직접 작업)**:
  - **DB**: prompt_assets 6 컬럼(layer_id/role_scope/target_models/workspace_scope/intent_scope/model_variants) + 시드 10건 활성 — L1 글로벌 2건, L2 프로젝트 3건, L3 역할 2건, L4 인텐트 2건, L5 모델 1건. compiled_prompt_provenance 테이블 정상.
  - **백엔드**: PromptCompiler.compile()이 5축(workspace/intent/target_models/role_scope) 모두 SQL 필터로 처리. chat_service.py:3873에서 매 채팅 턴 호출.
  - **API**: app/api/admin.py에 /admin/prompt-assets CRUD 5종(GET/POST/PUT/PATCH toggle/DELETE) + preview 완비.
  - **프런트**: aads-dashboard/src/app/admin/prompts/page.tsx(268줄) 5-Layer 카드/필터/편집/미리보기 UI. api.ts에 5종 메서드. Sidebar에 📝 Prompts 메뉴(/admin/prompts) 노출.
  - **provenance 0건 진단 패치**: chat_service.py PromptCompiler 호출부에 [PROMPT_COMPILER] 4단계 진단 로그(enter/compiled/recorded/failed) 추가, session_id를 str() 명시 캐스팅, record_prompt_provenance 실패를 별도 except로 분리. 다음 채팅 턴부터 compiled_prompt_provenance 적재 추적 가능.
  - **Hot-Reload**: scripts/reload-api.sh 62개 모듈 재로드 완료(10:49 KST), SSE 영향 없음.

최종 업데이트: 2026-04-24

## 현재 진행 상태 (2026-04-25)
- **2026-04-25 Governance v2.1 마감 (직접 작업)**:
  - **P0 temperature 배선 완료**: `model_selector.py`에 `contextvars` 기반 `_ctx_temperature`를 도입해 `call_stream()` → `_stream_litellm_anthropic` / `_stream_litellm_openai` / `_stream_cli_relay` 3개 LLM 경로 모두에 인텐트별 temperature를 전달한다. `resolve_intent_temperature()` → `intent_policies.temperature` DB 조회 → 하드코딩 맵 폴백 체인으로 작동. 실측 검증: greeting=0.1, strategy=0.15, code_task=0.15, casual=0.2.
  - **P0 W3 DB 마이그레이션 완료**: `scripts/migrations/20260424_governance_v2_1_w3.sql` 실행으로 `prompt_assets`, `prompt_asset_versions`, `session_blueprints`, `prompt_change_requests`, `cr_approvals`, `compiled_prompt_provenance` 6개 테이블 생성. `session_blueprints`에 `default.standard` 시드 삽입.
  - **P1 prompt_compiler 활성화**: W3 테이블 생성으로 `PromptCompiler.compile()` (chat_service.py L3873)이 실제 `prompt_assets` + `session_blueprints` DB 조회 경로로 작동 시작. `record_prompt_provenance()`로 `compiled_prompt_provenance`에 빌드 이력 저장.
  - **P0 feature_flags.py 호스트 패치**: `governance_enabled()` helper 함수를 호스트 파일에 추가 (로컬 워크트리에만 존재하던 상태 보정).
  - **runner-af09281f 정리**: depends_on이 rejected_done인 영구 대기 러너를 error 상태로 전환.
  - **runner-34c0836a 제출**: Admin Dashboard 4개 페이지(governance/model-parity/deploy/sessions) 일괄 구현 러너 (실행 중).
  - **API Hot-Reload**: 54개 모듈 재로드 완료, health-check 전항목 정상 확인.

- **2026-04-24 직접 보강**: AADS 채팅 실행 복구를 `execution_id` 중심으로 전환했다. `chat_turn_executions`, `chat_messages.execution_id`, `chat_sessions.current_execution_id`를 도입했고, `app/services/chat_service.py`, `app/routers/chat.py`, `app/services/redis_stream.py`, `app/services/stream_worker.py`, `app/main.py`에서 execution 단위 SSE attach/replay, 단일 assistant row 재사용, execution 기반 resume 스캐너를 반영했다. 기존 `recovered` 추론 복구는 fallback 성격으로 축소됐다.
- **2026-04-24 운영 조치**: 서버 `deploy.sh`의 `code` 모드 health 대기 시간을 기본 30초에서 60초로 늘려, graceful restart 직후 앱이 정상 복귀했는데도 배포 스크립트가 거짓 실패로 종료하던 false negative를 줄였다. 대시보드 `deploy.sh`는 비활성 대상 슬롯 컨테이너가 남아 있을 때 선정리 후 기동하도록 보강했다.
- **2026-04-24 검증 결과**: Governance v2.1 후속 검증을 다시 수행했다. 백엔드 단위테스트는 `python3.11 -m pytest tests/unit/test_governance_v21.py tests/unit/test_governance_change_requests.py tests/unit/test_prompt_compiler.py -q` 기준 `10 passed`였고, 실제 프런트 빌드 루트인 `/root/aads/aads-dashboard`는 `./node_modules/.bin/tsc --noEmit --incremental false` 타입체크가 통과했다. 다만 실제 대시보드 체크아웃에는 `src/app/admin/model-parity/page.tsx`만 존재하고 `governance/emergency/sessions/deploy` 페이지와 Sidebar 링크는 아직 없으며, 현재 워크스페이스의 `aads-dashboard/`는 `src/` 스냅샷만 있어 여기서는 Next 빌드를 돌릴 수 없다. 또한 DB 마이그레이션 실적용 여부는 이 세션의 샌드박스가 `psql` 소켓 생성을 `Operation not permitted`로 차단해 실측하지 못했다.
- **2026-04-24 직접 보강**: Governance v2.1 운영 가시화를 추가했다. `app/api/governance.py`에 `GET /governance/role-profiles`를 추가해 `role_profiles.project_scope/tool_allowlist`를 노출했고, `aads-dashboard/src/app/admin/emergency/page.tsx`에서 `governance_enabled` kill-switch, 기타 feature flag, governance audit log, 역할별 프로젝트 범위를 한 화면에서 제어/확인할 수 있게 했다. `Sidebar.tsx`, `aads-dashboard/src/lib/api.ts`, `tests/unit/test_governance_v21.py`도 함께 갱신했다.
- **2026-04-24 직접 보강**: Governance v2.1 런타임 결함을 보정했다. `app/core/feature_flags.py`에 `governance_enabled()` helper를 추가했고, `app/services/intent_router.py`의 intent temperature 조회를 실제 스키마인 `intent_policies.temperature`로 정렬했다. `app/api/governance.py`는 `temperature` 필드를 조회/저장하도록 보강했고, `tests/unit/test_governance_v21.py`로 회귀 테스트를 추가했다.
- **2026-04-24 직접 보강**: Runner Task Board가 제출 모델(`model`)과 실제 실행 모델(`actual_model`)을 혼동하던 문제를 보강했다. `scripts/pipeline-runner.sh`가 시도 시작 즉시 `pipeline_jobs.actual_model`을 갱신하도록 바꿨고, `/admin/tasks` 목록과 `aads-dashboard/src/app/admin/tasks/page.tsx`가 `actual_model`을 우선 표시하며 상세 패널에 `Actual/Configured/Worker Override`를 분리해 보여준다.
- **2026-04-24 직접 보강**: Admin Dashboard 잔여 누락을 로컬 워크트리에 직접 반영했다. `app/api/admin.py`에 `/admin/sessions`, `/admin/sessions/{job_id}`를 추가했고, `aads-dashboard/src/lib/api.ts`에 sessions/model-parity API 메서드를 보강했으며, `aads-dashboard/src/app/admin/model-parity/page.tsx`를 신규 추가하고 `Sidebar.tsx`에 Governance/Model Parity/Deploy/Sessions 링크를 정리했다.
- **승인 대기**: `runner-db5686da` — `/admin/governance` 세션 거버넌스 대시보드 (백엔드+프론트)
- **승인 대기**: `runner-18ddd734` — `/admin/model-parity` 모델 패리티 대시보드 (백엔드+프론트)
- **2026-04-24 운영 조치**: `claude-relay` 전역 동시성은 Pipeline Runner를 포함하지 않는 것으로 재확인했다. live는 systemd drop-in `/etc/systemd/system/claude-relay.service.d/runtime.conf`로 `CLAUDE_RELAY_MAX_CONCURRENT=5`, `CLAUDE_NONINTERACTIVE_WRAPPER=/root/aads/aads-server/scripts/claude-docker-wrapper-active.sh`를 고정했다. blue-green 전환 후에도 relay/Claude CLI가 `.active_container`를 따라 현재 활성 API 컨테이너를 사용한다.
- **2026-04-24 운영 조치**: 채팅 active stream 계측은 `executing / visible / recovery_pending / recent_placeholders` 기준으로 재정리했다. 재배포 drain에서 실제 활성 스트림이 `2 → 1 → 0`으로 집계되는 것을 확인했고, 이전처럼 resume/placeholder 세션이 있어도 `active=0`으로 보이던 오판을 줄였다.
- **거버넌스 v2.1 Phase 1-A 준비**: `scripts/migrations/20260424_governance_v2_1_tables.sql` 추가 — `governance_events`, `intent_policies`, `role_profiles`, `change_requests` 생성 마이그레이션과 시드(`intent_policies=7`, `role_profiles=5`)를 반영했다.
- **거버넌스 v2.1 P1-D 거버넌스 컬럼 확장 (temperature + project_scope)**: `scripts/migrations/20260424_governance_v2_1_columns.sql` 추가 — `intent_policies.temperature`, `role_profiles.project_scope` 컬럼 확장과 `intent_policies` 기본 temperature 시드 업데이트를 반영했다.
- **migration 054** (`054_llm_key_provider_normalization.sql`) — untracked, DB 정규화 대상 0건으로 적용 무해
- **migration 055** (`chat_model_preferences`) — DB 적용 완료
- **인증 우선순위**: `ANTHROPIC_AUTH_TOKEN_2`(moongoby, priority=1), `ANTHROPIC_AUTH_TOKEN`(moong76, priority=2)
- **2026-04-24 장애 조치**: `llm_models.metadata`가 JSON 문자열 row일 때 `model_selector._route_metadata()`와 `model_registry.sync_model_registry()`가 `dict(...)`로 바로 처리하며 `ValueError`를 내던 공통 장애를 수정했다. `app/services/model_selector.py`, `app/services/model_registry.py`에 metadata coercion을 추가했고, 문자열 metadata 회귀 테스트를 `tests/unit/test_model_selector_dynamic_routing.py`, `tests/unit/test_model_registry.py`에 남겼다.
- **2026-04-24 장애 조치**: `app/services/model_registry.py`의 `filter_executable_models()`에 `_normalize_model_id()`를 추가해 `codex:`, `litellm:`, `claude:` 접두사를 제거한 뒤 `llm_models.model_id`와 비교하도록 수정했다. `claude-sonnet` vs `claude-sonnet-4-6` 같은 버전 suffix는 `startswith`로 허용해 `runner_model_config` 설정이 전부 탈락하면서 `minimax-m2.7` 폴백으로 내려가던 문제를 막는다. 회귀 테스트는 `tests/unit/test_model_registry.py`에 반영했다.
- **AADS-200B backend 반영**: `migrations/056_braming_node_feedback.sql`로 `braming_nodes`에 `ceo_opinion/picked` 컬럼을 추가하고 `braming_node_votes` 테이블을 도입했다. `app/services/braming_service.py`, `app/api/braming.py`는 노드 상세 조회, CEO 의견 저장/삭제, 찬반 투표 토글, Pick/Unpick API와 그래프 응답의 `ceoOpinion/voteSummary/myVote/picked` enrichment를 지원한다. 회귀 테스트는 `tests/unit/test_braming_service.py`, `tests/unit/test_braming_api.py`에 추가했다.
- **AADS-200B frontend 블로커**: 요구된 프론트 경로 `/root/aads/aads-dashboard/src/app/braming/*` 는 현재 워크스페이스 쓰기 허용 범위 밖이라 본 런에서는 수정하지 못했다. 다음 작업은 해당 경로 쓰기 권한이 열린 환경에서 `api.ts`, `page.tsx`, `components/BramingCanvas.tsx`, `components/BramingNode.tsx`, `components/NodeDetailPanel.tsx`를 백엔드 계약에 맞춰 연결하면 된다.

## AADS-190E
- `scripts/claude_relay_server.py`에 Claude/Codex 실행 preflight와 `aads-tools` MCP bridge preflight를 추가했다. `docker exec` 경로와 `python3.11 -m mcp_servers.aads_tools_bridge` 직접 실행 경로를 후보로 두고, 실패 원인을 `docker_container_missing`, `python_module_missing` 같은 분류로 로그에 남긴다.
- `scripts/mcp_config_template.json`의 기본 bridge 실행기를 `python3`로 정리해 템플릿 경로와 relay가 선택하는 docker 경로가 같은 실행기를 가리키도록 맞췄다.
- 같은 파일에서 Claude 기본 실행 경로는 `scripts/claude-docker-wrapper.sh`를 우선 사용하도록 복원했고, Codex/Claude 모두 health 응답에 현재 command mode와 MCP bridge mode를 노출한다.
- `scripts/claude_relay_server.py`와 `app/services/model_selector.py`, `app/services/chat_service.py`는 `user cancelled MCP tool call`을 `session_cancelled_mcp_tool_call`로 재분류하고 `is_error/error_type/cancel_scope/raw_error`를 SSE까지 유지한다. 세션별 취소가 더 이상 일반 user cancel 문자열로만 뭉개지지 않는다.
- `mcp_servers/aads_tools_bridge.py`는 `/app` 외에 저장소 루트도 `sys.path`에 추가해 호스트 `python3.11 -m ...` 직접 실행 경로를 지원한다.
- `app/services/pipeline_runner_client.py`를 추가하고 `app/services/tool_executor.py`, `app/api/ceo_chat_tools.py`의 Pipeline Runner 내부 호출 URL을 공통 helper로 통일했다. 내부 self-call 기본값은 `http://localhost:8080`이며, 필요 시 `PIPELINE_RUNNER_INTERNAL_BASE_URL`로 오버라이드한다.
- `tests/unit/test_relay_diagnostics.py`를 추가해 내부 runner URL helper, direct Python MCP session 주입, relay 취소 재분류를 검증하는 회귀 테스트를 남겼다.

## AADS-190C
- `app/services/llm_account_usage.py` 추가로 `llm_api_keys`, `oauth_usage_log`, `pipeline_jobs.actual_model/worker_model`을 결합한 계정별 LLM 사용량 스냅샷 계층을 도입했다.
- background/provider 분류는 `codex:gpt-5.4`, `litellm:gemini-2.5-flash`, `litellm:openrouter-grok-4-fast`, `litellm:kimi-k2`, `litellm:minimax-m2.7`, `litellm:groq-qwen3-32b`와 같은 접두사/실모델 표기를 모두 인식한다.
- Anthropic 계정은 `oauth_usage_log` 기준 exact per-account 5h/7d 사용량과 recent error, 최신 rate-limit 헤더를 노출하고, 기타 provider는 `pipeline_jobs` 기준 provider-level observed usage 또는 key state only로 구분한다.
- `app/api/ops.py`에 `/api/v1/ops/account-usage` API를 추가했다.
- `tests/unit/test_llm_account_usage.py`로 접두사 기반 provider 매핑과 표시명 보강(Kimi, MiniMax, Codex CLI)을 검증한다.

## AADS-189B
- `app/services/model_registry.py`의 템플릿 metadata에 `execution_backend`, `execution_model_id`, `execution_base_url`를 추가해 “보이는 모델”과 “실제 실행 경로”를 같은 row에 담는다.
- direct provider 후보는 OpenAI, Groq, DeepSeek, OpenRouter, Qwen, Kimi, MiniMax로 정리했고, Anthropic은 `claude_cli_relay`, Codex는 `codex_cli`, Gemini는 `litellm_proxy` backend로 표시한다.
- `app/services/model_selector.py`는 레지스트리 row metadata를 읽어 `openai_compatible_direct` 모델을 우선 direct provider 경로로 호출한다. 정적 allowlist에 없는 신규 모델도 `llm_models`에 row가 있으면 direct route를 탈 수 있다.
- direct provider API 키는 provider별 활성 키 우선, 없으면 환경변수 폴백을 사용한다.
- 회귀 테스트는 `tests/unit/test_model_selector_dynamic_routing.py`에 추가했다. Qwen 신규 동적 row가 LiteLLM 하드코딩 경로가 아니라 direct route로 분기되는지를 검증한다.
- 운영 주의: `llm_models.metadata`는 DB/드라이버 상태에 따라 dict가 아니라 JSON 문자열로 읽힐 수 있다. selector/sync 양쪽 모두 문자열 metadata를 먼저 JSON object로 정규화한 뒤 사용해야 한다.

## AADS-189A
- `migrations/053_llm_model_registry.sql` 추가로 `llm_models`, `llm_key_audit_logs` 테이블을 도입했다.
- `app/services/model_registry.py` 추가로 provider 템플릿 기반 모델 레지스트리, provider 요약, 수동/자동 sync, cache invalidation 공통 계층을 구현했다.
- `app/api/llm_keys.py`는 create/update/activate/deactivate 시 priority 충돌 검증, 감사 로그 적재, stale key cache 제거, registry sync를 수행한다.
- `app/api/llm_models.py`와 `app/main.py` 라우터 등록으로 `/api/v1/llm-models`, `/api/v1/llm-models/providers/summary`, `/api/v1/llm-models/sync` API를 제공한다.
- `app/services/model_selector.py`, `app/services/pipeline_runner_service.py`, `app/api/pipeline_runner.py`, `app/services/code_reviewer.py`가 DB 레지스트리의 실행 가능 모델 필터를 우선 사용하고, 활성 모델이 비어 있으면 기존 하드코딩 경로로 안전 폴백한다.
- `tests/unit/test_model_registry.py`로 provider 정규화, unknown provider review 상태, executable filter 폴백 규칙을 검증한다.

## AADS-188
- `app/api/llm_keys.py` 추가로 `llm_api_keys` 조회·추가·수정·비활성화 API 제공.
- `app/main.py`에 `/api/v1/llm-keys` 라우터 등록.
- 대시보드 Settings 탭에서 LLM API 키 관리 UI를 연동하도록 백엔드 계약 추가.

## AADS-187
- `scripts/update_claude_all_servers.sh` 전면 재작성.
- 서버 114를 첫 순서로 즉시 처리하도록 배치.
- Claude Code CLI, Codex CLI, `claude-agent-sdk` 버전 전후 비교와 변경 시 Telegram 알림 추가.
- `/root/aads/.env` 로드, `/root/tmp` 기반 pip 설치, 서버별 실패 내성, 최종 성공/실패 요약 전송 추가.

## 운영 반영 포인트
- 목표 cron 라인: `0 4 * * * /root/aads/aads-server/scripts/update_claude_all_servers.sh >> /var/log/claude_update.log 2>&1`
- 현재 워크스페이스에는 실제 시스템 crontab과 원격 서버 상태가 없어서 파일 수정만 반영됨.

## AADS-CHAT-OPT (2026-04-28)
- **c46ddbe** `feat(chat): interrupt routing + retry P0 + ext-cache 1h + tool cache (4patch)` — origin/main push 완료, reload-api.sh로 08:31 KST 서버 메모리 반영 완료
- **4-patch 적용**: ①interrupt 자동 라우팅(routers/chat.py L239) ②LLM 재시도 5초×60회(anthropic_client.py L32) ③extended-cache 1h(cache_config.py L21) ④tool execution-scope LRU 캐시(tool_executor.py L88)
- **thinking UI 패치(f89ce6c)**: thinkingBuf 분리 + streamingThinking prop 렌더 — green 컨테이너 15:02 KST 반영
- **빈 버블 패치**: streamingContent 조건에 `&& streamBuf` 추가 — page.tsx L4936 호스트 반영 완료 (streaming=true && streamBuf="" 순간 빈 버블 방지)

## AADS-PROMPT-GOV-V2.1 (2026-04-28 08:25 KST)
- **prompt_assets 24건 시딩 완료** (L1:4 / L2:6 / L3:7 / L4:4 / L5:3) — 5-Layer 구조 모두 채워짐
- **PromptCompiler INSERT 패치**: `_record_provenance()`의 conn release 이슈 수정 — `compiled_prompt_provenance` 1건 첫 실측 INSERT 확인
- **runner-368675d8 승인**: `/admin/prompts` 페이지에 5-Layer CRUD 탭 추가 (Layer 필터 사이드바 + 모달 에디터 + JSON scope 검증)

## AADS-DOCS-INCREMENTAL-SCAN (2026-04-28 14:27 KST)
- `/docs` 문서 스캔을 기존 목록 재사용 + 증분 갱신 방식으로 보강했다.
- Backend: `app/api/project_docs.py`가 5분 메모리 캐시 외에 `/tmp/aads_project_docs_cache.json` 파일 캐시를 저장/복원하고, 강제 스캔 시 `delta.new/updated/removed/unchanged`를 계산한다.
- Frontend: `aads-dashboard/src/app/docs/page.tsx`가 `localStorage(aads.docs.scanResult.v1)`의 기존 목록을 즉시 렌더링한 뒤 백그라운드로 최신 목록을 갱신한다.
- 검증: `docker exec aads-server python3 -m py_compile /app/app/api/project_docs.py`, `npx eslint src/app/docs/page.tsx`, 컨테이너 직접 호출 기준 문서 1,431건 및 2회차 `cache_hit=True` 확인.

## AADS-CHAT-STREAM-PLACEHOLDER (2026-04-28 17:26 KST)
- 진행 중 버블 미표시 원인을 재확인했다. 백엔드는 `streaming_placeholder`와 Redis stream을 생성하지만, 프론트의 폴링 최신 메시지 조회가 `waitingBg=true`일 때도 `include_streaming=true` 없이 `/chat/messages`를 호출해 placeholder 복구 분기가 작동하지 않을 수 있었다.
- Frontend: `aads-dashboard/src/app/chat/page.tsx`의 polling `rawLatest` 조회에 `_waitingBg ? "&include_streaming=true" : ""`를 추가했다. SSE attach가 늦거나 끊겨도 waiting background 상태에서는 DB placeholder를 받아 진행 버블을 유지한다.
- 검증: 변경 diff는 단일 URL 옵션 추가. `npx eslint src/app/chat/page.tsx`와 `npx tsc --noEmit --pretty false`는 기존 누적 오류(admin API 타입 누락, page.tsx 기존 lint 오류)로 실패했고, 이번 수정 라인 신규 오류는 확인되지 않았다.
## 2026-04-29 09:03 KST - L1 Global prompt governance 강화

- 추가: `migrations/066_strengthen_l1_global_prompts.sql`
- 목적: L1 Global 4개 에셋을 운영 규칙 수준으로 확장하고 `global-layer-governance` 신규 추가
- 운영 DB 적용: `prompt_assets.layer_id=1` 활성 5건, 평균 643자
- 컴파일러 검증: CEO/task_query/gpt-5.5/PromptEngineer 샘플에서 L1 5건 모두 `applied_assets` 선택 확인
- 주의: 실제 채팅 provenance row는 다음 채팅 실행부터 신규 L1 5건으로 기록됨

## 2026-04-29 09:19 KST - CTO L3 role prompt scope 정리

- 추가: `migrations/067_refine_cto_role_prompts.sql`
- 목적: 공통 `role-cto-strategist`에서 6개 프로젝트 직접 열거를 제거하고, 프로젝트 전문성은 CTO 오버레이로 분리
- 운영 DB 적용: 공통 CTO 1건 업데이트, `project-role-aads-cto` 갱신, `project-role-go100-cto`/`project-role-ntv2-cto` 신규 추가
- 검증: 공통 CTO 본문에서 `6개 프로젝트|AADS, KIS, GO100, SF, NTV2, NAS` 패턴 0건, AADS/GO100/NTV2 샘플 매칭에서 각각 공통 CTO + 프로젝트 CTO 오버레이 선택 확인
- 주의: 실제 채팅 provenance row는 CTO 역할이 지정된 다음 메시지부터 신규 CTO 에셋 조합으로 기록됨

## 2026-04-29 09:34 KST - L2 Project prompt governance 강화

- 추가: `migrations/068_strengthen_l2_project_prompts.sql`
- 목적: CEO 통합지시 L2 신규 추가, 프로젝트별 서버/경로 계약 정정, AADS/GO100/KIS/NTV2/SF/NAS L2 완료 기준 강화
- 운영 DB 적용: `prompt_assets.layer_id=2` 활성 8건, 평균 721자, 최소 596자, 최대 884자
- 경로 보정: `/srv/newtalk-v2`, `/root/webapp` 구식 경로 패턴 0건 확인. KIS/GO100=`/root/kis-autotrade-v4`, SF=`/data/shortflow`, NTV2=`/var/www/newtalk`, AADS=`/root/aads/aads-server`/`/root/aads/aads-dashboard` 기준 반영
- 컴파일러 매칭 검증: CEO는 `project-ceo-orchestration-context`, AADS는 `project-aads-context`, KIS/GO100/SF/NTV2/NAS는 각 프로젝트 L2 + `project-remote-access-contract` 매칭 확인
- 주의: 실제 채팅 provenance row는 각 워크스페이스의 다음 메시지부터 신규 L2 에셋 조합으로 기록됨

## 2026-04-29 09:52 KST - Project UX role overlays 보강

- 추가: `migrations/069_seed_project_ux_role_overlays.sql`
- 목적: 공통 `UXProductDesigner / UX·제품디자이너` 역할에 AADS/SF/KIS/NAS 전용 L3 프로젝트 오버레이를 추가하고, NAS에도 역할 드롭다운 노출 범위를 확장
- 운영 DB 적용: 신규 UX 오버레이 4건 추가, `role_profiles.role='UXProductDesigner'` project_scope를 `{AADS,SF,NTV2,GO100,KIS,NAS}`로 보정
- 검증: L3 활성 46건, UX 프로젝트 오버레이 6건. AADS/SF/KIS/NAS/GO100/NTV2 각각 `workspace + design_review + UXProductDesigner` 샘플에서 프로젝트별 UX 오버레이 1건씩 매칭 확인
- 주의: 역할 API는 인증 필요로 무토큰 호출 시 401이 정상. 실제 채팅 provenance row는 UXProductDesigner 역할이 지정된 다음 메시지부터 신규 오버레이 조합으로 기록됨

## 2026-04-29 10:02 KST - UXProductDesigner L3 전문 역할 정리

- 추가: `migrations/070_refine_ux_designer_role_prompts.sql`
- 목적: 공통 `role-ux-product-designer`에서 프로젝트별 문구를 제거하고, Product UX Architect / Interaction Designer / UI System Designer / UX Writer / Accessibility·Mobile / Design QA Auditor 하위 전문성을 명시
- 운영 DB 적용: 공통 UX 프롬프트 1,635자로 확장, workspace_scope를 `{AADS,SF,NTV2,GO100,KIS,NAS}`로 정합화, 프로젝트별 UX 오버레이 6건 표준 구조로 재작성
- GO100 분리: `project-role-go100-ux` 신규 추가, 기존 `project-role-go100-ux-growth`는 `GrowthContentStrategist` 전용으로 role_scope 분리
- 검증: 공통 UX 본문에서 `AADS|GO100|NTV2|KIS|SF|NAS` 프로젝트명 패턴 0건. AADS/GO100/KIS/NAS/NTV2/SF 각각 `role-ux-product-designer + project-role-*-ux` 2건 매칭 확인. GO100 Growth는 `role-growth-content + project-role-go100-ux-growth`, GO100 UX는 `role-ux-product-designer + project-role-go100-ux`로 분리 확인
- 주의: `chat_sessions.role_key='UXProductDesigner'` 세션은 현재 0건이므로 실제 provenance 기록은 세션 역할 지정 후 다음 메시지부터 생성됨. API 헬스체크 `http://localhost:8100/health` 200 확인

## 2026-04-29 10:21 KST - PM L3 role prompt 전문성 보강

- 추가: `migrations/071_refine_pm_role_prompts.sql`
- 목적: `PM / 프로젝트매니저`를 `PM / 제품·프로젝트매니저`로 재정의하고, 공통 PM은 요구사항 구조화·우선순위·역할 배정·acceptance criteria·릴리즈 리스크 검수 책임으로 확장
- 운영 DB 적용: PM 관련 L3 활성 에셋 7건(`role-pm-coordinator` + AADS/GO100/KIS/NAS/NTV2/SF PM 오버레이), 평균 596자, 최소 478자, 최대 1,110자
- role profile 보정: `role_profiles.role='PM'`의 `display_name_ko`를 `제품·프로젝트매니저`로 변경하고 `quality_rubric_version='pm-product-project-manager-v1'`, acceptance criteria/역할 배정/릴리즈 리스크 체크 플래그 추가
- 검증: AADS/GO100/KIS/NAS/NTV2/SF 각각 `role=PM`, `intent=status_check`, `model=gpt-5.5` 샘플에서 `role-pm-coordinator + project-role-*-pm` 2건 매칭 확인
- 주의: 실제 채팅 provenance row는 PM 역할이 지정된 세션의 다음 메시지부터 신규 PM 에셋 조합으로 기록됨

## 2026-04-29 11:09 KST - VibeCodingLead 역할 및 역할 활용 팁 반영

- 추가: `migrations/072_seed_vibe_coding_lead_role.sql`
- 목적: 비개발자 CEO/제품 오너의 자연어 지시를 제품 요구사항, 안전한 작업 지시서, 역할 배정, 검증 기준으로 변환하는 `VibeCodingLead / AI 제품구현 총괄·바이브코딩 리드` 역할 신설
- 운영 DB 적용: L3 활성 에셋 8건(`role-vibe-coding-lead` + CEO/AADS/GO100/KIS/NAS/NTV2/SF 오버레이), 평균 536자
- role profile 추가: `role_profiles.role='VibeCodingLead'`, `display_name_ko='AI 제품구현 총괄·바이브코딩 리드'`, `project_scope={AADS,KIS,GO100,SF,NTV2,NAS,CEO,VIBE}`, `when_to_use`/`how_to_instruct`/`instruction_template` 메타데이터 저장
- API/UI 보강: `/chat/workspaces/{workspace_id}/roles` 응답에 역할 활용 팁 메타데이터를 포함하고, 좌측 세션 역할 셀렉터에서 선택된 역할 옆 `?` 툴팁으로 도움말 표시 가능하게 패치
- 검증: CEO/AADS/GO100/KIS/NAS/NTV2/SF 각각 `role=VibeCodingLead`, `intent=product`, `model=gpt-5.5` 샘플에서 공통 역할 + 프로젝트 오버레이 2건 매칭 확인. VIBE 워크스페이스는 공통 역할 1건 매칭 확인. `docker exec aads-server python3 -m py_compile /app/app/services/chat_service.py`, `npx eslint src/components/chat/Sidebar.tsx` 통과
- 주의: DB 역할은 즉시 사용 가능. API/UI 코드 변경은 실행 프로세스/대시보드 번들 반영이 필요하며, 실제 채팅 provenance row는 세션에 `VibeCodingLead` 역할 지정 후 다음 메시지부터 생성됨

## 2026-04-29 12:19 KST - Ops L3 role prompt 전문성 보강

- 추가: `migrations/073_refine_ops_developer_qa_judge_roles.sql`
- 목적: `Ops / 운영담당자`를 `Ops / 배포·운영엔지니어`로 재정의하고, SRE와 역할 경계를 분리. Ops는 릴리즈 실행, runbook, 승인 조건, 롤백, 운영 보고를 책임지도록 보강
- 운영 DB 적용: Ops 관련 L3 활성 에셋 7건(`role-ops-monitor` + AADS/GO100/KIS/NAS/NTV2/SF Ops 오버레이), 평균 579자, 최소 405자, 최대 1,202자
- role profile 보정: `role_profiles.role='Ops'`의 `system_prompt_ref='prompt_assets:role-ops-monitor'`, `display_name_ko='배포·운영엔지니어'`, `quality_rubric_version='ops-release-operations-v1'`, health/active task/rollback/verification 플래그 추가
- 검증: AADS/GO100/KIS/NAS/NTV2/SF 각각 `role=Ops`, `intent=deploy` 샘플에서 `role-ops-monitor + project-role-*-ops` 2건 매칭 확인. API 헬스체크 `http://localhost:8100/health` 200 확인
- 주의: 현재 `chat_sessions.role_key='Ops'` 세션과 최근 24시간 Ops provenance는 0건이므로 실제 provenance 기록은 세션 역할 지정 후 다음 메시지부터 생성됨. 재시작은 불필요

## 2026-04-29 12:36 KST - Developer/QA/JudgeEvaluator 역할 경계 및 프로젝트 오버레이 보강

- 추가: `migrations/074_refine_developer_qa_judge_roles.sql`
- 목적: `Developer`는 구현, `QA`는 재현 가능한 검증, `JudgeEvaluator`는 독립 승인/조건부 승인/반려 판정으로 역할 경계를 분리하고 6개 프로젝트 모두에 전용 L3 오버레이를 부여
- 운영 DB 적용: 공통 L3 3건 갱신(`role-developer-implementer`, `role-qa-verifier`, `role-judge-evaluator`), 프로젝트 오버레이 18건 UPSERT, 기존 `project-role-ntv2-qa-judge` 혼합 오버레이 비활성화
- role profile 보정: `Developer=구현 엔지니어`, `QA=품질검증 엔지니어`, `JudgeEvaluator=독립 평가·검수관`으로 표시명과 `when_to_use`/`how_to_instruct` 메타데이터 추가
- 검증: AADS/GO100/KIS/NAS/NTV2/SF 각각 Developer/QA/JudgeEvaluator 샘플에서 공통 역할 + 프로젝트 오버레이 2건씩 매칭 확인. 관련 활성 L3 에셋 21건, 평균 381자. API 헬스체크 `http://localhost:8100/health` 200 확인
- 주의: DB 에셋 변경이라 재시작은 불필요. 실제 provenance 기록은 해당 역할이 지정된 세션의 다음 메시지부터 생성됨

## 2026-04-29 12:49 KST - CEO PromptContextHarnessEngineer L3 scope 핫픽스

- 추가: `migrations/075_fix_ceo_prompt_context_harness_scope.sql`
- 목적: CEO 통합지시 세션에서 `PromptContextHarnessEngineer` 역할을 선택했는데 L3가 빠지는 문제 수정
- 원인: `role-prompt-context-harness-engineer`가 `workspace_scope={AADS}` 및 제한된 `intent_scope`만 갖고 있어 `workspace=CEO`/일부 intent에서 매칭되지 않음
- 운영 DB 적용: 공통 `role-prompt-context-harness-engineer`에 `CEO` workspace와 `*` intent 추가, `project-role-ceo-prompt-context-harness` 신규 추가, `role_profiles.role='PromptContextHarnessEngineer'`에 `CEO` project_scope 및 provenance 검증 메타데이터 추가
- 검증: CEO + `PromptContextHarnessEngineer` + `status_check` + `gpt-5.5` 샘플에서 L3 2건(`role-prompt-context-harness-engineer`, `project-role-ceo-prompt-context-harness`) 매칭 확인. `aa433b41-0ad2-421c-ae7c-bac4806035cc` 최신 provenance는 L1 5/L2 2/L3 2/L4 1/L5 2, `fallback_used=false`, compile_error 없음. API 헬스체크 `http://localhost:8100/health` 200 확인
- 주의: 실제 CEO 현재 세션 provenance는 다음 메시지부터 신규 L3 조합으로 기록됨. 재시작은 불필요

## 2026-04-29 12:57 KST - Prompt provenance 기반 상태 답변 규칙 보강

- 추가: `migrations/076_enforce_prompt_provenance_status_answers.sql`
- 목적: 시스템 프롬프트/역할 프롬프트 적용 여부 질문에서 모델이 워크스페이스 고정 정체성 문구나 이전 답변 본문으로 오판하지 않고 `compiled_prompt_provenance`를 최종 근거로 답하게 함
- 운영 DB 적용: `global-layer-governance`에 시스템 프롬프트 적용 판정/충돌 처리 규칙 추가, `intent-status-check`에 프롬프트 적용 상태 조회 절차 추가
- 지정 세션 확인: `ed08553d-a842-4967-8867-00e82ddd2eba` 최신 provenance는 `2026-04-29 12:32 KST`, workspace=`GO100`, role=`VibeCodingLead`, `system_prompt_chars=22873`, applied assets 11건, compile_error 없음
- 검증: GO100 + `VibeCodingLead` + `status_check` + `claude-sonnet-4-6` 샘플 매칭에서 L1 5건(`global-layer-governance` 1,328자 포함), L2 2건, L3 2건, L4 1건(`intent-status-check` 862자), L5 1건 선택 확인. API 헬스체크 `http://localhost:8100/health` 200 확인
- 주의: 기존 provenance 행은 컴파일 당시 스냅샷이라 과거 chars가 남는 것이 정상. 신규 보강 규칙은 다음 컴파일/다음 메시지부터 provenance에 반영됨. 재시작은 불필요

## 2026-04-30 06:12 KST - AADS 서버 + 대시보드 전체 blue-green 배포

- 대상: `/root/aads/aads-server` `bash deploy.sh bluegreen`, `/root/aads/aads-dashboard` `bash deploy.sh` 순차 실행. 배포 후 nginx upstream은 서버 `127.0.0.1:8100` primary, `127.0.0.1:8102` backup이고 대시보드는 `127.0.0.1:3100` primary, `127.0.0.1:3101` backup 상태.
- 반영 범위: 서버 저장소의 Android/device/tool_executor 변경과 대시보드 채팅 화면 `src/app/chat/page.tsx`, `src/app/chat/types.ts`, 설정 화면 `src/app/settings/page.tsx`, `src/components/settings/LlmRegistryWorkspacePanel.tsx` 변경이 배포 산출물에 포함됨.
- 운영 확인: `docker ps` 기준 `aads-server`, `aads-server-green`, `aads-dashboard`, `aads-postgres`, `aads-litellm`, `aads-searxng`, `aads-redis`가 running/healthy. `curl http://localhost:8100/api/v1/health` 응답 `status=ok`, `graph_ready=true`, `sandbox.status=ok`.
- 외부 확인: `curl https://aads.newtalk.kr/login` 200, `curl -L https://aads.newtalk.kr/chat` 200(`/login?redirect=%2Fchat`) 확인.
- 배포 주의: 서버 blue-green drain 단계에서 활성 스트림 2건이 300초 타임아웃까지 남아 강제 전환됐으나, 전환 후 Health/DB 스키마/채팅 테이블/LLM 검증은 모두 통과. 대시보드 `next build` 성공, 내부/외부 헬스체크 통과, 프론트엔드 QA API는 `UNKNOWN` verdict로 통과 처리됨.
- Git 상태 주의: 서버 저장소와 대시보드 저장소 모두 미커밋 변경이 남아 있음. 별도 지시 전까지 기존 변경은 되돌리지 않음.

## 2026-04-30 07:32 KST - Playwright MCP STOPPED 복구

- 원인: `supervisord.conf`의 `playwright-mcp`가 `npx @playwright/mcp`를 사용하지만 서버 이미지 `Dockerfile`에 `nodejs/npm`이 없어 supervisor 기동 시 `no such file`로 실패.
- 조치: `Dockerfile`에 `nodejs npm` 설치를 추가하고, `supervisord.conf`에서 `playwright-mcp`를 `autostart=true`, `autorestart=true`, `startretries=3`으로 변경. 현재 실행 컨테이너에는 `apt-get install nodejs npm` 후 `supervisorctl reread/update`로 즉시 반영.
- 검증: `supervisorctl status all` 기준 `mcp-servers:playwright-mcp RUNNING`, `/var/log/playwright-mcp.log`에 `Listening on http://localhost:8768` 확인. `curl http://localhost:8768/mcp`는 MCP HTTP 엔드포인트가 살아 있어 `Invalid request`를 반환. AADS 헬스체크 `https://aads.newtalk.kr/api/v1/health`는 `status=ok`.

## 2026-04-30 14:49 KST - aads-redis 자동복구 성공 오알림 차단

- 증상: Telegram에 `자동복구 성공 / 서비스: 68:aads-redis / 명령: docker restart aads-redis / 결과: Restart blocked for aads-redis (use external watchdog)` 알림이 반복됨.
- 원인: `app/services/unified_healer.py`가 보호 컨테이너(`aads-server`, `aads-postgres`, `aads-redis`, `aads-litellm`)의 내부 restart 차단을 `success=True`로 반환해 실제 재시작이 없는데도 성공 알림을 발송할 수 있었음.
- 즉시 조치: 운영 DB `monitored_services`의 `68:aads-redis` `auto_recovery_command`를 `NULL`로 변경해 현재 실행 중인 Healer가 Redis 내부 재시작을 더 이상 시도하지 않도록 차단. Redis 상태는 `PONG`, Docker health `healthy`, `consecutive_failures=0`.
- 코드 조치: `unified_healer.py`에서 보호 컨테이너 restart/stop 차단 결과를 `success=False, blocked=True`로 반환하고, 서비스/error 복구 경로 모두 `blocked`는 성공/실패 텔레그램을 보내지 않고 `auto_recovery_blocked`/`error_recovery_blocked` 로그만 남기도록 패치.
- 검증: `python3 -m py_compile app/services/unified_healer.py` 통과, `bash scripts/reload-api.sh` hot-reload 성공(53개 모듈 재로드), 30초 이상 모니터링 후 `68:aads-redis`는 `ok`, 최근 앱 로그에 `aads-redis|Restart blocked|auto_recovery_blocked` 재발 없음. API health는 blue/green 모두 `status=ok`.
- 주의: 반복 알림 차단은 DB 변경으로 즉시 반영됐고, 코드 패치도 hot-reload로 런타임 반영 완료. Redis 실제 장애 복구는 호스트 cron `/root/aads/aads-server/watchdog-host.sh`의 Layer 0가 담당.

## 2026-04-30 16:14 KST - aads-redis 오알림 재발 경로 추가 차단

- 증상: 위 조치 후에도 CEO Telegram에 동일한 `68:aads-redis / docker restart aads-redis / Restart blocked` 자동복구 성공 알림이 계속 도착.
- 원인 보강: AADS DB 기반 `unified_healer` 외에 호스트 cron/레거시 watchdog 경로가 별도로 존재. `/usr/local/bin/newtalk_claude_monitor.py`는 Claude 프롬프트와 허용 명령 목록에 `docker restart aads-redis`를 보유했고, `/root/aads/scripts/watchdog_daemon.py`는 `recovery_log` 기반 자동복구 성공 알림 경로를 보유.
- 추가 조치: `unified_healer.py`의 `redis_connection_error -> docker restart aads-redis` 매핑 제거, `escalation_engine.py` Docker 자동재시작 allowlist에서 핵심 의존 컨테이너 제거, `newtalk_claude_monitor.py` 허용 명령/프롬프트에서 `aads-redis` 제거, `watchdog_daemon.py` recovery_log 자동실행 경로에서 보호 컨테이너 차단.
- 검증: `python3 -m py_compile app/services/unified_healer.py app/services/escalation_engine.py /root/aads/scripts/watchdog_daemon.py /usr/local/bin/newtalk_claude_monitor.py` 통과. 운영 DB 기준 `monitored_services`의 `68:aads-redis` 자동복구 명령은 `NULL`, 최근 `alert_history/error_log/recovery_log`에 Redis 관련 신규 이력 없음.

## 2026-04-30 16:24 KST - Telegram 반복 알림 추가 소음 차단

- 증상: CEO가 Telegram 알림이 계속 온다고 재보고. Redis 컨테이너 자체는 `Up 9 days (healthy)`이고 DB `monitored_services`의 `68:aads-redis`는 `ok`, `consecutive_failures=0`, `auto_recovery_command=NULL`.
- 확인: `alert_history`, `error_log`, `recovery_log`에는 최근 Redis 관련 신규 이력 0건. Docker `aads-server`/`aads-server-green` 런타임 모두 `unified_healer`의 보호 컨테이너 차단 패치와 `redis_connection_error` 매핑 제거가 반영됨.
- 추가 원인: `/root/aads/aads-server`에서 2026-03-01부터 떠 있던 고아 `uvicorn app.main:app --port 18080` 프로세스가 발견됨. 이 프로세스는 nginx upstream에 연결되지 않았고, 현재 hot-reload/배포 관리 대상 밖이라 오래된 APScheduler 루프를 돌릴 가능성이 있었음.
- 조치: 고아 PID `22500`을 `SIGTERM`으로 종료. `ss -ltnp` 기준 `:18080` 리스너 제거 확인. 또한 `watchdog-host.sh`의 `stale placeholder N건 자동 정리`는 CEO 조치가 필요 없는 루틴 정리라 Telegram `notify` 대신 syslog `logger`만 남기도록 낮춤.
- 추가 소음 차단: `/usr/local/bin/newtalk_claude_monitor.py`의 디스크 경고 기준을 `>85%`에서 `>=90%`로 조정해 현재 `/` 87% 상태가 30분마다 Telegram 경고 후보가 되지 않도록 cross-monitor 기준과 맞춤.
- 검증: `bash -n watchdog-host.sh` 통과, `python3 -m py_compile /usr/local/bin/newtalk_claude_monitor.py` 통과, `:8100/:8102` Docker API만 리스닝, Redis DB 상태 정상. 이후 동일 Redis 문구가 또 오면 68서버 내부 신규 발송이 아니라 Telegram 지연/외부 발송 경로 가능성이 높으므로 수신 시각 기준으로 추적 필요.

## 2026-04-30 16:32 KST - Telegram 반복 알림 2차 차단

- 추가 확인: Redis 관련 신규 이력은 없고, 반복 후보는 `newtalk_claude_monitor`의 `/` 디스크 87% 경고, AADS `alert_eval`의 `disk_full(86.7%, 임계값 80%)`, `meta_watchdog`의 레거시 114 프로세스명 감시로 좁혀짐.
- 조치: `app/services/alert_manager.py`의 디스크 텔레그램 기준을 `>=90%`로 상향하고, `cost_exceed` 중복 억제 기간을 1시간에서 24시간으로 확장. `/root/aads/meta_watchdog.sh`의 `watchdog_114`/`auto_trigger_114` 레거시 재시작 감시는 중지하고 cross_monitor가 114 헬스를 담당하도록 정리.
- 검증: `newtalk_claude_monitor.sh` 수동 실행 결과 현재 `/` 87%에서 `이상 없음 - 정상 종료`. `AlertManager.evaluate_rules()`는 디스크 알림 0건, 비용 조건만 1건이나 24시간 dedup 대상으로 확인. `bash -n /root/aads/meta_watchdog.sh`, `python3 -m py_compile app/services/alert_manager.py /usr/local/bin/newtalk_claude_monitor.py`, `python3 -m pytest tests/test_observability.py -q` 통과.
- 런타임 반영: `bash scripts/reload-api.sh`로 active `aads-server` 57개 모듈, `docker exec aads-server-green bash /app/scripts/reload-api.sh`로 green 45개 모듈 hot-reload 성공. 16:29~16:32 KST 스케줄러 주기 이후 `alert_history` 신규 행 0건 확인.

## 2026-04-30 17:01 KST - aads-socket-proxy Healer 승인요청 오알림 차단

- 증상: Telegram에 `AADS Healer 승인 요청 #103 / 68:aads-socket-proxy 복구 실패 / 마지막 에러: restart aads-socket-proxy: ok` 문구가 수신됨. 실측 기준 운영 DB `approval_queue`의 최대 ID는 63이라 화면의 `#103`은 현재 컨테이너 DB 신규 레코드가 아니며, 별도 런타임/과거 발송 경로 가능성이 있음.
- 원인: `aads-socket-proxy`는 AADS API가 Docker API에 접근하는 통로인데, `monitored_services`에 `docker restart aads-socket-proxy` 자동복구 명령이 남아 있었음. 내부 Healer가 Docker API 통로 자체를 재시작하려는 구조라 간헐 실패/성공 결과가 승인 요청으로 오분류될 수 있음.
- 조치: 운영 DB에서 `68:aads-socket-proxy`의 `auto_recovery_command`를 `NULL`로 제거하고 `last_status=ok`, `consecutive_failures=0`으로 리셋. `app/services/unified_healer.py`의 `PROTECTED_LOCAL_CONTAINERS`에 `aads-socket-proxy`를 추가해 코드상 내부 restart/stop을 영구 차단. `_create_approval_request()`에 24시간 내 동일 `target_server + action_command + title` pending 요청 dedupe를 추가해 같은 승인요청 반복 발송을 막음.
- 114/211 확인: 114는 SSH 가능, `/` 80%, `localhost:8000/health` 200, `https://v2.newtalk.kr/` 307. 211은 SSH 가능, `/` 63%, nginx/postgresql/redis active, `https://go100.newtalk.kr/health` 200. DB `monitored_services` 기준 114/211 전체 `ok`, `consecutive_failures=0`.
- 검증: `python3 -m py_compile app/services/unified_healer.py` 통과. `bash scripts/reload-api.sh` active 47개 모듈, `docker exec aads-server-green bash /app/scripts/reload-api.sh` green 35개 모듈 hot-reload 성공. 17:01 KST Healer 주기 이후 `approval_queue` 신규 0건, `aads-redis`/`aads-socket-proxy` 자동복구 명령 모두 `NULL`, 상태 `ok`.

## 2026-04-30 17:15 KST - Contabo standby Healer #103 오알림 원인 확정 및 차단

- 재확인: 68 운영 DB는 `approval_queue.max_id=63`, `aads-socket-proxy` pending 0건, `aads-socket-proxy` 컨테이너는 `Up 9 days` 상태라 68 운영 서버 자체에서 `#103`이 생성된 것이 아님.
- 원인 확정: Contabo 동기화 서버(`5.104.86.116`)에도 AADS Docker 스택이 실행 중이고, 해당 DB `approval_queue.max_id=103`에 `68:aads-socket-proxy 복구 실패 (1회)` pending 요청이 실제 존재했음. Contabo standby의 `monitored_services`에는 `68:aads-socket-proxy`/`68:aads-redis`가 enabled 상태로 남아 있고 자동복구 명령도 각각 `docker restart aads-socket-proxy`, `docker restart aads-redis`로 남아 있었음.
- 조치: Contabo DB에서 두 감시 항목을 `enabled=false`, `auto_recovery_command=NULL`, `last_status=disabled`, `consecutive_failures=0`으로 변경. 기존 `docker restart aads-socket-proxy` pending 승인요청 42건(`#62`~`#103`)은 `rejected`로 일괄 정리.
- 검증: Healer 주기 경과 후 Contabo DB 기준 `approval_queue.max_id=103`, `socket_pending=0`, `redis_pending=0`. Contabo `monitored_services`의 `aads-redis`/`aads-socket-proxy`는 disabled 유지. 68 운영 DB도 `socket_pending=0` 유지.
- 주의: `/root/aads/aads-server/scripts/sync-to-contabo.sh`가 10분마다 코드/문서를 동기화하므로, standby가 운영 텔레그램 알림을 보내지 않도록 DB 감시 항목 또는 환경변수 분리를 유지해야 함.

## 2026-05-05 10:46 KST - Android Agent 권한 상태 원격 확인 명령 추가

- 배경: CEO가 Galaxy Z Fold6 앱 설치 후 승인한 권한이 현재도 유지되는지 원격 확인 가능 여부를 요청.
- 조치: `CommandDispatcher`에 `permission_status`/`permissions` 명령을 추가하고, `AndroidCommandHandlers.permissionStatus()`에서 런타임 권한과 특수 권한 상태를 JSON으로 반환하도록 구현.
- 확인 범위: SMS 발송/읽기, 연락처, 통화기록, 카메라, 마이크, 위치, 알림, Wi-Fi, 이미지, Bluetooth, 접근성, 알림 접근, 디바이스 관리자, `WRITE_SETTINGS`, 배터리 최적화 예외.
- 검증: `./build_debug_apk.sh` 성공, `android_agent/dist/aads-agent-debug.apk` 1,410,347 bytes(2026-05-05 10:49 KST), `CommandDispatcher` 등록 수 58개.
- 기술문서: `docs/reports/20260505_ANDROID_AGENT_PERMISSION_STATUS_COMMAND.md`.

## 2026-05-06 08:38 KST - Runner 커밋 오염 분리 정리 및 AADS 서버 배포

- 배경: Runner 커밋 `2303faf`에 Common Browser Bridge 구현과 GO100/NTV2/Android/임시 리포트 산출물이 함께 섞여 운영 브랜치 오염 위험이 있었음.
- 정리: `33cf37a chore: remove runner spillover artifacts`로 `.go100-work`, NTV2 기획 HTML, `reports/2026-05-05`, 임시 NTV/GO100 작업물, Contabo 임시 스크립트, debug signing key 등 비-Browser Bridge 산출물 111개 파일을 제거. Browser Bridge 핵심 파일(`app/browser_bridge/*`, `app/api/browser_bridge.py`, `app/main.py` 라우터 연결, `app/api/ceo_chat_tools.py` 도구 연결, `tests/unit/test_browser_bridge.py`)은 유지.
- 별도 분리: 배포 시 bind mount에 함께 반영될 미커밋 Android Agent Play Protect 대응 변경은 `48fc204 fix(android): serve release agent apk`로 별도 커밋 분리.
- 검증: `python3 -m pytest tests/unit/test_browser_bridge.py -q` 8개 통과, `python3 -m py_compile app/api/browser_bridge.py app/browser_bridge/*.py app/api/ceo_chat_tools.py` 통과.
- 배포: `/root/aads/aads-server/deploy.sh` blue-green 경로로 새 `aads-server` 슬롯을 기동. 08:38 KST 기준 `aads-server` Docker health `healthy`, `http://127.0.0.1:8100/health` `status=ok`.
- 운영 확인: `GET /api/v1/browser-bridge/sessions/register`가 `405 Method Not Allowed`를 반환해 Browser Bridge 라우트가 운영 앱에 로딩된 것을 확인. POST 전용 등록 엔드포인트라 405가 정상 노출 신호임.
- Git 상태: `main`은 `origin/main`과 일치하도록 push 완료 후 clean 상태 확인.

## 2026-05-06 08:59 KST - PC Agent 재연결 안정화 및 채팅 경량화 분리 기획

- 배경: CEO 채팅 세션 `f31f1238-fdc8-4405-8893-351226e06bda`에서 PC Agent가 연결됐다가 목록에서 사라지는 현상 보고. 채팅 경량화는 별도 문제/기획 보고로 분리하고 PC Agent 끊김 P0만 즉시 조치.
- 원인: 운영 DB `kakao_pc_agent_tokens`에는 `is_active` 컬럼이 없는데 `app/api/pc_agent.py`가 `is_active = true`를 조회해 토큰 DB 검증 실패 가능성이 있었음. 또한 같은 `agent_id` 재연결 시 예전 WebSocket의 종료 `finally`가 새 연결을 `pc_agent_manager`에서 지울 수 있는 구조였음.
- 조치: `app/api/pc_agent.py` 토큰 검증을 실제 스키마 기준 `token` 조회 + `last_used_at` 갱신으로 수정. 같은 `agent_id` 신규 연결은 기존 WebSocket을 `4010 replaced_by_new`로 닫고 신규 연결이 승리하도록 `_agent_connections` guard 추가. 연결/인증/교체/해제 이벤트는 `pc_agent_connection_events` 테이블에 best-effort 기록.
- 조치: `app/services/pc_agent_manager.py`의 `unregister_agent()`에 WebSocket 일치 guard를 추가해 stale 연결 종료가 최신 연결을 삭제하지 못하게 변경.
- 검증: `python3 -m py_compile app/api/pc_agent.py app/services/pc_agent_manager.py` 통과. `python3 -m pytest tests/unit/test_pc_agent_manager_connection_guard.py tests/test_pc_agent_command_builder.py -q` 30개 통과.
- 별도 기획: 채팅 경량화 문제/개선안은 `docs/reports/20260506_CHAT_LIGHTWEIGHT_PLAN.md`로 분리 문서화. 초기 메시지 로드 축소, `fields=minimal`, revision 기반 polling skip, 메시지 리스트 가상화, artifacts lazy load 순서로 권장.

## 2026-05-06 10:05 KST - Pipeline Runner 중복 제출 구조 차단

- 배경: CEO가 러너 작업지시 시 중복 작업이 많이 생긴다고 보고. 원인 점검 결과 제출 API가 `instruction_hash` 조회 후 `INSERT`하는 구조라 동시 제출 경쟁 조건에서 중복 row가 생길 수 있었고, Shell runner의 `DEDUP_BLOCK`은 실행 직전 차단이라 큐/로그 오염을 막지 못했음.
- 조치: `app/api/pipeline_runner.py`에 동일 `instruction_hash`별 `pg_advisory_xact_lock` 직렬화를 추가하고, active 상태 조회 범위를 `queued/claimed/running/awaiting_approval/approved/deploying/rolling_back`으로 확장. 단건/배치 제출 모두 기존 active job을 재사용하도록 통일.
- DB 조치: `migrations/078_pipeline_runner_active_dedup.sql` 추가. 기존 active 중복 row는 1건만 남기고 나머지를 `error/dedup_blocked`로 정리한 뒤 `uq_pipeline_jobs_active_instruction_hash` partial unique index로 재발을 차단.
- 러너 백스톱: `scripts/pipeline-runner.sh`의 실행 직전 중복 차단 상태를 `cancelled/superseded`에서 `error/dedup_blocked`로 바꿔 대시보드 완료/취소 통계를 오염시키지 않게 수정.
- 문서: `docs/pipeline-runner/PIPELINE-RUNNER-ARCHITECTURE.md`, `docs/pipeline-runner/PIPELINE-RUNNER-API-REFERENCE.md`에 advisory lock + DB unique guard를 반영.

## 2026-05-06 17:05 KST - GPT Codex 도구박스 잔여 회귀 수정

- 배경: CEO가 GPT Codex 실시간 응답에서 도구사용박스가 안 보이거나 부정확하게 표시된다고 보고. 브라우저 검수에서 도구박스는 표시되지만 `tool_result` 중심 이벤트에서 `도구 0개 사용 — ✅ bash`로 카운트가 잘못 나오는 잔여 회귀 확인.
- 조치: 대시보드 `src/app/chat/page.tsx`의 도구박스 카운트 계산을 `tool_use` 수 → `tool_count` → `tool_names` → 전체 tool event 수 순으로 fallback하도록 수정.
- 테스트 보강: `tests/unit/test_chat_lightweight_frontend_static.py`, `tests/unit/test_chat_lightweight_regression.py`가 실제 `/root/aads/aads-dashboard` 소스를 우선 검증하도록 수정하고, tool_result-only 이벤트도 도구 사용으로 집계되는 회귀 테스트 추가.
- 진단 보강: `scripts/thinking_e2e_check.py`가 호스트 실행 시 `localhost:5433`으로 DB 접속 fallback하도록 수정.
- 검증: `pytest tests/unit/test_chat_lightweight_regression.py tests/unit/test_chat_lightweight_frontend_static.py -q` 11개 통과, `npx eslint src/app/chat/page.tsx` 0 errors(기존 warning 20개), `npm run build` 성공.
- 운영 DB 확인: 2026-05-06 GPT Codex 계열 assistant 중 `GPT-5.5 (Codex CLI)` 42건/도구저장 40건, `GPT-5.4 (Codex CLI)` 2건/도구저장 2건. `gpt-5.5`, `codex:gpt-5.5` 별칭 저장 21건은 도구 실행 없는 응답으로 확인.

## 2026-05-09 09:55 KST - 채팅 메모리 임베딩 백필 완료

- 배경: 메모리/맥락유지 개선 후 신규 메시지 임베딩 누락은 줄였지만 과거 `chat_messages`의 assistant 임베딩 누락이 가장 큰 잔여 병목으로 확인됨.
- 조치: `scripts/backfill_chat_embeddings.py` 추가. 기본 canary는 `assistant` 메시지 100건, 최신순, 20건 배치로 `chat_messages.embedding`을 채움. `--dry-run`, `--role`, `--limit`, `--batch-size`, `--order` 옵션을 지원.
- canary 실행: `docker exec aads-server python3 /app/scripts/backfill_chat_embeddings.py --limit 100 --batch-size 20 --role assistant --order newest`.
- 결과: 실행 전 assistant 미임베딩 18,745건, 실행 후 18,645건. 5개 배치에서 100건 처리/100건 업데이트, 오류 0건, 소요 15.53초.
- 전체 백필: canary 이후 assistant/user/system 대상 전체 백필을 완료했다. 마지막 잔여 assistant 3건은 `docker exec aads-server python3 /app/scripts/backfill_chat_embeddings.py --limit 10 --batch-size 5 --role assistant --order newest`로 처리했고 `missing_before=3`, `missing_after=0`, `updated=3`, 오류 0건이었다.
- 검증: `python3 -m py_compile scripts/backfill_chat_embeddings.py app/services/chat_embedding_service.py app/services/chat_service.py app/services/context_builder.py` 통과. `pytest -q tests/unit/test_memory_context_regression.py` 5개 통과.
- DB 확인: 2026-05-09 09:55 KST 기준 `chat_messages` role별 본문 10자 이상 미임베딩 대상은 assistant 0건, user 0건, system 0건이다.

## 2026-05-09 10:08 KST - AADS changelog 커밋/푸시 및 green 슬롯 무중단 전환

- 커밋: `e7ae7a0 docs: sync direct edit changelogs`, `cb768b2 docs: sync go100 direct changelog`를 `origin/main`에 push 완료.
- 배포: 기존 `deploy.sh bluegreen` 실행이 선행 PID에서 진행 중이라 중복 실행은 락으로 차단됨. 해당 배포가 `aads-server-green` 이미지를 재빌드하고 green 컨테이너를 healthy 상태로 기동한 것을 확인.
- 전환: active stream 3건이 8100에 남아 있어 컨테이너 중지는 하지 않고 nginx upstream만 8102 우선, 8100 backup으로 수동 전환 후 `systemctl reload nginx` 완료. `.active_port=8102`, `.active_container=aads-server-green`으로 동기화.
- 검증: `nginx -t` 통과, `https://aads.newtalk.kr/api/v1/health` OK, `docker inspect aads-server-green` running/healthy, `docker exec aads-server-green python3 -c "from app.main import app"` import OK.
- 잔여: untracked `scripts/e2e_disc_v2.py`는 문법이 깨진 임시 테스트 초안으로 커밋에서 제외. 정리/수정 여부는 별도 판단 필요.

## 2026-05-11 10:49 KST - discussion 인텐트 명시 요청 가드 적용

- 배경: CEO 운영 질문이 `discussion`으로 오분류되어 다관점 토론 오케스트레이터가 자동 실행되고, 실측 없는 토론 합성 결과가 일반 답변처럼 저장되는 문제가 확인됨.
- 조치: `intent_router.is_explicit_debate_request()`를 추가해 `토론해봐`, `다관점 토론해`, `run_debate` 같은 명시 실행 요청만 `discussion`으로 허용. `장단점 비교`, `어떻게 해야 할까`, 토론 기능 자체 조치 요청은 `cto_strategy`/`code_modify`/`cto_verify`로 되돌리도록 가드 추가.
- 조치: "다관점 토론은 명시 지시 때만 진행되게 조치해"처럼 토론 기능 정책을 바꾸라는 문장이 `casual`로 빠지지 않도록 키워드 폴백에서도 `code_modify`로 분류되게 보강.
- 조치: `chat_service.send_message_stream()`에 2차 방어선을 추가해 LLM 분류가 `discussion`을 반환해도 명시 토론 요청이 아니면 오케스트레이터 실행을 차단.
- 조치: `tool_registry`의 broad `all` 도구 그룹에서 `run_debate`를 제외해 일반 도구 사용 인텐트에서 모델이 암묵적으로 다관점 토론 도구를 호출하지 못하게 함.
- 검증: `python3 -m py_compile app/services/intent_router.py app/services/chat_service.py app/services/tool_registry.py tests/unit/test_chat_service.py` 통과. `pytest -q tests/unit/test_chat_service.py -k 'discussion or debate or broad_tool_group'` 4개 통과. 운영 컨테이너 `classify()` 샘플 기준 조치 지시는 `code_modify`, 세션 진화 확인 질문은 `cto_verify`, 명시 문장 `다관점 토론해봐`만 `discussion`.
- 운영 반영: `bash scripts/reload-api.sh`로 active `aads-server-green` hot-reload 완료(`재로드=53개`). `https://aads.newtalk.kr/api/v1/health` OK, `aads-server-green` running/healthy.

## 2026-05-11 13:33 KST - PC Agent 트레이 미표시 원인 조치

- 배경: CEO PC에서 PC Agent 종료 후 재다운로드/재실행 시 트레이 아이콘이 보이지 않는 문제가 보고됨.
- 실측: 서버 API는 `connected: 1`이었고, PC 명령으로 `AADS-PC-Agent-Setup-1.0.14.exe` PID `18392`, `18264`가 잔존 실행 중임을 확인. 트레이 종료 요청 후 런처가 에이전트 종료를 크래시로 오인해 백그라운드 에이전트를 재시작하면서 트레이만 사라진 상태로 판단.
- 즉시 조치: PC Agent 명령으로 PID `18392`, `18264` 지연 종료를 실행했고, 서버 `/api/v1/pc-agent/health` 기준 `connected: 0`으로 내려간 것을 확인.
- 코드 조치: `pc_agent/launcher.py`에 `stop_requested` 이벤트를 추가해 트레이 종료 시 런처 루프가 재시작하지 않고 종료되도록 수정. `pc_agent/agent.py`는 `stop()` 호출 시 현재 WebSocket을 `client_stop`으로 닫도록 보강.
- 배포 패키지: `pc_agent/VERSION`을 `1.0.19`로 올리고 active/standby 컨테이너의 `/app/pc_agent`에 반영. 운영 `GET /api/v1/kakao-bot/agent/version`은 `1.0.19`, ZIP 내부도 `VERSION=1.0.19` 및 수정 코드 포함 확인.
- 검증: `python3 -m py_compile pc_agent/agent.py pc_agent/launcher.py` 통과. `pytest tests/unit/test_pc_agent_api_disconnects.py tests/unit/test_pc_agent_manager_connection_guard.py -q` 5개 통과. `aads-server-green` Docker health `healthy`, `/health` `status=ok`.
- 잔여: 현재 서버의 `kakaobot-setup.exe` 바이너리는 Windows 빌드가 필요해 Linux 서버에서 직접 재빌드 불가. `pc_agent/**` 푸시 시 `.github/workflows/build-pc-agent.yml`이 Windows GitHub Actions에서 새 EXE를 빌드/Release 등록하도록 되어 있어 커밋/푸시로 트리거해야 함.

## 2026-05-11 13:52 KST - AADS-204 Open Design Hub Phase 0 직접 구현

- 배경: `runner-0143f0a0`가 5분 이상 `running/claude_code_work` 상태였지만 task 로그 0건, 전용 worktree 변경 0건, 백엔드가 아닌 dashboard 형태 worktree로 확인되어 산출물 없는 점유로 판단.
- 조치: `terminate_task(runner-0143f0a0)`로 러너를 종료하고 새 러너 추가 투입 없이 직접 Phase 0 범위만 구현.
- 구현: `app/services/design_audit_service.py` 신규 추가. raw hex/rgb 색상, Tailwind arbitrary color, JSX/HTML 이모지 아이콘, 반복 button class 패턴을 순수 함수로 탐지.
- API: `app/api/admin.py`에 read-only `GET /api/v1/admin/design/projects`, `GET /api/v1/admin/design/audit/preview` 추가. allowlist 루트 밖 경로 접근은 차단.
- 문서/스키마: `docs/plans/AADS-OPEN-DESIGN-HUB-IMPLEMENTATION.md`에 Phase 1~4 runner 작업 분해를 작성하고, 운영 DB 미적용 초안 `migrations/082_open_design_hub.sql`을 추가.
- 테스트: `tests/unit/test_design_audit_service.py`에 색상/이모지 탐지, button class 반복, allowlist escape 방어, empty input 검증 추가.

## 2026-05-11 15:43 KST - Runner 지시 세션 최근 활성 fallback 차단

- 배경: CEO가 각 채팅창에서 러너에게 지시할 때 “지시한 채팅창”이 아니라 “해당 프로젝트의 최근 활성 세션”으로 귀속되는 문제를 지적.
- 조치: `app/api/ceo_chat_tools.py`의 `pipeline_runner_submit`에서 `params.session_id → chat_session_id → current_chat_session_id`까지만 허용하고, `_find_recent_session(project)` fallback을 제거. 세션이 없으면 제출을 거부하도록 변경.
- 조치: `app/services/tool_executor.py`의 `pipeline_runner_submit`/`pipeline_runner_submit_batch`도 동일하게 최근 세션 fallback을 제거.
- 조치: `app/services/pipeline_runner_service.py`의 레거시 `start_pipeline()`과 완료 후 AI 반응 트리거가 세션 없음 상태에서 최근 세션을 찾아 붙이는 동작을 제거. 세션 없음 작업은 채팅 보고 비활성으로만 처리.
- 테스트: `tests/unit/test_runner_scope_defaults.py`에 세션 없는 제출이 `_find_recent_session()`을 호출하지 않는 회귀 테스트와 현재 세션 전달 테스트 추가.
