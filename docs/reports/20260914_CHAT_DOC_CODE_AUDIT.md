# 채팅창 문서·구현 대조 감사

- 일시: 2026-09-14 KST
- 범위: `docs/chat/*` 6종 + `docs/knowledge/5-LAYER-PROMPT-GOVERNANCE.md`,
  `app/routers/chat.py`, `app/services/chat_service.py`, `app/services/context_builder.py`,
  `aads-dashboard/src/app/chat/**`, `src/features/chat/**`
- 측정 근거: nginx 실트래픽 24시간, 운영 DB 실측, 컨테이너 내 실행

## 요약

문서가 말하는 것과 도는 것이 다르다. 설계는 SSE 스트리밍인데 **실제 전달 경로는 폴링**이고,
24시간 채팅 API 트래픽 **10,956MB 중 99.6%가 폴링**이다. 같은 기간 실제로 보낸 메시지는 111건.
**대화 1턴당 약 100MB**를 내려받는다.

## 1. 트래픽 — 메시지 111건에 11GB  (P0)

24시간 nginx 실측.

| 엔드포인트 | 호출 | 전송량 | 평균 |
|---|---:|---:|---:|
| `streaming-status` | 63,031 | 662 MB | 11 KB |
| `chat/messages` | 39,438 | **6,054 MB** | 161 KB |
| `sessions/{id}/todos` | 24,092 | **2,674 MB** | 116 KB |
| `memory-context` | 3,259 | **1,510 MB** | 474 KB |
| `messages/send` | **111** | 13 MB | — |
| `stream-resume` | 4 | — | — |

`stream-resume` 4회 — 문서가 1차 방어선으로 적은 경로는 사실상 안 쓰인다.
실제 복구는 `streaming-status` 폴링이 한다. **문서의 6계층 방어는 현실에서 1계층이다.**

### 1-1. `/todos` 116KB 중 93KB 가 UI 미사용

`ChatTodoItemOut.metadata` 는 상한이 없다. 실측 키 구성:

    audit             382 bytes × 15,981행
    message_excerpt   343 bytes × 12,563행

프론트가 todo 에서 읽는 필드는 `id`, `status`, `title`, `updated_at`, `created_at` 뿐이다
(`page.tsx` 실측: `item.metadata` 참조 0회). 한 세션에 미완료 todo 가 701건 쌓여 있고
`limit=100` 이 꽉 찬다.

### 1-2. `memory-context` 336KB — 상한 없는 쿼리 2건

컨테이너 실측(`ac5278a7`): 전체 336,009 bytes.

    injected_memory.observations       242,076 bytes   ← ai_observations, LIMIT 없음
    injected_memory.session_summaries   74,861 bytes   ← session_notes,   LIMIT 없음
    injected_memory.long_term_memory    17,491 bytes

현재 981행 / 588행이고 **세션이 늘수록 무한히 커진다.** 상태 표시줄 하나가 3,259회/일 이걸 받는다.

### 1-3. `/chat/messages` 161KB 중 59%가 `tools_called`

`limit=50` 실측: 본문 118,305 + `tools_called` 171,800 = 290,305 bytes.

호출 지점이 **17곳, 규격 6종**(`limit` 5/8/10/50/120 × `fields` render/minimal/미지정)이고
**11곳이 `fields` 를 안 붙인다.** 서버 기본값은 `fields=full`.
lazy hydration(`fields=minimal` + `GET /chat/messages/{id}`)은 구현돼 있지만 하루 109회만 쓰인다.

## 2. GET 이 쓰기를 한다  (P0)

    GET /chat/sessions/{id}/todos?cleanup_stale=true   ← 기본값 true

`cleanup_stale_in_progress_todos()` 가 **조회 때마다 DML** 을 돈다. 하루 24,092회.

WP04 가 `streaming-status`·`last-response`·`messages` 에서 repair DML 을 걷어내고
"GET 은 read-only" 를 원칙으로 세웠는데(`chat_read_model`, 코드로 확인됨) **`/todos` 만 누락됐다.**
원칙이 문서에만 있고 한 곳에 적용이 안 되면, 다음 사람은 그 한 곳을 보고 원칙이 없다고 판단한다.

## 3. 죽은 코드 7,864줄  (P1)

앱 라우트에서 import 를 전이적으로 따라간 도달성 분석 결과 — 전체 224개 중 49개 미도달,
채팅 관련만 43개 / 7,864줄.

**WP 현대화 모듈 5개는 참조가 0이다** (자기 테스트 파일만 참조):

    features/chat/auth/chatAuthPolicy.ts            191줄
    features/chat/observability/chatTelemetry.ts    183줄
    features/chat/composer/draftStore.ts            189줄
    features/chat/composer/uploadQueue.ts           114줄
    features/chat/rendering/renderPerformancePolicy.ts

**병렬 구현 한 벌이 통째로 미연결** (AADS-172/185 계열):

    components/chat/ChatBubble.tsx   772줄     hooks/useChatSSE.ts      624줄
    components/chat/ChatInput.tsx    455줄     hooks/useChatSession.ts  161줄
    components/chat/ChatStream.tsx   348줄     services/chatApi.ts      333줄
    components/chat/Sidebar.tsx      465줄     components/chat/ChatLayout.tsx 140줄

`components/chat/ChatInput.tsx` 와 `app/chat/ChatInput.tsx` 가 동시에 존재하고
**운영은 후자를 쓴다.** 테스트는 통과하는데 아무것도 안 쓰는 상태다.

## 4. 대시보드 사본이 2벌  (P1)

| 경로 | chat/page.tsx | 최종 커밋 | 상태 |
|---|---:|---|---|
| `aads-dashboard` | 12,865줄 | 2026-09-14 | **빌드·배포됨** (`docker-compose.prod.yml: context ../aads-dashboard`) |
| `aads-dashboard-unni` | 10,324줄 | 2026-07-24 | 빌드 안 됨. 단 **nginx 가 `/brands` 를 여기서 서빙** |

차이 6,399줄. 잘못된 사본을 고쳐도 오류가 안 난다 — 배포만 조용히 반영이 안 된다.
이번 세션에서 같은 종류의 함정(`ModelSelector.tsx` 트리셰이킹, `lib/api.ts` vs `lib/auth.ts`)을
이미 두 번 밟았다.

## 5. 임베딩 — 가져온 CLI 기록이 회수 불가  (P2)

`chat_messages.embedding` 은 실제로 검색된다 (`auto_rag._search_chat_messages`,
`chat_embedding_service.search_semantic`). 그런데 최근 3일 커버리지가 intent 별로 갈린다.

| intent | 건수 | 임베딩 |
|---|---:|---:|
| `claude_code_terminal` (assistant) | 2,398 | **0%** |
| `claude_code_terminal` (user) | 642 | **0%** |
| `stale_empty_placeholder` | 862 | 0% |
| 일반 user (intent NULL) | 312 | 100% |
| 일반 assistant (intent NULL) | 424 | 37% |

CLI 세션 기록 4,594건(452세션)은 **임베딩이 하나도 없다** — 저장은 되는데 Auto-RAG 로
꺼낼 수가 없다. 가져오기 경로가 `schedule_message_embedding` 을 안 부른다.
`backfill_embeddings()` 는 정의돼 있으나 **호출처가 없다**(`app/main.py` 의 백필은 facts 전용).

부수 확인: 로컬 임베딩 경로가 죽어 있다 —
`server_ollama_embed_failed` / `pc_agent_embed_failed: All connection attempts failed`.
폴백으로 동작은 하지만 매번 경고를 남긴다.

## 6. 문서 표류  (P2)

`docs/chat/*` 최종 수정 2026-07-15. 이후 채팅 코드 커밋 **서버 175 / 대시보드 160건**.

| 항목 | 문서 | 실제 | 배수 |
|---|---:|---:|---:|
| `page.tsx` | 4,501줄 | 12,865줄 | 2.9× |
| `ChatArtifactPanel.tsx` | 513줄 | 2,928줄 | 5.7× |
| `ChatSidebar.tsx` | 566줄 | 832줄 | 1.5× |
| 프론트 합계 | 6,509줄 | 약 18,600줄 | 2.9× |
| `chat_service.py` | 4,158줄 | 14,855줄 | 3.6× |
| 라우터 엔드포인트 | "30+" | 77 | 2.6× |
| 프론트 `sseTimeout` | 90s | **150s** | — |

`CHAT-FRONTEND-SPEC` 의 파일 목록 8개 중 `api.ts` 는 같은 이름이 3곳에 있고 문서는 어느 것인지
안 적는다. 문서만 보고 고치면 틀린 파일을 고친다.

**코드 주석도 코드와 안 맞는다** (`page.tsx:7411`):

    // 90초 비활성 타임아웃 → heartbeat(5초) + 실제 데이터 모두 리셋
    // 절대 타임아웃(300초)이 무한 연장 방지 안전망
    }, 150000);            ← 90초가 아니라 150초
    }, 3600000);           ← 300초가 아니라 1시간

## 7. "레이어" 라는 말이 두 가지를 가리킨다  (P2)

| 이름 | 위치 | 의미 |
|---|---|---|
| Layer 1~5 | `prompt_assets.layer_id`, `PromptCompiler` | 프롬프트 자산 우선순위 |
| Layer 1/2/2.5/3/4.5/D | `context_builder.py` | 컨텍스트 조립 단계 |

둘 다 "5-Layer" 로 불린다. 같은 대화에서 두 사람이 다른 것을 말할 수 있다.

## 8. 사소

- `_HISTORY_EXCLUDED_INTENTS` 에 `"runner_notification"` 이 **3번** 들어 있다
  (`chat_service.py`). 동작에는 영향 없지만 검토가 없었다는 표시다.
- `stale_empty_placeholder` 1,644행이 정리 없이 쌓여 있다. 제외 목록에도 없다.
- 소스 트리에 백업 파일 106개 / 7MB (`*.bak_aads` 등). `.dockerignore` 가 빌드 컨텍스트에서는
  빼주지만, `grep` 이 `chat_service.py.bak_ohvis` 를 먼저 잡아 잘못된 코드를 읽게 만든다
  (이번 감사 중 실제로 발생).

## 개선안

효과가 큰 것부터. 괄호는 예상 감축.

### 1순위 — 트래픽 (합계 약 9GB/일 감축, 코드 변경 작음)

| # | 조치 | 파일 | 효과 |
|---|---|---|---|
| A | `/todos` 응답에서 `metadata` 제거 (UI 미사용) | `app/models/chat.py` `ChatTodoItemOut` | 2.1 GB/일 |
| B | `memory-context` 의 `ai_observations`·`session_notes` 에 LIMIT + 캐시 | `chat_service.py:14527,14548` | 1.3 GB/일, 증가 중단 |
| C | 메시지 목록 호출 11곳에 `fields=render` 부여 | `page.tsx` 17개 호출 지점 | 3.5 GB/일 |
| D | 유휴 시 `streaming-status` 폴링 7.5초 → 20초 | `page.tsx:6340` | 0.4 GB/일 |

A·B는 서버만 고치면 되고 프론트 변경이 필요 없다. C는 호출 지점을 한 헬퍼로 모으는 게 맞다 —
17곳에 규격 6종이 흩어진 상태가 원인이다.

### 2순위 — 원칙 일관성

| # | 조치 |
|---|---|
| E | `/todos` 의 `cleanup_stale` 기본값을 `false` 로. 정리는 WP04 가 만든 fenced worker 로 이관 |
| F | 미완료 todo 상한/만료 정책 (한 세션 701건은 하네스가 회수를 안 한다는 뜻) |

### 3순위 — 회수 가능성

| # | 조치 |
|---|---|
| G | CLI 기록 가져오기 경로에서 `schedule_message_embedding` 호출 |
| H | `backfill_embeddings()` 를 주기 작업에 연결 (현재 호출처 0) |
| I | 로컬 임베딩(Ollama/PC Agent) 경로 복구 또는 경로 제거 |

### 4순위 — 표류 차단

| # | 조치 |
|---|---|
| J | `aads-dashboard-unni` 를 정리하거나, `/brands` 를 빌드되는 쪽으로 옮기고 사본 삭제 |
| K | 죽은 모듈 7,864줄 — 연결하든 지우든 결정. 지금은 "테스트 통과하는 미사용 코드" |
| L | `docs/chat/*` 의 줄 수·파일 경로를 스크립트로 생성. 손으로 적으면 또 표류한다 |
| M | 문서·코드 주석의 타임아웃 수치를 코드값으로 정정 (90s→150s, 300s→3600s) |

L 이 핵심이다. 문서를 다시 손으로 맞춰도 160커밋 뒤에 또 벌어진다.
**줄 수·엔드포인트 수·타임아웃 상수는 코드에서 뽑아 넣어야 한다.**
