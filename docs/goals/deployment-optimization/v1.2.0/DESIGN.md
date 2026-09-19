# AADS 불변 의존성 이미지·단일 릴리스 빌드 설계서

```yaml
document: design
version: 1.2.0
status: measured-candidate
goal_id: 1997c471-ba2e-4f0d-a1e3-9df0dd6d1240
milestone_id: b00da83d-ec47-460a-8ce0-10c814fbe3e4
updated_at: 2026-09-19 23:28 KST
```

## 1. 상태 흐름

```text
warm-deps (명시적 사전 준비, release 아님)
  -> clean committed HEAD archive
  -> dependency key 계산
  -> 동일 key+label 이미지 존재: 무작업 성공
  -> 없음: runtime-deps 한 번 빌드·라벨 검증

bluegreen
  -> clean committed release SHA
  -> dependency image key+label 검증, 불일치/누락은 중단
  -> release image 정확히 한 번 빌드
  -> candidate --no-build -> direct health
  -> 짧은 nginx lock에서 cutover+state marker+routed health
  -> 이전 active drain -> same-image standby --no-build
  -> QA -> 5분 P0/P1 감시
```

## 2. 의존성 키 계약

키 입력은 커밋된 HEAD의 `Dockerfile`, `requirements.runtime.lock`,
`requirements.visual.lock`, `AADS_IMAGE_PROFILE`, `INSTALL_PLAYWRIGHT`다. 작업 트리의
미커밋 변경은 키에 들어가지 않는다. 태그는 `aads-server-deps:<key 앞 24자>`이고 전체
키를 `io.aads.dependency-key` 라벨에 저장한다. 태그가 있어도 라벨이 다르면 중단한다.

## 3. 이미지·디스크 계약

- `runtime-deps`는 wheel, 런타임 패키지와 Chromium까지 포함한다.
- `runtime`은 검증된 dependency image를 base로 코드와 editable install만 더한다.
- Docker의 공유 layer를 사용하므로 dependency/release 태그가 같은 대형 layer를 복제하지 않는다.
- cold dependency 준비는 최소 20GiB, 검증된 warm image를 쓰는 release는 최소 8GiB를 요구한다.
- 운영자가 `AADS_DEPLOY_MIN_FREE_GB`를 지정하면 해당 값을 우선한다.

## 4. 실패·롤백 계약

의존성 이미지 누락·키 불일치·디스크 부족·lock freshness 실패는 release build 전에
중단한다. candidate health 실패는 라우팅 전 중단하고, cutover 후 routed health 실패는
기존 upstream/state marker로 즉시 되돌린다. 이 변경은 active 컨테이너 직접 재시작이나
전체 compose 배포를 추가하지 않는다. 코드 롤백은 구현 커밋 revert, 운영 롤백은 기존
active release digest로 라우팅 복원이다.

## 5. 관측 계약

`dependency-image-warmup`, `dependency-image-reuse`, `build-disk-preflight`를 control audit에
남긴다. 운영 확정 시 release SHA, dependency key, release build 시간, candidate/routed
health, 두 슬롯 digest, P0/P1 감시 시작·종료를 한 배포 원장에서 조회할 수 있어야 한다.
