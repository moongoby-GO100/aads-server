# 오류노트: 현재 자동 지시 누락 후 과거 응답이 완료 처리된 사고

- 기록일: 2026-09-21
- 심각도: P0
- 상태: 수정·배포·운영 검증 완료
- 세션: `ac5278a7-2f13-4cd7-9aa1-83d41fb23c97`
- 사용자 메시지: `8f618f56-91f0-4eb6-ae57-2c5f9c522aac`
- 잘못 저장된 assistant: `276f948d-f065-4ce3-b886-629393aa0605`
- 실행: `0ca8fa07-d39d-421b-8530-8cc24eaf729c`
- Ohvis 작업: `dd50c1eb-e26a-4dc2-9b9c-931706c43598`
- PRD: `docs/plans/20260921_SYSTEM_TRIGGER_CONTEXT_FENCING_PRD.md`

## 증상

사용자는 목표 등록, 기획/설계 PRD 작성·저장, 작업 세분화, 마일스톤 등록과 진행을 지시했다. 화면에는 작업이 진행되는 듯 보였지만 최종 응답은 과거 질문인 “브라우저 테스트 시 탭을 무한정 여는 문제”에 대한 내용이었다. 그럼에도 Ohvis 작업은 `done`으로 기록됐다.

## 영향

- 현재 핵심 지시가 모델 입력에서 누락됐다.
- 오래된 질문의 답이 현재 작업 결과로 저장됐다.
- 일부 부수효과(PRD 커밋과 goal document)는 정상 수행됐지만 사용자에게 연결된 최종 보고는 무관했다.
- 올바른 지시로 시작된 재개 worker가 구 producer의 잘못된 완료 처리 때문에 fence-out 됐다.
- 허위 `done` 상태가 작업 카드와 운영 판단을 오염시켰다.

## 확정 근거

### 1. 현재 지시 누락

- `intent_override` 메시지는 `intent='system_trigger'`, `is_hidden=true`로 저장된다.
- 정상 히스토리 쿼리는 `_HISTORY_EXCLUDED_INTENTS`로 `system_trigger`를 제외한다.
- 이후 코드는 현재 message ID를 기존 `raw_messages`에서 찾아 내용만 바꿨으며, 필터로 사라진 경우 추가하지 않았다.
- 사고 provenance의 `current_user_message_id`는 맞았지만 `latest_raw_history_ids`에 해당 ID가 없었다.
- 모델이 받은 마지막 user 메시지는 2026-09-18의 브라우저 탭 질문이었다.

7일 표본에서 provenance가 존재하는 `system_trigger` 실행 1,303건 모두 현재 user message ID가 raw history에 없었다. 이는 특정 세션이 아니라 구조적 결함이다.

### 2. heartbeat 우회

- HTTP 채팅 라우트는 `with_background_completion()`을 사용한다.
- `trigger_ai_reaction()`과 큐 소비기는 `send_message_stream()`을 직접 순회했다.
- 따라서 `_active_bg_tasks` 등록과 독립 heartbeat가 없었고, 모델 대기 48.6초 뒤 scanner가 stale로 회수했다.
- generation 1/epoch 1은 superseded 되고 generation 2/epoch 2 재개가 시작됐다.

7일 `system_trigger` 1,320건 중 1,226건(92.9%)이 owner epoch 2 이상이었다.

### 3. 구 producer의 새 epoch 채택

- `_interim_save_streaming()`은 DB의 최신 epoch를 읽은 뒤 `state['owner_epoch']`에 덮어썼다.
- `_save_and_update_session()`은 producer가 캡처한 epoch가 아니라 프로세스 전역 `_execution_owner_epochs`를 읽었다.
- 재개 claim이 전역 값을 epoch 2로 바꾸자 epoch 1 producer도 자신을 epoch 2 소유자로 오인했다.
- 구 producer가 11:12:23 KST에 오답을 최종 저장했고, 올바른 generation 2 worker는 11:12:31에 lease lost로 종료됐다.

### 4. 의미 검증 없는 Ohvis 완료

- 자동 반응 후 “트리거 시작 시각 이후 가장 최근 assistant”를 조회했다.
- 응답 문자열만 있으면 지시 관련성, exact execution, provenance와 관계없이 `done` 처리했다.
- response critic timeout 뒤에도 완료가 계속됐다.
- 비동기 self evaluator는 이미 완료된 뒤 0.355점, `failure_type='지시_위반'`을 기록했지만 상태를 되돌리지 못했다.

7일 평가 439건 중 182건이 0.5 미만이었고, 그중 177건이 이미 completed였다. `지시_위반`은 99건이었다.

### 5. 라우팅 증폭 요인

- 현재 지시가 빠진 문맥은 `status_check` 후속 질문으로 오분류됐다.
- DB 기본은 `claude-opus-5`였으나 cascade 정책으로 Sonnet 계열에 내려갔다.
- 모델 강등은 최초 원인이 아니지만, 누락된 문맥에 대한 짧은 과거 답을 더 쉽게 확정했다.

## 사고 시간선(KST)

- 11:08:17 — 올바른 자동 지시 저장
- 11:08:40 — 현재 지시 없는 prompt 컴파일, `status_check`, Sonnet 시작
- 11:08:54 — 과거 브라우저 문제 도구 호출
- 11:09:06 — stale reclaim, generation 1 superseded, generation 2 claim
- 11:09:09 — 재개 worker는 올바른 현재 지시로 Opus 실행
- 11:10:37 — PRD 커밋 `65e8c2b4`
- 11:11:12 — goal document 등록
- 11:12:23 — 구 producer가 무관 응답을 최종 승격하고 Ohvis `done`
- 11:12:31 — 올바른 worker fence-out
- 11:13:51 — self evaluator 0.355, `지시_위반`

## 즉시 수정

1. 필터 후 현재 message ID를 마지막 user 턴으로 정확히 1회 재결합한다.
2. provenance에 포함 여부, 중복 수, 마지막 위치를 기록한다.
3. 모든 내부 자동 반응을 `with_background_completion()`으로 감싼다.
4. producer의 execution epoch를 태스크 로컬 `ContextVar`에 캡처한다.
5. 중간/최종 저장은 캡처 epoch와 DB epoch가 같을 때만 허용한다.
6. placeholder UPSERT 문장 자체에도 owner/epoch 조건을 둔다.
7. 최종 assistant에 execution generation ID를 스탬프한다.
8. Ohvis 완료는 exact execution, exact assistant, prompt binding, instruction alignment를 모두 검증한다.
9. 복잡 자동 지시의 내부 route intent를 `execute`로 고정한다.
10. 사고 Ohvis 작업의 허위 `done`을 `error`로 보정한다.

## 재발 방지

- 과거 노이즈 필터와 현재 턴 결합은 별도 단계로 유지한다.
- 전역 캐시는 write fence 증명으로 사용하지 않는다.
- 소유권 검증과 DB mutation은 가능한 한 같은 SQL 문 안에서 수행한다.
- 작업 완료는 문자열 존재가 아니라 execution/provenance/semantic evidence의 결합으로 판단한다.
- 비동기 품질 평가 결과만으로 이미 확정된 완료를 사후 감시하지 않는다. 선행 결정 게이트를 둔다.
- 회귀 테스트에서 “구 producer epoch 1, DB epoch 2”를 고정 시나리오로 유지한다.

## 데이터 보정 계획

- Ohvis 작업 `dd50c1eb-e26a-4dc2-9b9c-931706c43598`를 `error`로 변경한다.
- result/judgement에 `current_instruction_omitted_and_stale_generation_promoted`를 남긴다.
- 잘못된 assistant 메시지는 감사 추적을 위해 물리 삭제하지 않는다.
- 해당 execution과 provenance는 사고 증거로 보존한다.

## 검증 기록

- 2026-09-21: 신규 무결성 테스트 7개와 기존 연속성 테스트 3개, 총 10개 통과.
- 2026-09-21: 최종 저장 회귀 테스트 3개 통과.
- 2026-09-21: 채팅·lease·generation 관련 통합 회귀군 169개 통과. 별도 1개는 현재 compose가 `/host/aads-server/docs`를 사용하지만 기존 테스트가 `/app/docs`를 기대하는 기준 불일치로 실패했으며 이번 변경과 무관하다.
- 2026-09-21: 수정 커밋 `23a0636f31df7ced4b006351481e49ee225de7a3` push 완료. 이후 최신 main `74fa07bdf925`도 이 커밋의 후손임을 확인했다.
- 2026-09-21: 배포 원장 `4931`, active `aads-server:8100`, standby `aads-server-green:8102`로 Blue/Green 배포 및 동일 digest 동기화 완료.
- 2026-09-21: 양 슬롯 모두 image digest `sha256:7cf974edd1ca636526cd31679caccd38169146803ce5ae5ccb43cf702a7f3ee6`, health 정상. 원장 상태는 `success_partial -> success`로 인증됐다.
- 2026-09-21: 외부 HTTPS health `status=ok`, `graph_ready=true`; 배포 내장 300초 release P0/P1 감시 이상 없음.
- 2026-09-21: 사고 Ohvis 작업과 task card를 `error`로 보정하고 `current_instruction_omitted_and_stale_generation_promoted`를 기록했다.
- 2026-09-21: 배포 전 추적 실행 7건은 동일 execution 재개 2건, 정상 완료 2건, partial 보존 후 새 current execution 승계 3건으로 전부 연결됐다. 소실 0건, 세션별 중복 active 0건, terminal 유효 lease 0건이다.
- 2026-09-21: 마지막 구 슬롯 실행 `7eb645e3`(세션 `2648cf77`)은 owner `aads-server-green -> aads-server`, epoch `4 -> 5`로 회수됐다. 최종 live execution 8건은 모두 새 active 슬롯에서 실행 중이고 standby live execution은 0건이다.
- 2026-09-21: 별도 운영 경보 `disk_full`이 86.3%에서 발생했다. 실제 여유 공간은 27GB였으며 이번 릴리스 기능 오류와는 무관하지만 용량 정리 후속 조치가 필요하다.

## 오류 사전 등록 값

- 제안 key: `chat.system_trigger_current_turn_generation_fence`
- symptom: 자동 지시 대신 과거 질문 응답이 저장되고 Ohvis 작업이 done 처리됨
- cause: 현재 system_trigger가 히스토리 필터에서 제거되고, 내부 스트림이 heartbeat wrapper를 우회했으며, 구 producer가 전역 epoch 캐시로 새 lease를 채택하고, task 완료가 의미 검증 없이 처리됨
- prevention: 현재 턴 ID 재결합, 모든 내부 스트림 공통 wrapper, task-local immutable epoch fence, exact execution/provenance/semantic 완료 게이트
- fix commit: `23a0636f31df7ced4b006351481e49ee225de7a3`
- 등록 key: `chat.system_trigger_current_turn_generation_fence`
