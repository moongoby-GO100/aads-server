# 목업 실행 게이트 제출 경로 전수와 PRD AC-M01~M22 대응표

- document_key: `aads-mockup-exec-gate-paths-and-ac-map-20261014` (kind=report, 초안) — **정본 미등록(미완료)**. 이 세션에는 정본 등록 도구가 없고 운영 DB 쓰기가 금지라 등록하지 못했다. 열람 링크 미제공.
- TASK_ID: AADS-RDOC-M8-MOCKUP-EXEC-GATE-20261014 (P1/L)
- 기준: origin/main `f346d1ba`(M8a 재작업 runner-097902b0 반영) + 이 작업의 변경. 직전 시도(runner-eaee44f2, 기준 `edd1e672`)의 산출물을 M8a 위에 다시 얹었다(§4). PRD 는 `docs/prd/20261003_AADS_RDOC_MOCKUP_REVIEW_PRD.md` §AC 표.
- 운영 모드: `MOCKUP_GATE_MODE` 는 여전히 어디에도 설정되지 않았다 → 기본 `shadow`. **이 문서의 "구현됨"은 enforce 로 올렸을 때 막는 코드와 테스트가 있다는 뜻이지 운영에서 지금 막고 있다는 뜻이 아니다.** 전환 절차: `docs/plans/20261014_AADS_목업_실행게이트_enforce_전환_절차.md`.

약칭: 규칙 = `app/services/mockup_gate_rules.py`, 서비스 = `app/services/mockup_review_service.py`, 러너API = `app/api/pipeline_runner.py`, CLI = `scripts/verify_mockup_approval.py`, 셸 = `scripts/pipeline-runner.sh`. 테스트: U2 = `tests/unit/test_mockup_execution_gate.py`, U8 = `tests/unit/test_mockup_exec_gate_m8.py`(신규), U3 = `tests/unit/test_mockup_gate_coverage.py`(M8a), U1 = `tests/unit/test_mockup_reviews.py`, I1 = `tests/integration/test_mockup_reviews_postgres.py`, I2 = `tests/integration/test_mockup_chat_flow.py`.

## 1. M8 에서 바뀐 것

| 항목 | 분류 | 내용 |
|---|---|---|
| `submit_batch` | 수정 | INSERT 트랜잭션 전에 job 마다 submit 단계 게이트(`_run_batch_mockup_gate`). 하나라도 거절되면 409 `mockup_approval_required`, `rejected=[{key, job_id, reasons}]`, `batch_rejected=true`, `jobs_submitted=0`. 부분 제출 없음. |
| `_run_batch_mockup_gate` | 신규 | 위 헬퍼. 단건 `_run_mockup_gate` 를 그대로 재사용. |
| 채팅 도구 DB fallback (`ToolExecutor._pipeline_runner_submit_db_fallback` / `_batch_db_fallback`) | 수정 | API 가 401/403 일 때 DB 로 직접 INSERT 하던 경로가 게이트를 완전히 건너뛰었다. 이제 게이트 대상 job 은 `gate_unavailable` 로 취급 → enforce 거절(배치 전체), shadow 는 로그 후 진행. 형식이 틀린 번들 선언은 모든 모드에서 거절. |
| `_mockup_fallback_denial` | 신규 | 위 판정 헬퍼(규칙의 `classify`·`unavailable_decision` 사용). 선언 번들은 shadow 에서도 거절(서버 검증 불가 = `gate_unavailable`). |
| 규칙 `unavailable_decision`·`stops_action`, 서비스 `gate_check`·`_decide`·`record_gate_decision`, 러너API `_run_mockup_gate` | 유지(M8a) | 직전 시도의 `mode_decision`(shadow 에서 선언 번들 검증 실패도 통과) 변경은 **버렸다** — M8a 의 계약(선언 번들은 모든 모드에서 검증, 멈춤 여부는 `blocking`)과 충돌. §4. |
| `verify_bundle`, `_hold_bindings`, `/pipeline/jobs/{id}/mockup-gate`, `/pipeline/mockup-gate/push` | 유지 | 변경 없음. |
| CLI | 수정 | `--classify-only`(exit 0 비대상 / 11 대상), `--offline`(CI 용). offline 의 변경 파일 입력은 M8a 의 `--changed-files` 와 `classify_paths` 를 그대로 쓴다(별도 플래그를 만들지 않음). `--job-id`/`--task-id` 는 이 두 모드에서만 생략 가능. 기존 exit 0/10/20 의미와 pre_push 동작 유지. |
| 셸 pre_execution | 수정 | 스크립트 장애(rc≠0/10/20) 시 `grep 'MOCKUP_REVIEW_ID\|aads-dashboard\|UI_TASK'` 로 통과시키던 분기 삭제. 새 `_mockup_gate_on_script_error` 가 `--classify-only` 로 서버 규칙 판정 → 비대상만 통과, 대상·판정불가는 enforce 거절 / shadow 통과 + `MOCKUP_GATE_SHADOW violation=gate_script_error` 로그. rc=0 이고 shadow 결과면 `MOCKUP_GATE_SHADOW` 로그. |
| `.github/workflows/ci.yml` | 신규 job `mockup-gate` | 변경 파일·커밋 메시지로 CLI `--offline` 실행, 경고만(`continue-on-error: true`, `MOCKUP_GATE_MODE: shadow`). 승인 여부는 판정하지 않는다. |

삭제: 셸의 문구 grep 통과 분기 1개(위). 호출처 없음(같은 함수 안 분기). 롤백은 이 커밋 revert.

## 2. job 생성·재실행 경로 전수 (grep `INSERT INTO pipeline_jobs`, `status='queued'` 재큐잉, 제출 도구)

| # | 경로 | 위치 | submit 게이트 | pre_execution 게이트(셸) | 판정 |
|---|---|---|---|---|---|
| 1 | 단건 제출 `POST /pipeline/jobs` (채팅 `pipeline_runner_submit`, ALLOW_DUP_JOB 재제출, SUPERSEDES 재제출, `goal_failure_retry`·`goal_dispatch` 가 채팅을 통해 내는 제출 포함) | 러너API `submit_job` | 적용(기존) | 적용 | 적용 |
| 2 | 배치 제출 `POST /pipeline/jobs/batch` (채팅 `pipeline_runner_submit_batch`) | 러너API `submit_batch` | **적용(M8 신규)**, 전체 거절 | 적용 | 적용 |
| 3 | 채팅 도구 DB fallback(단건·배치, API 401/403 때) | `app/services/tool_executor.py` | **적용(M8 신규)**, 서버 검증 불가이므로 `gate_unavailable` 규칙 | 적용 | 적용 |
| 4 | dedup 차단 기록 행·의존성 차단 기록 행 | 러너API `_record_dedup_blocked`, `_record_blocked_dependency` | 해당 없음 | 해당 없음 | 비실행 행(종결 상태로 INSERT, 워커 시작 안 함) |
| 5 | 자동 재작업 `AUTO_REWORK_OF` | `app/services/pipeline_auto_rework.py` `maybe_submit_auto_rework` | **미적용** (TenantContext 없이 notify 경로에서 DB 직접 INSERT) | 적용 — 원 지시서의 번들 선언을 그대로 담으므로 새 job_id 로 재검증된다 | **미지원 경로(submit 단계)**, 실행 전 게이트로만 보호 |
| 6 | 오류 자동수정 디스패처 (`autofix-*`, 5분 주기) | `app/services/auto_fix_dispatcher.py` | **미적용** | 적용 | **미지원 경로(submit 단계)**, 실행 전 게이트로만 보호. 지시서가 오류 템플릿이라 UI 메타 줄이 거의 없다 |
| 7 | 레거시 Pipeline C 상태 저장 | `app/services/pipeline_runner_service.py` `_save_to_db` | 미적용 | **해당 없음** — 셸 러너가 실행하지 않는 별도 엔진 | **미지원 경로** (Pipeline C 는 이 게이트 밖) |
| 8 | 같은 job 재큐잉: Redis 작업잠금 대기, orphan 복구, claim 해제, 의존성 해제 | 셸 4151·4384·4630·8251행, 러너API `_release_auto_file_dependency` 등 | 해당 없음(같은 job, 제출 때 검증됨) | 적용 — 다시 시작할 때 `run_job` 의 pre_execution 을 다시 지난다 | 적용 |
| 9 | 배포 잠금 대기 재큐잉 `deploy_lock_wait` | 셸 6686행 | 해당 없음 | 해당 없음(코딩 완료 후 배포 단계) | 워커 재실행 아님 |
| 10 | `retry-review`, `adjudicate-review`, `approve` | 러너API | 해당 없음 | 해당 없음 | 리뷰/승인 재판정이며 새 코딩 실행이 아님 |
| 11 | 이미 running 인 작업 (checkpoint) | 서비스 `verify_bundle(phase=checkpoint)`, `_hold_bindings` | — | 셸은 checkpoint 를 호출하지 않는다 | 보류/재검토 **표시만**(`review_required`/`held`, `binding_held`). kill/restart 없음 — U8 로 증명 |
| 12 | 채팅 직접 쓰기 도구 `write_remote_file`/`patch_remote_file`/`git_remote_commit` | `app/api/ceo_chat_tools.py` `_mockup_chat_gate` | `phase=chat_write`(M8a) | — | 적용(M8a). shadow 는 기록+경고, enforce 는 차단 |
| 13 | 채팅 `run_remote_command` 의 쓰기(`sed -i`, 리다이렉션), `git_remote_push` | — | — | — | **미지원 경로.** 명령 문자열을 해석하지 않는다 |
| 14 | 로컬 git push | `scripts/hooks/pre-push` | `phase=pre_push`(M8a) | — | 훅이 설치된 저장소만. `--no-verify`·훅 없는 클론은 **미지원 경로** |
| 15 | CI (GitHub Actions) | `.github/workflows/ci.yml` `mockup-gate` | 오프라인 선언 검사(M8 신규) | — | 승인 판정 못 함 — 선언 누락·형식 오류만 경고 |
| 16 | 터미널에서 사람/에이전트가 직접 편집 | — | — | — | **미지원 경로.** push 할 때 pre-push 가 마지막 방어선 |

## 3. PRD AC-M01~M22 대응표

상태: 구현됨 = 서버 코드+테스트 있음 / 부분 = 일부 조건만 / 미구현 / 미지원 경로 = 서버가 강제할 수 없는 경로.

| AC | 시나리오(요약) | 상태 | 근거 파일 | 테스트 |
|---|---|---|---|---|
| AC-M01 | 신규 화면 2개 중 하나만 목업 → 누락 screen_id, 제출 차단 | 구현됨 | 서비스 `completeness`(`mockup_missing`) | U1 `test_visual_evidence_required_per_screen`, `test_complete_modify_manifest_passes` |
| AC-M02 | Before 생략·로그인 오류 화면 → blocked_evidence | 구현됨 | 서비스 `completeness`(`before_missing`, `BLOCKED_BEFORE_SOURCES`) | U1 `test_login_screen_cannot_be_before_evidence_and_redaction_pending_blocks`; I1 `test_exception_contracts` |
| AC-M03 | backend-only 인데 사용자 영향 → UI 재분류 | 부분 | 서비스 `completeness`(backend-only 사유 요구), 규칙 `ui_task_reasons`(TARGET/TARGET_FILES/UI_TASK 줄만) | U1 `test_new_screen_needs_no_before_and_backend_only_needs_rationale`; U2 `test_ui_task_detection_uses_targets_not_prose_or_read_only_files`. 오류 메시지·순서 변경을 내용으로 판별하는 재분류는 없다 |
| AC-M04 | 모든 증거 첨부 → 검토 제출 가능, 구현은 여전히 불가 | 구현됨 | 서비스 `submit_review` vs `verify_bundle`(`not_approved`) | I1 `test_draft_save_review_submit_and_approval_are_distinct_steps` |
| AC-M05 | v1 수정 의견 → v2, v1 보존·v2 미승인 | 구현됨 | 서비스 `create_revision`, DB append-only | I1 `test_new_revision_does_not_inherit_approval_and_revisions_are_immutable`, `test_change_requests_preserve_text_links_and_outcome` |
| AC-M06 | v2 제출 후 v1 승인 → 409 | 구현됨 | 서비스 `_stale`, `approve_review` | I1 `test_concurrent_writers_produce_exactly_one_winner`, `test_new_revision_does_not_inherit_approval_and_revisions_are_immutable` |
| AC-M07 | 승인 파일 1바이트·문서 revision 변경 → 실행 차단 | 구현됨(enforce) | 서비스 `verify_bundle`(`asset_tampered`, `manifest_hash_mismatch`, `document_revision_changed`), `gate_check` | I1 `test_tampering_after_approval_blocks_execution`; U8 `test_bundle_contract_enforce_denies[manifest_tampered]`, `test_bundle_contract_shadow_still_denies_a_declared_bundle_and_records[manifest_tampered]`; U3 `test_ac_m07_*` |
| AC-M08 | 미승인 번들로 API/Runner/CLI 실행 → 같은 차단·감사 이벤트 | 부분 | API: `submit_job`·`submit_batch`·DB fallback, Runner: 셸 pre_execution→`/mockup-gate`, CLI | U2 `test_submit_gate_denies_with_409…`, `test_cli_exit_codes`; U8 `test_batch_runs_the_submit_gate_for_every_job_and_rejects_the_whole_batch`, `test_db_fallback_refuses_gated_jobs…`, `test_runner_script_error_decision`; U3 `test_ac_m08_every_path_gets_the_same_denial_and_an_audit_row`. 감사 이벤트는 리뷰가 존재할 때 `verify_denied`, 모든 적용 대상 판정은 `mockup_gate_decisions`(M8a, migration 운영 미적용). 자동 재작업·autofix 의 submit 단계, `run_remote_command`·터미널은 §2 미지원 경로. 운영은 shadow |
| AC-M09 | 승인 저장 중 단절 후 재시도 → 중복 없음 | 구현됨 | 서비스 `_replay` | I1 `test_idempotent_replay_returns_the_stored_result` |
| AC-M10 | 다른 테넌트·무권한 승인 → 거절 | 구현됨 | API `_authorize`, 서비스 `_lock`(tenant/project 필터) | I1 `test_permissions_tenant_project_and_session`; U8 `test_bundle_contract_*[other_tenant_or_project]` |
| AC-M11 | 새 초안·회수 → 자동 승계 없음, 회수 뒤 실행 거절 | 구현됨(enforce) | 서비스 `create_revision`, `revoke_review`, `verify_bundle`(`approval_revoked`, `superseded_by_newer_revision`) | I1 `test_verify_binds_tasks_and_revoke_holds_them`; U8 `test_bundle_contract_*[revoked]`, `[newer_revision]` |
| AC-M12 | 브라우저 검증 실패 + health 200 → 시각 평가불가 분리 보고 | 미구현 | 해당 코드 없음 | 없음 |
| AC-M13 | 채팅 카드 "패널에서 열기" | 부분 | 서버: `notification_link`, `resolve_change_target`. 대시보드 카드·패널 UI 없음 | I2 `test_chat_message_to_new_revision_to_reapproval_end_to_end` |
| AC-M14 | 카드에 답장 "버튼 아래로" → 수정 요청 접수, 승인·구현 변화 없음 | 부분 | 서비스 `intake_change`, `request_changes`(`approved:false`, `implementation_command:false`). 대상 표시 UI 없음 | U2 `test_target_comes_from_the_message_being_replied_to`, `test_intake_files_a_change_request_on_the_server_chosen_revision_and_never_approves` |
| AC-M15 | 후보 둘 이상 "방금 것 수정" → 선택 질문 1회 | 부분 | 서버 409 `ambiguous_review_target`+후보 목록. 질문 UI 없음 | U2 `test_two_active_reviews_in_one_session_are_ambiguous_and_none_is_not_found`; I2 `test_intake_target_is_decided_by_the_server` |
| AC-M16 | 수정중에서 v2 완료 → 요청별 반영/미반영, 비교, 재검토 | 부분 | 서비스 `change_report`, `create_revision(resolves)`. 화면·자산 단위 비교 없음(링크·hash 나열) | I1 `test_change_requests_preserve_text_links_and_outcome` |
| AC-M17 | v1 카드에서 승인 → 409 stale_revision, 승인 이벤트 0 | 구현됨 | 서비스 `_approval_gate`, `_stale` | I1 `test_concurrent_writers_produce_exactly_one_winner` |
| AC-M18 | 같은 change_request_id/idempotency_key 두 번 → 증가 0 | 구현됨 | 서비스 `_replay`, `intake_change` 해시 안정화(M6) | U2 `test_intake_idempotency_hash_ignores_server_filled_generation`; I2 `test_replay_does_not_notify_twice_and_inboxes_are_private` |
| AC-M19 | 생성 중 추가 요청 → 새 change_request_id 로 대기, 서버 순서 | 부분 | 서비스 `request_changes`(`queued`, `pending_change_requests`). 진행 중 입력 집합 고정·사용자 확인 1회 UI 없음 | I1 `test_change_requests_preserve_text_links_and_outcome`(queued 직접 단언은 없음) |
| AC-M20 | 승인 저장 실패 → 미표시, 같은 키 재시도 중복 없음 | 부분 | 서버 멱등(`_replay`). 실패 시 미표시·입력 유지는 대시보드 몫, UI 없음 | I1 `test_idempotent_replay_returns_the_stored_result` |
| AC-M21 | 재접속 → 서버 기준 대상·미전송 입력 복원 | 미구현 | 대상은 서버 해석(`resolve_change_target`)이지만 미전송 입력 복원 저장소·UI 없음 | 없음 |
| AC-M22 | 늦은 v1 결과(낮은 generation) → 포인터 불변 | 구현됨 | 서비스 `archive_if_stale` | I1 `test_stale_writer_can_be_archived_without_moving_the_pointer` |

요약: 구현됨 12(AC-M01·02·04·05·06·07·09·10·11·17·18·22 — 이 중 07·11 은 enforce 전제), 부분 8(03·08·13·14·15·16·19·20), 미구현 2(12·21), 합계 22. 실행 게이트(07·08·11)는 **운영이 shadow** 라 지금은 기록만 한다. AC-M08 의 채팅 직접 수정 도구·터미널 경로는 서버가 강제할 수 없는 **미지원 경로**이며, 프롬프트 지시로 강제됐다고 보고하지 않는다.

## 4. M8a 와 겹친 부분의 처리 (재작업)

직전 시도(runner-eaee44f2)는 M8a 보다 먼저 `edd1e672` 위에서 만들어졌다. M8a(runner-097902b0, `f346d1ba`)가 먼저 main 에 들어갔으므로 보존 패치를 그 위에 얹으며 아래처럼 정리했다. 충돌 시 M8a 를 우선했다.

| 직전 시도의 변경 | 처리 | 이유 |
|---|---|---|
| 규칙 `mode_decision` 신설, `unavailable_decision` 완화(선언 번들도 shadow 통과), `gate_check` 가 검증 실패를 shadow 에서 통과 | **버림** | M8a 는 "선언한 번들은 모든 모드에서 검증, 멈춤 여부는 phase 별 `blocking`" 으로 정했고 U3 `test_database_failure_fails_closed_for_declared_bundles_and_enforce_in_every_phase`, `test_shadow_still_denies_a_bad_declaration_but_only_stops_runner_phases` 가 그 계약을 고정한다. 두 계약을 같이 둘 수 없다 |
| U2(`test_mockup_execution_gate.py`) 의 shadow 기대값 변경 | **버림** | 위와 같은 이유. U2 는 M8a 의 것 그대로 |
| CLI `--changed-files-from` | **`--changed-files` 로 통합** | M8a 가 pre_push 용으로 같은 입력을 이미 만들었다. offline 도 M8a 의 `classify_paths` 로 판정한다 |
| shadow 판정 기록, 채팅 쓰기 경로(chat_write), pre-push | **M8a 것만 사용** | 두 벌 금지 |
| `submit_batch` 게이트, DB fallback 게이트, 셸 fail-closed, CI offline job, 이 두 문서 | 유지 | M8 범위 |
| `scripts/pipeline-runner.sh` | 유지 + `scripts/pipeline-runner.sh.local` 에 복사, `cmp` 동일 | 직전 실패 원인(사본 미동기화) |

결과: shadow 에서 **선언한 번들**의 검증 실패(변조·회수·새 수정본·범위 밖·다른 tenant/project·DB 장애)는 거절된다. submit / pre_execution / checkpoint 에서는 멈추고, chat_write / pre_push 에서는 기록 + 경고만 한다. shadow 에서 통과 + 기록되는 것은 **번들 선언 없는 UI 작업**뿐이다. 원 지시서 범위 5("shadow 는 기록 후 통과")와 이 점이 다르다 — M8a 우선 지시에 따른 것이며, 운영 `mockup_review_*` 테이블이 0행이라 실제로 막히는 작업은 없다.

남은 한계: 배치의 한 job 이 승인된 번들로 통과(바인딩 `bound` 행 생성)하고 다른 job 이 거절되면 배치 전체가 409 로 끝나 그 바인딩은 생성되지 않은 job_id 를 가리킨 채 남는다. 단건 제출이 게이트 통과 뒤 dedup 으로 막힐 때도 같다(기존 동작). 실행에는 영향이 없다 — 그 job_id 로는 아무것도 시작되지 않는다.

## 5. 검증

- `bash scripts/run_unit_tests.sh tests/unit/test_mockup_exec_gate_m8.py tests/unit/test_mockup_execution_gate.py tests/unit/test_mockup_reviews.py` → exit 0, 135 passed.
- 전체 `tests/unit/` 결과와 HEAD 기준선 비교는 작업 RESULT 에 적는다.
- DB 통합(I1, I2): `AADS_MOCKUP_TEST_DATABASE_URL`(일회용 DB) 가 필요하다. 이 세션에서는 일회용 DB 를 띄우지 않아 **실행하지 않았다.** I2 의 게이트 테스트는 `environ={"MOCKUP_GATE_MODE": "enforce"}` 를 명시하므로 이번 shadow 계약 변경의 영향을 받지 않는다. M6(`edd1e672`)에서 20건 PASS 기록이 있다.

## 교훈

- 게이트 본체가 있어도 **제출 경로가 여러 개면 하나만 막은 것**이다. 단건 제출만 게이트를 탔고 채팅 AI 가 기본으로 쓰는 배치 제출과 API 장애 때의 DB fallback 은 아무것도 검사하지 않았다. 게이트를 추가할 때는 `INSERT INTO pipeline_jobs` 를 grep 해 전 경로 표를 먼저 만든다.
- "판정 불가"를 문구 grep 으로 메우면 문구를 안 쓴 작업이 새어 나간다. 적용 대상 판정은 서버 규칙 한 벌(`classify`)만 쓴다.
