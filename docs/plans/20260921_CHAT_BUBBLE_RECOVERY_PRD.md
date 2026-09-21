# 채팅 응답 버블 보존 및 재개 무한 반복 방지 PRD

- 작성일: 2026-09-21
- 상태: 구현·데이터 복구 완료 / 운영 API 배포는 정상 활성 스트림 drain 대기로 차단 (2026-09-21 20:14 KST)
- 범위: 채팅 API, 실행 복구, Claude 인증 사전 검사, 두 신고 세션의 상태 복구
- 오류 사전: `chat.oauth_lock_outlives_first_response_watchdog`

## 1. 문제와 확인 근거

| 세션 | 관측 결과 | 개선 대상 |
| --- | --- | --- |
| `7fb5f50a-9fe7-4a29-9333-9c023125aa6e` | 첫 응답 198~199초 타임아웃 후 assistant_message_id=NULL. 새 사용자 요청에 의해 대체된 실행 `bc296d42`가 재개되며 owner_epoch 535. 19:31 KST 기준 5분간 resume 241건 | 취소 분류, 버블 보존, 과거 실행 재개 차단 |
| `15782f6e-35ca-475b-ac45-c152c26a42fa` | 첫 응답 197~198초 타임아웃 후 버블 유실. 이후 정상 응답 완료 | 동일 경로 회귀 방지; 정상 실행은 보존 |
| `2c929b8e-ae6a-4351-bca1-3c850273d981` | 실제 claude-opus-5, OAuth slot4에서 정상 출력 확인 | 비교 대조군; 모델 및 정상 슬롯 설정 보존 |

Opus 5 전체 장애로 규정하지 않는다. 이전 실패 시점에 slot2 인증 만료/300초 락 대기가 관측됐지만, 현재 세션이 계속 응답하지 못하는 직접 원인은 실행 생명주기 결함이다. 모델명이 같아도 인증 슬롯은 다르다. `/health`의 slot2는 access/refresh token 부재, slot4 출력은 실제 provider receipt로 확인했다. 검증 시각이 오래됐다는 이유만으로 정상·갱신 가능한 슬롯을 차단하지 않는다.

확인된 결함:

1. 시스템 타임아웃이 만든 `CancelledError`를 새 사용자 요청에 의한 대체로 오분류하여 빈 버블을 삭제한다.
2. 자동 재개가 기존 작업 확인 전에 lease/placeholder를 변경해, 실제 재개 작업 없이 retrying 상태를 만든다.
3. 수동/자동이 공유하는 resume API가 `_archived_partial`을 다시 고르며, lease 획득 전에 최신 사용자 요청 여부를 검증하지 않는다.
4. placeholder 우회 조회에서 연결된 execution_id를 NULL로 만들어 terminal 상태 검사를 우회할 수 있다.
5. API의 슬롯 사전 검사 누락과 wrapper의 300초 락 대기는 인증 오류를 첫 응답 제한보다 늦게 전달한다.

## 2. 목표와 비목표

- 실패한 턴에도 사용자에게 보이는 응답 버블 하나와 실패 이유를 남긴다.
- 최신 사용자 요청보다 오래된 실행, 완료/취소된 실행, 보관된 부분 응답은 재개하지 않는다.
- 기존 작업이 살아 있거나 다른 owner가 lease를 보유하면 재개 상태를 변경하지 않는다.
- 실제 모델 호출 전 대기·검사·중복 요청은 모델 재시도 예산을 소모하지 않는다.
- 인증 불가 슬롯은 빠르게 기존 폴백 경로로 넘긴다. 모델/계정의 사용자 선택 정책은 바꾸지 않는다.
- 토큰 재발급을 위해 사용자 계정에 임의 로그인하거나, 정상 세션/전체 서비스를 재시작하지 않는다.

## 3. 구현 설계

### 3.1 취소와 버블 생명주기

- superseded 판정은 `superseded`, `newer_user`, `new_execution` 같은 실제 원인으로 한정한다.
- `CancelledError` 자체는 실행 교체 증거가 아니다. 타임아웃은 interruption_notice 또는 interrupted_partial로 보존한다.
- 이미 terminal인 실행에 늦게 도착한 정리 콜백은 메시지나 포인터를 변경하지 않는다.
- 자동 재개는 활성 API 여부, 살아 있는 로컬 작업, 최신 사용자 요청 여부를 확인한 뒤 lease를 획득한다.

### 3.2 재개 자격과 중복 방지

- lease 획득 공통 지점에서도 superseded 실행을 거부한다. 수동 재시도 예산 초기화로 이를 우회할 수 없다.
- resume API는 최신 실행에 귀속된 실제 execution_id를 유지한다. 실행 없는 legacy placeholder만 별도 취급한다.
- `_archived_partial`/숨김/삭제 메시지는 자동 재개 후보에서 제외한다.
- 새 사용자 요청이 있거나 선택된 실행이 terminal 완료/취소 상태이면 부작용 없이 거부한다.
- DB lease와 로컬 task 등록으로 동일 실행의 동시 재개를 차단한다. 재개 시작 전후 owner_epoch를 유지한다.
- 실패 후 즉시 재개 반복을 막기 위해 DB 기준 재개 간격을 적용하며, 실제 모델 호출 횟수 상한은 유지한다.

### 3.3 인증 실패 처리

- 요청 슬롯의 `auth_available=false` 같은 확정된 인증 불가 상태만 빠르게 거부한다. validation_stale 및 갱신 가능한 만료 토큰은 허용한다.
- 인증 불가/락 혼잡은 같은 슬롯에서 불필요하게 재시도하지 않고 기존 계정 폴백에 전달한다.
- credential flock은 짧은 제한 시간 안에 획득하지 못하면 실패한다. 잠금 없이 토큰 갱신을 진행하는 경로를 제거한다.
- CLI 내부 자동 갱신이므로 잠금을 임의로 풀면 refresh-token 경쟁이 발생한다. 이번 조치는 락 대기 상한·실패 처리부터 적용하고, 갱신 전용 임계 구역 분리는 명시적 refresh API 검증 후 진행한다.

### 3.4 운영 데이터 복구

- 두 신고 세션만 대상으로, 변경 전 실행/메시지/포인터 상태를 보관한다.
- 정상 활성 실행과 valid lease는 건드리지 않는다.
- superseded terminal 실행에 잘못 남은 streaming_placeholder는 내용을 보존해 보관 상태로 전환한다.
- current_execution_id가 해당 terminal 실행을 가리킬 때만 조건부로 해제한다.
- 버블이 없는 실패 실행은 복구 안내 버블로 보완하되 새 모델 호출이나 과거 도구 실행을 자동 재실행하지 않는다.

## 4. 수용 기준

1. 타임아웃+CancelledError에서 버블이 삭제되거나 숨김 상태로 남지 않는다.
2. 활성 task가 있을 때 자동 재개 함수는 DB write/lease claim을 하지 않는다.
3. 새 사용자 요청에 의해 대체된 실행은 어떤 resume 경로에서도 owner_epoch가 증가하지 않는다.
4. 동시 재개 요청은 한 실행/한 placeholder로 수렴한다. budget reset으로 superseded를 되살리지 못한다.
5. 실제 인증 불가 슬롯은 POST /stream 전 실패하고, 정상 slot4 및 validation_stale slot1은 차단하지 않는다.
6. 인증 락 경합은 제한 시간 안에 종료하며 잠금 없이 진행하지 않는다.
7. 신고 세션의 현재 실행과 정상 비교 세션의 모델/기존 내용이 보존된다.
8. 관련 단위/통합 회귀 검사, 불변 이미지 Blue/Green 배포, 외부 health, 두 슬롯 동일 digest, 최소 5분 P0/P1 관측을 통과한다.

## 5. 배포 및 롤백

- 기존 미커밋 변경은 보존하고 격리 worktree에서 구현한다.
- 대상 파일만 커밋·푸시하고 clean release SHA에서 이미지 한 번을 빌드한다.
- candidate health → 라우팅 전환 → 외부 health → 기존 요청 drain → 동일 이미지 standby 동기화 순서로 배포한다.
- 실패하면 기존 이미지 라우팅으로 롤백한다. 데이터 복구는 보관한 snapshot과 조건부 역변경으로 복원 가능해야 한다.
- 실제 테스트/배포 SHA, 관측 시각, 남은 한계는 완료 보고에 별도 기록한다.

## 6. 사전 검증 결과

- 관련 단위 회귀 검사 308개 통과: 취소/버블, 복구 owner fence, OAuth 갱신/락, 모델 계약, 채팅 서비스/명령 생명주기.
- PostgreSQL 통합 검사 10개 통과: 실제 lease SQL 9개와 운영 visibility trigger를 적용한 타임아웃 버블 보존 1개. 접속 전용 `pg_temp` 테이블만 사용한다. 과거 실행 epoch=535에서 수동 예외를 주어도 superseded/새 사용자/완료/취소 실행은 변경되지 않는다. 추가 지시·시스템 트리거·회수된 메시지는 정상 재개를 막지 않는다.
- credential lock의 공유/배타 경합 모두 제한 시간 후 exit 75로 종료함을 실행 검증했다.
- `scripts/repair_chat_bubble_recovery.py`: 기본 preview, `--apply --snapshot` 명시 시에만 두 신고 세션의 terminal 상태와 누락 안내 4건을 복구한다. 실행 중 상태는 제외하고 snapshot을 덮어쓰지 않는다.

## 7. 운영 조치 및 미완료 항목 (2026-09-21 20:14 KST)

- 애플리케이션 수정 커밋 `425a5a73306c654ff49b235e39019c06277d27b3`을 main과 작업 브랜치에 푸시했다.
- 실제 Opus 5/slot4 단건 검증에서 응답 `OK`, `model_verified=true`, `model_mismatch=false`를 확인했다. 정상 모델·계정 선택은 변경하지 않았다.
- 두 신고 세션의 누락 오류 안내를 각 2건, 총 4건 복구했다. 원래 실행 종료 시각으로 저장하여 이후 대화 순서를 보존했다. 기존 답변/사용자 지시는 변경하지 않았다.
- 복구 중 기존 DB trigger가 INSERT의 `interruption_notice`를 숨기는 것을 확인했다. 정상 `_mark_execution_interrupted` 경로에 이미 있는 최종 `is_hidden` 전용 UPDATE를 복구 스크립트에도 적용했다. 전역 trigger 정책은 변경하지 않았다. 4건 모두 `is_hidden=false`, execution 연결 정상이다.
- 변경 전 snapshot: `/root/aads/backups/chat-bubble-recovery-20260921/before-repair-20260921.json`, `/root/aads/backups/chat-bubble-recovery-20260921/before-visibility-repair-20260921.json` (0600).
- 복구 시 기존 placeholder 보관 0건, current_execution_id 해제 0건. 첫 신고 세션의 현재 실행 `e2561c93`과 비교 세션 `8bbc1c71`을 보존했다.
- 현재 프론트엔드의 실제 표시 판정 함수를 실행해 복구 문구가 숨김/짧은 placeholder 필터를 통과함을 확인했다. 이것은 브라우저 화면 검증과 구분한다.
- 배포 요청 #4987은 상위 릴리스 `da94c12385cf`의 #4988에 통합되었다. 수정된 앱/인증 wrapper 파일이 포함된 것을 비교 확인했다. 이미지 digest: `sha256:57cf1de69d70a2282e130256f761d83fa3b997338c4a11211737af368294939b`.
- #4988 및 자동 재시도 #4990은 `target_slot_drain`에서 차단됐다. 마지막 확인 시 이전 green에 유효한 lease/heartbeat를 가진 정상 실행 3건이 남아 있고, 이 중 사용자가 정상 응답이라고 제시한 비교 세션이 포함된다. 강제 중단하지 않았다.
- 아직 새 API 이미지로 라우팅 전환되지 않았다. 운영 active=`a08bcc01a2d2`, 이전 green=`e772c4ce7628`이다. 호스트 wrapper의 5초 락 제한은 반영됐지만 API 수정 전체가 운영 적용됐다고 보고하지 않는다.
- 남은 완료 조건: 이전 green 실행 자연 종료 → 동일 이미지 배포 재개 → 외부 health → 이전 active drain/동일 digest standby → 최소 5분 P0/P1 관측. 자동 재시도 예산은 소진되어 #4990은 운영자 확인을 요청한 상태다.

### 20:17 KST 재검증: 복구 후 구버전 재개에 의한 재보관

- 후속 검증에서 복구 4건 중 `d9eb0fad` 실행의 안내 1건이 구버전 실행 처리에 의해 다시 `_archived_partial/is_hidden=true`로 바뀐 것을 확인했다. owner_epoch가 2→3, 원인이 `stale_superseded_by_newer_user_message`로 변경됐다. 다른 3건은 표시 가능하며 네 메시지의 실행 연결은 유지된다.
- 따라서 "누락 4건 복구 작업 수행"과 "4건 표시 상태 유지"는 다르다. 재발 방지 API가 아직 배포되지 않았으므로 완전 복구로 보고하지 않는다. 진행 중인 새 사용자 요청과 경쟁하지 않도록 반복적인 강제 상태 덮어쓰기는 하지 않는다.
- 정상 green 실행은 3건에서 2건으로 감소했다. 비교 세션 `2c929b8e`의 현재 실행은 여전히 유효한 heartbeat/lease를 보유한다.
- 브라우저 확인은 로컬 Playwright 실행 파일 부재로 실패했다. 기존 이미지의 브라우저로 대체 실행했으나 첫 세션은 화면 대기 timeout, 두 번째 세션은 초기 DOM에서 대상 버블을 찾지 못했다. 변경 요청을 차단한 읽기 전용 검사였으며, 화면 표시 성공으로 보고할 수 없다. DB/프론트 함수 검증을 화면 E2E 성공으로 대체하지 않는다.
