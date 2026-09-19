# AADS Blue/Green drain·standby 병렬화 계획

```yaml
document: plan
version: 1.3.0
status: implementation-candidate
goal_id: 1997c471-ba2e-4f0d-a1e3-9df0dd6d1240
milestone_id: b00da83d-ec47-460a-8ce0-10c814fbe3e4
updated_at: 2026-09-19 23:48 KST
```

## 1. 실측 기준선

최근 48시간 운영 `deploy_phase_events` 성공 표본 기준으로 image build p90은
815.5초, active-slot drain p50은 68.0초, standby same-digest sync p50은
254.0초, nginx cutover p50은 4.0초였다. active-slot 대기는 트래픽이 계속
유입되는 동안 직렬로 실행됐고, busy standby는 필수 5분 감시 전에 최대 180초를
추가로 기다렸다.

## 2. 변경

1. inactive target drain deadline을 release build 시작 시점에 열어 두 단계를 겹친다.
2. pre-cutover active drain은 DB owner lease 스냅샷만 남기고 기본 직렬 대기는 0초로 한다.
3. nginx graceful reload가 기존 stream을 유지하게 하고, old slot 재생성은 lease가 0일 때만 한다.
4. busy standby는 즉시 `success_partial` 경로로 보류하고 5분 P0/P1 감시와 drain을 겹친다.
5. 배포 flock 해제 뒤 retry worker가 `--no-build`로 same-digest를 맞춰야만 인증 완료한다.

## 3. 완료 기준

단위·계약 검사, release image 1회 빌드, candidate/routed health, 짧은 nginx lock,
routed-health rollback, DB `owner_instance` lease gate, standby same digest, 300초 P0/P1
무오류와 단계별 DB 시간이 모두 운영 배포 한 건에서 확인돼야 한다.
