# AADS 채팅창 기능 전수 분석 보고서

- 작성 시각: 2026-04-28 12:14 KST
- 분석 기준: 로컬 워크트리 현재 상태 + 운영 Docker/DB 실측
- 저장 위치: `docs/reports/20260428_CHAT_FEATURE_FULL_AUDIT.md`
- 분석 범위: FastAPI 채팅 API, SSE 스트리밍/복구, LLM 라우팅, 도구 실행, 프롬프트/메모리, Next.js 채팅 UI, 파일/아티팩트/모델/역할/인터럽트 기능

## 1. 결론

AADS 채팅창은 단순 채팅 UI가 아니라 "CEO 입력 -> 세션/실행 DB 기록 -> 컨텍스트 빌드 -> 인텐트/모델 라우팅 -> LLM/도구 루프 -> SSE/Redis replay -> 메시지/아티팩트/메모리 저장"까지 연결된 운영 콘솔입니다. 핵심 스트리밍 안정화 패치(Patch A/B/C)는 코드상 반영되어 있으며, 재진입 시 `streaming-status.execution_id`가 비어도 `activeSession.current_execution_id`로 execution SSE attach를 시도합니다.

다만 사용자 체감 관점에서는 아직 세 가지 구조 문제가 남아 있습니다. 첫째, 프론트 채팅 페이지가 5,800줄대 단일 컴포넌트라 스트리밍/폴링/세션전환 상태가 서로 얽혀 회귀 위험이 큽니다. 둘째, 백엔드 `get_streaming_status()`의 메모리 상태 경로는 `_active_bg_tasks`만 살아 있고 `_streaming_state.execution_id`가 비어 있는 경우 여전히 빈 execution 정보를 반환할 수 있어 프론트 Patch C에 의존합니다. 셋째, 이미지 생성은 도구(`generate_image`)와 별도 모달이 공존하지만, "일반 입력문을 자연어로 감지해 즉시 이미지 생성"하는 전용 UX는 아직 완성 상태로 보기 어렵습니다.

## 2. 실측 현황

### 2.1 운영 컨테이너

2026-04-28 12:14 KST 기준 `docker ps`/`docker inspect` 결과입니다.

| 컴포넌트 | 상태 | 근거 |
|---|---:|---|
| `aads-server` | healthy, 포트 8100 | `docker ps`, `docker inspect` |
| `aads-dashboard` | healthy, 포트 3100 | `docker ps`, `docker inspect` |
| `aads-dashboard-green` | healthy, 포트 3101 | `docker ps`, `docker inspect` |
| `aads-postgres` | healthy, 6일 이상 실행 | `docker ps` |
| `aads-litellm` | healthy, 6일 이상 실행 | `docker ps` |
| `aads-redis` | healthy, 6일 이상 실행 | `docker ps` |

`GET http://localhost:8100/api/v1/health`는 `status=ok`, `graph_ready=true`, `docker_connected=true`를 반환했습니다.

### 2.2 DB 카운트

운영 PostgreSQL 컨테이너에서 직접 조회했습니다.

| 항목 | 수치 | 출처 |
|---|---:|---|
| 워크스페이스 | 20 | DB 조회 |
| 세션 | 66 | DB 조회 |
| 메시지 | 36,674 | DB 조회 |
| 아티팩트 | 12,344 | DB 조회 |
| 실행 중 execution | 1 | DB 조회 |
| streaming placeholder | 1 | DB 조회 |

실행 중 execution 1건은 `AADS-002[기능개선]` 세션의 `394c20ba-8486-4606-bd17-0a11e943ce33`이며, 조회 시점 idle 약 6초였습니다.

### 2.3 Git/worktree 상태

서버 repo는 13개 tracked 파일과 2개 신규 migration 파일이 미커밋 상태입니다. 대시보드 repo는 `src/app/chat/page.tsx`, `src/app/chat/types.ts` 2개 파일이 미커밋 상태입니다. 본 문서는 현재 워크트리 기준 분석이며, 기존 변경은 수정하지 않았습니다.

## 3. 전체 아키텍처

```
CEO 입력
  -> Next.js /chat page.tsx sendMessage()
  -> POST /api/v1/chat/messages/send
  -> chat_service.send_message_stream()
      1. 첨부/URL/멘션/HTML edit context 처리
      2. user message 저장 + chat_turn_executions 생성
      3. context_builder: Layer1/2/3/D + 메모리 + Auto-RAG + artifact context
      4. semantic cache / contradiction detector / intent classify 병렬 실행
      5. PromptCompiler 5-Layer asset 적용
      6. model_selector.call_stream()
          - Claude/LiteLLM/Codex/Gemini route
          - Tool Use loop
          - heartbeat / retry / interrupt
      7. Output Validator + Response Critic
      8. assistant message 저장 + artifact 추출 + memory/evaluation background task
  -> with_background_completion()
      - Queue 기반 SSE consumer
      - producer는 client disconnect 후에도 계속 실행
      - Redis Stream publish
      - interim placeholder DB 저장
  -> Frontend stream parser
      - delta/thinking/tool_use/tool_result/done
      - session switch/reconnect/polling fallback
      - artifact refresh
```

## 4. 백엔드 API 기능 전수

`app/routers/chat.py` 기준 현재 노출된 채팅 API는 다음 기능군으로 나뉩니다.

| 기능군 | 엔드포인트 | 역할 |
|---|---|---|
| 워크스페이스 | `/chat/workspaces` CRUD | 프로젝트/역할 단위 작업공간 관리 |
| 세션 | `/chat/sessions` CRUD | 대화 세션, 모델, role_key, current_execution 관리 |
| 메시지 | `/chat/messages`, `/chat/messages/send` | 메시지 목록, SSE 전송 |
| 스트리밍 상태 | `/chat/sessions/{id}/streaming-status` | 세션 재진입/폴링/partial 복구 |
| execution replay | `/chat/executions/{id}/events` | Redis Stream 기반 SSE attach/replay |
| stream resume | `/chat/sessions/{id}/stream-resume` | Last-Event-ID 또는 offset 기반 재연결 |
| last response | `/chat/sessions/{id}/last-response` | SSE 끊김 후 최종 DB 응답 복구 |
| 중단/인터럽트 | `/stop`, `/interrupt`, `/resume` | 생성 중단, 추가 지시 큐, 수동 재개 |
| 편집/재생성/분기 | message PUT/DELETE/regenerate/branch | 이전 메시지 수정, 응답 재생성, 분기 대화 |
| diff 승인 | `/chat/approve-diff` | 코드 패치 승인/거부 |
| 아티팩트 | `/chat/artifacts` CRUD/export | 보고서/코드/차트/이미지/파일/HTML 저장물 |
| Drive/files | `/chat/drive`, `/chat/files/upload` | 업로드 파일, 썸네일, 다운로드 |
| 검색/템플릿/export | search/templates/session export | 대화 검색, 템플릿, 세션 내보내기 |
| 오류/메모리 | `/chat/errors/report`, `/memory-context` | 프론트 오류 기록, 세션 메모리 조회 |

주요 코드 위치:

- `app/routers/chat.py:169`: 메시지 전송 SSE 진입점
- `app/routers/chat.py:284`: streaming-status
- `app/routers/chat.py:439`: execution events attach
- `app/routers/chat.py:458`: stream-resume
- `app/routers/chat.py:609`: stop
- `app/routers/chat.py:629`: interrupt
- `app/routers/chat.py:991`: artifact 목록
- `app/routers/chat.py:1100`: 파일 업로드

## 5. 메시지 전송 흐름

### 5.1 요청 수신

`send_message()`는 JSON과 multipart를 모두 받습니다. JSON은 `session_id/content/model_override/attachments/reply_to_id/idempotency_key`를 받고, multipart는 raw 파일을 서버에서 base64/text로 변환합니다. 파일 크기는 50MB 제한입니다.

현재 세션이 이미 스트리밍 중이면 신규 메시지를 일반 전송하지 않고 `interrupt_queue`에 넣고 `{status:"interrupt_queued"}`를 반환합니다. 즉 CEO가 답변 생성 중에 추가로 입력하면 새 턴을 강제로 시작하지 않고, 도구 루프 완료 지점에 추가 지시로 반영됩니다.

### 5.2 서버 처리 단계

`send_message_stream()`의 핵심 단계는 다음과 같습니다.

1. `current_chat_session_id` ContextVar 설정
2. `interrupt_queue.set_streaming(session_id, True)`로 스트리밍 상태 등록
3. 첨부 파일 처리
   - 이미지: Claude Vision content block
   - 동영상: Gemini 분석 후 텍스트 컨텍스트로 주입
   - PDF/text/code: ephemeral document context
4. URL 감지 후 최대 3개 URL 컨텍스트 추출
5. `@AADS`, `@KIS` 같은 프로젝트 멘션 감지
6. user message 저장, 중복/`idempotency_key` 방어
7. `chat_turn_executions` 생성 또는 기존 실행 재사용
8. workspace/session 설정 조회
9. 최근 메시지 최대 200개로 Layer3 히스토리 구성
10. `context_builder.build_messages_context()` 호출
11. `stream_start` SSE 전송
12. semantic cache / contradiction detector / intent classifier 병렬 실행
13. PromptCompiler 5-Layer assets 적용
14. LLM/model route + tool loop 실행
15. validator/critic 후 최종 저장
16. 메모리/품질/패턴/semantic cache 저장 background task 실행
17. `done` SSE 전송

## 6. SSE 스트리밍과 복구

### 6.1 일반 스트림

서버는 `with_background_completion()`으로 원본 generator를 감쌉니다. 구조는 producer/heartbeat/consumer 3개 축입니다.

- producer: `send_message_stream()` 이벤트를 계속 소비하고 Queue/Redis Stream/DB placeholder에 반영
- heartbeat task: 평시 3초, 도구 실행 중 2초 간격 heartbeat
- consumer: Queue에서 SSE를 yield하고 클라이언트 disconnect 시 종료

중요한 점은 클라이언트 연결이 끊겨도 producer는 계속 실행되어 DB 저장을 완료한다는 것입니다. 이 때문에 사용자가 세션을 이동하거나 배포 중 SSE가 끊겨도 최종 응답을 복구할 수 있습니다.

### 6.2 Redis Stream replay

producer는 execution_id가 잡히면 execution 단위 stream에 토큰을 publish합니다. 프론트는 `id:` 라인을 `lastEventIdRef`에 저장하고, 재연결 시 `/chat/executions/{execution_id}/events?last_event_id=...`로 이어받습니다.

`app/services/stream_worker.py`의 `deliver_sse()`는 Redis Stream에서 catch-up 후 XREAD blocking으로 새 토큰을 읽습니다. stream 완료 마커가 있거나 stream이 사라지면 `resume_done`을 보냅니다.

### 6.3 재진입 흐름

세션 진입 시 프론트는 먼저 `/streaming-status`를 호출합니다. `is_streaming=true`이면 메시지를 placeholder 포함 로드하고, `execution_id`가 있으면 `attachExecutionReplay()`를 붙입니다. 현재 Patch C는 `status.execution_id`가 null이어도 `activeSession.current_execution_id`로 attach합니다.

코드 위치:

- `src/app/chat/page.tsx:1867`: streaming-status 호출
- `src/app/chat/page.tsx:1891`: Patch C fallback
- `src/app/chat/page.tsx:1333`: attachExecutionReplay

### 6.4 남은 백엔드 취약점

`app/services/chat_service.py:1459` 이후 경로는 `_active_bg_tasks`가 살아 있으면 `_streaming_state`가 비어 있어도 `is_streaming=true`를 반환하고, `execution_id`는 `state.get("execution_id")`만 사용합니다. DB fallback은 router 레벨에서 status가 없거나 완료 상태일 때만 타기 때문에, 이 경로에서는 `chat_sessions.current_execution_id` fallback이 적용되지 않습니다.

프론트 Patch C가 이 문제를 우회하지만, 백엔드 자체의 응답 정합성은 아직 완전하지 않습니다. 후속 패치는 async router 레벨에서 active task 상태라도 DB `current_execution_id`를 병합하는 방식이 안전합니다.

## 7. 프론트 채팅 UI 기능 전수

`src/app/chat/page.tsx`는 현재 채팅 화면의 대부분 상태와 로직을 직접 보유합니다.

### 7.1 주요 상태

| 상태 | 의미 |
|---|---|
| `workspaces`, `activeWs` | 프로젝트/워크스페이스 |
| `sessions`, `activeSession` | 세션 목록/현재 세션 |
| `messages` | 현재 세션 메시지 |
| `artifacts`, `artifactMode`, `artifactTab` | 우측 아티팩트 패널 |
| `model`, `runtimeModels`, `modelPreferences` | 모델 선택/카탈로그 |
| `roleKey` | 세션 역할 |
| `streaming`, `streamBuf`, `thinkingBuf` | 스트리밍 표시 |
| `toolStatus`, `toolLogs`, `toolTurnInfo` | 도구 실행 UI |
| `waitingBgResponse`, `bgPartialContent` | 백그라운드 생성/복구 |
| `lastEventIdRef`, `currentExecutionIdRef` | SSE replay anchor |
| `pendingPreviewFiles`, `pendingAttachments` | 첨부/미리보기 |

### 7.2 입력 기능

`ChatInput.tsx`는 별도 컴포넌트로 분리되어 있으며 다음을 제공합니다.

- textarea auto-height
- `/` 슬래시 명령어 메뉴
- `@프로젝트` 멘션 자동완성
- IME composition 방어
- hidden screen capture bridge
- imperative handle: `getValue/setValue/clear/focus/captureNow`

### 7.3 메시지 렌더링

프론트는 메시지를 생성 시각 기준으로 정렬하고, 시스템/러너성 메시지는 채팅 본문에서 숨겨 로그/아티팩트 쪽으로 보냅니다. 연속 중복 user 메시지는 접어 표시합니다. active streaming placeholder는 `streamBuf`가 있을 때만 `streamingContent`를 넘기므로 빈 버블 회귀를 줄였습니다.

코드 위치:

- `src/app/chat/page.tsx:5065`: 메시지 정렬/중복 압축
- `src/app/chat/page.tsx:5122`: active streaming 판정
- `src/app/chat/page.tsx:5127`: 빈 버블 방지 조건 `streamBuf ? streamBuf : undefined`

### 7.4 도구/사고 표시

`sendMessage()` 직접 스트림과 `attachExecutionReplay()` 모두 `tool_use`, `tool_result`, `thinking`, `yellow_limit`, `tool_turn_limit`, `stream_reset`를 처리합니다. 이 점은 이전 Patch B의 핵심 개선입니다.

다만 같은 로직이 두 곳에 거의 중복되어 있어 이벤트 타입이 추가될 때 한쪽만 누락될 위험이 있습니다. 사용자 관점에서는 "직접 전송 중에는 보이는데 세션 재진입 때는 안 보임" 같은 회귀가 반복될 수 있는 구조입니다.

## 8. 모델/역할/프롬프트 체계

### 8.1 모델 선택

프론트는 static `MODEL_OPTIONS`와 runtime `llm_models`/`chat_model_preferences`를 합쳐 모델 선택지를 구성합니다. provider별 중복 모델 id는 `provider:model` 형태로 구분하려는 로직이 있습니다. 세션에는 `current_model`이 저장되고, user message에는 `model_override`가 `model_used`로 저장되어 재개 시 모델 복원에 사용됩니다.

### 8.2 역할 선택

현재 프론트에는 `ROLE_OPTIONS`와 `roleKey` 상태가 있고, 세션 타입에도 `role_key`가 추가되어 있습니다. 백엔드는 `chat_sessions.role_key`를 읽고 PromptCompiler에 전달합니다.

### 8.3 PromptCompiler

컨텍스트 빌더는 기본 Layer 1/2/3/D를 먼저 만들고, 일반 채팅 경로에서는 이후 `chat_service.py:3882`에서 PromptCompiler가 5-Layer prompt assets를 한 번 더 컴파일합니다.

Layer 구조:

- Layer 1: global/static system prompt
- Layer 2: project/runtime context
- Layer 3: role
- Layer 4: intent
- Layer 5: model

PromptCompiler는 provenance도 기록합니다. 이는 향후 "왜 이 답변에 어떤 규칙이 들어갔는가"를 추적하는 기반입니다.

## 9. 도구 실행 체계

### 9.1 도구 목록과 라우팅

`ToolRegistry`는 도구 그룹과 인텐트별 도구를 제공합니다. 이미지 생성, 검색, DB, 파일, 원격 명령, pipeline runner, screenshot, memory, research 등 80개 이상 기능을 Anthropic Tool Use 포맷으로 노출합니다.

### 9.2 Tool Use loop

Claude 계열 경로는 최대 tool turn을 `MAX_TOOL_TURNS` 기준으로 운영하고, wall-clock timeout은 기본 1,800초입니다. 도구 실행은 별도 `asyncio.Task`로 실행하고, 완료 전 8초마다 heartbeat를 내보내 SSE 타임아웃을 방지합니다. 긴 도구는 600초, 일반 도구는 120초 timeout입니다.

### 9.3 execution-scope tool cache 이슈

`ToolExecutor`에는 execution_id별 읽기 도구 결과 캐시가 구현되어 있습니다. 그러나 `model_selector.py`의 Claude tool loop에서는 `ToolExecutor()`를 각 LLM tool turn 처리 시 새로 생성합니다. 따라서 현재 구현은 "같은 execution 전체" 캐시라기보다 "해당 turn 안의 동일 executor 생명주기" 캐시에 가깝습니다.

이 부분은 사용자 체감 속도와 비용에 직접 영향이 있습니다. 같은 파일/DB/로그를 여러 turn에 걸쳐 반복 조회하면 캐시가 기대보다 덜 작동할 수 있습니다.

## 10. 파일/이미지/동영상/아티팩트

### 10.1 첨부 처리

프론트는 이미지/텍스트/동영상/PDF를 구분해 파일 업로드 또는 base64 attachment로 보냅니다. 백엔드는 텍스트/PDF/code를 ephemeral document context로 넣고, 이미지는 Vision block, 동영상은 Gemini 분석 텍스트로 변환합니다.

### 10.2 이미지 생성

백엔드 도구는 `generate_image`가 존재하며 `image_service.generate()`를 호출합니다. ToolRegistry 설명은 "Google Imagen 4.0 -> GPT-Image-1 폴백"입니다. 생성 결과가 Markdown image URL 형태면 artifact extractor가 `image` artifact로 저장할 수 있습니다.

현재 프론트에는 별도 이미지 생성 모달 상태(`showImageGen`, `imageGenPrompt`)와 액션칩 `"이미지생성"`이 남아 있습니다. CEO 규칙의 "모달 방식 제거", "`이미지:` 키워드 강제 제거", "자연어 자동 감지" 기준으로 보면, 도구 기반 자연어 처리는 가능하더라도 UI/UX는 아직 목표 상태와 다릅니다.

## 11. 아티팩트 패널

`ChatArtifactPanel.tsx`는 보고서/코드/차트/아젠다/작업/로그/대화응답/HTML 미리보기 탭을 제공합니다. artifact edit, copy, directive 변환, HTML iframe preview, 작업 모니터를 포함합니다.

서버는 assistant 응답에서 다음을 추출합니다.

- 코드 블록 -> code artifact
- 긴 구조화 응답 -> full_response/report
- markdown table -> table
- directive block -> report subtype directive
- markdown image URL -> image
- Mermaid -> chart
- 파일 링크 -> file
- HTML preview -> html_preview

DB 실측상 아티팩트는 12,344건으로, 채팅창이 이미 "대화 + 산출물 관리" 화면으로 쓰이고 있습니다.

## 12. 사용자 관점 현황 평가

### 잘 동작하는 부분

- 세션/워크스페이스/모델/역할을 유지하며 대화할 수 있습니다.
- 스트리밍 중 도구 실행 상황이 보입니다.
- 세션 이동/재진입 시 streaming-status와 execution replay로 이어붙이는 구조가 있습니다.
- 파일/이미지/동영상 첨부가 모델 컨텍스트에 연결됩니다.
- 긴 답변과 코드/표/이미지는 아티팩트로 분리 저장됩니다.
- 추가 지시는 스트리밍 중 interrupt로 큐잉됩니다.
- 서버 재시작/SSE 끊김에 대한 invisible recovery가 있습니다.

### 사용자가 느낄 수 있는 불편

- 첫 토큰 전 단계가 길면 "분석 중..."만 오래 보일 수 있습니다. 실제로는 context build, semantic cache, intent classify, PromptCompiler, 모델 route, LLM first token 대기 등이 모두 포함됩니다.
- 도구가 긴 경우 tool heartbeat는 오지만, 사용자는 "왜 오래 걸리는지"를 작업 단계별로 충분히 알기 어렵습니다.
- 세션 재진입 시 상태 경로가 많아 "생성 중", "응답 복구 중", "응답 확인 중" 표시가 상황별로 다르게 보일 수 있습니다.
- 이미지 생성은 채팅 자연어 흐름과 별도 모달/칩 흐름이 혼재되어 있습니다.
- 채팅창의 기능이 많아 입력 영역 주변이 복잡해질 위험이 있습니다.

## 13. 응답 지연 원인 코드 기준 분석

사용자가 "응답이 느리다"고 느끼는 구간은 크게 5개입니다.

### 13.1 TTFT 전처리

LLM 호출 전 다음 작업이 선행됩니다.

- 첨부 파일 추출/동영상 Gemini 분석
- URL 최대 3개 처리
- DB user message 저장
- execution 생성
- 최근 메시지 최대 200개 조회
- context_builder Layer 1/2/3/D 생성
- memory/Auto-RAG/workspace preload/artifact context 병렬 생성
- semantic cache/contradiction/intent classify 병렬 실행
- PromptCompiler DB assets 적용

이 중 하나라도 느리면 첫 토큰 전 대기 시간이 늘어납니다.

### 13.2 모델/라우트 대기

`model_selector.call_stream()`은 registry, governed intent policy, available model, route metadata를 확인합니다. Claude 계열은 LiteLLM/Anthropic/Codex relay 경로와 OAuth slot cooldown/rotation이 얽혀 있습니다. 429/503/529 계열은 retry heartbeat를 내보내지만 실제 응답 텍스트는 지연됩니다.

### 13.3 도구 루프

도구 실행은 일반 120초, 긴 도구 600초 timeout입니다. 도구가 여러 turn 반복되면 LLM 호출 -> 도구 실행 -> LLM 호출이 계속 반복됩니다. 사용자에게는 하나의 응답처럼 보이나 내부적으로는 여러 번의 모델 호출입니다.

### 13.4 검증/비평 재생성

Output Validator 또는 Response Critic이 실패를 감지하면 `stream_reset` 후 재생성합니다. 품질은 올라가지만, 사용자는 "응답이 사라졌다가 다시 쓰이는" 형태로 느낄 수 있습니다.

### 13.5 프론트 복구/폴링

SSE가 끊기면 execution events, stream-resume, last-response, streaming-status polling 순으로 복구합니다. 안정성은 높지만, 경로가 많아 실제 완료 후 UI 반영까지 0.3초~수 초 지연이 생길 수 있습니다.

## 14. 주요 리스크

| 우선순위 | 리스크 | 근거 | 영향 |
|---|---|---|---|
| P0 | 백엔드 active task 상태에서 execution_id null 가능 | `chat_service.py:1459` 경로 | 재진입 SSE attach 실패 가능 |
| P0 | 채팅 페이지 단일 파일 과대 | `page.tsx` 5,800줄대 | 회귀/중복 수정/상태 충돌 |
| P1 | execution tool cache 생명주기 불일치 | `ToolExecutor()` turn별 생성 | 반복 조회 속도 개선 효과 제한 |
| P1 | sendMessage/attachExecutionReplay SSE 핸들러 중복 | `page.tsx:1333`, `page.tsx:2800`대 | 이벤트 추가 시 한쪽 누락 |
| P1 | 이미지 생성 UX 목표와 구현 혼재 | modal/chip + tool 공존 | CEO 자연어 생성 요구 미달 |
| P1 | worktree 미커밋 변경 다수 | 서버 13개, dashboard 2개 | 배포/검증 범위 혼동 |
| P2 | 주석/실제 값 불일치 | cleanup 주석 90초, 코드 300초 | 운영자 오판 가능 |
| P2 | `sendMessage` 중복 조건문 | `if (!_existingMsgId) { if (!_existingMsgId) ... }` | 기능 영향은 작지만 품질 저하 |

## 15. 개선안

### P0. 스트리밍 상태 정합성 백엔드 보강

`get_streaming_status()` 또는 router fallback에서 `_active_bg_tasks`가 살아 있으면 DB `chat_sessions.current_execution_id`와 `chat_turn_executions.last_event_id`를 병합해야 합니다. 프론트 Patch C는 유지하되, API 자체가 항상 execution_id를 반환하도록 바꾸는 것이 맞습니다.

기대 효과:

- 세션 재진입 시 execution SSE attach 성공률 증가
- Patch C 같은 프론트 우회 의존도 감소
- E2E 검증 기준 단순화

### P0. SSE 이벤트 핸들러 단일화

`sendMessage()`와 `attachExecutionReplay()`의 이벤트 처리 코드를 `handleStreamEvent(ev, context)` 형태로 분리해야 합니다. 최소 이벤트 타입은 다음을 동일 처리해야 합니다.

- `stream_start`
- `delta`
- `heartbeat`
- `tool_use`
- `tool_result`
- `thinking`
- `stream_reset`
- `yellow_limit`
- `tool_turn_limit`
- `interrupt_applied`
- `done`
- `resume_done`
- `error`

기대 효과:

- 재진입/직접 스트림 UI 차이 제거
- Patch A/B/C류 회귀 감소
- E2E 테스트 작성 용이

### P1. 채팅 페이지 모듈 분리

`src/app/chat/page.tsx`를 다음 단위로 분리해야 합니다.

- `useChatBootstrap`: workspace/session 복원
- `useChatStreaming`: send/parse/reconnect/polling
- `useChatMessages`: pagination, dedupe, edit/delete/regenerate
- `useChatArtifacts`: artifact fetch/toast/filter
- `useChatModelRole`: runtime model + role sync
- `StreamEventReducer`: SSE 이벤트 -> UI state 변경

기대 효과:

- 단일 파일 수정 충돌 감소
- 회귀 테스트 단위 확보
- 세션 전환/스트리밍 동시 상태를 더 명확히 검증 가능

### P1. 사용자 체감 진행 단계 개선

첫 토큰 전에도 단계별 상태를 보여줘야 합니다.

- "입력 저장 중"
- "컨텍스트 구성 중"
- "모델 선택 중"
- "도구 준비 중"
- "모델 응답 대기 중"
- "도구 실행 중: {tool}"
- "검증 중"

현재는 주로 `분석 중...`, tool heartbeat, `응답 복구 중...`이 보입니다. TTFT가 긴 원인을 사용자가 이해하기 어렵습니다.

### P1. 이미지 생성 자연어 UX 통합

별도 모달 중심 흐름을 줄이고, 일반 입력에서 다음을 감지해야 합니다.

- "이미지 만들어줘"
- "로고 그려줘"
- "배너 이미지 생성"
- "이 장면을 그림으로"

감지 후에는 바로 `generate_image` 도구를 쓰되, 내부 prompt refinement 단계를 거쳐 고품질 프롬프트를 생성해야 합니다. 생성 결과는 채팅 메시지 + image artifact + 아티팩트 패널에 동시에 표시하는 방식이 적합합니다.

### P1. execution-scope cache 실제 전역화

ToolExecutor 캐시를 call_stream execution 전체에서 유지하려면 다음 중 하나가 필요합니다.

- `executor = ToolExecutor()`를 Claude loop 바깥에서 1회 생성
- 또는 process-level/shared LRU를 execution_id keyed로 관리

기대 효과:

- 동일 파일/DB/로그 반복 조회 감소
- 긴 분석 응답 속도 개선
- 도구 비용/로그 노이즈 감소

### P2. 채팅 E2E 회귀 테스트 고정

최소 E2E 케이스:

1. 새 메시지 전송 -> stream_start -> delta -> done -> placeholder 교체
2. 도구 사용 응답 -> tool_use/tool_result UI 표시
3. 세션 이동 후 복귀 -> streaming-status -> execution events attach
4. `execution_id=null` streaming-status -> current_execution_id fallback attach
5. SSE 중단 -> stream-resume 또는 last-response 복구
6. interrupt 입력 -> user 추가 지시 표시 -> interrupt_applied 후 큐 제거
7. 이미지 생성 자연어 -> image artifact 생성

## 16. 권장 실행 순서

1. P0 backend streaming-status DB merge 패치
2. P0 프론트 SSE event handler 단일화
3. P1 tool cache 생명주기 보정
4. P1 이미지 생성 자연어 UX 통합
5. P1 `page.tsx` hook/component 분리
6. P2 Playwright E2E 7종 고정
7. P2 worktree 미커밋 변경 정리 후 커밋/배포 매트릭스 갱신

## 17. 보고서 근거 파일

서버:

- `app/routers/chat.py`
- `app/services/chat_service.py`
- `app/services/model_selector.py`
- `app/services/context_builder.py`
- `app/services/prompt_compiler.py`
- `app/services/tool_executor.py`
- `app/services/tool_registry.py`
- `app/services/stream_worker.py`
- `app/core/interrupt_queue.py`
- `app/core/cache_config.py`
- `app/models/chat.py`

대시보드:

- `src/app/chat/page.tsx`
- `src/app/chat/ChatInput.tsx`
- `src/app/chat/ChatSidebar.tsx`
- `src/app/chat/ChatArtifactPanel.tsx`
- `src/app/chat/MarkdownRenderer.tsx`
- `src/app/chat/api.ts`
- `src/app/chat/types.ts`

## 18. 최종 판단

현재 채팅창은 기능 폭과 복구 장치가 매우 넓고, 최근 스트리밍 패치로 URL 재진입/도구 표시/빈 버블 문제는 상당 부분 개선되었습니다. 그러나 안정성을 프론트 우회에 의존하는 부분과 대형 단일 컴포넌트 구조가 남아 있어, 다음 개선의 초점은 "기능 추가"보다 "스트리밍 상태 정합성, 이벤트 처리 단일화, 사용자 체감 진행 표시"가 되어야 합니다.
