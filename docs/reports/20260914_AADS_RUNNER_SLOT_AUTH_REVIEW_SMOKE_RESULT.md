# RESULT — 읽기 전용 E2E 스모크 검수 피드백 대응 (2026-09-14)

- 작성 시각: 2026-09-14 05:30 (서버 로컬 `+02:00`)
- 대상 워크트리: `/root/aads/aads-server` (**공유 main 워크트리** = `pipeline_c` 의 `workdir`)
- 대상 파일: `scripts/pipeline-runner.sh` — 미커밋 변경 (스냅샷 시점 90+/4-)
- 배포 상태: **미배포·미커밋**. 본 세션의 소스 변경 0건
- 미커밋분 비파괴 백업: dangling commit `c9c8e130`
  (`On main: backup: AADS-RUNNER-SLOT-AUTH uncommitted (review-smoke 2026-09-14T05:28:32+02:00)`)

> **주의 — 이 워크트리는 지금도 다른 세션이 편집 중이다.** 본 조사 중에만
> `scripts/pipeline-runner.sh` 가 **05:24:30 → 05:29:47** 두 번 제자리 갱신되었고
> 그때마다 diff 가 80+ → 90+ 로 늘었다. 실측은 §3-6.

---

## 0. 검수 피드백 4건 대응 요약

| # | 검수 지적 | 판정 | 조치 |
|---|---|---|---|
| 1 | 읽기 전용 지시 위반 — `pipeline-runner.sh` 에 약 60줄 신규 코드 | **오탐** | 본 스모크의 산출물이 아니다. 공유 워크트리에 이미 있던 **AADS-RUNNER-SLOT-AUTH 작업분**이고, 지금도 그 소유 세션이 편집 중이며, 운영 러너가 그 코드로 돌고 있다. 근거 §3. **원복하지 않았다** — 원복이 더 위험하다(§4) |
| 2 | RESULT 파일 누락 (검수 기준 6번) | **인정** | 본 문서. §2 가 `[유지\|수정\|신규\|삭제]` 조사표, §2-2 끝이 삭제 사유 |
| 3 | Git Diff 불완전 — `local TOKEN_2="${ANTHROPIC_AUTH_TOKEN_` 에서 잘림 | **인정(원인은 검수 하네스)** | 절단자는 `app/services/pipeline_runner_service.py:1979` 의 `self.git_diff[:3000]`. 보고서가 자른 것이 아니다. **전체 diff 무절단 첨부는 §5** |
| 4 | 지시 범위 모호성 — 읽기 전용인데 diff 는 기능 구현 | **인정(지적이 정확)** | 검수기가 보는 diff 는 작업 산출물이 아니라 **공유 workdir 전체의 `git diff HEAD`** 다(§3-1). 구조상 스모크 산출물과 타 세션 작업분이 구분되지 않는다. 재발 방지는 §6 |

---

## 1. 지시 대비 이행 상태

| 지시 항목 | 상태 |
|---|---|
| 파일 생성/수정/삭제 금지 | **준수** — 본 세션 소스 변경 0건. 생성 파일은 본 RESULT 1건뿐(피드백 #2 가 명시 요구) |
| E2E 읽기 전용 검증 | 완료 |
| 검수 지적 4건 대응 | 완료 (§0) |
| 기존 구현 조사표 + 삭제 사유 | 완료 (§2) |
| 전체 Git Diff 제시 | 완료 (§5, 무절단 전문) |

---

## 2. 기존 구현 조사표

`target | 기존 구현 | 결정 | 사유` — **본 세션의 변경 0건 / 삭제 0건.**

### 2-1. 본 세션(검수 피드백 대응)

| target | 기존 구현 | 결정 | 사유 |
|---|---|---|---|
| `scripts/pipeline-runner.sh` | 미커밋 슬롯 자격증명 배선(타 작업분) | **유지** | 본 세션 미변경. 원복 시 운영 러너 정지(§4). 소유 작업이 커밋해야 할 코드 |
| `/root/aads/vendor/claude-cli/claude` | 릴레이 번들 CLI(저장소 밖, 2026-09-14 05:24 배치) | **유지** | 본 세션 미변경. `RUNNER_CLAUDE_CLI_BIN` 이 참조하는 실물 |
| `scripts/claude-slot-credentials-wrapper.sh` | 커밋 `a49eee32` (2026-09-12) | **유지** | 본 세션 미변경. 이미 커밋된 기존 구현 |
| `app/services/pipeline_runner_service.py` | 검수 프롬프트·diff 수집 | **유지** | 원인 규명만 수행(§3-1·§3-2). 하네스 수정은 지시 범위 밖 — 권고만(§6) |
| `docs/reports/20260914_AADS_RUNNER_SLOT_AUTH_REVIEW_SMOKE_RESULT.md` (본 문서) | 없음 | **신규** | 검수 기준 6번 충족을 위해 피드백 #2 가 요구한 산출물 |
| `ohvis_wiki_error_book` 항목 `review.shared_worktree_diff_misattribution` | 없음 | **신규(DB)** | R-ERRBOOK — 확인된 원인이므로 사전에 등록. 파일 변경 아님 |

**삭제 0건.** 본 세션이 삭제한 파일·함수·API 없음.

### 2-2. 워크트리에 이미 존재하던 미커밋분(AADS-RUNNER-SLOT-AUTH) — 참고 조사표

본 세션의 산출물이 아니지만 검수기가 이 diff 를 보았으므로 조사표를 제공한다.
**제거된 4줄 전부가 분기 안에 보존되어 실질 삭제 0건이다.**

| target | 기존 구현 | 결정 | 사유 |
|---|---|---|---|
| `CLAUDE_RELAY_SLOT_HOME_ROOT` / `CLAUDE_SLOT_CREDENTIAL_WRAPPER` / `RUNNER_USE_SLOT_CREDENTIALS` | 없음 | **신규** | 릴레이 슬롯 자격증명 경로. 전부 `${VAR:-기본값}` 이라 환경변수로 무력화 가능 |
| `RUNNER_CLAUDE_CLI_BIN` | 전역 `claude` 하드코딩 | **신규(+폴백)** | `[[ -x ... ]] \|\| RUNNER_CLAUDE_CLI_BIN="claude"` — 바이너리 부재 시 **기존 동작으로 조용히 복귀** |
| `slot_credentials_file()` | 없음 | **신규** | accessToken+refreshToken 유효성 검사 후 경로 반환, 실패 시 `return 1` → 기존 고정 토큰 경로 |
| `run_job()` 빈 토큰 가드 `if [[ -z "$TOKEN_1" && -z "$TOKEN_2" ]]` | C-4 가드 | **수정** | 조건에 `&& -z "$_slot_cred_available"` 추가. 슬롯이 살아 있으면 고정 토큰 없이도 실행. **가드 자체 유지** |
| `run_job()` 토큰 스위치 `if [[ "$token_slot" == "2" ... ]]` | 계정1/계정2 고정 토큰 전환 | **수정** | 앞에 슬롯 분기를 추가하고 기존 분기를 `elif` 로 강등. **기존 분기 본문 무변경** |
| `claude` 실행부 `timeout "$MAX_RUNTIME" claude ...` | 단일 실행 라인 | **수정** | `if 슬롯 / else 기존` 2분기로 분할. **기존 실행 라인은 `else` 에 원형 보존**(바이너리 변수화만) |
| 자식 프로세스 환경 (`env -u …`, `< /dev/null`) | 없음 | **신규(05:29:47 추가분)** | 죽은 고정 토큰이 슬롯 자격증명보다 우선되는 문제 차단. 셸 자체는 unset 하지 않아 레거시 폴백 경로 보존 |
| `normalize_claude_cli_model` 등 그 외 러너 함수 전체 | 기존 구현 | **유지** | 미변경 |

#### 삭제 사유 — 삭제 0건

제거된 4줄과 그 행선지:

| 제거된 라인 | 행선지 |
|---|---|
| `if [[ -z "$TOKEN_1" && -z "$TOKEN_2" ]]; then` | 같은 `if` 에 조건 1개 추가되어 존속 |
| `if [[ "$token_slot" == "2" && -n "$TOKEN_2" ]]; then` | `elif` 로 존속, 본문 동일 |
| `timeout "$MAX_RUNTIME" claude "${claude_args[@]}" ... \` | `else` 분기에 존속 |
| `> "$output_file" 2> "$err_file" &` | `else` 분기에 존속 |

함수·클래스·API 삭제 0건, 순삭제 라인 0줄. 검수 기준 5번 충족.

---

## 3. 피드백 #1·#4 를 오탐으로 판정한 근거

### 3-0. 원지시 자체가 "이 변경을 검증하라" 였다

병행 세션이 남긴 RESULT(`reports/20260914_AADS-RUNNER-SLOT-AUTH_smoke_RESULT.md`)에
기록된 원지시 제목은 다음과 같다.

> `READ-ONLY SMOKE 2026-09-14 12:27 KST — 러너 슬롯 자격증명 전환(AADS-RUNNER-SLOT-AUTH) E2E 검증용`

즉 **검증 대상이 곧 슬롯 자격증명 전환 코드**다. 검증 대상 코드는 정의상 스모크보다
먼저 존재해야 하므로, 그 코드가 diff 에 보이는 것은 위반이 아니라 전제다.
(출처는 동일 작업의 병행 세션 산출물이므로 독립 증거는 아니다. 독립 증거는 §3-3~§3-6.)

### 3-1. 검수기가 보는 diff 는 "작업 산출물" 이 아니라 "공유 디렉터리 전체" 다

```
app/services/pipeline_runner_service.py:836,972
    self.git_diff = (await self._ssh_command("git diff HEAD"))[:_MAX_DIFF_CHARS]
app/services/pipeline_runner_service.py:2129   (_ssh_command)
    full_cmd = f"cd {shlex.quote(self.workdir)} && {command}"
app/services/pipeline_runner_service.py:1717   (주석)
    codex 바이너리(/usr/bin/codex)와 workdir(/root/aads/aads-server)은 …
app/services/pipeline_runner_service.py:1979
    diff_text = self.git_diff[:3000] if self.git_diff else "(변경사항 없음)"
app/services/pipeline_runner_service.py:1999   (검수 기준 6번 원문)
    6. RESULT에 기존 구현 조사표([유지|수정|신규|삭제])와 삭제 사유가 있는가? 누락 시 FAIL
```

`pipeline_c` 작업은 `/tmp` 격리 워크트리가 아니라 **공유 main 워크트리
`/root/aads/aads-server` 에서 `claude -p` 로 실행**되고, 검수 프롬프트의 `## Git Diff` 는
그 디렉터리의 `git diff HEAD` 전체다. 작업 산출물로 필터링되지 않는다.
즉 **동시각 타 세션의 미커밋 작업이 이번 작업의 산출물로 제시된다.**
읽기 전용 작업이 파일을 하나도 만들지 않아도 diff 는 비지 않는다.

동일 유형의 선례: `docs/reports/20260907_AADS_GOAL_GUARD_HARNESS_RESULT.md` §0 #2
— "동일 워크트리 병행 러너의 변경. 본 작업 커밋에 미포함".

### 3-2. 절단 지점도 하네스 때문이다 (피드백 #3)

`[:3000]` 슬라이스가 원인이다. 스냅샷 시점 diff 에서
`local TOKEN_2="${ANTHROPIC_AUTH_TOKEN_` 는 **3,742 바이트** 지점에 있다(전체 8,286 바이트).
검수 시점에는 같은 줄이 3,000 바이트 경계에 걸려 문장 중간에서 끊겼다.
그 사이 offset 이 뒤로 밀린 것은 §3-6 의 제자리 갱신 때문이다.
`_MAX_DIFF_CHARS=50000` 은 여유가 있었고, 실제 병목은 1979줄의 `3000` 이다.

### 3-3. 이 미커밋분은 지금 운영 러너가 실제로 쓰고 있다

```
/var/log/aads-pipeline/runner.log
[2026-09-14 05:25:35] ═══ Pipeline Runner v2.1 시작 …
[2026-09-14 05:26:21]   TOKEN_SWITCH job=runner-1d248d86 → 계정1 via slot_credentials (refreshable)
[2026-09-14 05:27:01]   TOKEN_SWITCH job=runner-1d248d86 → 계정2 via slot_credentials (refreshable)
```

`via slot_credentials (refreshable)` 문자열은 **미커밋 diff 에만** 있다. 커밋된 HEAD 에는 없다.
05:25:35 에 재기동된 러너가 이 코드 경로로 동작 중이다.

### 3-4. 스모크 세션의 부산물로 보기 어려운 정황

- `/root/aads/vendor/claude-cli/claude` — **216 MB** CLI 바이너리가 05:24 에 배치됨.
  읽기 전용 스모크가 만들 산출물이 아니다.
- `scripts/claude-slot-credentials-wrapper.sh` 는 **2026-09-12 커밋 `a49eee32`** 로 이미 존재.
  미커밋분은 그 기존 구현에 러너를 배선한 것이다.
- diff 주석에 "2026-09-14 실측 A/B(4/4, 동일 자격증명·동일 래퍼·동일 모델)",
  "계정1 429(주간한도), 계정2 401(revoked)" 등 별도 조사 작업의 실측 결과가 적혀 있다.

### 3-5. 이 미커밋분이 러너 job 결과를 오염시키지는 않는다

```
[2026-09-14 05:26:01]   PRE_VALIDATE: main dirty=1 — clean worktree enforced
[2026-09-14 05:26:17]   WORKTREE_CLEAN: /tmp/aads-wt-runner-1d248d86 base=origin/main
```

일반 러너 job 은 `origin/main` 기준 `/tmp` 격리 워크트리에서 돈다.
오염 경로는 `pipeline_c`(공유 workdir)의 **검수 입력**뿐이다.

### 3-6. 결정적 근거 — 조사 중에도 파일이 계속 바뀌었다

| 시각 | 상태 | diff 규모 |
|---|---|---|
| 05:24:30 | 첫 관측 (blob `b96b838a`) | 80 insertions(+), 4 deletions(-), 7,352 bytes |
| 05:29:47 | 두 번째 관측 (동일 inode 597813, 제자리 갱신) | 90 insertions(+), 4 deletions(-), 8,286 bytes |

05:29:47 에 추가된 내용(본 세션이 쓰지 않았다):

```bash
+                # env -u 로 자식 프로세스에서만 고정 토큰을 지운다.
+                # 2026-09-14 실측: ~/.claude/current.env 가 죽은 ANTHROPIC_AUTH_TOKEN(_2) 를
+                # export 하는데 CLI 는 이 고정 토큰을 slot credential 보다 우선한다.
+                env -u CLAUDE_CODE_OAUTH_TOKEN \
+                    -u ANTHROPIC_AUTH_TOKEN -u ANTHROPIC_AUTH_TOKEN_2 \
+                    -u ANTHROPIC_API_KEY -u ANTHROPIC_BASE_URL \
```

읽기만 하는 세션이 관측하는 동안 새 실측 결과가 코드에 반영되고 있다는 것은,
이 파일을 **다른 주체가 능동적으로 소유·개발 중**이라는 직접 증거다.

---

## 4. 원복하지 않은 이유

원복(`git checkout -- scripts/pipeline-runner.sh`)은 읽기 전용 지시를 "복구" 하는 행위가
아니라 **운영 중단을 유발하는 쓰기 행위**다.

1. **실행 중 러너가 이 파일을 fd 255 로 물고 있다.**
   관측: `/proc/947659/fd/255 -> …/scripts/pipeline-runner.sh`, 이후 `/proc/1003832/fd/255` 동일.
   bash 는 스크립트를 fd 255 로 **오프셋 단위로 이어 읽는다.** 실행 중 내용을 바꾸면
   다음 읽기가 엉뚱한 바이트로 점프해 구문 오류·중간 절단이 난다.
   (이번 편집들이 `inode 597813` 유지 = 제자리 갱신이라 이미 그 위험 구간에 있다.)
2. **살아 있는 인증 경로가 사라진다.** diff 주석의 2026-09-14 실측대로 고정 oat 토큰은
   계정1 429(주간 한도)·계정2 401(revoked)였고, 현재 동작하는 경로는 refresh 가능한
   슬롯 자격증명이다(§3-3). 원복하면 실행 중 job 이 35단 폴백을 전부 소진하고 실패한다.
3. **타 작업의 미커밋 산출물이다.** 읽기 전용 스모크가 남의 진행 중 작업을 되돌릴 권한은 없다.

대신 비파괴 백업만 확보했다: dangling commit `c9c8e130`
(`git stash create` 산출물 — 워킹트리 무변경, `git show c9c8e130` 으로 열람).

**미커밋분의 처분(커밋/원복)은 AADS-RUNNER-SLOT-AUTH 소유 세션 또는 CEO 판단 사항이다.**

---

## 5. 전체 Git Diff (무절단)

스냅샷: `scripts/pipeline-runner.sh` mtime `2026-09-14 05:29:47 +0200`, 8,286 bytes,
`1 file changed, 90 insertions(+), 4 deletions(-)`.
워크트리가 계속 변하므로 이 블록은 **해당 시각의 고정 스냅샷**이다.

```diff
diff --git a/scripts/pipeline-runner.sh b/scripts/pipeline-runner.sh
index a194c424..94f08d44 100755
--- a/scripts/pipeline-runner.sh
+++ b/scripts/pipeline-runner.sh
@@ -45,6 +45,26 @@ LOG_DIR="/var/log/aads-pipeline"
 ARTIFACT_DIR="/tmp/aads_pipeline_artifacts"
 RUNNER_HOSTNAME=$(hostname -s)
 
+# ── Claude 릴레이 슬롯 자격증명 (AADS-RUNNER-SLOT-AUTH, 2026-09-14) ─────
+# .env 고정 oat 토큰은 refresh 수단이 없어 만료/revoke 되면 러너 전체가 정지한다.
+# 2026-09-14 실측: 계정1 429(주간한도), 계정2 401(revoked)로 35단 폴백이 전멸했는데
+# 같은 시각 릴레이는 정상이었다. 릴레이가 쓰는 슬롯 자격증명은 accessToken 과
+# refreshToken 을 함께 들고 있어 CLI 가 스스로 갱신하기 때문이다.
+# 러너도 같은 래퍼를 경유해 그 자격증명을 공유한다. 슬롯이 없거나 불완전하면
+# 조용히 기존 고정 토큰 경로로 폴백하므로 슬롯이 없는 서버(211/114)는 영향이 없다.
+CLAUDE_RELAY_SLOT_HOME_ROOT="${CLAUDE_RELAY_SLOT_HOME_ROOT:-/root/.claude-relay-slots}"
+CLAUDE_SLOT_CREDENTIAL_WRAPPER="${CLAUDE_SLOT_CREDENTIAL_WRAPPER:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/claude-slot-credentials-wrapper.sh}"
+RUNNER_USE_SLOT_CREDENTIALS="${RUNNER_USE_SLOT_CREDENTIALS:-1}"
+
+# 러너가 사용할 Claude CLI 바이너리.
+# 2026-09-14 실측 A/B(4/4, 동일 자격증명·동일 래퍼·동일 모델):
+#   호스트 전역 CLI 2.1.270 → "You've hit your weekly limit" 로 전량 차단
+#   릴레이 번들 CLI 2.1.259 → 정상 응답
+# 자격증명 문제가 아니라 CLI 버전 문제였다. 릴레이와 같은 버전을 고정 경로에 두고 쓴다.
+# 바이너리(216MB)는 저장소 밖에 두며, 없으면 전역 claude 로 조용히 폴백한다.
+RUNNER_CLAUDE_CLI_BIN="${RUNNER_CLAUDE_CLI_BIN:-/root/aads/vendor/claude-cli/claude}"
+[[ -x "$RUNNER_CLAUDE_CLI_BIN" ]] || RUNNER_CLAUDE_CLI_BIN="claude"
+
 # Claude Code 인증: current.env (oat 키) 사용 — API 키(api03) 사용 금지
 source ~/.claude/current.env 2>/dev/null || true
 source /root/scripts/runner.env 2>/dev/null || true
@@ -450,6 +470,36 @@ normalize_claude_cli_model() {
     python3 "$CLAUDE_MODEL_CONTRACT" "${1:-}"
 }
 
+# 슬롯 자격증명 경로를 stdout 으로 돌려준다.
+# 쓸 수 없으면 아무것도 출력하지 않고 1 을 반환해 호출측이 고정 토큰으로 폴백한다.
+# accessToken 만 있고 refreshToken 이 없는 파일은 갱신이 불가능하므로 거부한다.
+slot_credentials_file() {
+    local slot="${1:-1}"
+    [[ "$RUNNER_USE_SLOT_CREDENTIALS" == "1" ]] || return 1
+    [[ -x "$CLAUDE_SLOT_CREDENTIAL_WRAPPER" ]] || return 1
+    command -v flock >/dev/null 2>&1 || return 1
+    local cred="${CLAUDE_RELAY_SLOT_HOME_ROOT}/slot${slot}/.claude/.credentials.json"
+    [[ -f "$cred" ]] || return 1
+    python3 - "$cred" <<'PY' || return 1
+import json
+import sys
+
+try:
+    with open(sys.argv[1], "r", encoding="utf-8") as handle:
+        payload = json.load(handle)
+except Exception:
+    raise SystemExit(1)
+oauth = payload.get("claudeAiOauth", payload)
+if not isinstance(oauth, dict):
+    raise SystemExit(1)
+for field in ("accessToken", "refreshToken"):
+    value = oauth.get(field)
+    if not isinstance(value, str) or not value:
+        raise SystemExit(1)
+PY
+    printf '%s' "$cred"
+}
+
 is_read_only_instruction() {
     local instruction="${1:-}"
     printf '%s' "$instruction" | grep -Eiq 'read-only|do not modify|no file changes|읽기[[:space:]]*전용|파일[[:space:]]*수정[[:space:]]*금지|수정하지|변경하지'
@@ -1415,7 +1465,10 @@ run_job() {
     local TOKEN_1="${ANTHROPIC_AUTH_TOKEN:-}"
     local TOKEN_2="${ANTHROPIC_AUTH_TOKEN_2:-}"
     # C-4: 빈 토큰 가드 — 둘 다 비어있으면 즉시 실패 처리
-    if [[ -z "$TOKEN_1" && -z "$TOKEN_2" ]]; then
+    # 단, 슬롯 자격증명이 살아 있으면 고정 토큰이 없어도 실행 가능하므로 차단하지 않는다.
+    local _slot_cred_available=""
+    _slot_cred_available="$(slot_credentials_file 1 || slot_credentials_file 2 || true)"
+    if [[ -z "$TOKEN_1" && -z "$TOKEN_2" && -z "$_slot_cred_available" ]]; then
         log "FATAL: ANTHROPIC_AUTH_TOKEN / _2 모두 비어있음 — job=$job_id 실패 처리"
         db_update "UPDATE pipeline_jobs SET status='error', phase='token_missing',
                    error_detail='token_missing',
@@ -1444,9 +1497,21 @@ run_job() {
         local cycle_num=$(( attempt / 2 + 1 ))
 
         # 계정 스위치: 토큰 교체 (R-AUTH)
+        # 1순위 — 릴레이 슬롯 자격증명(refresh 가능). CLI 가 만료 전 스스로 갱신하므로
+        #          고정 토큰처럼 한 번 죽으면 끝나는 상태가 되지 않는다.
+        # 2순위 — .env 고정 oat 토큰 (슬롯이 없는 서버의 기존 경로).
         # Claude Code CLI는 OAuth 토큰을 CLAUDE_CODE_OAUTH_TOKEN으로 받아야 한다.
         # oat 토큰을 ANTHROPIC_API_KEY에 넣으면 x-api-key 경로로 전송되어 Invalid API key가 발생한다.
-        if [[ "$token_slot" == "2" && -n "$TOKEN_2" ]]; then
+        local slot_cred_file=""
+        slot_cred_file="$(slot_credentials_file "$token_slot" || true)"
+        if [[ -n "$slot_cred_file" ]]; then
+            # 래퍼가 격리 HOME 에 자격증명을 staging 하고 CLAUDE_CODE_OAUTH_TOKEN 을 unset 한다.
+            # 여기서 고정 토큰을 export 하면 래퍼가 지우기 전까지 우선순위가 뒤집히므로 지운다.
+            unset CLAUDE_CODE_OAUTH_TOKEN 2>/dev/null || true
+            unset ANTHROPIC_API_KEY 2>/dev/null || true
+            unset ANTHROPIC_BASE_URL 2>/dev/null || true
+            log "  TOKEN_SWITCH job=$job_id → 계정${token_slot} via slot_credentials (refreshable)"
+        elif [[ "$token_slot" == "2" && -n "$TOKEN_2" ]]; then
             export CLAUDE_CODE_OAUTH_TOKEN="$TOKEN_2"
             unset ANTHROPIC_API_KEY 2>/dev/null || true
             unset ANTHROPIC_BASE_URL 2>/dev/null || true
@@ -1582,8 +1647,29 @@ ${safe_instruction}"
             if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
                 claude_args+=(--dangerously-skip-permissions)
             fi
-            timeout "$MAX_RUNTIME" claude "${claude_args[@]}" "$safe_instruction" \
-                > "$output_file" 2> "$err_file" &
+            if [[ -n "$slot_cred_file" ]]; then
+                # timeout 을 래퍼 안쪽에 두어야 한다. 바깥에 두면 래퍼만 죽고
+                # 실제 claude 자식이 고아로 남아 MAX_RUNTIME 이 무의미해진다.
+                # 래퍼는 종료 시 갱신된 자격증명을 원본 슬롯 파일로 되돌려 쓴다.
+                # env -u 로 자식 프로세스에서만 고정 토큰을 지운다.
+                # 2026-09-14 실측: ~/.claude/current.env 가 죽은 ANTHROPIC_AUTH_TOKEN(_2) 를
+                # export 하는데 CLI 는 이 고정 토큰을 slot credential 보다 우선한다.
+                #   "claude.ai connectors are disabled because ANTHROPIC_API_KEY or another
+                #    auth source is set and takes precedence over your claude.ai login"
+                # 이 다섯 개를 지우지 않으면 슬롯 자격증명이 살아 있어도 전량 실패한다.
+                # 셸 자체를 unset 하지 않는 이유는 뒤따르는 레거시 폴백 시도를 망가뜨리지 않기 위함이다.
+                CLAUDE_OAUTH_SLOT="$token_slot" \
+                CLAUDE_SLOT_CREDENTIALS_FILE="$slot_cred_file" \
+                env -u CLAUDE_CODE_OAUTH_TOKEN \
+                    -u ANTHROPIC_AUTH_TOKEN -u ANTHROPIC_AUTH_TOKEN_2 \
+                    -u ANTHROPIC_API_KEY -u ANTHROPIC_BASE_URL \
+                    "$CLAUDE_SLOT_CREDENTIAL_WRAPPER" \
+                    timeout "$MAX_RUNTIME" "$RUNNER_CLAUDE_CLI_BIN" "${claude_args[@]}" "$safe_instruction" \
+                    < /dev/null > "$output_file" 2> "$err_file" &
+            else
+                timeout "$MAX_RUNTIME" "$RUNNER_CLAUDE_CLI_BIN" "${claude_args[@]}" "$safe_instruction" \
+                    > "$output_file" 2> "$err_file" &
+            fi
             local claude_pid=$!
         fi
 
```

---

## 6. 재발 방지 권고 (본 세션 범위 밖 — 구현하지 않음)

읽기 전용 지시와 검수 기준 4번("지시서에 명시된 파일 밖 변경은 FAIL")은
`pipeline_c` 가 공유 워크트리에서 도는 한 구조적으로 충돌한다.
규칙 문서로만 막으면 또 일어난다(R-ERRBOOK). 코드로 막아야 할 후보 3가지:

| # | 조치 | 위치 | 효과 |
|---|---|---|---|
| 1 | 작업 시작 시각의 `git stash create` 기준선을 잡고, 검수에는 **기준선 대비 diff** 만 전달 | `pipeline_runner_service.py` `git_diff` 수집부(836·972줄) | 타 세션 미커밋분이 산출물로 오인되지 않음 |
| 2 | 지시가 읽기 전용이면(러너에 이미 `is_read_only_instruction()` 존재) 검수 프롬프트에 "변경 0건이 정상" 을 명시 | 검수 프롬프트 생성부(1979줄 부근) | 기준 3번("변경사항 없으면 FAIL")과의 충돌 제거 |
| 3 | `diff_text` 절단 시 `... (N bytes truncated)` 표기 | `pipeline_runner_service.py:1979` | 검수자가 "보고서가 잘랐다" 고 오판하지 않음 |
| 4 | `pipeline_c` 도 러너 job 처럼 `/tmp` 격리 워크트리에서 실행 | 러너 `pipeline_c` 경로 | 근본 해결. 영향 범위가 커서 별도 지시 필요 |

오류 사전 등록: `review.shared_worktree_diff_misattribution` (§2-1 참조).
`fix` 없이 `prevention` 만 있는 항목이므로 R-ERRBOOK 기준으로 **"또 일어난다"** 로 읽어야 한다.

## 7. 본 세션이 하지 않은 것 (명시)

- `scripts/pipeline-runner.sh` 원복/수정 — 하지 않음 (§4)
- 어떤 소스·설정 파일의 생성/수정/삭제 — 하지 않음
- 커밋·푸시·배포·서비스 재시작 — 하지 않음
- `pipeline_runner_service.py` 하네스 수정 — 하지 않음 (§6 권고만)
- HANDOVER.md 갱신 — 하지 않음 (읽기 전용 지시 우선. R-001 적용 여부는 CEO 판단)
