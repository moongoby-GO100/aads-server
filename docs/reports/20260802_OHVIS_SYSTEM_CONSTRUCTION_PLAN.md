# OHVIS 즉각 응답 시스템 — 구축 계획서

> **문서 ID**: OHVIS-BUILD-PLAN-20260802
> **작성 기준**: 기존 4개 문서 종합 + 코드 베이스라인 실측
> **작성 모델**: Claude Opus 4.6
> **작성일**: 2026-08-02 KST

---

## 1. 요약

오비스(OHVIS)가 **3초 이내 즉답 + 심층 병렬 처리 + 다중 질문 취합 보고**를 달성하기 위한 실행 계획서.
기존 4개 문서(초기 기획, 오케스트레이션 플랜, 블루프린트, 아키텍처 리뷰)의 핵심을 통합하고,
리뷰에서 식별된 **P0 차단 이슈 4건을 모두 해소**한 상태의 최종 실행 로드맵이다.

### 입력 문서와 반영 현황

| 문서 | 핵심 기여 | 본 계획 반영 |
|---|---|---|
| `OHVIS_5SEC_IMMEDIATE_RESPONSE_ARCHITECTURE.md` | 5계층 아키텍처 원안, 6개 테이블 DDL, 10개 SSE 이벤트 | 아키텍처 골격 채택, SLA를 5초→3초로 강화 |
| `OHVIS_REALTIME_RESPONSE_ORCHESTRATION_PLAN.md` | 현재 상태 실측(169완료/31중단), 다중 질문 정책, 화면 UX 설계 | 실측 수치 기준선, UX 레이아웃 채택 |
| `OHVIS_PERFECT_RESPONSE_SYSTEM_BLUEPRINT.md` | Latency Budget, 7유형 분류, Hot Digest, 모듈 분리, Feature Flag | 핵심 설계의 80% 직접 채택 |
| `OHVIS_5SEC_ARCHITECTURE_REVIEW.md` | P0 4건, P1 6건, P2 5건 문제점 + 12건 개선안 | **P0 4건 전부 해소**, P1 6건 반영, P2 5건 후속 |

---

## 2. P0 차단 이슈 해소 방안

리뷰에서 차단으로 분류한 4건을 구축 전에 반드시 해결해야 한다.

### 2.1 5초 SLA 달성 경로 부재 → **3초 Latency Budget 확정**

| 구간 | 할당 | 구현 수단 | 초과 시 |
|---|---|---|---|
| Gateway (저장+라우팅) | 300ms | DB INSERT 1회 + 규칙 분류 (if/else, 0ms) | — |
| Context 조회 | 400ms | Redis `GET` (hot_digest) | Cache miss → DB 500ms, 총 800ms |
| LLM 즉답 생성 | 1,500ms | **Haiku 4.5** (first-token ~300ms, 200토큰 생성 ~1.2s) | Timeout → 기본 메시지 |
| SSE 전송 | 100ms | 기존 `StreamManager` 경로 | — |
| 여유분 | 700ms | jitter·GC·네트워크 흡수 | — |
| **합계** | **3,000ms** | | 3초 초과 시 "처리 중입니다" 기본 메시지로 **5초 SLA 100% 보장** |

**핵심 결정**: 즉답 모델은 **Haiku 4.5 고정**. Opus/Sonnet은 심층 전용. 이것이 3초를 가능하게 하는 유일한 경로.

### 2.2 11,416줄 모놀리스 확장 → **7개 모듈 분리 계획**

`chat_service.py` (11,416줄)에 코드를 추가하지 않는다. 대신 7개 신규 모듈을 생성하고, `chat_service.py`는 기존 함수를 유지하면서 새 모듈을 호출하는 얇은 어댑터로만 확장한다.

| 순서 | 모듈 파일 | 책임 | 예상 줄수 | 의존 |
|---|---|---|---|---|
| 1 | `app/services/chat_turn_gateway.py` | 턴 접수, 저장, 분류, 라우팅 | 200-300 | DB, Redis |
| 2 | `app/services/chat_immediate.py` | 즉답 생성 (Haiku 호출, 스트리밍) | 250-350 | Gateway, LLM Client |
| 3 | `app/services/context_cache.py` | Hot Digest 캐시 (Redis CRUD) | 150-200 | Redis |
| 4 | `app/services/deep_work_manager.py` | 심층 작업 Job 관리 (생성, 단계 전이, 복구) | 300-400 | DB, LLM Client |
| 5 | `app/services/response_coordinator.py` | 결과 귀속, 합성 보고, 순서 역전 처리 | 200-300 | DB, SSE |
| 6 | `app/core/interrupt_queue_durable.py` | 인터럽트 큐 DB 영속화 (기존 인메모리 대체) | 100-150 | DB |
| 7 | `app/services/turn_quality_tracker.py` | 소요시간·품질·비용 데이터 수집 | 100-150 | DB |

**chat_service.py 수정 범위**: `send_message_stream()` 함수(line 7906~) 진입부에 Feature Flag 분기 1개 추가. Flag ON이면 `chat_turn_gateway.accept_turn()` 호출, OFF이면 기존 로직 유지. **기존 코드 삭제 없음**.

### 2.3 마이그레이션 전략 부재 → **Feature Flag + Canary 전략**

```
Phase A (기반)     → flag OFF, 모듈만 배포, 기존 로직 100%
Phase B (즉답)     → flag ON 10% canary (CEO 세션만)
Phase C (검증)     → 48시간 canary 모니터링, p95 < 5초 확인
Phase D (전면)     → flag ON 100%, 기존 즉답 경로 deprecated
Phase E (심층)     → deep_work_manager 활성화
Phase F (합성)     → response_coordinator 합성 보고 활성화
```

**Flag 구현**: DB `system_config` 테이블에 `ohvis_immediate_enabled` 키. 값 변경 즉시 반영 (요청마다 조회, Redis 캐시 60초).

**롤백**: Flag OFF → 즉시 기존 로직 복귀. DB 테이블은 남지만 쓰기만 중단되므로 데이터 손실 없음.

### 2.4 R-AUTH 위반 (gpt-5.6-sol 지정) → **Anthropic 우선 라우팅 확정**

| 용도 | 모델 | 근거 |
|---|---|---|
| 질문 분류 | Haiku 4.5 | 규칙 분류 실패 시에만, 0.5초 이내 |
| 즉답 생성 | **Haiku 4.5** | 3초 SLA 유일 경로, $1/$5 저비용 |
| 심층 분석 | Sonnet 4.6 | 균형 모델, $3/$15 |
| 고위험 판단 | Opus 4.6+ | 아키텍처·보안·비용, $5/$25 |
| 폴백 | Gemini (LiteLLM 경유) | Anthropic 전면 장애 시에만 |

**gpt-5.6-sol은 즉답/심층 어디에도 사용하지 않는다.** Codex Relay 경유 모델은 레이턴시가 불안정하여 SLA 보장 불가.

---

## 3. 아키텍처 설계

### 3.1 전체 흐름도

```
사용자 질문 입력
       │
       ▼
┌─────────────────────────┐
│  Chat Turn Gateway      │  ← 300ms: 저장 + 분류 + 라우팅
│  (chat_turn_gateway.py) │
└────────┬────────────────┘
         │
    ┌────┴────┐
    │ 분류 결과 │
    └────┬────┘
         │
    ┌────▼────────────┐          ┌──────────────────────┐
    │ simple/status/  │          │ analysis/code_modify/ │
    │ greeting/follow │          │ deploy/multi_part/    │
    │                 │          │ dependent             │
    └────┬────────────┘          └────┬─────────────────┘
         │                            │
         ▼                            ▼
┌─────────────────────┐    ┌──────────────────────────┐
│ Immediate Response  │    │ Immediate ACK (0.5초)    │
│ (chat_immediate.py) │    │ + Deep Work Manager      │
│ Haiku 4.5, 3초 완료 │    │ (deep_work_manager.py)   │
└────────┬────────────┘    │ Sonnet/Opus, 비동기 처리  │
         │                 └────────┬─────────────────┘
         │                          │
         ▼                          ▼
┌─────────────────────────────────────────────┐
│ Response Coordinator (response_coordinator.py)│
│ - 턴 귀속 (turn_sequence 기준)               │
│ - 순서 역전 처리 (완료 순 ≠ 질문 순)          │
│ - 합성 보고 (다중 결과 → 종합 응답)           │
└────────┬────────────────────────────────────┘
         │
         ▼
┌─────────────────────┐
│ SSE Stream → 프론트  │
│ 10개 이벤트 타입     │
└─────────────────────┘
```

### 3.2 Context Cache Layer

```
┌─────────────────────────────────────────┐
│           Redis (aads-redis)            │
│                                         │
│  hot:{session_id}:turns    ← 최근 20턴  │
│  hot:{session_id}:jobs     ← 활성 Job들  │
│  hot:{session_id}:todos    ← TODO 목록   │
│  hot:{session_id}:identity ← 워크스페이스│
│  hot:{session_id}:digest   ← 압축 요약   │
│                                         │
│  TTL: 30분 (활동 시 갱신)                │
│  무효화: DB 변경 이벤트 시 즉시 삭제     │
└─────────────────────────────────────────┘
```

**Cache miss 정책**: Redis miss → DB 조회 → Redis SET → 응답. miss penalty ~500ms. 초기 세션 접속 시 1회만 발생.

### 3.3 질문 유형 분류 (2단계)

| 단계 | 방식 | 소요시간 | 정확도 |
|---|---|---|---|
| 1단계 | 규칙 기반 (키워드 + 패턴) | 0ms | ~70% |
| 2단계 | Haiku 4.5 분류 (1단계 불확실 시) | ~500ms | ~95% |

**7가지 유형**:

| 유형 | 처리 경로 | 예시 |
|---|---|---|
| `greeting` | 즉답 (규칙, LLM 불필요) | "안녕", "고마워" |
| `simple` | 즉답 (Haiku) | "현재 시간?", "서버 상태?" |
| `status` | 즉답 (Haiku + 도구 1회) | "러너 진행상황?", "배포 됐나?" |
| `follow_up` | 즉답 (Haiku, 맥락 참조) | "그거 자세히", "왜?" |
| `analysis` | 즉답 ACK + 심층 (Sonnet) | "성능 분석해줘", "비교 보고" |
| `code_modify` | 즉답 ACK + 심층 (Sonnet/Runner) | "이 버그 고쳐", "기능 추가" |
| `deploy` | 즉답 ACK + 심층 (Opus) | "배포해줘", "서버 재시작" |

### 3.4 다중 질문 병렬 처리

#### 동시성 한도

| 범위 | 한도 | 근거 |
|---|---|---|
| 세션당 심층 Job | 최대 5개 | LLM 동시 호출 비용 통제 |
| 세션당 Runner Job | 최대 2개 | 파일 충돌 방지 |
| 시스템 전체 심층 Job | 최대 20개 | 서버68 리소스 한계 |
| 즉답은 한도 없음 | — | Haiku 저비용, <3초 완료 |

#### 질문 완료 순서 역전 처리

```
[시나리오]
Q1: "전체 코드 분석해줘" (심층, 예상 120초)
Q2: "현재 서버 상태?" (즉답, 3초)
Q3: "러너 진행상황?" (즉답, 3초)

[처리 흐름]
t=0초   Q1 접수 → 즉답 ACK "분석을 시작합니다. 진행 상황을 표시합니다."
t=1초   Q2 접수 → 즉답 완료 "서버 정상, CPU 23%, 메모리 64%"
t=2초   Q3 접수 → 즉답 완료 "활성 러너 0건, 대기 0건"
t=0~120초  Q1 심층 진행 → 우측 패널에 단계별 진행 표시
t=120초  Q1 심층 완료 → 메인 버블에 분석 결과 삽입 (Q2, Q3 아래에 위치하지 않음)

[핵심 규칙]
- 완료된 결과는 해당 질문의 turn_sequence 위치에 삽입
- 이미 지나간 위치의 결과는 상단 칩 알림 + 클릭 시 해당 위치로 스크롤
- 현재 화면 하단에 있지 않은 결과를 하단에 추가하지 않음 → 스크롤 역주행 방지
```

#### 합성 보고 (Synthesis)

여러 심층 Job이 동일 주제에 대한 결과를 생성한 경우:

1. `response_coordinator`가 `synthesis_group_id`로 관련 Job을 묶음
2. 개별 결과가 모두 완료되면 Sonnet으로 합성 보고서 생성
3. 합성 보고서는 별도 "종합 보고" 버블로 표시
4. 개별 결과 버블은 접힘(collapse) 처리

---

## 4. 데이터 모델 (DDL)

### 4.1 신규 테이블 6개

```sql
-- 1. 턴 원장: 모든 질문의 접수-분류-라우팅 기록
CREATE TABLE IF NOT EXISTS chat_turns (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES chat_sessions(id),
    turn_sequence INTEGER NOT NULL,
    user_message_id UUID REFERENCES chat_messages(id),
    question_type VARCHAR(20) NOT NULL DEFAULT 'simple',
    -- simple, greeting, status, follow_up, analysis, code_modify, deploy
    route VARCHAR(20) NOT NULL DEFAULT 'immediate',
    -- immediate, deep, hybrid
    priority VARCHAR(20) NOT NULL DEFAULT 'normal',
    -- urgent, normal, background
    depends_on UUID REFERENCES chat_turns(id),
    status VARCHAR(20) NOT NULL DEFAULT 'accepted',
    -- accepted, immediate_done, deep_queued, deep_running, completed, failed, cancelled
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    UNIQUE(session_id, turn_sequence)
);

CREATE INDEX idx_chat_turns_session_status ON chat_turns(session_id, status);
CREATE INDEX idx_chat_turns_session_seq ON chat_turns(session_id, turn_sequence);

-- 2. 즉답 기록: 3초 SLA 추적
CREATE TABLE IF NOT EXISTS chat_immediate_responses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    turn_id UUID NOT NULL REFERENCES chat_turns(id),
    model_used VARCHAR(50) NOT NULL,
    -- claude-haiku-4-5, rule-based, fallback-message
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    first_token_ms INTEGER,       -- LLM first-token 레이턴시
    total_ms INTEGER NOT NULL,    -- Gateway 접수 ~ SSE 전송 완료
    sla_met BOOLEAN NOT NULL DEFAULT true,  -- total_ms <= 3000
    content_preview TEXT,         -- 즉답 내용 앞 200자
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_immediate_turn ON chat_immediate_responses(turn_id);
CREATE INDEX idx_immediate_sla ON chat_immediate_responses(sla_met, created_at);

-- 3. 심층 작업 Job
CREATE TABLE IF NOT EXISTS chat_deep_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    turn_id UUID NOT NULL REFERENCES chat_turns(id),
    session_id UUID NOT NULL REFERENCES chat_sessions(id),
    job_type VARCHAR(30) NOT NULL,
    -- analysis, code_modify, deploy, research, synthesis
    model_used VARCHAR(50),
    status VARCHAR(20) NOT NULL DEFAULT 'queued',
    -- queued, running, step_checkpoint, completed, failed, cancelled, timeout
    current_step VARCHAR(50),
    total_steps INTEGER DEFAULT 1,
    completed_steps INTEGER DEFAULT 0,
    progress_pct SMALLINT DEFAULT 0,
    result_message_id UUID REFERENCES chat_messages(id),
    error_message TEXT,
    retry_count SMALLINT DEFAULT 0,
    max_retries SMALLINT DEFAULT 3,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    timeout_at TIMESTAMPTZ,       -- 절대 타임아웃 (시작 + 30분)
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_deep_jobs_session_status ON chat_deep_jobs(session_id, status);
CREATE INDEX idx_deep_jobs_turn ON chat_deep_jobs(turn_id);
CREATE INDEX idx_deep_jobs_timeout ON chat_deep_jobs(status, timeout_at)
    WHERE status IN ('queued', 'running');

-- 4. 심층 작업 단계 체크포인트
CREATE TABLE IF NOT EXISTS chat_deep_job_steps (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id UUID NOT NULL REFERENCES chat_deep_jobs(id) ON DELETE CASCADE,
    step_name VARCHAR(50) NOT NULL,
    step_order SMALLINT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    -- pending, running, completed, failed, skipped
    input_summary TEXT,
    output_summary TEXT,
    tokens_used INTEGER DEFAULT 0,
    duration_ms INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX idx_deep_steps_job ON chat_deep_job_steps(job_id, step_order);

-- 5. 합성 보고 그룹
CREATE TABLE IF NOT EXISTS chat_synthesis_groups (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES chat_sessions(id),
    trigger_type VARCHAR(20) NOT NULL DEFAULT 'auto',
    -- auto (시스템 판단), manual (CEO 요청), scheduled
    job_ids UUID[] NOT NULL,              -- 합성 대상 Job ID 배열
    synthesis_message_id UUID REFERENCES chat_messages(id),
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    -- pending, generating, completed, failed
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX idx_synthesis_session ON chat_synthesis_groups(session_id, status);

-- 6. 인터럽트 큐 영속화 (인메모리 대체)
CREATE TABLE IF NOT EXISTS chat_interrupt_queue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL,
    content TEXT NOT NULL,
    attachments JSONB DEFAULT '[]',
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    -- pending, consumed, expired
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    consumed_at TIMESTAMPTZ
);

CREATE INDEX idx_interrupt_session_status ON chat_interrupt_queue(session_id, status)
    WHERE status = 'pending';
```

### 4.2 기존 테이블 확장

```sql
-- chat_turn_executions에 turn_id 연결 컬럼 추가
ALTER TABLE chat_turn_executions
    ADD COLUMN IF NOT EXISTS turn_id UUID REFERENCES chat_turns(id);

-- chat_messages에 turn_sequence 역참조 추가
ALTER TABLE chat_messages
    ADD COLUMN IF NOT EXISTS turn_sequence INTEGER;
```

### 4.3 시스템 설정 (Feature Flag)

```sql
-- system_config 테이블이 없으면 생성
CREATE TABLE IF NOT EXISTS system_config (
    key VARCHAR(100) PRIMARY KEY,
    value JSONB NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Feature Flag 초기값
INSERT INTO system_config (key, value) VALUES
    ('ohvis_immediate_enabled', '{"enabled": false, "canary_pct": 0, "canary_sessions": []}')
ON CONFLICT (key) DO NOTHING;
```

---

## 5. SSE 이벤트 규격 (10개)

| 이벤트 | 발생 시점 | payload 주요 필드 |
|---|---|---|
| `turn_accepted` | Gateway 접수 완료 | `turn_id`, `turn_sequence`, `question_type`, `route` |
| `immediate_start` | 즉답 생성 시작 | `turn_id`, `model` |
| `immediate_delta` | 즉답 토큰 스트리밍 | `turn_id`, `content` (기존 delta와 동일 형식) |
| `immediate_done` | 즉답 완료 | `turn_id`, `total_ms`, `sla_met` |
| `deep_queued` | 심층 Job 생성 | `turn_id`, `job_id`, `job_type`, `estimated_seconds` |
| `deep_progress` | 심층 진행 갱신 | `job_id`, `step`, `progress_pct`, `message` |
| `deep_done` | 심층 Job 완료 | `job_id`, `turn_id`, `result_message_id` |
| `deep_failed` | 심층 Job 실패 | `job_id`, `error`, `will_retry` |
| `synthesis_ready` | 합성 보고 완료 | `group_id`, `synthesis_message_id` |
| `fallback_notice` | 모델 폴백 발생 | `from_model`, `to_model`, `reason` |

**하위 호환**: 기존 `delta`, `done`, `error` 이벤트는 Feature Flag OFF 시 그대로 유지. Flag ON 시 위 이벤트로 대체.

---

## 6. 프론트엔드 설계

### 6.1 화면 레이아웃

```
┌─────────────────────────────────────────────────────┐
│ 세션 Status Bar                                      │
│ [🟢 즉답 대기] [심층 2건 진행중] [Q7 완료 알림 칩]    │
├───────────────────────────┬─────────────────────────┤
│                           │                         │
│  채팅 버블 영역 (메인)     │  심층 작업 패널 (우측)   │
│                           │                         │
│  Q5: 사용자 질문           │  ┌─────────────────┐   │
│  A5: 즉답 (3초)            │  │ Job #1: 코드분석 │   │
│       소요시간: 2.3초      │  │ ██████░░ 75%    │   │
│                           │  │ 현재: 파일 읽기   │   │
│  Q6: 사용자 질문           │  └─────────────────┘   │
│  A6: [심층 처리 중...]     │  ┌─────────────────┐   │
│       → Job #2 진행중      │  │ Job #2: DB분석   │   │
│                           │  │ ███░░░░░ 30%    │   │
│  Q7: 사용자 질문           │  │ 현재: 쿼리 실행   │   │
│  A7: 즉답 완료             │  └─────────────────┘   │
│                           │                         │
├───────────────────────────┴─────────────────────────┤
│ [입력창 — 항상 활성]                     [전송] [📎] │
└─────────────────────────────────────────────────────┘
```

### 6.2 React 훅 인터페이스

```typescript
// 턴 상태 관리
function useTurnState(sessionId: string) {
  return {
    turns: Turn[],              // 전체 턴 목록
    activeTurn: Turn | null,    // 현재 즉답 진행 중인 턴
    sendMessage: (content: string, attachments?: File[]) => Promise<Turn>,
    cancelTurn: (turnId: string) => void,
  }
}

// 심층 Job 상태 관리
function useDeepJobs(sessionId: string) {
  return {
    jobs: DeepJob[],            // 활성 Job 목록
    completedJobs: DeepJob[],   // 완료된 Job (최근 10건)
    cancelJob: (jobId: string) => void,
    retryJob: (jobId: string) => void,
  }
}

// 세션 상태 바
function useSessionStatus(sessionId: string) {
  return {
    immediateReady: boolean,    // 즉답 대기 상태
    activeJobCount: number,     // 진행 중 심층 Job 수
    notifications: Notification[], // 완료 알림 칩
    dismissNotification: (id: string) => void,
  }
}
```

### 6.3 모바일 대응

- 우측 패널 → 하단 시트(bottom sheet)로 변환
- 상태 바 → 고정 하단 미니 바 (1줄)
- 심층 진행 → 버블 내 인라인 프로그레스 바

---

## 7. 모듈 상세 설계

### 7.1 chat_turn_gateway.py

```python
# 핵심 함수 시그니처
async def accept_turn(
    session_id: str,
    user_message: str,
    attachments: list[dict] | None = None,
    *,
    pool: asyncpg.Pool,
    redis: aioredis.Redis,
) -> TurnResult:
    """
    1. turn_sequence 채번 (session 내 max + 1)
    2. chat_turns INSERT
    3. classify_question() → question_type, route 결정
    4. route에 따라:
       - immediate → chat_immediate.generate()
       - deep → deep_work_manager.create_job() + chat_immediate.generate_ack()
       - hybrid → 둘 다
    5. TurnResult 반환 (turn_id, question_type, route)
    """
```

**분류 로직 (2단계)**:

```python
def classify_by_rules(message: str) -> str | None:
    """규칙 기반 분류. 확실하면 유형 반환, 불확실하면 None"""
    # 인사: "안녕", "고마워", "ㅎㅇ" 등
    # 상태: "상태", "현황", "됐나", "했나" 등
    # 코드: "수정해", "고쳐", "추가해", "삭제해" + 파일/함수명
    # 배포: "배포", "deploy", "재시작" 등
    ...

async def classify_by_llm(message: str, context: str) -> str:
    """Haiku 4.5로 분류. 규칙 실패 시에만 호출"""
    # 프롬프트: "다음 질문을 7가지 유형 중 하나로 분류하세요: ..."
    # 응답: JSON {"type": "analysis", "confidence": 0.92}
    ...
```

### 7.2 chat_immediate.py

```python
async def generate_immediate(
    turn: TurnRecord,
    context: str,       # hot_digest에서 가져온 압축 맥락
    *,
    pool: asyncpg.Pool,
    sse_send: Callable,
) -> ImmediateResult:
    """
    1. SSE turn_accepted 전송
    2. SSE immediate_start 전송
    3. Haiku 4.5 스트리밍 호출 (max_tokens=500)
    4. 토큰마다 SSE immediate_delta 전송
    5. 완료 시 chat_immediate_responses INSERT
    6. SSE immediate_done 전송 (total_ms, sla_met)
    """

async def generate_ack(
    turn: TurnRecord,
    job_type: str,
    *,
    sse_send: Callable,
) -> None:
    """심층 라우팅 시 즉시 ACK 메시지 생성 (규칙 기반, LLM 불필요)"""
    # "분석을 시작합니다. 우측 패널에서 진행 상황을 확인하실 수 있습니다."
    # "코드 수정을 진행합니다. 완료되면 결과를 보고드리겠습니다."
```

### 7.3 context_cache.py

```python
class ContextCache:
    """Redis 기반 세션 맥락 캐시"""

    CACHE_KEYS = {
        'turns':    'hot:{sid}:turns',     # 최근 20턴 요약
        'jobs':     'hot:{sid}:jobs',      # 활성 Job 상태
        'todos':    'hot:{sid}:todos',     # TODO 목록
        'identity': 'hot:{sid}:identity',  # 워크스페이스 정체성
        'digest':   'hot:{sid}:digest',    # 전체 압축 요약
    }
    TTL = 1800  # 30분

    async def get_hot_digest(self, session_id: str) -> str:
        """캐시 hit → 즉시 반환, miss → DB 조회 후 캐시 SET"""

    async def invalidate(self, session_id: str, key: str = None):
        """DB 변경 시 호출. key 지정하면 해당 키만, 없으면 전체 삭제"""

    async def warm_up(self, session_id: str):
        """세션 접속 시 전체 캐시 프리로드 (1회)"""
```

### 7.4 deep_work_manager.py

```python
async def create_job(
    turn: TurnRecord,
    job_type: str,
    *,
    pool: asyncpg.Pool,
    sse_send: Callable,
) -> str:
    """
    1. 동시성 한도 체크 (세션 5개, 시스템 20개)
    2. chat_deep_jobs INSERT (status=queued)
    3. timeout_at 설정 (NOW() + 30분)
    4. SSE deep_queued 전송
    5. asyncio.create_task(execute_job()) → 비동기 실행
    6. job_id 반환
    """

async def execute_job(job_id: str, ...):
    """
    1. status → running, started_at 기록
    2. 단계별 실행 (step INSERT → LLM/도구 호출 → step UPDATE)
    3. 단계마다 SSE deep_progress 전송
    4. 완료 시 result_message INSERT → SSE deep_done
    5. 실패 시 retry_count 체크 → 재시도 or failed → SSE deep_failed
    """

async def cleanup_stale_jobs():
    """
    1분 주기 실행.
    - timeout_at < NOW() → status=timeout, SSE deep_failed
    - heartbeat 없이 updated_at 5분 초과 → status=failed
    """
```

### 7.5 response_coordinator.py

```python
async def attribute_result(
    job_id: str,
    result_message_id: str,
    *,
    pool: asyncpg.Pool,
    sse_send: Callable,
) -> None:
    """
    1. job → turn_id → turn_sequence 조회
    2. 현재 화면 하단 turn_sequence와 비교
    3. 같으면: 메인 버블에 직접 삽입
    4. 다르면: 상단 칩 알림 SSE 전송 (클릭 시 해당 위치로 스크롤)
    """

async def check_synthesis(
    session_id: str,
    *,
    pool: asyncpg.Pool,
) -> str | None:
    """
    동일 세션에서 5분 이내 완료된 관련 Job이 2개 이상이면
    synthesis_group 생성 → Sonnet으로 합성 → synthesis_ready SSE
    """
```

### 7.6 interrupt_queue_durable.py

```python
async def push_interrupt(session_id: str, content: str, attachments: list = None, *, pool):
    """DB INSERT (기존 인메모리 dict 대체)"""

async def pop_interrupts(session_id: str, *, pool) -> list[dict]:
    """pending 상태 조회 → consumed로 UPDATE → 반환"""

async def has_pending(session_id: str, *, pool) -> bool:
    """pending 존재 여부만 빠르게 체크 (COUNT)"""
```

### 7.7 turn_quality_tracker.py

```python
async def record_immediate_quality(
    turn_id: str,
    total_ms: int,
    model: str,
    tokens: dict,
    *,
    pool,
) -> None:
    """chat_immediate_responses INSERT + sla_met 자동 계산"""

async def get_sla_stats(hours: int = 24, *, pool) -> dict:
    """최근 N시간 SLA 통계: p50, p95, p99, sla_met_rate"""
```

---

## 8. 구축 로드맵 (6 Phase)

### Phase A: 기반 구축 (1~2일)

| 순서 | 작업 | 담당 | 검증 |
|---|---|---|---|
| A-1 | DB 마이그레이션: 6개 테이블 + 2개 ALTER + system_config 생성 | Runner | `\dt chat_turns`, `\dt chat_deep_jobs` 존재 확인 |
| A-2 | `context_cache.py` 생성 + Redis 연결 검증 | Runner | `pytest test_context_cache.py` |
| A-3 | `turn_quality_tracker.py` 생성 | Runner | `pytest test_quality_tracker.py` |
| A-4 | `interrupt_queue_durable.py` 생성 | Runner | `pytest test_interrupt_durable.py` |
| A-5 | `system_config` Feature Flag 헬퍼 함수 | Runner | Flag 읽기/쓰기 테스트 |

**Phase A 완료 기준**: 테이블 6개 존재, Redis GET/SET 동작, Flag OFF 상태에서 기존 채팅 정상 동작.

**예상 비용**: Runner 1회 ($2~5)

### Phase B: 즉답 레이어 (2~3일)

| 순서 | 작업 | 담당 | 검증 |
|---|---|---|---|
| B-1 | `chat_turn_gateway.py` 생성 (분류 + 라우팅) | Runner | 단위 테스트: 7유형 분류 정확도 |
| B-2 | `chat_immediate.py` 생성 (Haiku 즉답) | Runner | Haiku 호출 → 3초 이내 응답 확인 |
| B-3 | `chat_service.py` line 7906에 Feature Flag 분기 추가 | Runner | Flag OFF → 기존 동작, Flag ON → Gateway 진입 |
| B-4 | SSE 이벤트 4종 추가 (`turn_accepted`, `immediate_*`) | Runner | SSE curl 테스트 |
| B-5 | 프론트: 즉답 버블 렌더링 + 소요시간 표시 + 상태 바 | Runner | 대시보드 빌드 성공 |

**Phase B 완료 기준**: CEO 세션에서 Flag ON → 간단 질문 3초 이내 즉답 확인.

**예상 비용**: Runner 2~3회 ($5~10)

### Phase C: Canary 검증 (48시간)

| 순서 | 작업 | 담당 | 검증 |
|---|---|---|---|
| C-1 | Flag canary_sessions에 CEO 세션 ID 추가 | 직접 (DB UPDATE) | 해당 세션만 즉답 동작 |
| C-2 | 48시간 SLA 모니터링 | 자동 (turn_quality_tracker) | p95 < 5초, 실패율 < 1% |
| C-3 | 기존 세션 정상 동작 모니터링 | Watchdog | 에러 로그 증가 없음 |

**Phase C 완료 기준**: 48시간 동안 canary 세션 p95 < 5초, 비canary 세션 장애 0건.

**예상 비용**: 모니터링만 ($0)

### Phase D: 전면 전환 (1일)

| 순서 | 작업 | 담당 | 검증 |
|---|---|---|---|
| D-1 | Flag enabled=true, canary_pct=100 | 직접 (DB UPDATE) | 전 세션 즉답 동작 |
| D-2 | 24시간 전면 모니터링 | 자동 + Watchdog | 전체 SLA 통계 |
| D-3 | 이상 없으면 기존 즉답 경로 `@deprecated` 마킹 | Runner | — |

**Phase D 완료 기준**: 24시간 전면 운영 안정, SLA 기준 충족.

**예상 비용**: Runner 1회 ($1~2)

### Phase E: 심층 작업 (3~5일)

| 순서 | 작업 | 담당 | 검증 |
|---|---|---|---|
| E-1 | `deep_work_manager.py` 생성 | Runner | Job 생성-실행-완료 사이클 테스트 |
| E-2 | 심층 단계 체크포인트 (`chat_deep_job_steps`) | Runner | 단계별 INSERT/UPDATE 검증 |
| E-3 | SSE 이벤트 3종 (`deep_queued`, `deep_progress`, `deep_done/failed`) | Runner | SSE 스트림 테스트 |
| E-4 | stale Job 정리 cron (`cleanup_stale_jobs`, 1분 주기) | Runner | timeout_at 초과 Job 자동 정리 확인 |
| E-5 | 프론트: 우측 심층 패널 + 진행 바 + 완료 알림 칩 | Runner | 대시보드 빌드 + 심층 Job 진행 표시 확인 |
| E-6 | Gateway에 deep 라우팅 연결 | Runner | 분석 질문 → Job 생성 → 진행 → 완료 E2E |

**Phase E 완료 기준**: "코드 분석해줘" → 즉답 ACK 3초 → 심층 진행 패널 표시 → 결과 버블 삽입.

**예상 비용**: Runner 3~5회 ($8~15)

### Phase F: 합성 + 마무리 (2~3일)

| 순서 | 작업 | 담당 | 검증 |
|---|---|---|---|
| F-1 | `response_coordinator.py` 생성 | Runner | 순서 역전 처리 테스트 |
| F-2 | 합성 보고 (`chat_synthesis_groups`) | Runner | 관련 Job 2개 → 합성 보고서 생성 |
| F-3 | 프론트: 합성 버블 + 접힘 처리 + 알림 칩 클릭→스크롤 | Runner | 대시보드 E2E |
| F-4 | SLA 대시보드 (관리자 페이지에 통계 차트) | Runner | p50/p95/p99 차트 렌더링 |
| F-5 | 인터럽트 큐 DB 전환 (인메모리 → durable) | Runner | 서버 재시작 후 인터럽트 보존 확인 |
| F-6 | 기존 heartbeat pump updated_at 갱신 버그 수정 | Runner | stale watchdog 정상 감지 확인 |

**Phase F 완료 기준**: 다중 질문 → 순서 역전 → 합성 보고 → 정상 표시 E2E.

**예상 비용**: Runner 3~4회 ($8~12)

---

## 9. 비용 및 일정 요약

### 일정

| Phase | 기간 | 누적 |
|---|---|---|
| A: 기반 | 1~2일 | 1~2일 |
| B: 즉답 | 2~3일 | 3~5일 |
| C: Canary | 2일 (대기) | 5~7일 |
| D: 전면 | 1일 | 6~8일 |
| E: 심층 | 3~5일 | 9~13일 |
| F: 합성 | 2~3일 | 11~16일 |
| **총계** | **11~16일** | |

### 비용

| 항목 | 예상 비용 |
|---|---|
| Pipeline Runner (코드 작성) | $20~45 |
| Haiku 4.5 즉답 (운영) | ~$0.5/일 (200회 기준) |
| Sonnet 심층 (운영) | ~$2/일 (20회 기준) |
| Opus 고위험 (운영) | ~$1/일 (5회 기준) |
| **구축 총 비용** | **$20~45** |
| **월 운영 비용 증감** | **+$3~5/일** (즉답 Haiku 추가 + 기존 비용 유지) |

### 리스크

| 리스크 | 확률 | 영향 | 대응 |
|---|---|---|---|
| Haiku 3초 SLA 미달 (네트워크 jitter) | 중 | p95 초과 | 기본 메시지 폴백으로 5초 보장 |
| chat_service.py Flag 분기 충돌 | 하 | 기존 채팅 장애 | Flag OFF 즉시 롤백 |
| Redis 장애 | 하 | 캐시 miss → DB 직접 조회 | graceful degradation |
| 모듈 분리 시 import 순환 | 중 | 빌드 실패 | 의존 순서대로 생성 (A→B→...) |

---

## 10. 완료 기준 종합

| 기준 | 측정 방법 | 목표 |
|---|---|---|
| 즉답 p95 | `chat_immediate_responses.total_ms` p95 | ≤ 5,000ms |
| 즉답 p50 | 같은 테이블 p50 | ≤ 3,000ms |
| SLA 충족률 | `sla_met = true` 비율 | ≥ 95% |
| 심층 실패율 | `chat_deep_jobs.status = 'failed'` / 전체 | ≤ 5% |
| stale Job | running 상태 30분 초과 | 0건 |
| 인터럽트 손실 | 서버 재시작 후 pending 보존 | 100% |
| 스크롤 역주행 | CEO 체감 + 프론트 로그 | 0건 |
| 질문 유실 | 전송 후 화면에서 사라짐 | 0건 |
| 합성 보고 정확도 | CEO 피드백 | ≥ 80% 유용 |

---

## 11. 즉시 실행 가능한 첫 작업

**Phase A-1: DB 마이그레이션** — 아래 DDL을 `scripts/migrations/` 디렉터리에 저장하고 Runner로 실행.

```
TASK_ID: AADS-OHVIS-PHASE-A-FOUNDATION-20260802
TITLE: OHVIS 즉각 응답 시스템 Phase A - DB 기반 구축
PRIORITY: P1
SIZE: M
MODE: code_modify

목표:
1. 6개 신규 테이블 + 2개 ALTER + system_config 생성
2. context_cache.py, turn_quality_tracker.py, interrupt_queue_durable.py 모듈 생성
3. Feature Flag 헬퍼 함수 구현
4. 단위 테스트 작성 및 통과 확인

검증:
- \dt chat_turns, chat_immediate_responses, chat_deep_jobs, chat_deep_job_steps, chat_synthesis_groups, chat_interrupt_queue 존재
- system_config에 ohvis_immediate_enabled 키 존재
- Redis GET/SET 동작
- pytest 전체 PASS
```

---

## 부록: 기존 문서 대비 변경점

| 항목 | 기존 문서 | 본 계획 변경 | 사유 |
|---|---|---|---|
| SLA 목표 | 5초 | **3초** (5초는 폴백 보장선) | Haiku first-token 300ms, 3초로 충분 |
| 즉답 모델 | gpt-5.6-sol / 미지정 | **Haiku 4.5 고정** | R-AUTH 준수 + 레이턴시 보장 |
| 마이그레이션 | Big bang | **Feature Flag + 48시간 Canary** | 전체 장애 방지 |
| 모놀리스 | chat_service.py 직접 확장 | **7개 모듈 분리 + 어댑터** | 15,000줄 방지 |
| 인터럽트 | 인메모리 (76줄) | **DB 영속화** | 재시작 시 손실 방지 |
| heartbeat 갱신 | 유지 | **stale 감지 방해 제거** | P0 운영 버그 |
| 구축 기간 | 10~16일 | **11~16일** (canary 48시간 포함) | 안전 마진 |
