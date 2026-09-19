# AAG V11-5 consumer inventory — phase 2 cutover

- 기준 SHA: `0fdcb4fa616a1c17ef4f94afbfc37cc553b1b686`
- 조사 시각: 2026-09-19 22:50 KST
- 판정: v1 폐기 불가. 이중 운영과 계측을 먼저 배포해야 한다.

| 소비자 | 현재 경로 | phase 1 조치 | 잔여 조치 |
|---|---|---|---|
| 세션 `aag_findings` | `app/services/aag_tools.py` legacy DB/local graph | 운영 기본값을 central v2로 전환, flag off 즉시 v1 rollback | telemetry로 v1 잔존 관측 |
| 세션 `aag_brief` | local `tools/aag/brief.py` | 운영 기본값을 pinned central v2로 전환, flag off 즉시 v1 rollback | telemetry로 v1 잔존 관측 |
| Pipeline Runner | `scripts/pipeline-runner.sh`가 local `brief.py` 직접 실행 | project-scoped credential 전용 `/aag/v2/runner-brief`와 명시적 skip 계약 구현 | 서버별 credential 발급 후 `AAG_V2_RUNNER_ENABLED=1` 전환 |
| CI | 중앙 AAG read API 직접 호출 없음 | 잔여 v1 consumer 아님 | v2 gate가 도입될 때 pinned snapshot/commit 계약 사용 |
| 목표 화면 | AAG read API 직접 호출 없음 | 잔여 v1 consumer 아님 | V11-7 4축 상태 화면에서 v2 aggregate 사용 |
| 외부 HTTP | `/api/v1/aag/findings`, `/projects` 호출자는 코드 검색만으로 확정 불가 | consumer header와 DB telemetry 추가 | 관측 기간 동안 이름 없는 소비자를 식별 |

V11-2 초기 배포 뒤 authoritative observation 3건이 존재했지만 latest pointer가 0건인
운영 공백이 확인됐다. V11-5 migration은 최신 verified authoritative observation만
pointer/ref-head로 백필하고, hourly pusher는 advisory transaction lock 안에서 observation과
pointer를 함께 기록한다. commit mismatch와 out-of-order 실행은 pointer를 갱신하지 않는다.

## Rollback

1. `AAG_V2_CONSUMERS_ENABLED=0`으로 세션 도구를 기존 v1 경로로 즉시 되돌린다.
2. `AAG_V2_ENABLED=0`으로 v2 HTTP 접근을 닫는다.
3. v1 route와 `aag_graph_snapshots`는 삭제·변경하지 않는다.
4. telemetry와 v2 additive table은 감사·재처리 근거로 보존한다.

Pipeline Runner는 `AAG_SCANNER_TOKEN_<PROJECT>`를 사용한다. v2가 켜진 상태에서
credential 또는 authoritative snapshot이 없으면 로컬 그래프로 조용히 강등하지 않고
브리프를 건너뛴다. 운영자가 `AAG_V2_RUNNER_ENABLED=0`으로 명시적으로 되돌린 경우에만
기존 repo/host `brief.py`를 사용한다. 이 구분은 stale local graph를 authoritative
evidence처럼 워커 프롬프트에 넣는 것을 방지한다.

## 완료 게이트

- v2 endpoint의 snapshot-pinned pagination 회귀 통과
- v1/v2 consumer telemetry migration 운영 적용
- 세션 v2 flag on 및 pinned central read 확인
- Pipeline Runner v2 경로·outage/skip 계약 구현, 서버별 credential 발급 및 실제 v2 telemetry 확인
- 관측 기간의 잔여 v1 request 0건
- v1 rollback 리허설 통과 후에만 폐기 승인
