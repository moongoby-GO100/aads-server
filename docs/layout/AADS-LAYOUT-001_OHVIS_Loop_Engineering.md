# AADS-LAYOUT-001 — OHVIS Loop Engineering 상세 구현 기획서

- 문서 유형: FLOW / Lay out (설계)
- 작성: 2026-07-27 KST
- 대상 시스템: AADS(서버68) OHVIS 3-Tier
- 상태: 설계 확정 대기 (CEO 승인 필요)
- 후속 산출물: AADS-{SEQ} 작업지시서 (Operate)

---

## 0. 한 줄 요약

현재 OHVIS는 "CEO가 말할 때마다 1회 실행"하는 **단발 실행 엔진**이다.
본 설계는 여기에 **종료조건 기반 자율 반복 루프(Loop Engine)** 를 얹어,
CEO가 한 번 지시하면 목표가 충족될 때까지 오비스가 스스로 재실행·재판단·보고하도록 만든다.

---

## 1. 현재 상태 (실측)

### 1.1 이미 존재하는 자산

| 구성요소 | 파일 | 규모 | 역할 | 출처 |
|---|---|---|---|---|
| Task Manager | `app/services/ohvis_task_manager.py` | 353줄 | create→update_step→complete→judge→artifact | [코드 조회] |
| Task API | `app/api/ohvis_tasks.py` | 312줄 | 작업 조회/제어 엔드포인트 | [코드 조회] |
| 자율 실행기 | `app/services/autonomous_executor.py` | 560줄 | 단발 자율 실행 | [코드 조회] |
| 에이전트 오케스트레이터 | `app/services/agent_orchestrator.py` | 722줄 | 병렬 에이전트 실행 | [코드 조회] |
| 작업 테이블 | `ohvis_tasks` (16컬럼) | — | 작업 생명주기 저장소 | [DB 조회] |

`ohvis_tasks` 컬럼 (실측): `id, session_id, title, status, task_type, steps, result,
ohvis_judgement, runner_job_id, agent_ids, cost_usd, created_at, updated_at,
completed_at, reported_at, parent_turn_id` [DB 조회]

### 1.2 현재 동작 상수 (실측)

| 상수 | 값 | 위치 | 의미 |
|---|---|---|---|
| `_MAX_CONCURRENT_TASKS` | 3 | ohvis_task_manager.py:21 | 동시 작업 3건 제한 |
| `_COMPLEX_INTENTS` | 8종 | ohvis_task_manager.py:16-19 | code_modify, deploy, execute, pipeline_runner, report, audit, cto_strategy, url_analyze |
| task status | running/done/error | `_finalize_steps()` | 3-상태 모델 |

### 1.3 구조적 공백 (= 이번 설계가 채우는 것)

| 공백 | 현상 | CEO 체감 |
|---|---|---|
| 반복 트리거 없음 | 작업이 done이면 그대로 종료 | "됐나?" 를 CEO가 다시 물어야 함 |
| 종료조건 개념 없음 | status만 있고 goal이 없음 | "될 때까지 해" 가 안 됨 |
| 재시도 정책 없음 | error면 멈춤 | 실패 복구를 CEO가 지시해야 함 |
| 반복 예산 없음 | 무한루프 위험 | 자율화 시 비용 폭주 리스크 |
| 이전 회차 기억 없음 | 각 실행이 독립 | 같은 실패를 반복 |

---

## 2. 설계 목표

1. **CEO 1회 지시 → 목표 충족까지 자율 반복**
2. **무한루프·비용폭주 원천 차단** (예산·횟수·시간 3중 상한)
3. **기존 3-Tier 구조 파괴 없이 얹기** (ohvis_tasks 확장, 신규 테이블 최소)
4. **모든 회차가 감사 가능** (회차별 근거·판정·비용 기록)

---

## 3. 아키텍처

### 3.1 계층 위치

```
Tier 1  CEO Chat (즉답)
Tier 2  Task Manager (작업 카드/진행)
Tier 3  Runner / Agent (실제 실행)
   ↑
[NEW] Loop Engine  ── Tier 2와 Tier 3 사이에 삽입
        - 종료조건 평가
        - 재실행 스케줄
        - 회차 메모리 전달
```

Loop Engine은 Tier 3를 **직접 대체하지 않는다.** Tier 3를 N회 호출하는 상위 제어기다.

### 3.2 상태 머신

```
        ┌──────────────────────────────────────┐
        ▼                                      │
  [PLANNED] → [RUNNING] → [EVALUATING] ────────┘ (계속)
                             │
                             ├─→ [SATISFIED]   목표 충족
                             ├─→ [EXHAUSTED]   예산/횟수/시간 소진
                             ├─→ [BLOCKED]     CEO 승인 필요
                             └─→ [ABORTED]     CEO 중단 지시
```

- `EVALUATING` 은 반드시 LLM 판정이 아니라 **규칙 우선 → 애매할 때만 LLM**.
- `BLOCKED` 는 배포/삭제/과금 등 승인필요 액션에서 강제 진입한다 (기존 보안 규칙 준수).

### 3.3 종료조건 4유형

| 유형 | CEO 발화 예 | 평가 방식 | 종료 |
|---|---|---|---|
| 조건형 | "에러 0 될 때까지 고쳐" | 지표 쿼리 | 지표 충족 |
| 횟수형 | "3번 더 돌려봐" | 카운터 | N회 도달 |
| 시간형 | "1시간 감시해" | 시계 | 종료시각 |
| 감시형 | "장애 나면 알려줘" | 이벤트 폴링 | CEO 중단까지 |

---

## 4. 데이터 모델

### 4.1 신규 테이블 `ohvis_loops`

```sql
CREATE TABLE ohvis_loops (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      UUID NOT NULL,
    root_task_id    UUID REFERENCES ohvis_tasks(id),
    goal_text       TEXT NOT NULL,           -- CEO 원문 지시
    goal_type       TEXT NOT NULL,           -- condition|count|time|watch
    goal_spec       JSONB NOT NULL DEFAULT '{}',  -- 평가 파라미터
    status          TEXT NOT NULL DEFAULT 'planned',
    iteration       INT  NOT NULL DEFAULT 0,
    max_iterations  INT  NOT NULL DEFAULT 5,
    budget_usd      NUMERIC(10,4) NOT NULL DEFAULT 5.0,
    spent_usd       NUMERIC(10,4) NOT NULL DEFAULT 0,
    deadline_at     TIMESTAMPTZ,
    next_run_at     TIMESTAMPTZ,
    last_verdict    JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at       TIMESTAMPTZ,
    close_reason    TEXT
);
CREATE INDEX idx_ohvis_loops_due
    ON ohvis_loops (next_run_at)
    WHERE status IN ('planned','running');
```

### 4.2 신규 테이블 `ohvis_loop_iterations`

```sql
CREATE TABLE ohvis_loop_iterations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    loop_id     UUID NOT NULL REFERENCES ohvis_loops(id) ON DELETE CASCADE,
    iteration   INT  NOT NULL,
    task_id     UUID REFERENCES ohvis_tasks(id),
    verdict     TEXT,                -- continue|satisfied|blocked|failed
    evidence    JSONB,               -- 판정 근거 (쿼리결과/지표/로그)
    cost_usd    NUMERIC(10,4) DEFAULT 0,
    started_at  TIMESTAMPTZ DEFAULT NOW(),
    ended_at    TIMESTAMPTZ,
    UNIQUE (loop_id, iteration)
);
```

### 4.3 기존 테이블 영향

`ohvis_tasks` 는 **스키마 변경 없음.** 루프는 `ohvis_loop_iterations.task_id` 로 기존 작업을 참조만 한다.
→ 기존 Task Manager/대시보드 회귀 위험 0.

---

## 5. 핵심 모듈 설계

### 5.1 `app/services/ohvis_loop_engine.py` (신규)

```python
async def create_loop(session_id, goal_text, goal_type, goal_spec,
                      max_iterations=5, budget_usd=5.0,
                      deadline_at=None) -> str:
    """CEO 지시를 루프로 등록. 반환 loop_id."""

async def tick(loop_id) -> dict:
    """1회차 실행: 실행 → 평가 → 다음 예약 또는 종료."""

async def evaluate(loop_id, iteration_result) -> Verdict:
    """규칙 우선 평가. 애매하면 LLM 판정 1회."""

async def close_loop(loop_id, reason) -> None:
    """종료 + CEO 최종 보고 아티팩트 저장."""
```

### 5.2 종료조건 평가기 (규칙 우선)

```python
def should_continue(loop, result) -> Verdict:
    # 1) 하드 상한 (LLM 호출 이전에 차단)
    if loop.iteration >= loop.max_iterations:
        return Verdict("exhausted", "최대 회차 도달")
    if loop.spent_usd >= loop.budget_usd:
        return Verdict("exhausted", "예산 소진")
    if loop.deadline_at and now() >= loop.deadline_at:
        return Verdict("exhausted", "기한 도달")

    # 2) 승인 게이트 (보안 규칙 우선)
    if result.requires_approval:
        return Verdict("blocked", "CEO 승인 필요")

    # 3) 목표별 규칙 평가
    if loop.goal_type == "count":
        return Verdict("satisfied") if loop.iteration >= loop.goal_spec["n"] \
               else Verdict("continue")
    if loop.goal_type == "condition":
        return _eval_condition(loop.goal_spec, result)   # 지표/쿼리 기반
    ...
    # 4) 여기까지 판정 불가일 때만 LLM 1회 호출
    return _llm_verdict(loop, result)
```

**설계 원칙**: LLM 판정은 최후 수단. 회차당 판정 LLM 호출 ≤ 1회로 고정한다 (CEO-DIRECTIVES: LLM 15회/task 준수).

### 5.3 회차 메모리 (같은 실패 반복 방지)

각 회차 프롬프트에 직전 2회차의 `verdict + evidence` 요약을 주입한다.

```
[이전 회차 요약]
1회차: continue — 테스트 3건 실패 (test_auth, test_stream, test_cost)
2회차: continue — test_auth 해결, test_stream 여전히 실패 (타임아웃 5s)
→ 이번 회차는 test_stream 타임아웃에 집중하라.
```

### 5.4 스케줄러 훅

기존 백그라운드 워커에 `loop_dispatcher` 추가:

```
매 30초: SELECT id FROM ohvis_loops
         WHERE status IN ('planned','running')
           AND next_run_at <= NOW()
         ORDER BY next_run_at LIMIT 3      -- 동시성 3 (기존 상수 정합)
→ 각 loop_id 에 대해 tick() 비동기 실행
```

동시성 상한 3은 기존 `_MAX_CONCURRENT_TASKS = 3` 과 정합시킨다 [코드 조회].

---

## 6. 안전장치 (필수)

| 장치 | 값(기본) | 근거 |
|---|---|---|
| 최대 회차 | 5회 | 폭주 차단 |
| 루프당 예산 | 모델별 자동 산출 (Sonnet $3.00 / Opus 5 $5.00 / Sol $5.83) | 상세 배율표는 `docs/AADS-LAYOUT-001_OHVIS-LOOP-SYSTEM.md` §6.3. 초과 시 CEO 승인, 최대 $30 |
| 기한 | 지시 시각 +24h | 좀비 루프 방지 |
| 무진전 감지 | 동일 verdict+evidence 2회 연속 → 강제 종료 | 헛돌기 차단 |
| 승인 게이트 | deploy/delete/ssh/docker/git push/과금 → BLOCKED | 기존 보안 규칙 |
| 세션당 활성 루프 | 최대 3개 | 자원 보호 |
| 킬 스위치 | `POST /api/v1/ohvis/loops/{id}/abort` | CEO 즉시 중단 |

**절대 금지**: 루프 내부에서 승인 없는 배포/삭제/force push 수행. BLOCKED 진입만 허용.

---

## 7. API 설계

| 메서드 | 경로 | 용도 |
|---|---|---|
| POST | `/api/v1/ohvis/loops` | 루프 생성 |
| GET | `/api/v1/ohvis/loops?session_id=` | 활성 루프 목록 |
| GET | `/api/v1/ohvis/loops/{id}` | 루프 상세 + 회차 이력 |
| POST | `/api/v1/ohvis/loops/{id}/abort` | 즉시 중단 |
| POST | `/api/v1/ohvis/loops/{id}/approve` | BLOCKED 해제 후 재개 |
| GET | `/api/v1/ohvis/loops/{id}/stream` | SSE 회차 진행 스트림 |

라우터: `app/api/ohvis_loops.py` (신규), `app/main.py` 에 include.

---

## 8. UI 설계 (대시보드)

기존 task_card 아티팩트 옆에 **loop_card** 추가.

```
┌─ 🔁 루프: "에러 0 될 때까지 고쳐"          [중단] ─┐
│ 회차 3/5   ·   $1.82/$5.00   ·   남은 시간 21h   │
│ ─────────────────────────────────────────────── │
│ 1회차  continue   테스트 3건 실패                │
│ 2회차  continue   1건 해결, 2건 남음             │
│ 3회차  running    진행 중…                       │
└─────────────────────────────────────────────────┘
```

파일: `aads-dashboard/src/components/chat/LoopCard.tsx` (신규)

---

## 9. 구현 단계 (P0 → P2)

| 단계 | 범위 | 산출물 | 예상 규모 |
|---|---|---|---|
| **P0-1** | DB 마이그레이션 2테이블 | `scripts/migrations/xxx_ohvis_loops.sql` | S |
| **P0-2** | Loop Engine 코어 (create/tick/evaluate/close) | `app/services/ohvis_loop_engine.py` | M |
| **P0-3** | 안전장치 3중 상한 + 킬스위치 | 동일 파일 + API | S |
| **P1-1** | 스케줄러 훅 (loop_dispatcher) | 기존 워커 확장 | S |
| **P1-2** | API 6종 | `app/api/ohvis_loops.py` | M |
| **P1-3** | 회차 메모리 주입 | Loop Engine 확장 | S |
| **P2-1** | LoopCard UI + SSE | 대시보드 | M |
| **P2-2** | 감시형(watch) 루프 | Loop Engine 확장 | M |

**P0만으로도 "될 때까지 해" 가 동작한다.** P1은 자동화, P2는 가시성.

---

## 10. 검증 방법 / 완료 기준

| 항목 | 검증 명령/방법 | 합격 기준 |
|---|---|---|
| 스키마 | `\d ohvis_loops` | 2테이블 + 인덱스 생성 |
| 상한 동작 | max_iterations=2 루프 실행 | 3회차 미실행, status=exhausted |
| 예산 상한 | budget_usd=0.01 루프 | 1회차 후 exhausted |
| 승인 게이트 | deploy 포함 목표 | status=blocked, 배포 미실행 |
| 무진전 감지 | 동일 결과 반환 목 | 2회 연속 후 강제 종료 |
| 킬스위치 | abort API 호출 | 5초 내 status=aborted |
| 회귀 | `pytest tests/unit/test_tools_and_pipeline.py -v` | 전건 PASS |
| 무중단 배포 | `docker exec aads-server bash /app/scripts/reload-api.sh` | health-check 200 |

**완료 선언 조건**: 위 8항목 전부 통과 + HANDOVER.md 갱신 (R-001) + 커밋·푸시 완료.

---

## 11. 리스크 및 대응

| 리스크 | 영향 | 대응 |
|---|---|---|
| 비용 폭주 | 높음 | 3중 상한 + 회차당 LLM 판정 1회 고정 |
| 좀비 루프 | 중간 | deadline_at 강제 + 24h 기본값 |
| 승인 우회 | 치명 | BLOCKED 강제, 루프 내 배포 코드 경로 자체를 차단 |
| 기존 Task 회귀 | 중간 | ohvis_tasks 스키마 무변경, 참조만 |
| 헛돌기 | 중간 | 무진전 감지 2회 룰 |
| DB 부하 | 낮음 | 부분 인덱스 (status 필터) |

---

## 12. 다음 단계

1. 본 설계 CEO 승인
2. 승인 시 `AADS-{SEQ}` 작업지시서 발행 (Operate 단계) — P0-1~P0-3 우선
3. P0 구현 → 검증 8항목 → 무중단 배포 → WRAP 문서

---

*본 문서의 수치 중 [DB 조회]/[코드 조회] 표기는 2026-07-27 실측값이다.
예상 규모(S/M) 및 기본 상한값은 설계 제안치로 실측이 아니다.*
