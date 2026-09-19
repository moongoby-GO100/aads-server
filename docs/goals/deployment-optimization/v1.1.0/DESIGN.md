# AADS 배포 요청 병합·릴리스 배처 설계서

```yaml
document: design
version: 1.1.0
status: implementation-candidate
goal_id: 1997c471-ba2e-4f0d-a1e3-9df0dd6d1240
implementation_commit: 304cfcc0
updated_at: 2026-09-19 18:17 KST
```

## 1. 상태 흐름

```text
enqueue
  -> same SHA: deduplicate
  -> first request: queued_for_deploy
  -> later request: waiting_batch_predecessor

host drain
  -> migration/checksum gate
  -> lane advisory lock
  -> host Git ancestry + manifest risk classification
     -> all proven compatible: newest representative + inclusion ledger
     -> unknown/risky/diverged: oldest FIFO, later requests remain waiting
  -> exactly one queued_for_deploy per lane
  -> existing blue/green worker
```

## 2. 호환성 계약

| 검사 | 병합 허용 | 분리 조건 |
|---|---|---|
| Git 관계 | 모든 이전 SHA가 대표 SHA의 ancestor | unknown, diverged, ambiguous |
| manifest | `changed_files`가 비어 있지 않음 | 누락/빈 목록 |
| 위험 | risk flag 없음 | migration, dependency, release contract |
| 실행 계약 | deploy type 동일 | 불일치 |
| 승인 계약 | approval policy 동일 | 불일치 |

분류 실패는 큐를 변경하지 않고 drain을 중단한다. “최신 요청 우선”으로 폴백하지 않는다.

## 3. 데이터 계약

`deploy_batch_inclusions`는 대표 run, 포함 run, 두 SHA, relationship, 판정 이유와 판정
주체를 보존한다. `included_run_id`는 유일하며 같은 요청을 둘 이상의 릴리스에 포함할
수 없다. intake와 batcher는 같은 advisory-lock key를 사용한다.

## 4. 동시성·실패 복구

- intake transaction 안에서 lane lock을 획득한다.
- batcher는 판정 후 같은 lock을 획득하고 대상 행 상태를 재검증한다.
- 판정 사이에 큐가 바뀌면 transaction 전체를 실패시켜 원상 유지한다.
- migration 또는 batcher 실패 시 drain은 exit 1로 종료하고 큐를 보존한다.
- 위험 요청은 FIFO로 직렬 배포하며 다음 timer 실행에서 후속 요청을 승격한다.

## 5. 기존 blue/green 불변 조건

이 설계는 build, candidate health, nginx lock, routed health rollback, standby same digest,
5분 P0/P1 monitoring을 변경하지 않는다. 배처는 실행 전 큐만 정리한다.

## 6. 알려진 제한

직접 `deploy.sh` lock-busy 경로에는 기존 unconditional supersede 코드가 남아 있다.
동일 파일을 수정 중인 `runner-faeb3aae`가 review_hold이므로 이번 커밋은 해당 파일을
건드리지 않는다. 해당 후보를 정리한 뒤 같은 분류기를 직접 호출 경로에도 연결한다.
