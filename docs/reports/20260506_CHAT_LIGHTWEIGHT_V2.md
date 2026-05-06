# 채팅 경량화 상세 기획 보고서 V2

> 작성: 2026-05-06 09:47 KST | 작성자: AADS CTO AI | 상위: 20260506_CHAT_LIGHTWEIGHT_PLAN.md

## 1. 결론 요약

**채팅 경량화는 맥락 이해도와 진화 시스템에 영향 없이 진행 가능합니다.**

근거: 맥락 주입(memory_recall)·프롬프트 컴파일러·진화 시스템(ai_observations, session_notes, ai_meta_memory)은 **chat_messages 테이블을 직접 참조하지 않고 별도 DB 테이블에서 LLM 호출 시점에 독립 조회**합니다. 따라서 프론트엔드 메시지 로딩을 경량화해도 LLM이 받는 컨텍스트 품질은 동일합니다.

---

## 2. 현재 문제점 (코드 실측 기준)

### 2-1. 프론트엔드 — 과도한 초기 로드 + 불필요한 재조회

| 문제 | 위치 | 영향 |
|------|------|------|
| 세션 진입 시 `limit=100` 전체 메시지 로드 | `page.tsx` (6,347줄 단일 컴포넌트) | 초기 렌더 지연, DOM 폭증 |
| `SELECT *` — embedding(vector 768), tools_called(JSONB), thinking_summary 등 대용량 컬럼 포함 | `chat.py:174~199` | 네트워크 전송량 과다 |
| streaming-status에 `message_revision` 미구현 | `chat.py:318~637` | 변경 없어도 `/messages` API 반복 호출 |
| 폴링 6개 분기(`just_completed`, SSE 끊김, `waitingBg`, `rate_limited` 등)에서 `limit=50` 재조회 반복 | `page.tsx` 내 다수 분기 | 동일 데이터 중복 fetch |

### 2-2. 백엔드 — 조회 함수 내 부수 쓰기

| 문제 | 위치 | 영향 |
|------|------|------|
| `list_messages()` 내부에서 `streaming_placeholder` → `recovered` 승격 (메시지당 최대 3회 추가 쿼리) | `chat_service.py:2644~2760` | 읽기 API가 쓰기를 유발, 응답 지연 |
| `_dedupe_recovery_like_messages()` 중복 제거 DELETE | `chat_service.py` 내부 | 조회마다 잠재적 DB 쓰기 |
| 서비스 함수 기본값 `limit=200` (라우터 기본값 50과 불일치) | `chat_service.py:2646` | 의도치 않은 과다 조회 가능 |

### 2-3. 체감 증상

- **채팅창 응답 체감 지연**: OTP 입력 타임아웃 미스 (CEO 보고)
- **세션 전환 시 빈 화면 → 로딩 → 렌더**: 100건 메시지 + embedding 데이터 전송
- **스크롤 시 버벅임**: 100개 메시지 DOM 동시 렌더링 (가상화 미적용)

---

## 3. 맥락 이해도·진화 영향 분석 — 왜 안전한가

### 3-1. 아키텍처 분리도

```
[프론트엔드 메시지 로딩]          [LLM 컨텍스트 구성]
       │                              │
  GET /chat/messages              build_messages_context()
  (프론트 표시용)                  (LLM 호출 직전)
       │                              │
  chat_messages                   ┌─ Layer 1: 정적 시스템 (prompt_assets)
  SELECT * LIMIT 100              ├─ Layer 2: 시각 + 작업 상태
       │                          ├─ memory_layer: 10섹션 병렬 DB 조회
  ┌────┴────┐                     ├─ preload_layer: 프로젝트 컨텍스트
  │ 완전 독립 │                    ├─ auto_rag_layer: 시맨틱 검색
  └─────────┘                     ├─ artifact_layer: 최근 아티팩트 20건
                                  └─ Layer 3: 대화 히스토리 (별도 구성)
```

**핵심**: 프론트의 `GET /chat/messages`는 **화면 표시 전용**이고, LLM이 받는 컨텍스트는 `build_messages_context()`에서 **별도로 구성**됩니다. 두 경로는 완전히 독립적입니다.

### 3-2. 진화 시스템 의존 관계

| 진화 구성요소 | 데이터 소스 | chat_messages 의존 | 경량화 영향 |
|---|---|---|---|
| memory_recall (10섹션) | session_notes, ai_observations, ai_meta_memory, directive_lifecycle | ❌ 없음 | ✅ 영향 없음 |
| prompt_compiler | prompt_assets, session_blueprints, llm_models | ❌ 없음 | ✅ 영향 없음 |
| observation masking | raw_messages (LLM 호출 시 전달된 것) | ⚠️ 간접 | ✅ 영향 없음 (아래 설명) |
| compaction (압축) | raw_messages (60K 토큰 초과 시) | ⚠️ 간접 | ✅ 영향 없음 (아래 설명) |
| session_notes 저장 | 20턴마다 LLM 요약 → DB 저장 | ❌ 없음 | ✅ 영향 없음 |
| ai_observations 축적 | 에이전트 완료 시 DB 저장 | ❌ 없음 | ✅ 영향 없음 |
| error_pattern 경고 | ai_observations 조회 | ❌ 없음 | ✅ 영향 없음 |

### 3-3. Layer 3 (대화 히스토리)가 안전한 이유

Layer 3은 프론트 `GET /messages`가 아니라 `_build_layer3_messages(raw_messages)`에서 구성됩니다.

현재 이미 적용된 보호 장치:
- **Observation Window**: 최근 20턴만 도구 결과 보존, 이전은 마스킹
- **강제 압축**: 2×window(40턴) 이전은 user 100자, assistant 200자로 절삭
- **토큰 가드**: 60K 초과 시 공격적 masking, 80K 초과 시 compaction(LLM 요약)
- **비상 절삭**: compaction 실패 시 최근 30개만 유지

**즉, LLM에 전달되는 대화 히스토리는 이미 자체적으로 경량화되어 있습니다.** 프론트 로딩과는 무관합니다.

---

## 4. 경량화 실행 계획

### Phase 0: 계측 기반 마련 (0.5일)

| 항목 | 구현 | 측정 지표 |
|------|------|----------|
| `/messages` 응답 시간 로깅 | 라우터에 `X-Response-Time` 헤더 추가 | p50/p95 응답 시간 |
| 메시지 페이로드 크기 측정 | 100건 기준 평균/최대 바이트 | 전송량 절감 기준선 |
| 프론트 `/messages` 호출 빈도 | `streaming-status` 폴링 시 재조회 카운트 | 불필요 재조회 비율 |

### Phase 1: 초기 로드 경량화 (1일) — 체감 즉시 개선

**1-1. `fields=minimal` 파라미터 추가**

```python
# chat.py GET /chat/messages
@router.get("/messages")
async def list_messages(
    ...,
    fields: str = Query("full", regex="^(full|minimal)$")
):
```

| 모드 | 포함 컬럼 | 제외 컬럼 | 예상 절감 |
|------|----------|----------|----------|
| `full` (기본) | 전체 | 없음 | 0% |
| `minimal` | id, session_id, role, content, model_used, created_at, bookmarked, reply_to_id, edited_at | embedding, tools_called, thinking_summary, quality_details, sources, attachments | **60~70%** |

**1-2. 초기 로드 limit 조정**

- 세션 진입 시: `limit=100` → `limit=40` + `fields=minimal`
- 스크롤 위로 올릴 때: cursor 기반 `limit=20` 추가 로드 (lazy)

**1-3. list_messages() 부수 쓰기 분리**

```python
# 현재: 조회 함수 안에서 placeholder 승격 + 중복 제거
# 개선: 별도 백그라운드 태스크로 분리
async def list_messages(...):
    # 순수 SELECT만 수행
    rows = await conn.fetch(query, ...)
    return rows

# 별도 주기 태스크 (30초마다)
async def cleanup_streaming_placeholders():
    # placeholder → recovered 승격
    # 중복 recovered 제거
```

### Phase 2: 불필요 재조회 제거 (1일) — 네트워크 트래픽 절감

**2-1. streaming-status에 message_revision 추가**

```python
# streaming-status 응답에 추가
{
    "is_streaming": false,
    "message_revision": "abc123",  # 최신 메시지 ID + count 해시
    "last_message_id": "..."
}
```

**2-2. 프론트 재조회 조건 변경**

```
현재: just_completed=true → 무조건 /messages 재호출
개선: just_completed=true + message_revision 변경 시에만 재호출
```

예상 효과: 폴링 시 `/messages` 호출 **70~80% 감소**

### Phase 3: DOM 가상화 + 컴포넌트 분리 (2일) — 렌더링 성능

**3-1. 메시지 리스트 가상화**

| 방식 | 장점 | 단점 |
|------|------|------|
| `react-virtuoso` | 역방향 스크롤 네이티브 지원, 채팅 특화 | 번들 +15KB |
| `@tanstack/virtual` | 경량, 유연 | 역방향 스크롤 직접 구현 |
| **권장: `react-virtuoso`** | 채팅 UI에 최적화, 역방향 무한스크롤 기본 제공 | |

가상화 적용 시 DOM 노드: 100개 → **15~20개** (뷰포트 내 메시지만 렌더)

**3-2. page.tsx 컴포넌트 분리**

```
page.tsx (6,347줄)
  ├─ ChatMessageList.tsx     (메시지 렌더링 + 가상화)
  ├─ ChatInput.tsx           (입력 + 파일 첨부)
  ├─ StreamingStatus.tsx     (실시간 상태 표시)
  ├─ ArtifactPanel.tsx       (아티팩트 사이드바)
  └─ SessionSidebar.tsx      (세션 목록)
```

### Phase 4: Artifacts Lazy Load (0.5일) — 추가 최적화

- 아티팩트 본문은 사이드바 열릴 때만 fetch
- 메시지 목록에는 `artifact_id` + 제목만 표시
- 이미지 아티팩트: 썸네일(200px) 우선, 클릭 시 원본

---

## 5. 맥락 보전 체크리스트

경량화 각 Phase에서 맥락/진화가 훼손되지 않는지 검증하는 체크리스트입니다.

| # | 검증 항목 | 검증 방법 | Phase |
|---|----------|----------|-------|
| 1 | LLM에 전달되는 시스템 프롬프트 동일 | compile → provenance 비교 (전후) | 전체 |
| 2 | memory_recall 10섹션 주입량 동일 | `build_memory_context()` 반환 문자수 비교 | 전체 |
| 3 | Layer 3 대화 히스토리 동일 | `_build_layer3_messages()` 반환 메시지 수·토큰 비교 | 전체 |
| 4 | observation masking window 유지 (20턴) | 환경변수 `OBSERVATION_WINDOW_SIZE` 불변 확인 | 전체 |
| 5 | compaction 트리거 (60K/80K) 유지 | `context_builder.py` 임계값 불변 확인 | 전체 |
| 6 | session_notes 20턴 자동 저장 유지 | 경량화 후 session_notes INSERT 확인 | Phase 1 |
| 7 | ai_observations 축적 유지 | 에이전트 완료 후 observation INSERT 확인 | Phase 2 |
| 8 | `fields=minimal` 시 프론트 표시 정상 | content, role, created_at 표시 확인 | Phase 1 |
| 9 | cursor 페이지네이션 시 누락 메시지 없음 | 전체 count vs 스크롤 로드 count 일치 | Phase 1 |
| 10 | 가상화 후 메시지 선택/복사/검색 정상 | 수동 QA | Phase 3 |

---

## 6. 예상 효과

| 지표 | 현재 (추정) | Phase 1 후 | Phase 1+2 후 | 전체 완료 |
|------|------------|-----------|-------------|----------|
| 세션 진입 전송량 | ~2MB (100건×SELECT *) | ~300KB (40건×minimal) | ~300KB | ~300KB |
| 폴링 시 /messages 호출 | 매 폴링마다 | 매 폴링마다 | revision 변경 시만 (70~80%↓) | 동일 |
| DOM 노드 (메시지) | 100개 | 40개 | 40개 | 15~20개 |
| list_messages 응답 시간 | ~500ms (추정, 부수쓰기 포함) | ~100ms (순수 SELECT) | 동일 | 동일 |
| 맥락 이해도 | 기준선 | 동일 | 동일 | 동일 |
| 진화 데이터 축적 | 기준선 | 동일 | 동일 | 동일 |

---

## 7. 리스크 및 대응

| 리스크 | 확률 | 대응 |
|--------|------|------|
| `fields=minimal`에서 프론트가 누락 컬럼 참조 | 중 | TypeScript 빌드 에러로 사전 감지 |
| 가상화 라이브러리와 기존 스크롤 로직 충돌 | 중 | Phase 3 전 프론트 스크롤 코드 사전 분석 |
| placeholder 승격 분리 후 미승격 메시지 잔류 | 저 | 30초 주기 + 세션 종료 시 강제 정리 |
| revision 해시 불일치로 메시지 누락 표시 | 저 | revision 불일치 시 fallback으로 전체 재조회 |

---

## 8. 실행 우선순위 권장

```
Phase 1 (1일) → 즉시 체감 개선 (전송량 85%↓, 응답 시간 80%↓)
  ↓
Phase 2 (1일) → 네트워크 트래픽 대폭 절감
  ↓
Phase 0 (0.5일, Phase 1과 병행 가능) → 개선 효과 수치 검증
  ↓
Phase 3 (2일) → 대규모 대화 시 렌더링 성능
  ↓
Phase 4 (0.5일) → 추가 최적화
```

총 소요: **약 5일** (Phase 1만 우선 적용 시 1일)

---

## 부록: 맥락 시스템 아키텍처 상세

### 메모리 자동 주입 (memory_recall.py) — 10섹션 구성

| # | 섹션 | DB 테이블 | 건수 | 토큰 예산 |
|---|------|----------|------|----------|
| 1 | session_notes | session_notes | 최근 3건 | ~500 |
| 2 | preferences | ai_observations (ceo_preference) | 최대 20건 | ~300 |
| 3 | tool_strategy | ai_observations (tool_strategy) | 최대 10건 | ~400 |
| 4 | active_directives | directive_lifecycle | 최대 10건 | ~400 |
| 5 | discoveries | ai_observations (learning/discovery) | 최대 10건 | ~400 |
| 6 | learned_memory | ai_meta_memory | 최대 15건 | ~300 |
| 7 | correction_directives | ai_meta_memory (correction) | 최근 6건 | ~200 |
| 8 | experience_lessons | ai_observations (experience) | 최근 5건 | ~300 |
| 9 | visual_memories | ai_observations (visual_memory) | 최근 3건 | ~300 |
| 10 | strategy_updates | ai_meta_memory (strategy_update) | 최근 3건 | ~500자 |

**총 상한: 4,000자 (~2,700 토큰). chat_messages 참조: 0건.**

### Layer 3 대화 히스토리 보호 장치

```
raw_messages (전체)
  │
  ├─ 최근 20턴: 도구 결과 보존 (Observation Window)
  ├─ 20~40턴: 도구 결과 마스킹
  ├─ 40턴 이전: user 100자 / assistant 200자 강제 압축
  │
  ├─ 60K 토큰 초과 → 공격적 masking (window/2)
  ├─ 80K 토큰 초과 → compaction (LLM 요약)
  └─ compaction 실패 → 최근 30개만 유지 (비상 절삭)
```

**이 전체 과정은 프론트 메시지 로딩과 완전히 독립적으로 LLM 호출 시점에 실행됩니다.**
