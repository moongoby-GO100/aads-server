# AADS 배포시스템 효율성 개선·최적화 기획서

```yaml
document: plan
version: 1.0.0
status: approved-baseline
goal_id: 1997c471-ba2e-4f0d-a1e3-9df0dd6d1240
owner_session_id: 539a6086-4854-49bf-a590-8ea60704d78b
owner_role: CTO
updated_at: 2026-09-19 17:32 KST
```

## 1. 목적

AADS의 코드 완료부터 운영 인증까지를 하나의 릴리스 흐름으로 관리한다. 여러 러너가 만든 호환 변경은 안전한 최신 SHA로 병합하고, 리뷰 모델 무응답·중복 배포·SSE drain·standby 동기화가 무기한 큐 적체로 이어지지 않게 한다.

안전 기준은 속도를 위해 완화하지 않는다. 모든 API 릴리스는 release SHA당 이미지 1회 빌드, `--no-build` 슬롯 시작, candidate health 선검증, 짧은 nginx lock, DB lease 소유권, routed health 실패 즉시 롤백, standby same-digest, 5분 P0/P1 무오류 감시를 지킨다.

## 2. 기준선

측정 시각은 `2026-09-19 17:32 KST`다.

| 항목 | 실측값 | 출처 |
|---|---:|---|
| AADS 러너 queued | 18건 | 운영 DB `pipeline_jobs` |
| AADS review_hold | 19건 | 운영 DB `pipeline_jobs` |
| AADS running | 3건 | 운영 DB `pipeline_jobs` |
| 최근 24시간 성공 배포 | 16건 | 운영 DB `deploy_runs` |
| 최근 24시간 실패 배포 | 7건 | 운영 DB `deploy_runs` |
| 성공 배포 평균 | 14.5분 | 운영 DB `deploy_runs.duration_ms` |
| 최신 배포 | #4773, `build_candidate_image` 진행 | 운영 DB `deploy_runs` |

이 값은 목표치가 아니라 출발점이다. 구현 단계마다 동일 쿼리로 다시 측정해 개선 여부를 판단한다.

## 3. 대상 사용자와 흐름

| 사용자 | 첫 진입 | 반복 사용 | 실패 복구 |
|---|---|---|---|
| CEO | 배포 현황에서 대기·진행·승인 필요를 즉시 확인 | 변경 묶음과 최종 인증 결과 확인 | 원인, 롤백 상태, 재시도 버튼 확인 |
| CTO/Ops | phase·queue·review 병목 확인 | SLO 위반과 자동복구 결과 점검 | fail-closed 원인과 수동 개입 경로 확인 |
| Runner | commit/push 후 배포 요청 등록 | 동일 범위 요청은 batch에 합류 | 리뷰 무응답은 bounded fallback으로 이관 |
| 개발 담당 | 변경이 어느 release SHA에 포함됐는지 확인 | 테스트·배포 증거 추적 | superseded/blocked 사유와 후속 작업 확인 |

## 4. 범위

### 포함

- 배포 요청 coalescing과 release manifest
- 러너 리뷰 처리량, 재시도, 원 지시 세션 검수 폴백
- blue/green build, drain, cutover, standby sync 시간 최적화
- 큐·phase·오류·승인·재시도·롤백 관측 API와 화면
- 회귀·부하·장애주입·롤백·5분 감시 검증
- 기획·설계·PRD의 SemVer·Git·DB 연결

### 제외

- active API 직접 재시작
- full compose stack 배포
- dirty worktree를 릴리스 이미지에 포함
- 5분 P0/P1 감시 삭제
- 보안·데이터 무결성 게이트를 성능 때문에 우회

## 5. 마일스톤

| 순서 | 마일스톤 | 핵심 산출물 | 완료 증거 |
|---:|---|---|---|
| M1 | 기준선·SLO·문서 정본 확정 | v1.0.0 문서, DB 링크 | Git SHA + goal_documents |
| M2 | 배포 요청 병합·릴리스 배처 최적화 | batch/coalesce 계약 | 통합 테스트 + 원장 관계 |
| M3 | 리뷰 처리량·원 세션 판정 폴백 | bounded retry/adjudicator | 무응답 재현 테스트 |
| M4 | Blue/Green 병목 최적화 | build/drain/sync 개선 | phase 시간 + 안전 게이트 |
| M5 | 관측·자동복구·관리 화면 | API·대시보드·알림 | API 응답 + 화면 캡처 |
| M6 | 회귀·부하·롤백·릴리스 인증 | 검증 보고·핸드오버 | external 200, same digest, 5분 감시 |

## 6. 성공 지표

- 동일 프로젝트·컴포넌트의 호환 queued 요청은 하나의 최신 안전 release로 병합되고 포함 관계가 원장에 남는다.
- `REVIEW_MODEL_NO_RESPONSE`가 무기한 `review_hold`를 만들지 않는다.
- 성공 릴리스의 요청→인증 p95를 20분 이하로 관리한다. 단, 안전한 실사용 스트림 보호로 넘긴 경우 별도 분류한다.
- 배포 화면에서 대기 이유, 현재 phase, 마지막 오류, 승인 필요, 재시도와 롤백 상태를 한 맥락에서 확인한다.
- 필수 blue/green 계약 위반은 0건이다.

## 7. 리스크와 대응

| 리스크 | 대응 |
|---|---|
| 호환되지 않는 변경을 한 배치로 병합 | 파일·migration·dependency 위험 분류 후 충돌 시 배치 분리 |
| 원 세션 검수가 자기 작업을 자동 승인 | 독립 read-only intent, diff hash·SHA·tenant 검증, 승인 전 상태까지만 이동 |
| stale stream 오판으로 응답 유실 | DB owner lease·heartbeat·visible delta를 함께 보고 unknown은 fail-closed |
| 최적화가 안전 감시를 축소 | 5분 P0/P1 감시는 고정 인증 단계로 유지 |

