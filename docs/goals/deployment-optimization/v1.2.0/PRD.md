# AADS 불변 의존성 이미지 최적화 PRD

```yaml
document: prd
version: 1.2.0
status: measured-candidate
goal_id: 1997c471-ba2e-4f0d-a1e3-9df0dd6d1240
milestone_id: b00da83d-ec47-460a-8ce0-10c814fbe3e4
updated_at: 2026-09-19 23:28 KST
```

## 1. 사용자 가치

운영자는 안전 게이트를 줄이지 않고 코드-only 릴리스의 준비 시간을 단축할 수 있어야
한다. cold 준비가 필요할 때와 실제 릴리스가 진행될 때를 분리해, 느린 의존성 설치가
트래픽 전환이나 nginx lock 시간을 늘리지 않게 한다.

## 2. 기능 요구사항

| ID | 요구사항 | 수용 기준 |
|---|---|---|
| FR-04A | content-addressed deps | lock·Dockerfile·profile 변경 시 새 key, 동일 입력은 동일 key |
| FR-04B | 명시적 사전 준비 | `warm-deps`는 컨테이너·라우팅·deploy run을 변경하지 않음 |
| FR-04C | fail-closed release | dependency 이미지 누락·라벨 불일치 시 release build 전 종료 |
| FR-04D | 단일 release build | release SHA당 `docker build` 정확히 1회 |
| FR-04E | 동일 이미지 슬롯 | candidate와 standby가 동일 digest, 모두 `--no-build` 시작 |
| FR-04F | 안전 전환 | direct/routed health, rollback, DB lease, 5분 감시 유지 |
| FR-04G | 근거 연결 | v1.2.0 문서와 구현·배포 SHA가 목표 원장에서 조회됨 |

## 3. 사용자 흐름

- 첫 실행: 의존성 key 이미지가 없으면 운영자가 `warm-deps`를 실행하고 완료 증거를 확인한다.
- 반복 실행: 같은 입력이면 `warm-deps`는 즉시 no-op하고 blue/green은 code-only release를 빌드한다.
- 실패 복구: 누락·불일치 메시지가 정확한 태그와 재실행 명령을 제공하며 기존 active는 유지된다.
- 확인: 배포 상세에서 빌드 시간, health, routed slot, 두 digest, 감시 결과를 함께 확인한다.

## 4. 비기능 요구사항

- 배포 중단 시간 0초 목표를 유지한다.
- nginx lock 안에서 build·install·drain·standby sync를 수행하지 않는다.
- 미커밋·dirty source를 이미지에 포함하지 않는다.
- cold/warm 측정은 KST 시각과 명령 또는 DB 출처를 남긴다.
- 삭제·prune은 이 기능이 자동 수행하지 않는다.

## 5. 운영 수용 시나리오

1. 동일 key의 두 번째 `warm-deps`는 docker build 없이 성공한다.
2. dependency tag만 있고 전체-key 라벨이 다르면 blue/green은 중단된다.
3. 코드만 변경한 release가 의존성 단계를 반복하지 않고 release image 한 번으로 끝난다.
4. candidate direct health 실패 시 nginx upstream은 바뀌지 않는다.
5. routed health 실패 시 기존 active로 자동 롤백한다.
6. 성공 전환 뒤 standby가 같은 digest로 동기화되고 5분 P0/P1 감시가 통과한다.
