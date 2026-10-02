# ACCT-CAFE24-DATA-FILE-COLLECTION-CLOSEOUT-20261003-R10

공개 전환 **불가**, M1/M3 BLOCK 유지(M3 완료 신고 안 함). 이 절은 맨 위에 추가한 것이며 아래 기존 RESULT.md 내용은 한 줄도 지우지 않았다. 전문: 서버 114 `/srv/acct/evidence/data-file-collection-r10/RESULT.md` (sha256 551f239f…a74e1).

## STEP 0 분류
- 저장소 파일 전부 [유지](변경 0, git 변경 없음). 서버 114 증거 영역 `data-file-collection-r10/` [신규]. `selected/` [신규, 비어 있음]. DB handover `acct-cafe24-data-file-collection-r10-20261003` [신규]. 삭제 없음.

## 실제 검증 결과
| 항목 | 결과 |
|---|---|
| obys 18 테이블 + acct + obys_auth 재조회 | 과거 수치와 일치. 차이는 `ddl_audit_log` GRANT 3행(31,261 대 31,258)뿐 |
| 라일론 유입 게이트 | 조회한 11개 항목 전부 0 (`gate_zero_lylon.out`) |
| source_file 귀속 | 비라일론(7–10) 0행 / 라일론(11) 604 / NULL 45,559 — "0행"은 귀속 근거 없음이지 무누락 증명이 아님 |
| "315 source rows", 서명 PDF, 최신성 | 재현 불가·미확인·UNKNOWN (사실로 기재하지 않음) |
| 백업 `서버_20261002_1130.tar.gz` 분류 (범위 2,435 파일) | LYLON 내용 676 / LYLON 경로 837(미열람) / 귀속 불가 922 / **비라일론 확인 0** → 적재 0건, 무누락 선언 불가 |
| 격리 파일 테스트(저장·재읽기·해시·중복·권한 거부·변조 감지) | 8/8 PASS, 기존 데이터·APP 경로 접촉 없음 |
| 수집 의존성 | 단독 앱에 수집 라우터 없음, 큐 소비자는 AADS `app/main.py` 뿐, PC agent 는 aads.newtalk.kr 의존. Cafe24 전용 JSON 큐 경로안은 문서화만(미활성) |

## APP 인계
- `app/yeoljeong_main.py`/`auth.py` 의 수집 라우터 반영은 APP 소유자 몫(본 작업 미수정). 순서: 라우터 마운트 → JSON 큐 env(`AADS_PC_AGENT_COLLECTION_QUEUE_PATH`, DATABASE_URL 미설정) → 소비자 구성.
- 승인 후 Runner 빌드 검증 대상: 위 APP 변경이 생길 경우에 한함.
- 외부 수집·클로브 가입·외부제공 동의는 수행/대행하지 않음.

# AADS-CHAT-TURN-MODEL-CONTRACT-20261002

턴 모델 결정 6곳을 `TurnModelContract` 하나로 통일. 요청 모델은 턴 시작에 한 번 정하고 이후 경로는 읽기만 한다. (이 절은 맨 위에 추가한 것이며 아래 기존 RESULT.md 내용은 한 줄도 지우지 않았다. runner-0cd99c37 은 이 작업이 대체한다.)

## 브리프 밖 파일 사전 설명
- 브리프 파일: `app/services/chat_service.py`, `app/services/model_selector.py` (수정).
- 신규 `app/services/turn_model_contract.py` — 계약 객체. 두 파일 모두에서 import 하므로 한 곳에 두어야 한다(양쪽 사본을 두면 R-ERRBOOK 이 경고한 "두 벌" 이 된다).
- 신규 `tests/unit/test_turn_model_contract.py` — 요구된 행렬 테스트.
- 수정 `scripts/pre_commit_test_map.py` — 새 테스트를 기존 `chat_stall_codex_auth_fallback` 묶음에 등록(chat_service/model_selector/turn_model_contract 변경 시 pre-commit 이 같이 돌린다). 3줄.

## STEP 0 기존 구현 분류

| 대상 | 분류 | 내용 |
|---|---|---|
| `chat_service.send_message_stream` 턴 시작 블록(요청 모델 결정·`chat_turn_executions.requested_model` UPDATE) | 수정 | 선택창 값을 정규화 → `build_turn_contract` 1회 → UPDATE 는 계약의 구체 모델로. 이전에는 `model_override or intent_result.model` 이라 "auto"/"mixture" 토큰이 그대로 기록될 수 있었다 |
| 선택창 `openai:<id>` → `codex:<id>` 정규화 (32745da4, `call_stream` 안) | 이관 | `turn_model_contract.normalize_selected_model` 로 계약 생성 시점에 수행. `call_stream` 쪽 처리는 이미 codex 인 값에는 no-op 이라 그대로 둔다(동작 동일) |
| `send_message_stream` 재시도 루프 (`iter_with_stall_timeout`, `_persist_retry_model_switch`, `_note_retry_model_switch`) | 수정 | 구조·횟수·앵커 문자열 유지. 예비 모델 목록과 시도별 모델 계산만 `_turn_retry_fallback_models` / `_plan_attempt_model` 로 뽑아 루프와 테스트가 같은 코드를 쓴다. 고정 턴은 예비 모델 `[]` |
| `chat_service._turn_retry_fallback_models`, `_plan_attempt_model` | 신규 | 순수 함수. 고정이면 같은 모델, 비고정일 때만 `_cross_provider_chat_fallback_chain` |
| `chat_service._note_retry_model_switch`, `_persist_retry_model_switch`, `_safe_fallback_reason`, `iter_with_stall_timeout`, `StreamStallError`, 인터럽트 중단 처리 | 유지 | 5cf57465(인터럽트는 재시도 안 함), 62feede6(스톨 타임아웃·Codex 401 표시) 되돌리지 않음. 체인 항목 4키 모양 {from,to,reason,at} 그대로 |
| `chat_service._resume_single_stream` 모델 결정 | 수정 | 원 턴 `requested_model` 우선(5cf57465 의 순서 유지: `resume_model_from_execution` → `session_current`). 계약 생성·`fallback_info` 저장 추가 |
| `model_selector.call_stream` 정책 강등(cascade/allowed_models) | 수정 | 계약이 면제(`policy_exempt`)한 턴은 건너뛰고, 면제가 아닌 실제 강등은 `fallback_chain` 에 `kind=policy_downgrade` 로 사유와 함께 기록 |
| `call_stream` Codex 오류→Claude Fable 전환 (`codex_cli` 등록행 폴백 체인, quota 우회, `_CODEX_FB` 분기) | 수정 | `user_pinned` 이면 전환 없이 분류된 오류 이벤트로 종료 |
| `call_stream` Claude 최후 sonnet 강등 | 수정 | `and not _pinned_no_switch` |
| `call_stream` gemini/deepseek/openrouter 등 나머지 내부 폴백, `_stream_direct_openai_provider` | 유지 | 범위 밖(아래 "남은 항목") |
| `scripts/claude_model_contract.py` `ModelObservation`, `intent_router.IntentResult`·`get_model_for_override` | 유지 | 변경 없음. `IntentResult` 에는 필드를 추가하지 않고 동적 속성(`turn_model_contract`, `policy_exempt`)만 붙인다(`asdict` 사용처 보호) |
| `turn_model_contract.py` 전체 | 신규 | `TurnModelContract`, `build_turn_contract`, `build_resume_contract`, `normalize_selected_model`, `pinned_failure_notice`, `note_switch`, `note_done` |
| 삭제 | 없음 | 기존 함수·테스트 삭제 없음 |

## 결정 지점 6곳 — 이전 / 이후

| # | 결정 지점 | 이전 | 이후 |
|---|---|---|---|
| 1 | 선택창 저장값 | `model_override` 가 곳곳에서 그대로 쓰이고 auto 토큰도 `requested_model` 로 기록. `openai:` → codex 정규화는 `call_stream` 안이라 기록 모델과 실행 모델이 어긋날 수 있었다 | 턴 시작에 정규화 후 계약 `source=user_select`, `user_pinned=true`. `chat_turn_executions.requested_model` 에 구체 모델 한 번 기록 |
| 2 | session.current_model | 재개에서 원 턴 기록보다 먼저/동등하게 쓰여 다른 턴이 남긴 값으로 바뀜(24h 8건: Opus 5.5 요청이 gpt-5.6-sol 로 이어짐) | 원 턴 기록이 없을 때만 `source=session_fallback` + `origin_requested_model_missing:<tier>` 를 `fallback_chain` 에 남기며 사용 |
| 3 | intent_policies / cascade_downgrade | 자동응답(`auto_reaction`)의 운영 기본 모델이 `allowed_models` 에 없으면 조용히 강등 | 자동응답 기본값은 `source=auto_reaction_default` 로 면제. 실제 강등이 일어나면 `kind=policy_downgrade` 로 `fallback_chain` + 로그(`cascade_downgrade`)에 사유 기록 |
| 4 | 재시도 루프 예비 모델 | 사용자가 고른 모델도 실패하면 예비 모델로 전환 | 고정 턴: 같은 모델 재시도, 최종 실패 시 `pinned_failure_notice` + error 이벤트(`reason=pinned_model_failed`, `failure_class`) 로 전환 없음. 비고정 턴만 전환 + 배너 + `fallback_chain` + `actual_model` |
| 5 | 재개 | `resume_model_override`(첫 응답 지연 예비 모델)가 원 턴 모델을 덮을 수 있었고 `fallback_info` 미저장 | 원 턴 `requested_model` 유지(`source=resume_origin`). 고정 턴은 override 무시(`resume_override_ignored_pinned` 로그), 재개 재시도도 같은 모델만. 비고정이고 override 가 적용되면 `kind=resume_override` 기록 |
| 6 | CLI 보고 actual_model | done 이벤트의 `actual_model` 과 `used_models` 를 요청 모델과 대조하지 않음 | `note_done` 이 보조 모델(`aux_models`)을 분리해 `turn_model_aux` 로그, 전환 기록이 없는데 요청과 다르면 `turn_model_actual_differs` 경고. 원인 불명의 차이를 전환으로 위조해 기록하지 않음 |

SSE done 의 `requested_model` 필드는 고정 턴에서 `model_used` 와 다를 때만 실린다.

## actual_model 이 opus-4-8 로 찍힌 건 — 원인 근거

- 관측: 5행, 세션 `acc75e55-…0002`, 09:36–10:14 KST, `requested_model=claude-opus-5-5`, `fallback_chain=[]`. 같은 세션의 다른 56행은 opus-5.
- (a) 보조(서브에이전트) 모델이 첫 키로 잡힌 경우 — **배제**. `scripts/claude_model_contract.py` `ModelObservation.observe` 는 `actual_model` 을 메인 스레드 `assistant.message.model`(`parent_tool_use_id` 비어 있음)에서만 취하고, 보조 모델은 `used_models`/`model_mismatch` 로 따로 낸다. `tests/unit/test_claude_model_contract.py::test_primary_model_not_first_subagent_usage_and_mismatch_detected` 가 이를 검증한다(실행 통과).
- (b) 별칭 매핑이 4-8 로 바꾼 경우 — **앱 코드에서는 배제**. `claude-opus-4-8` 은 `EXACT_MODEL_IDS` 에만 있고, `claude-opus-5-5`/`claude-opus` 가 4-8 로 풀리는 매핑이 없다(`resolve_model` 은 명시 버전을 바꾸지 않는다 — `test_explicit_version_never_changes`).
- (c) CLI/제공자가 실제로 다른 모델을 보고 — **잠정 결론**. 원시 relay 로그를 찾지 못해 CLI 가 보낸 `assistant.message.model` 원문은 확인하지 못했다. 따라서 (c) 는 (a)(b) 배제에서 나온 추정이며 확정이 아니다.
- 조치: (c) 이므로 고치지 않았다. 앞으로 같은 일이 생기면 `turn_model_actual_differs`(요청·실제·source·pinned·model_mismatch) 와 `turn_model_aux`(used_models) 로그로 원문 근거가 남는다.

## 동작 변경·주의
- 자동 선택 턴의 재시도 기준 모델이 "mixture"/"auto" 토큰이 아니라 운영 DB 기본 모델(예: claude-opus-5-5)이 되어, 예비 모델 목록이 그 모델 기준(예: gpt-6-astra, gpt-5.6-sol)으로 계산된다.
- 재개의 "고정 여부"는 DB 컬럼이 없어(스키마 변경 없음) 원 턴 user 메시지 `model_used` 가 비어 있지 않고 auto 토큰이 아니면 고정으로 본다.
- 기존 잠재 버그 수정: 재개의 Redis 완료 분기에서 `_resume_model_used` 가 미바인딩이던 것을 `_resume_model` 로 설정.
- `call_stream` 의 gemini/deepseek/openrouter 등 비-Claude/비-Codex 모델 내부 폴백과 `_stream_direct_openai_provider` 폴백은 여전히 배너와 함께 전환한다 — 요구 행렬({Opus 5.5, codex gpt-6.1-sol}) 범위 밖이라 건드리지 않았다.

## 테스트 결과 (실제 실행한 것만)

`bash scripts/run_unit_tests.sh` 로 실행.

- 신규 `tests/unit/test_turn_model_contract.py` + 기존 3개(`test_chat_retry_model_switch`, `test_chat_stall_codex_auth_fallback`, `test_chat_interrupt_no_model_switch`) + `test_claude_model_contract`: **166 passed**.
- 관련 묶음(위 + test_chat_service, test_chat_auto_reaction_integrity, test_chat_retry_partial_lifecycle, test_chat_status_retry_projection, test_chat_resume_owner_fence_v2, test_chat_stream_completion_static, test_model_selector_codex_db_route, test_model_selector_dynamic_routing, test_chat_interrupt_receipt_failclose, test_stream_watchdog_liveness, test_relay_resume_persistence, test_dup_guard): **422 passed, 1 warning**(FastAPI `regex` deprecation, 이 작업과 무관).
- `ruff check --select F821,F811` (chat_service, model_selector, turn_model_contract, 신규 테스트): All checks passed.
- `python3 -m compileall` (chat_service, model_selector, turn_model_contract): 통과.
- 실행하지 않은 것: 전체 `tests/unit` 스위트, `scripts/dup_guard.py` 단독 실행, 운영 환경 검증. 이 작업에서는 빌드·배포·재시작·git 조작을 하지 않았다.

행렬 커버리지 ({Opus 5.5, codex gpt-6.1-sol} × 시나리오):

| 시나리오 | 검증 내용 |
|---|---|
| 정상 | 고정 턴은 3회 시도 모두 요청 모델, 예비 모델 없음. 자동 턴은 구체 기본 모델로 실행 |
| 인터럽트 | 계약·체인 불변, 인터럽트 분기가 재시도 이월보다 앞(5cf57465 앵커) |
| 재개 | 원 턴 모델 유지, 고정 턴 override 무시, 비고정 override 기록, 원 턴 기록 없음→`session_fallback` + 체인 |
| 자동응답 | 운영 기본 모델이 `requested_model`, 정책 강등 면제, 비고정이라 전환 시 체인·배너 기록 |
| 401 인증 실패 | 고정: `call_stream` 수준에서 Claude 전환 없이 error(`codex:` 접두사·무접두사·무잠금 3경로). 비고정: 기존 폴백 유지. 재시도 루프 수준에서 고정은 체인 비어 있음 |
| 스톨/타임아웃 | 고정: 같은 모델, 전환 없음, 분류된 안내문. 비고정: 전환 + 체인 + 배너 |

---

# AADS-CHAT-STALL-CODEX401-FALLBACK-20261002

채팅 무출력 정지 방지 — 스톨 타임아웃 + Codex 401 인증 오류 표시 + 전환 기록. (이 절은 맨 위에 추가한 것이며 아래 기존 RESULT.md 내용은 한 줄도 지우지 않았다.)

## STEP 0 기존 구현 조사 (분류)

| 접점 | 분류 | 내용 |
|---|---|---|
| `chat_service.send_message_stream` 재시도 루프 (`for _stream_attempt in range(3)`) | 수정 | `call_stream` 소비부를 `iter_with_stall_timeout` 로 감쌈. 루프 구조·재시도 횟수·백오프 유지. 마지막 시도가 멈추면 예외 대신 명시적 error 이벤트 + `_mark_execution_interrupted` |
| `chat_service._note_retry_model_switch` / `_retry_fallback_chain` (7fbaa57d) | 유지 | 그대로 재사용. 중복 기록 로직을 만들지 않음 |
| `chat_service._persist_retry_model_switch` | 신규 | 전환 시점에 `chat_turn_executions.fallback_chain`(같은 `[{from,to,reason,at}]` 리스트) + `actual_model` 즉시 기록. 완료 저장(`_save_and_update_session`)의 `COALESCE($7::jsonb, fallback_chain)` 와 모양 동일 |
| `chat_service._safe_fallback_reason` | 수정 | `codex_auth_failed` → "Codex 인증 실패(재로그인 필요)" 분기 추가(기존 "인증 오류" 앞) |
| `chat_service._INTERRUPT_REASON_CATEGORIES` | 수정 | `stream_stall_timeout` → watchdog_timeout(자동 재개 대상), `codex_auth_failed` → llm_provider_error |
| `model_selector.iter_with_stall_timeout` / `StreamStallError` / `stream_stall_limits` | 신규 | 실제 출력(delta/thinking/tool_use/tool_result/done/error) 기준 스톨 감시. heartbeat·model_info·retry_progress 는 시계를 되돌리지 않음 |
| `model_selector.call_stream` 의 `_stream_agent_sdk` 두 호출(~2724, ~2824) | 수정 | 스톨 가드로 감싸 `as_error_event=True` → 기존 `_err=True; break` 경로로 다음 슬롯/모델 단계 진행 |
| `model_selector._stream_codex_relay_once` error·empty result | 수정 | `_classify_claude_auth_error`(기존 분류 유틸) 재사용. 인증 계열이면 `codex_auth_failed: Codex 인증 실패(재로그인 필요) [분류]` 로 올림. 새 분류기 없음 |
| `model_selector._codex_auth_failure_message`, `_codex_revoked_account_names` | 신규 | 문구 조립 / `codex_usage_snapshots.auth_usable·auth_revoked_reason` 로 인증 불능 계정 이름 조회(3초 상한, 실패 시 빈 목록) |
| `_RELAY_NON_RETRYABLE_ERROR_MARKERS` | 수정 | `codex_auth_failed` 추가 — 같은 모델 재시도 안 함 |
| `call_stream` codex_cli 분기 | 수정 | 폴백 로그·배너에 인증 실패 구분(분류명만) |
| `scripts/pre_commit_test_map.py` | 수정 | chat_service/model_selector → 신규 테스트 + test_chat_retry_model_switch 묶음 추가 |
| `tests/unit/test_pre_commit_test_map.py` | 수정(승인 범위 밖, 사유 아래) | |
| 삭제 | 없음 | 기능·테스트·파일 삭제 없음 |

삭제된 줄(diff `-`)은 전부 같은 문장의 치환이다: `call_stream` 호출을 스톨 가드로 감싸며 재들여쓰기, `_stream_agent_sdk` 호출 2줄을 가드 호출로 치환, codex 폴백 로그/배너를 인증 구분 포함 문구로 치환, error 이벤트 패스스루를 분류 후 패스스루로 치환. 롤백은 `git revert` 한 번이며 환경변수 `AADS_STREAM_STALL_FIRST_OUTPUT_SEC=0`, `AADS_STREAM_STALL_IDLE_SEC=0` 으로 스톨 감시만 끌 수도 있다.

## 지시서 밖 파일을 바꾼 사유

`tests/unit/test_pre_commit_test_map.py::test_unmapped_change_is_skipped_with_exit_zero` 가 "매핑 없는 파일" 예시로 `app/services/chat_service.py` 를 쓰고 있었다. 지시서가 chat_service.py 매핑 추가를 요구하므로 그 예시가 거짓이 된다. 테스트를 지우지 않고 예시 파일만 `app/services/memory_manager.py`(매핑 없음)로 바꿨다(1줄).

## 요구사항별 구현

1. **스톨 타임아웃.** 기본값은 기존 관례에서 정했다: 첫 출력 420s(첫응답 워치독 `AADS_STREAM_FIRST_RESPONSE_TIMEOUT_SEC`=180s + 큰 컨텍스트 가산 최대 210s 보다 길게), 출력 이후 600s(`heartbeat_idle_ceiling` 과 동일). 환경변수 `AADS_STREAM_STALL_FIRST_OUTPUT_SEC`, `AADS_STREAM_STALL_IDLE_SEC`(0=끔). 두 곳에 건다 — (a) `call_stream` 안의 폴백 SDK 단계: 멈추면 error 이벤트로 다음 슬롯/모델로 진행, (b) `send_message_stream` 재시도 루프 전체: 60초 여유를 더해 안쪽이 먼저 걸리게 했고, 멈추면 `StreamStallError` → 기존 예외 재시도 경로(백오프·다음 폴백 모델), 마지막 시도면 명시적 error + 실행 중단 기록.
2. **Codex 401.** 릴레이(`scripts/claude_relay_server.py` ~2492)는 CLI stderr 를 로그에만 남기고 클라이언트에 전달하지 않는다. 그래서 401 이 error 이벤트로 오면 분류해서 올리고, 이벤트 없이 빈 `result` 로 끝나면 (a) result 이벤트에 진단 문자열(error/stderr/detail)이 실려 있으면 분류, (b) 없으면 `codex_usage_snapshots` 가 인증 불능으로 표시한 활성 계정이 있는지 본다. 어느 쪽 증거도 없으면 기존 `codex_empty_result` 를 그대로 둔다 — 근거 없이 인증 문제라고 단정하지 않는다. 오류 원문·토큰은 문구/로그에 넣지 않고 분류명(revoked/unauthorized 등)과 계정 key_name 만 쓴다.
   - **한계(후속 필요):** 스냅샷 수집이 늦거나 계정이 정상 표시인데 실제로는 401 인 경우, 릴레이가 stderr 를 전달하기 전에는 `codex_empty_result` 로 남는다. 릴레이는 인증 핵심 파일이라 이번 승인 범위 밖이다.
3. **전환 기록.** 재시도 루프의 `_note_retry_model_switch` 직후 `_persist_retry_model_switch` 로 즉시 DB 에 쓴다. 이전에는 완료 저장에서만 써서 전부 실패한 턴(이번 사고)에는 기록이 없었다. 사유는 `_safe_fallback_reason` 고정 분류(Codex 401 이면 "Codex 인증 실패(재로그인 필요)"). `call_stream` 내부에서 일어나는 전환(슬롯 교체, Codex→Fable 직접 폴백)은 chat_service 가 신호를 받지 못해 이번에는 기록 대상이 아니다.

## 검증 (실제 실행)
- `bash scripts/run_unit_tests.sh tests/unit/test_chat_stall_codex_auth_fallback.py` → 15 passed (신규)
- `bash scripts/run_unit_tests.sh tests/unit/test_chat_service.py tests/unit/test_model_selector_codex_db_route.py tests/unit/test_model_selector_dynamic_routing.py tests/unit/test_chat_retry_model_switch.py tests/unit/test_chat_stall_codex_auth_fallback.py tests/unit/test_codex_token_revoked_detect.py` → 225 passed
- `bash scripts/run_unit_tests.sh tests/unit/test_pre_commit_test_map.py` → 12 passed
- `bash scripts/run_unit_tests.sh tests/unit/test_tools_and_pipeline.py` → 79 passed
- `ruff check --select F821,F811` (chat_service.py, model_selector.py, 신규 테스트, pre_commit_test_map.py, test_pre_commit_test_map.py) → All checks passed
- `python3 -m compileall` (chat_service.py, model_selector.py) → 통과
- 신규 테스트 매핑: (i) 출력 없는 시도 타임아웃 + `call_stream` 이 멈춘 SDK 단계를 취소하고 다음으로 진행 / (ii) Codex 401·빈 결과 인증 분류, 원문 미노출, 재시도 안 함 / (iii) 전환 시 fallback_chain·actual_model 기록 모양.
- 빌드·배포는 실행하지 않았다.

---

# AADS-CHAT-RETRY-SILENT-MODEL-SWITCH-20261002 (자동 재작업 1/2)

## 리뷰 지적 처리

1. **삭제 라인 544 > 추가 라인 134 의 50% / 순삭제 410줄** — 지적은 수치상 맞지만 원인은 코드 삭제가 아니다.
   - 반려 커밋 a2c6661a 의 numstat: `RESULT.md +28/-548`, `chat_service.py +109/-3`, `model_selector.py +7/-1`, 테스트 `+130/-0`.
   - 삭제 548줄 중 코드 삭제는 4줄(아래 표)이고 나머지는 **git 에 추적 중인 이전 작업(AADS-SMARTBROWSER-M11-E2E-RELEASE-R5-20260920)의 RESULT.md 를 통째로 덮어써서** 생긴 것이다. 이전 작업의 검증 근거를 지운 것이므로 잘못이었다.
   - 조치: origin/main(3362b518) 기준 새 worktree 에서 코드·테스트 diff 만 가져왔고, RESULT.md 는 **이 절을 맨 위에 추가만** 했다. 기존 내용은 한 줄도 지우지 않았다(RESULT.md 삭제 0줄).
   - 코드 삭제 4줄 상세(모두 같은 줄의 치환이며 기능 삭제 아님):

| 위치 | 삭제 줄 | 사유 | 호출처 영향 / 롤백 |
|---|---|---|---|
| chat_service.py `_cross_provider_chat_fallback_chain` | `if normalized.startswith(("claude-", "claude_")):` → `elif` 로 바뀜 | 앞에 `openai:` 분기를 끼우려고 `if`→`elif` | claude 경로 결과 불변. 롤백: 새 분기 제거 후 `elif`→`if` |
| chat_service.py `send_message_stream` | `_missing_done_fallback_models = ..._chain(...)[1:3]` 한 줄 | codex 후보 필터(`_drop_unregistered_codex_retry_candidates`)를 거치도록 여러 줄로 재작성 | 슬라이스 `[1:3]` 동일 유지 |
| chat_service.py `_save_and_update_session` | `fallback_info: Optional[dict] = None,` | 타입 힌트만 `Optional[dict \| list]` 로 확장(체인은 list). 본문은 `json.dumps` 라 list 도 처리 | 기존 dict 호출처 영향 없음 |
| model_selector.py `call_stream` | `if _model_locked:` → `elif` | `retry_override` 분기를 앞에 끼우려고 `if`→`elif` | `retry_override=False` 기본값이면 기존 동작과 동일 |

## STEP 0 기존 구현 조사 (분류)

| 접점 | 분류 | 내용 |
|---|---|---|
| `_cross_provider_chat_fallback_chain` | 수정 | `openai:<id>` 요청이면 `codex:<id>` 를 1순위 재시도 후보로 |
| `_drop_unregistered_codex_retry_candidates` | 신규 | 레지스트리에 활성·실행 가능한 codex 행이 없으면 후보에서 제외(재시도 1회 낭비 방지). `model_selector._get_registered_model_row` 재사용 |
| `_model_switch_banner` | 신규 | model_selector 의 `[<모델> 실행 불가 → ... 전환]` 과 같은 문구 |
| `_note_retry_model_switch` | 신규 | 모델이 바뀌면 fallback_chain 에 `{from,to,reason,at}` 기록, 배너 반환 |
| `send_message_stream` 재시도 루프 | 수정 | 전환 기록·배너·`retry_override` 전달·실패 사유 수집. 루프 구조/재시도 횟수 유지 |
| `_save_and_update_session` | 수정(타입만) | list 체인 수용. DB 컬럼 `chat_turn_executions.fallback_chain` 기존 경로 사용 |
| `model_selector.call_stream` | 수정 | `retry_override` 인자(기본 False) 추가, 로그 문구 `cascade_skip: retry_override` 로 구분 |
| 삭제 | 없음 | 기능·테스트·파일 삭제 없음 |

## 요구사항별 구현
1. 전환 시 fallback_chain 기록 + requested_model 유지 + 본문 맨 앞 배너(기존 형식 재사용): `_note_retry_model_switch`, `_pending_switch_banner`(실패한 시도에는 붙지 않고 첫 delta/tool_use 에서 출력).
2. 재시도 override 에는 "user explicitly selected" 대신 "retry_override" 로그.
3. `openai:gpt-6.1-sol` 실패 → `codex:gpt-6.1-sol` 1순위(레지스트리 행 있을 때만).
4. 단위 테스트 `tests/unit/test_chat_retry_model_switch.py` 6건: (i) codex 재시도 선택·미등록 시 제외, (ii) fallback_chain 비어 있지 않음 + 배너 접두, (iii) retry_override 에 'user explicitly selected' 로그 없음 / 일반 명시 선택은 유지.

## 검증 (실제 실행)
- `bash scripts/run_unit_tests.sh tests/unit/test_chat_retry_model_switch.py` → 6 passed
- `bash scripts/run_unit_tests.sh tests/unit/test_model_selector_codex_db_route.py tests/unit/test_model_selector_dynamic_routing.py tests/unit/test_chat_service.py` → 161 passed
- `ruff check --select F821,F811` (변경 3개 파일) → All checks passed
- 빌드·배포·커밋·push 는 실행하지 않음(승인 후 Runner 담당).

---

# AADS-SMARTBROWSER-M11-E2E-RELEASE-R5-20260920

## STEP 0 기존 구현 조사 및 분류

| 접점 | 분류 | 처리 |
|---|---|---|
| `scripts/smart_browser_readonly_e2e.py:run` | 수정 | 기존 실제 Playwright capture 경로를 보존하고, ARIA snapshot 및 브라우저 실패 R-E2E artifact 경로만 추가했다. |
| `_exercise_skill_resolution` | 수정 | production exact/vector resolver 사용은 유지하되, 선택 DB 의존성 때문에 pytest collection이 깨지지 않도록 지연 import했다. |
| `aria_nodes`, `_sha256`, local storefront fixture | 유지 | read-only fixture와 캡처 증거 계산 계약을 변경하지 않았다. |
| `tests/unit/test_smart_browser_readonly_e2e.py` | 수정 | optional `asyncpg` 환경에서는 관련 경로만 skip하고, 의존성 없는 브라우저 실패 artifact 계약은 계속 실행 가능하게 했다. |
| `tests/integration/test_smart_browser_m11_postgres.py` | 수정 | `asyncpg` 직접 import를 `pytest.importorskip`으로 바꿔 collection failure를 안전한 dependency skip으로 전환했다. disposable DB가 제공되면 기존 실제 DB 검증은 그대로 실행된다. |
| `tests/integration/test_smart_browser_learning_postgres.py` | 수정 | 위와 동일하게 collection 안전성만 보정했다. |
| 기존 production API/DB schema/LLM client | 유지 | 테스트 편의를 위한 production 약화, `ANTHROPIC_API_KEY` 추가, 직접 외부 LLM 호출은 없다. |
| 삭제 | 없음 | 호출처 영향·롤백 대상 삭제 없음. |

지시서 중심 파일 밖에서 변경한 두 integration test의 사유는 `asyncpg` 부재가 M11
pytest collection 전체를 막지 않도록 하고, disposable DB 제공 시 기존 integration
본문을 실제 실행 경로로 남기기 위해서다.

## 변경 파일

- `scripts/smart_browser_readonly_e2e.py`
- `tests/unit/test_smart_browser_readonly_e2e.py`
- `tests/integration/test_smart_browser_m11_postgres.py`
- `tests/integration/test_smart_browser_learning_postgres.py`
- `RESULT.md`

## 구현 결과

- 실제 Playwright가 가능하면 fixture 화면 PNG 3개, `locator("main").aria_snapshot()`의 최초·핵심 anchor 무효화 snapshot, chat artifact를 생성한다.
- 최초 candidate evidence(캡처 SHA-256), exact/vector 재사용 route/reason code, 동적 가격 재검증 hash, 핵심 ARIA invalidation의 candidate `+1`/active 보존, Human Gateway 복구, LLM 호출 수·비용·실행시간을 artifact에 기록한다.
- 브라우저 import/launch/capture 또는 후속 production resolver가 실패하면 결과는 `degraded`다. `browser-fallback.json`에 `HTTP status -> API /health -> container/process` 순서의 read-only 진단을 저장하고, chat artifact에 `BROWSER_CAPTURE_UNAVAILABLE`를 남긴다. 이 경우 문구는 **브라우저 E2E 미실행, API 검증으로 대체**이며 browser pass로 판정하지 않는다.
- 재검증 artifact 경로: `/tmp/aads-smartbrowser-r5-recheck.v916bh/01-first-learning.png`, `02-revisit-price.png`, `03-aria-invalidated.png`, `aria-snapshots.json`, `chat-artifact.json`; 폴백 검증 artifact는 `/tmp/aads-smart-browser-e2e-recheck-1039/browser-fallback.json`이다.

## 검증 결과

| 항목 | 결과 |
|---|---|
| Playwright E2E | **PASS**, 15/15 checks, 2,701ms, write action 0, LLM call 0, 비용 $0.00. 기존 릴리스 이미지에 현재 worktree를 read-only mount한 격리 컨테이너에서 최종 HEAD 재실행. |
| focused/affected unit pytest | `run_unit_tests.sh` — **86 passed**. |
| disposable PostgreSQL integration | M11 + M8 실제 DB 경로 — **2 passed**; candidate→active→candidate+1/active 보존과 concurrent candidate/tenant 격리를 재검증. |
| 브라우저 불가 폴백 | 호스트 직접 실행에서 `ModuleNotFoundError`를 재현하고 `HTTP → API health → container/process` artifact와 `degraded` 판정을 확인. 브라우저 PASS로 오판하지 않음. |
| Ruff | 대상 4개 Python 파일 **PASS**; 최초 검사에서 발견한 style 6건 교정 후 재검증. |
| `py_compile` / `git diff --check` | 최종 HEAD에서 모두 **PASS**. |
| repo 표준 pre-commit (`scripts/hooks/pre-commit`) | 대상 5개 파일 stage 후 수동 실행 및 실제 commit hook 모두 **PASS**. |
| npm/next/docker build | 승인 후 Runner 빌드 검증 대상. |

## 릴리스 후보 전제 및 핸드오버

- 기준 SHA: `origin/main` `f6a124b2254aaf4676d1016af322948130b92ba5`; 검증 후보는 로컬 branch `runner-ea3210d3-recovered`의 단일 후속 commit이며 push는 수행하지 않았다.
- 배포 전 preflight: clean committed candidate SHA, 단일 immutable image digest, candidate `--no-build` start, candidate direct health, DB owner/epoch fence, nginx lock 직전 health 재확인, routed health 실패 시 즉시 rollback, standby same-digest 동기화 확인.
- 롤백 경로: routed health failure면 nginx route를 기존 active slot으로 즉시 복귀하고 candidate를 비활성화한다. 코드 롤백은 본 작업의 4개 코드/test hunk만 역적용 가능하며 삭제 파일은 없다.
- P0/P1 관찰 계획: cutover 뒤 5분 동안 routed health, error/5xx, browser/recovery reason code, owner lease 및 LLM 비용/호출량을 관찰한다.
- 운영 DB 및 handover DB 기록은 수행하지 않았다. 공식 Runner handover 경로에서 기록해야 하며, 불가하면 원 세션이 기록해야 한다.

---

# AADS-GOAL-V12-W14A-R1-20260919

- 구현: immutable change set body hash/target version, 독립 다중 승인, reject-wins,
  W-14F 실행 직전 재검증, transaction 내부 mutation/effect/outbox, owner epoch outbox
  claim/ack fence, unknown outcome reconciliation.
- 보안 보강: change-set immutable DB trigger, 신규 3개 테이블 FORCE RLS,
  policy decision principal 결합, UUID/시간 envelope 정규화, ordered patch 적용.
- 검증: `pytest -q tests/unit/test_goal_*.py` **348 passed**; disposable PostgreSQL
  W14a multi-approval/rollback/exactly-once **1 passed**; 전체 migration/RLS **1 passed**;
  Ruff, py_compile, `git diff --check` PASS.
- 운영 DB migration·배포: 미실행.

---

# AADS-GOAL-V12-W14F-POLICY-FOUNDATION-20260919

## STEP 0 기존 구현 조사 및 분류

| 접점 | 분류 | 결과 |
|---|---|---|
| W-13 precondition 함수 3종 | 유지 | 서버 계산 snapshot/실행 직전 stale 계약 유지 |
| `goal_policy_decisions` 및 policy foundation stores | 수정 | 서명 key·ancestor epoch와 executor fence 보강 |
| assignment/grant/kill-switch DB 접점 | 수정 | mutable epoch 재검증용 additive 컬럼·trigger·함수 |
| 4축 evaluator/JCS/hash/HMAC/executor verifier | 신규 | W-14F foundation service 추가 |
| 단일 grant reservation/overrun guard | 신규 | 합성 금지·원자 차감·stale 처리 |
| 집중 unit/integration 검증 | 신규/수정 | T36/T38/T45/T46/T48~T58 및 migration 반복 적용 |
| 기존 router/API와 W12/W13 테스트 | 유지 | 삭제·통째 대체 없음 |
| 삭제 | 없음 | 호출처 영향 및 롤백 대상 삭제 없음 |

지시서 외 service/migration/test/handover 파일 변경 사유는 evaluator/executor 계약과
DB fence 및 독립 검증 증거를 구현하기 위해서다. 상세 분류와 롤백 범위는
`docs/handover/AADS-GOAL-V12-W14F-POLICY-FOUNDATION-20260919.md`에 기록했다.

## 변경 파일

- `app/services/goal_policy_foundation.py`
- `migrations/20260919_goal_policy_foundation_w14f.sql`
- `migrations/20260919_goal_policy_foundation_stores.verify.sql`
- `tests/unit/test_goal_policy_foundation_w14f.py`
- `tests/unit/test_goal_policy_foundation_migration.py`
- `tests/integration/test_goal_policy_foundation_migration.py`
- `docs/contracts/GOAL_POLICY_FOUNDATION_INTERFACE_V1.md`
- `docs/handover/AADS-GOAL-V12-W14F-POLICY-FOUNDATION-20260919.md`
- `RESULT.md`

## 검증 결과

- `git diff --check`: PASS.
- `pytest -q tests/unit/test_goal_*.py`: **340 passed**.
- W-14F/W-13/W-12 집중 unit 4개 파일: **41 passed**.
- disposable PostgreSQL `goal_w14f_r5b_test`에서
  `tests/integration/test_goal_policy_foundation_migration.py`: **1 passed**.
- 독립 검수에서 mutable epoch 서명, 현재 target version, 활성 policy/assignment,
  kill switch, grant ancestor 상태, RFC 8785 binary64/Unicode 경계를 추가 보강했다.
- 러너 생성물 `.runner_full_diff.patch`는 최종 커밋에서 제외했다. 원본은
  `/tmp/runner-2085d6bd-full-diff.patch`로 이동해 복구 가능하다.
- npm/next/docker build: 승인 후 Runner 빌드 검증 대상.
- production DB evidence: 조회·변경하지 않음. B-04 승인 완료를 주장하지 않음.
- commit SHA/push state: 최종 커밋 시 갱신 대상.
- 삭제 0건, production write 0건, 배포 0건.

---

# AADS-GOAL-V12-W13-PRECONDITIONS-BOUNDARY-20260919

## STEP 0 기존 구현 조사

| 접점 | 분류 | 처리 |
|---|---|---|
| `ActorScope`, `resolve_actor_scope`, `require_project_access` | 유지 | DB 원천 identity/project 판정 계약 유지 |
| `create_work_item`, `create_project_assignment`, tree/governance API | 유지 | 기존 M13 경로와 응답을 대체하지 않음 |
| `project_role_assignments` active role/session unique index | 유지 | DB 409 변환 계약 유지 |
| `work_item_dependencies`, `work_item_evidence`, review stores | 유지 | W-12 primary source로 읽기만 수행 |
| `app/routers/work_items.py` identity/API surface | 수정 | 빈 tenant/identity 401 fail-close, W-13 preview/input API 추가 |
| `compute_preconditions`, `create_policy_input`, `require_current_preconditions` | 신규 | 서버 계산·immutable snapshot·실행 직전 stale guard |
| `goal_precondition_snapshots`, `goal_policy_inputs` | 신규 | additive migration, FORCE RLS |
| W-13 unit/disposable PostgreSQL checks | 신규 | T56/T57, hash 결정성, repeat migration/RLS |
| 삭제 | 없음 | 기존 계약 삭제 불필요 |

지시서에 직접 열거되지 않은 파일 중 migration과 test 파일을 변경/추가한 사유는
버전 snapshot 영속화와 disposable PostgreSQL 완료기준을 코드로 검증하기 위해서다.

## 결과

- 기준 HEAD/origin-main: `0f98c5eefc56305944c90cce2d748a41623e0dee`
- 선행 `e1061c657a59fadf612c30bb294c432221aed6e6`: origin/main 조상 확인
- 서버 계산 필드: parent state/version, evidence completeness/hash, review verdict,
  blocker/dependency blocker count
- client precondition object는 request model에서 입력으로 채택하지 않으며
  `expected_parent_version`만 optimistic check에 사용
- CEO integrated는 active local project assignment가 없으면
  `403 local_assignment_required`; 일반 session/assignment project 불일치는 403
- 다른 tenant resource는 외부 `404 resource_not_found`, 내부
  `tenant_scope_denied` work-item event 기록
- policy input은 `automation_claimed=false`, `decision_id=null`,
  `reason_codes=[evaluation_required]`이며 AUTO를 주장하거나 grant를 소비하지 않음
- 변경 파일 삭제 0건, production write 0건

## 검증

- `python3 -m py_compile ...`: PASS
- `git diff --check`: PASS
- AAG baseline: PASS (`고정선 대비 증가 없음`)
- pytest target: HOLD — 현재 호스트 Python에 `fastapi`가 없어 collection 중단
- disposable PostgreSQL: 코드 추가, 미실행 (`M12_TEST_DATABASE_URL` 미제공)
- pre-commit: 미실행 (staging이 필요한 hook이며 `git add` 금지)
- npm/next/docker build: 승인 후 Runner 빌드 검증 대상
- commit/push/deploy: 미실행(사용자 필수 규칙)

---

# AADS-VISION-UNIFY-20260917-R3 — 비전 입력 통합 (extract_image_blocks 보존)

`build_vision_blocks()` 를 **신규 추가**하고, 기존 public 함수
`extract_image_blocks()` 는 선언·시그니처·docstring 을 그대로 둔 채 본문만
위임으로 바꿨다. R1 을 막았던 PRESERVATION_HARD_GATE(=public 함수 삭제·재추가)
를 피하려고, 새 함수를 `extract_image_blocks` **뒤에** 배치해 diff 에서 선언 줄이
context(변경 없음)로 남도록 했다.

## 전제 확인 — R2 내용은 이미 이 브랜치에 있다

지시서는 "R2 diff a6064a06 기준으로 재구성" 이라고 했으나, 실측 결과 a6064a06 의
내용은 `e4f9468f` 로 이미 현재 HEAD(a86fa739) 계보에 들어와 있다.

```
git diff a6064a06 HEAD -- app/core/document_context.py app/services/chat_service.py \
    app/core/anthropic_client.py tests/unit/test_vision_blocks.py app/services/design_auditor.py
# → 출력 없음 (동일)
```

따라서 R3 에서 실제로 남은 델타는 ①`build_vision_blocks` 분리 ②chat_service 호출부
이름 교체 ③anthropic_client 의 러너 경로 변환 ④heic/heif 사유 처리 ⑤테스트 보강
다섯 가지다. R2 를 통째로 다시 얹지 않았다(중복 재적용 방지).

## STEP 0 — 기존 구현 조사 및 분류

### `app/core/document_context.py` (수정 전 705행)

| 항목 | 분류 | 근거 |
|---|---|---|
| `estimate_tokens`, `extract_file_contents`, `_extract_pdf*`, `_extract_excel`, `build_ephemeral_document_layer`, `build_file_reference_summary`, `detect_file_rereference`, `build_rereference_context` | 유지 | 비전 경로와 무관 |
| `_is_sensitive_path`, `_downscale_to_limit`, `_convert_to_png` | 유지 | 손대지 않음 |
| `_build_block_from_bytes` | 수정 | HEIF 분기를 UNSUPPORTED 판정 **앞에** 삽입. 그 외 경로 불변 |
| `extract_image_blocks` | 수정(본문만) | 선언 줄·시그니처·docstring 보존, 본문 = `return build_vision_blocks(...)` |
| `build_vision_blocks` | 신규 | 실제 로직(기존 본문을 그대로 이동) |
| `_convert_heif_to_png` | 신규 | pillow_heif 있으면 PNG, 없으면 None |
| `HEIF_EXTENSIONS`, `HEIF_MEDIA_TYPES` | 신규 | 상수 |
| `UNSUPPORTED_IMAGE_EXTENSIONS` | 유지 | `.heic/.heif` 항목도 **제거하지 않았다** — HEIF 분기가 먼저 걸리고, 그 분기가 실패하면 여기 규칙이 그대로 남아 있어야 안전 |
| 삭제 | **0건** | — |

### `app/core/anthropic_client.py`

| 항목 | 분류 | 근거 |
|---|---|---|
| `_user_content`, `_to_openai_image_content`, `_try_user_key_claude`, `_call_dashscope`, `_call_litellm` | 유지 | R2 에서 이미 `images` 를 받는다 |
| `call_llm_with_fallback` | 수정 | 본문 첫 줄에 `images = _normalize_images(images)` 한 줄 + docstring 보강. 시그니처 불변 |
| `_normalize_images` | 신규 | 러너가 넘기는 디스크 경로(str/PathLike) → `build_vision_blocks(extra_paths=...)` |
| 삭제 | **0건** | — |

정규화를 `call_llm_with_fallback` 진입부 한 곳에만 넣은 이유: 그 아래 BYOK·OAuth·
DashScope·LiteLLM 네 경로가 모두 같은 `images` 를 재사용하므로, 여기서 한 번
바꾸면 폴백 경로까지 자동으로 덮인다.

### `app/services/chat_service.py`

| 항목 | 분류 | 근거 |
|---|---|---|
| `send_message_stream` attachments 경로(11739·11751) | 수정 | `extract_image_blocks` → `build_vision_blocks` (import + 호출 + 주석) |
| `uploaded_files` 경로(11824·11825) | 수정 | 동일 |
| `file_id` 첨부 인라인 image block(11762~11772) | **유지** | 지시서가 허용한 교체 범위는 `extract_image_blocks` 호출 2곳뿐이다. 이 경로는 아직 인라인으로 block 을 만들고 있어 포맷 변환·5MB 축소·중복 제거를 받지 못한다 — 다음 작업 후보로 남긴다(아래 "남긴 것") |
| 삭제 | **0건** | — |

### `app/services/design_auditor.py`

**변경 0건** (`git diff --stat` 출력 없음). 지시서 금지 항목.

## 변경 파일 (4개)

```
 app/core/anthropic_client.py     | +38
 app/core/document_context.py     | +61 -1
 app/services/chat_service.py     |  +3 -3
 tests/unit/test_vision_blocks.py | +95
```

## 동작

- png/jpg/jpeg/gif/webp → 네이티브 image block (원본 재인코딩 없음)
- bmp/tiff/tif/ico/ppm/pcx → Pillow 로 PNG 변환
- 5MB 초과 → 장변 1568px 로 축소해 **전송**(폐기하지 않음), 실패 시에만 스킵
- pdf → 네이티브 document block (32MB 초과 시 스킵)
- heic/heif → pillow_heif 있으면 PNG, 없으면 사유를 남기고 스킵
- SHA-256 중복 제거(첨부와 extra_paths 에 같은 바이트가 오면 1건)

### heic 사유 문자열에 대한 해석

지시서는 "pillow_heif 없으면 사유 문자열 반환" 이라고 했으나,
`_build_block_from_bytes`/`build_vision_blocks` 의 반환 타입은 content block 목록이라
사유 문자열을 섞으면 호출처(messages content)가 그대로 깨진다. 그래서 **사유는
로그로 남기고 block 목록에서는 제외**하는 R2 방식을 유지했다:

```
[Vision] [unsupported_image_format: .heic] photo.heic — pillow_heif 미설치로 건너뜀
```

기존 테스트(`test_heic_skipped_with_log`)도 `blocks == []` 를 요구하고 있어 이쪽이
계약과 일치한다. 사용자에게 사유를 노출해야 한다면 반환 타입을 바꾸는 별개 작업이다.

## 검증

| 항목 | 결과 |
|---|---|
| `grep '^def extract_image_blocks' document_context.py` | 616행 존재 ✅ |
| diff 에서 `def extract_image_blocks(` 가 `-`/`+` 로 안 나옴 | ✅ (context 로만 등장) |
| `grep '^def build_vision_blocks'` | 686행 신규 ✅ |
| `design_auditor.py` 변경 | **0건** ✅ |
| `run_unit_tests.sh tests/unit/test_vision_blocks.py` | **32 passed** ✅ |
| `run_unit_tests.sh tests/unit/test_tools_and_pipeline.py` | **73 passed** ✅ (기준선 68 이상) |
| `ruff check --select F821,F811` (변경 4파일) | All checks passed ✅ |
| 운영 이미지 + 워킹트리 마운트 import | `document_context`·`anthropic_client` import ok, `chat_service` 컴파일 ok ✅ |
| `extract_image_blocks` 잔존 호출처(app/) | 0건 — 정의만 남음 ✅ |

추가한 테스트 10건: 위임 확인(spy), 구/신 이름 결과 동일, 혼합 입력, 빈 입력,
HEIF 변환 가능/불가, `_normalize_images` 경로 변환·통과·잘못된 경로,
`call_llm_with_fallback(images=[경로])` end-to-end.

## 남긴 것

`chat_service.py` 의 `file_id` 첨부 경로(11762~11772)는 여전히 인라인으로 image
block 을 만든다. 지시서가 허용한 교체 범위(호출 2곳) 밖이라 건드리지 않았다.
이 경로로 들어온 5MB 초과 이미지나 bmp/heic 은 통합 파이프라인을 타지 못해
Anthropic 400 이 날 수 있다 — 다음 라운드에서 `build_vision_blocks` 로 옮길 대상.

---

# AADS-SMARTBROWSER-G2-UNTRUSTED-PAGE-DATA-20260919

## STEP 0 — 기존 구현 조사 및 분류

| 항목 | 분류 | 반영/판단 |
|---|---|---|
| `ChannelRouter.route_directive`, `validate_action_intent`, `route_observation` | 수정 | 인증된 서버 ingress provenance를 확인하고 DOM/ARIA/OCR/RAG/file taint가 명령으로 승격되면 `PAGE_DATA_COMMAND_ATTEMPT`로 차단한다. |
| `DirectiveEnvelope`, `ActionIntent` | 수정 | `authenticated_provenance`를 유지·실행 경계까지 전달한다. caller의 source 라벨만으로 신뢰하지 않는다. |
| `ObservationEnvelope` | 수정 | 기본·필수 taint를 `UNTRUSTED_PAGE_DATA`로 고정한다. |
| `directive_from_authenticated_context` | 신규 | request의 서버 인증 context에서 tenant/user/source를 결선하는 유일한 API ingress 생성기다. |
| `/browser-tasks` 생성 ingress | 수정 | request context 기반 directive 생성으로 교체했다. screenshot 관측은 기존 ObservationEnvelope 경로를 유지한다. |
| `/ohvis/console/command`, `/recipes/run` 및 `run_directive` | 수정 | 레시피 directive와 inputs를 같은 ActionIntent에 묶고, resume/별도 인자에 의한 taint 우회를 실행 직전 재검사한다. |
| `/pc-agent/execute`, `/pc-agent/route-execute` | 수정 | PC 전송 직전 params의 taint를 fail-closed 재검사한다. |
| `ToolExecutor.execute` | 수정 | LLM이 생성한 tool input에서 taint 발견 시 dispatch 전에 구조화된 차단 결과를 반환한다. |
| 기존 recipe guard의 `sanitize_page_text`, `assert_not_page_derived` | 유지 | 문자열 패턴 방어는 보조층으로 보존하며 구조적 provenance 검사를 대체하지 않는다. |
| 삭제 | 0건 | 호출처 삭제 및 롤백 대상 없음. 롤백은 이 작업의 변경 hunks만 되돌리면 된다. |

## 변경 및 보안 경계

- DOM/ARIA/OCR/screenshot OCR/downloaded file/RAG/file 관측은 `ObservationEnvelope`로만 수용하며 `UNTRUSTED_PAGE_DATA` taint를 유지한다.
- 페이지 텍스트의 태그 탈출 문자열은 신뢰 태그가 아니라 구조적 taint로 판정하므로 command/tool capability를 얻지 못한다.
- Browser recipe, PC command, LLM tool dispatch 각각에서 실행 직전 taint를 재검증한다. 차단은 감사 로그에 `reason_code=PAGE_DATA_COMMAND_ATTEMPT`로 남는다.
- 정상적인 사용자 directive 및 사용자 입력 검색어는 taint marker가 없으므로 계속 허용된다. tainted cross-tenant 값은 명령 채널로 들어오기 전에 차단된다.

## 검증

| 항목 | 결과 |
|---|---|
| 간접 프롬프트 인젝션 golden cases | `tests/unit/test_channel_router.py`에 DOM/ARIA/OCR/RAG/file, 태그 탈출, tool-call, cross-tenant tainted input 차단 케이스 추가 |
| 정상 추출 회귀 | 동일 테스트에 일반 사용자 검색 인자 허용 케이스 추가 |
| focused/affected test 실행 | 실행하지 않음 — 사용자 규칙상 코드 수정만 수행 |
| 빌드 검증 | 승인 후 Runner 빌드 검증 대상 |
| commit/push/deploy | 실행하지 않음 |

| AADS handover DB evidence | DB 변경/기록을 수행하지 않음 — 사용자 규칙상 파일 수정 외 작업 금지 |

---

# AADS-SMARTBROWSER-G4-ARIA-PARTIAL-SIGNATURE-20260919

## STEP 0 — 기존 구현 조사 및 분류

| 항목 | 분류 | 반영/판단 |
|---|---|---|
| `browser_recipes` 및 `browser_recipe_registry`의 recipe/version tenant 정본 | 유지 | 사이트·페이지 템플릿 정본을 새 테이블로 복제하지 않는다. `capture_rules.aria_signature`만 읽어 재방문 정책을 적용한다. |
| `normalize_recipe_payload`, `get_browser_recipe`, `plan_browser_recipe_run`, `create_browser_recipe_run` | 유지 | 실행·큐·승인 흐름을 변경하지 않는다. |
| `browser_page_structure_signatures` migration | 신규 | tenant·recipe·version·page/area별 signature version, hash, similarity, reason, decision과 Human Gateway 필요 여부를 additive하게 저장한다. |
| `aria_structure_signature` | 신규 | ARIA subtree의 role/안정 이름 해시/state/의미 관계/stable data attribute만 정규화하고 재사용·재탐색·Human Gateway를 판정한다. |
| `tests/unit/test_aria_structure_signature.py` | 신규 | 허용 변화, 필수 anchor/role 변경, ambiguity, ARIA 부재 폴백, 동적·개인화 텍스트 배제를 회귀한다. |
| 삭제 | 0건 | 호출처 영향 없음. 롤백은 신규 서비스·migration·테스트 변경만 되돌리면 된다. |

## 변경 및 보안 경계

- raw accessible name, raw relationship id, DOM id, 가격·재고·광고·시각·개인화 텍스트는 시그니처에 저장하지 않는다. 안정적인 이름은 정규화 후 SHA-256 해시만 저장한다.
- `data-testid`, `data-test`, `data-qa`, `data-cy`, `data-component`만 허용하며 값도 해시로 저장한다. 형제 순서는 정렬해 비교한다.
- ARIA 부재·중복 구조는 `rediscover`, 필수 anchor 삭제는 `human_gateway`로 판정한다. similarity가 recipe template의 threshold 이상일 때만 `reuse`다.
- 모든 DB 조회/저장은 `tenant_id`와 recipe/version/page/area 범위로 제한한다. 서명 이력에 사용자 맞춤 텍스트를 보관하지 않는다.

## 검증

| 항목 | 결과 |
|---|---|
| focused regression | `pytest -q tests/unit/test_aria_structure_signature.py` — **4 passed** (pytest 설정 경고 1건) |
| affected regression | `tests/unit/test_browser_task_policy.py` 포함 실행은 환경의 `asyncpg` 미설치로 collection 실패. 변경 전 의존성 문제이며 통과로 처리하지 않음. |
| migration/DB handover evidence | DB 변경·handover 기록 미수행 — 사용자 규칙상 파일 작업 외 실행 금지. migration은 Runner 적용 대상. |
| 빌드 검증 | 승인 후 Runner 빌드 검증 대상 |
| commit/push/deploy | 모두 미실행 |

---

# AADS-SMARTBROWSER-M4-RECOVERY-R3-20260919

## STEP 0 — 기존 구현 조사 및 분류

| 항목 | 분류 | 반영/판단 |
|---|---|---|
| `browser_recipes` / `browser_recipe_registry`의 tenant별 recipe/version 정본 | 유지 | 정본을 복제하거나 recovery가 직접 수정하지 않는다. 후보 patch는 기존 G6 learned artifact lifecycle에만 생성한다. |
| `aria_structure_signature.assess_revisit` / `record_revisit_signature` | 유지 | G4의 `reuse`/`rediscover`/`human_gateway` 판정 및 개인정보 배제는 그대로 사용한다. |
| `golden_promotion_gate.evaluate_promotion_gate`, learned artifact candidate→shadow→active 및 rollback | 유지 | recovery는 `candidate`만 만들며 승격을 호출하거나 우회하지 않는다. G6 gate와 기존 rollback만 active 전환을 허용한다. |
| `browser_recipe_recovery` | 신규 | selector/ARIA, 로그인 만료, 일시 네트워크 오류를 분류하고 bounded·idempotent 복구 결정을 기록한다. |
| `browser_recipe_recovery_events` migration | 신규 | tenant/recipe/version/site/page/skill/version 및 evidence hash·retry 한도·candidate 참조만 additive하게 보관한다. |
| `/browser-recipes/{recipe_id}/versions/{version}/recovery` | 신규 | MEMBER 권한과 기존 tenant-scoped recipe 존재 확인 뒤 recovery를 기록한다. |
| `tests/unit/test_browser_recipe_recovery.py` | 신규 | 실패 분류, 재시도 한도, 민감정보·페이지 명령문 미기록, tenant scope, candidate-only 경로를 회귀한다. |
| 삭제 | 0건 | 호출처 영향 및 별도 롤백 대상 없음. 롤백은 본 작업의 신규 migration/service/API/test hunks만 되돌리면 된다. |

## 변경 및 보안 경계

- recovery evidence는 허용된 SHA-256 hash와 node count만 저장한다. raw credential/cookie/OTP/error text/page command는 저장하지 않는다.
- selector 재발견은 제한된 CSS selector 형식만 후보 patch에 보관하며, `javascript:`·명령문·credential marker는 거부한다.
- tenant와 recipe/site/page/skill/version scope 및 idempotency key를 모든 recovery 조회·저장에 적용한다. 네트워크 재시도는 scope별 최대 2회이고 동일 idempotency key는 replay한다.
- 로그인 만료와 재시도 한도 초과는 명확한 Human Gateway 안내를 반환한다. selector 변경·ARIA 불일치는 rediscover로만 진행하고, candidate 상태의 G6 artifact version만 생성한다.
- active recipe 또는 active learned artifact로 자동 승격하지 않는다. shadow/active 전환은 기존 G6 golden·regression gate가 통과한 별도 promotion 경로에서만 가능하다.

## 검증

| 항목 | 결과 |
|---|---|
| focused/affected pytest, Ruff | 실행하지 않음 — 사용자 규칙상 파일 수정 외 명령 실행 금지 |
| migration `BEGIN/ROLLBACK` 검증 | 실행하지 않음 — 사용자 규칙상 DB 작업 금지 |
| `git diff --check` | 실행하지 않음 — 사용자 규칙상 파일 수정 외 명령 실행 금지 |
| 빌드 검증 | 승인 후 Runner 빌드 검증 대상 |
| commit/push/deploy | 실행하지 않음 |

# AADS-SMARTBROWSER-M8-AUTO-LEARNING-R6-20260920

## STEP 0 — 기존 구현 조사 및 분류

| 항목 | 분류 | 반영/판단 |
|---|---|---|
| `site_knowledge.create_page_template_candidate`, tenant/site canonical lookup | 유지 | 기존 수동 candidate 생성과 G6 정본을 변경하지 않고 자동 방문 경로가 동일 artifact/version 테이블을 사용한다. |
| `aria_structure_signature.build_partial_signature`, `assess_revisit` | 유지 | G4 role/name/state/관계 기반 비교와 동적 텍스트 배제 정책을 그대로 호출한다. |
| `ohvis_harness.validate_skill_manifest`, executor registry, G6 promotion gate | 유지 | eval/import 없이 등록 callable만 실행하는 계약 및 candidate→shadow→active gate를 우회하지 않는다. |
| `smart_browser_learning.auto_learn_site_visit` | 신규 | 최초 방문 paired candidate 생성, 재방문 reuse/Human Gateway/invalidation, 단조 version 할당을 scope row lock transaction으로 수행한다. |
| `smart_browser_learning._pending_candidate` | 신규 | 동일 transaction의 두 pending candidate 조회를 공통 private helper로 통합하여 중복 guard를 해소한다. |
| `/site-knowledge/profiles/{site_profile_id}/auto-visit` | 신규 | 인증 tenant context와 untrusted observation channel을 결선하고 후보 상태만 반환한다. |
| `browser_site_learning_scopes` migration/rollback | 신규 | tenant/site/page unique scope와 `FOR UPDATE` allocator, tenant-bound trigger를 additive/idempotent하게 제공한다. |
| focused tests | 수정 | 자동 skill 계약, candidate-only 경로, migration/rollback, API route를 정적 회귀한다. |
| 삭제 | 0건 | 호출처 삭제 및 데이터 삭제 없음. rollback은 scope allocator만 제거하고 canonical artifact/version은 보존한다. |

## 변경 파일

- `app/services/smart_browser_learning.py`
- `app/api/site_knowledge.py`
- `migrations/20260920_m8_auto_site_learning.sql`
- `migrations/rollback/20260920_m8_auto_site_learning.down.sql`
- `tests/unit/test_smart_browser_learning.py`
- `tests/integration/test_smart_browser_learning_postgres.py`
- `RESULT.md`

## 검증 결과

| 항목 | 결과 |
|---|---|
| pending candidate 중복 | 공통 `_pending_candidate()` helper 1개와 호출 2곳으로 통합한 소스 수준 확인. |
| focused + M7/G1/G2/G4/G6 영향 회귀 | `run_unit_tests.sh` — **83 passed**. |
| PostgreSQL 동시성·tenant 격리 | disposable `smartbrowser_m8_verify_0928` — **1 passed**; 동시 최초 방문이 candidate 1쌍만 생성하고 타 tenant 접근을 차단. |
| Ruff, `py_compile`, `git diff --check` | 모두 PASS. |
| disposable PostgreSQL migration 2회 적용 및 rollback/reapply | 2회 적용 성공 → rollback 후 테이블 `ABSENT` → reapply 성공. |
| npm/next/docker build | 실행하지 않음 — 승인 후 Runner 빌드 검증 대상. |
| commit SHA | 이 결과와 통합 테스트를 포함한 최종 amend 커밋으로 확정. |

## 미완료 항목

- push/deploy는 후속 승인·M9~M11 의존 체인에서 수행한다.

# AADS-SMARTBROWSER-M9-ROUTING-R2-20260920

## 변경 및 보안 경계

- `resolve_site_skill`은 tenant/site/active version/capability 범위 안에서 Exact → Qwen3 vector → bounded LLM 순서를 강제하고 단계별 reason/score/threshold/cost/latency를 감사 이벤트에 기록한다.
- `plan_skill_runtime`은 요청 capability가 manifest capability의 부분집합인지, manifest permission이 서버 인증 membership 권한에 포함되는지 각각 검증한다. 둘을 혼용하지 않는다.
- Browser/PC Agent executor 계약이 없거나 세션·로컬 환경·고위험 승인 조건이 부족하면 Human Gateway로 fail-closed 한다. 클라이언트가 capability를 비워 manifest permission을 우회할 수 없다.
- 기존 JSONB 감사 이벤트 저장소를 재사용해 신규 migration은 필요하지 않다.

## 변경 파일

- `app/services/smart_browser_learning.py`
- `app/api/site_knowledge.py`
- `tests/unit/test_smart_browser_learning.py`
- `RESULT.md`

## 검증 결과

| 항목 | 결과 |
|---|---|
| 최초 러너 회귀 | 1 failed, 87 passed — 누락된 `required_capabilities`를 재현하고 승인 차단 |
| 교정 후 focused + M7/M8/G1/G2/G6 영향 회귀 | **91 passed** |
| 권한 음성 테스트 | 빈 capability 우회, browser executor 부재, 고위험 승인 부재를 Human Gateway로 차단 |
| Ruff, `py_compile`, `git diff --check` | 모두 PASS |
| migration | 스키마 변경 없음; M7/M8 정본과 기존 JSONB 감사 저장소 재사용 |

## 미완료 항목

- push/deploy는 M10·M11 순차 완료 후 릴리스 단계에서 수행한다.

# AADS-SMARTBROWSER-M10-LIVE-DATA-R3-20260920

## STEP 0 기존 구현 조사 및 분류

| 접점 | 분류 | 처리 |
|---|---|---|
| `live_fact_gate.display_fact`, `guard_payload_for_display` | 수정 | 기존 G5 최종 표시 gate를 유지하고 STALE/CONFLICT/UNAVAILABLE `reason_code`와 복구 행동을 추가했다. |
| `live_fact_gate.revalidate_live_fact`, revalidator registry | 수정 | 기존 서버 등록형 read-only 원출처 재검증을 유지하고 context/source/evidence/value 불일치를 fail-closed로 분리했다. |
| `live_fact_gate.record_live_fact` | 수정 | fact/provenance/freshness와 최초 evidence event를 단일 transaction에 저장한다. |
| `site_knowledge.record_live_observation` 및 API | 수정 | M7 tenant/site 정본을 재사용하고 live fact 오류 변환과 raw DOM/ARIA/OCR key 차단을 보강했다. |
| browser task/artifact/chat 최종 응답, Redis SSE replay gate | 유지 | 기존 G5 결선을 재사용한다. 중복 구현하지 않았다. |
| `20260920_m10_live_fact_freshness` migration/rollback | 신규 | `fetched_at`, reason ledger, tenant/site·event composite FK, fact type/TTL constraint, tenant RLS를 additive/idempotent하게 추가한다. |
| focused/PostgreSQL tests | 신규·수정 | TTL boundary, mismatch, source failure, SSE, tenant/constraint/rollback 계약을 검증한다. |
| 삭제 | 0건 | 호출처·데이터 삭제 없음. rollback은 M10 enforcement만 제거하고 observation 데이터와 추가 column은 보존한다. |

지시서 밖 파일 변경은 없다. 기존 G5/M7 구현은 대체하지 않았다.

## 변경 파일

- `app/services/live_fact_gate.py`
- `app/services/site_knowledge.py`
- `app/api/site_knowledge.py`
- `migrations/20260920_m10_live_fact_freshness.sql`
- `migrations/rollback/20260920_m10_live_fact_freshness.down.sql`
- `tests/unit/test_live_fact_gate.py`
- `tests/unit/test_site_knowledge.py`
- `tests/integration/test_live_fact_m10_postgres.py`
- `RESULT.md`

## 검증 결과

| 항목 | 결과 |
|---|---|
| 기준선 | HEAD/origin/main `a8e7d431`; M9 포함 확인, 시작 시 clean detached worktree. |
| focused + M7~M9/G1/G2/G5/G6 영향 회귀 | `./scripts/run_unit_tests.sh ...` — **89 passed**. |
| 최초 focused | **28 passed**. |
| Ruff / py_compile / diff-check / pre-commit hook | 모두 PASS. |
| disposable PostgreSQL 2회/rollback/reapply | 격리 DB `smartbrowser_m10_verify_1002`에서 2회 적용, tenant/site FK, RLS 격리, rollback/reapply — **1 passed**. |
| npm/next/docker build | 실행하지 않음 — 승인 후 Runner 빌드 검증 대상. |
| commit SHA | 러너 산출물 `e95c52efd0be45ae26bf2f8c6903767975d65917`; 독립 검증 보강은 후속 커밋에 기록한다. |

## 미충족 항목

- 없음. 운영 적용은 M11 완료 후 단일 블루그린 릴리스에서 수행한다.

# AADS-SMARTBROWSER-M11-E2E-RELEASE-R4-20260920

## 구현·교정

- 실제 Playwright 읽기전용 쇼핑 검색, 최초/재방문/ARIA 무효화 3단계 캡처와 ARIA·채팅 artifact를 생성한다.
- production Exact→Qwen3 vector resolver, live fact 표시 gate, Human Gateway, G6 golden gate를 한 여정에서 검증한다.
- disposable PostgreSQL에서 실제 `auto_learn_site_visit` candidate 생성, Page Template·Site Skill shadow→active 승격, exact/vector 재사용, ARIA 필수 anchor 제거 후 version 2 candidate 생성과 version 1 active 보존을 검증한다.
- M10 raw 캡처 차단 정규식의 `dom_fallback` 오탐을 교정했다.
- asyncpg JSONB 문자열을 Site Skill 승격 검증기가 object로 복원하지 못하던 런타임 결함을 교정했다.

## 변경 파일

- `scripts/smart_browser_readonly_e2e.py`
- `tests/unit/test_smart_browser_readonly_e2e.py`
- `tests/integration/test_smart_browser_m11_postgres.py`
- `app/services/site_knowledge.py`
- `tests/unit/test_site_knowledge.py`
- `app/services/ohvis_harness.py`
- `tests/unit/test_ohvis_harness.py`
- `RESULT.md`

## 검증 결과

| 항목 | 결과 |
|---|---|
| Playwright E2E | **PASS**, 15/15 checks, 2,626ms, write action 0, LLM call 0, 비용 $0.00 |
| 화면 근거 | `/tmp/aads-smart-browser-e2e-final/01-first-learning.png`, `02-revisit-price.png`, `03-aria-invalidated.png` |
| artifact | `aria-snapshots.json`, `chat-artifact.json`; exact/vector/Human Gateway reason code 포함 |
| focused + 영향 회귀 | **111 passed**, 0 failed |
| PostgreSQL 실전이 | `smartbrowser_m11_verify_1015` — **1 passed**, 실제 candidate→active→candidate+1/active 보존·exact/vector 검증 |
| 정적 검사 | py_compile·diff-check PASS; Ruff는 기존 `ohvis_harness.py`의 BLE001/S110/RUF100 경고만 제외하고 PASS |
| 최초 결함 재현 | `dom_fallback` 오탐과 JSONB manifest 문자열 승격 실패를 실제 DB E2E에서 재현 후 교정 |

## 남은 릴리스 항목

- clean release SHA fast-forward push, `deploy.sh bluegreen`, 동일 digest standby, 외부 health 200, 5분 P0/P1 감시, 배포 후 화면 캡처와 DB handover 기록.
