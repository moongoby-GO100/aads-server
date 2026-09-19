# AADS 배포시스템 효율성 개선·최적화 기획서

```yaml
document: plan
version: 1.1.0
status: implementation-candidate
goal_id: 1997c471-ba2e-4f0d-a1e3-9df0dd6d1240
milestone_id: 4dc8d7b3-bfb8-4500-bb6a-1c10a8b41b56
implementation_commit: 304cfcc0
updated_at: 2026-09-19 18:17 KST
```

## 1. 이번 버전의 목적

v1.0.0 기준선을 유지하면서 M2의 배포 요청 병합을 “최신 요청이 이전 요청을 무조건
대체”하는 방식에서 “호스트 Git이 포함 관계와 위험도를 증명한 요청만 한 릴리스로
병합”하는 방식으로 전환한다.

## 2. 18:17 KST 실측 기준선

| 항목 | 실측값 | 출처 |
|---|---:|---|
| AADS pipeline queued | 13건 | 운영 DB `pipeline_jobs` |
| AADS review_hold | 26건 | 운영 DB `pipeline_jobs` |
| AADS running | 4건 | 운영 DB `pipeline_jobs` |
| 실제 배포 검증 중 | 1건 | 운영 DB `deploy_runs`, `p0p1_monitoring` |
| 실제 배포 대기 | 0건 | 운영 DB `deploy_runs` |
| 배처 단위·관측 테스트 | 23건 통과 | `pytest` |

## 3. 구현 범위

1. API intake는 같은 SHA만 중복 제거하고 서로 다른 요청을 보존한다.
2. lane(`project + component + target_env`)별 advisory lock으로 동시 요청을 직렬화한다.
3. 첫 요청만 `queued_for_deploy`, 후속 요청은 `waiting_batch_predecessor`로 둔다.
4. 호스트 배처가 Git ancestor, deploy type, 승인 정책, manifest 완전성, 위험 파일을 검사한다.
5. 병합 시 `deploy_batch_inclusions`에 대표/포함 run과 SHA를 기록한다.
6. 불명확·migration·dependency·release contract 요청은 FIFO로 분리한다.

## 4. 단계 판정

| 단계 | 상태 | 완료 증거 |
|---|---|---|
| API 요청 보존 | 구현 | `304cfcc0`, 단위 테스트 |
| 호스트 분류기 | 구현 | 실제 Git ancestry 통합 테스트 |
| 포함관계 원장 | 운영 DB 적용 | migration checksum + table 존재 |
| 기존 큐 dry-run | 검증 | `batch queue empty`, 무변경 |
| 운영 릴리스 | 대기 | 현재 #4779 인증 종료 후 blue/green 1회 |
| 직접 `deploy.sh` 요청 통합 | 보류 | `runner-faeb3aae` 동일 파일 충돌 해소 필요 |

## 5. 완료 기준

- 호환 요청 4건이 대표 release 1건과 inclusion 4건 이하 관계로 남는다.
- 위험/불명확 요청은 순서를 바꾸거나 supersede하지 않는다.
- release SHA당 image build는 1회다.
- candidate/routed health, rollback, same digest, 5분 P0/P1 감시는 기존 계약을 유지한다.
