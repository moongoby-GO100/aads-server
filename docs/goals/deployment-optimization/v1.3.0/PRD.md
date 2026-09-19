# AADS Blue/Green 병목 최적화 PRD

```yaml
document: prd
version: 1.3.0
status: implementation-candidate
goal_id: 1997c471-ba2e-4f0d-a1e3-9df0dd6d1240
milestone_id: b00da83d-ec47-460a-8ce0-10c814fbe3e4
updated_at: 2026-09-19 23:48 KST
```

## 요구사항

| ID | 요구사항 | 수용 기준 |
|---|---|---|
| FR-04H | target drain/build 겹침 | drain deadline이 build 전에 시작되고 busy target은 재생성하지 않음 |
| FR-04I | 무효 직렬 대기 제거 | pre-cutover 기본 대기 0초, lease 표본은 phase metadata에 저장 |
| FR-04J | standby/monitor 겹침 | busy standby는 즉시 보류되고 retry worker가 flock 해제 후 재시도 |
| FR-04K | 인증 fail-closed | same digest 전 success/provenance 금지 |
| FR-04L | 안전 전환 | candidate/routed health, short lock, rollback, DB lease, 300초 감시 유지 |

## 사용자 흐름

- 정상 release: build 중 target이 drain되고, candidate health 후 수초 내 cutover한다.
- live stream 존재: nginx 기존 worker가 응답을 유지하고 old slot은 교체하지 않는다.
- standby 보류: 화면에는 `success_partial`과 stream 수가 보이며 retry가 자동 실행된다.
- 인증 확인: 양 슬롯 digest와 5분 감시가 모두 통과해야 completed success로 보인다.

## 비목표

active API 직접 restart, full compose deploy, busy slot 강제 종료, 감시 단축,
unknown lease 강제 정리는 포함하지 않는다.
