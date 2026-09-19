# AADS 배포시스템 효율성 개선·최적화 기획서

```yaml
document: plan
version: 1.2.0
status: measured-candidate
goal_id: 1997c471-ba2e-4f0d-a1e3-9df0dd6d1240
milestone_id: b00da83d-ec47-460a-8ce0-10c814fbe3e4
updated_at: 2026-09-19 23:28 KST
```

## 1. 이번 버전의 목적

M4의 blue/green 안전 계약을 그대로 유지하면서, requirements와 Playwright가 바뀌지
않은 릴리스마다 wheel·브라우저·런타임 설치를 반복하는 낭비를 제거한다. 의존성
사전 준비는 릴리스와 분리하고, 실제 릴리스는 SHA당 이미지 한 번만 빌드한다.

## 2. 실측 기준선과 후보 결과

| 항목 | 실측값 | 출처 |
|---|---:|---|
| 최근 운영 image build p90 | 838.2초 | 운영 DB `deploy_runs` 단계 시간 |
| cold dependency image 총 시간 | 963.5초 | 2026-09-19 Docker BuildKit 로그 |
| cold wheelhouse | 120.2초 | 같은 빌드 로그 |
| cold runtime·Chromium | 296.1초 | 같은 빌드 로그 |
| cold export | 408.7초 | 같은 빌드 로그 |
| warm dependency 기반 code-only release smoke | 68.1초 | `aads-server:m4-cache-smoke-de49c580f19a` 빌드 로그 |

68.1초는 수동 release-image smoke 결과이며 blue/green 전체 완료 시간이나 운영 SLO로
확정하지 않는다. 운영 배포에서 candidate/routed health, standby 동기화와 5분 감시를
모두 통과해야 M4를 완료로 판정한다.

## 3. 구현 범위

1. Dockerfile을 `runtime-deps`와 `runtime`으로 분리한다.
2. Dockerfile·runtime/visual lock·profile·Playwright 설정을 해시한 불변 의존성 이미지를 사용한다.
3. `warm-deps`는 배포·재시작·배포 원장 생성 없이 의존성 이미지만 준비한다.
4. blue/green은 의존성 이미지가 없거나 라벨이 다르면 fail-closed한다.
5. 실제 릴리스 이미지는 release SHA당 한 번만 빌드하고 두 슬롯을 `--no-build`로 시작한다.
6. 기존 candidate health, 짧은 nginx lock, routed-health rollback, same digest, 5분 감시는 유지한다.

## 4. 완료 기준

- 단위·릴리스 계약 검사가 모두 통과한다.
- `warm-deps` 재실행이 이미 존재하는 동일 키 이미지를 재빌드하지 않는다.
- blue/green 1회에서 release image build가 정확히 한 번 실행된다.
- candidate와 routed health가 200이고 standby가 같은 digest를 사용한다.
- 전환 후 5분 동안 신규 P0/P1 오류가 없다.
- PLAN/DESIGN/PRD/LATEST v1.2.0이 Git과 `goal_documents`에 연결된다.
