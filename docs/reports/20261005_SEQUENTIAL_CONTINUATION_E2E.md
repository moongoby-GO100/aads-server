# SEQUENTIAL-CONTINUATION-E2E (2026-10-05) — 러너 종결 후 자동 검토 / 내부 결과 보고 검증

TASK_ID: AADS-SEQUENTIAL-CONTINUATION-E2E-20261005 · worktree `/tmp/aads-wt-runner-4e443ba6` · 세션 `8bf0405a`

**운영 관측**(읽기 전용 SELECT / 컨테이너 파일 해시)과 **격리 테스트**(인메모리 fake DB)를 구분해 적는다.
이번 작업에서 DB·서비스·컨테이너를 바꾼 것은 없다. 장애 주입·재시작·전역 자동승인 enable 없음.

## 0. STEP 0 — 기존 구현 분류

| 항목 | 분류 | 비고 |
|---|---|---|
| `pipeline_runner.notify_completion` / `_enqueue_terminal_followup` / 멱등키 `runner_terminal:<job>:<status>:<sha>` | 유지 (READ_ONLY) | 81bdcc6c 에서 들어온 구현. 수정 없음 |
| `chat_service.enqueue_next_step_reaction` / `_process_deferred_reactions_once` / `trigger_ai_reaction` | 유지 (READ_ONLY) | 소비 쪽 계약만 테스트로 고정 |
| `chat_service._claim_resume_model_attempt` (재시도 예산 청구 단일 지점) | 유지 (READ_ONLY) | 테스트로 고정 |
| `tests/unit/test_runner_terminal_continuation_e2e.py` | **신규** | 적재+소비를 한 DB 로 잇는 통합/계약 테스트 14개 |
| `docs/reports/20261005_SEQUENTIAL_CONTINUATION_E2E.md` | 신규 | 이 문서 |
| 삭제 | 없음 | 호출처 영향·롤백 해당 없음 (롤백 = 신규 파일 2개 삭제) |

지시서에 없는 파일: 테스트 파일 1개를 추가했다. 사유 — 지시서가 "계약/통합 테스트로 검증"을 요구하는데 기존 테스트는 적재(notify)와 소비(drain)를 따로만 고정해 둘을 잇는 검증이 없었다. 앱 코드는 수정하지 않았다.

## 1. 운영 적용 SHA 확인 (운영 관측)

| 항목 | 값 |
|---|---|
| runner-f0ab94ee | status=done, commit `81bdcc6c0a40…` (push_only_by_directive) |
| 실행 컨테이너 | `aads-server`, `aads-server-green` 이미지 `aads-server:81bdcc6c0a40`, healthy, Up 17h |
| 컨테이너 vs 저장소 | `app/api/pipeline_runner.py` sha256 `eb825254c7e85928…`, `app/services/chat_service.py` `9010a177adcace3b…` — **컨테이너 / HEAD / 81bdcc6c 세 곳 모두 동일** |
| 81bdcc6c 이후 두 파일 변경 | 없음 (`git log 81bdcc6c..HEAD -- <두 파일>` 빈 결과) |

→ 최신 배포본이 81bdcc6c 이고 종결 후속 큐 코드를 포함한다. (운영 배포는 이번 작업이 한 것이 아니다.)

## 2. 실제 종결 이벤트 trace (운영 관측, 세션 8bf0405a, KST 2026-10-05)

A = runner-51aca7ee(NODEPLOY-COMPAT-GUARD), B = runner-68acf38e(CONTROL-VERIFY), C = runner-a0be8138(ARTIFACT-RECOVER, 참고).

| 단계 | A | B | C |
|---|---|---|---|
| job done (updated) | 01:54:53 (`9bc1ebfa`) | 01:55:48 (`2abdebdb`) | 02:03:01 (`f06de685`) |
| 큐 적재 `runner_terminal:<job>:done:<sha>` | 01:54:54 | 01:55:49 | 02:03:03 |
| 검토 턴 시작 (user `system_trigger` 메시지) | 02:00:00 | 02:01:10 | 02:03:51 |
| 검토 응답 (assistant `runner_response`, 원 세션) | 02:00:01 시작 응답 저장 · 실행 `bf114095` 완료 02:01:08 | 실행 `d9356f15` 완료 02:01:43 | 실행 `93bf8b3b` 완료 02:04:20 |
| 큐 행 최종 | completed, attempts=1 | completed, attempts=1 | completed, attempts=1 |

- **중복 0**: `runner_terminal:%` 키 중복 그룹 0건, `attempts>1` 0건 (전체 25건: completed 24 + 진행 중 1 → 조회 시점 이후 C 도 completed).
- **busy 사용자 턴 보존 (실측)**: A 는 적재 후 약 5분 대기했다. 같은 세션의 사용자 턴 `a4bb9f28`(01:45:49→01:59:56)이 live 였고, 그 턴은 정상 완료(`completed`, retry_count 0, epoch 1)했다. 검토 턴은 그 종료 4초 뒤(02:00:00)에 시작했다. B 는 A 의 검토 턴(02:00:00~02:01:08)이 끝난 직후(02:01:10) 시작 → 같은 세션 안에서 직렬화됐다.
- 종결 검토 전체 대기 시간(completed 25건): 평균 216초, 최대 643초 — 모두 attempts=1.
- 모든 검토 실행의 `retry_count=0`, `owner_epoch=1` — 검토 턴이 예산을 소비하거나 epoch 충돌을 겪지 않았다.

한계: 위는 정상 경로 관측이다. 중복 notify·슬롯 교체·크래시 재청구는 운영에 주입하지 않았고 §3 격리 테스트로만 확인했다.

## 3. 격리 테스트 (`tests/unit/test_runner_terminal_continuation_e2e.py`)

방식: 적재(`notify_completion`)와 소비(`_process_deferred_reactions_once` → 실제 `trigger_ai_reaction`)가 **같은 인메모리 DB** 를 공유한다. 모델 호출(`_consume_internal_reaction_stream`)만 스텁. SQL 은 실행되지 않고 fake 가 의미(후보 선택/청구/환불/소유 펜싱)를 파이썬으로 흉내 낸다 — SQL 문구는 소스 계약 테스트로 따로 고정했다.

| 완료기준 | 테스트 | 결과 |
|---|---|---|
| terminal event → durable 큐 → 검토 실행 → 원 세션 | `test_trace_terminal_event_to_session_review`, `test_error_terminal_event_is_reviewed_as_failure` | 통과 |
| 중복 notify → 동일 효과 1건 | `test_duplicate_notifies_yield_exactly_one_effect` (5회 동시 + 배달 후 지연 중복 → 큐 1행, 모델 호출 1) | 통과 |
| busy 사용자 턴 보존 | `test_live_user_turn_is_never_touched_and_review_waits` (12회 drain 해도 attempts 0, 실행 행 쓰기 0, 종료 후 1회 배달), `test_turn_that_starts_between_claim_and_launch_refunds_the_attempt`, `test_in_process_bg_task_defers_without_burning_budget` | 통과 |
| active slot / 재시작 내구성 | `test_standby_slot_never_claims_the_review`, `test_slot_replacement_between_notify_and_delivery_keeps_single_effect` (blue→green, 메모리 비움, notify 재전송) | 통과 |
| 소유 펜싱 (큐 행) | `test_crashed_delivery_is_reclaimed_once_and_live_claim_is_not_stolen` (리스 살아 있으면 가로채지 않음, 만료 뒤 1회 재청구 attempts=2), `test_refund_is_owner_fenced_…` | 통과 |
| epoch 펜싱 + 모델 호출 직전에만 retry 증가 (실행 단위) | `test_retry_budget_is_charged_only_by_the_epoch_holder_at_model_call` (옛 epoch / 다른 owner / 종결 실행은 `ResumeFencedOut` + 예산 불변, 진짜 소진은 `ResumeAttemptLimitExceeded`), `test_charge_site_is_single_and_sits_between_fence_check_and_model_call` | 통과 |
| SQL 문구 계약 | `test_deferred_claim_sql_contract_busy_session_and_budget` (live lease NOT EXISTS, `attempts<8`, SKIP LOCKED, 환불 ≥2곳, 소유자 펜싱 ≥3곳, 실행 행 UPDATE 없음) | 통과 |
| 외부 알림 0 | 모든 시나리오 teardown 에서 push/telegram/소켓 connect 시도 0 단언, `test_terminal_followup_path_has_no_external_sender_reference` | 통과 |

**테스트 검출력 확인**: `_process_deferred_reactions_once` 의 환불 SQL 한 곳을 일시 훼손하자 `test_in_process_bg_task_defers_without_burning_budget` 와 SQL 계약 테스트가 실패했고, 원복 후 통과했다 (`git status` 로 `chat_service.py` 무변경 확인).

## 4. 검증 명령과 실제 결과

| 명령 | 결과 |
|---|---|
| `bash scripts/run_unit_tests.sh tests/unit/test_runner_terminal_continuation_e2e.py` | 14 passed |
| 위 + `test_pipeline_terminal_followup`, `test_stale_approval_trigger_guard`, `test_execution_lease_contract`, `test_chat_resume_owner_fence_v2`, `test_pipeline_runner_notify_contract`, `test_deferred_reaction_stale_jobs`, `test_standby_session_ownership` | **98 passed** (실패 0) |
| `ruff check --select F821,F811` (신규 테스트) | All checks passed |
| `python3 -m compileall -q` (신규 테스트) | OK |
| `python3 scripts/dup_guard.py --paths <신규 테스트>` | rc=0 |
| 전체 `tests/unit` 스위트 | **실행하지 않음** (관련 8개 파일만). 직전 보고서의 무관 실패 `test_sdk_injection_does_not_kill_the_turn` 의 현재 상태는 이번에 확인하지 않았다 |
| `scripts/error_book.py match` | 실행 불가: 이 환경에 `psql` 이 없다 (`No such file or directory: 'psql'`). 조회는 `docker exec aads-postgres psql` 로 직접 했다 |
| 빌드/배포/재시작 | 하지 않음. 승인 후 Runner 빌드 검증 대상은 없음 (앱 코드 무변경) |

## 5. 판정

**통과 (조건부)**: 종결 이벤트 → durable 큐 → 검토 실행 → 원 세션 결과 경로는 운영 실측(A·B·C 3건)과 격리 테스트 양쪽에서 계약을 만족한다. 이 경로에서 **이벤트 키/phase/원인 근거의 실패는 발견되지 않았고, 최소 수정 대상도 없다.** D 단계를 막을 근거는 이 검증에서 나오지 않았다. 다만 아래 §6 의 위험은 D 이전에 인지해야 한다.

"종결 status done" 자체를 성공 근거로 쓰지 않았다 — 근거는 큐 행의 키/attempts, 검토 턴의 `system_trigger` 메시지와 실행 행, 컨테이너 파일 해시다.

## 6. 남은 위험 (소스 읽기 근거 — 테스트·운영 재현 없음)

1. **watchdog 의 이중 청구 가능성** — `app/main.py:996` 의 재개 스캐너는 실행을 `retrying` 으로 바꾸며 `retry_count = retry_count + 1` 을 올리고, 이후 재개가 모델을 부를 때 `_claim_resume_model_attempt` 가 다시 +1 한다. "실제 모델 호출 직전에만 청구" 계약(chat_service·`routers/chat.py:646` 주석)과 어긋난다. 종결 검토 턴이 중단돼 watchdog 으로 재개될 때만 해당하며 이번 운영 trace 3건에는 없었다(retry_count 0). 수정은 별도 작업 후보.
2. **큐 행 completed 의 의미** — `_finish_deferred_reaction` 은 태스크가 끝나면(내부 `_consume_stream` 이 예외를 삼키므로 모델 호출이 실패해도) `completed` 로 기록한다. 즉 completed = "배달 시도 종료"이지 "검토 응답 검증됨"이 아니다. 실패한 턴의 복구는 실행 watchdog 책임이다. 이번 3건은 실행 행이 `completed` 이고 assistant 응답이 저장돼 있어 확인됐다.
3. **채팅 완료 web push** — `chat_service.py` 의 assistant 메시지 저장 직후 `notify_chat_response_complete`(Web Push, 앱 자체 푸시)가 호출된다. 검토 턴(`intent_override="auto_reaction"`)에서 건너뛰는 조건을 소스에서 찾지 못했다. 이번 테스트는 모델 호출을 스텁해 이 경로를 실행하지 않았다. Telegram/Slack 등 외부 sender 호출은 종결 후속 경로(`_enqueue_terminal_followup`, `enqueue_next_step_reaction`, drain)의 소스에 없고 테스트에서도 0건이지만, **web push 가 "외부 알림 0" 정책에 해당하는지는 CEO 판단이 필요하다.** (이 push 는 이번 변경이 아니라 기존 동작이다.)
4. 재시작 내구성은 격리 테스트로만 확인했다 (운영 재시작 금지 준수).

## 7. 커밋·푸시·배포 구분

- 실제 커밋/푸시: **하지 않음** (지시상 Runner 가 승인 후 수행). 따라서 커밋 SHA·GitHub URL 은 아직 없다.
- 배포: 안 함 (DEPLOY false). 서비스 재시작 안 함.
- `project_handover_entries` / `HANDOVER.md` 기록: **미수행** — DB 쓰기와 공유 문서 수정은 이 작업 범위(TARGET_FILES)에 없다. 승인·커밋 후 원 세션 보고와 함께 기록해야 한다.
- 오류 사전: 새로 확인된 원인이 없어 등록하지 않았다.
- 비용: 외부 LLM 호출 없음(코드·테스트·읽기 전용 조회). $5 한도 미달, 실제 측정치는 0 으로 알고 있음(별도 계측하지 않음).
