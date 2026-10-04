# AADS-SEQUENTIAL-CONTROL-VERIFY-20261005 — 순차 복구 1단계: 승인 통제·PUSH_ONLY 실행본 검증

작성: 2026-10-05 KST · 러너 job `runner-68acf38e` · 코드 변경 없음(보고서 1개만 추가) · 커밋/푸시/배포/재시작 안 함

## 0. 결론 (먼저)

| # | 검증 항목 | 판정 | 근거 |
|---|---|---|---|
| 1 | PUSH_ONLY gate 코드(9ca4854b)의 계약 | **PASS** | origin/main 사본 기준 `test_runner_deploy_directive_contract.py` 59 passed |
| 2 | PUSH_ONLY gate 가 **실행 중 러너 프로세스에 로드됨** | **FAIL** | 러너가 로드한 파일(fingerprint `56080140393b`)에 gate 코드 0건. 실행본 사본에 같은 계약 테스트를 돌리면 31 failed / 28 passed |
| 3 | 이 배치 지시서(A~E)의 `PUSH_ONLY` / `DEPLOY: false` 가 실행본에서 배포를 막는가 | **FAIL** | 실행본 함수는 A~E 5건 모두 ALLOW. 신규 함수는 5건 모두 FORBID |
| 4 | 승인 없는 push/deploy 차단 | **PASS(승인 경로 한정)** | deploy 는 `status='approved'` 잡만 claim. approved 는 `/approve` 가 `awaiting_approval` 에서만 만든다. 이 세션의 자동승인 grant 0건 |
| 5 | e266ec09→4fddd8cf 의존·상태 | **PASS(변경 없음)** | e266ec09 `queued`(depends_on 4fddd8cf), 4fddd8cf `awaiting_approval`(리뷰 APPROVE, approved_at 없음). 건드리지 않음 |
| 6 | deploy_runs 5589 | **PASS(재동기화 안 함)** | success/completed, `image_digest = standby_digest` |

**핵심**: 게이트 코드는 맞지만 **운영 러너는 아직 옛 게이트로 돈다.** 지금 이 배치의 잡이 승인되면 `PUSH_ONLY` 표기에도 불구하고 push 뒤 blue/green 배포 경로가 열려 있다. 막고 있는 것은 사람 승인 단계 하나뿐이다. 승인 시 이 한계를 알고 있어야 한다.

## 1. STEP 0 — 조회 대상 분류

| 대상 | 분류 | 비고 |
|---|---|---|
| `scripts/pipeline-runner.sh` (`instruction_forbids_deploy`, `read_job_instruction_strict`, `deploy_job` 게이트, `maybe_reexec_on_self_change`) | 유지(읽기만) | 수정 없음 |
| `app/api/pipeline_runner.py` `/pipeline/jobs/{id}/approve` | 유지(읽기만) | `require_tenant_member`, `awaiting_approval` 에서만 전이 |
| `tests/unit/test_runner_deploy_directive_contract.py` | 유지 | 원본·사본 모두 실행만 함 |
| `docs/reports/20261005_SEQUENTIAL_CONTROL_VERIFY.md` | 신규 | 이 보고서(TARGET_FILES) |
| 삭제 | 없음 | 호출처 영향·롤백 대상 없음 |

## 2. 오류사전 match

`scripts/error_book.py match -` 는 쉘에 `PGHOST=localhost` 가 있어 호스트 `psql` 을 찾다가 실패했다(`No such file or directory: 'psql'`). `env -u PGHOST` 로 컨테이너 경로를 쓰면 조회된다.

- 일치: `runner.push_only_directive_miss` (재발 1회). 사전의 조치란은 "수정안 작성·로컬 검증 완료, 커밋 SHA 미기록 — 운영 러너 반영 전까지 옛 게이트" 이다.
- 이번 검증이 확인한 것은 사전 문구와 일치한다. 사전을 갱신하는 일은 이 작업 범위 밖이라 하지 않았다(제안은 §7).

## 3. PUSH_ONLY 실행본 검증

### 3.1 러너 프로세스와 소스 흐름 (확인한 사실)

| 항목 | 값 | 확인 명령 |
|---|---|---|
| 서비스 | `aads-pipeline-runner.service`, MainPID **4050729**, NRestarts=0, 기동 2026-10-02 10:19:15 CEST | `systemctl show/status` |
| ExecStart | `/root/aads/aads-server/scripts/pipeline-runner.sh` (공유 체크아웃) | unit 파일 |
| 열린 스크립트 fd | `/proc/4050729/fd/255` → inode 583296, mtime 2026-10-02 08:24:26, 현재 디스크 파일과 같은 inode | `stat -L` |
| 러너 자체 기록 | `RUNNER_SELF=…/pipeline-runner.sh fingerprint=56080140393b` (10-02 10:19:16 CEST) | `runner.log` |
| 디스크 파일 sha256 | `56080140393b00f2…` — 위 fingerprint 와 일치 | `sha256sum` |
| gate 코드 유무 | 공유 체크아웃 `pipeline-runner.sh`·`.local` 에서 `read_job_instruction_strict`/`re_push_any`/`deploy_directive_gate_bypassed` **0건** | `grep -c` |
| 9ca4854b 위치 | origin/main 에는 있고 공유 체크아웃 HEAD(`cf963b73`)에는 없음 | `merge-base --is-ancestor` |
| origin/main 의 스크립트 sha256 | `5d9bc048…` (러너 fingerprint 와 다름) | `git show` |
| 자기 재적용 | `maybe_reexec_on_self_change` 는 **`RUNNER_SELF_PATH`(공유 체크아웃 파일)** 의 sha256 이 바뀌고 유휴일 때만 `exec`. 마지막 SELF_RELOAD 는 10-02 08:21 (`80c450df→56080140`). 9ca4854b 이후 없음 | 소스 5948~5990행, `runner.log` |

판단 근거 두 개는 서로 독립이다. (a) 러너가 자기 기동 시 기록한 fingerprint 가 gate 없는 파일의 sha256 과 같다. (b) 열린 fd 가 gate 없는 파일의 inode 와 같다.

시작 시각만으로는 판단하지 않았다. `exec` 는 PID 와 시작 시각을 유지하므로 "10-02 10:19 기동 → 9ca4854b(10-04) 이전" 은 보조 증거일 뿐이다. 새 셸에서 파일을 source 한 결과도 증거로 쓰지 않았다. 다만 `/proc/<pid>/mem` 으로 메모리 안의 함수를 직접 읽지는 못했다(ptrace 는 시도하지 않음). 그래서 "메모리 내용 직접 확인" 이 아니라 "러너 로그 fingerprint + fd inode 로 추론" 이다.

공유 체크아웃 상태: 브랜치 main, 31개 dirty. `scripts/pipeline-runner.sh` 는 **스테이징된(M ) 변경**이 있고 그 내용은 HEAD 와 다르며 gate 도 없다. 원장 `chat_workspace_change_ledger` 에 같은 파일의 `dirty` 행이 세션 `5090a247` 로 남아 있다(id 35664/35667, 10-02 14:38). 이 변경은 다른 세션 소유라 보존했다.

### 3.2 부작용 없는 stub 계약 테스트 (실행 결과)

모든 실행은 `/tmp` 사본에서 했고 운영 파일·DB·컨테이너를 쓰지 않았다. 테스트가 쓰는 stub 은 `db_exec`·`_fail_job`·빌드 호출이며 실제 `deploy.sh`/docker 는 호출되지 않는다.

| 대상 스크립트 | 명령 | 결과 |
|---|---|---|
| 워크트리(= origin/main, 9ca4854b 포함) | `pytest -q tests/unit/test_runner_deploy_directive_contract.py` | **59 passed** |
| 실행본 사본(공유 체크아웃 파일, 러너 fingerprint 와 동일) | 같은 테스트를 `/tmp/verify_20261005/oldroot` 에 사본으로 배치해 실행 | **31 failed, 28 passed** |

실행본 사본의 대표 실패: 불변식 테스트에서 `[]\nBUILD_REACHED\nRC=0` — 게이트 기록 없이도 빌드 단계에 도달한다. 즉 fail-closed 와 빌드 직전 불변식이 실행본에 없다.

### 3.3 실제 지시서 대조 (`pipeline_jobs.instruction`, 함수만 추출해 판정)

실행본 = 공유 체크아웃 파일에서 `instruction_forbids_deploy` 를 추출, 신규 = 워크트리 파일에서 추출.

| job | 역할 | 실행본 | 신규 |
|---|---|---|---|
| runner-68acf38e | A (이 작업) | **ALLOW** | FORBID |
| runner-a0be8138 | B | **ALLOW** | FORBID |
| runner-4e443ba6 | C | **ALLOW** | FORBID |
| runner-6c4be959 | D | **ALLOW** | FORBID |
| runner-9a050352 | E | **ALLOW** | FORBID |
| runner-9d5d8d45 | 07:54 사고 잡 | ALLOW (사고 재현) | FORBID |
| runner-1e5131a2, runner-139813c4 | 과거 PUSH_ONLY 잡 | FORBID | FORBID |
| runner-4fddd8cf, runner-e266ec09 | 보류 승인 건 | FORBID | FORBID |

해석: 10-04 이후 `runner.log` 에 `DEPLOY_SKIPPED_BY_DIRECTIVE` 가 여러 건 찍혔고 DB 에 `push_only_by_directive` 로 끝난 잡이 많다. 이것은 gate 가 로드됐다는 증거가 **아니다**. 그 잡들의 지시서는 "배포 금지" 문구가 옛 12자 규칙 안에 있어 옛 게이트도 FORBID 였다(위 표의 1e5131a2·139813c4). 이번 A~E 지시서는 `PUSH_ONLY`·`DEPLOY: false` 구조화 표기라서 옛 게이트가 읽지 못한다.

## 4. 승인 통제 (읽기 조회)

| 항목 | 결과 |
|---|---|
| deploy 진입 | `claim_approved_job` 은 `status='approved'` 만 claim 한다(소스 2893행). `deploy_job` 은 그 뒤에만 호출(6094행) |
| approved 로 만드는 길 | `POST /api/v1/pipeline/jobs/{id}/approve` — `require_tenant_member`, 대상이 `awaiting_approval` 이 아니면 400, approve 시 e2e 증거 게이트(`assert_screen_evidence_gate`) 실행. **이 API 는 PUSH_ONLY 를 검사하지 않는다** — PUSH_ONLY 방어선은 러너 쪽 하나뿐이다 |
| 이 세션(`8bf0405a…`)의 자동승인 | `goal_auto_approval_grants` 에 이 세션 principal 로도, AADS active 로도 **0건**. `goal_approval_kill_switches` active 0건 |
| pending 카드 | `approval_queue` pending 5건은 모두 2026-03~04 의 오래된 항목(테스트·구형 다운 알림)이며 이번 배치와 무관. 이번 배치 pending 카드 없음 |
| 이번 배치 큐 | A `running`, B(a0be8138)→C(4e443ba6)→D(6c4be959)→E(9a050352) 모두 `queued`, `depends_on` 이 직전 잡으로 직렬. 아직 아무도 awaiting_approval/approved 가 아님 |
| 후속 예약 | 이 배치의 직렬 의존 외에 별도 예약 없음(`depends_on` 사슬만 확인) |
| e266ec09→4fddd8cf | `runner-e266ec09` queued, `depends_on=runner-4fddd8cf`. `runner-4fddd8cf` awaiting_approval, review_verdict APPROVE, commit `34106ae0…`, approved_at/deployed_at 없음. 이 단계에서 상태·승인을 바꾸지 않았다 |
| deploy_runs 5589 | status success / phase completed / `current_slot=8102`, `candidate_slot=8100`, `image_digest=standby_digest` → true, release_sha `81bdcc6c`. stale 핸드오버만으로 standby 재동기화하지 않았다 |
| 동일 파일 충돌 | `docs/reports/20261005_SEQUENTIAL_CONTROL_VERIFY.md` 를 건드리는 ledger 행·다른 워크트리 변경 없음. `scripts/pipeline-runner.sh` 는 위 §3.1 의 dirty 행이 있어 **다른 세션 소유로 보존** |

주의: deploy_runs 5589 의 release_sha `81bdcc6c` 에는 9ca4854b 가 포함돼 있다(그 앞 커밋). 그러나 러너 스크립트는 **컨테이너 이미지가 아니라 호스트 공유 체크아웃 파일**이 실행본이라, aads-server 가 배포됐다는 사실이 러너 게이트 반영을 뜻하지 않는다.

## 5. 적용 불가 사유와 실행본 반영안 (실행하지 않음)

**이 작업에서 반영하지 않은 이유**: 실행본은 공유 체크아웃의 파일이다. 그 파일은 다른 세션(`5090a247`)의 스테이징된 변경이 걸려 있고, 이 작업의 규칙은 타 세션 dirty/staged 보존이다. 또 파일을 덮거나 서비스를 재시작하는 일은 이번 승인 범위(확인·정비·검증)를 넘는 별도 승인 사항이다.

**실행본**: `/root/aads/aads-server/scripts/pipeline-runner.sh` (systemd ExecStart, PID 4050729). 반영은 파일이 바뀌고 러너가 유휴일 때 `maybe_reexec_on_self_change` 가 `exec` 하거나, 승인된 점검 창구에서 서비스 재기동으로 이뤄진다. 재기동은 `docker compose` 가 아니라 서비스 단위이며 CEO 승인 사항이다.

**변경 필요 내용(제안)**
1. 5090a247 세션의 스테이징 변경(ACCT 부팅 안전망)을 먼저 처리(커밋 또는 의도적 폐기)해 공유 체크아웃을 clean 으로 만든다.
2. 공유 체크아웃을 origin/main(≥ 9ca4854b)으로 fast-forward 한다. 단 `pipeline-runner.sh` 와 `.local` 은 `test_scripts_are_byte_identical` 이 바이트 동일을 요구하므로 둘을 함께 갱신한다. 이때 fingerprint 가 바뀌어 유휴 시 자동 `exec` 된다.
3. 새 파일이 `bash -n` 통과인지는 `maybe_reexec_on_self_change` 가 직접 검사한다(실패 시 현재 코드 유지, `SELF_RELOAD_SKIP` 로그).

**반영 후 검증안**
- `runner.log` 에 `SELF_RELOAD … → 5d9bc048…`(또는 그 이후 sha) 와 새 `RUNNER_SELF=… fingerprint=` 줄.
- 새 fingerprint 와 `sha256sum` 이 일치하고, `/proc/<pid>/fd/255` 의 inode 가 새 파일.
- 실행본 사본에 §3.2 계약 테스트 재실행 → 59 passed.
- `PUSH_ONLY` 잡 하나가 `DEPLOY_SKIPPED_BY_DIRECTIVE` 와 `phase=push_only_by_directive` 로 끝나는지 로그와 DB 로 확인(지시서가 옛 규칙으로도 막히는 문구가 아닌 것으로 골라야 구분된다).

**롤백안**: 파일 갱신 직전 sha(`56080140393b…`)의 파일을 `/tmp` 에 보관해 두었다가 되돌리면 러너가 다시 `exec` 로 옛 코드를 로드한다. 커밋 단위로는 9ca4854b 를 `git revert` 하면 스크립트·테스트·보고서가 함께 원복된다. 정상 배포가 fail-closed 로 막히면 `DEPLOY_DIRECTIVE_LOOKUP_ATTEMPTS`/`DEPLOY_DIRECTIVE_LOOKUP_RETRY_SLEEP` 로 재시도만 늘리는 것이 먼저다.

**그 사이 임시 완화(제안, 실행 안 함)**: B~E 잡을 승인할 때 `PUSH_ONLY` 에 의존하지 말고 승인자가 "push 만" 임을 승인 화면에서 확인한다. 반영 전까지 이 배치의 승인은 곧 배포 허용이라는 전제로 판단해야 한다.

## 6. 실행한 검증 명령과 결과

| 명령 | 결과 |
|---|---|
| `git status` / `git log` / `git show 9ca4854b` | 워크트리 clean, 9ca4854b 내용 확인 |
| `env -u PGHOST python3 scripts/error_book.py match -` | `runner.push_only_directive_miss` 일치 (PGHOST 설정 시 psql 없음 오류) |
| `systemctl show/status aads-pipeline-runner`, `ps`, `/proc/4050729/{cmdline,fd,environ 이름}` | §3.1 |
| `sha256sum`, `stat -L /proc/4050729/fd/255`, `grep -c` | 러너 로드 파일 = 공유 파일, gate 0건 |
| `git merge-base --is-ancestor 9ca4854b {HEAD, origin/main}` | 공유 HEAD 미포함, origin/main 포함 |
| `pytest -q tests/unit/test_runner_deploy_directive_contract.py` (워크트리) | 59 passed |
| 같은 테스트 (실행본 사본, `/tmp/verify_20261005/oldroot`) | 31 failed, 28 passed |
| 함수 추출 후 지시서 판정 스크립트 (`/tmp/verify_20261005`) | §3.3 표 |
| `pipeline_jobs`, `goal_auto_approval_grants`, `goal_approval_kill_switches`, `approval_queue`, `deploy_runs`, `chat_workspace_change_ledger` SELECT | §4 |

**실행하지 않은 것**: `bash scripts/run_unit_tests.sh`(컨테이너 기동이 필요해 호스트 pytest 로 대체, 이 테스트는 호스트에서 통과), ruff/compileall(코드 변경 없음), 빌드·배포·재시작·push, `/proc/<pid>/mem` 직접 읽기, 메모리 안 함수 정의 직접 덤프.

**부수 사실(투명성)**: 조사 중 공유 체크아웃에서 `git fetch -q` 를 한 번 실행했다. 원격 추적 ref 만 갱신되고 작업 트리·인덱스·dirty 변경은 건드리지 않았다.

## 7. 커밋·URL·푸시·배포·남은 장애 (분리 보고)

- **실제 커밋**: 없음. 이 작업은 커밋하지 않았다(승인 후 Runner 담당).
- **GitHub URL**: 없음(커밋 전).
- **푸시**: 하지 않음.
- **배포·재시작**: 하지 않음. 전역 자동실행 enable 도 하지 않음.
- **외부 LLM 비용**: 외부 LLM 호출 없음. 추가 비용 0, 다만 이 세션 자체의 토큰 비용은 측정하지 않았다.
- **handover**: `project_handover_entries` 기록과 `docs/HANDOVER.md` 동기화는 이 작업에서 하지 않았다. 지시서의 TARGET_FILES 가 이 보고서 하나이고 DB 쓰기·타 파일 수정은 범위 밖이어서다. 이 보고서를 근거로 B 단계 또는 Runner 가 기록해야 한다. R-001 에 따라 그 전까지 이 항목은 완료가 아니다.
- **오류사전**: `runner.push_only_directive_miss` 의 조치란이 "커밋 SHA 미기록" 상태다. 9ca4854b 가 origin/main 에 있으므로 SHA 는 채울 수 있으나, 같은 항목의 "운영 러너 반영" 은 미충족이라 `fix` 를 완료로 승격하지 않는 것이 맞다. 실행본이 옛 게이트라는 사실은 "코드가 막는 것" 이 아니라 "문서 규칙/미반영" 상태이므로 R-ERRBOOK 3항에 따라 '또 일어난다' 로 읽어야 한다. 새 항목 후보: `runner.gate_fix_not_loaded_by_shared_checkout` (원인: 러너 실행본이 공유 체크아웃 파일인데 그 체크아웃이 origin/main 보다 뒤이고 다른 세션의 스테이징 변경이 있어 fast-forward 되지 않음 — 확인된 사실).

### 남은 장애

1. **[P0 위험] 실행 러너에 PUSH_ONLY gate 미반영.** 승인 시 `PUSH_ONLY`/`DEPLOY: false` 가 배포를 막지 못한다. 방어선은 사람 승인 하나.
2. 공유 체크아웃이 origin/main 보다 뒤(`cf963b73`)이고 31개 dirty, `pipeline-runner.sh` 에 타 세션 스테이징 변경이 있어 자동 반영이 막혀 있다.
3. `PGHOST=localhost` 환경에서 `error_book.py` 가 psql 부재로 실패한다(`env -u PGHOST` 로 우회 가능). 도구 개선 후보.

## 8. B 단계 인계 (수락조건 확인용)

- 판정 FAIL 두 건(§0 2·3)은 코드 결함이 아니라 **반영 상태** 문제다. B 는 자신의 지시서에 배포 효과가 있는지 확인하고, 있으면 승인 경로에서 "push 만" 임을 사람이 확인하도록 해야 한다.
- 4fddd8cf/e266ec09 의 승인·상태는 이 단계에서 바꾸지 않았다.
- 승인되지 않은 특정 목업 revision 은 승인하지 않았다.
