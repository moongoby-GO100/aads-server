# AADS Chat Lightweight Plan v2 — 맥락 이해도·진화 시스템 영향 검토 통합본

작성 시각: 2026-05-06 10:05 KST
원본: `docs/reports/20260506_CHAT_LIGHTWEIGHT_PLAN.md` (103 lines, 08:59 KST)
범위: v1 경량화 4단계가 ① LLM 맥락 이해도(컨텍스트 빌더, 메모리, RAG)와 ② 진화 시스템(embedding/quality_score/ai_observations/session_notes/reflexion/sleep-time)에 회귀를 일으키지 않는지 코드·DB 실측으로 재검토하고, 빠진 안전장치를 v2에 추가한다.

## TL;DR

- v1 Phase 1~4는 모두 **프론트 표시/폴링 경로**에 영향을 주는 변경이다.
- LLM 맥락 이해도와 진화 시스템은 모두 **DB(chat_messages, ai_observations, session_notes)를 직접 SELECT** 하므로 v1 변경에 자동 영향받지 않는다.
- 다만 v1에는 다음 5가지 안전장치가 빠져 있다. v2에서 이를 P0로 격상한다.
  1. fields=minimal payload에 `quality_score / bookmarked / edited_at / has_thinking / has_tools / is_compacted` 누락.
  2. revision bump 트리거 목록이 placeholder 위주여서 edit·bookmark·quality_score 부여·recovered 갱신을 놓침.
  3. "프론트 캐시 메시지를 LLM 입력으로 재활용" 안티패턴 차단 코드 contract 부재.
  4. fields=minimal 도입 후 어드민·검수·회고 화면에서 thinking/tools/attachments 사용처 회귀 가능.
  5. 가상화 도입 시 IntersectionObserver 기반 quality 표시·embedding 결손 표시·"원본 보기"가 누락 가능.

## 1. 현재 시스템 지도 (실측)

### 1.1 프론트 → 백엔드 → DB 실측 경로

| 경로 | 호출 위치 | 데이터 출처 | v1 영향 |
|---|---|---|---|
| `/chat/messages?session_id=...&limit=100` | `aads-dashboard/src/app/chat/page.tsx` | `app/services/chat_service.py:2686` `SELECT * FROM chat_messages` | ✅ Phase 1/2 직접 영향 |
| `/chat/streaming-status` | `page.tsx` (재진입/폴링) | Redis stream + `chat_executions` | ✅ Phase 2 직접 영향 |
| `/chat/artifacts?workspace_id=...` | `page.tsx` (세션 진입) | `chat_artifacts` | ✅ Phase 4 직접 영향 |
| `/chat/sessions/{id}/send` (LLM 호출 경로) | `chat_service.py:1859,1880` `build_messages_context(...)` | `chat_messages` 직접 SELECT (프론트 페이로드 사용 안 함) | ❌ 영향 없음 |

### 1.2 진화/맥락 시스템 데이터 경로 (모두 DB 직독, 프론트와 분리)

| 시스템 | 모듈 | 데이터 소스 | v1 영향 |
|---|---|---|---|
| 5-Layer 컨텍스트 빌더 | `app/services/context_builder.py` `build_messages_context()` | `chat_messages` SELECT + 5-Layer prompt + memory_recall + auto_rag + workspace_preload + semantic_code | ❌ 영향 없음 |
| 메모리 회수 | `app/core/memory_recall.py` | `session_notes`, `ai_observations`, `chat_messages.quality_score` | ❌ 영향 없음 |
| Auto-RAG | `app/services/auto_rag.py` | `chat_messages.embedding`(pgvector), `ai_observations.embedding` | ❌ 영향 없음 |
| Embedding | `app/services/chat_embedding_service.py` | INSERT 시점 자동 생성 | ❌ 영향 없음 |
| Quality 피드백 루프 | `app/services/quality_feedback_loop.py` | `chat_messages.quality_score` 7일 평균 | ❌ 영향 없음 |
| Fact 추출 | `app/services/fact_extractor.py` | `chat_messages.content` 직접 SELECT | ❌ 영향 없음 |
| 압축/요약 | `app/services/context_compressor.py`, `compaction_service.py` | LLM 호출 80K 토큰 기준 자동 트리거 | ❌ 영향 없음 |
| Reflexion / Sleep-Time | 백엔드 cron (백그라운드) | `chat_messages.quality_score` < 0.4 트리거, 14:00 KST 정제 | ❌ 영향 없음 |
| Workspace Preload | `app/services/workspace_preloader.py` | `ai_observations` 카테고리 필터 | ❌ 영향 없음 |

### 1.3 실측 카운트 (2026-05-06 10:01 KST)

| 항목 | 수치 | 비고 |
|---|---:|---|
| chat_messages 총합 | 39,011 | 전체 |
| embedding 보유 | 19,425 | 49.8% — 진화/RAG 핵심 자산 |
| quality_score 보유 | 9,585 | 24.6% — reflexion/feedback 입력 |
| ai_observations | 1,770 | memory_recall 입력 |
| session_notes | 766 | session_notes 카운트 |

→ 경량화 변경이 이 수치/컬럼을 건드리지 않아야 한다. v2의 모든 P0 항목은 이 컬럼들을 보존하는 방향으로 잡는다.

## 2. v1 4단계가 맥락 이해도/진화에 미치는 영향

### Phase 1: 초기 로드 경량화 (limit=100→40, fields=minimal)

| 영역 | 영향 | 근거 |
|---|---|---|
| LLM 맥락 이해도 | ❌ 없음 | 컨텍스트 빌더는 별도 SELECT 경로 (`chat_service.py:1859`) |
| 메모리 회수 | ❌ 없음 | session_notes / ai_observations 별도 테이블 |
| Auto-RAG | ❌ 없음 | embedding 기반 벡터 검색, 프론트 limit과 무관 |
| 어드민/검수/회고 화면 | ⚠️ 위험 | minimal에 thinking_summary/tools_called/attachments 빠지면 표시 회귀 |
| Quality/Bookmark UI | ⚠️ 위험 | minimal에 quality_score/bookmarked/edited_at 빠지면 표시 누락 |

### Phase 2: 폴링 재조회 제거 (revision skip)

| 영역 | 영향 | 근거 |
|---|---|---|
| LLM 맥락 이해도 | ❌ 없음 | 다음 턴 send 시 컨텍스트 빌더가 DB 직독 |
| 진행 중 버블 표시 | ⚠️ 위험 | placeholder 갱신/recovered/edit/quality 부여/bookmark 토글이 revision bump에서 빠지면 화면 멈춤 |
| Embedding 채움 | ❌ 없음 | INSERT 시점 트리거 |
| Quality 피드백 | ❌ 없음 | 7일치 백그라운드 집계 |

### Phase 3: 리스트 가상화 / 컴포넌트 분리

| 영역 | 영향 | 근거 |
|---|---|---|
| LLM 맥락 이해도 | ❌ 없음 | 프론트 렌더링만 |
| Quality 표시 | ⚠️ 위험 | 가상화 시 화면 밖 메시지의 quality 갱신 이벤트 처리 빠질 수 있음 |
| Auto-RAG 인용 표시 | ⚠️ 위험 | 검색 결과 → 메시지 점프 시 가상화된 항목을 못 찾을 수 있음 |

### Phase 4: artifacts lazy load

| 영역 | 영향 | 근거 |
|---|---|---|
| LLM 맥락 이해도 | ❌ 없음 | artifact 컨텍스트는 `_build_artifact_context_layer`가 DB 직독 |
| 사용자 작업 흐름 | ⚠️ 위험 | OTP/긴급 상호작용 중 패널 미열림 상태에서 artifact 미반영 가능 |

## 3. v2 안전장치 (P0 격상 항목)

### 3.1 fields=minimal payload 재정의 (v1 보강)

```jsonc
// /chat/messages?fields=minimal 응답 메시지 한 건
{
  "id": "uuid",
  "session_id": "uuid",
  "role": "user|assistant|system",
  "intent": "string|null",
  "model_used": "string|null",
  "created_at": "iso8601",
  "edited_at": "iso8601|null",
  "content_preview": "string (최대 280자, content 원본은 제외)",
  // ── v2 추가: 회귀 방지 메타필드 ──
  "quality_score": 0.0,            // 진화/표시 필수
  "bookmarked": false,             // 북마크 UI 필수
  "is_compacted": false,            // 압축 표시 필수
  "has_thinking": true,             // thinking_summary 보유 여부
  "has_tools": true,                // tools_called 보유 여부
  "has_attachments": false,         // attachments 보유 여부
  "has_sources": false,             // sources 보유 여부
  "has_embedding": true             // 진화/RAG 자산 보유 여부 (관측용)
}
```

`content` 본문, `thinking_summary`, `tools_called`, `attachments`, `sources`, `embedding`은 `fields=full`이거나 `GET /chat/messages/{id}?fields=full`로 별도 lazy 호출.

### 3.2 Revision bump 트리거 목록 (v1 보강)

`/chat/streaming-status` 응답의 `message_revision`은 다음 이벤트에서 반드시 +1:

1. assistant placeholder INSERT
2. assistant placeholder content 갱신 (delta tick)
3. placeholder → 최종 응답 confirm
4. recovered/interrupted/stopped 상태 전환
5. 메시지 edit/intent 변경/model_used 변경
6. quality_score 부여 또는 갱신
7. bookmark 토글
8. 메시지 삭제
9. compaction 적용으로 `is_compacted=true` 전환

`artifact_revision`은 artifact INSERT/UPDATE/DELETE에 한해 bump.

### 3.3 코드 Contract: 프론트 캐시의 LLM 재활용 금지

`app/services/context_builder.py:build_messages_context()`는 호출 시 항상 DB에서 `chat_messages`를 직접 SELECT 한다. 프론트에서 받은 minimal payload는 LLM 입력으로 재활용하지 않는다.

- 회귀 방지 단위 테스트 추가:
  - `test_context_builder_uses_db_select_only`: chat_service.send 흐름 mock으로 `SELECT ... FROM chat_messages` 호출 횟수가 0이면 fail.
- 코드 주석으로 contract 명시:
  ```python
  # CONTRACT (AADS-CHAT-LIGHTWEIGHT v2):
  #   build_messages_context()는 chat_messages를 직접 SELECT 한다.
  #   프론트 minimal payload를 LLM raw_messages로 재활용하면 thinking_summary/tools_called/attachments/embedding 자산이 누락되어
  #   맥락 이해도와 진화 시스템(reflexion/quality_feedback)이 동시 회귀한다.
  ```

### 3.4 가상화 + Quality/Embedding 갱신 안전장치

가상화 도입 시 화면 밖 메시지의 `quality_score`, `has_embedding`, `is_compacted` 변경 이벤트도 React state에 반영. 구현 방법:

- 가상화 컴포넌트는 항상 `messagesById` Map을 단일 소스로 사용.
- `streaming-status.message_revision` bump 시 변경된 id 목록만 minimal로 재조회 (`/chat/messages?ids=...&fields=minimal`) — 신규 엔드포인트 추가 P0.
- 화면에 보이는 항목만 `fields=full` 호출로 본문 채움.

### 3.5 Lazy load full body API 명세

```
GET /chat/messages/{message_id}?fields=full
→ 200 { id, content, thinking_summary, tools_called, attachments, sources, sources_v2, ... }
ETag: "<row_version>"
Cache-Control: private, max-age=3600
```

프론트 캐시는 `messagesById[id].full = response`로 1회 보관, edit/quality bump 시 invalidate.

### 3.6 어드민/검수/회고 화면 회귀 방지

- 어드민 메시지 목록 화면은 `fields=full`을 명시 사용. `fields` 미지정 기본값은 채팅 UI 전용 minimal — 명시적 옵트인.
- 회귀 테스트:
  - 어드민 `/admin/chat-messages` 응답에 `thinking_summary` 키 포함 여부.
  - `/admin/quality-review`에서 `quality_score` not null 표시 여부.

## 4. 우선순위 체크리스트 v2 (맥락/진화 영향 컬럼 추가)

| 우선순위 | 작업 | 기대 효과 | 맥락 영향 | 진화 영향 | 위험 |
|---|---|---|---|---|---|
| P0 | fields=minimal 실제 구현 + 메타필드 9종 포함 | 초기 payload 감소 + 회귀 방지 | 없음 | 없음 | 낮음 |
| P0 | 초기 limit 100 → 40 | 첫 화면 TTFB 단축 | 없음 | 없음 | 낮음 |
| P0 | streaming-status revision 9종 트리거 | 폴링 중 messages 재조회 0회 | 없음 | 없음 | 중간 |
| P0 | `GET /chat/messages?ids=...&fields=minimal` 신규 | 가상화 가능 + 부분 갱신 | 없음 | 없음 | 낮음 |
| P0 | `build_messages_context()` DB 직독 contract 코드/테스트 | LLM 맥락 회귀 차단 | 보호 | 보호 | 낮음 |
| P1 | 메시지 리스트 가상화 (react-virtuoso) | 긴 세션 렌더링 비용↓ | 없음 | 없음 | 중간 |
| P1 | artifacts lazy load + count/last_updated 요약 | 세션 전환 가벼움 | 없음 | 없음 | 낮음 |
| P1 | `GET /chat/messages/{id}?fields=full` lazy 본문 | 대형 응답 세션 최적화 | 없음 | 없음 | 중간 |
| P2 | `page.tsx` hook/component 분리 | 유지보수성 | 없음 | 없음 | 중간 |
| P2 | API 헤더 `X-AADS-Query-MS / Payload-Bytes / Row-Count` | 측정 계측 | 없음 | 없음 | 낮음 |

## 5. 배포 후 회귀 모니터링 (P0 자동 점검)

```sql
-- 1) embedding 결손률 변화 (v1 배포 전후 비교)
SELECT
  date_trunc('hour', created_at) AS hr,
  count(*) AS msgs,
  count(embedding) AS embedded,
  ROUND(100.0 * count(embedding) / count(*), 1) AS embed_pct
FROM chat_messages
WHERE created_at > NOW() - INTERVAL '24 hours'
GROUP BY 1 ORDER BY 1 DESC;

-- 2) quality_score 부여율 (reflexion 입력 자산)
SELECT
  date_trunc('hour', created_at) AS hr,
  count(*) FILTER (WHERE role='assistant') AS asst,
  count(quality_score) FILTER (WHERE role='assistant') AS scored
FROM chat_messages
WHERE created_at > NOW() - INTERVAL '24 hours'
GROUP BY 1 ORDER BY 1 DESC;

-- 3) thinking_summary 보유율 (도구박스/싱킹박스 표시 회귀 감지)
SELECT
  date_trunc('hour', created_at) AS hr,
  count(*) FILTER (WHERE role='assistant') AS asst,
  count(*) FILTER (WHERE role='assistant' AND thinking_summary IS NOT NULL) AS w_thinking
FROM chat_messages
WHERE created_at > NOW() - INTERVAL '24 hours'
GROUP BY 1 ORDER BY 1 DESC;
```

세 지표 중 하나라도 직전 7일 평균 대비 -10%p 이상 떨어지면 즉시 롤백 알림.

프론트 측 메트릭 (Phase 0 계측과 결합):
- `chat.session_enter.payload_bytes` p95 ≥ 50% 감소 미달 → 실패.
- `chat.idle_polling.messages_calls_per_min` > 0 → 실패.
- `chat.message_render.long_session_500.frame_drop_count` > 5 → 가상화 회귀.

## 6. 완료 기준 v2

원안 4개 항목 + v2 추가 5개:

1. 세션 진입 시 `/chat/messages` payload bytes 50% 이상 감소.
2. idle polling 상태에서 `/chat/messages` 호출 0회 유지.
3. 500개 메시지 세션에서도 스크롤/입력 프레임 드랍이 체감되지 않을 것.
4. SSE 중 응답 버블 손실, 세션 전환 빈 화면, recovered 중복 메시지 회귀가 없을 것.
5. **(v2)** embedding 보유율 / quality_score 부여율 / thinking_summary 보유율이 배포 전 7일 평균 대비 -10%p 이내.
6. **(v2)** `build_messages_context()` 단위 테스트로 DB 직독 contract 통과.
7. **(v2)** 어드민 메시지 화면에서 thinking_summary / tools_called / attachments / quality_score 표시 회귀 없음.
8. **(v2)** Auto-RAG 검색 결과 클릭 → 가상화 리스트에서 정확한 메시지 위치 점프 동작.
9. **(v2)** memory_recall 주입 토큰량(`system_prompt_tokens chars` 로그) 변화 ±5% 이내.

## 7. 권장 실행 순서 (v2)

1. **Phase 0**: API 헤더 계측 + 프론트 `performance.mark` (1일).
2. **P0 묶음 1차 배포**: fields=minimal(v2 정의) + limit=40 + revision 9종 + ids 일괄 조회 + LLM 컨텍스트 contract 테스트 (2~3일). 채팅 UI/어드민/검수 화면 동시 회귀 점검.
3. **모니터링 7일**: §5 SQL/메트릭 기반.
4. **P1 2차 배포**: 가상화 + artifacts lazy + lazy full body (2일).
5. **P2 정리**: page.tsx 분리, 측정 헤더 정착.

각 단계는 PC Agent SSE 끊김 P0 hotfix와 분리 워크트리/분리 PR로 진행한다.

## 8. 변경 사항 요약 (v1 → v2)

| 항목 | v1 | v2 |
|---|---|---|
| fields=minimal payload | `id/role/content_preview/intent/model_used/created_at/edited_at` | + `quality_score / bookmarked / is_compacted / has_thinking / has_tools / has_attachments / has_sources / has_embedding` |
| revision bump | `message/placeholder/artifact` 3종 | placeholder/delta/confirm/recovered/edit/quality/bookmark/delete/compact 9종 |
| 신규 엔드포인트 | (없음) | `GET /chat/messages?ids=...&fields=minimal`, `GET /chat/messages/{id}?fields=full` |
| LLM 컨텍스트 보호 | 명시 없음 | DB 직독 contract + 단위 테스트 |
| 회귀 모니터링 | "체감 아닌 측정" 원칙만 | embedding/quality/thinking 보유율 SQL + 롤백 임계치 |
| 완료 기준 | 4개 | 9개 (진화·맥락 보호 5개 추가) |

## 9. 결론

v1 경량화 4단계는 데이터 측면에서 LLM 맥락 이해도와 진화 시스템에 자동 영향을 주지 않는다. 다만 v2에서 추가한 5가지 안전장치(메타필드, revision 9종, DB 직독 contract, 가상화 보호, 회귀 모니터링)를 P0로 함께 가야 어드민/검수/회고 화면 회귀와 향후 잘못된 최적화로 인한 진화 자산 손실을 막을 수 있다. v2를 정식 실행 기준으로 채택을 권고한다.
