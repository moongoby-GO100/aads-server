# AADS Chat Lightweight V2 — 맥락·진화 영향 분석 및 보완 설계

작성: 2026-05-06 10:12 KST  
근거: V1 기획서 + 백엔드 실측 코드 분석 (context_builder.py, auto_rag.py, memory_recall.py, self_evaluator.py, chat_service.py)  
목적: V1 Phase 0~4 경량화가 **LLM 맥락 이해도**와 **진화 시스템**에 영향을 주지 않음을 실증하고, 원안에 빠진 회귀 위험을 보완한다.

---

## v2.2 업데이트 — 도구박스/최종버블 회귀 보강 (2026-05-06 KST)

### 적용 범위

- `fields=minimal` 응답은 계속 표시 전용 preview를 반환하되, 도구박스 복원에 필요한 `has_tools`, `tool_count`, `tool_names` 요약 메타를 포함한다.
- full `tools_called` JSON은 minimal payload에 싣지 않고, 프론트가 `has_tools=true` assistant 메시지에 한해 `/chat/messages/{message_id}` 상세 API를 1회 lazy hydrate한다.
- hydrate merge는 현재 화면에 이미 있는 긴 assistant 본문을 200자 preview로 덮어쓰지 않는다. `content_length/is_truncated` 기준으로 더 긴 content와 기존 tool 이벤트를 보존한다.
- 스트리밍 중 수신한 `tool_use/tool_result` 이벤트는 final assistant 버블에도 누적해, DB 저장 `tools_called`와 화면 렌더 `tools_called`가 같은 정규화 구조를 쓰도록 했다.
- `tools_called`가 과거 문자열 배열이거나 Codex relay 구조화 이벤트여도 `normalize_tool_events()`가 동일한 `{type, tool_name, tool_use_id, tool_input/content}` 배열로 맞춘다.
- `codex:gpt-5.5`, `gpt-5.5`, `GPT-5.5 (Codex CLI)` 표기는 모두 Codex 실행 모델 `gpt-5.5`로 정규화한다.

### v2.2 불변 원칙

프론트 API payload 축소는 **SELECT 컬럼 제한과 상세 lazy load**로만 처리한다. `chat_messages.content`, `embedding`, `quality_score`, `quality_details`, `thinking_summary`, `tools_called` 원본 저장 경로는 축소하지 않는다. LLM 컨텍스트, memory/RAG, quality/reflexion/sleep-time 계층은 서버가 DB 원본을 읽는 기존 경로를 계속 사용한다.

### 수동 검증 절차

대상 세션: `b8a8651b-6226-46df-9a44-36a70e478959`

1. `/chat#b8a8651b-6226-46df-9a44-36a70e478959` 진입 후 Network에서 `/chat/messages?fields=minimal` 응답의 assistant 메시지에 `has_tools/tool_count/tool_names`가 있는지 확인한다.
2. 같은 assistant 메시지에 `tools_called`가 없거나 빈 배열이어도 도구박스 placeholder가 표시되는지 확인한다.
3. 이어서 `/chat/messages/{message_id}` 요청이 메시지당 1회만 발생하고, 응답 후 도구박스가 tool detail로 갱신되는지 확인한다.
4. 800자 이상 assistant 본문이 표시된 상태에서 minimal polling이 다시 와도 화면의 본문 길이가 200자로 줄지 않는지 확인한다.
5. 새 Codex 응답에서 스트리밍 중 보인 도구 이벤트와 완료 후 도구박스의 도구명/개수가 일치하는지 확인한다.

### 자동 검증

- `python3 -m pytest tests/unit/test_chat_service.py tests/unit/test_chat_lightweight_frontend_static.py -q`

---

## 1. 핵심 결론

| 판정 | 내용 |
|------|------|
| ✅ 안전 | V1 Phase 0~4는 **프론트 표시/폴링/렌더링 경로**만 변경하며, LLM 컨텍스트 빌더·진화 시스템과 **데이터 경로가 분리**되어 있다 |
| ✅ 안전 | 진화 5계층(session_notes, ai_observations, memory_facts, ai_meta_memory, experience_memory)은 chat_messages를 **직접 SELECT하지 않는** 별도 테이블이다 |
| ⚠️ 보완 필요 | Auto-RAG(Layer 4.5)만 chat_messages.embedding을 직접 검색하므로, `fields=minimal` 설계 시 embedding 컬럼은 **DB에서 절대 제거 불가** |
| ⚠️ 보완 필요 | V1 원안에 **4가지 회귀 위험**이 누락되어 있으며, 아래에서 보완한다 |

---

## 2. 아키텍처 실측: 데이터 경로 분리 증거

### 2.1 LLM 컨텍스트 빌더 (context_builder.py) — 5 Layer 구조

```
Layer 1 (Static)     ─ system_prompt_v2.py 캐시 ─── chat_messages 무관 ✅
Layer 2 (Dynamic)    ─ directive_lifecycle, 현재시각 ─── chat_messages 무관 ✅
Layer 3 (History)    ─ raw_messages 파라미터 전달 ──── chat_service에서 SELECT 후 전달 ⚠️ (아래 상세)
Layer 4 (Evolution)  ─ memory_recall.py 별도 테이블 ─── chat_messages 무관 ✅
Layer 4.5 (Auto-RAG) ─ auto_rag.py 시맨틱 검색 ──── chat_messages.embedding 직접 SELECT ⚠️
```

**Layer 3 상세**: `build_messages_context(raw_messages=...)` — chat_service가 DB에서 SELECT한 메시지를 파라미터로 전달한다. context_builder 자체는 DB를 직접 조회하지 않는다. 따라서 **API 응답 payload를 줄여도 Layer 3에는 영향 없음** (API 응답 ≠ LLM 입력).

**Layer 4.5 상세**: Auto-RAG는 chat_messages 테이블에서 `embedding <=> query_vector` 코사인 검색을 수행한다. 사용 컬럼: `id, role, content(500자), created_at, embedding`. 이 경로는 **프론트 API 응답과 완전히 독립**이므로 `fields=minimal`에 영향 없음.

### 2.2 진화 시스템 — 완전 분리

| 시스템 | 소스 테이블 | chat_messages 의존 | 경량화 영향 |
|--------|------------|-------------------|------------|
| Memory Recall (7섹션) | session_notes, ai_observations, ai_meta_memory | ❌ 없음 | ✅ 무관 |
| Quality Scoring | chat_messages에 **WRITE** (quality_score, quality_details) | 쓰기만 | ✅ 무관 (읽기 아님) |
| Reflexion (<0.4 반성) | ai_meta_memory에 INSERT | ❌ 없음 | ✅ 무관 |
| Sleep-Time 정제 | memory_facts confidence 조정 | ❌ 없음 | ✅ 무관 |
| Error Pattern 경고 | ai_meta_memory category='error_pattern' | ❌ 없음 | ✅ 무관 |
| Experience Memory | experience_memory 테이블 | ❌ 없음 | ✅ 무관 |
| Procedural Memory | procedural_memory 테이블 | ❌ 없음 | ✅ 무관 |

**결론**: 진화 시스템 전체가 chat_messages와 **읽기 의존성이 없다**. Quality Scoring만 chat_messages에 값을 쓰지만, 이는 self_evaluator가 LLM 응답 후 서버 사이드에서 수행하므로 프론트 payload 축소와 무관하다.

### 2.3 Layer 3 관측 마스킹 (이미 적용 중)

context_builder의 `_build_layer3_messages()`는 이미 공격적 압축을 적용하고 있다:
- **20턴 초과 메시지**: 도구 출력 상세 제거, 코드블록 1500→500자 축소
- **40턴 초과 메시지**: user 100자, assistant 200자로 deep compress
- **80K 토큰 초과**: compaction_service로 구조적 요약

따라서 V1에서 제안한 `fields=minimal`(thinking/edit_history/metadata 제거)은 **Layer 3에서 이미 제거되는 필드**를 프론트에서도 제거하는 것이므로, 맥락 이해도에 영향 없음.

---

## 3. V1 원안에 빠진 회귀 위험 4건

### 3.1 ⚠️ `fields=minimal` 필드 누락 위험

**문제**: V1의 minimal 필드 목록에 `quality_score`, `intent`, `model_used`가 빠져 있다.
- `quality_score`: 프론트 UI에서 품질 표시 기능이 있을 경우 누락됨
- `intent`: 세션 목록에서 메시지 유형(report/code_modify/casual) 표시에 사용
- `model_used`: 프론트에서 "Claude Opus / Sonnet" 모델 배지 표시에 사용

**보완**: minimal 필드 목록을 다음으로 확정한다:
```
minimal: id, session_id, role, content_preview(200자), intent, model_used, 
         quality_score, created_at, edited_at, has_attachments(bool), has_tools(bool)
```

### 3.2 ⚠️ revision bump 누락 케이스

**문제**: V1은 `message_revision`, `placeholder_revision`, `artifact_revision` 3종 revision을 제안하지만, 다음 케이스에서 bump가 누락될 수 있다:
- **SSE 중 partial → complete 전환**: streaming-status의 partial_content가 최종 메시지로 교체될 때 message_revision을 bump하지 않으면, 프론트가 최종 메시지를 놓침
- **quality_score 사후 기록**: self_evaluator가 응답 완료 후 0.5~2초 뒤에 quality_score를 UPDATE하지만, 이 시점에 revision이 bump되지 않으면 프론트에 품질 점수가 표시되지 않음
- **메시지 편집(edit)**: 사용자가 메시지를 편집하면 content가 바뀌지만, revision이 올라가지 않으면 다른 탭에서 편집 결과를 볼 수 없음

**보완**: revision bump 트리거를 명확히 정의한다:
```
message_revision bump 트리거:
  1. 새 메시지 INSERT
  2. streaming complete (partial → final)
  3. 메시지 edit/delete
  4. quality_score UPDATE (0.5초 debounce)

artifact_revision bump 트리거:
  1. 새 artifact INSERT
  2. artifact content UPDATE

placeholder_revision bump 트리거:
  1. placeholder 생성/제거
  2. streaming 시작/종료
```

### 3.3 ⚠️ "프론트 캐시 → LLM 재활용" 안티패턴 방지

**문제**: 경량화 후 프론트가 `fields=minimal`로 받은 content_preview(200자)를 캐시하고, 이것을 LLM 컨텍스트에 재활용하려는 유혹이 생길 수 있다. 이렇게 하면 맥락 이해도가 **심각하게 훼손**된다.

**원칙**: 프론트의 표시용 데이터와 LLM 입력용 데이터는 **절대 혼용 불가**.
- 프론트 캐시: 표시 전용 (minimal payload)
- LLM 입력: 서버 사이드에서 chat_messages 원본을 SELECT (full content)

**보완**: `context_builder.py`와 `chat_service.py`에 다음 주석/가드를 추가한다:
```python
# INVARIANT: raw_messages must come from DB SELECT, never from client cache
assert all('content' in m and len(m['content']) > 0 for m in raw_messages)
```

### 3.4 ⚠️ Auto-RAG embedding 생성 타이밍

**문제**: V1에서 메시지 로드를 `limit=40`으로 줄이고 lazy load하면, 프론트 UX는 개선되지만 **embedding 생성 파이프라인에는 영향 없다** — embedding은 메시지 INSERT 시 서버 사이드에서 비동기 생성된다. 그러나 이 사실이 V1에 명시되어 있지 않아, 향후 "API에서 안 보내는 필드는 DB에도 안 넣어도 되겠지?"라는 오해가 생길 위험이 있다.

**보완**: 다음을 명문화한다:
```
DB 스키마 변경 금지 원칙:
  - chat_messages 테이블의 모든 컬럼은 유지한다
  - API payload 축소는 SELECT 컬럼 제한으로만 구현한다
  - embedding, quality_score, quality_details, thinking 등은 
    API에서 제외하더라도 DB INSERT/UPDATE는 그대로 유지한다
```

---

## 4. 보완된 Phase 설계

### Phase 0: 측정 계측 (V1 원안 유지 + 보완)

V1 원안 그대로 + 추가:
- **Auto-RAG 히트율 측정**: 경량화 전후 Auto-RAG의 cross-session 검색 결과 수, 평균 similarity score를 기록
- **quality_score 분포 측정**: 경량화 전후 평균 quality_score 변화 감시 (변화 시 경보)

### Phase 1: 초기 로드 경량화 (V1 + 보완)

| 항목 | V1 원안 | V2 보완 |
|------|---------|---------|
| limit | 100→40 | ✅ 유지 |
| fields=minimal | id, role, content_preview, intent, created_at, edited_at | + `model_used`, `quality_score`, `has_attachments(bool)`, `has_tools(bool)` 추가 |
| content_preview | 미정의 | **200자** + `content_length` 필드 추가 (펼침 판단용) |
| DB 스키마 | 미언급 | **변경 없음** 명문화 |

### Phase 2: 폴링 재조회 제거 (V1 + 보완)

| 항목 | V1 원안 | V2 보완 |
|------|---------|---------|
| revision 3종 | message/placeholder/artifact | ✅ 유지 |
| bump 트리거 | 미정의 | **7개 트리거 명확화** (위 3.2 참조) |
| quality_score bump | 미고려 | UPDATE 후 0.5초 debounce로 message_revision bump |
| SSE complete 시 | 미언급 | streaming complete 시점에 반드시 message_revision bump |

### Phase 3: 리스트 가상화와 컴포넌트 분리 (V1 원안 유지)

V1 원안 그대로. 추가 회귀 위험 없음.

### Phase 4: artifacts lazy load (V1 원안 유지)

V1 원안 그대로. artifact_revision bump 트리거만 보완 (위 3.2).

---

## 5. 맥락 이해도 · 진화 보호 체크리스트

경량화 배포 전 반드시 확인할 항목:

| # | 체크 항목 | 검증 방법 | 합격 기준 |
|---|----------|----------|----------|
| 1 | Layer 3 conversation history가 full content를 사용하는가 | context_builder.py에서 raw_messages의 content 길이 로깅 | 모든 메시지의 content가 원본 길이와 일치 |
| 2 | Auto-RAG 시맨틱 검색이 정상 동작하는가 | 경량화 전후 동일 쿼리로 검색 결과 비교 | top-5 결과의 similarity score 차이 < 0.01 |
| 3 | quality_score가 정상 기록되는가 | self_evaluator 로그 확인 | 응답 100%에 quality_score 기록 (NULL 0%) |
| 4 | memory_facts 신규 생성이 유지되는가 | 배포 후 24시간 memory_facts INSERT 건수 | 배포 전 일평균 대비 ±20% 이내 |
| 5 | session_notes 자동 저장이 유지되는가 | 20턴 대화 후 session_notes 확인 | 정상 INSERT 확인 |
| 6 | Reflexion 트리거가 유지되는가 | quality_score < 0.4 메시지 발생 시 ai_meta_memory INSERT 확인 | 트리거 정상 동작 |
| 7 | revision bump로 프론트 메시지 누락이 없는가 | 500개 메시지 세션에서 streaming 완료 후 메시지 count 비교 | DB row count = 프론트 표시 count |
| 8 | content_preview 잘림이 프론트 표시에 문제 없는가 | 200자 미만/초과 메시지 혼재 세션에서 UI 확인 | 잘린 메시지에 "더 보기" 표시, 펼침 시 full content 로드 |

---

## 6. 안전한 API 필드 분류 (최종)

### ✅ API에서 제거 가능 (DB 유지, 프론트 불필요)

| 필드 | 이유 |
|------|------|
| `thinking` | Layer 3에서 이미 제거됨. 프론트 표시 불필요 |
| `edit_history` | 내부 메타데이터. 편집 여부는 `edited_at`으로 판단 |
| `metadata` | 서버 내부용. context_builder/auto_rag 미사용 |
| `tokens_in` / `tokens_out` | 비용 추적 서버 사이드 전용 |
| `cost` | 서버 사이드 집계 전용 |
| `sources` (조건부) | 검색 결과 표시가 필요한 메시지만 lazy load |
| `tools_called` (조건부) | 도구 호출 표시가 필요한 메시지만 lazy load |
| `embedding` | 1536차원 벡터. API 전송 자체가 낭비 |

### ⚠️ API에서 유지 필수

| 필드 | 이유 |
|------|------|
| `id` | 메시지 식별 |
| `session_id` | 세션 귀속 |
| `role` | user/assistant 구분 |
| `content` (full 또는 preview) | 표시 필수. minimal 시 200자 preview + content_length |
| `intent` | 메시지 유형 배지 표시 |
| `model_used` | 모델 배지 표시 |
| `quality_score` | 품질 표시 (선택적) |
| `created_at` | 시간 표시 |
| `edited_at` | 편집 여부 표시 |
| `bookmarked` | 북마크 UI 상태 |

### 🔒 DB에서 절대 제거 불가

| 필드 | 의존 시스템 |
|------|------------|
| `embedding` | Auto-RAG 시맨틱 검색 (Layer 4.5) |
| `content` (full) | Layer 3 conversation history, Auto-RAG |
| `quality_score` / `quality_details` | 진화 시스템 (Reflexion, Sleep-Time) |
| `thinking` | 향후 thinking 분석 파이프라인 가능성 |

---

## 7. 예상 효과 (실측 기반 추정)

| 지표 | 현재 (추정) | Phase 1 후 | Phase 2 후 | 출처 |
|------|------------|-----------|-----------|------|
| 초기 메시지 payload | ~200KB (100건 full) | ~40KB (40건 minimal) | 동일 | [코드: limit=100, SELECT *] |
| idle 폴링 시 /messages 호출 | 2~5회/분 | 동일 | **0회** (revision 기반) | [코드: just_completed 등 분기] |
| 프론트 렌더링 (500건 세션) | 전체 DOM | 동일 | 동일 (Phase 3에서 가상화) | [미측정] |
| LLM 맥락 품질 | 기준선 | **변화 없음** | **변화 없음** | [구조 분석: 경로 분리] |
| 진화 시스템 기능 | 기준선 | **변화 없음** | **변화 없음** | [구조 분석: 별도 테이블] |
| Auto-RAG 히트율 | 기준선 | **변화 없음** | **변화 없음** | [구조 분석: embedding 유지] |

---

## 8. 실행 권장안

| 순서 | 작업 | 규모 | 위험 | 권장 방식 |
|------|------|------|------|----------|
| 1 | Phase 0: 계측 코드 삽입 | S | 낮음 | Pipeline Runner 1건 |
| 2 | Phase 1: fields=minimal + limit=40 | M | 낮음 | Pipeline Runner 1건 (백엔드) + 1건 (프론트) |
| 3 | 배포 후 체크리스트 8항목 검증 | - | - | 수동 + 자동 테스트 |
| 4 | Phase 2: revision 기반 폴링 | M | 중간 | Pipeline Runner 1건 (streaming-status 확장) |
| 5 | Phase 3: 가상화 + 컴포넌트 분리 | L | 중간 | Pipeline Runner 2건 (병렬) |
| 6 | Phase 4: artifacts lazy load | S | 낮음 | Pipeline Runner 1건 |

**총 예상**: Runner 6~7건, 단계별 배포·검증. Phase 1~2만으로 체감 개선의 80%를 달성할 수 있다.

---

## 9. 결론

V1 원안의 경량화 방향은 **올바르며, 맥락 이해도와 진화 시스템에 영향을 주지 않는다**. 이는 AADS 아키텍처가 "프론트 표시 경로"와 "LLM 컨텍스트/진화 경로"를 **구조적으로 분리**하고 있기 때문이다.

다만 V2에서 보완한 4가지 회귀 위험(minimal 필드 누락, revision bump 미정의, 프론트캐시-LLM 혼용 방지, DB 스키마 변경 금지 명문화)을 반영해야 안전한 배포가 가능하다.

**핵심 원칙**: API payload를 줄이되, DB 스키마는 건드리지 않는다. 프론트 캐시와 LLM 입력은 절대 혼용하지 않는다.
