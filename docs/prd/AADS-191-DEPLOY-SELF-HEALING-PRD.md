# AADS-191 — 배포 자가치유(Self-Healing Deploy) 설계 / PRD

- 작성: 2026-09-13 07:30 KST
- 상태: v1 구현 완료(P0), P1·P2 로드맵 유지
- 대상: `/root/aads/aads-server/deploy.sh`, `scripts/deploy_autoheal.sh`, `deploy_runs`
- 관련 규칙: R-DOCKER(단일 서비스 재시작), R-COMMIT(--no-verify 금지), CEO-DIRECTIVES

---

## 1. 배경 — 왜 지금인가

배포가 실패하면 파이프라인이 그 자리에서 **멈추고 끝난다**. 사람이 로그를 열어
원인을 읽고 손으로 조치한 뒤 다시 돌려야만 진행된다.

### 1.1 실측 근거 [DB 조회 `deploy_runs`, 2026-09-13 07:20~07:27 KST]

| 항목 | 실측값 |
|------|--------|
| 최근 10일 배포 | 성공 59건 / 실패·차단 241건 / 전체 364건 (성공률 16.2%) |
| 14일 실패·차단 원인 1위 | `interrupted by TERM` 75건 |
| 2위 | `dirty worktree blocks release` 55건 |
| 3위 | `heartbeat exceeded 1200s`(stale) 48건 |
| 4위 | 분류 미매칭 32건 |
| 5위 | `standby same-digest sync failed` 14건 |
| 6위 | `unexpected error exit=` 10건 |
| 7위 | `insufficient build disk` 6건 |

### 1.2 구조적 원인 3가지

1. **실패 = 파이프라인 종료.** `start_deploy_queue_worker` 호출이 성공 경로
   (`deploy.sh:2565 post_success`)와 락 대기 경로(1357·1376)에만 있고, 실패/차단
   종료 경로(`cleanup_deploy` EXIT 트랩)에는 없었다. 큐에 다음 릴리스가 있어도
   이어받는 주체가 없다.
2. **자가치유 엔진과 배포가 분리.** `app/services/unified_healer.py`는
   `disk_space_critical → docker system prune -f` 같은 매핑을 갖지만 **배포 실패
   이벤트를 입력으로 받지 않는다.**
3. **교정이 원인에 못 미침.** `prune_old_release_images`는 이미지만 정리한다.
   실측상 이미지 회수 가능량 4.456GB vs **빌드 캐시 22.28GB**. 그래서 디스크
   임계(기본 20GB) 미달이 반복된다.

### 1.3 재현된 실패 (설계 근거)

2026-09-13 07:25 KST, `deploy_runs#366` 성공 직후 큐 워커가 `#367`을 집었으나
`preflight / insufficient build disk`로 차단되고 **그대로 종료**됐다. 이후 아무도
이어받지 않았다.

---

## 2. 목표 / 비목표

### 목표 (v1)
- G1. 배포 실패 시 **원인을 코드로 분류**한다(추측 아님, `deploy_runs.error_summary` 기반).
- G2. 원인별 **자동 교정**을 수행한다(디스크 회수, clean worktree 우회 등).
- G3. 교정에 성공하면 **사람 개입 없이 재개**한다.
- G4. 자동 복구가 불가능한 원인은 **즉시 CEO 에스컬레이션**하고 조용히 끝내지 않는다.
- G5. 어떤 경우에도 **무한 재시도 루프를 만들지 않는다.**

### 비목표 (v1에서 하지 않음)
- N1. 작업 트리 자동 정리(stash/clean/checkout) — CEO·러너의 미커밋 작업을 삼킬 수 있다. **영구 금지.**
- N2. 컷오버 이후 중단의 자동 전체 재배포 — 서비스는 이미 새 슬롯에서 살아 있다.
- N3. 단계 재개(`--resume-from=<phase>`) — P1으로 분리.
- N4. 코드 자체가 틀린 실패(build/test fail)의 자동 코드 수정 — Pipeline Runner 영역.

---

## 3. 아키텍처

```
deploy.sh 실패 (exit != 0)
        │
        ▼
cleanup_deploy (EXIT 트랩)          ← 하트비트 정지 · 릴리스 컨텍스트 정리
        │  ① 배포 락(flock) 해제 완료 후에만 아래로 진행
        ▼
deploy_autoheal_on_exit(rc)         ← scripts/deploy_autoheal.sh
        │
        ├─ 1. classify_deploy_failure(phase, error_summary) → cause
        ├─ 2. autoheal_policy(cause) → retry | manual
        ├─ 3. 예산·쿨다운 검사 (SHA×cause 1회 / 180초)
        ├─ 4. remediate_deploy_failure(cause) → 실제 교정
        ├─ 5. queue_autoheal_retry_request(cause) → deploy_runs 재큐
        └─ 6. launch_autoheal_worker(cause)
                 → scripts/start_aads_deploy_queue_worker.sh
                    → clean detached worktree 에서 deploy.sh 재실행
```

**핵심 설계 선택**: 재개를 "deploy.sh 재호출"이 아니라 **"큐 등록 + 큐 워커 기동"**
으로 구현했다. 큐 워커는 이미 커밋된 SHA의 clean detached worktree에서 돌고
dirty 게이트를 건너뛴다(`deploy.sh:1447`). 따라서 이 경로 하나로
`dirty_worktree` 원인까지 함께 해소된다.

---

## 4. 원인 코드 · 정책 · 교정 매핑 (구현 사양)

| 원인 코드 | 분류 키워드 | 정책 | 자동 교정 내용 |
|-----------|-------------|------|----------------|
| `disk_full` | insufficient build disk / no space left | retry | release image retention + `docker image prune -f` + `docker builder prune -f --filter until=48h` → 임계 재검사 |
| `dirty_worktree` | dirty worktree | retry | **작업 트리 불변.** 큐 워커의 clean worktree 경로로 우회 |
| `stale_heartbeat` | heartbeat exceeded / stale deploy | retry | reconcile은 기존 로직이 수행 → 재개만 |
| `standby_sync_fail` | standby same-digest sync | retry | 동일 릴리스 재기동으로 standby 슬롯만 정렬 |
| `lock_wait_timeout` | queued deploy wait timeout / flock | retry | 재개만 |
| `signal_interrupt` | `deploy interrupted by` (TERM/INT/HUP/QUIT 공통) | 조건부 | 컷오버 **전**이면 retry, **후**면 manual. 컷오버 판정은 `DEPLOY_UPSTREAM_SWITCHED` + phase 이름(`autoheal_phase_is_post_switch`) 2중 확인 |
| `mem_limit_mismatch` | memory limit mismatch | manual | 에스컬레이션 |
| `release_context_too_large` | release context too large | manual | 에스컬레이션 |
| `dependency_lock_stale` | dependency lock | manual | 에스컬레이션 |
| `unexpected_exit` | unexpected error exit= | manual | 에스컬레이션 |
| `other` / `unknown` | 그 외 | manual | 에스컬레이션 |

---

## 5. 안전장치 (폭주 방지)

| # | 장치 | 기본값 | 환경변수 |
|---|------|--------|----------|
| 1 | 전체 킬스위치 | 활성(1) | `AADS_DEPLOY_AUTOHEAL=0`이면 기존 동작 |
| 2 | 재시도 예산 | SHA×원인당 1회 | `AADS_DEPLOY_AUTOHEAL_MAX_ATTEMPTS` |
| 3 | 기동 쿨다운 | 180초 | `AADS_DEPLOY_AUTOHEAL_COOLDOWN_SEC` |
| 4 | 화이트리스트 | retry 정책 원인만 | (코드) |
| 5 | 락 해제 후 기동 | 필수 | (코드) |
| 6 | 드라이런 | 0 | `AADS_DEPLOY_AUTOHEAL_DRYRUN=1` |
| 7 | 중복 큐 차단 | 동일 SHA가 queued/running/verifying/syncing이면 재등록 안 함 | (SQL) |
| 8 | 빌드 캐시 회수 범위 | 48시간 이전만 | `AADS_DEPLOY_AUTOHEAL_BUILDER_PRUNE_UNTIL` |

교정 후에도 임계를 못 넘기면(예: 디스크 회수 실패) **재개하지 않고 에스컬레이션**한다.
같은 곳에서 다시 막힐 것이 확실한 재시도는 자원 낭비이자 로그 오염이다.

---

## 6. 관측 · 감사

- `audit_control "autoheal" ...` → `/var/log/aads-control-audit.jsonl`
  - `classified`(원인·정책·시도횟수) / `retry_launched` / `escalated`
- `deploy_runs` 재큐 row: `requested_by='deploy.sh_autoheal'`,
  `request_source='autoheal_<cause>'`, `error_summary='autoheal retry: cause=...; remediation=...'`
- 텔레그램: 재개 시 🔄, 자동 복구 실패 시 ❌ (`notify()` 재사용, 미정의 시 건너뜀)

조회 예시:
```sql
SELECT id, status, phase, request_source, error_summary, created_at
FROM deploy_runs
WHERE requested_by = 'deploy.sh_autoheal'
ORDER BY id DESC LIMIT 20;
```

---

## 7. 검증 기준 (Acceptance)

| # | 기준 | 검증 방법 |
|---|------|-----------|
| A1 | 구문 무결성 | `bash -n deploy.sh`, `bash -n scripts/deploy_autoheal.sh` |
| A2 | 분류 정확도 | 실제 `error_summary` 문자열 표본으로 단위 테스트 전건 통과 |
| A3 | 정책 정확도 | 컷오버 전/후 `signal_interrupt` 분기 테스트 |
| A4 | 루프 차단 | 예산 소진 시 재개하지 않고 escalated 기록 |
| A5 | 실패 후 재개 | 실패 배포 1건에서 autoheal이 원인 분류 → 교정 → 큐 워커 기동 → 배포 성공 |
| A6 | 무해성 | `AADS_DEPLOY_AUTOHEAL=0`이면 기존과 동일하게 종료 |

---

## 8. 롤백

1. `AADS_DEPLOY_AUTOHEAL=0` 환경변수만으로 즉시 비활성(코드 되돌리기 불필요).
2. 완전 제거: `git revert <commit>` — `deploy.sh` 2개 훅(source 1곳, cleanup_deploy 1곳,
   record_deploy 1곳)과 신규 파일 1개만 되돌리면 된다.
3. 라이브러리 파일이 없으면 deploy.sh는 경고만 출력하고 기존 동작으로 진행한다(fail open).

---

## 9. 로드맵

| 단계 | 내용 | 상태 |
|------|------|------|
| P0-1 | 실패 시 큐 재구동 | ✅ v1 구현 |
| P0-2 | 원인 분류기 + 원인별 자동 교정 | ✅ v1 구현 |
| P0-3 | 단계 재개 `--resume-from=<phase>` (컷오버 이후 인증 단계만) | 미착수 |
| P1-1 | `deploy_runs`에 `attempt`/`remediation_applied` 컬럼 정규화 | 미착수 |
| P1-2 | 동일 원인 3연속 실패 시 서킷브레이커 + 대시보드 노출 | 미착수 |
| P2-1 | 빌드 캐시 상한 상시화(`keep-storage`) 및 디스크 예측 경보 | 미착수 |
| P2-2 | `unified_healer`와 이벤트 통합(배포 실패를 error_log로 승격) | 미착수 |

---

## 10. 개정 이력

### v1.1 (2026-09-13 07:50 KST) — 실패 주입 검증 후 보완

| # | 변경 | 사유 |
|---|------|------|
| 1 | 큐 등록 실패 시 워커를 기동하지 않고 에스컬레이션 | 기존 코드는 `queue_autoheal_retry_request \|\| true` 라서 DB 불가로 큐 등록이 안 돼도 워커를 띄우고 "재개 기동 완료"를 찍었다. 워커가 집을 릴리스가 없어 실패를 은폐한다. |
| 2 | 회귀 테스트 3건 추가 | 큐 등록 실패→차단, 큐 등록 성공→재개, 작업 트리 변경 명령(`git stash/checkout/clean/reset`) 금지 정적 검사 |

### 검증 실적 (v1.1)

- 단위 테스트: `pytest -q tests/unit/test_deploy_autoheal.py` → **28 passed**
- 실패 주입(DRYRUN, 운영 DB·도커 무영향) 10종 전부 설계대로 동작:
  disk_full / dirty_worktree / standby_sync_fail / signal(컷오버 전) → 재개,
  signal(컷오버 후) / mem_limit_mismatch / 재시도 예산 소진 / 교정 실패 → 에스컬레이션,
  rc=0 → 무동작, 킬스위치 → 무동작
- 구문: `bash -n scripts/deploy_autoheal.sh`, `bash -n deploy.sh` 통과

### 알려진 한계 (v1.1)

`cleanup_deploy` EXIT 트랩은 `deploy.sh` 락 획득 이후에 설치된다. 따라서 그 이전에
종료되는 경로(`queued deploy wait timeout`, `deploy flock acquisition failed`)는
자가치유가 적용되지 않는다. `autoheal_policy` 의 `lock_wait_timeout` 분기는 현재
도달하지 않는 예약 경로이며, 해당 구간 보호는 P1 과제로 남긴다.
