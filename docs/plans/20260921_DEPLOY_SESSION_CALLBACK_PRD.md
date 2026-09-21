# 배포 등록 세션 실패 알림·후속조치 PRD

## 1. 목표

배포를 등록한 채팅 세션이 배포 종료 결과를 놓치지 않게 한다. 성공은 결과만
알리고, 실패·차단·부분성공은 같은 세션에 영속 알림을 남긴 뒤 AI 후속 반응을
자동 요청한다. Telegram과 기존 `deploy_autoheal`은 보조 채널로 그대로 둔다.

## 2. 사용자와 핵심 흐름

- 대상 사용자: AADS에서 배포를 등록·승인하는 CEO와 운영 담당자.
- 첫 실행: 배포 요청 API 또는 Pipeline Runner가 현재 `chat_session_id`를
  `deploy_runs`에 결합한다.
- 반복 사용: 사용자는 채팅을 떠나도 같은 세션의 히스토리에서 배포 결과,
  실패 단계, 오류 요약, 배포 ID와 SHA를 확인한다.
- 실패 복구: 실패·차단·부분성공이면 활성 API 슬롯이 AI 반응을 트리거한다.
  AI는 DB·로그·헬스를 실측하고, 승인 없는 파괴 작업이나 재배포는 하지 않는다.
- 연결 복구: 세션이 실행 중이거나 슬롯 전환 중이면 기존 deferred reaction 큐가
  인계한다. 알림 claim이 중단되면 5분 뒤 다른 활성 슬롯이 회수한다.

## 3. 기능 요구사항

| ID | 요구사항 | 완료 기준 |
|---|---|---|
| FR-1 | 배포 원장에 원 세션 UUID를 정규화 저장 | FK가 유효한 세션만 결합 |
| FR-2 | terminal 상태를 멱등 claim | 동일 run/status 알림 1건 |
| FR-3 | 성공은 알림만, 문제 상태는 알림+AI 반응 | 성공 시 모델 호출 0회 |
| FR-4 | 활성 슬롯만 callback 소비 | 비활성 슬롯 claim 0건 |
| FR-5 | 오류 시 재시도하고 3회 후 명시적 실패 | 상태·attempts·오류 DB 기록 |
| FR-6 | 기존 배포 이력은 재생하지 않음 | 마이그레이션 후 과거 알림 0건 |

문제 상태는 `failed`, `error`, `blocked`, `cancelled`, `success_partial`이다.
`superseded`는 정보 알림만 남긴다.

## 4. 데이터·상태 설계

`deploy_runs`에 `chat_session_id`, 알림 상태, 시도 횟수, claim 소유자/시각,
완료 시각, 마지막 오류를 추가한다. 상태는 아래처럼 전이한다.

```text
unbound ──(유효 세션으로 등록)──> pending
pending ──claim──> processing ──메시지 저장──> reported
reported ──AI 반응 접수──> notified
processing ──성공/대체됨──> notified
processing/reported ──실패──> pending/reported ──3회──> failed
```

claim은 `FOR UPDATE SKIP LOCKED`와 `session_notification_owner`로 fencing한다.
5분 넘은 `processing`은 회수한다. 메시지는
`deploy-run:{run_id}:terminal:{status}` idempotency key로 중복을 차단한다.

## 5. 운영·보안 원칙

- 알림 본문은 비밀값이나 전체 로그를 싣지 않고 1,500자 오류 요약만 사용한다.
- AI 반응은 기존 `trigger_ai_reaction`을 사용해 실행 lease·active slot·deferred
  queue 규칙을 그대로 적용한다.
- callback 실패가 배포 결과 자체를 변경하지 않는다.
- 재배포, 삭제, 컨테이너 재시작은 기존 승인 정책을 우회하지 않는다.

## 6. 검증 기준

1. 유효/무효 세션 결합 단위 테스트.
2. 동시 claim SQL에 `SKIP LOCKED`, owner fencing, stale 회수 확인.
3. 성공 run은 세션 메시지만 생성하고 반응을 호출하지 않음.
4. 실패 run은 세션 메시지와 AI 반응을 각각 한 번 호출.
5. 메시지 성공·반응 실패 시 메시지를 중복 생성하지 않고 반응만 재시도.
6. 마이그레이션을 임시 PostgreSQL 스키마 또는 운영 DB 트랜잭션에서 검증.
7. 배포 후 API health, 동일 digest, 외부 routed health, 5분 P0/P1 감시 확인.
