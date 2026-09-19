# AADS 배포시스템 효율성 개선·최적화 PRD

```yaml
document: prd
version: 1.0.0
status: approved-baseline
goal_id: 1997c471-ba2e-4f0d-a1e3-9df0dd6d1240
updated_at: 2026-09-19 17:32 KST
```

## 1. 제품 정의

AADS Deployment Control은 여러 러너와 세션이 만든 변경을 안전한 릴리스 단위로 묶고, 리뷰부터 blue/green 인증까지의 상태와 복구 행동을 CEO가 한 화면에서 확인하게 하는 운영 기능이다.

## 2. 사용자 요구

| ID | 사용자 스토리 | 우선순위 |
|---|---|---|
| U-01 | CEO로서 대기 건이 실제 배포인지 코드/리뷰 대기인지 구분하고 싶다. | P0 |
| U-02 | CEO로서 여러 호환 변경이 한 번의 배포에 포함되는지 확인하고 싶다. | P0 |
| U-03 | CTO로서 리뷰 모델 무응답과 코드 반려를 구분하고 자동 복구하고 싶다. | P0 |
| U-04 | 운영자로서 현재 phase, 경과시간, 마지막 오류와 롤백 상태를 보고 싶다. | P0 |
| U-05 | 담당자로서 내 변경이 어느 release SHA와 인증 결과에 포함됐는지 추적하고 싶다. | P1 |
| U-06 | 모바일 사용자로서 세션이 끊겨도 상태를 복구하고 재시도하고 싶다. | P1 |

## 3. 기능 요구사항

| ID | 요구사항 | 수용 기준 |
|---|---|---|
| FR-01 | 대기 분류 | queued code, review_hold, approval, deploy queue를 별도 수치·최장 대기시간으로 표시 |
| FR-02 | 릴리스 병합 | 호환 queued 요청을 최신 안전 SHA 1건으로 만들고 포함 관계 보존 |
| FR-03 | 불변 manifest | SHA, commits, files, tests, migration, rollback, approvals를 실행 전 고정 |
| FR-04 | 리뷰 폴백 | bounded 모델 재시도 후 원 세션 read-only adjudicator로 이관 |
| FR-05 | 안전 배포 | build-once, no-build start, candidate/routed health, short lock, rollback, same digest 적용 |
| FR-06 | 인증 분리 | cutover와 release certification 상태를 별도 표시하고 5분 감시 전 완료 금지 |
| FR-07 | 복구 행동 | 실패 category별 재시도·분리 배포·승인·롤백 행동을 제공 |
| FR-08 | 증거 연결 | runner job→goal/milestone→deploy run→phase/test/health→handover 연결 |
| FR-09 | 문서 버전 | PLAN/DESIGN/PRD 최신 버전과 변경 이력을 목표 화면에서 열 수 있음 |

## 4. 비기능 요구사항

| ID | 요구사항 | 기준 |
|---|---|---|
| NFR-01 | 안전성 | 필수 release gate 우회 0건 |
| NFR-02 | 정합성 | 동일 배포의 active/standby image digest 일치 |
| NFR-03 | 소유권 | 상태 변경은 유효 DB lease 보유자만 수행 |
| NFR-04 | 추적성 | 모든 자동 병합·재시도·판정·롤백은 감사 이벤트 보존 |
| NFR-05 | 성능 | 요청→인증 p95 20분 이하 목표, 초과 원인 phase 기록 |
| NFR-06 | 복구성 | routed health 실패 시 즉시 이전 라우트 복원 |
| NFR-07 | 접근성 | 모바일 줄바꿈, 큰 터치영역, 키보드 접근, 상태 텍스트 제공 |

## 5. 화면 요구사항

첫 화면 상단에는 `배포 중`, `실제 배포 대기`, `리뷰 보류`, `승인 필요`, `마지막 실패`를 표시한다. 내부 job ID보다 업무명과 변경 요약을 우선하며 상세 보기에서만 ID와 SHA를 보여 준다.

각 릴리스 카드는 현재 phase, 경과시간, 포함 변경, 마지막 로그 요약, 승인/재시도/롤백 가능 여부를 보여 준다. 실패 시 막다른 화면 대신 권한·세션·네트워크·리뷰 인프라·코드 결함별 다음 행동을 제공한다.

## 6. 수용 시나리오

1. 호환 변경 4건이 queued이면 대표 release 1건만 실행되고 4건 모두 포함 관계를 조회할 수 있다.
2. 리뷰 모델이 연속 무응답이면 정해진 시간 안에 원 세션 검수 요청이 생성되고 중복 요청은 없다.
3. candidate health가 실패하면 nginx route는 바뀌지 않는다.
4. routed health가 실패하면 이전 route가 즉시 복원되고 실패 원인이 원장에 남는다.
5. active 사용자 stream과 stale placeholder가 섞이면 live stream만 보호하고 unknown은 fail-closed한다.
6. cutover 성공 뒤 standby digest가 다르면 release는 인증 완료가 되지 않는다.
7. 5분 P0/P1 감시 중 새 오류가 생기면 `completed`가 되지 않는다.
8. 브라우저 세션이 만료되면 재로그인 후 같은 release 상세로 복귀한다.

## 7. 완료 정의

- M1~M6의 completion criteria와 연결된 테스트·운영 증거가 모두 존재한다.
- 외부 health 200, candidate/routed health, rollback 훈련, same digest, 5분 P0/P1 무오류가 확인된다.
- 목표 화면에 주도담당, 마일스톤, PLAN/DESIGN/PRD 최신 버전이 표시된다.
- DB 핸드오버에 최종 SHA, 검증 결과, 남은 리스크가 기록된다.

## 8. 의존성과 비목표

의존성은 `deploy_runs`, `deploy_phase_events`, `pipeline_jobs`, goal/milestone 원장, chat session deferred reaction, nginx blue/green adapter다. active 직접 restart, full compose deploy, dirty build, 감시 삭제는 제품 기능으로 제공하지 않는다.

