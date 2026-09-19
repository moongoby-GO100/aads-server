# AADS 배포시스템 효율성 개선·최적화 설계서

```yaml
document: design
version: 1.0.0
status: approved-baseline
goal_id: 1997c471-ba2e-4f0d-a1e3-9df0dd6d1240
updated_at: 2026-09-19 17:32 KST
```

## 1. 설계 원칙

1. PostgreSQL 원장을 source of truth로 사용한다.
2. 코드 작업 완료와 릴리스 인증을 분리한다.
3. 같은 release SHA의 이미지는 정확히 한 번만 빌드하고 모든 슬롯은 그 이미지를 사용한다.
4. nginx lock은 candidate health 이후 route/state marker 갱신과 즉시 routed health 동안만 잡는다.
5. 실행 상태를 바꾸는 주체는 DB `owner_instance + owner_epoch` lease 보유자뿐이다.
6. 자동 판정이 불확실하면 fail-closed하되 이유와 다음 행동을 원장에 남긴다.

## 2. 목표 구조

```text
Runner / Chat / Admin
        |
        v
Release Intake -> Compatibility Classifier -> Batch Window
        |                                  |
        |                                  +-> superseded/included links
        v
Review Queue -> Model Retry -> Session Adjudicator Fallback
        |
        v
Release Manifest (immutable SHA, tests, migrations, rollback)
        |
        v
Blue/Green Coordinator
  build once -> candidate --no-build -> direct health
  -> short nginx lock -> cutover -> routed health/rollback
  -> old slot drain -> standby --no-build -> same digest
  -> QA -> 5-minute P0/P1 monitoring -> certified
        |
        v
deploy_runs + phase events + Dashboard + Handover
```

## 3. 배처 계약

배처 키는 `project + component + target_env`다. 실행 전 요청만 병합하며, 실행 중 release에 새 커밋을 끼워 넣지 않는다.

| 조건 | 처리 |
|---|---|
| 새 SHA가 기존 queued SHA의 fast-forward 후속이고 호환 | 최신 SHA를 대표 release로 선택, 이전 요청은 `superseded/included` |
| migration, dependency, rollback 계약 충돌 | 별도 release로 분리 |
| security/critical 승인 경계가 다름 | 더 높은 승인 tier로 합치거나 분리 |
| active deploy 존재 | 다음 batch window에서 최신 안전 SHA를 1건으로 유지 |
| diff/manifest 불명확 | 자동 병합 금지 |

각 원 작업은 대표 `deploy_run_id`, 포함 SHA, 변경 파일, 테스트, 최종 인증 결과를 역추적할 수 있어야 한다.

## 4. 리뷰 무응답 폴백

```text
review request
  -> model attempt (bounded timeout)
  -> retry with healthy configured model
  -> consecutive infrastructure no-response threshold
  -> durable deferred reaction to originating chat_session_id
  -> read-only adjudicator verifies tenant/job/SHA/diff hash/test evidence
  -> APPROVE: awaiting_approval only
  -> REJECT: review_failed with findings
  -> UNKNOWN: review_hold with explicit next retry
```

원 세션은 배포·push·DB 변경을 직접 실행하지 않는다. 검수 결과만 구조화해 중앙 상태 머신에 제출한다. 코드 결함과 리뷰 인프라 장애를 별도 category로 보존한다.

## 5. Blue/Green 상태 머신

| 상태 | 필수 입력 | 성공 전이 | 실패 처리 |
|---|---|---|---|
| preflight | clean committed SHA, rollback plan | image_build | 차단 |
| image_build | release SHA | candidate_start | 빌드 실패 기록 |
| candidate_start | immutable image digest | candidate_health | candidate 제거 |
| candidate_health | direct health 200 | cutover | 라우팅 유지 |
| cutover | 짧은 nginx lock | routed_health | 즉시 라우팅 롤백 |
| routed_health | external/routed 200 | old_slot_drain | 롤백 |
| old_slot_drain | slot-local live streams | standby_sync | bounded 대기·분류 |
| standby_sync | same release image | same_digest | 인증 실패 |
| certification | QA evidence | p0p1_monitor | 실패 기록 |
| p0p1_monitor | 5분 오류 표본 | completed | 롤백/incident |

## 6. 관측 모델

필수 지표는 queue depth/age, review latency와 no-response 비율, batch coalesce ratio, build/drain/sync/certification phase duration, rollback count, same-digest mismatch, routed health failure다.

화면은 기본적으로 가장 자주 쓰는 배포 현황을 먼저 보여 주고 설정은 Admin으로 분리한다. 세션 만료·권한 부족·네트워크 오류에는 재로그인, 권한 요청, 재시도 경로를 제공한다. 모바일은 대기/진행/오류/승인 카드와 재시도 버튼을 한 손 조작 크기로 제공한다.

## 7. 변경·버전·롤백

- 문서: SemVer 버전 디렉터리 + `LATEST.md` + Git commit.
- DB 정책: append-only policy version 또는 감사 이벤트를 사용한다.
- 코드: 작은 단계별 커밋, release manifest에 커밋 목록 기록.
- 롤백: route 이전 복원, 이전 검증 이미지, 정책 이전 버전을 각각 독립적으로 참조한다.
- 문서와 구현이 달라지면 구현 릴리스 전에 문서 minor/major 버전을 먼저 올린다.

## 8. 검증 전략

- 단위: classifier, state transition, retry budget, tenant/SHA/diff hash 검증.
- 통합: 여러 queued 변경의 단일 release 병합, review model timeout fallback.
- 장애주입: candidate health 실패, routed health 실패, stale/live stream 혼합, standby digest 불일치.
- E2E: Ops 화면에서 대기→진행→인증/실패 복구 경로와 모바일 렌더 확인.
- 운영: external health 200, 두 슬롯 same digest, 5분 P0/P1 신규 오류 0건.

