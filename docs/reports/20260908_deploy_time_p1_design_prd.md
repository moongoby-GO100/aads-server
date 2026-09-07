# AADS Deploy Time P1 Optimization Design and PRD

작성 시각: 2026-09-08 04:49 KST  
대상 프로젝트: AADS backend (`/root/aads/aads-server`)  
문서 목적: Dockerfile P1 튜닝 이후에도 남은 blue-green 전체 배포시간 병목을 줄이기 위한 설계, 기술스택, PRD, 구현 지시 기준 정리

## 1. 요약

Dockerfile P1 튜닝으로 운영 이미지 크기와 build 단계는 줄었지만, 전체 배포시간의 최대 병목은 아직 `standby_same_digest_sync`이다. 최근 성공 배포 5건 기준 평균 `standby_same_digest_sync`는 `571초`, 평균 `build_candidate_image`는 `517초`이며, 최신 성공 run `137`은 전체 `1,404초` 중 `standby_same_digest_sync 678초`, `build_candidate_image 275초`, `p0p1_monitoring 309초`가 차지했다.

따라서 추가 단축 P1의 핵심은 5분 P0/P1 모니터링을 줄이는 것이 아니라, 다음 세 가지를 안전하게 줄이는 것이다.

1. inactive 슬롯에 남은 오래된 SSE/복구 실행을 정확히 분류한다.
2. standby sync 전 stream drain 대기를 bounded lease 기반으로 줄인다.
3. 배포 원장에 stream drain 원인을 남겨 다음 배포 전에 자동 정리되게 한다.

## 2. 현재 기준선

### 2.1 운영 상태

| 항목 | 실측값 | 출처 |
|---|---:|---|
| 현재 active/standby image | `aads-server:9bc830c97dd3` | `docker ps`, 2026-09-08 04:49 KST |
| API 슬롯 상태 | `aads-server`, `aads-server-green` 모두 healthy | `docker ps`, 2026-09-08 04:49 KST |
| Docker image total | 36.81GB | `docker system df`, 2026-09-08 04:50 KST |
| Docker build cache | 22.98GB | `docker system df`, 2026-09-08 04:50 KST |
| 실행 중 chat execution | 5건 | `chat_turn_executions`, 2026-09-08 04:50 KST |
| `aads-server-green` 소유 실행 | 5건 | `chat_turn_executions`, 2026-09-08 04:50 KST |
| hidden streaming placeholder | 4건 | `chat_messages`, 2026-09-08 04:50 KST |

### 2.2 최근 성공 배포 단계별 시간

최근 성공 배포 5건의 평균 단계 시간이다.

| phase | 표본 | 평균 | 최대 | 최소 | 판정 |
|---|---:|---:|---:|---:|---|
| `standby_same_digest_sync` | 5 | 571초 | 836초 | 227초 | 최대 병목 |
| `build_candidate_image` | 5 | 517초 | 719초 | 275초 | Docker P1 이후 개선 중 |
| `p0p1_monitoring` | 2 | 311초 | 312초 | 309초 | 안전 규칙상 유지 |
| `active_slot_drain` | 5 | 65초 | 66초 | 65초 | 고정 대기성 병목 |
| `candidate_health` | 5 | 26초 | 35초 | 22초 | 허용 |
| `code_validation` | 5 | 17초 | 20초 | 14초 | 허용 |
| `nginx_cutover` | 5 | 3초 | 4초 | 3초 | 허용 |

최신 성공 run `137`의 상세값은 다음과 같다.

| phase | duration |
|---|---:|
| `standby_same_digest_sync` | 678초 |
| `p0p1_monitoring` | 309초 |
| `build_candidate_image` | 275초 |
| `active_slot_drain` | 65초 |
| `candidate_health` | 35초 |
| `code_validation` | 17초 |

## 3. 현재 아키텍처

### 3.1 배포 흐름

현재 `deploy.sh bluegreen`은 다음 순서로 동작한다.

1. release worktree gate, dependency lock, context/image size preflight 수행
2. 현재 active port와 candidate port 결정
3. candidate slot의 active stream을 확인하고 필요 시 drain 대기
4. release image를 1회 build
5. candidate slot을 `--no-build --force-recreate`로 기동
6. candidate direct health 통과 후 nginx upstream 전환
7. routed health 확인
8. 이전 active slot을 inactive standby로 전환하고 같은 digest로 동기화
9. DB/schema/chat/LLM/frontend QA 수행
10. 5분 P0/P1 모니터링 통과 후 `success/completed`로 완료

이 계약은 `/root/aads/AGENTS.md`의 필수 릴리스 규칙이므로 완화하지 않는다.

### 3.2 stream drain 관련 현재 구현

| 기능 | 현재 구현 | 위치 |
|---|---|---|
| stream count | `chat_turn_executions`에서 `status IN ('running','retrying')`, `owner_instance`, lease/heartbeat 조건으로 count | `deploy.sh:1225` |
| hidden recovery 제외 | `error_message='recovery_auto_retry_scheduled'` + hidden placeholder + 긴 assistant 응답 없음이면 count 제외 | `deploy.sh:1243` |
| inactive target recovery 정리 | hidden recovery placeholder 실행을 `cancelled` 처리 | `deploy.sh:1266` |
| active slot drain | cutover 전 최대 60초 대기 후 nginx graceful reload로 진행 | `deploy.sh:1903` |
| standby sync drain | cutover 후 inactive old slot active stream이 0이 될 때까지 최대 1,800초 대기 | `deploy.sh:1477` |
| same digest 검증 | standby recreate 후 active/standby Docker image digest 비교 | `deploy.sh:1533` |

## 4. 문제 정의

### 4.1 사용자 문제

| 문제 | 영향 |
|---|---|
| 배포 완료 보고가 20분 이상 지연 | CEO가 구현 테스트 가능 여부를 늦게 판단 |
| standby sync 대기 중 응답 중단/재개 반복 | 채팅 세션에서 완료 보고 유실 가능성 증가 |
| 오래된 실행이 active stream으로 잡힘 | 실제 사용자 보호가 아니라 stale lease 때문에 배포가 지연 |
| 배포 큐 중복/대기 증가 | 후속 커밋이 queued 상태로 밀림 |

### 4.2 기술 문제

| 원인 | 근거 | 개선 방향 |
|---|---|---|
| inactive slot 실행 lease 판정이 보수적 | `lease_expires_at IS NULL`도 active로 count | lease null/heartbeat age/owner epoch 조합으로 세분화 |
| hidden placeholder 정리 조건이 좁음 | hidden placeholder 4건, green 소유 running 5건 | stale placeholder/recovery execution classifier 도입 |
| standby sync 최대 대기가 큼 | 기본 `AADS_DEPLOY_STANDBY_SYNC_MAX_WAIT=1800` | stale 분류 후 정상 실행만 보호 |
| active slot drain이 고정 60초에 가까움 | 최근 5건 평균 65초 | zero sample 기반 조기 종료 및 worker reload 관측 개선 |
| phase event metadata 활용 부족 | `deploy_phase_events.metadata` 존재 | active stream sample, stale count, cancelled count 기록 |

## 5. 목표

### 5.1 정량 목표

| 지표 | 현재 기준 | 목표 | 측정 방식 |
|---|---:|---:|---|
| full blue-green success duration | 최신 run `137` 1,404초 | 900초 이하 | `deploy_runs.duration_ms` |
| standby sync 평균 | 최근 성공 5건 평균 571초 | 180초 이하 | `deploy_phase_events` |
| active slot drain 평균 | 최근 성공 5건 평균 65초 | 20초 이하 | `deploy_phase_events` |
| build 단계 | Docker P1 후 최신 275초 | 300초 이하 유지 | `deploy_phase_events` |
| P0/P1 monitoring | 309초 | 300초대 유지 | 안전 규칙상 축소 금지 |
| stale execution 자동 분류 정확도 | 미측정 | 오분류 0건 | unit/integration test + deploy dry-run |

### 5.2 비목표

| 제외 항목 | 이유 |
|---|---|
| 5분 P0/P1 모니터링 축소 | release certified 조건 위반 |
| active API 직접 재시작 | SSE 끊김 및 운영 규칙 위반 |
| 활성 사용자 스트림 강제 종료 | 사용자 응답 유실 위험 |
| `DROP/TRUNCATE` 또는 대량 삭제 | 운영 DB 파괴 작업 금지 |
| full compose stack 재배포 | app 변경 배포 원칙 위반 |

## 6. 기술스택

### 6.1 유지 기술

| 계층 | 기술 | 역할 |
|---|---|---|
| Deploy orchestration | Bash `deploy.sh` | blue-green 순서, nginx cutover, standby sync |
| Container | Docker Compose | candidate/standby slot `--no-build` 기동 |
| Routing | nginx upstream | 짧은 cutover lock으로 active slot 전환 |
| Database | PostgreSQL 15 | `deploy_runs`, `deploy_phase_events`, `chat_turn_executions` 원장 |
| API | FastAPI | health, active streams, graceful shutdown |
| Process | supervisord | container 내부 API/MCP 프로세스 제어 |
| Test | pytest, shell contract tests | 배포 스크립트 guard 검증 |

### 6.2 신규/보강 기술

| 기술 | 용도 | 적용 방식 |
|---|---|---|
| stale stream classifier | inactive slot의 안전 종료 가능 실행 판정 | Python 스크립트 또는 SQL CTE |
| lease policy constants | heartbeat/lease/placeholder TTL 표준화 | `deploy.sh` env default + 테스트 |
| deploy metadata writer | drain sample 기록 | `deploy_phase_events.metadata` JSONB |
| safe reconcile dry-run | 실제 cancel 전 영향 확인 | `--dry-run`, `--apply` 분리 |
| queue-aware preflight | 이미 running deploy와 latest SHA 큐 정리 | 기존 queue worker 보강 |
| unit contract tests | 배포 스크립트 안전 조건 고정 | `tests/unit/test_deploy_*` |

## 7. 제안 아키텍처

### 7.1 Target flow

```text
deploy.sh bluegreen
  |
  +-- preflight
  |     +-- dirty/lock/context/image gate
  |     +-- deploy queue reconcile
  |
  +-- target slot preflight
  |     +-- classify streams on candidate slot
  |     +-- cancel only stale hidden recovery executions
  |     +-- block if real active streams remain
  |
  +-- build/start candidate
  |
  +-- candidate health
  |
  +-- cutover with short nginx lock
  |
  +-- standby sync preflight
  |     +-- classify old active slot streams
  |     +-- graceful-shutdown inactive owner
  |     +-- bounded drain wait for real streams only
  |     +-- recreate old slot from same release image
  |
  +-- release certification
        +-- external health
        +-- same digest
        +-- QA
        +-- 5 min P0/P1 monitoring
```

### 7.2 stream 상태 분류

| 분류 | 조건 | 배포 처리 |
|---|---|---|
| `live_user_stream` | heartbeat fresh, lease valid, visible assistant delta 또는 current session execution | 보호, drain 대기 |
| `live_tool_wait` | heartbeat fresh, tool/model wait 중, placeholder visible | 보호, drain 대기 |
| `stale_placeholder` | hidden placeholder만 있고 assistant content 없음, heartbeat expired | cancel 가능 |
| `stale_recovery_retry` | `recovery_auto_retry_scheduled`, hidden placeholder, 긴 assistant 응답 없음 | cancel 가능 |
| `orphan_owner` | owner_instance가 inactive slot이고 owner_epoch/lease가 현재 slot marker와 불일치 | cancel 또는 owner transfer 후보 |
| `unknown` | DB/stream API 판정 실패 | fail closed, 배포 차단 또는 보수적 대기 |

### 7.3 DB/원장 설계

기존 테이블을 우선 사용한다.

| 테이블 | 활용 |
|---|---|
| `chat_turn_executions` | 실행 상태, owner_instance, owner_epoch, heartbeat_at, lease_expires_at 기준 분류 |
| `chat_messages` | hidden placeholder, assistant content 길이, final response 존재 여부 확인 |
| `deploy_runs` | 전체 배포 상태, phase, duration, queue 상태 |
| `deploy_phase_events` | phase별 duration과 `metadata` JSONB에 stream sample 기록 |

신규 테이블은 P1 필수는 아니다. 단, 추후 감사 추적을 강화하려면 `deploy_stream_reconcile_events`를 P2로 추가한다.

```sql
CREATE TABLE deploy_stream_reconcile_events (
  id BIGSERIAL PRIMARY KEY,
  deploy_run_id BIGINT REFERENCES deploy_runs(id),
  execution_id UUID,
  owner_instance TEXT,
  classification TEXT NOT NULL,
  action TEXT NOT NULL,
  reason TEXT,
  before_status TEXT,
  after_status TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

P1에서는 이 테이블 없이 `deploy_phase_events.metadata`에 아래 형태로 남긴다.

```json
{
  "stream_samples": [
    {"port": 8102, "owner_instance": "aads-server-green", "live": 0, "stale": 4, "unknown": 0}
  ],
  "reconciled": {"stale_placeholder": 4, "stale_recovery_retry": 0},
  "drain_wait_seconds": 15
}
```

## 8. PRD

### 8.1 제품명

AADS Deploy Time Optimizer P1

### 8.2 사용자

| 사용자 | 요구 |
|---|---|
| CEO | 배포 완료 여부를 15분 안에 판단하고, 지연 원인을 phase별로 확인 |
| CTO/Ops | stream 보호와 stale 정리를 구분해 안전하게 배포 |
| Pipeline Runner | 배포 큐가 오래된 lock/standby sync 때문에 장시간 적체되지 않음 |
| 개발자 | 작은 backend 변경 후 운영 반영까지의 피드백 시간이 줄어듦 |

### 8.3 사용자 스토리

1. CEO로서 배포가 늦어질 때 `build`, `standby sync`, `monitoring` 중 어디서 막혔는지 즉시 알고 싶다.
2. 운영자로서 실제 사용자 응답 스트림은 보호하되, hidden placeholder만 남은 stale 실행 때문에 standby sync가 10분 이상 지연되는 일을 막고 싶다.
3. 러너로서 새 배포 요청이 이미 끝난 이전 배포 lock 때문에 대기하지 않도록 stale run을 자동 reconcile하고 싶다.
4. 개발자로서 배포 스크립트 수정이 release 안전 계약을 깨지 않았다는 테스트를 갖고 싶다.

### 8.4 기능 요구사항

| ID | 요구사항 | 우선순위 | 완료 기준 |
|---|---|---|---|
| P1-D01 | stream classifier 구현 | P1 | `live/stale/unknown` 분류 unit test 통과 |
| P1-D02 | standby sync 전 stale execution dry-run | P1 | deploy log와 phase metadata에 stale 후보 수 기록 |
| P1-D03 | stale hidden placeholder apply 정리 | P1 | 조건 충족 execution만 `cancelled` 처리, final content 있는 메시지는 보존 |
| P1-D04 | active stream count를 live stream 기준으로 전환 | P1 | `stream_count_for_port`가 stale placeholder를 count하지 않음 |
| P1-D05 | standby drain max 기본값 축소 | P1 | live stream 없을 때 standby sync가 180초 이내 완료 |
| P1-D06 | phase metadata 기록 | P1 | `deploy_phase_events.metadata`에 sample/reconcile/wait 기록 |
| P1-D07 | stale run/queue reconcile 보강 | P1 | 종료된 deploy PID는 failed/superseded로 닫히고 최신 queued SHA만 유지 |
| P1-D08 | 배포 리포트 요약 API 보강 | P2 | latest deploy status에 phase별 병목과 stale 정리 결과 표시 |
| P1-D09 | dashboard 표시 | P2 | Ops 화면에서 배포 병목/예상 남은 시간 확인 |

### 8.5 비기능 요구사항

| 항목 | 요구 |
|---|---|
| 안전성 | `unknown` 분류는 자동 cancel 금지 |
| 무중단성 | nginx lock은 cutover 구간에만 보유 |
| 감사성 | 자동 cancel된 execution id와 사유를 metadata 또는 감사 테이블에 기록 |
| 결정성 | env 기본값과 테스트로 drain TTL을 고정 |
| 호환성 | `bash deploy.sh bluegreen` 호출 방식 유지 |
| 롤백 | 스크립트 revert 또는 env로 stale apply 비활성화 가능 |

### 8.6 정책 요구사항

| 정책 | 기본값 | 설명 |
|---|---:|---|
| `AADS_DEPLOY_STALE_STREAM_RECONCILE` | `dry-run` | 첫 배포는 기록만 수행 |
| `AADS_DEPLOY_STALE_STREAM_APPLY` | `false` | 검증 전 자동 cancel 금지 |
| `AADS_DEPLOY_STALE_HEARTBEAT_TTL_SECONDS` | `90` | heartbeat가 이보다 오래되면 stale 후보 |
| `AADS_DEPLOY_STANDBY_SYNC_MAX_WAIT` | `600` | P1 1차 목표. apply 안정화 후 `180`으로 축소 |
| `AADS_DEPLOY_STANDBY_ZERO_SAMPLES` | `2` | 일시적 0건 오판 방지 |

## 9. 구현 계획

### Phase 1: 관측/분류만 추가

| 작업 | 파일 | 검증 |
|---|---|---|
| stream classifier SQL/Python 함수 추가 | `scripts/classify_deploy_streams.py` 또는 `deploy.sh` 함수 | dry-run 출력 test |
| phase metadata writer 추가 | `deploy.sh` | `deploy_phase_events.metadata` insert/update 확인 |
| unit contract test 추가 | `tests/unit/test_deploy_stream_reconcile.py` | pytest |

예상 효과: 배포 시간 자체는 크게 줄지 않지만, stale/live/unknown 원인을 다음 배포부터 확정할 수 있다.

### Phase 2: safe apply

| 작업 | 파일 | 검증 |
|---|---|---|
| stale hidden placeholder cancel 적용 | `deploy.sh`, helper script | final content 보존 test |
| target/standby stream count를 classifier 기반으로 변경 | `deploy.sh` | live stream count mock test |
| standby sync max wait 600초로 축소 | `deploy.sh` env default | staged deploy smoke |

예상 효과: stale placeholder가 원인인 standby sync를 10분대에서 3분 이하로 단축.

### Phase 3: Ops 가시화

| 작업 | 파일 | 검증 |
|---|---|---|
| deploy bottleneck summary API | `app/api` 또는 기존 ops router | API 200 + schema test |
| dashboard deploy status 표시 | `aads-dashboard` | browser screenshot |
| queue worker 상태 노출 | backend + dashboard | queued/run 상태 E2E |

예상 효과: CEO가 배포 지연 원인을 채팅 보고 없이도 화면에서 즉시 확인.

## 10. 테스트 계획

| 테스트 | 목적 | 명령/방법 |
|---|---|---|
| classifier unit | stale/live/unknown 분류 정확도 | `pytest tests/unit/test_deploy_stream_reconcile.py -q` |
| deploy script contract | nginx lock, same digest, monitoring 계약 유지 | `pytest tests/unit/test_deploy_script_guards.py -q` |
| DB metadata test | `deploy_phase_events.metadata` 기록 | local Postgres 또는 mock SQL |
| no final loss test | assistant final content가 있는 execution cancel 금지 | fixture 기반 unit |
| dry-run deploy | 자동 cancel 없이 후보 수만 기록 | `AADS_DEPLOY_STALE_STREAM_RECONCILE=dry-run bash deploy.sh bluegreen` |
| staged apply deploy | stale 후보만 cancel 후 standby sync 단축 | apply env + blue-green |

## 11. 롤백 계획

| 상황 | 롤백 |
|---|---|
| live stream 오분류 발견 | `AADS_DEPLOY_STALE_STREAM_APPLY=false`로 즉시 비활성화 |
| standby sync 실패 증가 | `AADS_DEPLOY_STANDBY_SYNC_MAX_WAIT=1800` 복귀 |
| metadata SQL 오류 | metadata update를 best-effort로 변경하고 배포 차단에서 제외 |
| API/UI 표시 오류 | backend deploy tooling은 유지, dashboard만 이전 버전으로 재배포 |

## 12. 완료 기준

P1 완료는 다음 조건을 모두 만족해야 한다.

| 조건 | 기준 |
|---|---|
| 코드 | classifier, deploy metadata, safe reconcile 구현 |
| 테스트 | 관련 unit/contract test 통과 |
| 배포 | `deploy.sh bluegreen` success/completed |
| 운영 검증 | 양 슬롯 same digest, `/api/v1/health=200`, P0/P1 5분 모니터링 통과 |
| 성능 | 성공 배포 2회 연속 full duration 900초 이하 또는 standby sync 180초 이하 |
| 안전 | 자동 cancel된 execution 중 final assistant content 손실 0건 |

## 13. 작업 지시서 초안

```text
>>>DIRECTIVE_START
TASK_ID: AADS-DEPLOY-TIME-P1
TITLE: AADS blue-green 배포시간 P1 단축 - standby stream lease/reconcile 최적화
PRIORITY: P1-HIGH
SIZE: M
MODEL: auto
DESCRIPTION:
Dockerfile P1 튜닝 이후 남은 전체 배포시간 병목을 줄인다.
대상은 deploy.sh의 target/active/standby stream drain, chat_turn_executions lease 분류, deploy_phase_events.metadata 기록이다.
절대 5분 P0/P1 모니터링을 줄이지 말고, active API 직접 재시작/전체 compose deploy/DROP/TRUNCATE/시크릿 노출을 금지한다.

구현 요구:
1. inactive slot stream classifier를 추가해 live_user_stream/live_tool_wait/stale_placeholder/stale_recovery_retry/orphan_owner/unknown으로 분류한다.
2. hidden placeholder 또는 recovery_auto_retry_scheduled 조건의 stale 실행만 safe cancel 대상으로 삼고, assistant final content가 있으면 cancel 금지한다.
3. standby_same_digest_sync 전후 stream sample, stale 후보 수, cancel 수, drain wait seconds를 deploy_phase_events.metadata에 기록한다.
4. 초기 기본값은 dry-run으로 두고, apply env가 true일 때만 cancel한다.
5. standby sync max wait는 safe apply 안정화 전 600초, 안정화 후 180초로 줄일 수 있게 env default를 분리한다.
6. unit/contract tests를 추가하고, blue-green release contract가 유지되는지 검증한다.
7. HANDOVER.md에 구현/검증/배포 상태를 기록한다.

검증:
- pytest tests/unit/test_deploy_stream_reconcile.py -q
- pytest tests/unit/test_deploy_script_guards.py -q
- bash -n deploy.sh
- 운영 반영 시 bash deploy.sh bluegreen 후 deploy_runs success/completed, same digest, /api/v1/health=200, P0/P1 300초 모니터링 확인
>>>DIRECTIVE_END
```

## 14. 결론

추가 단축안은 Dockerfile보다 배포 오케스트레이션 쪽의 효과가 크다. 현재 안전 규칙상 5분 P0/P1 모니터링은 유지해야 하므로, 실질 단축 대상은 `standby_same_digest_sync`와 `active_slot_drain`이다. P1 구현은 먼저 dry-run 관측으로 오분류 위험을 제거하고, 그 다음 stale placeholder/recovery 실행만 자동 정리해 standby sync를 180초 이하로 낮추는 순서가 맞다.
