# RESULT — AADS-GOAL-GUARD-HARNESS-S-20260907 (검수 피드백 반영본)

- TASK_ID: `AADS-GOAL-GUARD-HARNESS-S-20260907`
- 관련 커밋: `5bc2245e` (1차) + 본 문서의 수정 커밋
- 작성 시각: 2026-09-07 13:50 KST
- 배포 상태: **미배포** (커밋만. 운영 슬롯은 이전 이미지 계속 서비스)

## 0. 검수 피드백 4건 대응 요약

| # | 검수 지적 | 판정 | 조치 |
|---|---|---|---|
| 1 | `ohvis_harness_traces` 추적 기록이 필요한 부분 누락 | **인정** | 진행 중 목표(시드/마이그레이션 생성분)와 러너 자기감사 경로에 trace 추가 |
| 2 | `app/services/chat_service.py` 범위 이탈 | **오탐(부분)** | 동일 워크트리 병행 러너의 변경. 본 작업 커밋에 미포함. 근거는 §3 |
| 3 | 기존 구현 삭제/대체 포함 | **인정** | `activate_goal` 재구조화를 원복(커넥션 1개·조기 return 구조 복원) |
| 4 | RESULT에 기존 구현 조사표 누락 | **인정** | 본 문서 §2가 조사표. HANDOVER에도 동일 표 반영 |

## 1. 지시 대비 이행 상태

| 지시 항목 | 상태 |
|---|---|
| Scope 1. `task_policy_compiler` (정책 dict/체크리스트) | 완료 (1차) |
| Scope 2. `ohvis_harness_trace` 비치명적 기록 헬퍼 | 완료 (1차) + 보유 커넥션 재사용 경로 추가 |
| Scope 3. `goal_manager` 저위험 지점 7종 배선 | 완료 (1차) + **진행 중 목표 정책 증거 추가(본 수정)** |
| Scope 4. 멱등 시드(활성 목표 4마일스톤 + draft 후속) | 완료 (1차, migration 160) |
| Scope 5. 단위 테스트 | 완료 (1차 3파일) + 본 수정 4건 추가 |
| Scope 6. HANDOVER 갱신 | 완료 (1차) + 본 수정 반영 |
| Purpose. **러너**가 기존 구현 준수를 자기감사하고 증거를 남김 | **본 수정에서 이행** (1차 미이행분) |

### 1차에서 빠졌던 추적 기록 (피드백 #1의 실체)

1. **진행 중 목표에 보존정책 증거 0건** — `_trace_policy()`가 `create_goal`에서만 호출되어,
   migration 160 시드로 만들어진 활성 목표처럼 `create_goal`을 거치지 않은 목표는
   자기감사 증거가 한 건도 남지 않았다. → `activate_goal` 성공 시점과 `advance_goal`의
   새 마일스톤 개시 시점에 `stage=activate|advance`로 기록하도록 보강.
2. **러너 측 증거 0건** — 지시서 Purpose가 "목표/**러너**가 … 증거를 남기게 한다"인데
   1차는 목표 경로만 배선했다. → 러너에 `runner_policy`(실행 전 정책 컴파일),
   `runner_preservation_audit`(검수 직후 diff 자기감사) 2종 trace 추가.

## 2. STEP 0 기존 구현 조사표

`target | 기존 구현 | 결정 | 사유` — **삭제 0건**.

### 2-1. 본 수정(검수 피드백 반영)

| target | 기존 구현 | 결정 | 사유 |
|---|---|---|---|
| `app/services/goal_manager.py` `activate_goal` | 커넥션 1개 안에서 status 확인 → 조기 return → UPDATE | **수정(원복)** | 1차가 `rejection` 변수 + acquire 2회로 재구조화해 검사/갱신 사이에 TOCTOU 창을 만들었다. 원래 제어 흐름을 되돌리고 trace만 끼워 넣음 |
| `app/services/goal_manager.py` `_trace` | 1차 신규 | 수정 | `conn` 인자 추가(기본값 `None`). 기존 호출부 시그니처 호환 |
| `app/services/goal_manager.py` `_trace_policy` | 1차 신규, `create_goal` 전용 | 수정 | `stage`/`conn` 인자 추가. 기존 create 경로 동작·문구 불변 |
| `app/services/goal_manager.py` `advance_goal` | 상태 전이·반환 스키마 | **유지** | SELECT에 `title` 컬럼만 추가. 분기·반환값 불변 |
| `app/services/goal_manager.py` 그 외 12개 메서드 | 기존 구현 | **유지** | 미변경 |
| `app/services/ohvis_harness_trace.py` `record_trace` | 1차 신규, 항상 pool acquire | 수정 | `conn` 인자 추가 + INSERT 본문을 내부 `_insert()`로 추출. 기존 pool 경로·반환 계약(True/False, 예외 없음) 동일 |
| `app/services/ohvis_harness_trace.py` `record_goal_trace` | 1차 신규 | 수정 | `conn` 위임만 추가 |
| `app/services/pipeline_runner_service.py` `PipelineCJob` | 기존 러너 전 메서드 | **유지** | 기존 메서드 0건 변경·0건 삭제 |
| `app/services/pipeline_runner_service.py` (신규 메서드 3종) | 없음 | **신규** | `_trace_runner` / `_trace_task_policy` / `_trace_preservation_audit`. 호출 2줄만 삽입 |
| `app/services/code_reviewer.py` `_precheck_preservation_gate` 및 헬퍼 | 보존 하드 게이트 | **유지** | 파일 미변경. `_diff_line_counts` / `_extract_changed_files` / `_extract_instruction_paths`를 **재사용만** 함(판정 기준 중복 구현 금지) |
| `app/routers/goals.py` | 엔드포인트 12종 | **유지** | 파일 미변경. trace는 서비스 계층에서 기록 |
| `app/services/task_policy_compiler.py` | 1차 신규 | **유지** | 파일 미변경 |
| `migrations/160_...sql` | 1차 신규 | **유지** | 파일 미변경. 재적용 없음 |
| `tests/unit/test_goal_control_loop_static.py` | 기존 6 테스트 | 수정 | 기존 테스트 유지, 3건 추가 |
| `tests/unit/test_ohvis_harness_trace.py` | 기존 6 테스트 | 수정 | 기존 테스트 유지, 2건 추가 |

### 2-2. 1차 커밋(`5bc2245e`) 조사표 — 검수 시 누락되었던 표

| target | 기존 구현 | 결정 | 사유 |
|---|---|---|---|
| `GoalStateMachine` 상태 전이·완료 판정 | 기존 구현 | 유지 | trace 호출만 추가, SQL·반환 스키마 불변 |
| `app/routers/goals.py` 엔드포인트 | 기존 구현 | 유지 | 미변경 |
| `pipeline_runner_service._VERIFICATION_CHECKLIST_TEMPLATE` | STEP 0 문구 | 유지 | `task_policy_compiler`가 문구를 그대로 재사용, 드리프트는 단위 테스트가 차단 |
| `code_reviewer._precheck_preservation_gate` | 보존 임계값 | 유지 | `no_broad_replacement` 규칙으로 미러링만 |
| `ohvis_harness.RISK_POLICIES` / `_table_exists` | 기존 구현 | 유지 | 신규 모듈에서 import 재사용 |
| `migrations/153,156,157,158` | 기존 스키마 | 유지 | 미변경 |
| `task_policy_compiler.py`, `ohvis_harness_trace.py`, 테스트 2종, `migrations/160` | 없음 | 신규 | — |

**삭제(delete) 항목: 0건.** 함수·클래스·엔드포인트·마이그레이션 삭제 없음.

## 3. 지시서 범위 밖 변경 — 사유 명시

STEP 0 체크리스트 "지시서에 명시되지 않은 파일을 변경해야 하면 변경 전 사유를 RESULT에 명시" 이행.

| 파일 | 지시서 명시 | 판단 | 사유 |
|---|---|---|---|
| `app/services/pipeline_runner_service.py` | 경로 미명시 (STEP 0 체크리스트를 "reuse"로만 언급) | **의도적 범위 확장** | 지시서 Purpose가 "진행 중인 목표/**러너**가 … 증거를 `ohvis_harness_traces`에 남기게 한다"이고, 러너 측 기록은 이 파일 외에 넣을 곳이 없다. 기존 메서드 0건 수정·0건 삭제, 신규 메서드 3종과 호출 2줄만 추가(+110/-0) |
| `app/services/chat_service.py` | 미명시 | **본 작업 변경분 아님** | 아래 근거 참조. 본 수정 커밋에 미포함 |

### `chat_service.py` 오탐 근거

- 같은 TASK_ID로 러너가 3개 기동됐다: `runner-66037bf2`(error), `runner-c7d0a8e1`(cancelled,
  Runner Guard가 `goal_manager.py` 동일 파일 충돌로 차단), `runner-808c968a`(running).
  `runner-c7d0a8e1`의 차단 사유 자체가 "동일 파일 충돌 감지"로 기록돼 있다.
- 검수는 `git diff HEAD`(워크트리 전체)를 읽으므로, 같은 워크트리에서 **다른 작업의 러너**가
  건드린 파일이 본 작업 diff에 섞여 보인다.
- `chat_service.py` 변경은 별도 커밋 `c6794939 fix(chat): preserve deferred reaction retry budget`으로
  자체 테스트(`tests/unit/test_execution_lease_contract.py`)와 함께 이미 커밋됐다. 커밋 타임존도
  본 작업(`+0200`)과 다른 `+0900`으로, 서로 다른 러너 프로세스 산출물이다.
- 따라서 **되돌리지 않는다.** 정상 동작하는 별도 수정을 범위 지적만으로 삭제하면
  "기존 구현 삭제 금지"를 오히려 위반한다. 본 작업 커밋에서 제외하고 여기 사유를 남기는 것으로 처리한다.
- 본 수정 시점에도 동일 워크트리에 병행 작업분(`tool_executor.py`, `tool_registry.py`,
  `test_deploy_safe.py` — 배포 async_mode 작업)이 dirty 상태다. 건드리지 않고 커밋에서 제외했다.

## 4. 검증

| 항목 | 결과 |
|---|---|
| `python3 -m py_compile` (goal_manager, ohvis_harness_trace, pipeline_runner_service) | 통과 |
| `git diff --check` | 통과 |
| pytest (`test_ohvis_harness_trace`, `test_goal_control_loop_static`, `test_task_policy_compiler`, `test_ohvis_harness`, `test_tools_and_pipeline`) | **92 passed** |
| 진행 중 목표 정책 증거 (실 DB) | 활성 목표 `채팅 시스템 안정화 및 응답 가독성 개선`에 `goal_policy / stage=advance risk_tier=write checks=25` 기록 확인 |
| 보유 커넥션 재사용 경로 (실 DB) | `goal_activate / rejected` 기록 확인 (중첩 acquire 없음) |
| 러너 자기감사 trace (실 DB) | `runner_policy / checks=24`, `runner_preservation_audit / verdict=FAIL +2/-1 files=2 out_of_scope=1` 기록 확인 → `out_of_scope_files=['app/services/chat_service.py']` 정확히 탐지. **검증 행은 삭제 완료** |
| DB 카운트 | `ohvis_harness_traces` 6 → 8 (검증 기록). goals 8 / milestones 22 / goal_task_links 20 — **변화 없음** |
| 목표 상태 전이 | 변경 0건. 검증은 trace 기록 경로만 호출 |

## 5. 남은 격차

- **미배포.** `deploy.sh bluegreen` 전까지 운영 런타임 경로에서는 새 trace가 기록되지 않는다.
- `compile_task_policy`는 러너 **증거 기록**에는 연결됐지만 러너 **프롬프트 주입**에는 아직 미연결이다.
  프롬프트 문구 변경은 모든 러너 동작에 영향을 주므로 별도 지시서·별도 검수가 필요하다.
- `add_milestone` / `link_task` / `update_task_status` trace는 `project=None`으로 남는다
  (해당 경로가 project를 조회하지 않음). 조회 비용을 감수할 때만 추가할 것.
- 동일 TASK_ID 러너 중복 기동은 미해결이다. Runner Guard가 2번째는 차단했지만 3번째는 통과했다.
  별도 조사 대상.
