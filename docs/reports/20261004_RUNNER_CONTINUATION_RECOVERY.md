# RUNNER-CONTINUATION-RECOVERY (2026-10-04)

TASK_ID: AADS-RUNNER-CONTINUATION-RECOVERY-20261004 · worktree `/tmp/aads-wt-runner-f0ab94ee`

이 문서는 이 작업 전용 보고서다(공유 HANDOVER.md 를 건드리지 않는다).
"보고됨" 과 "복구됨" 을 구분해 적는다. **이번 작업에서 DB 를 바꾼 것은 없다 — 복구된 것은 0건이다.**

## 0. STEP 0 — 기존 구현 분류

| 항목 | 분류 | 내용 |
|---|---|---|
| `notify_completion` 승인 재검수 트리거 + 직전 재조회 stale 가드 | 유지 | 그대로. 승인 가드는 제거하지 않았다. |
| `chat_service.enqueue_next_step_reaction` / `_process_deferred_reactions_once` | 유지 (READ_ONLY) | 내구 큐·advisory lock·active slot·lease 건너뛰기를 그대로 재사용 |
| `notify_completion` 의 `TERMINAL_JOB_STATUSES` 조기 return | 수정 | done/error 만 종결 검토를 큐에 넣고 반환. cancelled/rejected_done 은 기존대로 `terminal status: X` 로 억제 |
| 종결 검토 이벤트 (`runner_terminal:<job>:<status>:<sha>`) | 신규 | `_enqueue_terminal_followup` 외 3개 헬퍼 |
| `_parse_write_scope` 의 READ_ONLY 처리 | 수정 | 외부 절대경로(`/root/aads/AGENTS.md`)는 읽기 전용 항목에서 무시. 쓰기 항목(TARGET/WRITE)에서는 여전히 보수 모드 |
| `_extract_target_files` | 유지 | 시그니처·반환 형식 불변 |
| 자동 의존 edge 계획/적용/롤백 + `POST /pipeline/dependency-repair` | 신규 | 기본 dry-run, 관리자 권한, plan_hash 확인 필요 |
| `_release_auto_file_dependency`, `_cancel_explicit_orphan` | 유지 | 종결 부모 처리 기존 경로 |
| `scripts/pipeline-runner.sh` (공유 dirty), `chat_service.py` | 유지 (READ_ONLY) | 수정하지 않음 |
| 삭제 | 없음 | 삭제 대상 없음 → 호출자 영향/롤백 해당 없음 |

지시서에 없는 파일의 변경: **없음**. `pipeline_runner_service.py` 는 읽기만 했고 수정하지 않았다
(TARGET_FILES 에 있었으나 `TERMINAL_JOB_STATUSES` 정의가 이미 단일 정의여서 수정이 불필요).
추가된 테스트는 `tests/unit/test_pipeline_runner_notify_contract.py` 의 기존 done 테스트 1개 수정이다
(조기 return 계약이 의도적으로 바뀌었기 때문; 지시서 목록에 없는 파일이므로 사유를 여기 남긴다).

## 1. (a) 종결 후속 검토 누락

**확인한 원인.** `notify_completion` 의 `if status in TERMINAL_JOB_STATUSES:` 블록이 무조건 return 해서,
아래쪽 done/error 후속 검토 branch 가 도달 불가능한 코드였다. 07:45 에 끝난 execution 뒤로 자동 턴이 이어지지 않은 이유다.

**수정.** 종결 블록에서 done/error 일 때만 `_enqueue_terminal_followup` 호출 → `chat_deferred_reactions` 에 적재.

- 멱등 키 `runner_terminal:{job}:{status}:{sha|nosha}` — 같은 job/status/SHA 는 한 번만, 다른 status·SHA 는 각각 한 번.
- 프로세스 메모리 태스크(`asyncio.create_task`)에 의존하지 않는다 → 슬롯 교체·재시작 뒤에도 DB 행이 남아 큐 소비자가 배달한다.
- 배달 경로는 기존 큐 소비자: active API 슬롯 소유 확인, 실행 lease 가 살아 있는 세션은 건너뜀 → CEO 응답 중단 없음.
- 테넌트: `session_ok` 쿼리로 job 의 테넌트와 세션 테넌트를 대조, 불일치는 `session_tenant_mismatch` 로 건너뜀.
- 메시지에는 "AI 검수 대기" 표식이 없고 키 접두도 `next_step:` 가 아니다 → stale 승인 가드와 freshness 가드를 우회하지만,
  승인 재검수 트리거 자체는 여전히 폐기된다(테스트로 두 방향 모두 고정).
- 쉘 POST 한 번만 성공해도 DB 에 이벤트가 남는다. 쉘 `_notify_ai` 자체(READ_ONLY)는 건드리지 못했다.

## 2. (b) 파일명 오탐

d48dd62e / 7f777952 이후 HEAD 에서 지시서의 재현 케이스(`TARGET_FILES: src/app/docs/page.tsx` + 본문 `page.tsx`)는
이미 해결돼 있다 — **회귀가 아니라 "당시 산출물에 아직 반영되지 않은" 경우**였다
(`test_directive_false_positive_case_returns_only_declared_file` 로 고정).

남은 실제 결함은 다른 것이었다: `READ_ONLY_FILES: /root/aads/AGENTS.md` 같은 **외부 절대경로**가
파싱 오류로 처리돼 지시서 전체가 보수(legacy) 스캔으로 강등되고, 본문 basename 이 쓰기 범위로 읽혔다.
최소 수정: 읽기 전용 종류에 한해 외부 절대경로 토큰을 무시한다(`_EXTERNAL_ABS_PATH_RE`).
쓰기 항목의 절대경로는 그대로 보수 모드(테스트 있음). 실제 `/chat/page.tsx` 동일 파일 충돌은 유지된다.

## 3. (c) 잘못된 자동 의존 edge 복구 경로

`POST /pipeline/dependency-repair` (관리자 권한):

1. **dry-run(기본)** — 모든 depends_on edge 를 분류: `explicit_keep`, `parent_unknown_keep`, `parent_terminal_keep`,
   `real_conflict_keep`, `weak_overlap_review`, `child_not_queued_keep`, `other_conflict_manual`, `false_auto_edge`.
   `file_conflict_auto_dependency` 이벤트가 있는 edge 만 자동으로 본다. 사이클 노드 표시.
2. **적용** — `false_auto_edge` 만, dry-run 이 돌려준 `plan_hash` 가 일치할 때만. 조건부 UPDATE
   (`status='queued' AND depends_on=<검토한 부모>`), 이벤트 로그 + pg_notify. awaiting_approval 등 무관한 작업은 승인/취소하지 않는다.
3. **롤백** — 해제 이벤트 기록으로 원래 부모를 복원(`false_auto_dependency_restored`).
4. `weak_overlap_review` — 교집합이 전부 디렉터리 없는 bare 파일명이면 기계가 단정하지 않고 사람이 본다.
   `include_weak_overlap=true` + `job_ids=[...]` 를 명시해야 대상이 된다(plan_hash 도 달라진다).

mockup job 은 생성·삭제·승인하지 않는다(중복 mockup 생성 경로 없음).

### 실측 체인(읽기 전용 조회만 수행)

| edge | 판정 | 근거 |
|---|---|---|
| e266ec09 → 4fddd8cf | `explicit_keep` | 사람이 건 명시 edge — 유지 |
| 4fddd8cf → c1c59525 | 애매 | 같은 렌더러 파일을 쓰는지 원 세션 확인 필요 |
| c1c59525 → 2066618c(/docs, awaiting_approval) | 오탐 의심 → `weak_overlap_review` | c1c59525 는 TARGET_FILES 없이 "page.tsx 는 건드리지 마라" 만 적었고, 겹침은 legacy 스캔의 bare `server:page.tsx` 뿐 |

**아직 복구되지 않았다.** 원 세션에 dry-run 증거(위 표 + 응답 JSON)를 먼저 보고한 뒤,
승인되면 `include_weak_overlap=true`, `job_ids=['runner-c1c59525']`, dry-run 의 `plan_hash` 로 적용한다.
보고 전에 DB 를 바꾸지 않았다. 정지된 54a97474 execution 의 done job 은 notify 를 다시 POST 하면
(이제 멱등) 종결 검토가 큐에 들어간다 — 운영 조치이므로 실행하지 않았고 원 세션에 알린다.

## 4. 검증 (실제 실행 결과)

| 명령 | 결과 |
|---|---|
| `bash scripts/run_unit_tests.sh tests/unit/test_pipeline_write_scope_recovery.py tests/unit/test_pipeline_terminal_followup.py` | 신규 테스트 모두 통과 (write_scope_recovery 19개) |
| 파이프라인·러너·stale·deferred 관련 `tests/unit` 64개 파일 | 834 passed, 3 skipped, **1 failed** |
| 실패 1건 | `test_doc_index_pipeline.py::test_sdk_injection_does_not_kill_the_turn` — `chat_service` 의 `interrupt_applied` 문자열 검사. 이번 변경과 무관(chat_service 는 READ_ONLY 로 미수정, 이 파일은 diff 에 없음). 수정하지 않음 |
| `ruff check --select F821,F811` (변경 4파일) | All checks passed |
| `python3 -m compileall -q` (변경 파일) | OK |
| `scripts/dup_guard.py --paths ...` | rc=0 |

회귀 테스트 포함: 중복 notify, 슬롯 교체(새 풀·같은 DB), done/error 각 1회, SHA 변경 시 새 이벤트,
테넌트 불일치, enqueue 실패 보고, stale 승인 가드 공존, 약한 교집합 기본 보류/플래그 시 해제/실제 공유 경로 유지.

## 5. 비용

LLM 호출 없음(코드·테스트·읽기 전용 조회만). 예상 $5 한도에 한참 못 미침.

## 6. 오류 사전 / DB 핸드오버 (아직 등록하지 않음)

수정 커밋이 아직 없어 `--fix-commit` 을 채울 수 없고, 원 세션 보고 전이다. Runner 가 승인·커밋한 뒤 확인된 원인만 등록한다.

```
scripts/error_book.py register --key runner.terminal_notify_early_return \
  --symptom "done/error 로 끝난 runner job 뒤에 자동 후속 검토 턴이 이어지지 않음" \
  --cause "notify_completion 의 TERMINAL_JOB_STATUSES 조기 return 이 done/error 후속 branch 를 도달 불가로 만듦" \
  --prevention "종결 이벤트는 프로세스 메모리가 아닌 chat_deferred_reactions 내구 큐 + job/status/sha 멱등 키로 보낸다" \
  --signature "notify_terminal_suppressed" --fix-commit <sha> --fix-note "_enqueue_terminal_followup 추가"

scripts/error_book.py register --key runner.scope_external_abs_readonly_demotion \
  --symptom "READ_ONLY_FILES 에 /root/aads/AGENTS.md 가 있으면 본문 basename 이 쓰기 범위로 잡혀 무관한 작업이 대기" \
  --cause "외부 절대경로 토큰이 파싱 오류로 처리돼 명시 범위 전체가 legacy 스캔으로 강등" \
  --prevention "읽기 전용 목록의 외부 절대경로는 무시, 쓰기 목록만 엄격" \
  --signature "scope_parse_error" --fix-commit <sha> --fix-note "_EXTERNAL_ABS_PATH_RE"
```

## 7. 남은 차단 요소

- 쉘 `_notify_ai`(READ_ONLY)가 POST 를 못 보내면(두 번 모두 실패) DB 이벤트가 생기지 않는다. 별도 작업에서 쉘 쪽에 재시도/sweeper 가 필요하다.
- 실측 체인 복구는 원 세션 검토 대기 중(위 3절).
- 1건 무관 실패(`test_sdk_injection_does_not_kill_the_turn`)는 별도 정리 필요.
- 커밋/푸시/빌드 검증은 CEO 승인 뒤 Runner 가 수행한다. 승인 후 Runner 빌드 검증 대상: `app/api/pipeline_runner.py` 의 API 서버 반영.
