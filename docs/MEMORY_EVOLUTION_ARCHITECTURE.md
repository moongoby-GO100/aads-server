# AADS AI 메모리 진화 시스템 아키텍처

> **버전**: 2.0 | **갱신일**: 2026-03-29 | **작성**: AADS PM/CTO AI
> **이전 버전**: 1.0 (2026-03-16)

---

## 목차

1. [전체 개요](#1-전체-개요)
2. [메모리 레이어 상세](#2-메모리-레이어-상세)
3. [Memory Recall — 10섹션 주입 시스템](#3-memory-recall--10섹션-주입-시스템)
4. [메모리 쓰기(저장) 파이프라인](#4-메모리-쓰기저장-파이프라인)
5. [Evolution Engine — 피드백 루프](#5-evolution-engine--피드백-루프)
6. [망각곡선 & GC](#6-망각곡선--gc)
7. [DB 스키마](#7-db-스키마)
8. [스케줄러](#8-스케줄러)
9. [데이터 흐름 — 1회 턴 전체 사이클](#9-데이터-흐름--1회-턴-전체-사이클)
10. [토큰 예산 & 비용](#10-토큰-예산--비용)
11. [파일 인벤토리](#11-파일-인벤토리)
12. [설계 원칙](#12-설계-원칙)
13. [변경 이력](#13-변경-이력)

---

## 1. 전체 개요

### 3-Phase 아키텍처

```
┌─────────────────────────────────────────────────────────────────┐
│                    CEO 메시지 수신                                │
└──────────────────────────┬──────────────────────────────────────┘
                           ▼
┌─ Phase A: 컨텍스트 조립 ──────────────────────────────────────────┐
│                                                                   │
│  context_builder.build()                                          │
│  ├── Layer 1: 시스템 프롬프트 (역할/규칙/도구)                       │
│  ├── Layer 2: 런타임 (인텐트/워크스페이스/프리로드)                   │
│  ├── Layer CKP: CKP 문서 (AADS-186D)                              │
│  ├── Layer Tool: 도구 카테고리 안내 (AADS-186D)                     │
│  ├── Layer 3: 대화 히스토리 (최근 N턴)                              │
│  └── Layer 4: 자기인식 (진화 상태 플레이스홀더)                      │
│                                                                   │
│  memory_recall.build_memory_context()  ← 10섹션 병렬 (asyncio)    │
│  ├── <session>       세션 노트 + correction 강제주입               │
│  ├── <ceo_rules>     CEO 선호/교정/결정                            │
│  ├── <tools>         도구 전략/프로젝트 패턴                        │
│  ├── <directives>    활성 지시서                                   │
│  ├── <discoveries>   발견/학습/반복이슈                             │
│  ├── <learned>       메타 메모리 (학습된 패턴)                      │
│  ├── <corrections>   반성 지시 (correction_directive)              │
│  ├── <experience_lessons> 대화 중 추출 교훈                        │
│  ├── <visual_memories>    시각 메모리                              │
│  └── <strategy_updates>   전략 수정 내역                           │
│                                                                   │
└──────────────────────────┬────────────────────────────────────────┘
                           ▼
┌─ Phase B: LLM 스트리밍 응답 ─────────────────────────────────────┐
│  model_selector → call_llm_with_fallback()                        │
│  AUTH_TOKEN(1순위) → AUTH_TOKEN_2(2순위) → Gemini LiteLLM(3순위)  │
└──────────────────────────┬────────────────────────────────────────┘
                           ▼
┌─ Phase C: 저장 + 백그라운드 ─────────────────────────────────────┐
│  ① chat_messages 저장 (스트리밍 완료 시)                           │
│  ② _detect_and_save_learning() — 매 턴 CEO 학습 신호 감지          │
│  ③ evaluate_response() — 품질 평가 (Haiku)                        │
│  ④ auto_reflexion_loop() — 키워드 기반 자기반성 (LLM-free)         │
│  ⑤ 20턴마다: session_note + auto_observe + mid_conversation_lesson│
│  ⑥ 주기적: meta_evaluator.auto_tune_memory() — confidence 조정    │
└───────────────────────────────────────────────────────────────────┘
```

---

## 2. 메모리 레이어 상세

### Layer 1: 시스템 프롬프트
- **파일**: `app/core/prompts/system_prompt_v2.py`
- **내용**: AI 역할, 행동 원칙, CEO 화법 해석, 보안 규칙, 도구 목록
- **갱신**: 코드 배포 시

### Layer 2: 런타임 컨텍스트
- **파일**: `app/services/context_builder.py`
- **내용**: 인텐트 분류 결과, 워크스페이스 정보, 프리로드 데이터
- **갱신**: 매 턴 동적 조립

### Layer CKP (AADS-186D)
- **함수**: `context_builder._build_ckp_layer()`
- **내용**: CKP 문서 (코드베이스 지식 패키지) 관련 컨텍스트
- **갱신**: CKP 스캔 시

### Layer Tool (AADS-186D)
- **함수**: `context_builder._build_tool_guide_layer()`
- **내용**: 도구 카테고리별 안내 (T1~T6 우선순위)
- **갱신**: 코드 배포 시

### Layer 3: 대화 히스토리
- **소스**: `chat_messages` 테이블
- **내용**: 최근 N턴 대화 (토큰 예산 내)
- **갱신**: 매 턴

### Layer 4: 자기인식
- **내용**: 진화 상태 플레이스홀더 (fact_count, obs_count 등)
- **현재 상태**: `get_evolution_stats()` 함수가 정의되어 있으나 **build_layer1()에서 미호출** — 고정값 "(로딩중)" 표시
- **TODO**: 실시간 수치 주입 연결 필요

### Layer Artifact
- **함수**: `context_builder._build_artifact_context_layer()`
- **내용**: 현재 대화의 아티팩트 컨텍스트
- **갱신**: 아티팩트 생성/수정 시

### Layer Semantic Code
- **함수**: `context_builder._build_semantic_code_layer()`
- **내용**: 코드 관련 질문 시 시맨틱 검색 결과
- **갱신**: code 인텐트 감지 시

---

## 3. Memory Recall — 10섹션 주입 시스템

**파일**: `app/core/memory_recall.py` (911줄)
**진입점**: `build_memory_context(session_id, project_id)`
**실행 방식**: 10개 빌더 함수를 `asyncio.gather()`로 **병렬** 실행 후 조립, 총 4,000자 상한

### 10섹션 상세

| # | 함수 | XML 태그 | DB 테이블 | 토큰 예산 | 역할 |
|---|------|---------|----------|----------|------|
| 1 | `_build_session_notes` | `<session>` | session_notes | ~500 | 최근 세션 요약 + correction 강제 주입 |
| 2 | `_build_preferences` | `<ceo_rules>` | ai_observations (ceo_preference/decision/ceo_correction) | ~300 | CEO 선호·교정·결정 |
| 3 | `_build_tool_strategy` | `<tools>` | ai_observations (project_pattern/tool_strategy) | ~400 | 도구 전략·프로젝트 패턴 |
| 4 | `_build_active_directives` | `<directives>` | directive_lifecycle (pending/running/queued) | ~400 | 활성 지시서 |
| 5 | `_build_discoveries` | `<discoveries>` | ai_observations (learning/recurring_issue/discovery) | ~400 | 발견·학습·반복 이슈 |
| 6 | `_build_learned_memory` | `<learned>` | ai_meta_memory (ceo_preference/project_pattern/known_issue/decision_history/prompt_optimization) | ~300 | 메타 메모리 (증류된 패턴) |
| 7 | `_build_correction_directives` | `<corrections>` | ai_meta_memory (correction_directive/strategy_update) | ~200 | **반성 지시 (최우선 주입)** |
| 8 | `_build_experience_lessons` | `<experience_lessons>` | ai_observations (experience_lesson) | ~300 | 대화 중 실시간 추출 교훈 |
| 9 | `_build_visual_memories` | `<visual_memories>` | ai_observations (visual_memory) | ~300 | 이미지 분석 결과 메모리 |
| 10 | `_build_strategy_updates` | `<strategy_updates>` | ai_meta_memory (strategy_update) | ~500자 고정 | 전략 수정 내역 |

### 시간 감쇠 공식 (P0-2)
모든 조회에 적용:
```sql
ORDER BY confidence * EXP(-0.1 * EXTRACT(EPOCH FROM (NOW() - updated_at)) / 86400) DESC
```
- 반감기: ~7일 (7일 경과 시 가중치 50%)
- 14일 경과 시 가중치 ~25%
- 30일 경과 시 가중치 ~5%

### correction_directive 이중 배치
correction_directive는 두 곳에 동시 주입:
1. `<corrections>` 독립 블록 (섹션 7)
2. `<session>` 내부 상단 강제 삽입

→ AI가 반드시 인지하도록 이중 보장

### 주요 유틸 함수

| 함수 | 역할 |
|------|------|
| `save_observation(category, key, content, source, confidence, project)` | UPSERT + GREATEST confidence 보호 |
| `deduplicate_observations()` | 중복 정리 → memory_archive 백업 → 삭제 |
| `get_evolution_stats(db)` | 통계 집계 (현재 미연결) |
| `check_learning_health(hours)` | 대화량 vs 학습량 비교 |
| `rescan_recent_conversations(hours)` | 건강도 이상 시 대화 재스캔 |
| `_log_memory_usage(session_id, observation_ids)` | usage_count 증가 (비차단) |

---

## 4. 메모리 쓰기(저장) 파이프라인

### 4-1. 매 턴 자동 학습 (`_detect_and_save_learning`)

**위치**: `chat_service.py:3257`, 매 턴 `asyncio.create_task()` 비차단 실행

**감지 키워드** (`_LEARNING_TRIGGERS`):

| 유형 | 키워드 | 저장 category | confidence |
|------|--------|-------------|-----------|
| correction | "아니", "틀렸", "그게 아니라", "다시 해", "잘못", "아닌데", "수정해", "바꿔", "변경해", "고쳐", "안돼", "왜 이래", "이상해" | ceo_correction | 0.7 |
| preference | "항상", "앞으로", "기억해", "절대", "반드시", "무조건", "금지", "좋겠", "해줘", "이렇게", "저렇게", "중요", "우선" | ceo_preference | 0.8 |
| positive | "잘했", "좋아", "이대로", "완벽", "훌륭", "정확", "맞아", "그래", "오케이", "OK", "좋네", "괜찮" | ceo_preference | 0.6 |

**key 형식**: `chat_learning_{md5(user_msg[:50])[:8]}`

### 4-2. 20턴 배치 (백그라운드 3종)

`_save_and_update_session()` 내부에서 20턴마다 실행:

| 태스크 | 함수 | 저장 위치 |
|--------|------|----------|
| 세션 노트 | `memory_manager.save_session_note()` | session_notes |
| 자동 관찰 | `memory_manager.auto_observe_from_session()` | ai_observations |
| 교훈 추출 | `experience_extractor.extract_mid_conversation_lessons()` | ai_observations (experience_lesson) |

### 4-3. 교훈 추출 — LLM-free 키워드/패턴 기반

**파일**: `app/memory/experience_extractor.py` (293줄)

| 함수 | 호출 시점 | 방식 |
|------|----------|------|
| `extract_and_store_experience()` | 프로젝트 완료 후 | Strategy + Lesson + Procedural Memory |
| `extract_mid_conversation_lessons()` | 매 20턴 | LLM-free 키워드 패턴 |

**감지 키워드** (`_LESSON_KEYWORDS` — 14종):

| 패턴 | 분류 |
|------|------|
| "이렇게 하면 안", "실패", "오류", "에러" | failure_pattern |
| "이건 좋았어", "성공", "효과적" | success_pattern |
| "다음부터", "앞으로", "항상", "절대", "기억해" | future_rule |
| "주의", "조심", "확인해" | caution |

추가 감지:
- **에러→성공 전환 패턴**: 이전 user 메시지의 에러 + 현재 assistant 응답의 성공 감지
- **도구 반복 사용**: 동일 도구 3회 이상 → tool preference 기록

### 4-4. 시각 메모리 저장

**파일**: `app/memory/multimodal_store.py`

| 함수 | 역할 |
|------|------|
| `store_visual_memory()` | 이미지 → SHA256 해시 → ai_observations (visual_memory) 저장 |

---

## 5. Evolution Engine — 피드백 루프

### 5-1. 품질 평가 (evaluate_response)

**파일**: `app/services/self_evaluator.py` (723줄)
**호출**: 매 AI 응답 후 백그라운드

**6기준 가중치**:
```
overall = context_awareness × 0.25
        + accuracy          × 0.25
        + completeness      × 0.15
        + tool_grounding    × 0.15
        + relevance         × 0.10
        + actionability     × 0.10
```

**LLM 사용**: Claude Haiku (비용 최소화)
**저장**: `chat_messages.quality_score`, `chat_messages.quality_details`

### 5-2. 자기반성 루프 (auto_reflexion_loop) — LLM-free

**함수**: `self_evaluator.auto_reflexion_loop(query, response, project, pool, session_id)`
**방식**: LLM 호출 없이 키워드/패턴 기반

**흐름**:
```
_calc_keyword_score() → 0.0~1.0 점수
    ├── score < 0.5 → _classify_failure_type()
    │                   ├── "지시_위반" / "도구_오류" / "형식_부적합" / "정보_부족"
    │                   └── ai_meta_memory (correction_directive) 저장
    │                   └── _check_strategy_update()
    │                        └── 3회 연속 실패 → strategy_update 저장 (escalation_needed=True)
    └── score >= 0.5 → PASS (저장 없음)
```

**키워드 점수 산출** (`_calc_keyword_score`):
- 부정 패턴 감점 (빈 약속, 추정 표현 등)
- 응답 길이 보정
- 키워드 포함 여부

### 5-3. 반복 에러 감지 (_check_repeated_errors)

**방식**: 임베딩 cosine similarity >= 0.8로 유사 에러 검색
**트리거**: 품질 평가(B1)에서 score < 0.5일 때
**결과**: 2회 이상 반복 시 Haiku LLM으로 `correction_directive` 생성

### 5-4. CEO 패턴 예측 (A4)

CEO의 시간대별/요일별 관심사 패턴 분석 → `<workspace_preload>` 예상 관심사항에 반영

### 5-5. 신뢰도 강화/감쇠 (P3)

| 조건 | 조치 |
|------|------|
| 반성 후 품질 +0.1 이상 상승 | confidence +0.05 강화 |
| 반성 후 품질 -0.05 이상 하락 | confidence ×0.85 감쇠 |

### 5-6. 시맨틱 캐시

**파일**: `app/services/semantic_cache.py` (~412줄)
**역할**: 유사 질문 캐시 → LLM 호출 절약
**방식**: 임베딩 cosine similarity 기반 캐시 히트

### 5-7. 중단 신호 (should_stop_generation)

| 조건 | 액션 |
|------|------|
| 최근 3개 score < 0.3 | 생성 중단 신호 |
| 5개 연속 하락 | 생성 중단 신호 |

---

## 6. 망각곡선 & GC

### GC 담당 모듈

> **주의**: `memory_gc.py`는 존재하지 않음. GC 역할은 아래 2개 모듈이 분담.

#### meta_evaluator.py (312줄)

**파일**: `app/memory/meta_evaluator.py`

| 함수 | 역할 |
|------|------|
| `evaluate_memory_effectiveness(project)` | 활용률(24h) + 재교정 횟수(7d) 통계 측정 (LLM-free) |
| `auto_tune_memory(project)` | confidence 자동 조정 + 결과 저장 |

**자동 튜닝 규칙**:

| 규칙 | 조건 | 조치 | 보호 카테고리 |
|------|------|------|-------------|
| 감쇠 | 활용률 < 30% | confidence -= 0.1 (최소 0.1) | ceo_preference, ceo_directive, compaction_directive 제외 |
| 상향 | 재교정 > 3회/week | confidence = 0.9 | - |
| 저장 | 항상 | ai_meta_memory (meta_evaluation) UPSERT | - |

#### memory_recall.deduplicate_observations()

- ROW_NUMBER 기반 중복 감지 → `memory_archive` 백업 → 원본 삭제

### 카테고리별 Decay Rate

| 카테고리 | 기본 confidence | 감쇠 속도 | 비고 |
|---------|---------------|----------|------|
| ceo_correction | 0.7 | 보통 | 교정 후 재발 시 상향 |
| ceo_preference | 0.8 | 느림 | 보호 카테고리 |
| ceo_directive | 1.0 | 매우 느림 | 보호 카테고리 |
| tool_strategy | 0.96 | 보통 | 도구 성능 변화에 민감 |
| project_pattern | 0.78 | 보통 | - |
| discovery | 0.41 | 빠름 | 초기 낮은 신뢰도 |
| experience_lesson | 0.6 | 보통 | 키워드 추출, 검증 전 |
| compaction_directive | 0.85 | 매우 느림 | 보호 카테고리 |

---

## 7. DB 스키마

### 핵심 테이블

| 테이블 | 역할 | 주요 컬럼 |
|--------|------|----------|
| `ai_observations` | CEO 교정/선호/발견/교훈 저장 | category, key, content, confidence, project, usage_count, last_used_at |
| `ai_meta_memory` | 시맨틱 캐시 + 교정 지시 + 전략 | category, key, content, confidence |
| `session_notes` | 대화 요약 저장 | session_id, summary, turn_count |
| `memory_facts` | 사실 추출 (confidence 강화) | category, key, content, confidence, embedding |
| `chat_messages` | 대화 메시지 + 품질 점수 | quality_score, quality_details |
| `memory_archive` | 중복 제거된 메모리 백업 | (ai_observations 구조 동일) |
| `directive_lifecycle` | 지시서 생명주기 | status, timestamps |
| `ceo_interaction_patterns` | CEO 행동 패턴 분석 | hour, day_of_week, workspace, intent |

### 실측 데이터 (2026-03-29)

| 테이블 | 건수 |
|--------|------|
| ai_observations | 274 |
| ai_meta_memory | 601 |
| session_notes | 182 |

---

## 8. 스케줄러

### 이벤트 기반 자동 업데이트

**파일**: `app/memory/auto_update.py`

| 이벤트 | 함수 | 저장 |
|--------|------|------|
| 프로젝트 완료 | `on_project_completed()` | experience_extractor → ai_observations |
| 커밋 푸시 | `on_commit_pushed()` | system_memory 업데이트 |
| 헬스 체크 | `on_health_check()` | system_memory 업데이트 |

### 주기적 태스크

| 주기 | 태스크 | 모듈 |
|------|--------|------|
| 매 턴 | _detect_and_save_learning | chat_service.py |
| 매 턴 | evaluate_response + auto_reflexion_loop | self_evaluator.py |
| 20턴마다 | session_note + auto_observe + mid_conversation_lessons | chat_service.py |
| 주기적 | auto_tune_memory | meta_evaluator.py |

---

## 9. 데이터 흐름 — 1회 턴 전체 사이클

```
CEO 메시지 입력
    │
    ▼
[1] chat_service.send_message_stream()
    │
    ├── [2] DB 저장: chat_messages (user)
    │
    ├── [3] asyncio.create_task: _detect_and_save_learning()
    │       └── _LEARNING_TRIGGERS 키워드 매칭
    │           ├── correction → ai_observations (ceo_correction, 0.7)
    │           ├── preference → ai_observations (ceo_preference, 0.8)
    │           └── positive  → ai_observations (ceo_preference, 0.6)
    │
    ├── [4] context_builder.build()
    │       ├── Layer 1: 시스템 프롬프트
    │       ├── Layer 2: 런타임 컨텍스트
    │       ├── Layer CKP: CKP 문서
    │       ├── Layer Tool: 도구 안내
    │       ├── Layer 3: 대화 히스토리
    │       └── Layer 4: 자기인식
    │
    ├── [5] memory_recall.build_memory_context()
    │       └── 10섹션 asyncio.gather (시간 감쇠 ORDER BY 적용)
    │           └── _log_memory_usage() → usage_count++
    │
    ├── [6] LLM 호출 (스트리밍)
    │       └── call_llm_with_fallback()
    │
    ├── [7] DB 저장: chat_messages (assistant)
    │
    ├── [8] 백그라운드 평가
    │       ├── evaluate_response() → quality_score 저장
    │       │   ├── score < 0.5 → memory_facts (error_pattern)
    │       │   │   └── _check_repeated_errors() → correction_directive
    │       │   └── P3 강화/감쇠
    │       │
    │       └── auto_reflexion_loop() → 키워드 점수
    │           ├── score < 0.5 → correction_directive + strategy_update
    │           └── score >= 0.5 → PASS
    │
    └── [9] 20턴 배치 (해당 시)
            ├── save_session_note()
            ├── auto_observe_from_session()
            └── extract_mid_conversation_lessons()
```

---

## 10. 토큰 예산 & 비용

### Memory Recall 토큰 예산

| 섹션 | 예산 |
|------|------|
| session_notes | ~500 토큰 |
| ceo_rules | ~300 토큰 |
| tools | ~400 토큰 |
| directives | ~400 토큰 |
| discoveries | ~400 토큰 |
| learned | ~300 토큰 |
| corrections | ~200 토큰 |
| experience_lessons | ~300 토큰 |
| visual_memories | ~300 토큰 |
| strategy_updates | ~500자 고정 |
| **합계** | **~3,500 토큰** |

전체 출력 상한: **4,000자** (초과 시 절단)

### LLM 비용

| 모듈 | 모델 | 호출 빈도 | 비용 |
|------|------|----------|------|
| evaluate_response | Haiku | 매 턴 | ~$0.001/턴 |
| _check_repeated_errors | Haiku | score < 0.5 시 | ~$0.002/회 |
| auto_reflexion_loop | **없음 (LLM-free)** | 매 턴 | $0 |
| extract_mid_conversation_lessons | **없음 (LLM-free)** | 20턴마다 | $0 |
| auto_tune_memory | **없음 (LLM-free)** | 주기적 | $0 |

---

## 11. 파일 인벤토리

### 핵심 모듈

| 파일 | 줄수 | 역할 |
|------|------|------|
| `app/core/memory_recall.py` | 911 | 10섹션 메모리 조회·주입 |
| `app/services/self_evaluator.py` | 723 | 품질 평가 + Reflexion |
| `app/services/context_builder.py` | ~600 | 레이어 조립 |
| `app/services/chat_service.py` | ~3500 | 채팅 핵심 + 학습 트리거 |
| `app/services/semantic_cache.py` | ~412 | 시맨틱 캐시 |
| `app/memory/experience_extractor.py` | 293 | LLM-free 교훈 추출 |
| `app/memory/meta_evaluator.py` | 312 | confidence 자동 튜닝 |
| `app/memory/multimodal_store.py` | ~150 | 시각 메모리 저장 |
| `app/memory/store.py` | ~400 | 5-Layer 메모리 스토어 |
| `app/memory/auto_update.py` | ~200 | 이벤트 기반 자동 업데이트 |

### app/memory/ 디렉토리

```
app/memory/
├── __init__.py
├── store.py                  # AADSMemoryStore — L2~L5 계층
├── auto_update.py            # 이벤트 기반 system_memory 업데이트
├── experience_extractor.py   # LLM-free 키워드/패턴 교훈 추출
├── multimodal_store.py       # 이미지 → SHA256 → visual_memory
└── meta_evaluator.py         # confidence 자동 튜닝 + 효과성 평가
```

### 마이그레이션 (001~040)

주요 메모리 관련:
- `024_memory_facts.sql` — memory_facts 테이블
- `025_ai_observations.sql` — ai_observations 테이블
- `026_ai_meta_memory.sql` — ai_meta_memory 테이블
- `027_session_notes.sql` — session_notes 테이블
- `037_memory_archive.sql` — memory_archive 테이블

---

## 12. 설계 원칙

1. **LLM-free 우선**: 키워드/패턴으로 처리 가능하면 LLM 호출하지 않음 (비용 효율)
2. **시간 감쇠**: 모든 메모리 조회에 `EXP(-0.1 * days)` 감쇠 적용
3. **이중 보장**: correction_directive는 2곳에 동시 주입
4. **비차단**: 학습·평가·GC 모두 `asyncio.create_task()` 비차단 실행
5. **GREATEST 보호**: confidence UPSERT 시 기존값과 MAX 비교
6. **보호 카테고리**: ceo_preference, ceo_directive, compaction_directive는 GC 감쇠 제외
7. **프로젝트 분리**: ai_observations.project 컬럼으로 AADS/KIS/GO100/SF/NTV2/NAS 분리 주입

---

## 13. 변경 이력

| 버전 | 날짜 | 변경 내용 |
|------|------|----------|
| 1.0 | 2026-03-16 | 초기 작성 |
| 2.0 | 2026-03-29 | Memory Recall 6→10섹션 반영, Layer 4 미작동 명시, memory_gc.py→meta_evaluator.py 정정, B1 Reflexion LLM-free 반영, context_builder 추가 레이어(CKP/Tool/Artifact/Semantic) 반영, migrations 032~040 추가, experience_extractor LLM-free 전환 반영, 시간 감쇠(P0-2) 공식 추가, DB 실측 수치 갱신 |
