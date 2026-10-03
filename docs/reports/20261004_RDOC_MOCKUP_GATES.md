# AADS-RDOC-MOCKUP-GATES-20261004 — 목업 제출 의무·승인 bundle 서버 실행 게이트·오비스 내부 알림

- 작업: P1 / SIZE M / 구현 (runner-48e5aafb 취소분 승계)
- 상태: 코드·테스트 작성 완료, **커밋/푸시/빌드/배포 미실행** (Runner가 CEO 승인 후 수행)
- 대시보드(B) 연동: **차단됨 (BLOCKED)** — 아래 "선행조건" 참조

## 0. 선행조건 SHA 검증

| 항목 | 기대 | 실측 | 판정 |
|---|---|---|---|
| A | 1d19322f (origin/main) | `git merge-base --is-ancestor`로 origin/main의 조상임을 확인 | 존재 |
| B | runner-e266ec09 의 대시보드 커밋 | aads-server, `/root/aads/aads-dashboard` origin/main 어디에도 e266ec09 관련 커밋 없음 (대시보드 최근 로그는 무관한 runner 커밋뿐) | **미존재 → UI 연동 차단** |

"완료" 문자열은 신뢰하지 않았고 SHA로만 판단했다. B가 없으므로 **대시보드(UI) 연동 부분은 이번 작업에서 하지 않았다.** 서버 게이트와 알림 생산자(서버 측)만 구현했다. 알림 링크 형식(`/chat?mockup_review=..&mockup_project=..&mockup_revision=..#<sessionId>`)은 서버가 생성하지만, 이를 열어 정확한 버전을 표시하는 화면은 B 이후 별도 작업이다.

## 1. STEP 0 — 기존 항목 분류

| 항목 | 분류 | 설명 |
|---|---|---|
| `verify_bundle` (submit/pre_execution/checkpoint 단계, 바인딩, `_hold_bindings`) | 유지 | 로직 그대로. submit 단계에서 선언된 GOAL_ID를 범위로 인정하는 폴백만 추가(`declared_goal_id`) |
| `mockup_review_service.submit_review/request_changes/approve_review/revoke_review` | 수정 | 알림 생산 훅 추가 (실패는 삼키며 본 흐름에 영향 없음) |
| `mockup_review_service` 알림·게이트·대화 intake 함수 | 신규 | `notification_link`, `_eligible`, `_session_owner`, `_audience`, `_emit`, `_notify_submitted`, `list_notifications`, `mark_notification_read`, `_gate_result`, `gate_check`, `resolve_change_target`, `intake_change` |
| `app/models/mockup_review.py` | 수정 | `ChangeIntake` 추가 |
| `app/api/mockup_reviews.py` | 수정 | `GET /notifications`, `POST /notifications/{id}/read`, `POST /change-intake` (모두 `/{review_id}` 앞에 등록) |
| `app/api/pipeline_runner.py` `submit_job` | 수정 | submit 단계 게이트 호출, 거부 시 409 `mockup_approval_required` |
| `app/api/pipeline_runner.py` 게이트 엔드포인트 | 신규 | `POST /pipeline/jobs/{job_id}/mockup-gate` |
| `scripts/pipeline-runner.sh` (+ `.local` 미러) | 수정 | `pre_validate` 직후 워커 실행 직전 게이트. 기존 dirty 변경은 보존 |
| `scripts/verify_mockup_approval.py` | 신규 | 공유 CLI. 종료코드 0 허용·비해당 / 10 거부 / 20 게이트 불가 |
| `app/services/mockup_gate_rules.py` | 신규 | API와 CLI가 공유하는 stdlib 전용 규칙 모듈 |
| `ohvis_notifications` 테이블 | 신규 | 마이그레이션 + 롤백 (운영 적용 안 함) |
| `tests/unit/test_mockup_execution_gate.py` | 신규 | |
| `tests/integration/test_mockup_chat_flow.py` | 신규 | DB 필요 — 미실행 |
| `app/services/tool_executor.py`, `app/models/chat.py`, `app/services/chat_service.py` | 유지 (미변경) | TARGET_FILES에 있었으나 변경이 필요 없었다. 채팅 → 리뷰 매핑은 새 컬럼 없이 `chat_artifacts.metadata.mockup_review_id` 관례를 사용 |
| 삭제 | 없음 | |

### TARGET_FILES 밖 파일 변경 사유
- `app/services/mockup_gate_rules.py`: API와 워커 CLI가 동일 규칙을 쓰도록 분리 (규칙 두 벌 방지)
- `app/api/mockup_reviews.py`, `app/models/mockup_review.py`: 알림 조회·읽음·대화 intake 라우트와 요청 모델
- `migrations/20261004_ohvis_notifications.sql`, `migrations/rollback/20261004_ohvis_notifications.down.sql`: 알림 저장소
- `scripts/pipeline-runner.sh.local`: `pipeline-runner.sh`와 바이트 동일해야 하는 미러 (단위 테스트 약 17건이 강제)

## 2. 구현 요약

### (1) 실행 게이트
- 제출 시(`submit_job`)와 워커 시작 직전(`run_job`) 두 곳에서 같은 승인 bundle을 `verify_bundle`로 검증한다.
- UI 작업은 권한·generation·hash·revoked·revising·DB 불가 시 모두 거부(fail-closed). 무관한 백엔드 작업은 DB 접근 없이 `applicable=false`로 통과.
- 게이트는 자체 트랜잭션에서 실행되어 거부 감사 행이 커밋된다. 게이트는 어떤 경우에도 예외를 던지지 않고 거부(`gate_unavailable`)로 변환한다.
- 실행 중인 작업을 죽이거나 재시작하지 않는다 (`_hold_bindings`는 표시·보류만).
- 이번에는 pre_execution 한 번만 검증하며, 실행 중 checkpoint 반복 호출은 추가하지 않았다.

### (2) 채팅 흐름
- `intake_change`: reply_to_id / 선택 artifact → 서버 측 대상 검증 → change_request → 새 revision → 수정 문서 → 재승인.
- 대상 해석 순서: 명시 review_id → artifact(또는 답장 대상 메시지의 artifact)의 `metadata.mockup_review_id` → 세션의 단일 활성 리뷰. 모호하거나 충돌하는 힌트는 거부한다. base revision은 항상 서버의 최신 revision.
- 응답은 항상 `approved=false`, `implementation_command=false`. 자연어 LLM 판단만으로 실행을 승인하지 않는다. 승인은 기존 승인 API 경로로만 가능.
- 원본 메시지가 요청한 `session_id`에 속하는지 확인한다.

### (3) 오비스 내부 알림
- 종류 7개: review_requested, change_received, re_reported, approval_waiting, approved, rejected(revoked), failed.
- 수신자는 활성 테넌트 멤버 + 프로젝트 권한(또는 admin/owner)이며, 세션 연결 리뷰는 세션 소유자 또는 admin/owner. 행위자 본인은 제외.
- 중복 억제: `UNIQUE(tenant, recipient, dedupe_key)`. 게이트 거부는 `failed:{head}:{task}:{phase}:{정렬된 사유}`로 원인당 1회.
- 외부 채널(Telegram/이메일/SMS/Slack)과 외부 폴백은 없다 (소스 스캔 테스트로 확인).

## 3. 실행한 검증 (실측)

| 검증 | 결과 |
|---|---|
| `test_mockup_execution_gate.py` + `test_mockup_reviews.py` | 97 passed |
| pipeline_runner / 러너 스크립트 미러 관련 단위 테스트 | 479 passed, 3 skipped |
| `ruff` F821/F811/F401 (변경 파일) | 이상 없음 |
| `python3 -m compileall` | OK |
| `scripts/dup_guard.py` (변경 파일) | rc=0 |
| CLI 수동 실행 | rc 0 / 10 / 20 각각 확인 |
| 전체 `bash scripts/run_unit_tests.sh tests/unit` | FULL_UNIT_PLACEHOLDER |

### 실행하지 않은 것
- `tests/integration/test_mockup_chat_flow.py` 5건: `AADS_MOCKUP_TEST_DATABASE_URL`이 없어 수집은 되나 전부 skip. **DB에서 실행된 적 없다.** 단위 테스트의 FakeConn은 SQL 의도만 흉내 내므로 실제 SQL 검증은 아니다.
- 마이그레이션 적용: 하지 않음.
- 실제 `run_job` 경로의 종단 실행: 하지 않음.
- 승인 후 Runner 빌드 검증 대상: 위 변경 전체의 이미지 빌드·기동 검증.

## 4. 비용
이 세션은 API 호출 비용을 직접 계측하는 수단이 없다. 측정값 없음 (중간 보고 기준 $5 초과 여부도 확인 불가).

## 5. 결정 필요·주의 사항
1. **기본 게이트 모드는 `shadow`** (`MOCKUP_GATE_MODE`). 선언된 MOCKUP_* bundle은 항상 검증·강제되지만, bundle 없는 UI 작업은 기록만 하고 통과시킨다. "UI 작업 fail-closed"를 문자 그대로 적용하려면 `enforce`로 올려야 하며 CEO 결정이 필요하다. 잘못된 값은 `enforce`로 매핑된다.
2. submit 단계 바인딩은 미리 생성한 job_id를 쓰므로 dedup_blocked 시 미사용 바인딩이 남을 수 있다.
3. 세션 연결 리뷰의 알림은 세션 소유자와 admin/owner에게만 간다. approve 권한만 가진 승인자는 알림을 받지 못한다.
4. 행위자 본인은 알림에서 제외된다.
5. 채팅 메시지 → 리뷰 매핑은 `chat_artifacts.metadata.mockup_review_id` 관례에 의존한다 (영속 매핑이 없었음).
6. canonical `approved_revision_id`는 임의로 변경하지 않았다.

## 6. 남은 작업
- B(대시보드 커밋) 확보 후: 알림 링크가 정확한 revision을 여는 UI, 알림 목록·읽음 UI.
- DB가 있는 환경에서 통합 테스트 5건 실행, 마이그레이션 적용 (승인 후).
- 승인 후 Runner 빌드 검증.
- `shadow` → `enforce` 전환 결정.

## 7. 인수인계 기록
HANDOVER.md는 수정하지 않았다. DB `handover_write`(entry_key `rdoc-mockup-gates-20261004`)는 이 세션에 DB 접근이 없어 **수행하지 못했다.** Runner/후속 세션이 기록해야 한다.

## 교훈
- 규칙 두 벌(API/CLI)은 반드시 어긋난다. stdlib 전용 공유 모듈로 합쳤다.
- 선행 커밋 존재 여부는 문자열이 아니라 SHA로 확인해야 하며, 없으면 해당 부분만 차단하고 나머지를 진행한다.
