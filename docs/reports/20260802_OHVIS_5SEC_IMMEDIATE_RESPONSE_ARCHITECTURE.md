# OHVIS 5초 즉답·심층 작업·맥락 캐시 아키텍처 기획서

> 작성 시각: 2026-08-02 21:07 KST  
> 대상: AADS/OHVIS CEO 채팅창, 세션별 비동기 작업, SSE 진행 표시, 맥락 캐시, 종합보고 취합  
> 상태: P0 구현 전 기획 확정안  

## 1. 요약

현재 AADS 채팅은 SSE 재연결, Redis Stream, background completion, completion contract, 응답 소요시간 표시는 이미 갖고 있다. 그러나 "5초 이내 즉답"과 "심층 작업의 백그라운드 진행 표시", "1차/2차/3차 질문 완료 순서가 뒤섞일 때 종합 취합"은 별도 제품 구조로 분리되어 있지 않다.

개선 방향은 한 버블에서 모든 일을 끝내려는 구조가 아니라, `즉답 레이어`와 `심층 실행 레이어`를 분리하고, 각 질문을 `turn_sequence + execution_id + background_job_id`로 묶어 화면에서 진행 상태와 최종 취합 결과를 안정적으로 보여주는 것이다.

## 2. 현재 상태

| 항목 | 현재 확인값 | 판정 | 출처 |
|---|---:|---|---|
| 최근 7일 `completed` 실행 | 169건 | 정상 완료는 쌓임 | [DB 조회, 2026-08-02 21:07 KST] |
| 최근 7일 `interrupted` 실행 | 31건 | 중단 체감이 반복될 수 있음 | [DB 조회, 2026-08-02 21:07 KST] |
| 현재 `running` 실행 | 2건 | 30분 초과 stale은 0건 | [DB 조회, 2026-08-02 21:07 KST] |
| 최근 7일 assistant 중 소요시간 보유 | 192/334건 | 표시 데이터화는 부분 적용 | [DB 조회, 2026-08-02 21:07 KST] |
| `streaming_placeholder` | 3건 | 일부 잔여 가능성 존재 | [DB 조회, 2026-08-02 21:07 KST] |
| API/대시보드/DB/Redis 컨테이너 | healthy | 인프라 생존 상태 정상 | [docker ps, 2026-08-02 21:07 KST] |
| `MemoryContextBar` | 존재 | 맥락 표시 UI는 있음 | [코드: `/root/aads/aads-dashboard/src/app/chat/page.tsx`] |
| 응답 소요시간 버블 하단 표시 | 존재 | 완료/진행 시간 표시는 적용됨 | [코드: `/root/aads/aads-dashboard/src/app/chat/page.tsx`] |
| 인터럽트 큐 | 프로세스 메모리 기반 | 재시작/슬롯전환/장기작업에 취약 | [코드: `app/core/interrupt_queue.py`] |

## 3. 현재 문제점

| 문제 | 사용자 체감 | 근본 원인 |
|---|---|---|
| 5초 안에 답변이 시작되지 않는 경우 | 질문 후 기다림, 중단처럼 보임 | LLM 호출, 도구 조회, 코드 검색, 검증을 한 응답 경로에 묶음 |
| 심층 작업 진행상태가 버블 본문에 섞임 | "확인하겠습니다"류 진행문이 반복됨 | 작업 상태와 최종 답변의 UI 채널이 분리되지 않음 |
| 1차/2차/3차 질문 완료 순서 역전 | 이전 답변이 뒤늦게 하단에 나타난 것처럼 보임 | 질문 순번과 background completion 결과를 별도 조정하지 않음 |
| 스트리밍 중 추가 지시 유실 가능성 | "이어서 진행"을 다시 말해야 함 | `interrupt_queue.py`가 인메모리 dict/set 기반 |
| 맥락 파악 비용 증가 | 긴 세션에서 첫 응답 지연 | 매번 깊은 맥락을 새로 구성하려는 경향, hot cache 계층 부족 |
| 개선 데이터 부족 | 어떤 질문이 왜 느린지 사후 개선 어려움 | 단계별 지연, 캐시 hit, 도구 대기, fallback 사유가 표준 메트릭으로 부족 |

## 4. 목표 사용자 경험

### 4.1 기본 원칙

1. 채팅 입력창은 백그라운드 작업 중에도 즉시 입력 가능한 상태를 유지한다.
2. 5초 안에 최소한 "질문 이해 + 현재 맥락 + 처리 방식 + 백그라운드 시작 여부"를 표시한다.
3. 진행상태는 본문 버블이 아니라 버블 하단 상태줄과 우측 작업 패널에 표시한다.
4. 최종 답변은 질문별로 귀속시키고, 오래된 질문 결과가 늦게 도착해도 현재 질문의 하단에 끼어들지 않는다.
5. 여러 질문의 결과가 모이면 별도 `종합보고` 버블 또는 아티팩트로 취합한다.

### 4.2 화면 표시 예시

| 위치 | 표시 내용 | 목적 |
|---|---|---|
| 사용자 질문 버블 하단 | `Q#124 · 접수 0.2초 · 즉답 완료 · 심층 진행중` | 질문 원장 고정 |
| AI 즉답 버블 하단 | `즉답 3.8초 · 맥락캐시 hit · 심층작업 2건 진행중` | 5초 SLA 체감 |
| AI 최종 버블 하단 | `최종 52.4초 · 도구 4개 · 검증 3개 · 비용 $0.018` | 사후 분석 |
| 우측 작업 패널 | `Q#124 코드확인 3/5`, `Q#125 DB조회 완료`, `Q#126 대기` | 병렬 상황 확인 |
| 세션 상단 상태바 | `백그라운드 작업 2건 · 종합보고 대기 1건` | 질문 계속 가능 여부 표시 |

## 5. 목표 아키텍처

```
CEO 입력
  |
  v
Chat Turn Gateway
  - turn_sequence 발급
  - idempotency_key 고정
  - 입력 즉시 DB 저장
  |
  +--> Immediate Response Layer
  |     - 5초 SLA
  |     - hot context cache 조회
  |     - 단문/상태/확인 질문은 즉시 답변
  |     - 심층 필요 시 background_job 생성 후 즉답
  |
  +--> Deep Work Layer
  |     - 도구/DB/코드/검색/러너 실행
  |     - step checkpoint 저장
  |     - completion contract 검증
  |
  +--> Context Cache Layer
  |     - session digest
  |     - workspace prompt digest
  |     - memory recall digest
  |     - active jobs digest
  |
  +--> Response Coordinator
        - 질문별 결과 귀속
        - 완료 순서 역전 정리
        - 종합보고 생성
        - stale/interrupted 자동 복구
```

## 6. 레이어별 설계

### 6.1 Immediate Response Layer

목표는 완성 보고가 아니라 "질문을 이해했고, 어떤 경로로 처리 중인지"를 5초 안에 보장하는 것이다.

| 유형 | 5초 내 동작 | 심층 레이어 사용 |
|---|---|---|
| 단순 대화/상태 질문 | 캐시와 최근 메시지로 직접 답변 | 불필요 |
| 서버/DB 실측 필요 | "즉답: 확인 대상과 기준" 표시 후 작업 시작 | 필요 |
| 코드 수정/배포 | 영향 범위/롤백/실행 방식 즉답 후 러너 또는 직접 작업 | 필요 |
| 긴 분석/보고 | 요약 즉답 후 report job 생성 | 필요 |
| 이전 질문 후속 | 관련 turn/result 링크 후 답변 | 필요 시 |

완료 기준:
- `immediate_response_started_at`, `immediate_response_completed_at` 저장
- p95 즉답 시간 5초 이하
- 즉답 실패 시 `immediate_timeout_reason` 기록

### 6.2 Deep Work Layer

심층 작업은 하나의 긴 LLM 응답이 아니라 단계별 작업으로 쪼갠다.

| 단계 | 예시 상태 | 저장 단위 |
|---|---|---|
| queued | `대기중` | job row |
| context_loaded | `맥락 로드 완료` | context snapshot id |
| tools_running | `DB 조회 2/3` | step row |
| waiting_external | `러너 승인 대기` | external job link |
| synthesizing | `결과 취합 중` | draft artifact |
| validating | `검증 중` | test/check result |
| completed | `최종보고 완료` | final assistant message/artifact |
| failed_recoverable | `자동 재개 대기` | retry policy |

완료 기준:
- 각 step은 idempotency key를 갖고 재실행 시 중복 저장하지 않는다.
- 브라우저가 닫혀도 DB 원장 기준으로 계속 이어진다.
- 실패 시 모델 폴백, 도구 오류, 사용자 중단, 네트워크 중단을 분리 저장한다.

### 6.3 Context Cache Layer

5초 즉답의 핵심은 전체 맥락을 매번 새로 만들지 않는 것이다.

| 캐시 | 내용 | TTL/무효화 |
|---|---|---|
| session_hot_digest | 최근 20턴 요약, 현재 결정, 미완료 TODO | 새 메시지/완료보고 시 갱신 |
| workspace_operating_digest | AADS 프로젝트 정체성, 서버, 배포 규칙 | prompt asset 변경 시 갱신 |
| memory_recall_digest | 선호, 절차 메모리, 반복 에러, 최근 결정 | 60초 TTL + memory write 후 무효화 |
| active_job_digest | 현재 세션/전체 프로젝트 background jobs | job 상태 변경 시 갱신 |
| artifact_digest | 최근 보고서/문서 제목과 요약 | artifact 생성/수정 시 갱신 |

캐시는 LLM 컨텍스트를 대체하지 않는다. 즉답에는 축약 캐시를 쓰고, 심층 레이어는 기존 `context_builder.build_messages_context()`와 메모리 회수를 사용한다.

### 6.4 Response Coordinator

질문 완료 순서가 바뀌어도 화면이 흔들리지 않게 하는 핵심이다.

규칙:
1. 모든 사용자 질문에 `turn_sequence`를 부여한다.
2. 모든 AI 응답은 `parent_turn_sequence` 또는 `reply_to_message_id`를 갖는다.
3. 즉답은 질문 직후 붙는다.
4. 심층 결과가 늦게 도착하면 해당 질문의 "최종 결과"로 귀속한다.
5. 현재 사용자가 다른 질문을 하고 있어도 오래된 결과를 맨 아래에 무작정 새 버블로 삽입하지 않는다.
6. 여러 결과를 묶어야 하면 `synthesis_group_id`로 종합보고를 생성한다.

## 7. 1차/2차/3차 질문 처리 정책

### 7.1 독립 질문

```
Q#1 접수 -> 즉답 A#1a -> 심층 Job#1
Q#2 접수 -> 즉답 A#2a -> 심층 Job#2
Q#3 접수 -> 즉답 A#3a -> 심층 Job#3

완료 순서: Job#2 -> Job#1 -> Job#3
화면 처리:
- Q#2 위치에 최종 A#2b 연결
- Q#1 위치에 최종 A#1b 연결
- Q#3 위치에 최종 A#3b 연결
- 세션 하단에는 "결과 2건 업데이트됨" 요약 칩만 표시
```

### 7.2 의존 질문

사용자가 "그거 이어서", "2번도 같이", "전체 종합해"라고 하면 새 질문은 이전 질문에 의존한다.

```
Q#1 시장 조사
Q#2 경쟁사 비교
Q#3 종합 보고

Q#3 depends_on = [Q#1, Q#2]
Q#3 즉답: "Q#1/Q#2 결과를 기다린 뒤 종합하겠습니다."
Q#1/Q#2 완료 후 Response Coordinator가 Q#3 종합보고 실행
```

### 7.3 사용자 우선순위 변경

사용자가 새 질문에서 "이전 건 중단하고 이걸 먼저"라고 하면:
- 기존 background job은 `paused_by_new_priority` 또는 `cancelled_by_user`로 명확히 저장
- 부분 결과가 있으면 질문별 draft artifact에 보존
- 새 질문이 즉답과 심층 작업 우선권을 가진다

## 8. 데이터 모델 제안

기존 `chat_messages.quality_details`와 `chat_turn_executions`는 유지하고, 단계/캐시/취합 전용 테이블을 추가한다.

| 테이블 | 목적 | 핵심 컬럼 |
|---|---|---|
| `chat_turn_sequences` | 질문 순번/귀속 원장 | `session_id`, `turn_sequence`, `user_message_id`, `status` |
| `chat_response_jobs` | 즉답/심층 작업 원장 | `turn_sequence`, `execution_id`, `job_type`, `status`, `priority` |
| `chat_response_steps` | 단계별 진행 표시 | `job_id`, `step_key`, `status`, `started_at`, `completed_at`, `progress_json` |
| `chat_context_cache` | 맥락 캐시 | `session_id`, `cache_type`, `cache_key`, `content`, `expires_at`, `source_revision` |
| `chat_response_synthesis` | 종합보고 취합 | `synthesis_group_id`, `depends_on_turns`, `status`, `final_message_id` |
| `chat_interrupts` | 내구 인터럽트 큐 | `session_id`, `execution_id`, `content`, `status`, `idempotency_key` |

기존 `quality_details`에는 다음 필드를 표준화한다.

```json
{
  "immediate_duration_ms": 3800,
  "deep_duration_ms": 52400,
  "context_cache_hit": true,
  "tool_wait_ms": 18400,
  "fallback_count": 0,
  "completion_order": 2,
  "parent_turn_sequence": 124,
  "synthesis_group_id": "syn-..."
}
```

## 9. SSE/API 이벤트 제안

| 이벤트 | 의미 | 프론트 처리 |
|---|---|---|
| `turn_accepted` | 질문 저장 및 순번 확정 | 사용자 버블 하단에 Q# 표시 |
| `immediate_started` | 즉답 생성 시작 | AI 즉답 placeholder 표시 |
| `immediate_done` | 5초 응답 완료 | `즉답 N초` 표시 |
| `deep_job_created` | 백그라운드 작업 생성 | 우측 작업 패널에 추가 |
| `deep_step_update` | 단계 진행 | 진행률/도구명/상태 갱신 |
| `deep_result_ready` | 질문별 최종 결과 도착 | 해당 질문에 결과 연결 |
| `synthesis_waiting` | 종합보고 의존 대기 | 하단 칩 표시 |
| `synthesis_done` | 종합보고 완료 | 종합 버블/아티팩트 표시 |
| `recovery_scheduled` | 끊김 자동 재개 예약 | 중단 버블 대신 복구 상태 유지 |
| `fallback_notice` | 모델 폴백 정보 | 본문 버블이 아닌 상태줄로 축소 |

## 10. 개선 시 기대 효과

| 개선 항목 | 현재 | 개선 후 |
|---|---|---|
| 첫 체감 응답 | 완성 응답까지 대기 | 5초 내 즉답 고정 |
| 장기 작업 중 입력 | 세션이 멈춘 것처럼 보임 | 입력 가능 + 작업 패널에서 진행 확인 |
| 이전 답변 재등장 | 완료 순서 역전 시 혼란 | 질문별 귀속/업데이트 칩으로 정리 |
| 중단 후 재개 | 사용자가 다시 지시해야 하는 경우 | recovery job이 자동 이어쓰기 |
| 비용/속도 개선 | 병목 사후 분석 어려움 | 단계별 latency 데이터로 모델/도구 최적화 |
| 맥락 품질 | 긴 세션에서 느림 | hot cache로 즉답, deep context로 품질 유지 |

## 11. 구현 로드맵

### P0: 5초 즉답 원장 + 실시간 진행 표시

범위:
- `chat_turn_sequences`, `chat_response_jobs`, `chat_response_steps` migration
- `send_message_stream()` 앞단에 즉답 경로 추가
- 프론트 버블 하단에 Q# / 즉답 / 심층 상태 표시
- 우측 패널 또는 세션 상단에 background job list 표시
- 모델 폴백 메시지는 본문 버블이 아니라 상태 이벤트로 표시

검증:
- 단순 질문 p95 즉답 5초 이하
- 도구 필요 질문도 5초 내 "즉답+작업 생성" 표시
- 브라우저 새로고침 후 job list 복원

### P1: 맥락 캐시 + 내구 인터럽트

범위:
- `chat_context_cache` 추가
- session/workspace/memory/active-job digest 생성
- `interrupt_queue.py`를 DB-backed `chat_interrupts`로 전환
- 프로세스 재시작 후 pending interrupt 복원

검증:
- 긴 세션에서도 즉답 레이어가 cache hit 기준으로 동작
- 스트리밍 중 추가 지시 후 API 재시작해도 지시가 사라지지 않음

### P2: 순서 역전 취합 + 종합보고 자동 생성

범위:
- `chat_response_synthesis` 추가
- depends_on 질문 자동 추론
- out-of-order completion UI 정책 구현
- 종합보고 아티팩트 생성

검증:
- Q#1/Q#2/Q#3 완료 순서가 뒤섞여도 각 질문 위치와 종합보고가 일관됨
- 오래된 결과가 현재 질문 하단에 갑자기 새 최종답변처럼 보이지 않음

## 12. 완료 기준

| 기준 | 목표 |
|---|---|
| 즉답 p95 | 5초 이하 |
| 즉답 실패율 | 1% 이하 |
| background job 상태 복원 | 새로고침 후 100% |
| stale running 30분 초과 | 0건 유지 |
| interrupt 유실 | 0건 |
| 완료 순서 역전 혼동 | 질문별 Q# 귀속으로 제거 |
| 소요시간 데이터화 | 즉답/심층/도구/검증 단계별 저장 |

## 13. 권장 실행안

즉시 P0를 Pipeline Runner로 실행한다. 변경 범위가 백엔드 migration, 채팅 서비스, SSE 이벤트, 대시보드 UI까지 걸리므로 직접 단일 패치보다 러너 작업으로 묶고, 파일 충돌이 있는 대시보드 기존 dirty 상태를 사전 확인한 뒤 진행해야 한다.

권장 작업명:

```
TASK_ID: AADS-OHVIS-5SEC-IMMEDIATE-RESPONSE-P0-20260802
TITLE: OHVIS 5초 즉답 레이어와 백그라운드 작업 실시간 표시 구현
PRIORITY: P0
SIZE: L
MODEL: gpt-5.6-sol 또는 안정 라우팅 모델
```

승인 전 영향 범위:
- Backend: `app/services/chat_service.py`, `app/routers/chat.py`, 신규 migration
- Frontend: `/root/aads/aads-dashboard/src/app/chat/page.tsx`, 필요 시 컴포넌트 분리
- DB: 신규 테이블 3~6개 추가, 기존 데이터 삭제 없음
- 배포: API + Dashboard blue/green 필요

롤백:
- DB 신규 테이블은 사용 중단 가능하도록 additive migration으로 작성한다.
- 코드 롤백은 단일 커밋 revert 후 API/Dashboard 재배포한다.
- 기존 `chat_messages`, `chat_turn_executions` 데이터는 삭제하지 않는다.
