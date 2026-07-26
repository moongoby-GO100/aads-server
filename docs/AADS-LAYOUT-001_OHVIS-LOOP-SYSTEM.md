# AADS-LAYOUT-001: OHVIS 루프 시스템 상세 구현 기획서

> **문서 유형**: FLOW Layout (설계)  
> **작성일**: 2026-07-27  
> **상태**: 설계 완료 → CEO 승인 대기  
> **우선순위**: P1  
> **관련**: AADS-190(서브에이전트), AADS 3-Tier Architecture, Claude Code `/loop` 스킬

---

## 1. 목적 및 배경

### 1.1 목적
OHVIS에 **자율 반복 실행(Loop)** 기능을 추가하여, CEO가 단일 지시로 목표 달성까지 자동 반복·감시·실행을 위임할 수 있는 시스템을 구현한다.

### 1.2 현재(AS-IS) vs 목표(TO-BE)

| 구분 | 현재 | 목표 |
|------|------|------|
| 감시 | "감시해" → 1회 확인 → 보고 → 종료 | "감시해" → N회 자동 반복 → 이상 시에만 알림 → 조건 충족까지 지속 |
| 작업 | "todo 다 처리해" → 순차 실행 → 실패 시 중단 보고 | 실패 시 자동 재시도 → 3회 실패 후에만 보고 → 나머지 계속 |
| 보고 | 매번 중간 보고 | 이상/완료 시에만 보고 (정상은 조용히 진행) |
| 비용 | 매 지시마다 CEO 개입 비용 | 초기 1회 지시 → 완료까지 자율 |

### 1.3 핵심 원칙
- **Silent Success**: 정상이면 조용히, 이상 시에만 알림
- **Bounded Autonomy**: 무한 루프 방지 (최대 반복, 비용 상한, 시간 제한)
- **CEO Override**: 언제든 "중단해" 한마디로 즉시 정지
- **Cost Efficiency**: LLM 15회/task 원칙 준수, 루프 전체 비용 상한

---

## 2. 아키텍처 설계

### 2.1 전체 구조도

```
┌─────────────────────────────────────────────────────────┐
│                    CEO 지시                               │
│         "서버 상태 1시간마다 감시해"                         │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────┐
│              Intent Classifier (확장)                     │
│  ┌─────────┐  ┌─────────┐  ┌──────────┐  ┌──────────┐  │
│  │ instant │  │ runner  │  │  loop    │  │ approval │  │
│  │ (Tier1) │  │ (Tier2) │  │ (NEW)   │  │ (Tier3)  │  │
│  └─────────┘  └─────────┘  └──────────┘  └──────────┘  │
└────────────────────────────────┬────────────────────────┘
                                 │ loop 감지
                                 ▼
┌─────────────────────────────────────────────────────────┐
│                Loop Controller                           │
│                                                         │
│  ┌───────────────┐  ┌──────────────┐  ┌─────────────┐  │
│  │ Loop Registry │  │ Loop Executor│  │ Loop Monitor│  │
│  │ (DB 관리)     │  │ (반복 실행)   │  │ (상태 감시) │  │
│  └───────────────┘  └──────────────┘  └─────────────┘  │
└────────────────────────────────┬────────────────────────┘
                                 │
                    ┌────────────┼────────────┐
                    ▼            ▼            ▼
             ┌──────────┐ ┌──────────┐ ┌──────────┐
             │ Monitor  │ │  Task    │ │Sequential│
             │  Loop    │ │  Loop    │ │  Loop    │
             │(주기감시)│ │(재시도)  │ │(순차작업)│
             └──────────┘ └──────────┘ └──────────┘
                    │            │            │
                    ▼            ▼            ▼
             ┌─────────────────────────────────────┐
             │        Notification Layer           │
             │  텔레그램 / 대시보드 / CEO Chat      │
             └─────────────────────────────────────┘
```

### 2.2 기존 3-Tier와의 통합 관계

```
CEO 지시 → Intent Classifier
    │
    ├─ instant_ack (Tier 1) → 즉시 응답 (변경 없음)
    │
    ├─ runner_task (Tier 2) → TaskCard 생성 → 단일 실행
    │
    ├─ loop_task (NEW) ─────→ Loop 생성 → 반복 실행
    │       │                      │
    │       │                      ├─ 각 iteration은 Tier 2 runner로 실행
    │       │                      └─ 완료/이상 시 Tier 3 판단 트리거
    │       │
    │       └─ loop 내 개별 iteration도 ohvis_tasks에 기록
    │
    └─ approval_needed (Tier 3) → CEO 승인 대기 (변경 없음)
```

---

## 3. 데이터베이스 스키마

### 3.1 ohvis_loops (루프 메타 정보)

```sql
CREATE TABLE ohvis_loops (
    id              SERIAL PRIMARY KEY,
    loop_type       VARCHAR(20) NOT NULL,  -- 'monitor' | 'task' | 'sequential'
    status          VARCHAR(20) NOT NULL DEFAULT 'active',
                    -- 'active' | 'paused' | 'completed' | 'failed' | 'cancelled'
    
    -- CEO 지시 원문
    original_command TEXT NOT NULL,
    parsed_intent   JSONB NOT NULL,
    
    -- 실행 설정
    interval_seconds INT,              -- monitor: 반복 주기 (초)
    max_iterations  INT DEFAULT 50,    -- 최대 반복 횟수
    max_cost_usd    DECIMAL(8,4) DEFAULT 0.50,  -- 비용 상한 (생성 시 resolve_max_cost()가 모델별 자동 산출, §6.3)
    execution_model_id VARCHAR(80),    -- 상한 산출 기준 모델 (폴백 시 갱신 + 상한 재계산)
    cost_override_by_ceo BOOLEAN DEFAULT FALSE,  -- CEO 수동 지정 여부 (TRUE면 자동 조정 skip)
    max_failures    INT DEFAULT 3,     -- 연속 실패 허용 횟수
    timeout_minutes INT DEFAULT 1440,  -- 전체 타임아웃 (기본 24시간)
    
    -- 조건
    success_condition JSONB,           -- 완료 판정 조건
    alert_condition   JSONB,           -- 알림 발송 조건
    
    -- 상태 추적
    current_iteration INT DEFAULT 0,
    consecutive_failures INT DEFAULT 0,
    total_cost_usd   DECIMAL(8,4) DEFAULT 0,
    last_result      JSONB,
    
    -- 타임스탬프
    created_at      TIMESTAMP DEFAULT NOW(),
    started_at      TIMESTAMP,
    completed_at    TIMESTAMP,
    next_run_at     TIMESTAMP,         -- 다음 실행 예정 시각
    
    -- 관계
    created_by      VARCHAR(50) DEFAULT 'ceo',
    project         VARCHAR(20) DEFAULT 'AADS'
);

CREATE INDEX idx_loops_status ON ohvis_loops(status);
CREATE INDEX idx_loops_next_run ON ohvis_loops(next_run_at) WHERE status = 'active';
```

### 3.2 ohvis_loop_iterations (반복 실행 기록)

```sql
CREATE TABLE ohvis_loop_iterations (
    id              SERIAL PRIMARY KEY,
    loop_id         INT REFERENCES ohvis_loops(id),
    iteration_num   INT NOT NULL,
    
    -- 실행 결과
    status          VARCHAR(20) NOT NULL,  -- 'success' | 'failure' | 'skipped' | 'alert'
    result_summary  TEXT,
    result_data     JSONB,
    
    -- 비용 추적
    llm_calls       INT DEFAULT 0,
    cost_usd        DECIMAL(8,4) DEFAULT 0,
    duration_ms     INT,
    
    -- 알림 여부
    alert_sent      BOOLEAN DEFAULT FALSE,
    alert_channel   VARCHAR(50),
    
    -- 관련 태스크
    ohvis_task_id   INT,               -- ohvis_tasks 연결 (있으면)
    
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_iterations_loop ON ohvis_loop_iterations(loop_id, iteration_num);
```

### 3.3 ohvis_loop_definitions (loop.md 대응 — 프리셋 정의)

```sql
CREATE TABLE ohvis_loop_definitions (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(100) UNIQUE NOT NULL,  -- 'server-health-monitor'
    description     TEXT,
    
    -- 기본 설정 (CEO가 미지정 시 적용)
    default_interval_seconds INT,
    default_max_iterations   INT,
    default_max_cost_usd     DECIMAL(8,4),
    default_alert_condition  JSONB,
    default_success_condition JSONB,
    
    -- 실행 템플릿
    task_template   JSONB NOT NULL,    -- 매 iteration에서 실행할 작업 정의
    
    -- 활성화
    is_active       BOOLEAN DEFAULT TRUE,
    project         VARCHAR(20) DEFAULT 'AADS',
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);
```

---

## 4. 루프 유형 상세

### 4.1 Monitor Loop (주기 감시형)

**용도**: 상태 변화 감지, 정기 점검, 이상 탐지

```
CEO: "서버 상태 30분마다 감시해"
CEO: "디스크 80% 넘으면 알려줘"  
CEO: "경쟁사 가격 매일 확인해"
```

**동작 흐름**:
```
[시작] → [interval 대기] → [상태 확인] → 정상? 
                                          ├─ Yes → [결과 기록] → [interval 대기] (반복)
                                          └─ No  → [알림 발송] → [interval 대기] (계속 감시)
```

**핵심 속성**:
- `interval_seconds`: 반복 주기 (최소 60초, 기본 1800초)
- `alert_condition`: 알림 트리거 조건 (JSON 표현식)
- 종료 조건: CEO 중단 / max_iterations / timeout

**비용 특성**: 매 iteration LLM 1-2회 (상태 판단만)

### 4.2 Task Loop (목표 달성형)

**용도**: 특정 목표까지 자동 재시도, 조건 충족 시 완료

```
CEO: "배포 성공할 때까지 진행해"
CEO: "테스트 전부 통과시켜"
CEO: "이 버그 고쳐질 때까지 시도해"
```

**동작 흐름**:
```
[시작] → [작업 실행] → 성공?
                        ├─ Yes → [완료 보고] → [종료]
                        └─ No  → 재시도 가능?
                                  ├─ Yes → [전략 조정] → [재실행]
                                  └─ No  → [실패 보고 + CEO 판단 요청]
```

**핵심 속성**:
- `success_condition`: 성공 판정 조건
- `max_failures`: 연속 실패 허용 (기본 3회)
- 각 재시도마다 이전 실패 원인 분석 → 전략 변경

**비용 특성**: iteration당 LLM 5-15회 (실제 작업 수행)

### 4.3 Sequential Loop (순차 작업형)

**용도**: 다중 작업을 순서대로 자동 실행

```
CEO: "todo 전부 처리하고 보고해"
CEO: "이 5개 파일 순서대로 리팩토링해"
CEO: "Phase 1~3 순차 실행해"
```

**동작 흐름**:
```
[시작] → [작업 1 실행] → 성공?
                          ├─ Yes → [작업 2 실행] → 성공? → ... → [전체 완료 보고]
                          └─ No  → [재시도 3회] → 성공?
                                                   ├─ Yes → [다음 작업으로]
                                                   └─ No  → [건너뛰기 + 기록] → [다음 작업]
```

**핵심 속성**:
- `task_list`: 실행할 작업 목록 (순서 보존)
- `skip_on_failure`: 실패 시 건너뛰기 여부 (기본 true)
- 전체 진행률 추적 (N/M 완료)

**비용 특성**: 작업 수 × iteration당 LLM 호출

---

## 5. 인텐트 분류 확장

### 5.1 루프 감지 키워드

```python
LOOP_INTENT_PATTERNS = {
    "monitor": {
        "keywords": ["감시", "모니터링", "확인해줘", "지켜봐", "알려줘"],
        "time_patterns": ["마다", "주기적", "매시간", "매일", "계속"],
        "condition_patterns": ["넘으면", "되면", "변하면", "안되면"],
    },
    "task": {
        "keywords": ["될때까지", "성공할때까지", "고쳐질때까지", "완료될때까지"],
        "retry_patterns": ["계속 시도", "반복", "끝까지"],
    },
    "sequential": {
        "keywords": ["전부", "모두", "순서대로", "하나씩", "다 처리"],
        "list_patterns": ["1~", "Phase", "단계별"],
    },
}
```

### 5.2 분류 로직

```python
async def classify_loop_intent(message: str) -> dict:
    """
    Returns:
        {
            "is_loop": bool,
            "loop_type": "monitor" | "task" | "sequential" | None,
            "interval": int | None,        # 초 단위
            "condition": str | None,       # 종료/알림 조건 원문
            "task_list": list | None,      # sequential인 경우
            "confidence": float,           # 0.0 ~ 1.0
        }
    """
```

### 5.3 분류 우선순위

```
1. 명시적 루프 키워드 ("감시해", "될때까지") → 바로 loop 분류
2. 시간 패턴 + 동작 ("30분마다 확인") → monitor loop
3. 목표 조건 + 반복 암시 ("테스트 통과시켜") → task loop  
4. 복수 작업 + 완료 지시 ("todo 다 처리해") → sequential loop
5. 애매한 경우 → 일반 runner task (기존 동작 유지)
```

---

## 6. 안전 제한 (Safety Limits)

### 6.1 계층적 제한

| 제한 항목 | Monitor | Task | Sequential | 비고 |
|-----------|---------|------|------------|------|
| 최대 반복 | 100회 | 10회 | 작업수×3 | CEO 오버라이드 가능 |
| 비용 상한 (Sonnet 기준) | $0.50 | $3.00 | $6.00 | 모델별 자동 조정 (6.3 참조) |
| 비용 상한 (Opus 5 기준) | $0.50 | **$5.00** | **$10.00** | CEO 지정 시 최대 $30 |
| 시간 제한 | 48시간 | 4시간 | 8시간 | 초과 시 자동 pause |
| 연속 실패 | 5회 | 3회 | 3회 | 초과 시 CEO 알림 |
| LLM/iteration | 3회 | 15회 | 15회 | AADS CEO 규칙 준수 |
| 최소 간격 | 60초 | 30초 | 10초 | 과부하 방지 |

### 6.2 비용 추적 공식

```python
max_cost_usd = await resolve_max_cost(loop_type, execution_model_id, ceo_override)  # §6.3
iteration_cost = sum(llm_call_costs)  # 모델별 단가 적용
loop_total_cost = sum(all_iteration_costs)

# 경고 단계
if loop_total_cost > max_cost_usd * 0.8:
    notify_ceo("비용 80% 도달, 잔여 예산: ${remaining}")
    
if loop_total_cost >= max_cost_usd:
    pause_loop("비용 상한 도달")
    notify_ceo("루프 일시정지 — 계속하려면 '계속해' / 중단하려면 '중단해'")
```

### 6.3 모델별 자동 비용 조정 (P0 — 2026-07-27 추가)

**배경**: 고정 상한($2.00)은 Haiku/Sonnet 기준으로는 충분하나, Opus 5·GPT-5.6 Sol 같은 고단가 모델에서는 3회 시도 전에 상한에 도달해 루프가 조기 일시정지된다.
**해결**: 루프 생성 시점에 실행 모델의 DB 단가를 조회해 상한을 자동 배율 적용한다.

#### 6.3.1 모델 단가·배율표

기준: Sonnet = 1.00. 배율 = `(input_cost + output_cost) / 18.0` [출처: DB `llm_models` 조회, 2026-07-27 07:30 KST]

| 모델 | input $/1M | output $/1M | 배율 |
|------|-----------|------------|------|
| claude-haiku (4.5) | 1.00 | 5.00 | 0.33 |
| gpt-5.6-luna | 1.00 | 6.00 | 0.39 |
| gpt-5.6-terra | 2.50 | 15.00 | 0.97 |
| claude-sonnet (4.6/5) | 3.00 | 15.00 | 1.00 |
| **claude-opus-5** | 5.00 | 25.00 | **1.67** |
| **gpt-5.6-sol** | 5.00 | 30.00 | **1.94** |

#### 6.3.2 자동 산출 결과 (기준 예산 × 배율, 하한 적용)

| 루프 유형 | 기준 예산 | Haiku | Luna | Terra | Sonnet | **Opus 5** | **Sol** |
|-----------|----------|-------|------|-------|--------|-----------|---------|
| Monitor | $0.50 | $0.50* | $0.50* | $0.50* | $0.50 | $0.84 | $0.97 |
| Task | $3.00 | $1.00 | $1.17 | $2.92 | $3.00 | **$5.00** | $5.83 |
| Sequential | $6.00 | $2.00 | $2.34 | $5.83 | $6.00 | **$10.00** | $11.67 |

`*` 하한 $0.50 적용 (저단가 모델도 최소 예산 보장)

#### 6.3.3 구현 로직

```python
# app/services/loop_controller.py
_BASE_BUDGET = {"monitor": 0.50, "task": 3.00, "sequential": 6.00}
_MIN_BUDGET = 0.50
_MAX_BUDGET_CEO_OVERRIDE = 30.00
_SONNET_BLENDED = 18.0  # input 3 + output 15

async def resolve_max_cost(loop_type: str, model_id: str, ceo_override: float | None) -> float:
    if ceo_override is not None:
        return min(ceo_override, _MAX_BUDGET_CEO_OVERRIDE)
    row = await db.fetchrow(
        "SELECT input_cost, output_cost FROM llm_models WHERE model_id=$1 AND is_active", model_id
    )
    multiplier = ((row["input_cost"] + row["output_cost"]) / _SONNET_BLENDED) if row else 1.0
    return max(round(_BASE_BUDGET[loop_type] * multiplier, 2), _MIN_BUDGET)
```

**모델 변경 시 재산출**: 루프 실행 중 폴백으로 모델이 바뀌면(예: Opus 5 → Sonnet) 다음 iteration 시작 시 `resolve_max_cost`를 재호출해 상한을 재계산한다. 이미 집행된 `total_cost_usd`는 유지한다.

**완료기준**: `resolve_max_cost("task", "claude-opus-5", None) == 5.00`, `resolve_max_cost("task", "claude-haiku", None) == 1.00` 단위 테스트 통과.

### 6.4 CEO 오버라이드 명령어

| 명령어 | 동작 |
|--------|------|
| "중단해" / "그만" / "stop" | 즉시 종료 (cancelled) |
| "잠깐 멈춰" / "pause" | 일시정지 (paused) → "계속해"로 재개 |
| "계속해" / "resume" | 일시정지에서 재개 |
| "간격 5분으로" | interval 변경 (실시간) |
| "10번만 더" | max_iterations 추가 |
| "예산 늘려 $5" | max_cost_usd 변경 |

---

## 7. 구현 우선순위 (Phase)

### Phase 0: 기반 준비 (3일)

| 항목 | 설명 | 파일 |
|------|------|------|
| DB 스키마 생성 | 3개 테이블 + 인덱스 | `migrations/` |
| Loop Controller 모듈 | 핵심 제어 로직 | `app/services/loop_controller.py` |
| **모델별 비용 자동 조정** | `resolve_max_cost()` 구현 + 단위 테스트 (§6.3) | `app/services/loop_controller.py` |
| 인텐트 확장 | loop 감지 추가 | `app/services/intent_classifier.py` |
| 설정 테이블 시드 | 기본 루프 정의 3종 | `scripts/init_loop_definitions.sql` |

### Phase 1: Monitor Loop 구현 (2일)

| 항목 | 설명 |
|------|------|
| Monitor Executor | 주기적 상태 확인 실행기 |
| 스케줄러 연동 | APScheduler / asyncio 기반 타이머 |
| 알림 조건 평가기 | JSON 조건식 평가 엔진 |
| 텔레그램/대시보드 알림 | 이상 감지 시 즉시 알림 |
| CEO 중단 명령 처리 | 실시간 loop 제어 |

**CEO 시나리오 검증**:
```
CEO: "서버 헬스 30분마다 확인해"
→ Monitor Loop 생성 (interval=1800s)
→ 정상: 조용히 기록
→ 이상: "⚠️ aads-server 응답 지연 (3.2초)"
→ CEO: "그만" → 즉시 종료
```

### Phase 2: Task Loop 구현 (3일)

| 항목 | 설명 |
|------|------|
| Task Executor | 목표 달성형 실행기 |
| 실패 분석기 | 이전 실패 원인 파악 → 전략 변경 |
| Success Evaluator | 성공 조건 판정 (LLM 기반) |
| 자동 재시도 로직 | backoff + 전략 변경 |
| 파이프라인 러너 연동 | 코드 실행 필요 시 runner 위임 |

**CEO 시나리오 검증**:
```
CEO: "이 테스트 통과시켜"
→ Task Loop 생성 (success_condition: "pytest 0 failures")
→ 1차: 코드 수정 → 테스트 실행 → 2/5 실패
→ 2차: 실패 분석 → 다른 접근 → 테스트 실행 → 1/5 실패  
→ 3차: 나머지 수정 → 전체 통과
→ "✅ 테스트 전체 통과 (3회 시도, $0.34 사용)"
```

### Phase 3: Sequential Loop 구현 (2일)

| 항목 | 설명 |
|------|------|
| Sequential Executor | 작업 목록 순차 실행기 |
| 진행률 추적기 | N/M 완료 상태 관리 |
| Skip/Retry 로직 | 실패 시 건너뛰기 또는 재시도 |
| 결과 집계기 | 전체 결과 종합 보고서 |

**CEO 시나리오 검증**:
```
CEO: "todo 5개 전부 처리해"
→ Sequential Loop 생성 (5 tasks)
→ Task 1 ✅ → Task 2 ✅ → Task 3 ❌(재시도) → Task 3 ✅ → Task 4 ✅ → Task 5 ✅
→ "✅ 5/5 완료 (1건 재시도, $1.23 사용)"
```

### Phase 4: 대시보드 UI + 고급 기능 (3일)

| 항목 | 설명 |
|------|------|
| Loop Status Panel | 활성 루프 목록, 상태, 진행률 |
| Loop History | 과거 루프 실행 기록 |
| Loop Control UI | 정지/재개/설정 변경 버튼 |
| 복합 루프 | Monitor + Task 조합 |
| 스마트 간격 조정 | 이상 빈도에 따라 간격 자동 조절 |

---

## 8. Loop Controller 핵심 로직

### 8.1 모듈 구조

```
app/services/
├── loop_controller.py       # 루프 생명주기 관리 (생성/시작/정지/삭제)
├── loop_executor.py         # 실제 반복 실행 엔진
├── loop_scheduler.py        # 타이밍 스케줄러 (asyncio 기반)
├── loop_evaluator.py        # 조건 평가기 (알림/성공/실패)
└── loop_intent_parser.py    # CEO 지시 → Loop 설정 파싱
```

### 8.2 생명주기

```
created → active → [iterating...] → completed
                 ↕                  → failed
              paused                → cancelled
```

### 8.3 실행 사이클 (의사코드)

```python
async def run_loop(loop_id: int):
    loop = await get_loop(loop_id)
    
    while loop.status == 'active':
        # 안전 체크
        if loop.current_iteration >= loop.max_iterations:
            await complete_loop(loop, "최대 반복 도달")
            break
        if loop.total_cost_usd >= loop.max_cost_usd:
            await pause_loop(loop, "비용 상한 도달")
            break
        if is_timed_out(loop):
            await pause_loop(loop, "시간 초과")
            break
            
        # CEO 중단 명령 체크
        if await check_cancel_signal(loop_id):
            await cancel_loop(loop, "CEO 중단 명령")
            break
        
        # iteration 실행
        result = await execute_iteration(loop)
        await record_iteration(loop, result)
        
        # 조건 평가
        if loop.loop_type == 'monitor':
            if evaluate_alert_condition(loop, result):
                await send_alert(loop, result)
            await sleep(loop.interval_seconds)
            
        elif loop.loop_type == 'task':
            if evaluate_success_condition(loop, result):
                await complete_loop(loop, "목표 달성")
                break
            if result.status == 'failure':
                loop.consecutive_failures += 1
                if loop.consecutive_failures >= loop.max_failures:
                    await fail_loop(loop, "연속 실패 한도 초과")
                    break
            else:
                loop.consecutive_failures = 0
                
        elif loop.loop_type == 'sequential':
            if all_tasks_done(loop):
                await complete_loop(loop, "전체 작업 완료")
                break
            advance_to_next_task(loop)
    
    # 최종 보고
    await send_completion_report(loop)
```

---

## 9. 3-Tier 통합 상세

### 9.1 Tier 1 (Instant ACK) 연동

```python
# chat_service.py에서 loop 관련 즉시 응답
LOOP_ACK_PATTERNS = {
    "감시 시작": "🔄 {target} 감시를 시작합니다. (간격: {interval}, 이상 시 알림)",
    "루프 시작": "🔄 {task} 루프를 시작합니다. (최대 {max}회, 예산: ${budget})",
    "루프 중단": "⏹️ 루프 #{loop_id}를 중단했습니다.",
    "루프 상태": "📊 활성 루프 {count}개 실행 중",
}
```

### 9.2 Tier 2 (Runner) 연동

```python
# 각 loop iteration은 내부적으로 runner와 동일한 실행 경로 사용
async def execute_iteration(loop):
    if loop.requires_code_execution:
        # Pipeline Runner 위임
        task_id = await pipeline_runner.submit(
            task=loop.current_task,
            parent_loop_id=loop.id,
            budget_remaining=loop.max_cost_usd - loop.total_cost_usd
        )
        return await wait_for_task(task_id)
    else:
        # 단순 조회/판단 (LLM 직접 호출)
        return await call_llm_with_fallback(
            prompt=build_iteration_prompt(loop),
            max_tokens=500
        )
```

### 9.3 Tier 3 (Auto-Judgment) 연동

```python
# 루프 완료/실패 시 자동 판단 트리거
async def send_completion_report(loop):
    report = build_loop_report(loop)
    
    if loop.status == 'failed' and loop.consecutive_failures >= loop.max_failures:
        # CEO 판단 필요 — Tier 3 approval 요청
        await create_approval_request(
            title=f"루프 #{loop.id} 실패 — 조치 필요",
            context=report,
            options=["재시도 (예산 추가)", "다른 접근으로 재시도", "포기"]
        )
    else:
        # 성공 완료 — 결과만 보고
        await notify_ceo(report)
```

---

## 10. Loop 프리셋 정의 (loop.md 대응)

### 10.1 기본 제공 프리셋

```json
[
  {
    "name": "server-health-monitor",
    "description": "AADS 서버 헬스 주기적 확인",
    "default_interval_seconds": 1800,
    "default_max_iterations": 48,
    "default_max_cost_usd": 0.30,
    "task_template": {
      "action": "health_check",
      "targets": ["aads-server", "postgres", "litellm"],
      "alert_on": ["response_time > 3000ms", "status != healthy"]
    }
  },
  {
    "name": "disk-usage-monitor",
    "description": "디스크 사용량 감시 (80% 경고)",
    "default_interval_seconds": 3600,
    "default_max_iterations": 24,
    "default_max_cost_usd": 0.15,
    "task_template": {
      "action": "run_command",
      "command": "df -h / | awk 'NR==2{print $5}'",
      "alert_on": ["usage_percent > 80"]
    }
  },
  {
    "name": "deploy-until-success",
    "description": "배포 성공할 때까지 재시도",
    "default_max_iterations": 5,
    "default_max_cost_usd": null,
    "_comment": "null = resolve_max_cost()가 실행 모델 단가로 자동 산출 (§6.3). Opus 5 기준 $5.00",
    "task_template": {
      "action": "pipeline_runner",
      "retry_strategy": "analyze_and_fix",
      "success_condition": "health_check == 200"
    }
  }
]
```

### 10.2 CEO 커스텀 정의 (자연어 → 프리셋 자동 생성)

```
CEO: "앞으로 '서버 감시해'라고 하면 5분마다 확인하고 장애 시 텔레그램 알림줘"
→ 자동으로 프리셋 생성:
  name: "ceo-server-watch"
  interval: 300s
  alert_channel: "telegram"
  저장 → 이후 "서버 감시해" 시 자동 적용
```

---

## 11. API 엔드포인트

### 11.1 Loop 관리 API

```
POST   /api/v1/loops              # 루프 생성 (내부용 — 인텐트 분류기가 호출)
GET    /api/v1/loops              # 활성 루프 목록
GET    /api/v1/loops/{id}         # 루프 상세 정보
PATCH  /api/v1/loops/{id}         # 설정 변경 (interval, max_iterations 등)
POST   /api/v1/loops/{id}/pause   # 일시정지
POST   /api/v1/loops/{id}/resume  # 재개
POST   /api/v1/loops/{id}/cancel  # 취소
GET    /api/v1/loops/{id}/iterations  # 반복 기록 조회
```

### 11.2 대시보드 WebSocket

```
WS /ws/loops/status   # 실시간 루프 상태 업데이트 스트림
```

---

## 12. 비용 분석

### 12.1 루프 유형별 예상 비용

| 루프 유형 | 일반 시나리오 | LLM 호출 | Sonnet 예상 | Opus 5 예상 | 자동 상한(Opus 5) |
|-----------|--------------|----------|------------|------------|-----------------|
| Monitor (24시간, 30분 간격) | 48회 × 2 calls | 96회 | $0.15~0.25 | $0.25~0.42 | $0.84 |
| Task (버그 수정, 3회 시도) | 3회 × 10 calls | 30회 | $0.50~1.50 | $0.84~2.51 | $5.00 |
| Sequential (5개 todo) | 5회 × 8 calls | 40회 | $0.60~2.00 | $1.00~3.34 | $10.00 |

**여유율**: Opus 5 최악 시나리오 대비 Task 1.99배, Sequential 3.0배 여유 → 조기 일시정지 리스크 해소. [산출: §6.3.2 배율표 × 시나리오 상단값]

### 12.2 월간 비용 예측 (일반 사용 패턴)

```
- Monitor 루프: 2~3개 상시 가동 → $5~15/월
- Task 루프: 주 5~10회 → $10~30/월
- Sequential 루프: 주 3~5회 → $10~25/월
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
총 예상: $25~70/월 (현재 AADS 월 운영비의 10~20%)
```

### 12.3 비용 절감 전략

1. **모델 계층화**: Monitor는 Haiku(저비용), Task는 Sonnet, 복잡한 판단만 Opus
2. **캐싱**: 동일 상태 반복 시 LLM 호출 스킵 (변화 없으면 이전 결과 재사용)
3. **적응형 간격**: 정상 지속 시 간격 증가, 이상 감지 시 간격 축소
4. **배치 처리**: Sequential에서 유사 작업을 묶어 1회 LLM으로 처리

---

## 13. 구현 일정 (총 13일)

```
Week 1: Phase 0 (기반) + Phase 1 (Monitor)
  Day 1-2: DB 스키마, Loop Controller 모듈 골격
  Day 3:   인텐트 분류기 확장
  Day 4-5: Monitor Loop 구현 + 테스트

Week 2: Phase 2 (Task) + Phase 3 (Sequential)  
  Day 6-7: Task Loop + 실패 분석기
  Day 8:   Task Loop 파이프라인 연동
  Day 9-10: Sequential Loop + 진행률 추적

Week 3: Phase 4 (UI + 고급)
  Day 11: 대시보드 Loop Panel
  Day 12: CEO 실시간 제어 (중단/재개/설정 변경)
  Day 13: 통합 테스트 + 문서화
```

---

## 14. 위험 요소 및 대응

| 위험 | 영향 | 대응 |
|------|------|------|
| 무한 루프 (비용 폭주) | 높음 | 3중 안전장치: 반복수/비용/시간 제한 |
| 서버 과부하 (다수 루프 동시) | 중간 | 동시 활성 루프 상한 5개, 큐 관리 |
| LLM 장애 시 루프 중단 | 중간 | fallback 모델 + 재시도 + graceful pause |
| 잘못된 인텐트 분류 | 낮음 | confidence < 0.7이면 CEO에게 확인 |
| 스케줄러 크래시 | 높음 | DB에 next_run_at 기록 → 재시작 시 복구 |

---

## 15. 성공 기준

- [ ] CEO "서버 감시해" → 30분 간격 자동 감시 시작, 이상 시 알림
- [ ] CEO "중단해" → 1초 내 루프 종료
- [ ] CEO "테스트 통과시켜" → 최대 3회 자동 재시도 후 보고
- [ ] CEO "todo 다 처리해" → 자동 순차 실행 + 완료 보고
- [ ] 비용 상한 도달 시 자동 일시정지 + CEO 알림
- [ ] 대시보드에서 활성 루프 상태 실시간 확인 가능
- [ ] 월 비용 $70 이내 유지

---

## 교훈

- Loop은 "반복"이 아니라 "자율 위임"의 핵심 인프라
- 안전 제한 없는 자율 실행은 비용 폭주의 직접 원인
- Silent Success 패턴이 CEO 인지 부하를 최소화하는 핵심
- 기존 3-Tier와의 통합이 신규 시스템보다 중요 (중복 방지)

---

*문서 끝 — CEO 승인 후 Phase 0 착수*
