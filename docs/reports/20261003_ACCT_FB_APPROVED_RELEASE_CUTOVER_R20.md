# ACCT-FB-APPROVED-RELEASE-CUTOVER-20261003-R20 — 결과 보고

- 작성: runner worker 세션, 2026-10-03
- 결론: **cutover 미실행 (하네스 규칙상 이 세션에서 실행 불가)**. 읽기 전용 사전 점검과 실행 경로 소스 확인만 했다.
- 실제 커밋/푸시/배포: 이 세션은 하지 않았다. 변경 파일은 이 보고서 1건뿐이다.
- DB handover `acct-fb-approved-release-cutover-20261003-r20`: 이 세션에서 쓰지 않았다.
- deploy_run_id: **없음**. 이 세션이 큐를 등록하지 않았으므로 시스템이 발급한 ID가 없다. 허위 ID나 성공 기록을 만들지 않았다.

## 1. 실행하지 않은 이유

이 세션의 필수 규칙이 지시서 3·4항이 요구하는 동작을 금지한다.

- 격리 worktree 구성: `git worktree`, `git checkout` 금지
- apply-origin / apply-edge / 방화벽 변경 / 300초 monitor: 배포·운영 변경이며 규칙상 CEO 승인 뒤 Runner 몫
- Playwright·Vault 로그인 E2E: 인증된 ops API와 Vault 자격 증명이 이 세션에 없다

규칙을 우회해 같은 효과를 내는 방법(다른 도구로 worktree 구성, wrapper 직접 호출)은 쓰지 않았다.

## 2. 재확인한 사실 (읽기 전용)

| 항목 | 값 |
|---|---|
| 이 worktree HEAD | `a0664ceeada8269ad3fd8b196029aedb13060abb`, 변경 없음 (git status clean) |
| `git ls-remote origin refs/heads/main` | `a0664cee…` — 승인 SHA와 동일 |
| `/root/aads/state/acct-fb-edge-fw.approved` | 내용 `a0664cee…` (41바이트 = SHA + 개행), 03:30 생성 |
| host `/root/aads/aads-server` | `git status --short` 27줄 (무관 dirty). 승인 파일에 무관 HEAD를 추가하거나 host 체크아웃을 갱신하면 안 되는 상태 |
| `deploy_acct_fb_cutover.sh`, `cutover_fb_cafe24.sh`, `config/apache/fb-cafe24.conf` | 모두 tracked. `bash -n` 통과 (두 스크립트) |
| 실행 중인 cutover 프로세스 | `ps` 조회 결과 없음 |
| 공개 `aads.newtalk.kr/api/v1/ops/health-check` | 200 |
| 공개 `fb.newtalk.kr/health/live` | 200 — 이는 현재 경로의 상태일 뿐이며 cafe24 origin 이 응답한다는 증거가 아니다 (access log correlation 미수행) |
| `error_book.py list --candidates` | 실행 실패: `psql` 없음. 오류사전 match는 **미확인** |
| pipeline_jobs / deploy_runs / workspace ledger 활성 조회 | DB 접근 도구가 없어 **미수행**. "활성 0" 을 재확인했다고 주장하지 않는다 |

## 3. 실행 경로 소스 확인 (핵심 발견)

`app/services/deploy_adapters/targets.py:90-104` 의 ACCT `fb-cutover` 타겟은 다음처럼 고정돼 있다.

- `executor="local"`, `repo_path="/root/aads/aads-server"`
- `command=("bash", "/root/aads/aads-server/scripts/deploy_acct_fb_cutover.sh")`
- `timeout_seconds=1200`, `supports_rollback=False`

`app/services/deploy_adapters/` 에서 `env`/`environ` 전달 코드는 `base.py:43 target_env`(배포 대상 환경 이름) 뿐이고, 어댑터가 `AADS_DEPLOY_REPO_DIR` 같은 환경 변수를 실행 프로세스에 넘기는 경로는 찾지 못했다.

따라서 지시서가 제안한 "worktree 를 `AADS_DEPLOY_REPO_DIR` 로 지정" 은 어댑터 기본 경로로는 불가능하다. 실제로는 아래 중 하나가 필요하다.

1. 운영 worker 프로세스 환경에 `AADS_DEPLOY_REPO_DIR=<격리 worktree>` 를 주입. 그래도 어댑터 `command` 의 스크립트 경로는 host 것이므로, 실행되는 wrapper 파일 자체는 host 체크아웃의 것이다. host 가 dirty 여도 wrapper 가 검사하는 것은 `scripts/cutover_fb_cafe24.sh` 와 `config/apache` 의 변경뿐이므로 wrapper 는 `REPO` 로 지정한 worktree 의 HEAD 와 승인 SHA 를 대조한다.
2. 어댑터에 env 전달 또는 `repo_path` 오버라이드를 추가하는 코드 변경. 이 지시서의 TARGET_FILES 밖이며 별도 러너·독립 리뷰가 필요하다.

wrapper 는 `REPO` 의 HEAD ≠ release 이면 exit 4, cutover 파일이 untracked/dirty 이면 exit 5, `FW_APPROVAL_FILE` 에 full head_sha 가 없으면 edge 를 건드리지 않는다. 승인 게이트 자체는 이 상태로 유지된다.

## 4. STEP 0 분류

- 유지: `deploy_acct_fb_cutover.sh`, `cutover_fb_cafe24.sh`, `fb-cafe24.conf`, `targets.py`, `external.py`, 승인 파일
- 수정: 없음
- 신규: 이 보고서
- 삭제: 없음

## 5. 완료기준 상태

| 항목 | 상태 |
|---|---|
| 중복 실행 확인 | ps 기준 없음. DB(pipeline_jobs/deploy_runs) 기준은 미수행 |
| a0664cee push 재확인 | 완료 (ls-remote 일치) |
| 격리 worktree 구성 | 미수행 (규칙상 금지) |
| ops 큐 등록·deploy_run_id | 미수행 / ID 없음 |
| apply-origin → 방화벽 대조 → apply-edge → 300초 monitor | 미수행 |
| `bash -n` | 통과 (두 스크립트) |
| lint / preflight | preflight 미수행, ruff 대상 Python 변경 없음 |
| 공개 `fb.newtalk.kr/health/live` | 200 (cutover 증거 아님) |
| access log correlation, root·static·API 검증 | 미수행 |
| 서버 Playwright/E2E, 화면 캡처 | 미수행. **브라우저 E2E 미실행, 화면 완료기준 미충족** |
| 300초 P0/P1 오류 0 측정 | 미수행 |
| goal/milestone link_task, M5 구분 | 미수행. wrapper 성공이 없으므로 M0~M6 어느 것도 완료로 신고하지 않는다 |
| Clobe runner-6aeed147 / 7f790efa | 건드리지 않음 |
| 유료 LLM 비용 | 이 세션의 비용 미측정 |

## 6. 다음 단계 (Runner / 오케스트레이터, 이 세션은 실행하지 않음)

1. Runner 가 `a0664cee…` 정확한 SHA 로 깨끗한 detached worktree 를 만들고 HEAD·clean 을 검증한다.
2. 운영 worker 환경에 `AADS_DEPLOY_REPO_DIR=<그 worktree>`, `FW_APPROVAL_FILE` 기존 경로, `MONITOR_SECONDS>=300` 을 주입할 수 있는지 확인한다. 불가하면 §3-2 의 어댑터 변경을 별도 러너로 등록한다.
3. ops 큐로 ACCT `fb-cutover` `release_sha=a0664cee…` 를 등록해 시스템이 발급한 deploy_run_id 를 기록한다.
4. `apply-origin → HTTPS origin health → (승인 파일 대조 후 5.104.86.116/32 443만) apply-edge → routed health → 300초 monitor` 를 실행하고 실패 시 wrapper 의 복구 경로를 쓴다. 1200초 상한을 넘기면 재시도 없이 실패 근거와 복구 상태를 보고한다.
5. 서버 E2E(Vault domain tenant 로그인, 단하루 회사 표시, 원장/매장비서, 첨부 접근, 세션복구)는 `run_e2e_verify` 정본 경로로 수행하고, 실패하면 HTTP→API→프로세스 폴백과 "브라우저 E2E 미실행" 을 명시한다.
6. 이 보고서와 별개로 오류사전 match 와 활성 job/deploy_run 중복 조회를 DB 접근 가능한 환경에서 다시 수행한다.
