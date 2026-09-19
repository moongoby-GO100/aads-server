# AADS Blue/Green 겹침 실행 설계

```yaml
document: design
version: 1.3.0
status: implementation-candidate
goal_id: 1997c471-ba2e-4f0d-a1e3-9df0dd6d1240
milestone_id: b00da83d-ec47-460a-8ce0-10c814fbe3e4
updated_at: 2026-09-19 23:48 KST
```

## 상태 흐름

```text
target lease sample + drain deadline
  ├─ immutable release build once
  └─ inactive target drains concurrently
       -> target owner lease=0
       -> candidate --no-build -> direct health
       -> active owner lease snapshot (no default serial wait)
       -> short nginx lock -> cutover -> routed health / rollback -> unlock
       -> standby lease=0: immediate --no-build same-digest sync
          standby busy: success_partial + detached retry after deploy flock release
       -> QA -> mandatory 300s P0/P1 monitor
       -> same digest only: certified success
```

## 안전 불변식

- stream count는 `owner_instance`, `owner_epoch`, `lease_expires_at`, heartbeat와 visible
  assistant content를 보수적으로 분류하며 unknown은 live로 처리한다.
- candidate health 전에는 nginx lock을 잡지 않는다. lock은 route/state marker와 즉시
  routed health까지만 유지한다.
- routed health 실패 시 이전 upstream·slot marker·resume owner를 즉시 복원한다.
- busy old slot은 재생성하지 않는다. retry도 active가 그대로이고 stream이 0일 때만
  동일 release image로 `--no-build --no-deps --force-recreate`한다.
- `success_partial`은 release provenance를 만들지 않으며 digest 일치 후에만 success가 된다.

## 관측

`build_candidate_image`, `target_slot_drain`, `active_slot_drain`, `nginx_cutover`,
`standby_same_digest_sync`, `p0p1_monitoring` 이벤트와 stream metadata를
`deploy_phase_events`에 저장한다. target metadata의 elapsed는 build와 겹친 전체 drain
창이며 phase duration은 build 뒤 추가 대기만 나타낸다.
