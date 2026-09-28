# 오비서 이전 M6 PRD — 안정화·복원·운영 인계

| 항목 | 내용 |
|---|---|
| 문서 ID | OBYS-MIGRATION-JINAH-M6 |
| 상위 PRD | `docs/prd/20260923_OBYS_FULL_MIGRATION_JINAH_PRD.md` (정본 v1.2.0, goal_documents id=214) |
| 목표/마일스톤 | goal `df479771-f250-4a11-90a3-220432da2bfa` / M6 `d3d873e6-abde-4ac9-b1cf-0f5764fcc82f` |
| 담당 역할 | CTO (owner_session_id 미배정) |
| 상태 | 초안 |

## 1. 목적과 범위
운영 전환 후 관찰 기간을 운영하고, 백업/복원을 검증하며, 운영 절차를 정본 시스템에 인계한다.

## 2. 완료 기준 (DB 정본)
관찰 기간 증거, 백업 복원 검증, 운영 절차 및 `project_handover_entries` 기록. 전체 PRD 기능 완료와 이전 인증 완료를 구분한다.

## 3. 작업 항목
1. 전환 후 관찰 기간(권장 72시간) 오류·성능 로그 수집
2. 백업 → 복원 리허설을 격리 환경에서 재현
3. 운영 절차서 작성 및 `project_handover_entries`(R-HANDOVER-DB)에 정본 기록

## 4. 완료 판정 방법
관찰 기간 신규 P0 0건, 복원 리허설 성공, handover 항목 기록 완료(entry_key 확인 가능).

## 5. 리스크
파일(HANDOVER.md)에만 기록하고 DB에 기록하지 않으면 R-HANDOVER-DB 기준 미완료로 처리된다.
