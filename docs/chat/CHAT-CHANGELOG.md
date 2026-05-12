# AADS Chat 변경 이력

_v1.0 | 2026-04-02 | 최초 작성_

## 변경 이력 (최신순)

### 2026-05-12

| 커밋 | 변경 | 구분 |
|------|------|------|
| 2026-05-12 | **Last-response stale execution settlement**: `/last-response`가 `current_execution_id`의 죽은 `running/retrying` 실행 때문에 무기한 `generating=true`만 반환하지 않도록, `streaming-status`와 동일한 stale 판정으로 placeholder를 보존 응답으로 승격하거나 빈 placeholder를 정리한 뒤 최신 assistant 조회를 계속 수행 | 🐛 Backend |
| 2026-05-12 | **Chat restart resume trigger guard**: 서버 재시작 직후 `chat_turn_executions.status IN ('running','retrying')`이지만 새 프로세스 시작 전 생성된 실행은 90초 stale 대기 없이 startup scanner가 즉시 claim하도록 보강. 평시 periodic scanner는 기존 stale 기준을 유지하되 env로 조정 가능 | 🐛 Backend |
| 2026-05-12 | **Chat-embedded Design Studio**: `/chat` 입력 액션에 `디자인수정` 칩과 Design Studio 패널을 추가해 채팅 문장을 수정 카드/컨텍스트팩으로 바로 저장하고, `Context`/`Workbench`/AI 운영 지시 삽입 흐름을 제공 | ✨ Frontend+Backend |
| 2026-05-12 | **Chat final visibility guard**: 완료 직후 메시지 재조회가 assistant 저장 gap에서 로컬 최종 버블을 덮어쓰지 않도록 `mergeServerMessagesPreservingLocal()` 경로로 통일하고, `done`/`message_done`/execution replay 완료 직후 `/last-response`를 재확인해 서버 최종 assistant를 병합 | 🐛 Frontend |

Last-response stale execution settlement:
- 기존 `/last-response`는 `chat_sessions.current_execution_id`가 `running/retrying`이면 실행이 실제로 죽었는지 확인하지 않고 `generating=true`를 반환했다.
- 개선 후 5분 이상 갱신이 없거나, 첫 응답 제한 시간 이후 토큰/도구/이벤트 진행이 없는 실행은 `interrupted`로 terminalize한다.
- 의미 있는 partial이 있으면 `streaming_placeholder`를 같은 row에서 최종 assistant로 승격하고, 비어 있으면 placeholder를 삭제한 뒤 `message_count`를 보정한다.
- 이 처리는 `/streaming-status`와 `/last-response` 양쪽에서 같은 helper를 사용해 재진입, 폴링, SSE 끊김 복구 경로의 판정 차이를 줄인다.

Restart resume trigger guard:
- 기존 트리거는 `current_execution_id`가 가리키는 `running/retrying` 실행 중 `updated_at < NOW() - 90 seconds`만 자동 claim했다.
- 개선 후 startup scan은 새 API 프로세스 시작 시각보다 이전에 갱신된 실행을 5초 후 즉시 claim한다. 이 조건은 서버 재시작으로 메모리 `_active_bg_tasks`가 사라진 실행만 대상으로 삼기 위한 안전장치다.
- 운영 조정값: `AADS_EXECUTION_RESUME_STARTUP_STALE_SECONDS` 기본 15초, `AADS_EXECUTION_RESUME_STALE_SECONDS` 기본 90초.

Design Studio 채팅 내장:
- `src/app/chat/page.tsx`에서 대상 화면, 수정 요청, 허용 범위, 금지 범위, 검수 기준을 입력해 `/api/v1/admin/design/modification-requests`와 `build-context`를 바로 호출한다.
- `app/services/tool_registry.py`와 `app/services/tool_executor.py`에 `create_design_modification_request` 도구를 등록했다.
- `app/services/intent_router.py`에서 `design/design_fix` 인텐트가 도구 사용 경로를 타도록 변경했다.
- 검증: `python3 -m py_compile app/services/intent_router.py app/services/tool_registry.py app/services/tool_executor.py`, `npx eslint src/app/chat/page.tsx`, `npx tsc --noEmit --pretty false`.

실측 원인:
- 2026-05-12 07:44 KST 재조회 기준 대상 세션 `8ad08cc2-620c-4a70-8305-74a8d9b43c4e`는 `chat_messages=1285`, `streaming_placeholder=0`, `current_execution_id=NULL`이었다.
- 문제로 지목된 assistant 최종 메시지 `2851f6d1-a52a-4f3d-a650-7b14e1f918cf`는 2026-05-12 07:20:03 KST에 저장되어 있었고, DB 기준 본문 길이는 2925자였다.
- 따라서 백엔드 저장 실패가 아니라 프론트 완료 직후 재조회/폴링/로컬 placeholder 교체 경로의 표시 소실 문제로 판정했다.

검증:
- `docker exec aads-postgres psql ... chat_messages/chat_sessions` 대상 세션 실측
- `python3 -m pytest tests/unit/test_chat_lightweight_frontend_static.py -q` → 3 passed
- `npx tsc --noEmit --pretty false` → 통과
- `npx eslint src/app/chat/page.tsx` → 0 errors, existing warnings 20개
- `docker ps` / `docker inspect aads-dashboard` → 컨테이너 healthy, 2026-05-12 07:42 KST 시작
- `curl -I -s https://aads.newtalk.kr/chat` → 미로그인 기준 `/login?redirect=%2Fchat` 307
- `.active_container` / `.active_port` 파일은 없어 활성 슬롯명은 미확인

### 2026-05-06

| 커밋 | 변경 | 구분 |
|------|------|------|
| 2026-05-06 | **Chat Lightweight v2.3**: GPT Codex 실시간 도구박스 잔여 회귀 수정. `tool_result` 중심 이벤트도 도구 사용 수에 반영하고, 테스트가 실제 대시보드 소스를 검증하도록 보정 | 🐛 Frontend+Test |
| 2026-05-06 | **Chat Lightweight v2.2**: `fields=minimal` 도구 요약 메타(`has_tools/tool_count/tool_names`)와 단건 full hydrate API 추가 | 🐛 Backend |
| 2026-05-06 | 완료 assistant 버블에서 `tools_called`가 비어도 도구박스 placeholder를 표시하고, 상세 hydrate 후 기존 긴 본문을 200자 preview로 덮어쓰지 않도록 병합 가드 보강 | 🐛 Frontend |
| 2026-05-06 | 스트리밍 `tool_use/tool_result` 누적 이벤트를 final assistant 메시지에 합치고, legacy string/Codex event `tools_called` 정규화 경로 통일 | 🐛 Backend+Frontend |
| 2026-05-06 | `codex:gpt-5.5`, `gpt-5.5`, `GPT-5.5 (Codex CLI)` 별칭을 동일 Codex 실행 모델로 정규화 | 🔧 Backend |

검증:
- `pytest tests/unit/test_chat_lightweight_regression.py tests/unit/test_chat_lightweight_frontend_static.py -q` → 11 passed
- `npx eslint src/app/chat/page.tsx` → 0 errors, existing warnings only
- `npm run build` → successful

운영 확인 절차:
- 대상 세션 `b8a8651b-6226-46df-9a44-36a70e478959`에서 minimal polling 후 도구박스 placeholder → 단건 hydrate → full 도구 상세 표시 순서를 확인한다.
- 800자 이상 assistant 본문이 minimal polling 이후에도 기존 길이를 유지하는지 확인한다.

### 2026-04-30

| 커밋 | 변경 | 구분 |
|------|------|------|
| 2026-04-30 | 스트리밍 중 active API 재시작 방지: `deploy.sh code`가 active stream 감지 시 peer slot을 먼저 재시작하고 nginx/복구 오너를 전환하도록 변경 | 🐛 Deploy |
| 2026-04-30 | blue/green 복구 오너 분리: inactive 컨테이너가 DB running/retrying 실행을 가로채지 않도록 active marker/env/owner flag 적용 | 🐛 Backend |
| 2026-04-30 | 첫 토큰 지연 구간 보존: `stream_start` 직후 DB placeholder 저장, heartbeat 중 10초 주기 interim save | 🐛 Backend |
| 2026-04-30 | 끊김 후 이전 응답 반환 방지: `last-response`를 현재 execution/message 기준으로 좁히고 system-trigger turn fallback 오판 수정 | 🐛 Backend |
| 2026-04-30 | 프론트 메시지 병합 보강: streaming placeholder와 최종 assistant를 같은 render key로 병합해 사라짐/중복 버블을 줄임 | 🐛 Frontend |

검증:
- `python3 -m pytest tests/unit/test_chat_service.py -q` → 10 passed
- `python3 /tmp/aads_stream_disconnect_e2e.py` → 강제 끊김 후 `resume_done`, assistant 1개, placeholder 0개, `current_execution_id=null`, 중복 replay 없음
- 브라우저 직접 확인: `https://aads.newtalk.kr/chat#e62f3c19-5558-4f89-87bf-709c7dccd4af` 로딩, chat/session/message/streaming-status API 200, `current_execution_id=null`

운영 조치:
- 2026-04-30 19:51 KST, 진행 중이던 `deploy.sh code`가 active API를 `STOPPING` 상태로 만들며 응답 끊김을 재현했다.
- 즉시 API upstream을 `8102(aads-server-green)`으로 failover하고 `.active_container/.active_port` 및 `/tmp/aads_execution_resume_owner`를 green 기준으로 전환했다.
- `claude-relay` 재시작으로 relay semaphore timeout 패치를 런타임에 반영했다.

### 2026-04-28

| 커밋 | 변경 | 구분 |
|------|------|------|
| `b24b47f` | **BUG #3**: streaming-status DB fallback에서 tool_count/last_tool 산출 (tools_called JSON parse) | 🐛 Backend |
| `56ed27c` (dashboard) | **Patch A+B**: URL 재진입 시 SSE 도구/사고/스트리밍 누락 해결. attachExecutionReplay 18종 SSE 핸들러 + partial_content 즉시 표시 | 🐛 Frontend |

증상: `https://aads.newtalk.kr/chat#{session_id}` 같은 진행 중 세션 재진입 시 도구 카드/사고 블록/스트리밍이 보이지 않고 빈 버블만 표시되던 문제. 백엔드는 18종 SSE를 정상 발행 중이었으나 attachExecutionReplay가 1/4(delta/heartbeat/done)만 처리해 정보 드롭. 또 streaming-status가 in-memory state 없을 때 tool_count=0/last_tool=""을 하드코딩 반환해 진입 시점 도구 진행 상태도 미표시.

해결:
- Backend: tools_called JSON에서 tool_use 카운트와 마지막 도구명 산출 (running/just_completed/placeholder-only 3개 분기)
- Frontend Patch A: status.partial_content를 즉시 setStreamBuf, status.tool_count/last_tool를 즉시 setToolStatus
- Frontend Patch B: attachExecutionReplay에 stream_start/stream_reset/tool_use/tool_result/thinking/yellow_limit/model_info/sdk_*/error + done 핸들러 추가 (sendMessage 메인 루프와 동등)

영향: 다른 워커/브라우저에서 진행 중 세션을 URL로 열 때 즉시 "🔧 X 실행 중..." + 도구 카드 + 사고 블록 + 스트리밍 텍스트 모두 정상 표시. SSE-STREAMING-ARCHITECTURE.md v2.1로 버전업.

### 2026-04-24

- 운영 조치: `claude-relay` 전역 동시성은 Pipeline Runner와 별개로 관리하며, live systemd override 기준 `max_concurrent=5`로 고정했다.
- 운영 조치: relay wrapper는 `.active_container`를 읽어 blue-green 활성 API 컨테이너를 따라가도록 보강했다. 배포 직후 inactive 컨테이너를 참조하며 MCP preflight가 실패하던 리스크를 낮췄다.
- 운영 조치: active stream 계측을 `executing / visible / recovery_pending / recent_placeholders`로 재정리했고, 실제 무중단 배포 drain에서 `2 → 1 → 0` 집계를 확인했다.

### 2026-04-02

| 커밋 | 변경 | 구분 |
|------|------|------|
| `e0b896d` | **A-1~A-4 끊김 시 새 버블 생성 방지** — 같은 버블에서 부드럽게 전환, rAF 교체, 복구 UI | 🔧 Frontend |
| `62f2fe7` | **A-2 offset→cursor 통일, A-3 타이머 cleanup, C-1 스켈레톤 UI** | 🔧 Frontend |

### 2026-03 (AADS-191 Redis Stream + SSE 안정화)

| 커밋 | 변경 | 구분 |
|------|------|------|
| `cd35304` | **AADS-191 Phase4 워커분리** — Redis Stream SSE 전송 분리 + Last-Event-ID + 프론트 버퍼링 | ✨ Backend+Frontend |
| `900f6a5` | **AADS-191 Phase1 Redis Stream 토큰 버퍼링** — 서버 재시작 시 스트리밍 복구 | ✨ Backend |
| `20ad11a` | **Phase4 프론트엔드** — 토큰 버퍼링 + Last-Event-ID SSE 재연결 | ✨ Frontend |
| `0d09965` | **CEO-019 SSE 끊김방지** — 아젠다/기술문서/개선보고서 + heartbeat 256byte CF flush 패딩 | 📝 Docs+Fix |
| `817dee1` | recovered 메시지 tool UI 복원 — placeholder에 tool_events 축적 + 인라인 렌더 | 🔧 Frontend |
| `8e3e68c` | 스트리밍 버블 2개 + 끊김 후 대화 이어가기 근본 수정 | 🐛 Fix |
| `da4071b` | recovered 후 대화종료 + 중복폭발 + 고아 placeholder 근본 수정 | 🐛 Fix |
| `29eb474` | 3단계 근본 수정 — DB unique index + promote dedup + idempotency key | 🐛 Fix |
| `18eaaea` | 사용자 메시지 중복 방지(30s dedup) + recovered 연속 중복 자동 정리 | 🐛 Fix |
| `a93f112` | streaming_placeholder 숨기지 않고 자동 promote — 부분 응답 보존 | 🐛 Fix |
| `cabd5ee` | streaming_placeholder 중복 버블 근본 수정 — 스마트 promote + 15초 cleanup | 🐛 Fix |
| `1565398` | stream-resume stale response bug — message_id validation + UPDATE 방식 | 🐛 Fix |
| `1df4355` | **Invisible Recovery** — SSE 끊김 시 AI 버블 유지 + 무음 재연결 | ✨ Frontend |
| `66e7781` | maxStreamTimeout 900s→3600s (1시간) — 200+ 도구 호출 세션 대응 | 🔧 Frontend |
| `faffdba` | SSE 끊김 후 recovered 응답 멈춤 근본 수정 3건 | 🐛 Fix |
| `edc3a77` | tool UI 접기/펼치기 + 풍부한 미리보기 — details 컴포넌트 | ✨ Frontend |

### 2026-03 (채팅 기능 개선)

| 커밋 | 변경 | 구분 |
|------|------|------|
| `7b32d06` | SearXNG 우선 실행 — Gemini Grounding 체인 앞에 삽입 | ✨ Backend |
| `22e7f6a` | P1-1 대화 중 실시간 교훈 추출 — LLM 제거, 키워드/패턴 기반 | ✨ Backend |
| `d67497a` | P2-1 multimodal memory — visual_memory store and recall | ✨ Backend |
| `8f78334` | logging kwargs TypeError + context_builder intent 파라미터 호환 | 🐛 Fix |
| `5aa92b1` | 대화 잘림·페이지네이션 중복·메시지 3중 표시 버그 수정 | 🐛 Frontend |
| `197b6ff` | 채팅 메시지 사라짐 버그 — 폴링 시 기존 메시지 보존 병합 | 🐛 Frontend |
| `25f3f89` | 채팅 폴링 15초 간격 최적화 (waitingBg=false 시 skip) — CPU 과부하 방지 | ⚡ Frontend |
| `b0e2b3d` | 채팅 UI 폴링 간격 최적화 + 스크롤 점프 수정 | 🔧 Frontend |
| `96a289e` | UI: user msg 버튼 하단 이동, edit textarea 크기 수정 | 🎨 Frontend |

### Dashboard Git 이력 (src/app/chat/)

```
e0b896d fix: 채팅 끊김 시 새 버블 생성 방지 (A-1~A-4)
62f2fe7 fix: A-2 offset→cursor, A-3 timer cleanup, C-1 skeleton
20ad11a feat: Phase4 프론트 — 토큰 버퍼링 + Last-Event-ID
5aa92b1 fix: 대화 잘림/중복/3중표시
197b6ff fix: 메시지 사라짐 — 폴링 보존 병합
25f3f89 perf: 폴링 15초 최적화
b0e2b3d fix: 폴링+스크롤 점프
66e7781 fix: maxStreamTimeout 3600s
faffdba fix: recovered 응답 멈춤 3건
edc3a77 feat: tool UI 접기/펼치기
```

## 관련 보고서

| 문서 | 경로 | 내용 |
|------|------|------|
| CEO-019 SSE 개선 | `docs/reports/CEO-019-SSE-IMPROVEMENT-REPORT.md` | SSE 끊김방지 13건 수정 보고 |
| SSE 아키텍처 | `docs/knowledge/SSE-STREAMING-ARCHITECTURE.md` | 8계층 방어 기술 문서 (v2.2) |
| SSE 신뢰성 아젠다 | `docs/agenda/AADS-SSE-STREAMING-RELIABILITY.md` | SSE 안정화 아젠다 (v2.2) |

## 이슈 태그 범례

| 태그 | 의미 |
|------|------|
| ✨ | 새 기능 (feat) |
| 🔧 | 개선 (improve) |
| 🐛 | 버그 수정 (fix) |
| ⚡ | 성능 개선 (perf) |
| 🎨 | UI/UX 개선 |
| 📝 | 문서 |

## 문서 버전 이력

| 버전 | 날짜 | 변경 |
|------|------|------|
| v1.1 | 2026-04-30 | 스트리밍 중 배포 재시작 방지, blue/green resume owner 분리, DB placeholder 보존, e2e/브라우저 검증 기록 |
| v1.0 | 2026-04-02 | 초기 작성 — 2026-03~04 변경 이력 통합 |
