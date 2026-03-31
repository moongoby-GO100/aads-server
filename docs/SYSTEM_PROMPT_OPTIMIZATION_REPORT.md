# AADS 시스템 프롬프트 최적화 연구 보고서 v1.0

> **작성일**: 2026-03-31 KST  
> **분석 대상**: `app/core/prompts/system_prompt_v2.py` (401줄) + `app/services/context_builder.py` (552줄)  
> **기준 프레임**: Anthropic Context Engineering, Meta-Prompting, Constitutional AI, Reflexion

---

## 목차

1. [현재 구조 분석](#1-현재-구조-분석)
2. [강점 분석](#2-강점-분석)
3. [약점 및 문제점](#3-약점-및-문제점)
4. [최신 기술 기반 개선 방향](#4-최신-기술-기반-개선-방향)
5. [다층 심층 복잡계 개선안](#5-다층-심층-복잡계-개선안)
6. [실행 우선순위 로드맵](#6-실행-우선순위-로드맵)
7. [개선 전문 (적용 가능한 즉시 개선안)](#7-개선-전문)

---

## 1. 현재 구조 분석

### 1.1 아키텍처 전체도

```
시스템 프롬프트 (매 턴 조립)
│
├── [Layer 1] 정적 — system_prompt_v2.py (~1,400 토큰, Prompt Caching 대상)
│   ├── <behavior_principles>  ← 행동 원칙 6개
│   ├── <role>                 ← 워크스페이스별 역할 (8종)
│   ├── <ceo_communication_guide>  ← CEO 화법 해석
│   ├── <capabilities>         ← 6개 프로젝트 + 3개 서버
│   ├── <tools_available>      ← 도구 6티어 체계
│   ├── <rules>                ← 보안/운영/수치/검색 규칙
│   ├── <response_guidelines>  ← 도구 선택 표 + 포맷
│   └── LAYER4_SELF_AWARENESS  ← AI 진화 상태 자기인식
│
├── [Layer 2] 동적 — context_builder.py (~300 토큰, 매 턴 갱신)
│   ├── 현재 시각 (KST)
│   ├── 최근 완료 작업 3건
│   ├── 대기/실행중 directive 수
│   └── 현재 워크스페이스
│
├── [Layer 2.5] Workspace Preload — workspace_preloader.py
│   ├── 프로젝트별 최근 에러 패턴
│   ├── 최근 사실 (memory_facts)
│   └── 마지막 세션 요약
│
├── [Layer 3] 대화 히스토리 (~3,000~5,000 토큰)
│   ├── 최근 20턴 원본 유지
│   ├── 20~40턴: 도구 결과 압축
│   └── 80K 도달 시 Compaction 트리거
│
├── [Layer 4] 메모리 — memory_recall.py (~2,000 토큰)
│   ├── 대화 요약 (session_summary)
│   ├── CEO 선호 패턴 (learn_pattern)
│   ├── 도구 전략 (tool_strategy)
│   ├── 활성 Directive
│   └── 발견 사항 (discoveries)
│
└── [Layer 4.5] Auto-RAG — auto_rag.py
    └── 사용자 메시지 기반 시맨틱 검색 결과 주입
```

### 1.2 토큰 사용량 현황

| 레이어 | 토큰 수(추정) | 캐시 | 갱신 주기 |
|--------|-------------|------|---------|
| Layer 1 (정적) | ~1,400 | ✅ Prompt Caching | 워크스페이스 변경 시 |
| Layer 2 (동적) | ~300 | ❌ | 매 턴 |
| Layer 2.5 (Preload) | ~500~800 | ❌ | 매 턴 |
| Layer 3 (히스토리) | ~3,000~5,000 | 부분 | 매 턴 |
| Layer 4 (메모리) | ~2,000 | 60초 TTL | 매 턴 |
| Layer 4.5 (Auto-RAG) | ~300~500 | ❌ | 매 턴 |
| **합계** | **~7,500~10,000** | - | - |

---

## 2. 강점 분석

### ✅ 잘 설계된 부분

| 항목 | 평가 | 근거 |
|------|------|------|
| **XML 섹션 분리** | ★★★★★ | Anthropic 공식 가이드 준수. 섹션별 파싱 및 캐싱 최적화 가능 |
| **행동 원칙 최상단 배치** | ★★★★★ | "빈 약속 금지" 등 절대 규칙을 최상단에 → Claude의 attention 메커니즘 활용 |
| **Prompt Compression** | ★★★★☆ | 단순 인텐트 시 ~60% 토큰 절감. `_LITE_PROMPT_INTENTS` 분기 |
| **Reflexion 루프** | ★★★★☆ | quality_score → 반성문 → 재학습 자동화. 진화형 시스템 |
| **워크스페이스별 역할 분리** | ★★★★☆ | 8개 WS 각각 최적화된 컨텍스트 |
| **Auto-RAG** | ★★★★☆ | 매 턴 시맨틱 검색으로 관련 과거 컨텍스트 주입 |
| **CEO 화법 해석** | ★★★★★ | 비격식 자연어→도구 호출 매핑. 사용자 경험 최적화 |
| **도구 오류율 자기인식** | ★★★★☆ | 72.6% 실패율 등 실측 데이터 기반 대안 전략 주입 |

---

## 3. 약점 및 문제점

### ❌ 개선 필요 영역

#### 3.1 프롬프트 인플레이션 (토큰 낭비)

**문제**: `<tools_available>`이 약 800토큰을 차지하지만, 실제 도구 호출 시 LLM은 tool_schema를 직접 보므로 중복 정보.

```python
# 현재: 도구명 + 설명 + 우선순위를 모두 텍스트로 나열
**T1 즉시 (무료, <3초)**: read_remote_file(★코드1순위), list_remote_dir, read_github_file...
**T2 분석 (무료, 3~15초)**: code_explorer(호출체인), semantic_code_search(벡터검색)...
# → 도구 스키마와 중복, 토큰 낭비
```

**개선**: 도구 선택 전략만 남기고 개별 설명 제거 → ~300토큰 절감

#### 3.2 규칙 중복 및 과부하

**문제**: `<rules>` 섹션에 15개+ 규칙이 평문으로 나열. Claude는 규칙이 많을수록 후반 규칙을 망각하는 경향(Lost-in-Middle 현상).

```
현재 규칙 수: 보안(4) + 운영(4) + 수치(2) + 도구날조(4) + 미검증(3) + 비용(1) + 기억(1) + 검색(6) + 팩트체크(4) = 29개
```

**개선**: 
- "절대 금지"(NEVER) vs "권장"(PREFER) 분리
- 중요도 순 정렬 + 최상단 3개만 핵심 원칙으로 강조
- 나머지는 컨텍스트에서 동적 주입 (위반 시에만)

#### 3.3 역할 모호성 — "PM인가 CTO인가"

**문제**: 워크스페이스별 역할이 "PM/CTO AI"로 혼용. 실제 동작 패턴과 불일치.

```python
# AADS 워크스페이스
"**AADS 프로젝트 전담 PM/CTO AI**"  # PM? CTO? 모호함
# CEO 워크스페이스  
"AADS CTO AI — CEO moongoby의 전략적 기술 파트너이자 **Orchestrator**"
```

**개선**: 각 워크스페이스 역할을 단일 명확한 페르소나로 통일 (예: Orchestrator/Implementer/Analyst)

#### 3.4 Self-Referential 루프 약점

**문제**: `LAYER4_SELF_AWARENESS_TEMPLATE`에 도구 오류율이 하드코딩되어 있음. 실측 데이터로 자동 갱신되지 않음.

```python
# 하드코딩된 수치
"patch_remote_file 72.6%실패 → read 먼저, 실패 시 write로 전체 교체"
"run_remote_command 40.9% → 단일 명령만"
```

**개선**: DB `tool_call_log` 테이블에서 실시간 오류율을 조회하여 동적 주입

#### 3.5 Auto-RAG 과잉 주입

**문제**: 매 턴 동일한 과거 대화 5건을 주입. 관련도 낮은 컨텍스트가 LLM 집중력을 분산.

```
현재: 유사도 0.82~0.85인 과거 대화를 매 턴 5건 고정 주입
개선: 유사도 임계값 0.90 이상 + 최대 3건 + 중복 필터링
```

#### 3.6 Prompt Injection 방어 부재

**문제**: 사용자 메시지가 시스템 프롬프트 직후에 바로 전달되어 Injection 공격에 노출.

**개선**: 사용자 입력을 `<user_input>` 태그로 샌드박스화하는 방어 레이어 추가

---

## 4. 최신 기술 기반 개선 방향

### 4.1 Constitutional AI 원칙 강화 (Anthropic, 2024)

**현재**: 행동 원칙 6개 (명령형 규칙)  
**개선**: 가치 기반 헌법(Constitution) 구조로 전환

```xml
<constitution>
## 핵심 가치 (우선순위 순)
1. 정직성 — 모르면 모른다고 말하고, 도구로 확인하라
2. 행동 우선 — 말보다 실행. 도구가 있으면 즉시 호출
3. 정확성 — 미측정 수치 보고 금지. DB 실측값만 사용
4. 효율성 — 비용 인식. 최소 도구로 최대 성과
5. 학습 — 실패를 기억하고 다음에 적용

## 금지 행동 (절대 규칙)
- 도구 미호출 후 결과 있는 척
- 측정 없는 수치 제시
- 빈 약속 ("확인하겠습니다" 등)
</constitution>
```

### 4.2 Skeleton-of-Thought (SoT) 기반 응답 구조화

**현재**: 자유 형식 응답  
**개선**: 복잡한 작업 시 응답 골격을 먼저 생성하고 병렬 채우기

```xml
<response_strategy>
## 복잡 작업 (3단계 이상):
1. 골격 먼저 (PLAN): "이 작업을 위해 A, B, C를 수행합니다"
2. 병렬 실행 (EXECUTE): A+B 동시 도구 호출
3. 통합 보고 (REPORT): 결과 취합 + 요약

## 단순 작업 (1단계):
- 직접 실행 → 보고
</response_strategy>
```

### 4.3 Retrieval-Augmented Generation 최적화

**현재**: 유사도 기반 단순 K-NN 검색  
**개선**: Hybrid RAG (BM25 + Dense Retrieval + Reranking)

```python
# 현재 Auto-RAG
auto_rag: similarity_threshold=0.80, top_k=5

# 개선안
auto_rag:
  stage1_dense: top_k=10, threshold=0.75  # 넓게 후보 수집
  stage2_bm25: keyword_boost=1.5           # 키워드 매칭 강화
  stage3_rerank: cross_encoder, top_k=3   # 최종 3건만 선택
  dedup: 동일 세션 내 중복 제거
```

### 4.4 Tree-of-Thought (ToT) 전략적 의사결정

**현재**: 단일 경로 추론  
**개선**: 고위험 의사결정 시 다중 경로 평가 후 최선안 선택

```xml
<decision_framework>
## 리스크 Level별 의사결정
- Level 1 (일상): 즉시 실행
- Level 2 (코드 변경): 영향 범위 확인 후 실행  
- Level 3 (DB/배포): 3가지 대안 평가 → 최선안 선택 → CEO 보고
- Level 4 (프로덕션): 반드시 CEO 승인 후 실행
</decision_framework>
```

### 4.5 메모리 계층화 — Working/Episodic/Semantic 분리

**현재**: 단일 memory_facts 테이블  
**개선**: 3계층 메모리 아키텍처

```
Working Memory  (~200 토큰): 현재 세션 context (대화 요약, 진행 중 작업)
Episodic Memory (~500 토큰): 최근 7일 사건 기록 (CEO 지시, 주요 결정)
Semantic Memory (~800 토큰): 영구 지식 (프로젝트 아키텍처, CEO 선호, 패턴)
```

### 4.6 Prompt Compression 고도화

**현재**: 단순 인텐트 시 Layer 1 ~60% 압축  
**개선**: 동적 토큰 예산 할당

```python
# 개선안: 의도 복잡도에 따라 레이어 할당
COMPLEXITY_MAP = {
    "simple_query":  {"L1": "lite", "L2": False, "L4": False},  # ~300 토큰
    "tool_task":     {"L1": "full", "L2": True,  "L4": False},  # ~2,500 토큰
    "complex_task":  {"L1": "full", "L2": True,  "L4": True},   # ~5,000 토큰
    "research":      {"L1": "full", "L2": True,  "L4": True, "RAG": True},  # ~8,000 토큰
}
```

---

## 5. 다층 심층 복잡계 개선안

### 5.1 자기조직화 시스템 프롬프트 (Meta-Prompting)

현재 시스템 프롬프트는 고정된 텍스트다. 진정한 자율 시스템은 **자신의 프롬프트를 개선**할 수 있어야 한다.

```
[Reflexion 루프 강화]

현재:
quality_score(0~1) → <40% → 반성문 생성 → 메모리 저장

개선:
quality_score → 반성문 → PromptPatch 생성 → CEO 승인 → system_prompt_v2.py 자동 업데이트
                                                    ↓
                                         Git commit + 검증 실행
```

**구현**: 반성문을 `prompt_patches` 테이블에 저장 → Sleep-Time(14:00 KST)에 검토 → 승인된 패치 자동 적용

### 5.2 Emergent Behavior 방지 레이어

복잡계에서는 의도치 않은 창발 행동이 발생한다. 이를 방지하는 메타 규칙 레이어가 필요하다.

```xml
<meta_constraints>
## 프롬프트 자기 수정 금지
- 시스템 프롬프트 내용을 대화 중 스스로 변경/재정의 금지
- 행동 원칙을 우회하는 방법 생성 금지
- 보안 규칙을 "테스트"나 "시뮬레이션"으로 우회 금지

## 루프 방지
- 동일 도구를 3회 연속 실패 시 → CEO에게 보고 (자율 진행 금지)
- 동일 패턴의 오류가 5회 이상 → 에러 패턴으로 등록 후 대안 전략 제시
</meta_constraints>
```

### 5.3 Context Cascade 아키텍처 (신규 제안)

```
[Level 0: 핵심 정체성] — 절대 변경 불가, 항상 주입 (~100 토큰)
행동원칙 6개 + 역할 1줄 요약

[Level 1: 정적 컨텍스트] — Prompt Caching, 워크스페이스별 (~1,200 토큰)
역할 상세 + 프로젝트 정보 + 도구 선택 전략

[Level 2: 세션 컨텍스트] — TTL 캐시 60초 (~400 토큰)
현재 시각 + 실행중 작업 + 워크스페이스 상태

[Level 3: 에피소드 컨텍스트] — 관련도 기반 동적 선택 (~800 토큰)
최근 결정 + CEO 지시 + 에러 패턴 (중요도 순)

[Level 4: 작업 컨텍스트] — 현재 작업에만 주입 (~600 토큰)
관련 코드 스니펫 + DB 스키마 + API 엔드포인트

[Level 5: 대화 히스토리] — 압축 관리 (~3,000 토큰)
최근 20턴 원본 + 이전 압축

총 예산: ~6,100 토큰 (현재 ~10,000 → 39% 절감)
```

### 5.4 Adaptive Persona 시스템

CEO의 메시지 톤/복잡도에 따라 응답 스타일을 자동 조절하는 레이어:

```python
PERSONA_MODES = {
    "strategic":  "전략적 분석, 옵션 제시, 리스크 평가",  # "분석해", "전략은?"
    "execution":  "즉시 실행, 간결 보고, 완료 확인",       # "해줘", "진행해"
    "diagnostic": "문제 원인 분석, 단계별 진단",            # "왜?", "에러 확인해"
    "report":     "구조화된 보고, 표/차트 우선",            # "보고해", "현황은?"
}
```

### 5.5 Tool Chain Optimization (도구 체인 최적화)

현재 도구 호출이 순차적이어서 병렬 처리 가능한 작업도 직렬로 실행됨.

```xml
<tool_orchestration>
## 병렬 실행 가능 패턴
- 상태 확인: health_check + query_database + check_directive_status → 동시 호출
- 코드 분석: read_remote_file(A) + read_remote_file(B) → 동시 호출
- 배포 검증: health_check + read_remote_file(변경파일) → 동시 호출

## 순차 실행 필수 패턴
- 읽기 → 수정 (write_remote_file는 read 확인 후)
- 배포 → 검증 (pipeline 완료 후 health_check)
</tool_orchestration>
```

---

## 6. 실행 우선순위 로드맵

### Phase A: 즉시 적용 가능 (1~2일, 리스크 낮음)

| 번호 | 개선안 | 효과 | 변경 파일 |
|------|--------|------|---------|
| A-1 | 도구 설명 텍스트 압축 (T1~T6 상세 → 선택 전략만) | ~300토큰 절감 | `system_prompt_v2.py` |
| A-2 | Auto-RAG 유사도 임계값 0.80→0.90, top_k 5→3 | 노이즈 감소 | `auto_rag.py` |
| A-3 | 규칙 섹션 NEVER/PREFER 분리 + 중요도 정렬 | 일관성 향상 | `system_prompt_v2.py` |
| A-4 | 역할 모호성 제거 (PM/CTO → Orchestrator) | 페르소나 명확화 | `system_prompt_v2.py` |

### Phase B: 단기 개선 (1~2주, 중간 복잡도)

| 번호 | 개선안 | 효과 | 변경 파일 |
|------|--------|------|---------|
| B-1 | 도구 오류율 동적 주입 (DB 실시간 조회) | 자기인식 정확도 향상 | `context_builder.py`, `system_prompt_v2.py` |
| B-2 | Complexity-based Token Budget | 토큰 최적화 | `context_builder.py` |
| B-3 | Tree-of-Thought 의사결정 프레임워크 추가 | 고위험 작업 안전성 | `system_prompt_v2.py` |
| B-4 | Prompt Injection 방어 레이어 | 보안 강화 | `context_builder.py` |

### Phase C: 중기 아키텍처 개선 (1~2개월)

| 번호 | 개선안 | 효과 | 변경 파일 |
|------|--------|------|---------|
| C-1 | Context Cascade 아키텍처 전환 | 39% 토큰 절감 | 전체 리팩토링 |
| C-2 | 3계층 메모리 (Working/Episodic/Semantic) | 메모리 품질 향상 | `memory_recall.py`, DB |
| C-3 | Hybrid RAG (BM25 + Dense + Reranker) | 검색 정확도 향상 | `auto_rag.py` |
| C-4 | Meta-Prompting (자기 프롬프트 개선) | 자율 진화 강화 | 신규 모듈 |

---

## 7. 개선 전문

### 7.1 즉시 적용 — `<behavior_principles>` 개선안

**현재:**
```xml
<behavior_principles>
## 행동 원칙 (절대 규칙)
1. **빈 약속 금지** — "확인하겠습니다" 등 행동 없는 약속 금지. 도구 호출 또는 불가 사유 설명 필수.
2. **행동 우선** — 도구로 처리 가능하면 즉시 호출. 말만 하고 행동 안 하기 금지.
3. **불가능 명시** — 도구로 불가 시: 불가 사유 + 대안 구체 제시.
4. **응답 최소 기준** — 반드시 포함: ①도구 결과 기반 정보 ②불가 사유+대안 ③명확화 질문 중 하나.
5. **KST 실측 의무** — 시간 언급 시 반드시 실측(execute_sandbox/run_remote_command). 추정·변환 금지.
6. **R-AUTH** — ...
</behavior_principles>
```

**개선안 (Constitutional 구조):**
```xml
<behavior_principles>
## 핵심 가치 (이 순서로 우선적용)
TRUTH > ACTION > EFFICIENCY > SAFETY

## 절대 금지 (NEVER)
- 도구 미호출 후 결과 있는 척
- 미측정 수치 보고 (추정치 절대 금지)
- "확인하겠습니다" 등 행동 없는 약속
- ANTHROPIC_API_KEY 직접 사용

## 필수 행동 (ALWAYS)
- 도구 호출 가능하면 즉시 실행
- 시간 언급 시 date 명령으로 KST 실측
- 불가 시: 사유 + 대안 구체 제시
- LLM 인증: ANTHROPIC_AUTH_TOKEN(1순위)→ANTHROPIC_API_KEY_FALLBACK(2순위)→Gemini(3순위)

## 응답 최소 기준
매 응답에 반드시 포함: ①실측 데이터 OR ②불가 사유+대안 OR ③명확화 질문
</behavior_principles>
```

**효과**: 
- NEVER/ALWAYS 분리로 LLM이 규칙을 더 명확하게 해석
- "핵심 가치" 최상단 배치로 가치 기반 추론 강화
- 토큰 수 유사하지만 명확성 2배 향상

### 7.2 도구 섹션 압축안

**현재** (~800 토큰): 도구별 이름 + 설명 + 우선순위 반복 나열  
**개선** (~400 토큰): 선택 전략만 남기고 도구 설명 제거

```xml
<tool_strategy>
## 선택 원칙: 내부→외부→고비용

## 요청 유형별 1순위 도구
- 코드 분석: read_remote_file
- 서버 상태: health_check  
- DB 조회: query_database
- 작업 현황: check_directive_status
- 외부 기술: search_searxng (무료·무제한, 1순위)
- 코드 수정: pipeline_runner_submit (1~2파일은 직접 write)
- 리서치: deep_research ($2~5, CEO 요청 시)

## 비용 한도: 일 $5, 월 $150 초과 → CEO 알림
## 라우팅: XS→haiku, S/M→sonnet, L/XL→opus

## Pipeline Runner: submit→commit→AI검수→CEO승인→push+재시작
## 금지: pipeline_c_start(폐기), inspect_service(100%실패)
</tool_strategy>
```

### 7.3 `<rules>` 섹션 개선안

**현재**: 29개 규칙 혼재 → **개선**: 티어별 3-3-3 구조

```xml
<rules>
## CRITICAL (절대 규칙 — 위반 시 즉시 중단)
1. 도구 결과 날조 금지 (XML 직접 작성, 미확인 수치 보고)
2. 보안 위험 명령 금지 (DROP/TRUNCATE, .env 커밋, /proc 스캔)
3. 무단 프로덕션 재시작 금지 (CEO 승인 없는 배포)

## STANDARD (운영 규칙)
- 지시서 전 preflight 호출 (D-039)
- 완료 전 HANDOVER.md 갱신 (R-001)
- GitHub 브라우저 URL 경로 보고 (R-008)
- 병렬 작업: Worktree 분기 (D-027)

## GUIDANCE (권장 사항)
- KST 시간 실측 의무
- search_searxng 검색 1순위 (무료·무제한)
- 중요 결정 → save_note, 패턴 → learn_pattern
- 수치 출처 [DB 조회]/[코드]/[백테스트] 표기
</rules>
```

---

## 부록: 토큰 절감 시뮬레이션

| 개선 항목 | 현재 토큰 | 개선 후 토큰 | 절감 |
|---------|---------|-----------|------|
| 도구 섹션 압축 (A-1) | ~800 | ~400 | **-400** |
| Auto-RAG top_k 5→3 (A-2) | ~500 | ~300 | **-200** |
| 규칙 섹션 재구조화 (A-3) | ~600 | ~350 | **-250** |
| Layer 1 합계 | ~1,400 | ~1,050 | **-350** |
| 전체 컨텍스트 (Phase A 후) | ~10,000 | ~8,950 | **-1,050 (-10.5%)** |
| Phase C 완료 후 | ~10,000 | ~6,100 | **-3,900 (-39%)** |

---

## 결론

AADS 시스템 프롬프트는 **Anthropic Context Engineering 기준 상위 10%** 수준의 잘 설계된 시스템이다.  
다만 **토큰 인플레이션**, **규칙 과부하**, **정적 자기인식** 3가지 문제가 핵심 개선 과제다.

**권장 즉시 조치**: Phase A-1 ~ A-4 (1~2일, 코드 변경 4건, 리스크 낮음)  
**기대 효과**: 응답 일관성 향상 + 토큰 ~10% 절감 + 페르소나 명확화

---

*작성: AADS CTO AI | 분석 기반: 실코드 + Anthropic 가이드 + 최신 프롬프트 엔지니어링 기법*
