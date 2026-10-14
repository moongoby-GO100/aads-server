# 목업 실행 게이트 enforce 전환 절차

- document_key: `aads-mockup-exec-gate-enforce-switch-runbook` (kind=runbook, 초안) — **정본 미등록(미완료)**. 이 작업 세션에는 정본 등록 도구가 없고 운영 DB 쓰기가 금지라 등록하지 못했다. 열람 링크 미제공.
- TASK_ID: AADS-RDOC-M8-MOCKUP-EXEC-GATE-20261014
- **enforce 전환은 CEO 승인 사항이다.** 이 문서는 절차만 적는다. M8·M8a 커밋은 설정 파일·컨테이너 env 를 바꾸지 않았고 기본값은 `shadow` 그대로다.

## 1. 모드가 하는 일 (M8a + M8 기준)

`MOCKUP_GATE_MODE` 하나가 서버(API)·Runner·CLI·pre-push 훅의 판정을 정한다. 규칙 원본은 `app/services/mockup_gate_rules.py` 다.
판정은 M8a 의 `mockup_gate_decisions` 테이블에 phase 별로 남는다(migration 운영 미적용 상태에서는 기록만 실패하고 판정은 그대로).

"멈춤" 열: submit / pre_execution / checkpoint 의 거절은 모드와 무관하게 작업을 멈춘다. chat_write / pre_push 의 거절은
enforce 일 때만 멈춘다(shadow 는 `allowed=false, blocking=false` 기록 + 경고). M8a 의 `stops_action` 규칙이다.

| 상황 | off | shadow (기본) | enforce |
|---|---|---|---|
| 백엔드 작업(번들 선언 없음, UI 아님) | 통과 | 통과·DB 접근 없음 | 통과·DB 접근 없음 |
| UI 작업인데 번들 선언 없음 | 통과 | 통과 + 로그 `mockup_gate_shadow` + 판정 기록 | **거절** `mockup_bundle_required` |
| 번들 선언 형식 오류(일부 누락·중복·형식) | 통과 | **거절** `bundle_*` | **거절** `bundle_*` |
| 선언한 번들의 변조·회수·새 수정본·범위 밖·다른 tenant/project | 통과 | **거절**(선언한 번들은 모드와 무관하게 검증한다) + `verify_denied` 감사 | **거절** |
| 게이트 DB 장애 — 번들 선언 없는 UI 작업 | 통과 | 통과 + 로그 `mockup_gate_unavailable` | **거절(fail-closed)** |
| 게이트 DB 장애 — 번들을 선언한 작업 | 통과 | **거절(fail-closed)** `gate_unavailable` | **거절(fail-closed)** |
| Runner 의 게이트 스크립트 자체 장애(rc≠0/10/20) | 통과 | 서버 규칙(`--classify-only`)이 비대상이면 통과, 대상·판정불가면 통과 + `MOCKUP_GATE_SHADOW violation=gate_script_error` | 비대상만 통과, 대상·판정불가는 **거절(fail-closed)** |

- 모드 값이 `off/shadow/enforce` 가 아니면(오타 포함) `enforce` 로 읽는다.
- UI 작업 판정은 지시서의 `TARGET` / `TARGET_FILES` / `UI_TASK` 메타 줄만 본다(`classify`). 본문 문구는 보지 않는다.
- 이미 running 인 작업은 어떤 모드에서도 kill/restart 하지 않는다. 승인이 바뀌면 바인딩이 `review_required`(실행 중) / `held`(미시작)로 바뀌고, checkpoint 검증이 `binding_held` 를 돌려줄 뿐이다.

## 2. 설정 위치 (모두 같은 값이어야 한다)

| 위치 | 대상 | 비고 |
|---|---|---|
| `/root/aads/aads-server/.env` (prod 는 `${AADS_RUNTIME_ENV_FILE:-.env}`) 의 `MOCKUP_GATE_MODE=` | API 의 submit / batch / `/pipeline/jobs/{id}/mockup-gate` 판정 | aads-server 서비스가 `env_file` 로 이 파일 전체를 받으므로 compose 수정은 필요 없다. 반영은 컨테이너 재생성(블루그린)이어야 한다 — `reload-api.sh` 는 env 를 다시 읽지 않는다. |
| 각 Runner 호스트 systemd drop-in (`/etc/systemd/system/aads-pipeline-runner.service.d/*.conf` 의 `Environment=MOCKUP_GATE_MODE=enforce`; 211·114 의 litellm runner 도 같음) | `scripts/pipeline-runner.sh` → `verify_mockup_approval.py` 의 pre_execution 판정, 스크립트 장애 시 판정 | 번들이 없는 UI 작업은 Runner 가 **자기 env** 로 판정한다. 서버만 enforce 로 올리면 이미 큐에 있던 작업은 Runner 에서 shadow 로 통과한다. |
| push 하는 셸의 env (`scripts/hooks/pre-push`, M8a) | 커밋별 pre_push 판정 | 훅은 실행한 셸의 `MOCKUP_GATE_MODE` 를 읽는다. Runner 호스트에서는 Runner env 와 같다. |
| `.github/workflows/ci.yml` 의 `mockup-gate` job `env.MOCKUP_GATE_MODE` | 오프라인 선언 검사 | CI 는 서버에 접속하지 않으므로 승인 여부를 판정하지 않는다. enforce 로 올려도 `continue-on-error: true` 를 지우기 전에는 경고만 낸다. |

## 3. 올리기 전 shadow 기록 검토

최소 7일 shadow 기록을 본다. 전제 조건(연속 7일 기록, 오차단 0건, M6/M7 실사용)은 M8a 검증 보고 §10 이 원본이다 — 여기에 다시 적지 않는다.
아래가 모두 "설명 가능"해야 올린다.

### 3.0 DB — phase 별 판정 기록 (M8a `mockup_gate_decisions`)

```sql
SELECT phase, mode, allowed, blocking, reasons, count(*)
  FROM mockup_gate_decisions
 WHERE decided_at > now() - interval '7 days'
 GROUP BY 1,2,3,4,5
 ORDER BY 6 DESC;
```

`mode='shadow' AND allowed=true AND shadow=true` 행이 enforce 였다면 거절됐을 작업이다.

### 3.1 DB — 번들을 선언했는데 검증에 실패한 작업

```sql
SELECT e.created_at, h.project_key, e.payload->>'task_id' AS task_id, e.payload->>'phase' AS phase,
       e.payload->'reasons' AS reasons
  FROM mockup_review_events e
  JOIN mockup_review_heads h ON h.id = e.head_id
 WHERE e.action = 'verify_denied'
   AND e.created_at > now() - interval '7 days'
 ORDER BY e.created_at DESC;
```

### 3.2 DB — 번들 선언 없이 제출된 UI 작업(enforce 였다면 거절됐을 작업)

```sql
SELECT job_id, project, status, created_at, left(instruction, 120) AS head
  FROM pipeline_jobs
 WHERE created_at > now() - interval '7 days'
   AND instruction !~ 'MOCKUP_REVIEW_ID'
   AND (instruction ~* '^\s*TARGET\s*[:=].*aads-dashboard'
        OR instruction ~* '^\s*UI_TASK\s*[:=]\s*(true|yes|1)'
        OR instruction ~* '^\s*TARGET_FILES\s*[:=].*\.(tsx|jsx|vue|svelte|css|scss|sass|less|html)')
 ORDER BY created_at DESC;
```

정규식은 근사치다(코드 블록 안의 줄도 잡는다). 최종 판정은 `python3 scripts/verify_mockup_approval.py --classify-only --instruction-file <파일>` (exit 11 = 적용 대상)로 한 건씩 확인한다.

### 3.3 로그

```bash
# API (활성 컨테이너)
docker logs --since 168h aads-server 2>&1 | grep -E 'mockup_gate_shadow|mockup_gate_unavailable'
# Runner 호스트마다
grep -E 'MOCKUP_GATE_SHADOW|MOCKUP_GATE job=' /var/log/aads-pipeline/runner.log | tail -200
```

`mockup_gate_unavailable`, `violation=gate_script_error` 가 한 건이라도 있으면 원인을 먼저 고친다 — enforce 에서는 그 작업이 전부 멈춘다.

## 4. 전환 순서

1. CEO 승인 기록(세션 ID)을 남긴다.
2. Runner 호스트 drop-in 에 `MOCKUP_GATE_MODE=enforce` 를 넣는다. Runner 재시작은 실행 중 작업이 없을 때 기존 runner 교체 절차로 한다(직접 kill 금지).
3. API: 런타임 env 파일에 `MOCKUP_GATE_MODE=enforce` 를 넣고 **점검 창구에서** `bash deploy.sh bluegreen` 으로 반영한다(`docker compose up -d` 전체 실행 금지, `docker-compose.yml` 수정 불필요).
4. 확인: `docker exec aads-server printenv MOCKUP_GATE_MODE` → `enforce`. 번들 없는 UI 테스트 지시를 하나 제출해 `409 mockup_approval_required` / `reasons=["mockup_bundle_required"]` 를 확인한다(배치 제출도 같은 409, `rejected` 에 key 목록).
5. (선택) CI 를 차단으로: `mockup-gate` job 의 `MOCKUP_GATE_MODE: enforce`, `continue-on-error` 제거. CI 는 승인 여부를 모르므로 enforce 에서도 "번들 선언 없는 UI 변경"과 "형식 오류 선언"만 막는다.

## 5. 롤백

- 값을 `shadow` 로 되돌린다(§2 의 모든 곳). 코드 되돌림은 필요 없다.
- 모든 곳을 되돌린다. 순서는 상관없다 — 되돌리는 방향은 느슨해지는 쪽이라 중간 상태에서 새로 막히는 작업은 없다.
- 거절된 작업은 자동 재시도되지 않는다. 필요한 작업만 다시 제출한다.
- `off` 는 게이트 기록까지 끈다. 장애 대응 중이 아니면 쓰지 않는다.

## 6. 서버가 강제하지 못하는 경로

채팅 직접 쓰기 도구 중 `write_remote_file` / `patch_remote_file` / `git_remote_commit` 은 M8a 에서 `phase=chat_write` 게이트를 탄다.
`run_remote_command` 로 하는 쓰기, 훅이 없는 클론·`--no-verify` push, 터미널에서 사람이·에이전트가 직접 고치는 경로는 이 게이트를 거치지 않는다.
enforce 로 올려도 이 경로는 막히지 않는다. 대응표: `docs/reports/20261014_AADS_목업_실행게이트_제출경로와_PRD_AC_대응표.md` §2, M8a 검증 보고 §9.
