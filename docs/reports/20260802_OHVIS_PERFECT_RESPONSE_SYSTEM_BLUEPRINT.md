# OHVIS 완벽 응답 시스템 구축 블루프린트

> 작성: 2026-08-02 21:35 KST
> 기반: `OHVIS_REALTIME_RESPONSE_ORCHESTRATION_PLAN.md` + `OHVIS_5SEC_ARCHITECTURE_REVIEW.md` 심층 분석
> 목적: 즉각 응답 + 다중 질문 + 심층 분석 + 개발 처리를 **완벽에 가깝게** 수행하는 시스템 구축 방법론
> 상태: CEO 판단 대기

---

## 1. 핵심 결론

기존 기획서 2건은 "무엇을 만들 것인가"를 잘 정의했지만, **"어떻게 5초를 달성하는가"와 "현재 11,416줄 모놀리스를 어떻게 전환하는가"가 빈칸**이었다. 이 문서는 그 빈칸을 채운다.

**3가지 핵심 원칙**:
1. **즉각 응답은 LLM 모델 분기로 달성한다** — Haiku 4.5로 0.5~1초 내 즉답, Sonnet/Opus는 심층 전용
2. **다중 질문은 질문별 독립 Job으로 병렬 처리한다** — 하나가 느려도 다른 질문은 영향 없음
3. **모놀리스는 Feature Flag로 점진 전환한다** — Big bang 전환 금지, CEO 세션에서 canary 검증 후 확대

---

## 2. 현재 상태 vs 목표 상태

| 항목 | 현재 (실측) | 목표 | 달성 수단 |
|---|---|---|---|
| 첫 응답 체감 | 완성 답변까지 대기 (중앙값 58초) | **3초 이내 즉답** 표시 | Haiku 즉답 레이어 + hot cache |
| 입력창 상태 | 응답 완료까지 대기감 | **항상 입력 가능** | 즉답 후 즉시 입력창 재활성화 |
| 다중 질문 | 순차 처리, 이전 답변 지연 시 전체 멈춤 | **병렬 독립 처리** | 질문별 Job + Response Coordinator |
| 순서 역전 | 오래된 결과가 하단에 새 버블로 삽입 | **질문별 귀속** | turn_sequence + parent 연결 |
| 심층 작업 진행 | 본문에 "확인하겠습니다" 반복 | **상태줄/패널에 단계 표시** | SSE step 이벤트 + 프론트 상태 머신 |
| 코드 수정/배포 | 단일 스트림에서 모두 처리 | **심층 Job으로 분리, 진행 표시** | Deep Work Layer + Runner 연동 |
| 종합보고 | 수동으로 "종합해줘" 요청 | **자동 취합** | synthesis_group + depends_on |
| 중단 후 복구 | 사용자가 다시 지시 | **자동 재개** | recovery job + DB 원장 |
| 인터럽트 | 프로세스 재시작 시 유실 | **영속 보장** | Redis + DB 백업 |
| 맥락 캐시 | 매번 전체 재구성 | **hot digest 캐시** | Redis 캐시 + TTL 무효화 |

---

## 3. 시스템 아키텍처

### 3.1 전체 흐름도

```text
CEO 질문 입력
  │
  ▼
┌─────────────────────────────────────────────────┐
│  Chat Turn Gateway (< 500ms)                     │
│  ① turn_sequence 발급                            │
│  ② user_message DB 저장                          │
│  ③ idempotency_key 생성                          │
│  ④ 입력창 즉시 재활성화 (SSE: turn_accepted)      │
│  ⑤ 질문 유형 분류 (simple/status/code/report/dep) │
└──────────┬──────────────────────────────────────┘
           │
     ┌─────┴─────┐
     ▼           ▼
┌─────────┐ ┌──────────────────────────────────────┐
│ Simple  │ │ Complex (심층 필요)                    │
│ 즉답    │ │                                       │
│ 완료    │ │  ┌─ Immediate Response (< 3초) ──┐    │
│         │ │  │ Haiku 4.5 + hot digest        │    │
│         │ │  │ "질문 이해 + 처리 계획" 즉답   │    │
│         │ │  │ SSE: immediate_done            │    │
│         │ │  └───────────┬───────────────────┘    │
│         │ │              ▼                        │
│         │ │  ┌─ Deep Work Job ──────────────┐    │
│         │ │  │ Sonnet 5 / Opus 5            │    │
│         │ │  │ 도구/DB/코드/러너 실행        │    │
│         │ │  │ step checkpoint 저장          │    │
│         │ │  │ SSE: deep_step_update         │    │
│         │ │  └───────────┬───────────────────┘    │
│         │ │              ▼                        │
│         │ │  ┌─ Response Coordinator ───────┐    │
│         │ │  │ 결과 → 질문별 귀속           │    │
│         │ │  │ 순서 역전 정리               │    │
│         │ │  │ 종합보고 자동 생성            │    │
│         │ │  │ SSE: deep_result_ready        │    │
│         │ │  └──────────────────────────────┘    │
│         │ │                                       │
└─────────┘ └──────────────────────────────────────┘
```

### 3.2 질문 유형 자동 분류

Gateway에서 질문을 받는 즉시, **규칙 기반 + 경량 LLM(Haiku)** 2단계로 분류한다.

| 유형 | 판정 기준 | 처리 경로 | 예시 |
|---|---|---|---|
| `simple` | 인사, 확인, 단답 | 즉답만 (심층 불필요) | "알겠어", "OK", "뭐하고 있어?" |
| `status` | 상태/현황 질문 | 즉답 + DB/서버 조회 Job | "서버 상태 확인해", "러너 진행상황" |
| `analysis` | 분석/보고 요청 | 즉답 + 분석 Job | "원인 파악하고 보고해", "비용 분석" |
| `code_modify` | 코드 수정/개발 | 즉답 + Runner/직접수정 Job | "버그 수정해", "기능 추가해" |
| `deploy` | 배포/재시작 | 즉답 + 배포 Job (CEO 승인 게이트) | "배포해줘", "리빌드" |
| `dependent` | 이전 질문 후속 | 즉답 + depends_on 연결 | "이어서", "그거 종합해줘" |
| `multi_part` | 복합 요청 | 즉답 + 하위 Job 여러 개 분리 | "A 하고, B도 확인하고, 보고해" |

**1차 판정 (규칙, 0ms)**:
```python
SIMPLE_PATTERNS = ["알겠", "OK", "ㅇㅇ", "넵", "고마워", "수고"]
DEPENDENT_PATTERNS = ["이어서", "그거", "아까", "종합", "전체", "같이"]
CODE_PATTERNS = ["수정", "패치", "구현", "추가해", "삭제해", "리팩"]
DEPLOY_PATTERNS = ["배포", "리빌드", "재시작", "deploy"]
```

**2차 판정 (Haiku, ~0.5초)**: 규칙으로 확정 못 하면 Haiku에 질문 + 최근 3턴 요약을 보내 유형과 depends_on 판정.

---

## 4. 즉답 엔진 — 3초 SLA 달성 경로

### 4.1 Latency Budget

5초가 아니라 **3초**를 목표로 잡아야 p95에서 5초를 지킨다.

| 구간 | 할당 | 수단 | 실패 시 |
|---|---|---|---|
| Gateway (저장+분류) | **300ms** | DB INSERT + 규칙 분류 | 규칙만 사용 (0ms) |
| Context 조회 | **400ms** | Redis hot digest GET | miss 시 축약 프롬프트 사용 |
| LLM 즉답 호출 | **1,500ms** | **Haiku 4.5** (first-token ~300ms, 200토큰 생성 ~1초) | 1,500ms timeout → 기본 메시지 |
| SSE 전송 | **100ms** | 기존 SSE 경로 | — |
| 여유분 | **700ms** | 네트워크/GC/DB jitter 흡수 | — |
| **합계** | **3,000ms** | — | 초과 시 "처리 중" 기본 즉답 |

### 4.2 즉답 프롬프트 설계

즉답은 **system prompt 1,000토큰 이하 + user context 500토큰 이하**로 제한한다.

```text
[System — 즉답 전용, ~800토큰]
너는 OHVIS AI 비서다. CEO의 질문에 3초 안에 즉답을 제공한다.
역할: 질문을 이해하고, 어떤 경로로 처리할지 안내한다.
규칙:
- 단순 질문은 바로 답변을 완성한다.
- 복잡한 질문은 "무엇을 확인/수정/분석할 것인지"를 1~3줄로 요약한다.
- "확인하겠습니다" 같은 빈 약속은 하지 않는다.
- 심층 작업이 필요하면 "→ 심층 작업 시작: [작업 설명]"으로 끝낸다.

[User Context — hot digest, ~500토큰]
프로젝트: {project_name}
최근 3턴 요약: {recent_turns_digest}
현재 진행 중 작업: {active_jobs_summary}
미완료 TODO: {pending_todos}

[User Question]
{user_message}
```

### 4.3 Hot Digest Cache 구조

| 캐시 키 | 내용 | 크기 | TTL | 무효화 |
|---|---|---|---|---|
| `hot:{session_id}:turns` | 최근 5턴 요약 (role + 핵심 1줄씩) | ~300토큰 | 없음 | 새 턴 저장 시 갱신 |
| `hot:{session_id}:jobs` | 활성 Job 목록 (id + 유형 + 상태) | ~100토큰 | 없음 | Job 상태 변경 시 갱신 |
| `hot:{session_id}:todos` | 미완료 TODO 목록 | ~100토큰 | 없음 | TODO 변경 시 갱신 |
| `hot:{workspace}:identity` | 프로젝트 정체성 1줄 + 핵심 규칙 5줄 | ~200토큰 | 1시간 | prompt_asset 변경 시 무효화 |

**캐시 저장소**: Redis (이미 aads-redis 컨테이너 존재)
**갱신 방식**: 이벤트 드리븐 — 메시지 저장/Job 변경/TODO 변경 시 해당 키만 갱신
**miss 처리**: cache miss 시 DB에서 조회 후 Redis에 저장, 즉답은 축약 프롬프트로 진행

### 4.4 타임아웃 보호

```python
async def generate_immediate_response(session_id, question, hot_digest):
    try:
        result = await asyncio.wait_for(
            call_haiku_immediate(question, hot_digest),
            timeout=2.5  # LLM 호출 타임아웃
        )
        return result
    except asyncio.TimeoutError:
        return ImmediateResponse(
            text=f"질문을 접수했습니다. 심층 분석을 시작합니다.",
            timeout_reason="haiku_timeout",
            deep_job_needed=True
        )
```

**3초 hard timeout**: Gateway 500ms + LLM 2.5초. 초과 시 기본 메시지로 100% SLA 보장.

---

## 5. 다중 질문 병렬 처리 시스템

### 5.1 동시 질문 처리 아키텍처

```text
Q#1 (서버 상태)  ──→ Job#1 (status 조회)     ──→ 완료 → Q#1에 귀속
Q#2 (코드 수정)  ──→ Job#2 (Runner 제출)     ──→ 진행중...
Q#3 (종합 보고)  ──→ Job#3 (depends_on [1,2]) ──→ 대기중 (Q#1,Q#2 완료 대기)
                                                    ↓ (Q#1,Q#2 완료)
                                                  ──→ 종합보고 생성 → Q#3에 귀속
```

### 5.2 Job 동시성 제어

| 제한 | 값 | 이유 |
|---|---|---|
| 세션당 동시 Deep Job | **최대 5개** | LLM API rate limit + DB 부하 제한 |
| 세션당 동시 Runner Job | **최대 2개** | Runner 슬롯 경합 방지 |
| 전체 시스템 동시 Deep Job | **최대 20개** | 서버68 리소스 제한 |
| Job 최대 실행 시간 | **30분** (started_at 기준) | stale 방지 — 기존 heartbeat 버그 근절 |

### 5.3 우선순위 정책

```python
class JobPriority:
    URGENT = 0      # CEO가 "먼저", "즉시" 명시
    NORMAL = 5      # 일반 질문
    BACKGROUND = 10 # "나중에", "시간 되면"
    SYNTHESIS = 15  # 종합보고 (의존 완료 후 자동)
```

CEO가 "이전 건 멈추고 이거 먼저"라고 하면:
1. 기존 Job → `paused_by_priority` 상태 저장 (부분 결과 보존)
2. 새 질문 → priority=URGENT로 즉시 슬롯 할당
3. 새 질문 완료 후 → 일시정지 Job 자동 재개 (옵션)

---

## 6. 심층 작업 파이프라인

### 6.1 작업 유형별 파이프라인

#### 분석/보고 파이프라인
```text
[context_load] → [tool_execute] → [synthesize] → [validate] → [deliver]
   맥락 로드      도구/DB 조회      결과 취합       검증         최종보고
   ~1초           ~5~30초          ~3~10초        ~2~5초       ~0.5초
```

#### 코드 수정 파이프라인
```text
[context_load] → [code_read] → [code_modify] → [test] → [commit] → [deliver]
   맥락 로드      대상 파일 읽기   수정 적용      테스트    커밋      보고
   ~1초           ~2초           ~5~15초        ~5~30초   ~2초     ~0.5초
```

#### 배포 파이프라인 (CEO 승인 게이트 포함)
```text
[context_load] → [preflight] → [CEO_APPROVE] → [deploy] → [healthcheck] → [deliver]
   맥락 로드      사전 점검      CEO 승인 대기    배포 실행    헬스체크      보고
   ~1초           ~3초          ∞ (대기)        ~30~120초   ~10초        ~0.5초
```

### 6.2 Step Checkpoint (중단 복구의 핵심)

각 단계 시작/완료 시 DB에 checkpoint를 저장한다.

```python
async def run_deep_job(job_id, steps):
    for step in steps:
        # 이미 완료된 step은 건너뛰기 (재개 시)
        existing = await get_step_status(job_id, step.key)
        if existing and existing.status == 'completed':
            continue

        await save_step_start(job_id, step.key)
        try:
            result = await step.execute()
            await save_step_complete(job_id, step.key, result)
            await emit_sse('deep_step_update', {
                'job_id': job_id,
                'step': step.key,
                'status': 'completed',
                'progress': f'{step.index}/{len(steps)}'
            })
        except RecoverableError as e:
            await save_step_failed(job_id, step.key, e)
            await schedule_recovery(job_id, retry_after=30)
            return
```

**복구 정책**:

| 실패 유형 | 재시도 | backoff | 최대 횟수 | 최종 실패 처리 |
|---|---|---|---|---|
| LLM rate limit / quota | 자동 | 30초 → 60초 → 120초 | 3회 | 모델 폴백 (Sonnet → Haiku → Gemini) |
| 도구 오류 (DB timeout 등) | 자동 | 10초 → 30초 | 3회 | 대안 도구 시도 후 사용자 알림 |
| 네트워크 중단 | 자동 | 15초 → 30초 | 5회 | 사용자에게 "네트워크 복구 대기" 표시 |
| 사용자 중단 | 중단 유지 | — | — | 부분 결과 draft 보존, "재개하시겠습니까?" |
| 30분 초과 stale | 강제 종료 | — | — | 부분 결과 + "시간 초과" 알림 |

---

## 7. Response Coordinator — 순서 역전 해결

### 7.1 결과 귀속 알고리즘

```python
async def deliver_result(job_id, result):
    job = await get_job(job_id)
    turn = await get_turn(job.turn_sequence)

    # 1. 결과를 해당 질문에 귀속
    final_message = await save_assistant_message(
        session_id=turn.session_id,
        content=result.text,
        parent_turn_sequence=turn.turn_sequence,
        message_type='deep_result'
    )

    # 2. 현재 사용자가 다른 질문을 하고 있는지 확인
    latest_turn = await get_latest_turn(turn.session_id)

    if latest_turn.turn_sequence == turn.turn_sequence:
        # 현재 질문의 결과 → 바로 표시
        await emit_sse('deep_result_ready', {
            'turn_sequence': turn.turn_sequence,
            'display': 'inline'
        })
    else:
        # 과거 질문의 결과 → 업데이트 칩으로 알림
        await emit_sse('deep_result_ready', {
            'turn_sequence': turn.turn_sequence,
            'display': 'chip',
            'chip_text': f'Q#{turn.turn_sequence} 결과 업데이트됨'
        })

    # 3. 종합보고 대기 중인 Job이 있으면 의존성 체크
    await check_synthesis_dependencies(turn.session_id)
```

### 7.2 종합보고 자동 생성

```python
async def check_synthesis_dependencies(session_id):
    pending = await get_pending_synthesis(session_id)
    for syn in pending:
        deps = syn.depends_on_turns  # [turn#1, turn#2]
        all_done = all(
            await is_turn_completed(session_id, t) for t in deps
        )
        if all_done:
            # 의존 질문 모두 완료 → 종합보고 Job 생성
            dep_results = [await get_turn_result(session_id, t) for t in deps]
            await create_synthesis_job(
                session_id=session_id,
                synthesis_group_id=syn.group_id,
                source_results=dep_results,
                model='claude-sonnet-5'  # 종합보고는 Sonnet
            )
```

### 7.3 화면 표시 정책

| 상황 | 화면 처리 | SSE 이벤트 |
|---|---|---|
| 현재 질문 즉답 | 질문 바로 아래 AI 버블 | `immediate_done` |
| 현재 질문 심층 결과 | 즉답 버블을 최종 결과로 교체/확장 | `deep_result_ready` (display: inline) |
| 과거 질문 결과 도착 | 해당 Q# 위치 업데이트 + 하단 칩 | `deep_result_ready` (display: chip) |
| 종합보고 완료 | 별도 "종합보고" 버블 생성 | `synthesis_done` |
| Job 실패 | 해당 Q# 위치에 오류 표시 | `deep_job_failed` |
| Job 복구 중 | 해당 Q# 위치에 "재시도 중" 표시 | `recovery_scheduled` |

---

## 8. 프론트엔드 설계

### 8.1 상태 머신 (질문 단위)

```text
                    ┌──────────────┐
                    │   accepted   │ ← Gateway 저장 완료
                    └──────┬───────┘
                           ▼
                 ┌──────────────────┐
                 │ immediate_started│
                 └──────┬───────────┘
                        ▼
              ┌──────────────────┐
              │ immediate_done   │
              └───┬──────────┬───┘
                  ▼          ▼
         ┌────────────┐  ┌──────────────┐
         │  completed  │  │ deep_running │ ← 심층 필요
         │  (단순질문) │  └──────┬───────┘
         └────────────┘         ▼
                        ┌──────────────┐
                        │ deep_step_*  │ ← 단계별 갱신
                        └──────┬───────┘
                               ▼
                      ┌─────────────────┐
                      │ deep_completed  │
                      └──────┬──────────┘
                             ▼
                    ┌────────────────────┐
                    │ synthesis_waiting? │ ← 종합보고 필요 시
                    └────────┬───────────┘
                             ▼
                    ┌────────────────────┐
                    │ synthesis_done     │
                    └────────────────────┘
```

### 8.2 React 훅 인터페이스

```typescript
// useTurnState.ts — 질문 단위 상태 관리
interface TurnState {
  turnSequence: number;
  status: 'accepted' | 'immediate_started' | 'immediate_done'
        | 'deep_running' | 'deep_completed' | 'synthesis_waiting'
        | 'synthesis_done' | 'failed' | 'paused';
  immediateResponse: string | null;
  immediateMs: number | null;
  deepJobs: DeepJob[];
  finalResult: string | null;
  finalMs: number | null;
  dependsOn: number[];        // 의존 질문 turn_sequence 목록
  synthesisGroupId: string | null;
}

interface DeepJob {
  jobId: string;
  type: 'status' | 'analysis' | 'code_modify' | 'deploy' | 'synthesis';
  status: 'queued' | 'running' | 'completed' | 'failed' | 'paused';
  steps: StepInfo[];
  currentStep: string;
  progress: string;           // "2/5"
}

// useSessionJobs.ts — 세션 전체 작업 현황
interface SessionJobsState {
  activeJobs: number;
  completedJobs: number;
  pendingSynthesis: number;
  recentUpdates: UpdateChip[];  // "Q#3 결과 업데이트됨" 칩 목록
}
```

### 8.3 화면 레이아웃

```text
┌─────────────────────────────────────────────────────┐
│ 세션 상태바: 백그라운드 2건 진행 · 입력 가능           │
├─────────────────────────────────┬───────────────────┤
│                                 │  작업 패널 (접이식) │
│  채팅 영역                       │                   │
│                                 │  Q#218 코드 수정   │
│  [CEO] 서버 상태 확인해          │    ▓▓▓▓▓░ 3/5    │
│  [AI 즉답] 서버68 상태를 조회    │                   │
│    합니다. (즉답 1.2초)           │  Q#219 DB 조회    │
│  [AI 최종] ✅ 전체 정상           │    ✅ 완료 12.3초  │
│    (최종 8.4초 · 도구 3개)        │                   │
│                                 │  Q#220 종합보고    │
│  [CEO] 코드 수정해              │    ⏳ Q#218 대기   │
│  [AI 즉답] chat_service.py의    │                   │
│    L7906 부근을 수정합니다.       │                   │
│    → 심층 작업 시작              │                   │
│    (즉답 2.1초 · 심층 진행중)     │                   │
│                                 │                   │
│  ┌─ 업데이트 칩 ──────────────┐  │                   │
│  │ Q#218 결과 업데이트됨 ▲    │  │                   │
│  └────────────────────────────┘  │                   │
│                                 │                   │
│  [입력창 — 항상 활성] ___________│                   │
├─────────────────────────────────┴───────────────────┤
│  모바일: 작업 패널은 상단 뱃지 + 터치 시 시트로 표시    │
└─────────────────────────────────────────────────────┘
```

**모바일 대응**: 우측 패널 대신 세션 상태바에 뱃지(`진행중 2`)를 표시하고, 터치하면 bottom sheet로 작업 목록을 표시한다.

---

## 9. 모듈 분리 계획

### 9.1 현재 → 목표 파일 구조

```text
현재:
  app/services/chat_service.py  ← 11,416줄, 모든 로직

목표:
  app/services/
    chat_service.py            ← 기존 (점진 축소, feature flag 분기점)
    chat_turn_gateway.py       ← turn_sequence 발급, 질문 저장, 유형 분류
    chat_immediate.py          ← 즉답 엔진 (Haiku 호출, hot cache, timeout)
    chat_deep_work.py          ← 심층 Job 관리, step checkpoint, recovery
    chat_response_coordinator.py ← 결과 귀속, 순서 역전, 종합보고
    chat_context_cache.py      ← Redis hot digest CRUD, TTL, 무효화
  app/core/
    interrupt_queue.py         ← Redis-backed (기존 인메모리 → Redis+DB)
  app/models/
    turn_models.py             ← TurnSequence, ResponseJob, ResponseStep 등
```

### 9.2 추출 순서 (의존성 기준)

| 순서 | 모듈 | 의존 | 이유 |
|---|---|---|---|
| 1 | `turn_models.py` | 없음 | 데이터 모델 먼저 정의 |
| 2 | `chat_context_cache.py` | Redis | 독립 모듈, 다른 모듈이 사용 |
| 3 | `chat_turn_gateway.py` | models, cache | 진입점, 분류 로직 |
| 4 | `chat_immediate.py` | gateway, cache | 즉답 엔진 |
| 5 | `chat_deep_work.py` | models | 심층 Job 관리 |
| 6 | `chat_response_coordinator.py` | models, deep_work | 결과 취합 |
| 7 | `interrupt_queue.py` 전환 | Redis | 기존 코드 교체 |

---

## 10. 데이터 모델 (DDL)

```sql
-- ① 질문 순번 원장
CREATE TABLE chat_turn_sequences (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    turn_sequence INTEGER NOT NULL,
    user_message_id UUID REFERENCES chat_messages(id),
    question_type VARCHAR(20) DEFAULT 'simple',
    parent_turn_sequence INTEGER,
    depends_on INTEGER[],
    priority SMALLINT DEFAULT 5,
    status VARCHAR(30) DEFAULT 'accepted',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (session_id, turn_sequence)
);
CREATE INDEX idx_cts_session_status ON chat_turn_sequences(session_id, status);
CREATE INDEX idx_cts_session_latest ON chat_turn_sequences(session_id, turn_sequence DESC);

-- ② 즉답/심층 작업 원장
CREATE TABLE chat_response_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL,
    turn_sequence INTEGER NOT NULL,
    execution_id UUID,
    job_type VARCHAR(20) NOT NULL,  -- 'immediate', 'deep_status', 'deep_analysis', 'deep_code', 'deep_deploy', 'synthesis'
    model_used VARCHAR(50),
    status VARCHAR(30) DEFAULT 'queued',
    priority SMALLINT DEFAULT 5,
    idempotency_key VARCHAR(64),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    error_category VARCHAR(30),
    retry_count SMALLINT DEFAULT 0,
    max_retries SMALLINT DEFAULT 3,
    result_message_id UUID REFERENCES chat_messages(id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    FOREIGN KEY (session_id, turn_sequence) REFERENCES chat_turn_sequences(session_id, turn_sequence)
);
CREATE INDEX idx_crj_session_active ON chat_response_jobs(session_id, status) WHERE status IN ('queued', 'running');
CREATE INDEX idx_crj_stale ON chat_response_jobs(started_at) WHERE status = 'running';

-- ③ 단계별 진행 상태
CREATE TABLE chat_response_steps (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id UUID NOT NULL REFERENCES chat_response_jobs(id) ON DELETE CASCADE,
    step_key VARCHAR(50) NOT NULL,
    step_index SMALLINT NOT NULL,
    status VARCHAR(20) DEFAULT 'pending',
    progress_json JSONB,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    duration_ms INTEGER,
    error_message TEXT,
    UNIQUE (job_id, step_key)
);
CREATE INDEX idx_crs_job_active ON chat_response_steps(job_id) WHERE completed_at IS NULL;

-- ④ 맥락 캐시 (Redis 주력, DB는 백업/감사)
CREATE TABLE chat_context_cache (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL,
    cache_type VARCHAR(30) NOT NULL,  -- 'turns', 'jobs', 'todos', 'workspace'
    cache_key VARCHAR(100) NOT NULL,
    content TEXT NOT NULL,
    token_count INTEGER,
    source_revision VARCHAR(64),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ,
    UNIQUE (session_id, cache_type)
);

-- ⑤ 종합보고 취합
CREATE TABLE chat_response_synthesis (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL,
    synthesis_group_id VARCHAR(64) NOT NULL UNIQUE,
    depends_on_turns INTEGER[] NOT NULL,
    status VARCHAR(20) DEFAULT 'waiting',
    trigger_turn_sequence INTEGER,
    final_message_id UUID REFERENCES chat_messages(id),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);
CREATE INDEX idx_crsyn_session_waiting ON chat_response_synthesis(session_id) WHERE status = 'waiting';

-- ⑥ 내구 인터럽트 큐
CREATE TABLE chat_interrupts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL,
    execution_id UUID,
    content TEXT NOT NULL,
    attachments JSONB DEFAULT '[]',
    status VARCHAR(20) DEFAULT 'pending',
    idempotency_key VARCHAR(64),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    consumed_at TIMESTAMPTZ
);
CREATE INDEX idx_ci_session_pending ON chat_interrupts(session_id) WHERE status = 'pending';
```

---

## 11. 점진적 마이그레이션 전략

### Phase A: 기반 (1~2일, 위험 없음)

| 작업 | 영향 | 롤백 |
|---|---|---|
| 6개 테이블 additive migration | 기존 코드 무관 | DROP TABLE |
| `chat_context_cache.py` 모듈 작성 | 기존 코드 무관 | 파일 삭제 |
| `turn_models.py` 작성 | 기존 코드 무관 | 파일 삭제 |
| Redis hot digest 캐시 로직 | 기존 코드 무관 | 무시 가능 |

### Phase B: 즉답 레이어 (2~3일, Feature Flag 보호)

| 작업 | Feature Flag | 롤백 |
|---|---|---|
| `chat_turn_gateway.py` 작성 | — | 파일 삭제 |
| `chat_immediate.py` 작성 | — | 파일 삭제 |
| `send_message_stream()` 진입부에 flag 분기 추가 | `OHVIS_IMMEDIATE_RESPONSE_ENABLED` | flag=false → 기존 경로 |
| 대시보드에 즉답 표시 UI 추가 | 같은 flag 체크 | flag=false → 기존 UI |

```python
# chat_service.py — send_message_stream() 진입부
async def send_message_stream(session_id, ...):
    if await is_feature_enabled('OHVIS_IMMEDIATE_RESPONSE', session_id):
        # 새 경로: Gateway → 즉답 → 심층 Job
        async for chunk in new_immediate_deep_flow(session_id, ...):
            yield chunk
    else:
        # 기존 경로: 변경 없음
        async for chunk in legacy_stream_flow(session_id, ...):
            yield chunk
```

**Feature Flag 저장**: `chat_sessions.feature_flags JSONB` 컬럼 또는 별도 `feature_flags` 테이블. 세션별/워크스페이스별/전역 3단계 적용.

### Phase C: Canary (1~2일)

| 작업 | 대상 | 검증 기준 |
|---|---|---|
| CEO 메인 세션 1개에서 flag ON | `session_id = 'ac5278a7...'` | 즉답 p95 < 5초 |
| 48시간 모니터링 | 해당 세션만 | 기존 기능 회귀 0건 |
| 즉답/심층 latency 데이터 수집 | 해당 세션만 | quality_details 정상 저장 |

### Phase D: 전체 확대 (1일)

| 작업 | 영향 | 롤백 |
|---|---|---|
| 전체 세션 flag ON | 모든 세션 | flag OFF (즉시) |
| 기존 경로 코드 제거 (선택) | chat_service.py 축소 | git revert |

### Phase E: 심층 확장 (3~5일)

| 작업 | 내용 |
|---|---|
| `chat_deep_work.py` 작성 | step checkpoint, recovery |
| `chat_response_coordinator.py` 작성 | 결과 귀속, 종합보고 |
| `interrupt_queue.py` Redis 전환 | 영속성 보장 |
| 우측 작업 패널 UI | 대시보드 확장 |

---

## 12. 모델 라우팅 전략

| 용도 | 모델 | 이유 | 비용/턴 |
|---|---|---|---|
| 질문 유형 분류 (2차) | `claude-haiku-4-5` | 빠름, 저비용, 분류 정확도 충분 | ~$0.00005 |
| 즉답 생성 | `claude-haiku-4-5` | first-token ~300ms, 200토큰 1초 | ~$0.0001 |
| 심층 분석/보고 | `claude-sonnet-5` | 품질+속도 균형 | ~$0.003 |
| 고위험 의사결정/아키텍처 | `claude-opus-5` | 최고 품질 필요 시만 | ~$0.015 |
| 종합보고 | `claude-sonnet-5` | 여러 결과 취합, 품질 필요 | ~$0.005 |
| 폴백 (Anthropic 장애) | Gemini via LiteLLM | R-AUTH 폴백 체인 | 별도 |

**비용 영향**: 즉답(Haiku) 추가로 턴당 ~$0.00015 증가. 월 500턴 기준 ~$0.075/월 추가 — **무시 가능**.

---

## 13. 기존 버그 근절 계획

이 아키텍처 전환 시 기존 P0 버그를 함께 해결한다.

| 기존 버그 | 근본 원인 | 새 아키텍처에서 해결 방법 |
|---|---|---|
| heartbeat pump `updated_at` 루프 → stale watchdog 무력화 | 10초마다 무조건 UPDATE | **제거**: 심층 Job은 `chat_response_steps` 단계별 상태로 liveness 판정. `started_at` + 30분 절대 타임아웃 |
| streaming_placeholder 잔여 | 스트림 중단 시 정리 실패 | **분리**: 즉답 placeholder(TTL 10초) + 심층 결과는 Job 완료/실패 시 자동 정리 |
| 이전 답변 하단 재등장 | finalization 타이밍 + execution_id mismatch | **근절**: turn_sequence 기반 귀속. 모든 AI 응답은 parent_turn_sequence를 가짐 |
| 인터럽트 유실 | 인메모리 dict, 프로세스 재시작 시 소멸 | **Redis + DB**: push는 Redis RPUSH + 비동기 DB INSERT, 재시작 시 DB에서 복원 |

---

## 14. 테스트 전략

| 단계 | 테스트 유형 | 대상 | 방법 | 완료 기준 |
|---|---|---|---|---|
| Phase A | 단위 테스트 | turn_models, context_cache | pytest | 커버리지 80%+ |
| Phase B | 통합 테스트 | Gateway → 즉답 → SSE | 실제 DB + mock LLM | 즉답 p95 < 3초 |
| Phase B | 회귀 테스트 | 기존 채팅 (flag OFF) | 기존 test suite | 전체 PASS |
| Phase C | E2E 테스트 | CEO 세션 canary | 실제 사용 48시간 | 장애 0건 |
| Phase D | 부하 테스트 | 동시 5세션 × 3질문 | k6/locust | 응답 시간 SLA 유지 |
| Phase E | 순서 역전 테스트 | Q#1~3 역순 완료 시나리오 | Playwright 시나리오 | UI 정합성 |

---

## 15. 구현 로드맵

| Phase | 기간 | 산출물 | 검증 | 위험도 |
|---|---|---|---|---|
| **A: 기반** | 1~2일 | DB 6테이블, models, cache 모듈 | migration 성공, 기존 영향 0 | 🟢 낮음 |
| **B: 즉답** | 2~3일 | Gateway, Immediate 모듈, flag 분기, 대시보드 즉답 UI | flag OFF 회귀 PASS, flag ON 즉답 p95 < 3초 | 🟡 중간 |
| **C: Canary** | 1~2일 | CEO 세션 검증 데이터 | 48시간 장애 0건, latency 데이터 확보 | 🟢 낮음 |
| **D: 확대** | 1일 | 전체 세션 ON | 전체 PASS | 🟡 중간 |
| **E: 심층** | 3~5일 | Deep Work, Coordinator, 인터럽트 전환, 작업 패널 | 다중 질문 병렬 처리, 순서 역전 해결 | 🟡 중간 |
| **F: 종합보고** | 2~3일 | Synthesis 모듈, depends_on 추론 | 자동 종합보고 생성 | 🟡 중간 |

**총 예상**: 10~16일 (Phase A~F)
**예상 비용**: Runner 4~6회 × $5~10 = $20~60

---

## 16. 완료 기준

| 기준 | 목표 | 측정 방법 |
|---|---|---|
| 즉답 p95 | **3초 이하** (5초 SLA 여유 포함) | `quality_details.immediate_duration_ms` |
| 즉답 실패율 | **0%** (timeout 시 기본 메시지) | timeout_reason 발생률 |
| 입력창 재활성화 | **즉답 후 즉시** | 프론트 측정 |
| 다중 질문 병렬 | **동시 5개** | 동시 Job 수 모니터링 |
| 순서 역전 혼동 | **0건** | turn_sequence 귀속 검증 |
| 종합보고 자동화 | **depends_on 완료 시 자동** | synthesis 트리거 성공률 |
| 인터럽트 유실 | **0건** | Redis + DB 복원 테스트 |
| stale 30분 초과 | **0건** | started_at 기반 watchdog |
| 기존 기능 회귀 | **0건** | 기존 test suite PASS |

---

## 17. 결론 및 즉시 실행안

기존 기획서 2건의 방향은 맞다. 이 문서는 **"어떻게"를 구체화**한 것이다.

**즉시 실행 순서**:

1. **Phase A 러너 제출** — DB migration + models + cache 모듈 (위험 없음, 1~2일)
2. **Phase B 러너 제출** — 즉답 엔진 + feature flag 분기 (핵심, 2~3일)
3. **Phase C CEO 검증** — canary 48시간
4. 결과 보고 후 Phase D~F 순차 진행

→ Phase A부터 러너로 제출할까요?
