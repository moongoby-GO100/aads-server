# AAG V11-5 consumer inventory — phase 1

- 기준 SHA: `0fdcb4fa616a1c17ef4f94afbfc37cc553b1b686`
- 조사 시각: 2026-09-19 22:50 KST
- 판정: v1 폐기 불가. 이중 운영과 계측을 먼저 배포해야 한다.

| 소비자 | 현재 경로 | phase 1 조치 | 잔여 조치 |
|---|---|---|---|
| 세션 `aag_findings` | `app/services/aag_tools.py` legacy DB/local graph | `AAG_V2_CONSUMERS_ENABLED` flag로 central v2 전환, off 즉시 v1 rollback | 운영 shadow 관측 후 flag on 승인 |
| 세션 `aag_brief` | local `tools/aag/brief.py` | 같은 flag로 pinned central v2 brief 전환 | 운영 golden 비교 후 flag on 승인 |
| Pipeline Runner | `scripts/pipeline-runner.sh`가 local `brief.py` 직접 실행 | 변경 없음; v1 consumer로 분류 | project-scoped credential과 outage/skip 계약 확정 후 v2 endpoint 전환 |
| CI | 중앙 AAG read API 직접 호출 없음 | 잔여 v1 consumer 아님 | v2 gate가 도입될 때 pinned snapshot/commit 계약 사용 |
| 목표 화면 | AAG read API 직접 호출 없음 | 잔여 v1 consumer 아님 | V11-7 4축 상태 화면에서 v2 aggregate 사용 |
| 외부 HTTP | `/api/v1/aag/findings`, `/projects` 호출자는 코드 검색만으로 확정 불가 | consumer header와 DB telemetry 추가 | 관측 기간 동안 이름 없는 소비자를 식별 |

## Rollback

1. `AAG_V2_CONSUMERS_ENABLED=0`으로 세션 도구를 기존 v1 경로로 즉시 되돌린다.
2. `AAG_V2_ENABLED=0`으로 v2 HTTP 접근을 닫는다.
3. v1 route와 `aag_graph_snapshots`는 삭제·변경하지 않는다.
4. telemetry와 v2 additive table은 감사·재처리 근거로 보존한다.

## 완료 게이트

- v2 endpoint의 snapshot-pinned pagination 회귀 통과
- v1/v2 consumer telemetry migration 운영 적용
- 세션 shadow 비교 통과 후 v2 flag on
- Pipeline Runner v2 전환 및 승인된 outage/skip 계약
- 관측 기간의 잔여 v1 request 0건
- v1 rollback 리허설 통과 후에만 폐기 승인
