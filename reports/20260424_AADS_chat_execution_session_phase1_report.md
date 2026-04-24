# AADS Chat Execution Session Phase 1 Report

작성일: 2026-04-24 KST

## 백업

- 스냅샷 디렉터리: `/root/aads/aads-server/reports/backups/20260424_chat_execution_session`
- 백업 파일:
  - `chat_service.py.pre_apply`
  - `main.py.pre_apply`
  - `model_selector.py.pre_apply`
  - `router_chat.py.pre_apply`
  - `test_chat_service.py.pre_apply`
  - `test_model_selector_dynamic_routing.py.pre_apply`

## 이번 1차 반영 범위

- `recovered_from_redis`를 recovery 계열로 함께 취급
- recovery 계열 중복 버블을 조회 시 가장 긴 1건으로 dedupe
- 서버 재시작 resume 스캐너가 `recovered_from_redis`도 자동 이어쓰기 대상으로 인식
- stale recovered row가 새로운 user 턴을 다시 잡지 못하도록 `created_at` 가드 추가
- 새 응답 시작 시 세션 Redis stream 초기화로 이전 턴 토큰 혼입 완화
- Codex relay 오류 시 동일 모델 재시도 후에만 Gemini 폴백

## 구조 전환 작업 순서

### 1. DB 스키마

1. `chat_turn_executions` 테이블 추가
2. 컬럼:
   - `id`
   - `session_id`
   - `user_message_id`
   - `assistant_message_id`
   - `status`
   - `requested_model`
   - `actual_model`
   - `fallback_model`
   - `retry_count`
   - `last_event_id`
   - `started_at`
   - `completed_at`
3. `chat_sessions.current_execution_id` 추가 여부 결정
4. `chat_messages.reasoning_effort_used` 추가

### 2. API

1. 메시지 전송 응답에 `execution_id` 포함
2. `GET /chat/executions/{execution_id}` 추가
3. `GET /chat/executions/{execution_id}/events` 추가
4. `streaming-status`는 세션 추론 대신 execution 상태 조회로 전환

### 3. 서버 코드

1. 메시지 전송 시작 시 execution row 먼저 생성
2. Redis stream 키를 `session_id` 기준에서 `execution_id` 기준으로 변경
3. placeholder promote/delete 흐름을 줄이고 assistant row 1건 + 상태머신으로 단순화
4. resume 스캐너는 메시지 추론 대신 `status in ('running', 'retrying')` execution만 복구
5. requested/actual/fallback 모델을 분리 저장

### 4. 프론트 코드

1. 현재 세션 외에 현재 execution을 상태로 보관
2. 재진입 시 메시지 재조회보다 execution attach 우선
3. SSE replay 또는 WebSocket attach에 `last_event_id` 반영
4. 한 턴당 assistant bubble 1개 원칙으로 렌더 단순화

### 5. 회귀 테스트

1. recovery 계열 중복 dedupe
2. stale recovered row가 최신 user 턴에 붙지 않는지
3. Codex 동일 모델 재시도 후 fallback 동작
4. execution 단위 replay 이후 버블 1개 유지

## 남은 후속 과제

- Redis stream 자체를 execution 단위로 분리해야 세션 단위 혼입 문제가 근본적으로 사라진다.
- Codex 재시도는 현재 continuation prompt 기반이다. 터미널 PTY처럼 동일 프로세스를 재부착하는 구조는 아직 아니다.
- fallback 추적은 아직 이벤트/로그 중심이며, DB requested/actual model 분리는 다음 단계 작업이다.
