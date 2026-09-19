# AADS 배포 요청 병합·릴리스 배처 PRD

```yaml
document: prd
version: 1.1.0
status: implementation-candidate
goal_id: 1997c471-ba2e-4f0d-a1e3-9df0dd6d1240
implementation_commit: 304cfcc0
updated_at: 2026-09-19 18:17 KST
```

## 1. 사용자 가치

CEO와 운영자는 대기 숫자가 늘어도 각 요청이 사라지지 않았음을 확인하고, 어떤 변경이
한 릴리스에 포함됐는지 추적할 수 있어야 한다. 개발 담당자는 위험 변경이 자동으로
다른 변경에 섞이지 않고 순서대로 배포된다는 보장을 받아야 한다.

## 2. 기능 요구사항

| ID | 요구사항 | 수용 기준 |
|---|---|---|
| FR-02A | 요청 보존 | 다른 SHA 요청을 ancestry 판정 전에 supersede하지 않음 |
| FR-02B | 안전 병합 | low-risk ancestor만 최신 대표 SHA에 포함 |
| FR-02C | FIFO 분리 | manifest 누락·위험·분기 요청은 오래된 순서로 실행 |
| FR-02D | 관계 추적 | 대표/포함 run과 SHA가 DB에서 조회됨 |
| FR-02E | 동시성 | lane별 ready 요청이 최대 1건 |
| FR-02F | 실패 복구 | 분류 실패 시 큐 무변경, 원인 로그, 다음 timer 재시도 |

## 3. 사용자 흐름

- 첫 실행: 배포 현황에서 `실제 배포`, `배치 판정 대기`, `포함됨`을 구분한다.
- 반복 사용: 대표 릴리스 상세에서 포함된 작업·SHA·검사 이유를 확인한다.
- 실패 복구: ancestry/manifest가 불명확하면 자동 삭제하지 않고 FIFO 대기로 남긴다.
- 승인 경계: 승인 정책이 다른 요청은 자동 병합하지 않는다.

## 4. 수용 시나리오

1. 같은 lane의 low-risk ancestor 4건은 최신 대표 1건으로 실행된다.
2. migration 포함 요청은 선행 요청과 분리되어 FIFO로 실행된다.
3. 분기된 SHA는 포함관계를 만들지 않는다.
4. 동시에 두 요청이 들어와도 advisory lock으로 ready head가 하나만 남는다.
5. batch transaction 도중 큐가 바뀌면 어떤 요청도 supersede되지 않는다.
6. 인증 완료 후 기존 provenance와 batch inclusion을 함께 조회할 수 있다.

## 5. 비기능 요구사항

- 안전 게이트 우회 0건.
- 분류 결정은 Git/DB 근거로 재현 가능해야 한다.
- API image의 `.git` 부재를 감안해 ancestry는 host에서만 판정한다.
- 큐 정리는 이미지 빌드 횟수를 늘리지 않는다.
- 문서·코드·DB migration은 각각 Git SHA와 checksum으로 추적한다.
