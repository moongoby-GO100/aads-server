# OHVIS 5초 즉답 아키텍처 기획서 검토 — 문제점 및 개선안

> 검토 시각: 2026-08-02 21:25 KST
> 검토 대상: `docs/reports/20260802_OHVIS_5SEC_IMMEDIATE_RESPONSE_ARCHITECTURE.md` (325줄)
> 참고 문서: `docs/reports/20260802_OHVIS_REALTIME_RESPONSE_ORCHESTRATION_PLAN.md` (401줄)
> 검토 방법: 기획서 내용 + 현재 코드 실측 대조 (chat_service.py, interrupt_queue.py, context_builder, DB 스키마)
> 상태: 검토 완료, CEO 판단 대기

---

## 1. 검토 요약

기획서의 방향(즉답/심층 분리, 질문별 귀속, 맥락 캐시, 종합보고 취합)은 올바르다. 그러나 **5초 SLA 달성 경로가 구체적이지 않고, 현재 코드 구조(11,416줄 모놀리스)와의 간극이 크며, 비용·테스트·마이그레이션 전략이 누락**되어 있어 이대로 P0 러너에 넣으면 실패 확률이 높다. 아래 15건의 문제점과 개선안을 제시한다.

---

## 2. 문제점 상세

### P0 — 구현 전 반드시 해결해야 하는 차단 이슈

| # | 문제 | 현재 상태 | 위험 | 출처 |
|---|---|---|---|---|
| 1 | **5초 SLA 달성 경로 미구체화** | 기획서는 "5초 SLA"를 12회 언급하지만, LLM first-token latency(Claude Opus 2~4초, Sonnet 1~2초) + context assembly(1~2초) + DB 저장(0.3~0.5초)을 합산한 실현 경로가 없음 | p95 5초는 Opus로 불가능. 모델 선택 전략 없이 SLA만 선언하면 검증 시 전면 실패 | [코드 조회: send_message_stream L7906, 실측: 최근 7일 중앙값 58초] |
| 2 | **chat_service.py 11,416줄 모놀리스에 추가하는 구조** | 즉답 레이어, Turn Gateway, Response Coordinator, Deep Work Layer를 전부 chat_service.py에 넣으면 15,000줄 이상 | 리뷰 불가, 테스트 불가, 버그 수정 시 회귀 위험 급증 | [코드 조회: `grep -c "" chat_service.py` = 11,416] |
| 3 | **점진적 마이그레이션 전략 부재** | 현재 단일 스트림 구조 → 즉답+심층 이중 스트림으로의 전환 기간 동안 양 구조가 어떻게 공존하는지 미정의 | Big bang 전환은 실패 시 전체 채팅 장애. Feature flag 또는 세션별 opt-in 전략이 필요 | [기획서 §13: "P0를 Pipeline Runner로 실행" — 전환 기간 정책 없음] |
| 4 | **모델 선택 R-AUTH 규칙 충돌** | 기획서 §13에 `MODEL: gpt-5.6-sol`로 기본 지정. CEO 규칙(R-AUTH)은 ANTHROPIC_AUTH_TOKEN(OAuth) 우선, Gemini/외부는 LiteLLM 경유 | GPT 모델을 기본으로 잡으면 인증 규칙 위반. 즉답용 경량 모델도 Anthropic 계열(Haiku 4.5 등) 우선 검토 필요 | [CLAUDE.md R-AUTH, 기획서 §13] |

### P1 — 구현 품질에 직접 영향하는 설계 누락

| # | 문제 | 상세 | 위험 |
|---|---|---|---|
| 5 | **즉답 레이어의 모델 라우팅 미정의** | 즉답에 Opus를 쓰면 5초 초과, Haiku를 쓰면 품질 부족. 질문 유형별 모델 분기 전략이 없음 | 5초 SLA와 답변 품질 사이에서 trade-off 불가 |
| 6 | **SSE 이벤트 10개 신규 추가의 프론트 복잡도** | `turn_accepted`, `immediate_started/done`, `deep_job_created`, `deep_step_update`, `deep_result_ready`, `synthesis_waiting/done`, `recovery_scheduled`, `fallback_notice` — 10개 신규 이벤트를 프론트에서 처리하려면 상태 머신 설계가 필요 | 이벤트 핸들러 없이 구현하면 race condition, 상태 불일치로 UI 버그 급증 |
| 7 | **신규 테이블 6개의 인덱스/FK/파티셔닝 미정의** | `chat_turn_sequences`, `chat_response_jobs`, `chat_response_steps`, `chat_context_cache`, `chat_response_synthesis`, `chat_interrupts` — 컬럼만 나열하고 PK, FK, 복합 인덱스, 파티셔닝 전략이 없음 | `chat_response_steps`는 매 단계 UPDATE가 발생. heartbeat pump의 `updated_at` 갱신 루프(기존 P0 버그)와 동일한 과도 write 패턴 재발 가능 |
| 8 | **기존 버그와의 관계 미분석** | heartbeat pump `updated_at` 갱신 루프(stale watchdog 무력화), streaming_placeholder 잔여 문제가 즉답+심층 분리 후 해결되는지/악화되는지 분석 없음 | 기존 P0 버그를 새 아키텍처가 가리거나 악화시킬 수 있음 |
| 9 | **인터럽트 큐 DB 전환 시 latency 미분석** | 현재 인메모리 dict O(1) 접근 → DB 전환 시 매 push/pop마다 INSERT/SELECT 필요. 스트리밍 중 인터럽트 확인 주기가 짧으면 DB 부하 증가 | 즉답 5초 SLA와 상충. Redis 중간 계층 또는 WAL 기반 경량 큐가 대안 |
| 10 | **depends_on 질문 자동 추론 정확도 보장 미정의** | "이어서", "그거", "전체 종합해"를 자동 감지한다고 했지만, NLP 기반인지 키워드 기반인지 사용자 명시 기반인지 미정의 | 오탐 시 독립 질문을 불필요하게 대기시킴, 미탐 시 종합보고 누락 |

### P2 — 운영 안정성에 영향하는 보완 필요 항목

| # | 문제 | 상세 | 위험 |
|---|---|---|---|
| 11 | **비용 추정 전무** | L 규모 작업에 대한 개발 비용(토큰/시간), 운영 비용(추가 DB write, 추가 LLM 호출 — 즉답+심층 이중 호출), 인프라 비용 추정 없음 | CEO 비용 통제 규칙(R-QUALITY-COST)에 따라 $5 초과 예상 시 중간보고 필요인데, 추정 자체가 불가 |
| 12 | **recovery job 재시도 정책 미정의** | `failed_recoverable` → `recovery_scheduled` 이벤트는 있지만, 재시도 횟수, backoff 전략, 최종 실패 처리가 없음 | 무한 재시도 시 비용/자원 낭비, 재시도 없으면 사용자가 수동 재요청 필요 |
| 13 | **우측 작업 패널의 반응형/모바일 설계 누락** | "우측 작업 패널"을 명시했지만 현재 대시보드는 채팅 중심 단일 컬럼 레이아웃. 모바일에서 우측 패널은 불가 | 모바일 사용자에게 동일 UX를 제공할 수 없음. 접이식 패널 또는 버블 내 축약 표시 대안 필요 |
| 14 | **테스트 전략 전무** | 단위 테스트, 통합 테스트, E2E 테스트 범위와 방법이 전혀 없음. 검증 항목은 "p95 5초 이하" 같은 목표만 있고 측정 방법이 없음 | L 규모 코드 변경을 테스트 없이 배포하면 기존 채팅 기능 회귀 위험 |
| 15 | **두 문서 간 70% 내용 중복** | ARCHITECTURE.md(325줄)와 ORCHESTRATION_PLAN.md(401줄)가 아키텍처 다이어그램, 데이터 모델, SSE 이벤트, 로드맵을 거의 동일하게 반복 | 유지보수 시 한 쪽만 수정하면 불일치 발생. 단일 문서로 통합하거나 역할을 명확히 분리 필요 |

---

## 3. 개선안

### 3.1 [P0-즉시] 5초 SLA 달성 경로 구체화

**현재 문제**: "5초 안에 즉답"이라는 목표만 있고 달성 수단이 없다.

**개선안**:

| 구분 | 방안 | 기대 효과 | 검증 방법 |
|---|---|---|---|
| 모델 분기 | 즉답 레이어는 **Haiku 4.5**(first-token ~0.5초) 또는 **Sonnet 5**(~1초), 심층은 Opus 5 | 즉답 p95 3초 이내 가능 | 모델별 first-token latency를 10회 측정해 p95 산출 |
| 프롬프트 경량화 | 즉답 프롬프트는 system prompt를 1,000토큰 이하로 축약 (hot digest만 사용) | context assembly 0.5초 이내 | 축약 프롬프트 토큰 수 측정 |
| pre-warming | 세션 활성 시 hot digest를 미리 Redis에 캐싱, 질문 도착 즉시 사용 | cache 조회 50ms 이내 | Redis GET latency 측정 |
| 타임아웃 보호 | 즉답 레이어에 4.5초 hard timeout → 초과 시 "처리 중입니다" 기본 메시지 | 5초 SLA 100% 보장 | timeout 발동률 모니터링 |

**완료기준**: 모델별 first-token latency 실측표 + 즉답 프롬프트 토큰 수 확정 + pre-warming 설계 포함.

### 3.2 [P0-즉시] 모듈 분리 계획 추가

**현재 문제**: 11,416줄 모놀리스에 기능을 추가하면 유지보수 불가.

**개선안**:

```text
app/services/
  chat_service.py          — 기존 (점진적 축소)
  chat_turn_gateway.py     — turn_sequence 발급, 질문 저장, idempotency
  chat_immediate.py        — 즉답 레이어 (모델 선택, hot cache 조회, 5초 SLA)
  chat_deep_work.py        — 심층 작업 job 관리, step checkpoint
  chat_response_coord.py   — 결과 귀속, 순서 역전 정리, 종합보고
  chat_context_cache.py    — 맥락 캐시 CRUD, TTL, 무효화
app/core/
  interrupt_queue.py       — DB-backed으로 전환 (Redis 중간 계층)
```

**완료기준**: 모듈별 책임 범위, 의존 방향, 기존 chat_service.py에서 추출 순서를 문서화.

### 3.3 [P0-즉시] 점진적 마이그레이션 전략 추가

**현재 문제**: Big bang 전환은 전체 채팅 장애 위험.

**개선안**:

| 단계 | 내용 | 롤백 |
|---|---|---|
| Phase A | DB 테이블 추가 (additive). 기존 코드 동작 변경 없음 | 테이블 무시 가능 |
| Phase B | `send_message_stream()` 진입부에 feature flag 분기. flag OFF면 기존 경로, ON이면 즉답+심층 경로 | flag OFF로 즉시 복원 |
| Phase C | CEO 세션 1개에서 flag ON으로 검증 (canary) | 해당 세션만 flag OFF |
| Phase D | 전체 세션 ON, 기존 경로 제거 | git revert + flag OFF |

**완료기준**: feature flag 이름, DB 저장 위치, canary 세션 선정 기준을 기획서에 포함.

### 3.4 [P0-즉시] 모델 선택 R-AUTH 준수

**현재 문제**: `MODEL: gpt-5.6-sol`은 R-AUTH 위반.

**개선안**:
- 즉답 레이어 기본 모델: `claude-haiku-4-5` (Anthropic OAuth 1순위)
- 심층 레이어 기본 모델: `claude-sonnet-5` 또는 세션 설정 모델
- 폴백: Anthropic 장애 시 → Gemini LiteLLM 경유 (기존 call_llm_with_fallback 활용)
- 러너 작업 모델도 Anthropic 우선으로 수정

### 3.5 [P1] SSE 이벤트 상태 머신 설계

**현재 문제**: 10개 신규 이벤트의 프론트 처리 로직이 없다.

**개선안**:

```text
상태 전이 (질문 단위):
  accepted → immediate_started → immediate_done
    ├→ (심층 불필요) completed
    └→ deep_job_created → deep_step_update* → deep_result_ready → completed
         └→ (실패) recovery_scheduled → deep_step_update* → ...

종합보고:
  synthesis_waiting → (의존 질문 전부 completed) → synthesis_done
```

프론트엔드에 `useTurnStateMachine(turnSequence)` 훅을 추가하고, 각 상태 전이를 하나의 reducer로 관리한다. 잘못된 순서의 이벤트(예: `deep_result_ready` before `deep_job_created`)는 버퍼링 후 재정렬.

**완료기준**: 상태 전이 다이어그램 + 프론트 상태 관리 훅 인터페이스 확정.

### 3.6 [P1] DB 스키마 구체화

**현재 문제**: 컬럼만 나열하고 PK, FK, 인덱스가 없다.

**개선안 — 핵심 테이블 스키마 예시**:

```sql
-- chat_turn_sequences
CREATE TABLE chat_turn_sequences (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id UUID NOT NULL REFERENCES chat_sessions(id),
  turn_sequence INTEGER NOT NULL,
  user_message_id UUID REFERENCES chat_messages(id),
  parent_turn_sequence INTEGER,          -- 후속 질문 연결
  priority SMALLINT DEFAULT 0,
  status VARCHAR(30) DEFAULT 'accepted',
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (session_id, turn_sequence)
);
CREATE INDEX idx_cts_session_status ON chat_turn_sequences(session_id, status);

-- chat_response_steps: 과도 write 방지
-- step_key 기준 UPSERT로 중복 저장 방지
-- completed_at IS NULL 인덱스로 활성 step만 빠르게 조회
CREATE INDEX idx_crs_job_active ON chat_response_steps(job_id) WHERE completed_at IS NULL;
```

**완료기준**: 6개 테이블 전체 DDL, 인덱스, FK, UPSERT 전략 확정.

### 3.7 [P1] 인터럽트 큐 전환: Redis 중간 계층

**현재 문제**: 인메모리 → DB 직접 전환은 latency 증가.

**개선안**:

```text
현재: dict (인메모리, 프로세스 재시작 시 유실)
    ↓
개선: Redis List (push/pop O(1), 50ms 이내) + DB 비동기 백업 (1분 주기)
    - push_interrupt → RPUSH redis + 비동기 INSERT DB
    - pop_interrupts → LRANGE + DEL redis, DB status='consumed'
    - 프로세스 재시작 시 → DB에서 status='pending' 복원 → Redis에 재적재
```

**완료기준**: Redis 컨테이너 활용 확인(이미 존재), latency 실측, 복원 테스트.

### 3.8 [P1] 기존 P0 버그와의 관계 분석 추가

**현재 문제**: heartbeat pump `updated_at` 루프와 streaming_placeholder 잔여 문제가 새 아키텍처와 어떻게 상호작용하는지 불명.

**개선안**: 기획서에 다음 분석 섹션 추가

| 기존 버그 | 즉답+심층 분리 후 | 추가 조치 |
|---|---|---|
| heartbeat pump `updated_at` 루프 → stale watchdog 무력화 | 심층 레이어의 `chat_response_steps`가 단계별 상태를 관리하므로, heartbeat 대신 step status 기반 watchdog으로 전환 가능 | heartbeat pump 제거 또는 step-based liveness로 교체 |
| streaming_placeholder 잔여 | 즉답 버블과 심층 최종 버블이 분리되므로 placeholder 생명주기가 명확해짐 | 즉답 placeholder TTL 10초, 심층 placeholder는 job 완료/실패 시 자동 정리 |

### 3.9 [P1] depends_on 추론 전략 확정

**현재 문제**: 자동 추론 방식이 미정의.

**개선안**: 3단계 판정

| 단계 | 방식 | 정확도 |
|---|---|---|
| 1차 | 사용자 명시 ("이전 거 이어서", "종합해") — 키워드 매칭 | 높음 |
| 2차 | 같은 세션 내 직전 turn이 running 상태면 자동 의존 후보 | 중간 |
| 3차 | LLM 즉답 시 "이 질문은 Q#N에 의존합니다" 판정 포함 | 높음 (비용 추가) |

1차만 우선 구현하고, 2차/3차는 P2에서 데이터 기반으로 추가.

### 3.10 [P2] 비용 추정 추가

**현재 문제**: 비용 추정이 전혀 없다.

**개선안**:

| 항목 | 현재 | 즉답+심층 분리 후 | 증감 |
|---|---|---|---|
| LLM 호출 횟수/턴 | 1회 | 2회 (즉답 Haiku + 심층 Sonnet/Opus) | +100% |
| 즉답 토큰 | 0 | ~500 input + ~200 output (Haiku) | +$0.0002/턴 |
| DB write/턴 | ~3 INSERT/UPDATE | ~8 (turn + job + steps + cache) | +167% |
| Redis 사용 | 기존 stream/session | + interrupt queue + context cache | 미미 |
| 개발 비용 | - | L 규모, Runner 2~3회 예상 | $15~30 추정 |

**결론**: 턴당 LLM 비용은 Haiku 즉답 추가로 ~$0.0002 증가(미미). DB write 증가는 인덱스 최적화로 대응 가능. 개발 비용이 주요 고려사항.

### 3.11 [P2] 테스트 전략 추가

**현재 문제**: 테스트 계획이 전혀 없다.

**개선안**:

| 테스트 유형 | 대상 | 방법 |
|---|---|---|
| 단위 테스트 | Turn Gateway, Immediate Layer, Response Coordinator | pytest, mock LLM 응답 |
| 통합 테스트 | DB migration, SSE 이벤트 전체 흐름 | docker-compose test, 실제 DB |
| E2E 테스트 | 질문 → 즉답 → 심층 → 최종보고 전체 경로 | Playwright 또는 API 기반 시나리오 |
| 성능 테스트 | 즉답 p95, DB write 부하 | k6 또는 locust로 부하 테스트 |
| 회귀 테스트 | 기존 채팅 기능 (메시지 저장, SSE, 세션 전환) | 기존 test suite 전체 실행 |

### 3.12 [P2] 문서 통합

**현재 문제**: 두 문서가 70% 중복.

**개선안**: 단일 마스터 문서로 통합하고, 역할 분리:

- `OHVIS_5SEC_ARCHITECTURE.md` → 아키텍처 결정 기록 (ADR 형식): 왜 이 구조인지, 대안은 무엇이었는지
- `OHVIS_5SEC_IMPLEMENTATION_SPEC.md` → 구현 명세: DB DDL, SSE 이벤트, 모듈 구조, 테스트, 마이그레이션 절차

---

## 4. 개선 우선순위 요약

| 우선순위 | 개선안 | 핵심 산출물 | 차단 관계 |
|---|---|---|---|
| **P0-1** | 5초 SLA 달성 경로 구체화 (§3.1) | 모델별 latency 실측표, 즉답 프롬프트 설계 | P0 러너 제출 전 필수 |
| **P0-2** | 모듈 분리 계획 (§3.2) | 모듈 책임 범위, 추출 순서 | P0 러너 제출 전 필수 |
| **P0-3** | 점진적 마이그레이션 전략 (§3.3) | Feature flag 설계, canary 절차 | P0 러너 제출 전 필수 |
| **P0-4** | 모델 선택 R-AUTH 준수 (§3.4) | 모델 라우팅 표 수정 | 즉시 반영 |
| **P1-1** | SSE 상태 머신 (§3.5) | 상태 전이 다이어그램 | P0 구현 시 필요 |
| **P1-2** | DB 스키마 구체화 (§3.6) | 6개 테이블 DDL | P0 구현 시 필요 |
| **P1-3** | 인터럽트 큐 Redis 계층 (§3.7) | Redis 설계, 복원 테스트 | P1 구현 시 필요 |
| **P1-4** | 기존 버그 관계 분석 (§3.8) | heartbeat/placeholder 전환 계획 | P0 구현 시 참조 |
| **P1-5** | depends_on 추론 전략 (§3.9) | 3단계 판정 로직 | P2 구현 시 필요 |
| **P2-1** | 비용 추정 (§3.10) | 턴당/월간 비용 추정표 | CEO 승인 시 필요 |
| **P2-2** | 테스트 전략 (§3.11) | 테스트 유형별 계획 | P0 배포 전 필수 |
| **P2-3** | 문서 통합 (§3.12) | ADR + 구현 명세 분리 | 장기 유지보수 |

---

## 5. 결론

기획서의 아키텍처 방향은 맞지만, **"어떻게 5초를 달성하는가"의 기술 경로가 가장 큰 빈칸**이다. 현재 코드에는 제안된 구조가 0건 구현되어 있고, 11,416줄 모놀리스를 확장하는 방식으로는 L 규모 작업의 성공 가능성이 낮다.

**즉시 권장 조치**:
1. P0 러너 제출 전에 §3.1(모델 latency 실측) + §3.2(모듈 분리 계획) + §3.3(feature flag 전략)을 기획서에 보강
2. 모델 기본값을 Anthropic 계열로 수정 (R-AUTH 준수)
3. 기존 heartbeat pump 버그 수정을 P0에 포함시켜, 새 아키텍처가 기존 버그를 상속하지 않도록 함

→ 이 3건 보강 후 P0 러너 제출이 적절하다.

---

## 교훈

- **SLA 선언과 SLA 달성 경로는 다르다**: 목표 수치만 반복하면 검증 시 전면 실패한다. 병목 구간별 latency budget을 할당해야 한다.
- **모놀리스에 기능을 추가하면 모놀리스가 커진다**: 새 아키텍처를 기획할 때 기존 코드 구조 개선을 같이 계획하지 않으면 구현 난이도가 급증한다.
- **문서 중복은 유지보수 부채**: 같은 내용을 두 문서에 쓰면 하나는 반드시 stale해진다.
