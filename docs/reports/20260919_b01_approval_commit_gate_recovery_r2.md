# B-01 Approval Commit Gate 복구 사후보고서

- 작업 ID: `AADS-GOAL-V12-B01-COMMIT-GATE-RECOVERY-R2-20260919`
- 조사 시각: 2026-09-19 KST
- 범위: `app/main.py`, `app/routers/work_items.py`, 승인 커밋 게이트
- 상태: 현재 `origin/main`에서 결함 없음; 중복 코드 변경 없음

## 요약

`runner-e56868f1`의 실패 보고는 새 `work_items` router가 `app/main.py`에
mount되지 않아 AAG `ORPHAN_ROUTER`가 증가한 경우를 가리킨다. 현 격리
worktree는 최신 검증 기준인 `origin/main`
`36d3d2421b148fdc5e8428994c79d1ebb26299c5`에서 분리했으며, 이 상태에는
해당 결함이 재현되지 않는다.

결함을 supersede한 커밋은 `629cb20966bf4d527ce43bd9b1dfbe439f68bb65`
(`feat(goals): add scoped workflow approval engine`)이다. 이 커밋의
`app/main.py` diff에는 다음 두 변경이 함께 들어 있다.

1. `from app.routers.work_items import router as work_items_router`
2. `app.include_router(work_items_router, prefix="/api/v1",
   tags=["goal-workflow-approval"])`

따라서 `/api/v1/work-items`, `/api/v1/work-item-change-sets`,
`/api/v1/auto-approval-grants`, `/api/v1/goals/*/auto-approval-grants`,
`/api/v1/goal-policy` 표면은 실제로 mount된다. 두 줄은 현 `app/main.py`의
85행 및 3852행에 유지된다.

## 결함 재현 및 현재 상태

실패 조건은 router 파일만 추가하고 위 `include_router` 호출을 누락하는
것이다. 현재 main과 router 도입 커밋 `629cb209`의 diff를 대조한 결과,
router 파일 생성과 import/mount가 같은 커밋에 원자적으로 포함되어 있어
현 소스 및 보존된 Git 이력에서는 이 실패 조건을 재현할 수 없었다.

`origin/main`과 격리 worktree `HEAD`가 같은 SHA임을 확인했으므로, 이미
해결된 mount를 다시 추가하거나 router를 복제하지 않았다. 이는 AAG 중복
라우터/중복 엔드포인트 위험을 피한다.

## STEP 0 기존 구현 분류

| 항목 | 분류 | 근거 |
|---|---|---|
| `app/main.py`의 `work_items_router` import | 유지 | `629cb209`부터 존재하며 실제 router 객체를 참조한다. |
| `app/main.py`의 `/api/v1` router mount | 유지 | `629cb209`부터 존재하며 ORPHAN 조건을 해소한다. |
| `app/routers/work_items.py`의 13개 API endpoint | 유지 | 모든 endpoint가 이 단일 `APIRouter`에 등록되어 있다. |
| 요청/실행/검토/권한 grant 모델과 helper | 유지 | endpoint의 기존 인증, transaction, 서비스 위임 경로다. |
| DB 접점 `goal_auto_approval_grants`, `goal_auto_approval_uses` | 유지 | grant 목록·사용량 조회에만 사용된다. |
| `tests/unit/test_work_items.py` | 신규 아님 | 현 저장소에 존재하지 않는다. superseding 상태라 중복 테스트 파일을 만들지 않았다. |
| B-01 사후보고서 | 신규 | 결함 조건, superseding SHA, 현재 상태 및 검증 한계를 기록한다. |
| 삭제 | 없음 | 호출처 영향 및 롤백 대상 없음. |

스케줄러는 이 두 모듈에 없다. router는 `app/main.py`가 유일하게
`include_router`하며, 별도/이중 mount는 확인되지 않았다.

## 검증 기록

아래 검증은 이 사후보고서를 포함한 격리 worktree에서 실제 수행했다.
`app/main.py` 또는 router의 코드 변경은 없으며, 문서만 선별 커밋했다.

| 검증 | 결과 |
|---|---|
| 검증 기준 SHA | 확인: `origin/main` `36d3d2421b148fdc5e8428994c79d1ebb26299c5` |
| router 도입 diff의 import + mount 대조 | 확인: `629cb20966bf4d527ce43bd9b1dfbe439f68bb65` |
| AAG baseline 검사 | 통과: 노드 821, 엣지 1,264, mount route 983, `ORPHAN_ROUTER 1`, 전체 결함 2, 고정선 대비 증가 없음. 이 router로 인한 증가는 0건이다. |
| `py_compile` | 통과: `.venv/bin/python -m py_compile app/main.py app/routers/work_items.py` |
| 관련 pytest | 통과: 36건, 실패 0건 (`test_goal_work_hierarchy_*`, `test_goal_workflow_approval.py`) |
| `git diff --check` | 통과 |
| 실제 pre-commit/commit hook | 문서 선별 커밋에서 실행·통과 |
| changed-files 선별 commit | 보고서 1개만 커밋; 애플리케이션 코드 변경 0건 |

이 결과는 원 실패 커밋을 그대로 재사용한 것이 아니라, 결함을 supersede한
`629cb209`의 router mount 상태를 최신 main에서 재검증한 결과다.

## 롤백

코드 변경이 없으므로 애플리케이션 롤백은 불필요하다. 보고서만 되돌릴 경우
이 파일을 제거하면 되며, `629cb209`의 import/mount는 변경하지 않는다.
