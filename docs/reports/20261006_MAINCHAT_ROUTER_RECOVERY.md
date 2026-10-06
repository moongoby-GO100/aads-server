# 메인 채팅 라우터 실연결 복구 (AADS-MAINCHAT-ROUTER-RECOVER-20261006)

- 작업 기준: origin/main `1f2fc90b` 격리 worktree. 대체 대상: runner-a2c978d5, runner-6c4be959 (approval_commit_failed)
- 상태: **코드 수정·검증 완료, 미커밋/미푸시**(커밋·push 는 승인 후 Runner). 배포·빌드·재시작·운영 migration 없음.

## 0. 배포 통제 선행 확인 (instruction_forbids_deploy)

함수만 `sed` 로 잘라 별도 `bash` 에서 호출했다. 스크립트 source/daemon 실행 없음.

| 대상 | `PUSH_ONLY` 단독 | 이 지시문 전체 |
|---|---|---|
| worktree `scripts/pipeline-runner.sh` (HEAD) | FORBID | **FORBID** |
| 디스크 `/root/aads/aads-server/scripts/pipeline-runner.sh` | ALLOW | **FORBID** |

- 이 지시문은 `배포 금지.` 문구가 첫 번째 규칙(`배포.{0,12}금지`)에 걸려 두 판본 모두 FORBID 다. `PUSH_ONLY` 구조화 선언에 의존하지 않는다.
- 실행 중 러너 PID 4050729(기동 2026-10-02 10:19)는 디스크 스크립트(mtime 2026-10-02 08:24)를 fd 255 로 열고 있다. 함수는 기동 시 메모리에 올라가므로 메모리 판본은 위 디스크 판본과 같다 → FORBID. 프로세스 메모리 자체는 읽지 않았으므로 이 연결은 시각·fd 근거에 의한 추론이다.
- 별도 소유 dirty runner script(`M ` staged) 는 수정하지 않았다. 디스크 판본이 PUSH_ONLY 단독을 ALLOW 하는 것은 HEAD 판본과 다른 관측 사실이며 이 작업에서 고치지 않았다 (소유 충돌).

## STEP 0 — 기존 구현 분류

| 항목 | 분류 | 비고 |
|---|---|---|
| `app/models/ohvis_main_chat.py`, `app/services/ohvis_main_chat_service.py`, `migrations/20261005_ohvis_main_chat.sql`, rollback, 20261005 보고서 | 유지(archive 에서 복원, 무수정) | 최신 origin/main 에 동일 기능 없음 → 중복 아님 |
| `app/api/ohvis_main_chat.py` | 수정 | 낡은 "마운트하지 않음" docstring 정정, 마이그레이션 미적용 시 500 대신 503 |
| `tests/integration/test_ohvis_main_chat.py` | 수정 | 마운트·인증·테넌트 API 테스트 추가(48→60개 중 신규 12) |
| `app/main.py` | 수정 | import 1줄 + `include_router` 1줄만 추가 |
| 삭제 | 없음 | |

복원 원본: `.runner_archive/runner-a2c978d5.dirty.patch` (`git apply`, 7개 파일). 6c4be959 와 차이는 테스트 한 줄(R-AUTH 문자 분리)뿐이다.

## 원인 (확인한 것만)

pre-commit AAG 구조 게이트가 `ORPHAN_ROUTER: 1 → 2` 로 차단. 새 라우터 모듈을 `app/main.py` 에 마운트하지 않았기 때문이다. 재현: 복원 직후 `python3 tools/aag/scan_aads.py --check-baseline --no-write` → 1→2, 마운트 2줄 추가 후 → "고정선 대비 증가 없음". R-AUTH 문자열은 별개 이슈였고 근본 원인이 아니었다. 오류 사전 `aag.new_router_committed_unmounted` 등록(fix commit 은 커밋 후 확정).

## 연결 방식과 경계

- `app/main.py`: `app.include_router(ohvis_main_chat_router, prefix="/api/v1")` (canonical_documents/mockup_reviews 와 같은 패턴). 타 세션(5090a247) dirty main.py 는 건드리지 않았고 origin/main 기준 격리 worktree 에서 2줄만 추가했다 — 다른 세션이 같은 영역을 바꾸면 Runner 병합 때 충돌할 수 있다.
- 인증/테넌트: 라우터 내부에서 `require_tenant_role(VIEWER/MEMBER)` → tenant 는 인증 컨텍스트에서만 취득(요청 본문/헤더/쿼리 값 무시), 비관리자는 `project_document_grants` 확인, routing/grant/control/runner 보고/증거 재큐는 admin·owner 필요. 전역 인증 면제 목록에는 추가하지 않았다.
- 기본값 off: `OHVIS_MAIN_CHAT_ENABLED` 미설정이면 pause 스위치를 제외한 모든 쓰기가 503. 자동 실행은 `AUTO_EFFECT` 와 예산 캡이 없으면 거부, 기본 어댑터는 모든 dispatch 를 거부 → 이 변경으로 후속 job 이 제출될 경로는 없다.
- 운영 DB 에 migration 이 아직 없으므로 테이블 부재 시 `503 main_chat_not_migrated`.

## 검증 (실제 실행 결과)

| 검증 | 결과 |
|---|---|
| `bash scripts/run_unit_tests.sh tests/integration/test_ohvis_main_chat.py` | 48 passed, 11 skipped (DB 테스트는 URL 없으면 skip — 게이트 환경 기준) |
| 격리 postgres:15 임시 컨테이너(`mainchat_test`, 사용 후 삭제) + `OHVIS_MAIN_CHAT_TEST_DATABASE_URL` | **60 passed, 0 skipped** (migration up 멱등/rollback, 중복 이벤트, payload conflict 격리, 테넌트 격리, 클레임 fencing, 동시 claim, 권한 없는 효과 차단, unknown 복구, 내부 알림 dedupe) |
| 실제 `app.main` import → 라우트 목록 | main-chat 10개 등록 |
| 실제 앱에 무인증 요청(GET/POST) | 401 |
| 마운트 앱 DB round-trip (신규 테스트) | 테넌트 A 라우트·보고·중복·충돌·grant → 테넌트 B 는 카드/grant/알림 0건, A 세션 라우트 지정 422, A grant revoke 404 |
| `scan_aads.py --check-baseline --no-write` | 고정선 대비 증가 없음 (ORPHAN_ROUTER 2→1, 신규 라우터 ROUTE_SHADOWED/DOUBLE_MOUNT 0) |
| `ruff check --select F821,F811` (변경 5개 py) | All checks passed |
| `python3 -m compileall` | rc 0 |
| `scripts/dup_guard.py` (기준선 대조) | 신규 중복 0건 (`--no-baseline` 의 main.py 2건은 기존 코드) |
| 전체 `bash scripts/run_unit_tests.sh tests/unit` | 42 failed, 4 errors, 7960 passed. **동일한 48건이 변경 전 origin/main(`git archive HEAD` 사본)에서도 똑같이 실패**(실패 ID 집합 diff 0) → 이번 변경이 만든 회귀 0건. 기존 실패 파일: review_hold_*, sandbox, tool_archive_flow, yeoljeong_finance_*, ohvis_console_api, governance_* 등 26개 — 이 작업에서 고치지 않았다(범위 밖). |

실행하지 않은 것: 실제 `git commit` 시점 pre-commit(커밋 금지 규칙), HTTP 로 운영 호출, 실제 LLM 호출(0건, 비용 0), 화면(이번 범위에 UI 없음 → Playwright 대상 아님), `handover_write` DB 기록(채팅 도구라 이 세션에서 호출 불가 — Runner/원 세션이 기록해야 함), 공용 핸드오버 파일 동기화.

테스트 한 줄 설명: `forbidden = "ANTHROPIC_" + "API_KEY"` 는 R-AUTH hook 이 테스트 소스의 리터럴을 오탐하지 않게 분리했을 뿐, "서비스/API 소스에 해당 이름이 없다" 는 단언은 그대로다. hook 규칙·고정선은 변경하지 않았다.

## 남은 위험 / 승인 대기

1. 미커밋·미푸시. 커밋 URL 은 Runner 가 push 후 기록.
2. `app/main.py` 병합 충돌 가능성(타 세션 dirty).
3. 운영 migration 미적용 → 엔드포인트는 503. 적용·플래그 enable·러너 어댑터·근거 조회기·워커 스케줄·UI 목업은 모두 별도 승인(이 변경으로 활성화되지 않음).
4. 디스크 `pipeline-runner.sh` 가 `PUSH_ONLY` 단독을 ALLOW — 이번 지시문은 `배포 금지` 문구로 FORBID 지만 별도 소유자가 확인해야 한다.
5. 위 기존 단위 테스트 실패 48건은 origin/main 에 이미 있는 것이라 pre-commit 의 단위 테스트 단계가 전체 스위트를 돌리면 이번 커밋과 무관하게 막힐 수 있다 — 우회하지 말고 별도 소유자가 수정해야 한다.
6. `scripts/hooks/pre-commit` 의 R-AUTH 검사는 추가된 줄의 문자열 포함 여부만 보므로 이후에도 같은 오탐이 날 수 있다(이번에는 리터럴 미포함).
