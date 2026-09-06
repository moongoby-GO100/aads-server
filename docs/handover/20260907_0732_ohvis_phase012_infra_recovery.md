# 2026-09-07 07:32 KST - OHVIS Phase 0/1/2 인프라 복구·품질게이트·목표원장 main 반영 (CEO 세션 8bf0405a)

## CEO request
- "구현해" — Phase 0(러너 리뷰 인프라 복구) → Phase 1(L3 동적 역할·CEO 통합 컨텍스트·품질게이트) → Phase 2(목표 원장) 순차 구현.

## Findings
- `write_remote_file`/`patch_remote_file`은 호스트 repo가 아니라 **활성 컨테이너(/app)** 에 쓴다. 컨테이너 /app은 bind mount가 아니므로 blue/green 전환 시 직접 수정분이 소실된다. 호스트 반영은 `docker cp <container>:/app/... /root/aads/aads-server/...` 후 git 커밋으로만 가능.
- 러너 `runner-1c3e6b81`(품질게이트) 승인 후 `deploy_preflight_git_state`(main behind=1/ahead=1)로 실패 → 산출물 커밋 e13d2fbb를 main에 cherry-pick(a911ee04)으로 수동 반영.
- 다른 세션 커밋 3a1fec84가 `auto_rag._search_chat_messages` 시그니처를 3인자로 바꿔 `tests/unit/test_memory_context_regression.py` 1건이 깨져 있었음 → 테스트 수정(2aaab78a).
- `tests/test_tool_awareness.py` 4건 실패는 운영 green 컨테이너(48b2fdab)에서도 동일 실패하는 기존 문제(범위 외, 미수정).
- 세션 9102c970(AADS PM 워크스페이스)이 동일 Phase 계획을 병행 구현 중 — 파일 충돌 방지를 위해 `chat_workspace_change_ledger` 확인 후 작업 필요.

## Changes (origin/main 6bcfe216)
- `app/services/pipeline_runner_service.py`: AI 검수 DELEGATED 3회 소진 시 `review_hold` 영구 대기 대신 `awaiting_approval`로 CEO 에스컬레이션. 검수 PASS 후 `quality_gate.evaluate()` — F등급 FAIL, C등급 경고.
- `app/services/quality_gate.py`(신규): LLM 없이 diff/구문/지시 키워드/출력 길이 기반 A/B/C/F 정적 품질 점수.
- `app/services/agent_orchestrator.py`(702eed05, runner-f4e4b570): `_load_role_prompts()` — `prompt_assets` layer 3 역할 프롬프트 DB 동적 로드(5분 캐시), 하드코딩 6역할은 fallback.
- `app/core/memory_recall.py`, `app/services/auto_rag.py`(3a1fec84): CEO/통합 워크스페이스는 7개 프로젝트 메모리 프로젝트별 5건·총 35건 교차 주입, 일반 세션 격리 유지.
- `app/services/goal_planner.py`(신규), `migrations/152_server_registry.sql`, `migrations/153_goal_management.sql`: goals/milestones/goal_task_links 목표 원장 + GoalPlanner(목표 생성→마일스톤→작업 연결→완료 판정→자동 전진). DB 테이블 생성 완료(goals 0건, server_registry 3건).
- `app/core/server_registry.py`, `app/services/session_sync.py`, `app/services/temporal_controller.py`: 세션 9102c970 작성분이 동일 커밋(2aaab78a)에 포함됨. 기존 SSOT `app/services/server_registry.py`(AADS-181)와 역할 중복 → 통합 검토 필요. `session_sync`/`temporal_controller`는 아직 호출부 미연결.
- 중복 마이그레이션 155/156 제거(6bcfe216).

## Verification
- `docker exec aads-server pytest tests/unit/test_tools_and_pipeline.py`: 64 passed.
- `docker exec aads-server pytest tests/unit/test_memory_context_regression.py`: 5 passed.
- `docker exec aads-server pytest tests/unit/test_model_registry.py`: 13 passed.
- `git push origin main` 성공(pre-push HOOK_VERIFIED).

## Deployment
- `_run_bg_release.sh`(bluegreen) 실행 시 "배포 이미 진행 중(PID 4173589)"으로 차단 — 타 세션 배포 완료 후 재실행 필요. 이 노트 시점 6bcfe216 운영 반영 **미완료**.

## Open items
- Phase 2-3/2-4(목표↔러너 자동 연결, 목표 현황 API), Phase 3-1 호출부 연결(session_sync → pipeline_runner_service 완료 훅), `app/core/server_registry.py` 중복 정리.
- 중복 러너 반려: runner-e64f378d, runner-80b7ed24, runner-b164e0be(이미 main 반영된 Phase 1-1/1-2).
