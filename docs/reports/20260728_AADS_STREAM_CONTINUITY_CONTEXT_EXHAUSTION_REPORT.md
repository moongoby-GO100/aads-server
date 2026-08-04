# AADS 스트리밍 연속성·컨텍스트 소진 정밀 개선 보고서

> 작성 시각: 2026-07-28 06:09 KST  
> 대상: AADS CEO 채팅, Claude Code 기능개선 세션, SSE 스트리밍, blue/green 배포, 추가 지시 반영  
> 결론: 무중단 배포는 상당 부분 반영되어 있다. 그러나 "응답 연속성"은 배포 문제가 아니라 `durable execution`, `DB-backed interrupt`, `context budget guard` 문제까지 포함하므로 별도 P0 개선이 필요하다.

## 1. 요약

CEO가 겪은 "이전 세션이 컨텍스트 소진으로 끊겼습니다"는 서버 재시작과 다르다. 서버 API는 살아 있고 blue/green 배포도 적용되어 있지만, 장시간 Claude Code 기능개선 세션은 모델 컨텍스트 한도, 도구 출력 누적, 중간 지시 큐의 비내구성, LLM 쿼터 오류가 겹치면 응답이 멈춘 것처럼 보인다.

현재 AADS에는 이미 SSE 복구, Last-Event-ID, Redis Stream, 백그라운드 완료, startup resume scanner, completion auto-continue가 있다. 하지만 추가 지시 큐가 프로세스 메모리 기반이고, 실행 단위가 완전한 step checkpoint가 아니며, 컨텍스트 예산이 한계에 닿기 전에 작업을 자동 분할하는 장치가 부족하다. 따라서 개선 방향은 "스트림을 더 오래 붙잡기"가 아니라 "작업을 끊겨도 이어지는 내구 실행으로 바꾸기"다.

## 2. 질문별 직접 답변

| 질문 | 현재 판정 | 설명 |
|---|---|---|
| 배포가 무중단으로 반영된 것 아닌가? | 부분적으로 맞다 | `deploy.sh`는 blue/green 전환, active 슬롯 직접 재시작 회피, old slot drain 후 standby 동기화를 수행한다. |
| 그런데 왜 끊기나? | 배포 외 실패 모드가 있다 | 컨텍스트 한도, provider 429, 브라우저/SSE 단절, 프로세스 재시작, 추가 지시 반영 지연이 모두 같은 "끊김"처럼 보인다. |
| 추가 지시를 바로바로 반영할 수 없나? | 가능하나 현재는 불완전 | 현재는 스트리밍 중 추가 메시지를 인메모리 큐에 넣고 도구 결과 이후 또는 최종 저장 직전 반영한다. 토큰 단위 즉시 반영/재시작 내구성은 부족하다. |
| 컨텍스트 소진은 뭔가? | 모델 작업 메모리 한계다 | 대화, 파일 읽기, 명령 출력, 시스템 프롬프트, 이전 응답이 누적되어 모델이 한 번에 참고할 수 있는 범위를 넘는 상태다. |
| 이대로 둬야 하나? | 아니다 | P0로 DB-backed interrupt와 context budget guard를 넣고, P1로 durable execution step checkpoint까지 확장해야 한다. |

## 3. 현재 AADS 실측 결과

### 3.1 서비스 상태

2026-07-28 06:09 KST 기준 로컬 Docker/health 확인 결과다.

| 항목 | 실측값 | 판정 |
|---|---|---|
| API health | `{"status":"ok","graph_ready":true,"version":"0.2.1"}` | 정상 |
| `aads-server` | Up 21 hours, healthy, `127.0.0.1:8100->8080` | 정상 |
| `aads-server-green` | Up 34 hours, healthy, `127.0.0.1:8102->8080` | 정상 |
| `aads-dashboard` | Up 2 minutes, healthy | 정상 |
| `aads-dashboard-green` | Up 6 minutes, healthy | 정상 |
| `aads-postgres` | Up 5 days, healthy | 정상 |
| `aads-redis` | Up 2 months, healthy | 정상 |
| `aads-litellm` | Up 42 hours, healthy | 정상 |

### 3.2 채팅 실행 원장

DB 조회 결과 `chat_turn_executions`는 다음 상태다.

| status | 건수 | 의미 |
|---|---:|---|
| `completed` | 4,853 | 정상 완료 |
| `interrupted` | 4,832 | 중단/대체/복구/오류로 종료 |
| `running` | 3 | 현재 실행 중 또는 stale 가능 |

중단 사유 상위 12개:

| 중단 사유 | 건수 | 해석 |
|---|---:|---|
| LiteLLM gpt-5 HTTP 429 quota | 2,787 | 모델/쿼터 라우팅 문제. 컨텍스트 소진과 별개로 가장 큰 중단 원인 |
| superseded by newer execution | 746 | CEO가 새 지시를 보내 기존 실행이 대체됨 |
| stale running execution settled by recovery endpoint | 193 | stale 실행을 recovery endpoint가 정리 |
| CancelledError | 133 | 클라이언트/태스크 취소 |
| auto-settled by stale execution watchdog | 118 | watchdog이 stale 실행 정리 |
| client_gone_auto_cancel | 95 | 클라이언트 단절 후 자동 취소 |
| stopped by user | 76 | 사용자 중지 |
| background_producer_incomplete_exit | 55 | 백그라운드 producer 완료 보장 실패 |
| stale_superseded_by_newer_user_message | 51 | 새 사용자 메시지로 이전 실행 중단 |
| assistant message already terminal | 50 | 이미 terminal 메시지로 판정되어 재개 중단 |
| recovery_auto_retry_scheduled | 28 | 자동 재시도 예약 |
| superseded while preserving partial response | 25 | partial 보존 후 대체 |

판정: CEO가 체감한 끊김은 단일 원인이 아니다. 가장 큰 숫자는 429 쿼터이고, 그 다음은 새 지시로 기존 실행을 대체하는 흐름이다. 컨텍스트 소진은 Claude Code 기능개선 세션 쪽의 별도 한계이며, AADS 채팅에서는 긴 프롬프트/도구 출력/문서 읽기 누적으로 비슷한 증상이 생길 수 있다.

### 3.3 남아 있는 스트리밍 잔여물

| 항목 | 건수 | 의미 |
|---|---:|---|
| `streaming_placeholder` | 2 | 진행 중 placeholder 또는 잔여 placeholder |
| `interrupted_partial` | 249 | 중단 시 보존된 부분 응답 |
| `interruption_notice` | 37 | 중단 안내 메시지 |

## 4. 현재 구현된 보호 장치

| 기능 | 파일 경로 | 현재 구현 |
|---|---|---|
| heartbeat | `app/services/chat_service.py:120` | 3초 간격 heartbeat와 padding으로 프록시 idle timeout 회피 |
| background completion | `app/services/chat_service.py:155`, `app/services/chat_service.py:3545` | 클라이언트가 끊겨도 producer는 백그라운드에서 계속 실행 |
| shutdown partial save | `app/services/chat_service.py:4618` | API 종료 전 partial을 DB에 저장하고 execution interrupted 표시 |
| startup/periodic resume scanner | `app/main.py:1118` | 재시작 후 stale running/retrying execution을 claim해 `_resume_single_stream()` 실행 |
| resume owner | `app/main.py:1124` | blue/green inactive 슬롯이 복구 작업을 가져가지 않도록 active container/port 확인 |
| Last-Event-ID resume | `app/routers/chat.py:1880`, `app/routers/chat.py:1894` | Redis Stream 또는 DB fallback으로 끊긴 SSE를 이어붙임 |
| last-response fallback | `app/routers/chat.py:2059` | 최종 저장된 assistant 메시지 조회 |
| 프론트 invisible recovery | `aads-dashboard/src/app/chat/page.tsx:3608` | 네트워크/SSE abort 시 같은 버블 유지 후 재연결/폴링 |
| 추가 지시 큐 | `app/routers/chat.py:1155`, `app/core/interrupt_queue.py:12` | 스트리밍 중 새 메시지를 인메모리 큐에 저장 |
| 추가 지시 반영 | `app/services/chat_service.py:9274` | 메인 스트림 후 큐를 읽어 기존 답변을 대체 재작성 |
| completion auto-continue | `app/services/chat_service.py:9645` | 완료보고 조건 미충족 시 자동 이어쓰기 |
| blue/green 배포 | `deploy.sh:591` | 새 슬롯 빌드/헬스체크 후 upstream 전환 |
| old slot drain | `deploy.sh:655`, `deploy.sh:708` | active stream 대기 및 old slot standby 동기화 지연 |

## 5. 현재 구조의 한계

### 5.1 무중단 배포와 세션 연속성은 다른 문제다

`deploy.sh`는 active 서버를 바로 죽이지 않고 새 슬롯으로 전환한다. nginx reload도 기존 worker가 들고 있는 연결을 보존하도록 설계되어 있다. 그러나 이것은 HTTP/SSE 연결 보호다. Claude Code 기능개선 세션의 컨텍스트 소진은 모델 작업 메모리 문제라 blue/green으로 해결되지 않는다.

### 5.2 추가 지시 큐가 내구적이지 않다

`app/core/interrupt_queue.py`는 `_interrupt_queues: dict`에 저장한다. 따라서 같은 Python 프로세스 안에서는 빠르지만, 다음 경우에는 약하다.

- API 프로세스 재시작
- blue/green 슬롯 전환 중 다른 슬롯에서 이어받는 경우
- 장시간 작업이 Claude Code 컨텍스트 소진으로 종료되는 경우
- 스트림이 도구 호출 없이 긴 텍스트를 생성하고 있어 interrupt check 지점이 늦는 경우

현재는 `chat_service.py:9274` 이후 최종 저장 직전에 최대 2패스 반영한다. "바로바로"가 아니라 "가능한 지점에서 사후 재작성"에 가깝다.

### 5.3 실행이 step checkpoint가 아니다

`chat_turn_executions`는 턴 단위 원장은 있지만, 내부 실행 단계가 완전히 쪼개져 있지는 않다. 예를 들어 "자료검색 -> 코드확인 -> 문서작성 -> 검증 -> 보고" 중 어디까지 완료됐는지 step별 idempotency key로 고정하지 않으면, 재개 시 전체를 다시 하거나 partial 기반 재생성을 하게 된다.

### 5.4 컨텍스트 예산 사전 차단이 부족하다

현재 AADS는 memory/context 압축과 completion contract가 있지만, 기능개선 세션이 길어질 때 다음 보호가 더 필요하다.

- 도구 출력 길이 예산 초과 전 자동 요약
- 읽은 파일 목록과 핵심 근거만 checkpoint에 저장
- 대화가 일정 토큰을 넘기기 전 자동 handover 생성
- 장기 작업은 채팅 응답 안에서 계속하지 않고 runner/job으로 분리

### 5.5 모델/쿼터 오류가 끊김처럼 보인다

DB상 중단 사유 1위는 `LiteLLM gpt-5 HTTP 429 quota`다. 이 경우 서버나 SSE 문제가 아니라 라우팅/쿼터 문제다. CEO에게는 "응답이 끊겼다"로 보이므로, 모델 fallback과 사용자 표시를 분리해야 한다.

## 6. 최신 자료 기반 설계 원칙

| 자료 | 핵심 내용 | AADS 적용 |
|---|---|---|
| WHATWG HTML SSE | SSE는 `id`와 `Last-Event-ID`로 재연결 위치를 전달할 수 있다. | AADS의 Redis Stream/Last-Event-ID는 방향이 맞다. 단, 연결 복구만 담당한다. |
| MDN EventSource | `EventSource`는 `text/event-stream` 연결을 열고, 이벤트 id/retry를 처리한다. | 프론트는 마지막 event id와 retry 정책을 명시적으로 관리해야 한다. |
| OpenAI Responses conversation state | `previous_response_id`, prior output 재전송, Conversations API 같은 상태 관리 옵션이 있다. | provider 상태에만 의존하지 말고 AADS DB 원장을 primary로 둔다. |
| Anthropic compaction docs | 긴 대화는 context window 한계 전에 압축해 effective context를 늘린다. | 자동 handover/compact artifact를 실행 단위마다 생성해야 한다. |
| Claude Code context window docs | 세션의 지시, 파일 읽기, 응답, 숨은 컨텍스트가 모두 context window를 차지한다. | 장시간 코드 세션은 subtask와 file evidence packet으로 분할해야 한다. |
| LangGraph persistence | checkpointer는 thread-scoped 상태, store는 장기 메모리에 쓴다. | `chat_turn_executions`를 step checkpoint + store 구조로 확장한다. |
| LangGraph HITL | 승인 필요 도구 호출 전 interrupt하고 persistence layer에 상태를 저장한 뒤 재개한다. | 배포/DB/파일쓰기/PC제어는 승인 전 상태 저장 후 resume해야 한다. |
| Temporal durable execution | 실패 후 남은 작업을 이어갈 수 있도록 실행 이력을 durable하게 보존한다. | AADS 장기 작업은 이벤트 히스토리 기반으로 재개해야 한다. |
| Inngest steps | 각 step은 독립 재시도되고 이미 성공한 step은 재실행하지 않는다. | 검색/수정/검증/보고를 step 단위 idempotency key로 분리한다. |
| Cloudflare Durable Objects WebSocket hibernation | 유휴 연결은 비용 없이 유지하고 이벤트 도착 시 깨울 수 있다. | 장기적으로 채팅 presence/control channel을 SSE와 분리하는 참고 모델이다. |

## 7. 권장 목표 구조

```
CEO 브라우저
  |
  | 1) user message / interrupt / stop / approve
  v
Chat Control Plane
  - DB-backed interrupts
  - idempotency key
  - active execution ownership
  |
  v
Durable Execution Engine
  - execution_step_started
  - tool_result_saved
  - context_checkpoint_saved
  - artifact_written
  - approval_waiting
  - completed / interrupted / resumed
  |
  +--> SSE/Event Replay Plane
  |     - Redis Stream
  |     - Last-Event-ID
  |     - DB fallback
  |
  +--> Context Budget Guard
        - token estimate
        - auto compact
        - handover artifact
        - runner split
```

핵심 원칙:

1. 스트리밍은 화면 전달 경로일 뿐, 작업의 원장이 아니다.
2. 추가 지시는 메모리 큐가 아니라 DB 이벤트로 저장한다.
3. 컨텍스트가 70% 이상이면 새 작업을 더 읽기 전에 checkpoint/handover를 만든다.
4. 장기 작업은 한 채팅 응답에 묶지 않고 durable job으로 승격한다.
5. 재개는 "partial을 보고 다시 답변"이 아니라 "마지막 완료 step 이후부터 실행"이어야 한다.

## 8. 개선안

### P0. DB-backed interrupt queue

목표: CEO 추가 지시가 서버 재시작/컨텍스트 소진/슬롯 전환에도 사라지지 않게 한다.

변경 대상:

| 파일/DB | 변경 내용 |
|---|---|
| 신규 migration | `chat_interrupts` 테이블 추가 |
| `app/core/interrupt_queue.py` | 메모리 큐를 DB-backed adapter로 교체 또는 보강 |
| `app/routers/chat.py:1155` | streaming 중 메시지를 DB interrupt로 저장 |
| `app/services/chat_service.py:9274` | DB에서 미소비 interrupt를 읽고 consumed 처리 |
| `aads-dashboard/src/app/chat/page.tsx` | `interrupt_queued` 응답을 화면 상태로 표시 |

권장 스키마:

```sql
CREATE TABLE chat_interrupts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
  execution_id UUID NULL REFERENCES chat_turn_executions(id) ON DELETE SET NULL,
  content TEXT NOT NULL,
  attachments JSONB NOT NULL DEFAULT '[]'::jsonb,
  status TEXT NOT NULL DEFAULT 'queued',
  idempotency_key TEXT UNIQUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  consumed_at TIMESTAMPTZ NULL
);
```

완료 기준:

- 스트리밍 중 CEO가 추가 지시를 보내면 `chat_interrupts.status='queued'`로 저장된다.
- API 재시작 후에도 같은 execution 또는 다음 resume 실행이 queued interrupt를 읽는다.
- consumed 처리 전까지 유실되지 않는다.
- 동일 `idempotency_key`는 중복 저장되지 않는다.

### P0. interrupt 즉시 반영 루프

목표: 긴 텍스트 생성이 끝날 때까지 기다리지 않고, 일정 주기마다 추가 지시를 감지해 현재 응답을 revision한다.

권장 방식:

- LLM delta 20~40개 또는 2~3초마다 interrupt check
- interrupt 발견 시 현재 stream에 `event: interrupt_applied`와 `stream_reset` 송신
- 이전 partial은 `interrupted_partial`로 보존
- 새 user instruction을 기존 assistant partial과 함께 넣어 재생성
- 도구 실행 중이면 도구 완료 후 checkpoint에서 revision

주의:

- 토큰 하나마다 interrupt check를 하면 DB 부하가 생긴다.
- 도구 실행 중 강제 cancel은 side effect 위험이 있으므로, 파일쓰기/DB쓰기/배포/PC제어는 step boundary에서만 중단한다.

### P0. context budget guard

목표: "컨텍스트 소진"으로 세션이 죽기 전에 자동으로 작업을 분할한다.

변경 대상:

| 파일 | 변경 내용 |
|---|---|
| `app/services/context_builder.py` | 최종 compiled prompt token estimate와 경고 레벨 반환 |
| `app/services/chat_service.py` | context budget 초과 시 자동 handover 생성 후 runner/job 승격 |
| `app/services/context_compressor.py` | 대화/도구 출력/facts를 근거 pointer 포함 요약으로 압축 |
| `docs/chat/CHAT-STREAMING-SPEC.md` | 컨텍스트 소진 대응 프로토콜 추가 |

정책:

| 레벨 | 조건 | 동작 |
|---|---|---|
| Green | 0~55% | 정상 실행 |
| Yellow | 55~70% | 대용량 파일/로그 읽기 요약 강제 |
| Orange | 70~85% | handover artifact 자동 생성, 추가 파일 읽기 제한 |
| Red | 85%+ | 현재 답변을 짧게 완료하고 durable job으로 분리 |

완료 기준:

- "컨텍스트 소진"으로 종료되기 전에 채팅에 `handover_created` 이벤트가 남는다.
- 다음 세션은 handover artifact와 event ledger로 이어받는다.
- CEO에게 "이전 세션 컨텍스트 소진" 대신 "작업이 durable job으로 전환되어 계속 진행 중"으로 표시된다.

### P1. durable execution step checkpoint

목표: 장기 작업을 step 단위로 재개한다.

권장 신규 테이블:

```sql
CREATE TABLE chat_execution_steps (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  execution_id UUID NOT NULL REFERENCES chat_turn_executions(id) ON DELETE CASCADE,
  step_key TEXT NOT NULL,
  step_type TEXT NOT NULL,
  status TEXT NOT NULL,
  input_hash TEXT,
  output_ref TEXT,
  error_message TEXT,
  started_at TIMESTAMPTZ DEFAULT NOW(),
  completed_at TIMESTAMPTZ,
  UNIQUE (execution_id, step_key)
);
```

적용 예:

| 단계 | step_key | 재개 방식 |
|---|---|---|
| 자료검색 | `research_sources` | 이미 저장된 source/evidence 재사용 |
| 코드확인 | `inspect_streaming_code` | 읽은 파일/라인 ref 재사용 |
| 보고서 작성 | `write_report` | artifact sha 확인 후 skip |
| 검증 | `validate_report` | diff/check 명령 재실행 |
| 최종보고 | `final_response` | 누락 시 이 단계만 재작성 |

### P1. provider/model failure router

목표: 429/402/5xx를 "채팅 끊김"이 아니라 "모델 라우팅 실패"로 분리한다.

개선:

- LiteLLM 429은 즉시 fallback model로 전환
- 쿼터 부족 모델은 UI 모델 선택에서 disabled 또는 "크레딧 필요" 표시
- `chat_turn_executions.error_message`를 normalized code로 분리 저장
- 429가 일정 시간 누적되면 해당 모델 circuit breaker open

완료 기준:

- `LiteLLM gpt-5 HTTP 429` 중단이 신규 실행에서 0에 가까워진다.
- UI에는 "서버 재시작"이 아니라 "모델 쿼터로 대체 모델 사용"으로 표시된다.

### P2. control channel 분리

목표: SSE 본문 스트림과 사용자 control event를 분리한다.

옵션:

| 옵션 | 장점 | 단점 | 권장 |
|---|---|---|---|
| 현재 SSE + REST interrupt | 구현 비용 낮음 | 즉시성/양방향성 제한 | P0 유지 |
| WebSocket control channel | stop/interrupt/approve 즉시성 좋음 | 연결 관리 필요 | P2 |
| Cloudflare Durable Object | 유휴 연결 비용/상태 관리 좋음 | 인프라 변경 큼 | 장기 |

## 9. UX 개선안

CEO 화면에는 실패 원인을 하나로 뭉개서 보여주면 안 된다.

| 상황 | 현재 체감 | 개선 표시 |
|---|---|---|
| 서버 재시작/배포 | "끊김" | "서버 전환 중 - 같은 답변을 복구 중" |
| 컨텍스트 예산 초과 | "세션 종료" | "작업이 길어져 자동 handover 후 계속 진행 중" |
| 추가 지시 수신 | 반영됐는지 불명확 | "추가 지시 1건 접수 - 현재 응답 재작성 중" |
| 모델 쿼터 429 | "응답 실패" | "선택 모델 쿼터 초과 - 대체 모델로 재시도" |
| 도구 장시간 실행 | 멈춘 것처럼 보임 | "도구 실행 중, 마지막 진행 시각 표시" |

## 10. 단계별 실행 계획

| 우선순위 | 작업 | 예상 범위 | 완료 기준 |
|---|---|---|---|
| P0-1 | `chat_interrupts` DB 큐 | migration + router + service + tests | 재시작 후 추가 지시 유실 없음 |
| P0-2 | interrupt 주기 체크 | `chat_service.py` 중심 | 긴 텍스트도 2~3초 내 반영 |
| P0-3 | context budget guard | context builder + chat service | 85% 전 자동 handover/job 전환 |
| P0-4 | 429 normalized fallback | model selector/LiteLLM router | 429 중단 신규 누적 감소 |
| P1-1 | execution step checkpoint | migration + service layer | step 단위 재개 가능 |
| P1-2 | runner/chat handover 통합 | runner submit/status + artifacts | 장기 작업은 채팅 중단 없이 백그라운드 진행 |
| P2-1 | WebSocket control plane | frontend + API | interrupt/stop/approve 지연 최소화 |

## 11. 검증 시나리오

### 11.1 추가 지시 내구성

1. 긴 응답 생성 시작
2. CEO 추가 지시 전송
3. API active 슬롯 재시작 또는 blue/green 전환
4. 복구된 실행이 `chat_interrupts`를 읽고 새 지시를 반영
5. 최종 assistant 메시지에 추가 지시 내용이 반영되고 interrupt row는 consumed 처리

성공 기준:

- `chat_interrupts.status='consumed'`
- `chat_turn_executions.status='completed'`
- 화면에 기존 partial이 사라지지 않고 최종 답변으로 교체

### 11.2 컨텍스트 소진 방지

1. 대용량 파일/로그 읽기 반복
2. context budget 70% 도달
3. 자동 handover artifact 생성
4. 85% 도달 전 runner/job으로 전환
5. 채팅은 job_id와 검증 항목을 짧게 보고하고 종료

성공 기준:

- "컨텍스트 소진" 종료 대신 `handover_created`, `job_submitted` 이벤트가 남는다.
- 다음 세션에서 handover를 읽고 이어갈 수 있다.

### 11.3 모델 쿼터 오류

1. 쿼터 부족 모델 선택
2. 429 발생
3. fallback model 자동 선택
4. UI에 "모델 쿼터로 대체 모델 사용" 표시

성공 기준:

- 사용자에게 서버 재시작 메시지를 표시하지 않는다.
- `error_code='provider_quota_exceeded'`로 저장된다.

## 12. 권장 결론

이 기능은 개선해야 한다. 현재 무중단 배포는 이미 적용되어 있으나, CEO가 요구한 "추가 지시 바로 반영"과 "컨텍스트 소진에도 끊기지 않는 작업"은 별도 아키텍처가 필요하다. 가장 먼저 할 일은 `chat_interrupts` DB 큐와 context budget guard다. 이 두 개만 들어가도 현재 체감 장애의 큰 부분이 줄어든다.

권장 순서:

1. P0-1 `chat_interrupts` DB-backed interrupt 구현
2. P0-2 스트림 중 2~3초 단위 interrupt 반영
3. P0-3 context budget guard와 자동 handover/job 전환
4. P0-4 429 fallback/circuit breaker
5. P1 step checkpoint로 durable execution 완성

## 13. 출처

- WHATWG HTML Standard, Server-sent events: https://html.spec.whatwg.org/multipage/server-sent-events.html
- MDN, Using server-sent events: https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events
- MDN, EventSource: https://developer.mozilla.org/en-US/docs/Web/API/EventSource
- OpenAI Developers, Migrate to the Responses API: https://developers.openai.com/api/docs/guides/migrate-to-responses
- OpenAI API Docs, Conversation state: https://developers.openai.com/api/docs/guides/conversation-state
- Anthropic Claude Platform Docs, Compaction: https://platform.claude.com/docs/en/build-with-claude/compaction
- Claude Code Docs, Context window: https://code.claude.com/docs/en/context-window
- Claude Code Docs, Memory: https://code.claude.com/docs/en/memory
- LangChain Docs, LangGraph Persistence: https://docs.langchain.com/oss/python/langgraph/persistence
- LangChain Docs, Human-in-the-loop: https://docs.langchain.com/oss/python/langchain/human-in-the-loop
- Temporal, What is Durable Execution: https://temporal.io/blog/what-is-durable-execution
- Inngest Docs, How functions are executed: https://www.inngest.com/docs/learn/how-functions-are-executed
- Inngest Docs, Steps: https://www.inngest.com/docs/learn/inngest-steps
- Cloudflare Docs, Durable Objects WebSockets: https://developers.cloudflare.com/durable-objects/best-practices/websockets/

