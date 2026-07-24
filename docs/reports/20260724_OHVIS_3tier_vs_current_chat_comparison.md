# OHVIS 3-Tier Response Architecture vs 현재 채팅 시스템 비교 보고서

> **작성일**: 2026-07-24 KST  
> **작성자**: OHVIS CTO AI  
> **대상**: CEO moongoby  
> **근거**: 서버68 실측 (DB 조회, 소스코드 분석, 대시보드 컴포넌트 확인)

---

## 1. 요약

현재 채팅 시스템은 **SSE 스트리밍, 아티팩트 자동 추출, 러너 완료 트리거, 작업 모니터 UI** 등 기반 인프라 약 **65%가 이미 구현**되어 있다. 그러나 핵심 연결 고리 3가지(즉시 응답 분리, 결과 카드 격리, 오비스 자동 판단)가 빠져 있어 CEO 체감상 **"느리고, 중간 과정이 불투명하고, 결과가 뒤섞인다"** 문제가 발생한다.

---

## 2. 현재 채팅 시스템 실측 현황

| 항목 | 실측값 | 출처 |
|---|---|---|
| 총 세션 수 | 192개 (역할 지정 141개) | [DB 조회] chat_sessions |
| 총 메시지 | 45,301건 (user 16,083 / assistant 29,210 / system 8) | [DB 조회] chat_messages |
| 아티팩트 | 23,484건 (table 11,362 / report 6,280 / full_response 5,597 / code 179 / image 42) | [DB 조회] chat_artifacts |
| SSE 스트리밍 | `with_background_completion()` — disconnect에도 DB 저장 보장, 8초 heartbeat | [코드 확인] chat_service.py:3490 |
| 인텐트 분류 | intent_router 기반 자동 분류 (auto_reaction, system_trigger 등) | [코드 확인] chat_service.py:8192 |
| 러너 완료 트리거 | `trigger_ai_reaction()` — 러너/에이전트 완료 시 세션에 시스템 메시지 삽입 + AI 자동 반응 | [코드 확인] chat_service.py:7182 |
| 러너 트리거 호출 지점 | pipeline_runner_service.py 내 6개 이상 위치에서 trigger_ai_reaction 호출 | [코드 확인] pipeline_runner_service.py |
| QA 자동 보고 | `auto_report_on_completion()` — 러너 완료 시 QA 자동 수행 | [코드 확인] pipeline_runner_service.py:756 |
| ohvis_tasks 테이블 | 생성 완료 (15개 컬럼), CRUD API 6개 등록, 데이터 0건 (미연동) | [DB 조회] ohvis_tasks |
| 대시보드 컴포넌트 | 38개 파일 (ChatBubble, ChatInput, ChatStream, ArtifactTaskMonitor 등) | [서버 확인] aads-dashboard/src/components/chat/ |
| bg_task 관리 | `_active_bg_tasks` 딕셔너리로 세션별 백그라운드 태스크 추적 | [코드 확인] chat_service.py:160 |

---

## 3. 현재 채팅 vs 3-Tier 아키텍처 상세 비교

### 3-1. 응답 흐름 비교

| 구분 | 현재 채팅 | 3-Tier 아키텍처 (제안) | 변화 |
|---|---|---|---|
| **응답 시작** | LLM이 도구 호출·분석 전부 끝낸 후 첫 토큰 출력 (수십 초~수 분) | 인텐트 분류 즉시 계획/핵심 답변 선행 (0~5초) | CEO 대기 시간 대폭 단축 |
| **긴 작업 처리** | 채팅 스레드에서 직렬 처리, 완료까지 다음 질문 불가 | 즉시 계획 응답 → 백그라운드 위임 → CEO는 다른 질문 가능 | 동시 작업 가능 |
| **중간 진행상황** | SSE 스트리밍으로 토큰 단위 출력만 존재 | 단계별 progress card + 실시간 step 업데이트 | 뭘 하고 있는지 시각적으로 파악 |
| **결과 표시** | 러너/에이전트 결과가 `role=system` 메시지로 대화 중간에 삽입 | 별도 아티팩트 카드(TaskCard)로 분리, 대화 흐름과 격리 | 맥락 뒤섞임 해소 |
| **완료 알림** | 없음 (CEO가 "됐나?" 물어야 확인 가능) | 완료 시 오비스가 자동 트리거받아 판단 요약을 카드에 기록 | 능동적 보고 |
| **작업 추적** | pipeline_jobs + project_tasks (별도 관리 화면) | ohvis_tasks 통합 + 세션 내 TaskCard로 한 눈에 확인 | 채팅 내 작업 관리 |

### 3-2. 기능별 상세 비교

| 기능 | 현재 상태 | 3-Tier 필요 | 갭 | 구현 난이도 |
|---|---|---|---|---|
| SSE 스트리밍 | ✅ 구현 완료 (heartbeat, disconnect 보호) | 그대로 활용 | 없음 | — |
| 인텐트 분류 | ✅ intent_router 기반 | Tier 1 분기 로직 추가 | 분류 후 즉시 응답 분리 로직 | S |
| 러너 완료 트리거 | ✅ trigger_ai_reaction 구현, 6+ 호출 지점 | ohvis_tasks 연동 추가 | 트리거 시 ohvis_tasks 상태 갱신 + 카드 갱신 | M |
| QA 자동 보고 | ✅ auto_report_on_completion | 오비스 판단 로직 연동 | 판단 결과를 ohvis_tasks.ohvis_judgement에 저장 | M |
| ohvis_tasks DB | ✅ 테이블 + CRUD API 생성 완료 | 실제 데이터 연동 | 러너/에이전트 시작 시 자동 생성, 상태 변경 시 자동 갱신 | M |
| 아티팩트 시스템 | ✅ 8종 타입 (table/report/code/image 등) | TaskCard 타입 추가 | 신규 아티팩트 타입 + 프론트 컴포넌트 | M |
| ArtifactTaskMonitor | ✅ 컴포넌트 존재 | TaskCard UI로 확장 | 단계별 progress, 판단 결과 표시 추가 | M |
| 즉시 응답 분리 | ❌ 미구현 | Tier 1 핵심 | 인텐트 분류 → 즉시 계획 응답 → 나머지 백그라운드 | L |
| 아티팩트 카드 격리 | ❌ 미구현 (시스템 메시지로 삽입) | Tier 2 핵심 | 결과를 chat_messages가 아닌 TaskCard에 격리 | L |
| 오비스 자동 판단 | ❌ 미구현 (ohvis_judgement 필드만 존재) | Tier 3 핵심 | 완료 트리거 → AI 판단 → 요약 카드 생성 | L |
| parent_turn_id | ❌ 미구현 | 어떤 지시의 결과인지 추적 | ohvis_tasks에 parent_turn_id 컬럼 추가 | S |
| 멀티태스크 큐 UI | ❌ 미구현 | 동시 진행 작업 시각화 | 프론트엔드 TaskCard 목록 패널 | M |
| 다중 작업 동시 진행 | ⚠️ 부분 (bg_task 1개 + trigger_ai_reaction 큐) | 세션당 N개 병렬 | bg_task 멀티슬롯 확장 | L |

---

## 4. 장점과 단점 비교

### 4-1. 현재 채팅 시스템

| 장점 | 단점 |
|---|---|
| ✅ SSE 스트리밍 안정 (disconnect 보호, heartbeat) | ❌ 응답 시작까지 긴 대기 (도구 호출 완료 후 출력) |
| ✅ 아티팩트 자동 추출 (23,484건 누적) | ❌ 러너/에이전트 결과가 대화 맥락에 섞여 혼란 |
| ✅ 러너 완료 트리거 이미 구현 | ❌ 완료 알림 없음 — CEO가 직접 확인해야 함 |
| ✅ 인텐트 분류 기반 라우팅 | ❌ 한 번에 하나만 처리 — 동시 질문 불가 |
| ✅ 45,301건 메시지 운영 검증됨 | ❌ 작업 진행상황 불투명 — "지금 뭐하는 거지?" |
| ✅ 38개 UI 컴포넌트 안정 운영 | ❌ 작업별 비용/시간 추적 불가 |

### 4-2. 3-Tier 아키텍처 (개선 후)

| 장점 | 단점/리스크 |
|---|---|
| ✅ 즉시 응답 (0~5초) — CEO 대기 해소 | ⚠️ 즉시 응답의 정확도가 낮으면 오히려 혼란 |
| ✅ 백그라운드 병렬 — 동시 질문 가능 | ⚠️ 병렬 작업 간 충돌 관리 필요 (같은 파일 수정 등) |
| ✅ 결과 카드 격리 — 맥락 보존 | ⚠️ UI 복잡도 증가 (카드 + 대화 동시 관리) |
| ✅ 오비스 자동 판단 — 능동적 보고 | ⚠️ 자동 판단 비용 (LLM 추가 호출) |
| ✅ 작업별 비용/시간 추적 (ohvis_tasks) | ⚠️ DB 부하 증가 (상태 변경마다 INSERT/UPDATE) |
| ✅ parent_turn_id로 지시↔결과 추적 | ⚠️ 기존 trigger_ai_reaction 흐름 리팩터링 필요 |

---

## 5. 현재 기능 완성도 평가

### 5-1. 기반 인프라 (구현 완료 — 약 65%)

| 영역 | 완성도 | 근거 |
|---|---|---|
| SSE 스트리밍 | 100% | with_background_completion, heartbeat, disconnect 보호 |
| 인텐트 분류 | 90% | intent_router 기반 분류 + intent_override 메커니즘 |
| 러너 완료 트리거 | 85% | trigger_ai_reaction 구현, 큐잉, 재귀방지, TTL 만료 |
| 아티팩트 시스템 | 80% | 8종 타입, DB 23,484건, 프론트 6개 컴포넌트 |
| ohvis_tasks API | 70% | DB + CRUD 6개 완성, 데이터 연동 미완 |
| QA 자동 보고 | 75% | auto_report_on_completion 구현, 오비스 판단 미연동 |

### 5-2. 핵심 연결 고리 (미구현 — 약 35%)

| 영역 | 완성도 | 미구현 사항 |
|---|---|---|
| Tier 1: 즉시 응답 분리 | 0% | 인텐트 분류 후 즉시 계획 응답 선행 로직 |
| Tier 2: 결과 카드 격리 | 10% | TaskCard 아티팩트 타입 + 프론트 컴포넌트 (ArtifactTaskMonitor는 존재하나 연동 미완) |
| Tier 3: 오비스 자동 판단 | 5% | ohvis_judgement 필드만 존재, 판단 로직/트리거 연동 없음 |
| parent_turn_id 추적 | 0% | 지시↔결과 연결 컬럼 및 로직 |
| 멀티태스크 큐 UI | 0% | 동시 작업 목록 패널 |
| 동시 다중 작업 | 20% | bg_task 1슬롯, trigger_ai_reaction 큐 존재하나 병렬 미지원 |

### 5-3. 전체 완성도 종합

```
기반 인프라 (65%)  ████████████████████░░░░░░░░░░  
핵심 연결 (6%)     █░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  
────────────────────────────────────────────────────
전체 3-Tier 완성도  약 40%
```

---

## 6. 구현 우선순위 (권장)

| 순서 | 항목 | 크기 | 기대 효과 | 의존성 |
|---|---|---|---|---|
| **P0-1** | ohvis_tasks ↔ 러너/에이전트 연동 | M | 작업 추적 기반 확보 | 없음 |
| **P0-2** | 완료 시 오비스 자동 판단 + 카드 갱신 | M | "됐나?" 질문 불필요 | P0-1 |
| **P1-1** | 즉시 응답 분리 (Tier 1) | L | CEO 대기 시간 0~5초로 단축 | 없음 |
| **P1-2** | TaskCard 아티팩트 + 프론트 UI | M | 결과 맥락 분리 | P0-1 |
| **P1-3** | parent_turn_id 추적 | S | 지시↔결과 연결 | P0-1 |
| **P2-1** | 멀티태스크 큐 UI | M | 동시 작업 시각화 | P1-2 |
| **P2-2** | 다중 작업 동시 진행 (bg_task 멀티슬롯) | L | 진정한 병렬 처리 | P1-1 |

---

## 7. 이전 시도 실패 원인과 해결 방향

| 실패 항목 | 원인 | 해결 방향 |
|---|---|---|
| 완료 메시지가 대화 사이에 끼어듦 | `trigger_ai_reaction`이 `role=system` 메시지를 chat_messages에 직접 삽입 | 결과를 ohvis_tasks + TaskCard 아티팩트로 격리, chat_messages에는 "작업 완료 알림" 1줄만 |
| 여러 러너 결과가 뒤섞임 | parent_turn_id 없이 시간순 삽입 | ohvis_tasks.parent_turn_id로 지시↔결과 연결 |
| 완료 알림이 대화 맥락을 끊음 | 시스템 메시지가 user/assistant 사이에 끼어듦 | TaskCard는 대화 흐름과 별도 레인(사이드/접힘)으로 표시 |
| 오비스가 결과를 스스로 확인 안 함 | 트리거가 단순 텍스트 삽입 | 완료 트리거 → 오비스가 결과 확인 → ohvis_judgement에 판단 기록 → 카드 갱신 |

---

## 8. 결론

| 판정 항목 | 현재 상태 |
|---|---|
| 기반 인프라 | ✅ 65% 구현 완료 — 추가 개발 토대 충분 |
| 핵심 연결 고리 | ❌ 6% — 3가지 핵심 기능 미구현 |
| 전체 3-Tier 완성도 | ⚠️ 약 40% |
| P0 구현 예상 소요 | 2~3일 (ohvis_tasks 연동 + 자동 판단) |
| P0+P1 전체 예상 소요 | 5~7일 |
| CEO 체감 개선 예상 | 응답 대기 수 분 → 5초, "됐나?" 질문 불필요, 맥락 뒤섞임 해소 |
