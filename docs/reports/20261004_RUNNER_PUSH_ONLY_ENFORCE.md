# AADS-RUNNER-PUSH-ONLY-ENFORCE-20261004 — PUSH_ONLY·긴 한글 배포 금지 누락 차단

작성: 2026-10-04 · 상태: **수정안 작성 + 로컬 검증 완료, 미승인·미커밋·미배포** (commit/push/릴리스는 기존 검수·승인 경로)

## 1. 사고와 인과 교차검증

runner-9d5d8d45 의 원 지시문(`pipeline_jobs.instruction`, 2056자 위치)에는 아래 줄이 있었다.

    PUSH_ONLY. 빌드·배포·운영 migration 금지. no-verify/force push/전체 compose 금지.

그런데 잡은 `status=done`, `deployed_at=2026-10-04 07:54:07+09` 로 끝났다.

| 증거 | 내용 |
|---|---|
| DB `pipeline_jobs` | `runner-9d5d8d45 \| done \| done \| deployed_at 07:54:07+09`, instruction 에 `PUSH_ONLY` 존재 |
| `/var/log/aads-pipeline/runner.log` 60544~60556 | `BG_DEPLOY` → `GIT_PUSH_OK` → `BLUEGREEN aads-server` → `HEALTH_OK` → `DEPLOYED`. **`DEPLOY_SKIPPED_BY_DIRECTIVE` 줄 없음** — 게이트가 "제약 없음" 으로 판정 |
| 시각 대조 | 로그 시각은 호스트 로컬(CEST). 00:45:37 CEST = 07:45 KST 배포 시작, 00:54:08 CEST = 07:54 KST 배포 완료. `deployed_at` 및 audit.jsonl(`+02:00`) 과 일치 |
| 실행본 | systemd `ExecStart=/root/aads/aads-server/scripts/pipeline-runner.sh`. 공유 체크아웃의 미커밋 변경(ACCT 부팅 안전망, 93줄 추가)은 `instruction_forbids_deploy` 를 건드리지 않음 — diff 에 `forbid`/`PUSH_ONLY` 없음 |
| 함수 단독 재현(HEAD) | `PUSH_ONLY` → false, `PUSH_ONLY. 빌드·배포·운영 migration 금지.` → false, `빌드·배포 금지. push 까지만 수행` → true |

**원인(확인된 것만)**
1. `PUSH_ONLY` 구조화 선언을 읽는 코드가 없었다.
2. `(배포|재기동|…).{0,12}(금지|…)` — "배포"와 "금지" 사이를 12자로 제한. 사고 문장은 `·운영 migration ` 14자라 빗나갔다.
3. 부수 결함: 지시서 조회가 실패하거나 비어도 `|| job_instruction=""` 로 삼켜 "제약 없음 → 배포 허용" 이 됐다(이번 사고 원인은 아니며 같은 게이트의 미탐 경로).

## 2. STEP 0 — 기존 구현 분류 (`scripts/pipeline-runner.sh`)

| 대상 | 분류 | 비고 |
|---|---|---|
| `instruction_forbids_deploy` 기존 3규칙(12자 정규식, `커밋/push 까지만`, `do not/no deploy`) | **유지** | 한 글자도 안 바꿈. 이전에 막던 표현은 계속 막는다 |
| `instruction_forbids_deploy` 구조화·긴 문구 규칙 | **신규(추가)** | PUSH_ONLY / `DEPLOY_POLICY:` / `DEPLOY: false` / 같은 절 40자 간격 |
| `read_job_instruction_strict` | **신규** | 재조회 3회, 실패·공백 → rc 1 |
| deploy_job 게이트(조회 부분) | **수정** | 조회 실패 시 `deploy_directive_unverifiable` 로 중단 |
| deploy_job 게이트(skip 블록 `push_only_by_directive`) | **유지** | 기존 테스트가 고정 |
| 빌드 직전 불변식 | **신규** | 게이트가 이 잡에 "허용" 으로 끝났을 때만 빌드 |
| `pipeline-runner.sh.local` | **수정(동기화)** | `test_scripts_are_byte_identical` 이 두 파일의 바이트 동일을 요구해 TARGET_FILES 밖이지만 함께 갱신 |
| 삭제 | 없음 | 호출처 영향 없음 |

공유 체크아웃의 dirty 변경은 읽기만 했고 덮어쓰지 않았다(최신 origin 기준 격리 worktree 에서 작업).

## 3. 변경 내용

- **PUSH_ONLY**: 어디에 있든 토큰 경계가 맞으면 금지(`push_only`, `push-only`, `pushonly`). 줄 머리의 `PUSH ONLY` 도 금지. `PUSH_ONLY: false/no/0/off` 처럼 **명시적으로 끈 표기만** 제외. `push_only_by_directive` 같은 식별자는 경계 규칙으로 제외.
- **정책 헤더**: `DEPLOY_POLICY|DEPLOYMENT_POLICY|RELEASE_POLICY: push_only|commit_only|no_deploy|forbidden|deny|none|blocked`, `DEPLOY: false|no|off|forbidden|…`. `DEPLOY_ONLY: true` 는 해당 없음.
- **긴 한글 문구**: 같은 절(`. ! ? ; 。` 줄바꿈으로 분리) 안에서 트리거~금지어 사이 40자까지 허용. 간격에 `후/뒤/이후/다음/하되/하고/하며/하면/전에/먼저/then/after/before` 가 있으면 별개 문장으로 보고 통과 → `배포 후 로그를 확인하고 설정은 변경하지 마라` 는 계속 배포 허용.
- **fail-closed**: 승인 후 push 가 끝난 뒤 지시서를 재조회하되 실패·빈 값이면 `_fail_job(deploy_directive_unverifiable)` + 락 해제 + 다음 큐 승격, 빌드·배포 없음. push 는 이미 끝났으므로 재승인 필요.
- **빌드 직전 불변식**: `_deploy_directive_state="allowed:${job_id}"` 가 게이트 통과 후에만 세팅되고, `무중단 배포 v3.0` 구간 맨 앞에서 확인한다. 없거나 다른 잡이면 `deploy_directive_gate_bypassed` 로 중단. 롤백 배포도 이 뒤에만 있다.
- 정규식은 줄/절 단위 입력에만 적용하고 중첩 반복을 쓰지 않았다(R-BG).

의도적 편향: 애매하면 건너뛴다(기존 정책 유지). 지시문 본문이 `PUSH_ONLY` 를 산문으로 언급만 해도 push 까지만 수행된다 — 그 경우 사람이 별도 승인으로 릴리스하면 끝난다. 반대 방향(금지인데 배포)은 오늘처럼 운영에 영향이 있다. 기존 12자 규칙의 오탐(`배포 후 변경하지 마` 가 금지로 판정)은 회귀 보호 차원에서 그대로 두었다.

## 4. 검증 (실제 실행 결과)

| 명령 | 결과 |
|---|---|
| `bash -n scripts/pipeline-runner.sh` / `.local` | OK |
| `pytest tests/unit/test_runner_deploy_directive_contract.py` | **59 passed** (신규) |
| `bash scripts/run_unit_tests.sh` (신규 + `test_pipeline_runner_deploy_directive_gate/forbid_regex/deploy_only_job_path/deploy_only_approval_commit/script_guards/deploy_gate_stale_chain/deploy_lock_requeue/shell_deploy_lock_requeue/autodep_release/push_stale_base`, `test_dup_guard`) | 해당 파일 전부 통과 |
| 같은 실행의 `test_review_hold_commit_gap.py` | **3 failed, 4 errors — 이번 변경과 무관(기존 실패)**. 이 테스트가 `review-hold-sweeper.sh` 에서 찾는 `# 재시도 추적 컬럼` 마커가 HEAD 에 없고(`ValueError: substring not found`), 이번 작업은 스위퍼를 수정하지 않았다 |

신규 테스트가 고정하는 것
- 사고 지시문 원문·`PUSH_ONLY` 변형·정책 헤더·긴 한글 문구는 FORBID, 기존 자연어 13종은 계속 FORBID.
- `PUSH_ONLY: false`, 식별자 `push_only_*`, `DEPLOY_ONLY: true`, `배포 후 검증해라` 등 무관한 문장은 ALLOW(정상 배포 회귀 없음).
- 큰 적대적 입력(약 3만 자)에서 즉시 종료.
- **stub 실행**: deploy_job 게이트 구간을 stub(`db_exec`, `_fail_job`, `db_update` …)으로 실행해 PUSH_ONLY → 빌드 stub 미도달·`status=done`·`deployed_at=NULL`, 조회 실패/빈 값 → 빌드 stub 미도달·`deploy_directive_unverifiable`, 일반 잡 → 빌드 stub 도달. 실제 `deploy.sh`/docker 명령은 실행하지 않았다.
- 불변식: 상태 없음/다른 잡 id → 중단, 일치 → 통과. 모든 `deploy.sh`·`docker compose` 호출 위치가 불변식보다 뒤에 있음(정적).

**실행하지 않은 것**: 실제 배포·빌드·재시작·migration. 운영 러너 프로세스에 대한 반영 확인(승인 후 Runner 경로 대상).

## 5. 후속·위험

- 이 수정은 승인·커밋 후 공유 체크아웃의 러너 스크립트에 반영돼야 효력이 있다. 그 전까지 운영 게이트는 옛 규칙이다. **배포 완료가 아니다.**
- runner-9d5d8d45 가 이미 반영한 릴리스(SHA `7bfe17b9…`)의 유지/되돌림은 별도 사람 판단 사항이다. 이 작업은 건드리지 않았다.
- 원격 호스트 러너가 스크립트 사본을 따로 가지면 동기화 전까지 옛 게이트다(동기화 경로는 이 작업 범위 밖).

## 6. 롤백 검토안 (실행하지 않음)

수정 커밋 1개를 `git revert <수정커밋>` 하면 `pipeline-runner.sh`, `.local`, 신규 테스트, 이 보고서가 함께 원복된다. 원복 시 PUSH_ONLY·40자 간격 규칙과 fail-closed 가 사라지고 사고 이전 게이트로 돌아간다. 롤백이 필요한 상황은 fail-closed 가 정상 배포를 막는 경우(DB 순단이 길 때)인데, 그 경우 `DEPLOY_DIRECTIVE_LOOKUP_ATTEMPTS`/`DEPLOY_DIRECTIVE_LOOKUP_RETRY_SLEEP` 환경변수로 재시도만 늘려도 대부분 해소된다.
