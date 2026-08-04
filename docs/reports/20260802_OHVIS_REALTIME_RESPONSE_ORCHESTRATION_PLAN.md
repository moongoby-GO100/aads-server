# OHVIS 실시간 즉답·심층레이어·맥락캐시·응답취합 상세 기획서

> 작성 시각: 2026-08-02 21:11 KST  
> 대상: AADS/OHVIS 채팅창, CEO 세션, 백그라운드 심층 작업, 순서 역전 응답 취합  
> 상태: 구현 전 상세 기획안  
> 관련 초안: `docs/reports/20260802_OHVIS_5SEC_IMMEDIATE_RESPONSE_ARCHITECTURE.md`

## 1. 결론

OHVIS가 5초 이내에 맥락을 파악해 응답하려면 기존의 "한 번의 긴 AI 응답" 구조를 유지하면 안 된다. 채팅 입력을 받는 즉시 `즉답 레이어`가 5초 안에 질문 이해, 적용 맥락, 처리 계획, 백그라운드 작업 생성 여부를 표시하고, `심층레이어`는 도구 조회·코드 수정·DB 검증·러너 작업을 별도 job으로 진행해야 한다.

사용자가 1차, 2차, 3차 질문을 연속으로 던지고 백그라운드 완료 순서가 뒤섞여도, 각 결과는 질문 순번(`turn_sequence`)에 귀속되어야 한다. 오래된 결과가 현재 질문 하단에 갑자기 새 응답처럼 붙는 현상은 `Response Coordinator`가 질문별 결과와 종합보고를 분리하면 제거할 수 있다.

## 2. 현재 상태 실측

| 항목 | 현재값 | 판정 | 출처 |
|---|---:|---|---|
| 최근 7일 completed 실행 | 169건 | 정상 완료 이력은 존재 | [DB 조회, 2026-08-02 21:11 KST] |
| 최근 7일 interrupted 실행 | 31건 | 중단 체감 재발 가능 | [DB 조회, 2026-08-02 21:11 KST] |
| 현재 active 실행 | 2건 | 진행 중 실행 존재 | [DB 조회, 2026-08-02 21:11 KST] |
| streaming placeholder | 1건 | 잔여 placeholder 관리 필요 | [DB 조회, 2026-08-02 21:11 KST] |
| assistant 소요시간 데이터 | 192/334건 | 데이터화는 부분 적용 | [DB 조회, 2026-08-02 21:11 KST] |
| API/대시보드/DB/Redis 컨테이너 | healthy | 인프라 기동 상태 정상 | [docker ps, 2026-08-02 21:11 KST] |
| 맥락 표시 UI | `MemoryContextBar` 존재 | 맥락 표시 기반은 있음 | [코드 조회] |
| 버블 하단 소요시간 | 구현됨 | 표시 기반은 있음 | [코드 조회] |
| 백그라운드 질문별 단계 원장 | 전용 구조 없음 | P0 신규 필요 | [코드 조회] |
| 질문 순번 기반 결과 취합 | 전용 구조 없음 | P0/P1 신규 필요 | [코드 조회] |

## 3. 현재 문제점

| 문제 | 현재 증상 | 원인 | 개선 방향 |
|---|---|---|---|
| 완성 답변을 기다리는 구조 | 5초 내 체감 응답 실패 | LLM, 도구, 검증이 한 응답 경로에 묶임 | 즉답과 심층작업 분리 |
| 백그라운드 진행상태 불투명 | 사용자가 멈춘 것으로 인식 | job/step 상태가 채팅 UI에 독립 표시되지 않음 | 버블 하단 상태줄 + 우측 작업 패널 |
| 질문 완료 순서 역전 | 이전 답변이 하단에 새로 생긴 것처럼 보임 | 응답 귀속 기준이 메시지 순서 중심 | `turn_sequence`, `reply_to_message_id` 도입 |
| 추가 질문 중 입력 대기 체감 | "다시 질문해야 하는" 느낌 | 장기 실행 중 입력 가능 상태와 진행 표시가 분리되지 않음 | 입력창은 항상 활성, 작업은 별도 원장 |
| 맥락 재구성 비용 | 긴 세션에서 첫 토큰 지연 | 세션 digest/hot cache 부족 | 맥락 캐시 계층 도입 |
| 개선 데이터 부족 | 느린 이유를 사후 분석하기 어려움 | 즉답/도구/검증/폴백 단계별 지연 미표준화 | `quality_details`와 step metrics 표준화 |

## 4. 목표 UX

### 4.1 채팅창은 항상 즉시 응답 가능한 상태

사용자가 질문을 입력하면 입력창은 잠기지 않는다. 같은 세션에서 심층 작업이 진행 중이어도 새 질문을 받을 수 있어야 한다.

필수 화면 상태:

| 위치 | 표시 예시 | 목적 |
|---|---|---|
| 입력창 상단 또는 세션 상태바 | `백그라운드 2건 진행중 · 입력 가능` | 채팅창이 멈춘 것이 아님을 명확히 표시 |
| 사용자 질문 버블 하단 | `Q#218 · 접수 0.2초 · 즉답 대기` | 질문 원장 고정 |
| AI 즉답 버블 하단 | `즉답 3.4초 · 맥락캐시 hit · 심층 1건 생성` | 5초 SLA 체감 |
| AI 최종 버블 하단 | `최종 48.2초 · 도구 3개 · 검증 2개` | 사후 개선 데이터 표시 |
| 우측 작업 패널 | `Q#218 코드 확인 2/5`, `Q#219 DB 조회 완료` | 병렬 작업 상황 표시 |
| 하단 업데이트 칩 | `이전 질문 결과 1건 업데이트됨` | 오래된 결과가 현재 대화 흐름을 깨지 않게 함 |

### 4.2 진행 표시의 핵심 원칙

1. 진행상태는 최종 답변 본문에 반복 출력하지 않는다.
2. 모델 폴백, 도구 조회, 파일 분석, 러너 진행, 검증 상태는 상태 이벤트로 분리한다.
3. 버블 본문은 사용자가 읽을 답변만 담는다.
4. 버블 하단은 `즉답/심층/검증/비용/소요시간` 같은 메타데이터를 표시한다.
5. 우측 패널은 질문별 작업 진행률과 완료 여부를 실시간으로 보여준다.

## 5. 목표 아키텍처

```text
CEO 질문 입력
  |
  v
Chat Turn Gateway
  - turn_sequence 발급
  - user_message_id 저장
  - idempotency_key 생성
  - 입력창 즉시 재활성화
  |
  +--> Immediate Response Layer
  |     - 5초 SLA
  |     - hot context cache 사용
  |     - 질문 이해/처리계획 즉답
  |     - 심층 필요 시 job 생성
  |
  +--> Deep Work Layer
  |     - DB/코드/로그/웹/러너 작업 수행
  |     - step checkpoint 저장
  |     - 중단 시 recovery job 생성
  |
  +--> Context Cache Layer
  |     - session digest
  |     - workspace digest
  |     - memory digest
  |     - active jobs digest
  |
  +--> Response Coordinator
        - 결과를 질문별 귀속
        - 완료 순서 역전 정리
        - 종합보고 생성
        - stale/interrupted 복구
```

## 6. 레이어별 상세 설계

### 6.1 Chat Turn Gateway

역할은 질문 원장을 먼저 확정하는 것이다. LLM 호출 전에 사용자 질문은 DB에 저장되고, 질문마다 순번을 부여한다.

필수 저장값:

| 필드 | 설명 |
|---|---|
| `session_id` | 채팅 세션 |
| `turn_sequence` | 세션 내 질문 순번 |
| `user_message_id` | 사용자 질문 메시지 |
| `idempotency_key` | 새로고침/재전송 중복 방지 |
| `parent_turn_sequence` | "이어서", "그거" 같은 후속 질문 연결 |
| `priority` | 현재 질문 우선순위 |
| `status` | accepted, immediate_done, deep_running, completed 등 |

Gateway의 완료 기준:
- 질문 저장은 500ms 이내 목표
- 저장 성공 즉시 화면에 Q# 표시
- 입력창은 저장 직후 다시 활성화

### 6.2 Immediate Response Layer

즉답 레이어는 완성 보고서가 아니라 "사용자가 지금 무슨 일이 진행되는지 알 수 있는 첫 응답"을 담당한다.

응답 유형:

| 질문 유형 | 5초 내 즉답 | 심층 작업 |
|---|---|---|
| 단순 대화 | 바로 답변 완료 | 없음 |
| 현재 상태 조회 | 확인 대상과 조회 기준 표시 | DB/API 조회 job |
| 코드 수정 | 영향 범위, 롤백, 실행 방식 표시 | 직접 수정 또는 Runner job |
| 장문 보고 | 요약 방향과 산출물 형식 표시 | report job |
| 연속 질문 | 연결된 이전 Q#와 처리 정책 표시 | 필요 시 취합 job |

즉답 하단 메타:

```json
{
  "turn_sequence": 218,
  "immediate_duration_ms": 3400,
  "context_cache_hit": true,
  "deep_job_count": 1,
  "status": "deep_running"
}
```

### 6.3 Deep Work Layer

심층 레이어는 채팅 스트림 한 줄에 묶지 않고 단계별 job으로 실행한다.

단계 예시:

| 단계 | 화면 문구 | 저장 상태 |
|---|---|---|
| queued | 대기중 | queued |
| context_loading | 맥락 불러오는 중 | running |
| tool_running | DB 조회 2/3 | running |
| code_editing | 파일 수정 중 | running |
| validating | 검증 중 | running |
| synthesizing | 결과 취합 중 | running |
| completed | 최종보고 완료 | completed |
| failed_recoverable | 자동 재개 대기 | retrying |
| failed_terminal | 사용자 확인 필요 | interrupted |

Deep Work의 원칙:
- 각 step은 `started_at`, `completed_at`, `duration_ms`, `status`, `error_category`를 가진다.
- 브라우저가 닫히거나 새로고침되어도 DB 원장 기준으로 복원된다.
- `Transport closed`, 모델 quota, 도구 오류, 사용자 중단, 네트워크 중단은 서로 다른 error_category로 분리한다.

### 6.4 Context Cache Layer

5초 즉답을 가능하게 하는 핵심은 매번 전체 세션을 새로 읽지 않는 것이다.

| 캐시 | 내용 | 무효화 조건 |
|---|---|---|
| `session_hot_digest` | 최근 20턴, 현재 미완료 질문, 결정사항 | 새 메시지/최종보고 저장 |
| `workspace_digest` | AADS 운영규칙, 서버, 배포 정책 | prompt asset 변경 |
| `memory_digest` | CEO 선호, 반복 오류, 절차 메모리 | memory write 또는 TTL 만료 |
| `active_job_digest` | 현재 세션/전체 프로젝트 작업 현황 | job 상태 변경 |
| `artifact_digest` | 최근 문서/보고서 요약 | artifact 생성/수정 |

사용 방식:
- 즉답 레이어는 hot digest만 사용해 빠르게 답한다.
- 심층 레이어는 기존 full context builder와 도구 조회를 사용해 정확도를 보강한다.
- 캐시 hit/miss는 `quality_details.context_cache_hit`로 저장한다.

### 6.5 Response Coordinator

질문 완료 순서가 뒤섞여도 화면과 최종보고가 안정적으로 유지되게 한다.

핵심 규칙:

1. 모든 AI 결과는 `parent_turn_sequence`를 가져야 한다.
2. 즉답은 질문 바로 아래에 붙는다.
3. 심층 결과가 늦게 도착하면 해당 질문의 최종 결과로 연결한다.
4. 현재 최신 질문 하단에 오래된 결과를 무작정 새 버블로 삽입하지 않는다.
5. 오래된 결과는 해당 질문 위치를 업데이트하고, 현재 하단에는 `Q#216 결과 업데이트됨` 칩만 표시한다.
6. 여러 질문의 결과를 묶어야 하면 `synthesis_group_id`로 종합보고를 만든다.

## 7. 1차, 2차, 3차 질문 처리 정책

### 7.1 독립 질문의 완료 순서가 뒤섞이는 경우

```text
Q#1: 서버 상태 확인
Q#2: 엑셀 첨부 분석 기능 조치
Q#3: 채팅 스크롤 문제 분석

완료 순서: Q#2 -> Q#1 -> Q#3
```

화면 처리:
- Q#2 결과는 Q#2 위치에 `최종보고 완료`로 연결한다.
- Q#1 결과는 Q#1 위치에 `최종보고 완료`로 연결한다.
- Q#3 결과는 Q#3 위치에 `최종보고 완료`로 연결한다.
- 화면 하단에는 `이전 질문 결과 2건 업데이트됨` 칩만 보여준다.
- 사용자가 원하면 칩을 눌러 해당 Q# 위치로 이동한다.

### 7.2 의존 질문의 경우

```text
Q#1: 원인 찾아줘
Q#2: 개선안까지 보고해
Q#3: 전체 세션에 같은 문제가 없는지 종합해
```

처리:
- Q#2는 Q#1에 의존한다.
- Q#3은 Q#1, Q#2에 의존한다.
- Q#3 즉답은 `Q#1/Q#2 결과를 기다린 뒤 종합보고로 묶겠습니다`로 표시한다.
- Q#1, Q#2 완료 후 `Response Coordinator`가 Q#3 종합보고 job을 실행한다.

### 7.3 사용자가 새 질문으로 우선순위를 바꾸는 경우

예: `이전 건 멈추고 이거 먼저 해`

처리:
- 기존 job은 `paused_by_user_priority` 또는 `cancelled_by_user`로 저장한다.
- 부분 결과는 draft artifact로 보존한다.
- 새 질문에 priority를 부여해 즉답과 심층 작업을 먼저 실행한다.

## 8. 데이터 모델 제안

기존 `chat_messages`, `chat_turn_executions`, `quality_details`는 유지한다. 신규 테이블은 additive migration으로 추가한다.

| 테이블 | 목적 | 핵심 컬럼 |
|---|---|---|
| `chat_turn_sequences` | 질문 순번 원장 | `session_id`, `turn_sequence`, `user_message_id`, `status`, `priority` |
| `chat_response_jobs` | 즉답/심층 작업 원장 | `turn_sequence`, `execution_id`, `job_type`, `status`, `idempotency_key` |
| `chat_response_steps` | 단계별 진행 상태 | `job_id`, `step_key`, `status`, `started_at`, `completed_at`, `progress_json` |
| `chat_context_cache` | 맥락 캐시 | `session_id`, `cache_type`, `content`, `expires_at`, `source_revision` |
| `chat_response_synthesis` | 종합보고 취합 | `synthesis_group_id`, `depends_on_turns`, `status`, `final_message_id` |
| `chat_interrupts` | 내구 인터럽트 큐 | `session_id`, `execution_id`, `content`, `status`, `idempotency_key` |

`quality_details` 표준 필드:

```json
{
  "turn_sequence": 218,
  "parent_turn_sequence": 217,
  "immediate_duration_ms": 3400,
  "deep_duration_ms": 48200,
  "context_cache_hit": true,
  "tool_wait_ms": 12800,
  "validation_duration_ms": 6200,
  "fallback_count": 0,
  "error_category": null,
  "completion_order": 2,
  "synthesis_group_id": "syn-20260802-..."
}
```

## 9. SSE/API 이벤트 제안

| 이벤트 | 의미 | 프론트 동작 |
|---|---|---|
| `turn_accepted` | 질문 저장/순번 확정 | 사용자 버블 하단 Q# 표시 |
| `immediate_started` | 즉답 시작 | AI 즉답 placeholder 생성 |
| `immediate_done` | 즉답 완료 | `즉답 N초` 표시 |
| `deep_job_created` | 심층 job 생성 | 우측 작업 패널 추가 |
| `deep_step_update` | 단계 진행 | 진행률/단계명 갱신 |
| `deep_result_ready` | 질문별 최종 결과 도착 | 해당 질문 위치에 결과 연결 |
| `synthesis_waiting` | 종합보고 의존 대기 | 종합보고 대기 상태 표시 |
| `synthesis_done` | 종합보고 완료 | 종합 버블/아티팩트 생성 |
| `recovery_scheduled` | 자동 재개 예약 | 중단 버블 대신 복구 상태 표시 |
| `fallback_notice` | 모델 폴백 | 본문이 아닌 상태줄에 축소 표시 |

## 10. 구현 로드맵

### P0: 즉답 원장과 실시간 진행 표시

목표:
- 5초 안에 즉답 또는 작업 생성 상태를 보여준다.
- 채팅 입력창은 즉답 후 즉시 대기 상태로 돌아간다.
- 진행상태는 우측 작업 패널과 버블 하단에 표시한다.

작업:
- `chat_turn_sequences`, `chat_response_jobs`, `chat_response_steps` migration
- `send_message_stream()` 앞단에 turn gateway 추가
- 즉답 완료 시간을 `quality_details.immediate_duration_ms`로 저장
- 대시보드에 Q# / 즉답 / 심층 상태 표시
- 모델 폴백 메시지를 본문 버블에서 상태줄로 이동

검증:
- 단순 질문 p95 즉답 5초 이하
- 도구 필요 질문도 5초 내 `심층 진행중` 표시
- 새로고침 후 작업 패널 복원
- 오래된 결과가 최신 질문 하단에 새 버블로 붙지 않음

### P1: 맥락 캐시와 내구 인터럽트

목표:
- 긴 세션에서도 즉답 레이어가 빠르게 맥락을 잡는다.
- 스트리밍 중 추가 지시가 API 재시작/브라우저 새로고침에도 유실되지 않는다.

작업:
- `chat_context_cache` 구현
- session/workspace/memory/active-job digest 생성
- `interrupt_queue.py` 인메모리 큐를 DB-backed `chat_interrupts`로 전환
- context cache hit/miss 메트릭 저장

검증:
- 긴 세션에서 cache hit 즉답이 5초 이하
- API reload 중 추가 지시가 pending interrupt로 복원
- memory/prompt 변경 시 cache 무효화

### P2: 순서 역전 취합과 종합보고 자동화

목표:
- 1차/2차/3차 질문 결과가 완료 순서와 관계없이 질문별로 정리된다.
- 종합보고가 필요한 질문은 이전 결과를 취합해 별도 최종 버블 또는 아티팩트로 생성한다.

작업:
- `chat_response_synthesis` 구현
- depends_on 질문 자동 추론
- out-of-order completion UI 정책 구현
- 종합보고 artifact 생성

검증:
- Q#1/Q#2/Q#3 완료 순서가 뒤섞여도 화면 정합성 유지
- 현재 질문 하단에 과거 답변이 갑자기 삽입되지 않음
- 종합보고에 의존 질문들의 결과 링크와 상태가 표시됨

## 11. 개선 시 기대 효과

| 항목 | 현재 | 개선 후 |
|---|---|---|
| 첫 체감 응답 | 완성 답변까지 대기 | 5초 내 즉답 |
| 장기 작업 중 입력 | 멈춘 것처럼 보임 | 입력 가능 + 작업 진행 표시 |
| 이전 답변 재등장 | 순서 역전 시 혼란 | 질문별 귀속 + 업데이트 칩 |
| 중단 후 재개 | 사용자가 다시 지시하는 경우 존재 | recovery 상태 표시와 자동 재개 |
| 맥락 품질 | 긴 세션에서 느려짐 | hot cache 즉답 + deep context 품질 유지 |
| 성능 개선 데이터 | 부분 수집 | 즉답/심층/도구/검증 단계별 데이터화 |
| 비용 최적화 | 어떤 단계가 비싼지 불명확 | 단계별 latency/cost 기반 라우팅 개선 |

## 12. 완료 기준

| 기준 | 목표 |
|---|---|
| 즉답 p95 | 5초 이하 |
| 즉답 실패율 | 1% 이하 |
| 질문 저장 지연 | p95 500ms 이하 |
| background job 복원 | 새로고침 후 100% |
| active stale 30분 초과 | 0건 유지 |
| 추가 지시 유실 | 0건 |
| out-of-order 혼동 | Q# 귀속으로 제거 |
| 소요시간 데이터화 | 즉답/심층/도구/검증 단계별 저장 |

## 13. 권장 실행 방식

이 작업은 백엔드 migration, SSE 이벤트, 채팅 서비스, 대시보드 UI, 자동 재개 정책까지 연결되는 L 규모 작업이다. 직접 한 번에 수정하기보다 Pipeline Runner로 P0를 먼저 분리 실행하는 것이 맞다.

권장 작업:

```text
TASK_ID: AADS-OHVIS-REALTIME-RESPONSE-ORCHESTRATION-P0-20260802
TITLE: OHVIS 5초 즉답 원장과 백그라운드 작업 실시간 표시 구현
PRIORITY: P0
SIZE: L
MODEL: gpt-5.6-sol 장애 시 안정 폴백 모델
```

영향 범위:
- Backend: `app/services/chat_service.py`, `app/routers/chat.py`, 신규 migration
- Frontend: `/root/aads/aads-dashboard/src/app/chat/page.tsx`, 필요 시 컴포넌트 분리
- DB: 신규 additive table 3개 이상
- 배포: API + Dashboard blue/green

롤백:
- 신규 테이블은 additive이므로 기존 데이터 삭제 없이 사용 중단 가능
- 코드 롤백은 단일 커밋 revert 후 API/Dashboard 재배포
- 기존 `chat_messages`, `chat_turn_executions` 데이터 삭제 없음

## 14. 다음 단계

1. P0 Runner 제출: 즉답 원장, 작업 패널, 버블 하단 상태 표시를 먼저 구현한다.
2. P1 별도 제출: 맥락 캐시와 내구 인터럽트 큐를 구현한다.
3. P2 별도 제출: 질문 순서 역전 취합과 종합보고 자동화를 구현한다.
