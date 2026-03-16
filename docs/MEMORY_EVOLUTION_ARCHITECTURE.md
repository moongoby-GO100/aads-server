# AADS 채팅 AI — 메모리 저장~진화 전체 아키텍처

> 작성일: 2026-03-14 | 최종 갱신: 2026-03-16 | 대상: aads.newtalk.kr 채팅 AI 시스템
> 소스: `/root/aads/aads-server/app/`

---

## 1. 전체 개요

AADS 채팅 AI는 **3-Tier 12-Feature 메모리 아키텍처**와 **Evolution Engine(12개 피드백 루프)**를 통해
대화 맥락 유지 → 사실 추출 → 시맨틱 검색 → 자기 평가 → 자기 개선까지 자동 수행한다.

```
┌─────────────────────────────────────────────────────────────────┐
│                    사용자 메시지 수신                              │
└───────────────────────────┬─────────────────────────────────────┘
                            ▼
┌─────────────────── Phase A: 컨텍스트 조립 (DB→release) ──────────┐
│                                                                  │
│  ┌─────────┐  ┌──────────┐  ┌──────────┐  ┌─────────┐          │
│  │ Layer 1  │  │ Layer 2  │  │Layer 2.5 │  │Layer 4.5│          │
│  │ 시스템   │  │ 런타임   │  │ 프리로드 │  │ Auto-RAG│          │
│  │ 프롬프트 │  │ 상태정보 │  │ 워크스페이│  │ 시맨틱  │          │
│  │ (캐시)   │  │ (동적)   │  │ 스 사실  │  │ 검색    │          │
│  └─────────┘  └──────────┘  └──────────┘  └─────────┘          │
│       │              │             │              │              │
│       └──────────────┴─────────────┴──────────────┘              │
│                            ▼                                     │
│              ┌─────────────────────────┐                         │
│              │  Memory Recall 6섹션     │                         │
│              │  (~2,300 토큰)           │                         │
│              └─────────────────────────┘                         │
│                            ▼                                     │
│              ┌─────────────────────────┐                         │
│              │  Layer D: 첨부문서       │                         │
│              │  (현재 턴만, 자동제거)    │                         │
│              └─────────────────────────┘                         │
│                            ▼                                     │
│              ┌─────────────────────────┐                         │
│              │  Layer 4: 자기인식       │                         │
│              │  (진화 메트릭 실시간)     │                         │
│              └─────────────────────────┘                         │
│                            ▼                                     │
│              ┌─────────────────────────┐                         │
│              │  Layer 3: 대화 히스토리   │                         │
│              │  (마스킹+압축)           │                         │
│              └─────────────────────────┘                         │
│                                                                  │
│  → DB 커넥션 해제 ✓                                               │
└──────────────────────────────────────────────────────────────────┘
                            ▼
┌─────────────────── Phase B: LLM 스트리밍 (DB 미사용) ────────────┐
│  Claude API 호출 → SSE 스트리밍 → 도구 실행                       │
└──────────────────────────────────────────────────────────────────┘
                            ▼
┌─────────────────── Phase C: 저장 + 백그라운드 (별도 conn) ────────┐
│                                                                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐        │
│  │ 메시지   │  │ F2 사실  │  │ F5 도구  │  │ F11 자기 │        │
│  │ DB 저장  │  │ 추출     │  │ 아카이브 │  │ 평가     │        │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘        │
│                ┌──────────┐  ┌──────────┐  ┌──────────┐        │
│                │ F8 CEO   │  │ B4 모순  │  │ 팩트체크 │        │
│                │ 패턴추적 │  │ 감지     │  │ 3단계    │        │
│                └──────────┘  └──────────┘  └──────────┘        │
└──────────────────────────────────────────────────────────────────┘
                            ▼
┌────────────── 야간 배치 (04:00~05:00 UTC) ───────────────────────┐
│  F4 통합 → C3 망각곡선 → C1 인사이트 → C2 프롬프트 최적화         │
└──────────────────────────────────────────────────────────────────┘
```

---

## 2. 메모리 레이어 상세

### 2.1 Layer 1 — 시스템 프롬프트 (정적, 캐시)
| 항목 | 값 |
|------|-----|
| 파일 | `system_prompt_v2.py` |
| 크기 | ~1,400 토큰 |
| 갱신 | 워크스페이스별 캐시, 코드 변경 시만 |
| 내용 | 역할/능력/도구/규칙/응답가이드 |

### 2.2 Layer 2 — 런타임 동적 정보
| 항목 | 값 |
|------|-----|
| 크기 | ~300 토큰 |
| 내용 | 현재시각, 완료 작업, 대기 작업 수 |

### 2.3 Layer 2.5 — 워크스페이스 프리로드 (F6)
| 항목 | 값 |
|------|-----|
| 파일 | `workspace_preloader.py` |
| 크기 | ~1,000 토큰 |
| 내용 | 프로젝트별 최근 사실 top-10 + 마지막 세션 요약 + CEO 패턴 예측(A3) |
| 소스 | `memory_facts`, `session_notes`, `ceo_interaction_patterns` |

### 2.4 Layer 3 — 대화 히스토리
| 항목 | 값 |
|------|-----|
| 크기 | ~3,000–5,000 토큰 |
| 마스킹 | 오래된 도구 결과 → placeholder, 코드 >3000자 → 요약 |
| 압축 | 총 80K 토큰 초과 시 구조적 요약(Compaction) 트리거 |

### 2.5 Layer 4.5 — Auto-RAG (F1/F3)
| 항목 | 값 |
|------|-----|
| 파일 | `auto_rag.py` |
| 크기 | ~2,000 토큰 |
| 검색 | 사용자 메시지 → Gemini 768차원 임베딩 → pgvector HNSW |
| 스코어링 | **유사도 × 최신성 × 중요도** (A2 3중 스코어) |
| 재질문감지 | 30분 내 고유사도(>0.85) → 재질문 경고 (A4) |
| 크로스세션 | 타 세션 결과 가중치 0.85 (F3) |

### 2.6 Layer D — 임시 문서
| 항목 | 값 |
|------|-----|
| 파일 | `document_context.py` |
| 동작 | 파일 첨부 → 현재 턴만 전문 주입 → 다음 턴 자동 제거 |
| 제한 | 30K 토큰 이하=전문, 초과=앞뒤 분할 |
| 지원 | PDF(pdfplumber), Excel(openpyxl), 텍스트 |

### 2.7 Layer 4 — 자기인식 (Self-Awareness)
| 항목 | 값 |
|------|-----|
| 파일 | `system_prompt_v2.py` (lines 407-469, `LAYER4_SELF_AWARENESS_TEMPLATE`) |
| 크기 | ~200 토큰 |
| 갱신 | 매 턴 — `context_builder`에서 실시간 수치 주입 |

AI에게 자신의 진화 상태를 실시간으로 인식시키는 레이어. `build_layer1()`에서 시스템 프롬프트 말미에 주입된다.

**실시간 메트릭:**
- `fact_count`: memory_facts 총 건수
- `obs_count`: ai_observations 총 건수
- `avg_quality`: 응답 품질 평균 (%)
- `error_pattern_count`: 축적된 에러 패턴 수

**"나는 어떻게 진화하는가" 섹션 — AI가 인식하는 자기 구조:**
- **memory_facts**: 대화에서 사실 추출·저장 → 자주 참조된 기억은 confidence 강화
- **quality_score**: 매 응답 품질 자동 평가 (0~1점)
- **Reflexion**: 품질 40% 미만 시 반성문 자동 생성 → error_pattern으로 저장
- **confidence 조정**: 자주 참조된 기억 강화(+0.02/회), 오래된 기억 감쇠
- **Sleep-Time Agent**: 매일 14:00 KST(05:00 UTC) 기억 정제 및 통합
- **error_pattern 경고**: 유사 작업 시 과거 실패 자동 경고 주입
- **CEO 패턴 예측**: 시간대/요일별 관심사 선제 준비
- **P4 필터**: discovery 카테고리 confidence 0.5 미만 자동 제외
- **팩트체크 엔진**: DB+웹 3단계 교차 검증 (VERIFIED/UNCERTAIN/DISPUTED)

---

## 3. Memory Recall — 6섹션 주입 시스템

**파일**: `memory_recall.py` | **실행**: 매 턴 | **총예산**: ~2,300 토큰

```
┌───────────────────────────────────────────────────────────┐
│  섹션1: 세션 노트         │ 500토큰 │ session_notes 최근3건   │
│  섹션2: CEO 선호           │ 300토큰 │ ai_observations ≥0.2   │
│  섹션3: 도구 전략          │ 400토큰 │ ai_observations ≥0.3   │
│  섹션4: 활성 지시사항      │ 400토큰 │ directive_lifecycle     │
│  섹션5: 발견/학습          │ 400토큰 │ ai_observations ≥0.4   │
│  섹션6: 학습된 메모리      │ 300토큰 │ ai_meta_memory          │
└───────────────────────────────────────────────────────────┘
```

- 6개 비동기 쿼리 병렬 실행 (~50-100ms)
- 프로젝트 정규화 (대문자)
- 신뢰도 임계값 환경변수로 제어

---

## 4. 메모리 쓰기(저장) 파이프라인

### 4.1 F2: 사실 추출 (`fact_extractor.py`)
```
AI 응답 완료
    ↓
Haiku가 응답 분석 → JSON 추출 (최대 5건/턴)
    ↓
memory_facts INSERT + 비동기 임베딩 생성
```

**추출 카테고리**: decision, file_change, config_change, error_resolution, ceo_instruction, error_pattern, timeline_event

### 4.2 F5: 도구 결과 아카이브 (`tool_archive.py`)
```
도구 실행 완료
    ↓
tool_results_archive INSERT (tool_name, params, output ≤500KB, is_error)
    ↓
동일 도구 호출 시 재사용 가능
```

### 4.3 F8: CEO 패턴 추적 (`ceo_pattern_tracker.py`)
```
CEO 메시지 수신
    ↓
시간대/요일/워크스페이스/의도 분석
    ↓
ceo_interaction_patterns UPSERT (원자적, TOCTOU-safe)
    ↓
다음 세션 프리로드에 활용 (A3 예측)
```

### 4.4 관찰 자동 저장 (`memory_recall.py::save_observation`)
```
ai_observations UPSERT (ON CONFLICT → GREATEST(confidence))
카테고리: ceo_preference, tool_strategy, discovery, learning 등
```

### 4.5 팩트체크 엔진 — 3단계 교차 검증 (`fact_checker.py`)

**파일**: `app/services/fact_checker.py` (595줄) + `app/api/fact_check.py` (API)

```
클레임 텍스트 입력
    ↓
┌─── Stage 1: DB 검증 ──────────────────────┐
│ memory_facts, ai_observations, chat_messages │
│ 에서 관련 기록 검색 → DB 근거 확보           │
└──────────────────────────────────────────────┘
    ↓
┌─── Stage 2: 웹 교차 검증 ─────────────────┐
│ Google + Naver 병렬 검색                    │
│ 검색 결과와 클레임 대조                      │
└──────────────────────────────────────────────┘
    ↓
┌─── Stage 3: 판정 + 신뢰도 산출 ───────────┐
│ Verdict:                                    │
│   VERIFIED     — DB+웹 모두 확인            │
│   DB_ONLY      — DB에만 근거 존재           │
│   WEB_ONLY     — 웹에만 근거 존재           │
│   UNCERTAIN    — 근거 불충분                │
│   DISPUTED     — 상충하는 근거 발견         │
│   UNVERIFIED   — 검증 불가                  │
└──────────────────────────────────────────────┘
```

**API 엔드포인트:**
- `POST /api/v1/fact-check/verify` — 단건 팩트체크
- `POST /api/v1/fact-check/verify-batch` — 배치 팩트체크 (최대 10건)

---

## 5. Evolution Engine — 12개 피드백 루프

### Phase A: 품질 → 신뢰도 연동 (매 턴)

| ID | 기능 | 파일 | 트리거 |
|----|------|------|--------|
| **A1** | 품질↔신뢰도 연동 | `self_evaluator.py` | quality_score < 0.5 → 관련 사실 신뢰도 하락 |
| **A2** | 3중 검색 스코어 | `auto_rag.py` | 유사도 × 최신성(14일 반감기) × 중요도 |
| **A3** | CEO 패턴 예측 | `ceo_pattern_tracker.py` → `workspace_preloader.py` | 매 턴 프리로드 |
| **A4** | 재질문 감지 | `auto_rag.py` | 30분 내 유사도 >0.85 |

### Phase B: 반성 & 에러 학습 (매 턴 + 배치)

| ID | 기능 | 파일 | 트리거 |
|----|------|------|--------|
| **B1** | Reflexion 반성 | `self_evaluator.py` | quality_score < 0.4 → Haiku 자기반성 → error_pattern 저장 |
| **B2** | CEO 수정 학습 | `chat_service.py` | `edited_at` 필드 감지 → 교정 학습 |
| **B3** | 도구 효율 분석 | `memory_gc.py` | 주간 배치: 에러율 >30% 또는 avg_tokens >3000 플래그 |
| **B4** | 모순 자동 해결 | `contradiction_detector.py` | CEO 지시 키워드(변경/취소/결정 등) → 이전 사실 supersede |

### Phase B 확장: should_stop_generation()

`self_evaluator.py`에 구현된 생성 중단 메커니즘:

- **3연속 저품질 중단**: 최근 3턴 연속 quality_score < 0.3 → 생성 즉시 중단
- **5턴 하락 트렌드 중단**: 최근 5턴 지속적 품질 하락 감지 → 생성 중단

중단 시 AI는 품질 저하 원인을 자가 분석하고 에러 패턴으로 기록한다.

### Phase C: 오프라인 최적화 (야간 배치)

| ID | 기능 | 파일 | 스케줄 |
|----|------|------|--------|
| **C1** | Sleep-Time Agent | `memory_gc.py` | 05:00 UTC — 프로젝트별 인사이트 1-3건 생성 |
| **C2** | 프롬프트 자동최적화 | `memory_gc.py` | 05:00 UTC — quality_score <0.5 → 교정 지시 생성 |
| **C3** | 적응형 망각곡선 | `memory_gc.py` | 04:00 UTC — 카테고리별 차등 decay |
| **C4** | 결정 의존성 그래프 | `ceo_chat_tools.py` | CEO 도구 호출 — BFS depth 3 탐색 |

### P-Series: 진화 품질 개선

| ID | 기능 | 파일 | 동작 |
|----|------|------|------|
| **P2** | 에러 패턴 경고 | `workspace_preloader.py` | 프리로드 시 error_pattern top-3 경고 주입 (최고 우선순위) |
| **P3** | Reflexion 효과 추적 | `self_evaluator.py` | 반성 후 개선(+0.05 effective boost) / 미개선(×0.85 decay) |
| **P4** | Discovery 신뢰도 필터 | `auto_rag.py`, `workspace_preloader.py` | confidence < 0.5 → recall + preloader에서 자동 제외 |
| **P5** | 참조 비례 부스팅 | `memory_gc.py` | 5+회→+0.03, 10+회→+0.05, 20+회→+0.08 부스트 |

---

## 5.5 품질 평가 파이프라인 (`eval_pipeline.py`)

**파일**: `app/services/eval_pipeline.py` (675줄)

응답 품질을 정량적으로 추적하고 회귀를 감지하는 파이프라인.

### 평가 메트릭 (5개)
| 메트릭 | 가중치 | 설명 |
|--------|--------|------|
| accuracy | 20% | 사실 정확성 |
| completeness | 20% | 답변 완전성 |
| relevance | 15% | 질문 관련성 |
| **tool_grounding** | **30%** | 도구 기반 근거 비율 (가장 중요) |
| actionability | 15% | 실행 가능성 |

### 주요 함수
| 함수 | 동작 |
|------|------|
| `aggregate_quality_stats()` | 워크스페이스별 7일 윈도우 통계 (평균/분포/트렌드) |
| `detect_quality_regression()` | 직전 기간 대비 15% 이상 하락 → 경고 생성 |
| `generate_weekly_report()` | 마크다운 주간 리포트 — 워크스페이스별 분포 + 상위/하위 응답 |
| `calculate_grounding_score()` | 도구 호출 기반 근거 점수 산출 |
| `record_ab_metric()` | A/B 테스트 메트릭 추적 |

---

## 5.6 시맨틱 캐시 (`semantic_cache.py`)

**파일**: `app/services/semantic_cache.py` (417줄)

유사 질문에 대한 고품질 응답을 재활용하여 비용과 지연을 줄이는 캐시.

```
사용자 메시지
    ↓
Gemini 임베딩 생성 (768차원)
    ↓
ai_meta_memory (category='semantic_cache') 검색
    ↓ 유사도 ≥ 0.92
캐시 히트 → 저장된 응답 반환 (LLM 미호출)
    ↓ 유사도 < 0.92
캐시 미스 → LLM 호출 → 조건 충족 시 캐시 저장
```

| 설정 | 값 |
|------|-----|
| 유사도 임계값 | ≥ 0.92 (`SEMANTIC_CACHE_SIMILARITY`) |
| 품질 게이트 | quality_score ≥ 0.7 |
| 캐시 조건 | 도구 사용 응답만 캐시 |
| TTL | 24시간 (`SEMANTIC_CACHE_TTL_HOURS`) |
| 최대 항목 | 500건 (`SEMANTIC_CACHE_MAX_ENTRIES`) |
| 저장소 | `ai_meta_memory` 테이블 (category=`semantic_cache`) |
| 격리 | 워크스페이스별 분리 — 타 워크스페이스 캐시 미참조 |

---

## 5.7 멀티에이전트 오케스트레이션 (`agent_orchestrator.py`)

**파일**: `app/services/agent_orchestrator.py` (722줄)

복잡한 작업을 역할 기반 에이전트 팀으로 분할·실행하는 오케스트레이터.

### 핵심 컴포넌트

| 컴포넌트 | 설명 |
|----------|------|
| **AgentMessage** | 에이전트 간 통신 메시지 (6종: request, response, discovery, error, status, complete) |
| **SharedDiscoveryStore** | 에이전트 간 발견 공유 저장소 (pub/sub + 태그 기반 검색) |
| **AgentOrchestrator** | DAG 기반 작업 스케줄링 + 비용 제어 |
| **AgentTeam** | 역할 기반 에이전트 팀 단계별 실행 |

### 에이전트 역할 (5종)

| 역할 | 담당 |
|------|------|
| `researcher` | 조사·분석 — 코드/DB/로그 탐색 |
| `developer` | 구현 — 코드 작성/수정/테스트 |
| `qa` | 품질 보증 — 테스트 실행/회귀 검증 |
| `devops` | 배포·인프라 — 서버/Docker/CI/CD |
| `architect` | 설계 — 아키텍처 리뷰/의사결정 |

### 도구 통합

`run_agent_team` 도구로 채팅 AI에서 직접 멀티에이전트 팀을 호출할 수 있다:
```
CEO: "KIS 백엔드 리팩토링해줘"
    ↓
run_agent_team(task="KIS 백엔드 리팩토링", roles=["researcher","developer","qa"])
    ↓
AgentOrchestrator → DAG 스케줄링 → 순차/병렬 실행 → 결과 통합
```

---

## 6. 망각곡선 (C3) — 카테고리별 Decay Rate

```
ceo_instruction ──── 0.99 ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓ (거의 영구)
decision ─────────── 0.98 ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
timeline_event ───── 0.95 ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
error_pattern ────── 0.93 ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
error_resolution ─── 0.92 ▓▓▓▓▓▓▓▓▓▓▓▓▓▓
file_change ──────── 0.90 ▓▓▓▓▓▓▓▓▓▓▓▓▓
config_change ────── 0.85 ▓▓▓▓▓▓▓▓▓▓▓  (빠른 소멸)
기본값 ──────────── 0.95 ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
```

- 14일 이상 미참조 + 생성 14일 이상 → decay 적용
- 참조 비례 부스트 (P5): 5+회→+0.03, 10+회→+0.05, 20+회→+0.08
- 삭제 임계: 신뢰도 < 0.1 + 30일 경과

---

## 7. DB 스키마 핵심 테이블

### 7.1 memory_facts (12-Feature 허브)
```sql
id              UUID PK
session_id      UUID FK → chat_sessions (SET NULL)
workspace_id    UUID FK → chat_workspaces (SET NULL)
project         VARCHAR(20)     -- KIS, AADS, SF, GO100, NTV2, NAS
category        VARCHAR(30)     -- decision, ceo_instruction, error_pattern, ...
subject         VARCHAR(300)
detail          TEXT
embedding       vector(768)     -- Gemini embedding-001, pgvector HNSW
confidence      FLOAT           -- 0.0~1.0
referenced_count INT
last_referenced_at TIMESTAMPTZ
superseded_by   UUID FK → memory_facts(id)  -- 버전 관리
related_facts   UUID[]          -- F9 의존성 그래프
tags            TEXT[]
created_at, updated_at TIMESTAMPTZ
```

### 7.2 ai_observations (자동 관찰)
```sql
id              SERIAL PK
category        VARCHAR(30)     -- ceo_preference, tool_strategy, discovery, ...
key             VARCHAR(100)    -- UNIQUE(category, key)
value           TEXT
confidence      FLOAT
project         VARCHAR(50)
source_session_id INTEGER
created_at, updated_at TIMESTAMPTZ
```

### 7.3 tool_results_archive (도구 이력)
```sql
id              UUID PK
message_id      UUID FK → chat_messages (CASCADE)
tool_use_id     VARCHAR(100)    -- UNIQUE(message_id, tool_use_id)
tool_name       VARCHAR(100)
input_params    JSONB
raw_output      TEXT (≤500KB)
output_tokens   INT
is_error        BOOLEAN
created_at      TIMESTAMPTZ
```

### 7.4 ceo_interaction_patterns (CEO 행동 패턴)
```sql
id              SERIAL PK
pattern_type    VARCHAR(30)     -- time_of_day, day_of_week, workspace_topic
pattern_key     VARCHAR(200)    -- UNIQUE(pattern_type, pattern_key)
pattern_value   JSONB
confidence      FLOAT
created_at, updated_at TIMESTAMPTZ
```

### 7.5 chat_messages (확장 컬럼)
```sql
-- 기존 + 메모리 확장:
embedding       vector(768)     -- F1 Auto-RAG 검색
quality_score   FLOAT           -- F11 자기평가 0.0~1.0
quality_details JSONB           -- {accuracy, completeness, relevance, tool_grounding, actionability, overall}
edited_at       TIMESTAMPTZ     -- B2 CEO 수정 감지
```

### 7.6 ai_meta_memory 카테고리
```
기존:     learning, meta, correction, ...
추가:     semantic_cache          ← 5.6 시맨틱 캐시 저장소
          correction_directive    ← C2 프롬프트 최적화 교정 지시
          prompt_optimization     ← C2 프롬프트 최적화 결과
```

---

## 8. 스케줄러 (야간 배치)

```
03:00 UTC ─── gc_observations()        ai_observations 30일+ decay→삭제
03:30 UTC ─── task_logs GC             7일+ 로그 삭제
04:00 UTC ─── consolidate_memory_facts()
              ├── 참조 비례 부스트 (P5: 5+→+0.03, 10+→+0.05, 20+→+0.08)
              ├── 미참조 decay (C3 곡선)
              ├── 중복 병합 (유사도 >0.92)
              ├── 프로젝트 스냅샷 생성
              └── 도구 효율 분석 (B3)
05:00 UTC ─── sleep_time_consolidation()
              ├── C1: 프로젝트 인사이트 (Haiku)
              └── C2: 프롬프트 최적화 (quality<0.5)
```

---

## 9. 데이터 흐름 — 1회 턴 전체 사이클

```
① 사용자 메시지 → chat_service.send_message_stream()

② Phase A — 컨텍스트 조립 (DB conn → release)
   ├── 세션 히스토리 로드
   ├── Memory Recall 6섹션 (6 async 쿼리 병렬)
   ├── Auto-RAG: 임베딩 → pgvector 검색 → top-5
   ├── Workspace Preload: 프로젝트 사실 + CEO 예측 + P2 에러 경고
   ├── Document Context: 첨부 파일 처리
   ├── Layer 4 자기인식: 진화 메트릭 실시간 주입
   ├── Contradiction Detection (B4): 모순 경고
   └── DB 커넥션 해제 ✓

③ Phase B — LLM 호출 (DB 미사용)
   ├── should_stop_generation(): 3연속 <0.3 또는 5턴 하락 시 중단
   └── Claude API → SSE 스트리밍 → 도구 실행

④ Phase C — 저장 (별도 conn)
   ├── 사용자+AI 메시지 DB 저장
   └── 백그라운드 태스크 (non-blocking):
       ├── F2: 사실 추출 (Haiku → memory_facts)
       ├── F5: 도구 결과 아카이브
       ├── F8: CEO 패턴 추적
       ├── F11: 자기평가 (quality_score) + P3 Reflexion 효과 추적
       └── B1: 품질 <0.4 시 Reflexion 반성
```

---

## 10. 토큰 예산 & 비용

### 매 턴 주입량
| 컴포넌트 | 예산 | 모델 |
|----------|------|------|
| Layer 1 시스템 | ~1,400 | - (캐시) |
| Layer 2 동적 | ~300 | - |
| Layer 2.5 프리로드 | ~1,000 | - (DB 검색) |
| Memory Recall 6섹션 | ~2,300 | - (DB 검색) |
| Auto-RAG | ~2,000 | - (임베딩 검색) |
| Layer 4 자기인식 | ~200 | - |
| Layer D 문서 | 0~60,000 | - |
| **총 오버헤드** | **~7,200** | |

### 매 턴 백그라운드 비용
| 컴포넌트 | 비용/턴 |
|----------|---------|
| F2 사실추출 | ~$0.0005 (Haiku) |
| F11 자기평가 | ~$0.0003 (Haiku) |
| B1 Reflexion | ~$0.0002 (조건부) |
| **턴당 총합** | **~$0.001** |

### 야간 배치 비용
| 컴포넌트 | 비용/일 |
|----------|---------|
| C1 인사이트 | ~$0.05 |
| C2 최적화 | ~$0.025 |
| **일간 총합** | **~$0.075** |
| **월간 추정** | **~$2.50** |

---

## 11. 파일 인벤토리

```
/root/aads/aads-server/app/
├── core/
│   ├── memory_recall.py          ← 6섹션 컨텍스트 주입
│   ├── memory_gc.py              ← GC + 통합 + C1/C2/C3
│   ├── document_context.py       ← Layer D 임시 문서
│   ├── token_utils.py            ← 한국어 토큰 추정
│   └── prompts/
│       └── system_prompt_v2.py   ← Layer 1 + Layer 4 자기인식 (LAYER4_SELF_AWARENESS_TEMPLATE)
├── services/
│   ├── context_builder.py        ← 3+D 레이어 오케스트레이션
│   ├── auto_rag.py               ← F1/F3 시맨틱 검색
│   ├── fact_extractor.py         ← F2 사실 추출
│   ├── fact_checker.py           ← 팩트체크 3단계 엔진 (DB+웹 교차 검증)
│   ├── tool_archive.py           ← F5 도구 결과 캐시
│   ├── workspace_preloader.py    ← F6 프로젝트 프리로드 + P2 에러 경고
│   ├── self_evaluator.py         ← F11/B1 자기평가+반성 + P3 효과추적 + should_stop_generation()
│   ├── contradiction_detector.py ← F10/B4 모순 감지
│   ├── ceo_pattern_tracker.py    ← F8/A3 CEO 패턴
│   ├── chat_embedding_service.py ← Gemini 768차원 임베딩
│   ├── chat_service.py           ← 메인 오케스트레이션 + 스트리밍 복구
│   ├── memory_manager.py         ← Layer 2/4 관리
│   ├── eval_pipeline.py          ← 품질 평가 파이프라인 (5메트릭, 주간 리포트)
│   ├── semantic_cache.py         ← 시맨틱 캐시 (유사도 ≥0.92, 24h TTL)
│   └── agent_orchestrator.py     ← 멀티에이전트 오케스트레이션 (DAG, 5역할)
├── api/
│   ├── ceo_chat_tools.py         ← C4 의존성그래프, F12 타임라인 도구
│   ├── fact_check.py             ← 팩트체크 API (verify, verify-batch)
│   └── memory.py                 ← 메모리 API
└── migrations/
    ├── 024_memory_tables.sql     ← session_notes, ai_meta_memory
    ├── 025_ai_observations.sql   ← ai_observations
    ├── 029_chat_message_embedding.sql ← vector(768)
    └── 031_memory_upgrade.sql    ← memory_facts, tool_results_archive,
                                     ceo_interaction_patterns, quality_score
```

---

## 12. 핵심 환경변수

```bash
# 메모리 설정
COMPACTION_TRIGGER_TOKENS=80000
COMPACTION_KEEP_RECENT=20
OBSERVATION_WINDOW_SIZE=20

# 신뢰도 임계값
CONFIDENCE_CEO_PREF=0.2
CONFIDENCE_TOOL_STRATEGY=0.3
CONFIDENCE_DISCOVERY=0.4

# GC 설정
MEMORY_GC_MAX_AGE_DAYS=30
MEMORY_GC_DECAY_FACTOR=0.9
MEMORY_GC_DELETE_THRESHOLD=0.1

# 통합 설정
CONSOLIDATION_SIMILARITY=0.92
CONSOLIDATION_REFERENCED_BOOST=0.05
FACTS_DECAY_DAYS=14
FACTS_DECAY_FACTOR=0.95

# 자기평가
SELF_EVAL_ENABLED=true
SELF_EVAL_MIN_LEN=200
SELF_EVAL_MODEL=claude-haiku-4-5-20251001

# 시맨틱 캐시
SEMANTIC_CACHE_SIMILARITY=0.92
SEMANTIC_CACHE_TTL_HOURS=24
SEMANTIC_CACHE_MAX_ENTRIES=500
SEMANTIC_CACHE_MIN_QUALITY=0.7

# 워크스페이스 프리로드
WORKSPACE_PRELOAD_ENABLED=true
WORKSPACE_PRELOAD_TOKENS=1000

# Sleep-Time Agent
SLEEP_AGENT_MODEL=claude-haiku-4-5-20251001

# 기능 토글
AUTO_RAG_ENABLED=true
CONTRADICTION_DETECTION_ENABLED=true
CEO_PATTERN_TRACKING_ENABLED=true
LANGFUSE_ENABLED=true
```

---

## 13. 아키텍처 다이어그램 — 테이블 관계

```
                    chat_workspaces (7개)
                         │ 1:N
                         ▼
                    chat_sessions
                    │ 1:N        │ 1:N
                    ▼            ▼
              chat_messages    session_notes
              │ (embedding,    (note_type,
              │  quality_score) content)
              │ 1:N
              ▼
         tool_results_archive
         (tool_name, is_error)
              │
              │ B3 분석
              ▼
         ai_observations ←──── Memory Recall ────→ 시스템 프롬프트
         (auto-learned)         6섹션 주입
              ↑
              │ save_observation()
              │
         ai_meta_memory ←──── C2 프롬프트 최적화
         (manual/hybrid)       semantic_cache (5.6)
                               correction_directive
                               prompt_optimization

         memory_facts ←───── F2 사실추출 (Haiku)
         │ (embedding,       Auto-RAG 검색 (F1)
         │  confidence,      Workspace 프리로드 (F6)
         │  related_facts[], Sleep-Time 인사이트 (C1)
         │  superseded_by)   통합/병합 (F4)
         │                   모순 감지 (B4)
         │                   팩트체크 검증 (4.5)
         │ self-ref (versioning)
         └──→ memory_facts

         ceo_interaction_patterns ←── F8 CEO 패턴추적
         (pattern_type,               A3 예측 → 프리로드
          pattern_value JSONB)
```

---

## 14. 설계 원칙

1. **비동기 우선**: 모든 백그라운드 작업은 `asyncio.create_task()`, 사용자 응답 차단 없음
2. **3-Phase 커넥션 관리**: DB conn 점유 수백ms×2 (Phase A/C만), Phase B는 conn 없음
3. **신뢰도 기반 랭킹**: 모든 검색/주입은 confidence × recency × importance 정렬
4. **Supersession 모델**: 삭제 없이 `superseded_by` FK로 버전 관리
5. **카테고리별 수명**: CEO 지시(0.99) → 설정변경(0.85) 차등 decay
6. **보호 카테고리**: ceo_preference, ceo_directive, compaction_directive는 GC 면제
7. **원자적 DB 연산**: ON CONFLICT + GREATEST()로 경쟁조건 방지
8. **프로젝트 필터링**: `project = $1 OR project IS NULL` 패턴 일관 적용

---

## 15. 스트리밍 자동 복구

### 서버 재시작 시 중단 스트리밍 복구

```
서버 재시작
    ↓
resume_interrupted_streams()
    ↓
chat_messages에서 streaming_placeholder 남은 세션 탐색
    ↓
┌─── _resume_single_stream() ──────────────────┐
│ 1. 세션 히스토리에서 중간 결과 보존             │
│ 2. 이전 컨텍스트 기반 LLM 재호출               │
│ 3. placeholder를 최종 응답으로 교체             │
│ 실패 시 → placeholder 삭제 폴백               │
└──────────────────────────────────────────────────┘
```

**실행 시점**: `main.py`에서 DB pool 초기화 직후 자동 실행

### 수동 복구 API

`POST /chat/sessions/{id}/resume` — 특정 세션의 중단된 스트리밍을 수동 재개

### 클라이언트 연결 해제 시 (with_background_completion)

```
클라이언트 disconnect 감지
    ↓
producer(LLM 스트리밍) 계속 실행
    ↓
15초마다 중간 결과 DB 저장 (interim save)
    ↓
완료 시 최종 응답 DB 저장
    ↓
사용자 재접속 시 완성된 응답 확인 가능
```
