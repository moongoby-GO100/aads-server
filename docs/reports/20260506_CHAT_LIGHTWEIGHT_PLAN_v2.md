# AADS 채팅 경량화 기획 v2 — 맥락·진화 보존 설계

> 작성일: 2026-05-06 10:30 KST | 작성자: CTO AI | 상태: CEO 검토 대기

## 1. 요약

채팅 UI의 응답 지연과 불필요한 네트워크 요청을 해결하되, **LLM 맥락 이해도와 진화 시스템(메모리·관찰·품질점수)에는 일절 영향을 주지 않는** 경량화 방안입니다.

핵심 원칙: **UI 표시용 fetch와 LLM 컨텍스트용 fetch는 완전히 분리된 독립 경로이므로, UI 경로만 최적화합니다.**

---

## 2. 현재 문제 (실측 기준)

### 2.1 DB 규모
| 항목 | 수치 | 출처 |
|---|---:|---|
| chat_messages 총 건수 | 39,109건 | [DB 조회] |
| chat_sessions 수 | 70개 | [DB 조회] |
| 세션별 평균 메시지 수 | 611건 | [DB 조회] |
| 세션별 최대 메시지 수 | 7,348건 | [DB 조회] |
| content 평균 길이 | 818자 | [DB 조회] |
| content p95 길이 | 3,083자 | [DB 조회] |
| ai_observations | 1,777건 | [DB 조회] |
| memory_facts | 41,049건 | [DB 조회] |

### 2.2 프론트엔드 과잉 요청 (page.tsx 6,347줄)
| 시점 | 현재 요청 | 문제 |
|---|---|---|
| 세션 진입 | `limit=100` full row | 초기 payload ~82KB, 화면 표시에는 30건 충분 |
| just_completed 감지 | `limit=50` full row | 마지막 1~2건만 필요한데 50건 재조회 |
| rate_limited 감지 | `limit=50` full row | 상태 변경만 필요 |
| SSE disconnect | `limit=50` full row | 마지막 메시지 변경 확인용인데 50건 |
| 폴링 (변경 없음) | `limit=5&fields=minimal` | ✅ 이미 최적화됨 |
| 폴링 (변경 있음) | `limit=50` full row | 신규 메시지만 필요 |

---

## 3. 맥락·진화 시스템 구조 분석

### 3.1 LLM 컨텍스트 구축 파이프라인 (변경 금지 영역)

```
send_message_stream() → DB에서 직접 LIMIT 200건 조회 (chat_service.py:4173)
    ↓
context_builder.build_messages_context()
    ├── Layer 1: 정적 시스템 프롬프트 (workspace 캐시)
    ├── Layer 2: 동적 컨텍스트 (시각, 작업 현황, 60초 TTL)
    ├── Layer 메모리: memory_recall 10섹션 병렬 조회 (60초 TTL)
    │   └── session_notes, ceo_preference, tool_strategy, directives,
    │       discoveries, learned, corrections, experience_lessons,
    │       visual_memories, strategy_updates
    ├── Layer Auto-RAG: 유사 과거 대화 검색
    ├── Layer Preload: workspace 프리로드
    ├── Layer Artifact: 최근 아티팩트 20건 (type/title만)
    └── Layer 3: _build_layer3_messages()
        ├── 최근 20턴: full content
        ├── 20~40턴: 도구 결과 마스킹
        ├── 40턴 이전: Deep Compression (user 100자, assistant 200자)
        └── 60K 토큰 초과 시: compaction_service 트리거
```

### 3.2 진화 데이터 축적 경로 (변경 금지 영역)

```
매 응답 저장 시 (message_count % 20 == 0):
    ├── _auto_observe_session() → ai_observations INSERT
    ├── _auto_save_session_note() → session_notes UPSERT
    └── _auto_extract_mid_conversation_lessons() → memory_facts INSERT
```

### 3.3 핵심 보존 원칙

| 영역 | 경량화 영향 | 판정 |
|---|---|---|
| `LIMIT 200` raw_messages (LLM용) | UI fetch limit과 **완전 독립** | ✅ 영향 없음 |
| context_builder 7개 레이어 | 메시지 로딩과 **독립 경로** | ✅ 영향 없음 |
| memory_recall 10섹션 조회 | 60초 TTL 캐시, fetch와 무관 | ✅ 영향 없음 |
| 진화 축적 (20턴마다) | raw_messages 인자로 받음 → LLM LIMIT 200 경로 | ✅ 영향 없음 |
| Deep Compression | _OBSERVATION_WINDOW 기반 자동 적용 | ✅ 영향 없음 |
| compaction_service | 80K 토큰 초과 시 자동 트리거 | ✅ 영향 없음 |

**결론: UI 표시용 fetch를 줄여도 LLM이 보는 대화 히스토리, 메모리, 진화 데이터 축적에는 일절 영향이 없습니다.**

---

## 4. 경량화 실행 계획

### Phase 0: 측정 계측 (0.5일)
- 프론트엔드에 `performance.mark()`/`measure()` 삽입
- API 응답 시간/payload 크기 로깅
- 기준선 측정: 세션 진입 → 첫 메시지 렌더링까지 소요 시간

### Phase 1: 초기 로드 경량화 (1일) — P0
| 변경 | 현재 | 변경 후 | 예상 효과 |
|---|---|---|---|
| 세션 진입 limit | 100 | 40 | payload 60% 감소 |
| 초기 로드 fields | full (SELECT *) | minimal 후 visible 메시지만 full fetch | 초기 payload ~90% 감소 |
| 이전 메시지 | 없음 | 스크롤 시 cursor 기반 40건씩 lazy load | UX 유지 |

맥락 영향: **없음** — LLM 컨텍스트는 `send_message_stream()` 내부에서 별도 `LIMIT 200` 조회

### Phase 2: 폴링/재조회 최적화 (1일) — P0
| 변경 | 현재 | 변경 후 | 예상 효과 |
|---|---|---|---|
| just_completed | `limit=50` full | revision 비교 → 변경 시 `limit=5&fields=minimal` | 요청 90% 감소 |
| rate_limited | `limit=50` full | 마지막 메시지 status만 PATCH 수신 | 요청 95% 감소 |
| SSE disconnect | `limit=50` full | revision 비교 → `limit=10&fields=minimal` | 요청 80% 감소 |
| 폴링 (변경 있음) | `limit=50` full | `limit=5&fields=minimal` → 신규만 full fetch | 요청 70% 감소 |

활용 기반: `streaming-status` API에 이미 `message_revision`, `placeholder_revision`, `artifact_revision` 구현됨 (routers/chat.py:82-154). revision 불변 시 재조회 자체를 건너뛰는 것이 가장 안전한 최적화.

맥락 영향: **없음** — 이 요청들은 모두 UI 표시용

### Phase 3: 컴포넌트 분리 + 가상화 (2일) — P1
| 변경 | 설명 |
|---|---|
| page.tsx 분리 | 6,347줄 → MessageList, MessageInput, SessionSidebar, ArtifactPanel 4개 이상 분리 |
| 메시지 리스트 가상화 | react-window 또는 @tanstack/virtual 적용, 화면 밖 DOM 미생성 |
| 메모이제이션 | 개별 메시지 React.memo, 렌더링 범위 제한 |

맥락 영향: **없음** — 순수 프론트엔드 렌더링 최적화

### Phase 4: Artifacts Lazy Load (0.5일) — P2
| 변경 | 현재 | 변경 후 |
|---|---|---|
| 세션 진입 시 | 전체 artifact 로드 | count만 조회, panel 열 때 full load |
| context_builder | 최근 20건 type/title만 (영향 없음) | 변경 없음 |

맥락 영향: **없음** — `_build_artifact_context_layer()`는 type/title만 사용 (~1KB)

---

## 5. 예상 효과

| 지표 | 현재 (추정) | Phase 1+2 후 | Phase 3+4 후 |
|---|---|---|---|
| 세션 진입 payload | ~82KB | ~15KB | ~15KB |
| 세션 진입 → 렌더링 | [미측정] | 50~70% 단축 예상 | 70~85% 단축 예상 |
| 폴링 당 네트워크 | ~40KB/회 | ~2KB/회 | ~2KB/회 |
| DOM 노드 (1000건 세션) | ~3000개 | ~3000개 | ~120개 (가상화) |

※ 정확한 수치는 Phase 0 측정 후 확정

---

## 6. 맥락·진화 보존 체크리스트

경량화 코드 리뷰 시 아래 항목을 반드시 확인:

- [ ] `chat_service.py:4173`의 `LIMIT 200` raw_messages 로딩이 변경되지 않았는가?
- [ ] `context_builder.py`의 7개 레이어 구조/호출이 변경되지 않았는가?
- [ ] `memory_recall.py`의 10섹션 조회/4000자 예산이 변경되지 않았는가?
- [ ] 진화 축적 트리거 (`message_count % 20 == 0`) 로직이 변경되지 않았는가?
- [ ] `_OBSERVATION_WINDOW` 값(20턴)이 변경되지 않았는가?
- [ ] compaction_service 트리거 조건(80K 토큰)이 변경되지 않았는가?
- [ ] `streaming-status` API의 revision 필드가 보존되었는가?

---

## 7. 실행 우선순위 및 일정

| 순서 | Phase | 우선순위 | 예상 기간 | 비고 |
|---|---|---|---|---|
| 1 | Phase 0 (측정) | P0 | 0.5일 | 기준선 없이 최적화 효과 판정 불가 |
| 2 | Phase 2 (폴링 최적화) | P0 | 1일 | revision 기반 skip이 가장 효과 높고 안전 |
| 3 | Phase 1 (초기 로드) | P0 | 1일 | limit 축소 + minimal 2단계 로딩 |
| 4 | Phase 3 (컴포넌트) | P1 | 2일 | page.tsx 분리 + 가상화 |
| 5 | Phase 4 (artifacts) | P2 | 0.5일 | lazy load |

총 예상: **5일** (Phase 0~2까지 2.5일이면 체감 성능 대폭 개선)

---

## 8. 위험 요소 및 대응

| 위험 | 확률 | 대응 |
|---|---|---|
| minimal → full 2단계 로딩 시 깜박임 | 중 | skeleton UI + content preview 200자 먼저 표시 |
| revision 기반 skip에서 메시지 누락 | 하 | revision mismatch 시 full refetch fallback |
| page.tsx 분리 시 상태 공유 버그 | 중 | Context API로 공유 상태 분리, E2E 테스트 |
| 가상화 적용 시 스크롤 점프 | 중 | 메시지 높이 추정 함수 + ResizeObserver 보정 |
