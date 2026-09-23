# 오비서 이전 M5 PRD — 승인된 운영 전환·배포 인증

| 항목 | 내용 |
|---|---|
| 문서 ID | OBYS-MIGRATION-JINAH-M5 |
| 상위 PRD | `docs/prd/20260923_OBYS_FULL_MIGRATION_JINAH_PRD.md` (정본 v1.2.0, goal_documents id=214) |
| 목표/마일스톤 | goal `df479771-f250-4a11-90a3-220432da2bfa` / M5 `e1c61408-fde7-46aa-8e8f-add4da007f56` |
| 담당 역할 | CTO (owner_session_id 미배정) |
| 상태 | 초안 — 운영 전환 미실행 |

## 1. 목적과 범위
M4 통합 검수를 통과한 릴리스 SHA로 실제 운영 전환(DNS/트래픽)을 수행하고 무중단 배포 계약을 준수한다.

## 2. 완료 기준 (DB 정본)
release SHA 단일 build, `--no-build`, candidate/routed health, 같은 digest, 신규 P0/P1 5분 0건, 신규 쓰기 보존 롤백. 운영 전환은 이 문서 작성 시점 기준 미실행.

## 3. 작업 항목
1. M4 통합 검수를 통과한 SHA를 릴리스 후보로 고정
2. Blue/Green 절차로 candidate 빌드 → health → 전환 → 동일 digest 동기화
3. 전환 후 5분 모니터링으로 신규 P0/P1 0건 확인
4. 롤백 절차(신규 쓰기 보존)를 사전 리허설로 검증

## 4. 완료 판정 방법
전환 후 5분 모니터링 P0/P1 0건, 양 슬롯 동일 digest, 롤백 리허설 성공 기록.

## 5. 승인 게이트
이 마일스톤의 실제 운영 전환 실행은 CEO 명시 승인 없이 착수하지 않는다(R-DOCKER, 배포 승인 규칙). M1~M4 게이트 없이 전환을 시도하는 것은 규칙 위반이다.
