# AADS-RUNNER-SLOT-AUTH — READ-ONLY SMOKE RESULT

- 작성: 2026-09-14 (검수 피드백 반영본)
- 대상 지시: `READ-ONLY SMOKE 2026-09-14 12:27 KST — 러너 슬롯 자격증명 전환(AADS-RUNNER-SLOT-AUTH) E2E 검증용`
- 스모크 세션: `1349cc83-67de-4bdb-a299-b17c125abfdc`
- 판정: **SLOT_AUTH_SMOKE_PASS**

---

## 1. 명령 출력 (원문 그대로)

1. `pwd`

```
/root/aads/aads-server
```

2. `date '+%F %T %Z'`

```
2026-09-14 05:26:43 CEST
```

> 지시서 헤더는 `12:27 KST`, 셸은 `CEST` 를 반환한다. 05:26 CEST = 12:26 KST 이므로
> 시각은 1분 차이로 일치한다. 시계 문제가 아니라 실행 환경 TZ 가 CEST 일 뿐이다.
> 러너 슬롯 로그를 KST 로 대조한다면 +7h 를 감안해야 한다.

---

## 2. 검수 피드백 4건 대응

### 2-1. "원래 지시 위반 — 스모크가 pipeline-runner.sh 를 60줄 고쳤다" → **사실이 아니다**

스모크 세션은 파일을 하나도 건드리지 않았다. 세션 트랜스크립트 전수 조사 결과다.

| 근거 | 값 |
|---|---|
| 스모크 세션 수명 | `2026-09-14T03:26:39Z` → `03:26:49Z` (10초) |
| 스모크 세션의 전체 도구 호출 | `Bash` 2회 — `pwd`, `date '+%F %T %Z'` |
| `Edit`/`Write`/`NotebookEdit` 호출 | **0회** |
| `scripts/pipeline-runner.sh` mtime | `2026-09-14 03:24:30Z` (05:24:30 CEST) |
| 같은 파일의 **그 다음** 수정 | `05:29:47 CEST` — 스모크 종료(05:26:49) **3분 후** |

파일 수정 시각이 세션 시작보다 **2분 09초 앞선다.** 아직 시작하지도 않은 세션이
파일을 고칠 수는 없다. 같은 분(03:24)에 `/root/aads/vendor/claude-cli/claude`
(216MB) 도 함께 배치됐다 — diff 가 참조하는 바로 그 경로다. 즉 스모크와 무관한
별개의 AADS-RUNNER-SLOT-AUTH 구현 작업이 스모크 직전에 워킹트리에 남긴 변경이다.

결정적으로, **그 작업은 아직 진행 중이다.** 10초짜리 스모크가 끝나고 3분 뒤인
05:29:47 에 같은 파일이 또 커졌다(+80 → +90). 이미 종료된 세션은 파일을 더 고칠 수
없다. 즉 저자는 스모크가 아니라 지금도 이 공유 워킹트리에서 작업 중인 다른 세션이다.

diff 가 참조하는 `scripts/claude-slot-credentials-wrapper.sh` 는 **09-12 에 이미
커밋된 기존 파일**이다(신규 아님). 슬롯 자격증명 기반 자체가 스모크 이전부터 있었다.

### 2-2. "RESULT 파일 누락" → **이 파일로 제출한다**

지시서의 STEP 0 조사표와 검증 체크리스트를 아래 4·5절에 파일로 기재했다.
응답 본문 체크리스트만 제출했던 것이 불충분했다는 지적은 맞다.

### 2-3. "Git Diff 불완전" → **전문을 7절에 첨부한다**

이전 보고는 3번째 hunk 중간(`local TOKEN_2="${ANTHROPIC_AUTH_TOKEN_`)에서 잘렸다.
7절은 손으로 옮기지 않고 `git diff` 출력을 그대로 파일에 덧붙인 전문이다.

**단, 이 파일은 지금도 다른 세션이 고치고 있어 diff 는 고정된 산출물이 아니라
시점 스냅샷이다.** 이 문서를 쓰는 동안에도 두 번 바뀌었다.

| 관측 시각 (CEST) | blob | 규모 |
|---|---|---|
| 05:28:32 | `b96b838a` | 5 hunk / +80 / −4 |
| 05:29:47 | `94f08d44` | 5 hunk / **+90 / −4** |

7절에 실린 것은 **05:29:47 판(`94f08d44`)** 이며, 붙여 넣은 뒤 현재 워킹트리
출력과 문자열 단위로 재대조해 일치를 확인했다.
검증용 체크섬: `git diff scripts/pipeline-runner.sh | sha256sum` =
`ca97851a599999e5982fb407d323d4beeadeb18d3714232caab8fcdcb1e50ad4`
(대조 시점 이후 또 바뀌었다면 이 값은 달라진다 — 그 자체가 2-1 의 증거다.)

### 2-4. "지시 범위 모호성 — 읽기 전용인데 diff 는 기능 구현" → **지적이 정확하다**

피드백 4번의 추정("사전에 이미 존재하던 변경")이 사실이다. 2-1 이 그 증거다.
diff 는 스모크의 산출물이 아니라 스모크가 **관측한 워킹트리 상태**다.
읽기 전용 지시와 diff 사이에 모순은 없다. 모순처럼 보인 것은, 스모크 보고가
"내가 만든 변경이 아니다" 를 명시하지 않고 diff 만 붙였기 때문이다. 그것이 이번
보고의 실제 결함이고, 이 문서로 바로잡는다.

---

## 3. 되돌리지 않은 이유 (중요)

피드백 1번은 문면상 "그 60줄을 되돌려라" 로 읽힌다. **되돌리지 않았다.** 네 가지
이유이며, 모두 비가역 파괴 또는 운영 중단에 해당한다.

1. **남의 미커밋 작업이다.** 이 변경은 스모크 산출물이 아니고 커밋되지도 않았다.
   `git checkout -- scripts/pipeline-runner.sh` 는 reflog 도 남지 않는 영구 삭제다.
   주석에 박힌 2026-09-14 실측 A/B 결과(CLI 2.1.270 주간한도 차단 vs 2.1.259 정상,
   4/4)는 재현에 실제 러너 호출이 필요해 되살릴 수 없다.
2. **살아 있는 러너가 이 파일을 읽고 있다.** PID 947659 가 fd 255 로 이 파일
   (inode 597813) 자체를 붙들고 있다. bash 는 스크립트를 fd 오프셋 기준으로
   조금씩 읽으므로, 실행 중에 90줄을 들어내면 뒤쪽 바이트 오프셋이 전부 밀려
   러너가 깨진 구문을 실행한다.
3. **작업이 아직 끝나지 않았다.** 05:29:47 에도 파일이 커졌다(2-1). 진행 중인
   작업을 중간에 되돌리면 저자는 자기 변경이 사라진 줄 모른 채 그 위에 이어 쓴다.
4. **이미 운영에서 도는 코드다.** 새 경로가 실제 로그를 찍고 있다 —
   `/var/log/aads-pipeline/runner.log` 에 05:26:21 부터
   `TOKEN_SWITCH job=... → 계정N via slot_credentials (refreshable)` **22줄**.
   고정 토큰이 429/401 로 죽은 상태에서 러너를 살리고 있는 인증 장애 수습분이므로,
   되돌리면 러너가 다시 멈춘다.

되돌리려면 러너를 세운 뒤 작성자 확인을 거쳐야 한다. 롤백 절차는 6절에 적었다.
스모크 권한으로 판단할 일이 아니라고 보고, 실행하지 않고 보고만 한다.

---

## 4. STEP 0 기존 구현 조사표

### 4-A. 이번 스모크의 변경 (지시 준수 여부)

| 대상 | 분류 | 비고 |
|---|---|---|
| 저장소 전체 | **유지** | 생성·수정·삭제 0건. 커밋·푸시·빌드·배포 0건 |
| 신규 | **없음** | — |
| 삭제 | **없음** | 삭제 사유 기재 대상 없음 |

이 RESULT 파일(`reports/...RESULT.md`)만이 이번 세션이 만든 유일한 파일이며,
검수 피드백 2번이 명시적으로 요구한 산출물이다. 커밋하지 않았다(지시서의 커밋 금지 유지).

### 4-B. 워킹트리에 이미 있던 변경의 조사표 (`scripts/pipeline-runner.sh`)

작성 주체는 이번 스모크가 아니다(2-1). 검수가 요구한 분류를 관측 기준으로 채운다.

| 기존 접점 | 분류 | 근거 / 영향 |
|---|---|---|
| 설정 블록 (`LOG_DIR`, `ARTIFACT_DIR`, `RUNNER_HOSTNAME`) | **유지** | 기존 3줄 그대로. 아래 변수들이 뒤에 추가됐을 뿐 |
| `CLAUDE_RELAY_SLOT_HOME_ROOT` / `CLAUDE_SLOT_CREDENTIAL_WRAPPER` / `RUNNER_USE_SLOT_CREDENTIALS` | **신규** | 전부 `${VAR:-기본값}` — 미설정 서버는 기존 동작 유지 |
| `RUNNER_CLAUDE_CLI_BIN` | **신규** | 벤더 CLI 경로. `[[ -x ]]` 실패 시 전역 `claude` 로 폴백 |
| `slot_credentials_file()` | **신규** | 기존 동명 함수 없음. 실패 시 1 반환 → 호출측이 고정 토큰 경로로 폴백 |
| `run_job()` 빈 토큰 가드 (C-4) | **수정** | 조건에 `-z "$_slot_cred_available"` 만 AND 로 추가. 토큰 둘 다 없고 슬롯도 없으면 여전히 실패 처리 — 기존 가드 무력화 아님 |
| `run_job()` 계정 스위치 (R-AUTH) | **수정** | 기존 `if [[ "$token_slot" == "2" ... ]]` 를 `elif` 로 내리고 슬롯 경로를 1순위로 앞에 둠. 기존 분기 본문은 그대로 |
| `claude` 실행부 | **수정** | 슬롯 자격증명이 있으면 래퍼 경유, 없으면 기존 `timeout ... "${claude_args[@]}"` 그대로. `claude` → `$RUNNER_CLAUDE_CLI_BIN` 치환뿐. 05:29:47 판에서 슬롯 분기에만 `env -u`(고정 토큰 5개)와 `< /dev/null` 이 추가됐다 — 자식 프로세스 한정이라 폴백 분기는 영향 없음 |
| `normalize_claude_cli_model()` | **유지** | 인접 위치일 뿐 변경 없음 |
| `is_read_only_instruction()` | **유지** | 변경 없음 |
| `scripts/claude-slot-credentials-wrapper.sh` | **유지** | 09-12 커밋된 기존 파일. 이번 변경에서 수정 없음 |
| DB 접점 (`pipeline_jobs` UPDATE) | **유지** | `status='error', phase='token_missing'` 경로 그대로 |

**삭제: 0건.** diff 의 `-` 4줄은 모두 같은 자리에서 확장된 in-place 치환이며
기능 제거가 아니다. 사유 기재가 필요한 삭제 대상이 없다.

| 삭제된 줄 | 실제 성격 |
|---|---|
| `if [[ -z "$TOKEN_1" && -z "$TOKEN_2" ]]; then` | 같은 조건에 AND 항 추가 |
| `if [[ "$token_slot" == "2" && -n "$TOKEN_2" ]]; then` | `elif` 로 강등, 본문 유지 |
| `timeout "$MAX_RUNTIME" claude ... \` | else 분기에 동일 형태로 존속 |
| `> "$output_file" 2> "$err_file" &` | 동일 |

**지시서에 없는 파일 변경**: 없음. 이 RESULT 파일 외 생성물 없음.

---

## 5. 검증 체크리스트

| 항목 | 결과 |
|---|---|
| 구현 목표 | 코드 구현 없음 — 슬롯 자격증명 전환 러너의 실행 환경(작업 디렉터리·시각) 읽기 전용 확인 |
| 검증 방법 | `pwd`, `date '+%F %T %Z'` 2개 명령 (지시서가 허용한 전부) |
| 완료 기준 | 두 명령이 종료코드 0 으로 출력 반환 → **충족** |
| 실패 기준 | 명령 실패 또는 예상 밖 디렉터리 → **해당 없음** (`/root/aads/aads-server` = 지정 작업 디렉터리 일치) |
| 서비스 재시작 확인 | **미실행 / 해당 없음** — 재시작한 서비스가 없다. 지시서가 배포·재시작을 금지했다 |
| 에러 로그 0건 | **미실행 / 해당 없음** — 배포가 없어 대조할 변경 시점이 없다 |
| 브라우저 E2E / 화면 검증 | **해당 없음** — UI·라우트·차트 변경이 없는 셸 명령 스모크. ⚠️ 브라우저 E2E 미실행 |
| 추가 (읽기 전용) 구문 검증 | `bash -n scripts/pipeline-runner.sh` → **OK**. 워킹트리의 미커밋 변경이 최소한 구문상 깨져 있지 않음을 파일 수정 없이 확인 |

---

## 6. 롤백 방법 (실행하지 않음, 절차만 기재)

워킹트리 변경을 되돌리기로 **결정된다면** 순서를 지켜야 한다.

1. 작성자 확인 — 이 변경은 미커밋이고 되돌리면 복구 불가.
2. 러너 정지 — fd 255 로 스크립트를 붙든 PID(관측 시 947659)가 없어야 한다.
   실행 중 되돌리면 오프셋이 밀려 러너가 깨진 구문을 실행한다.
3. 보존 후 되돌리기:
   `git diff scripts/pipeline-runner.sh > /root/aads/_backup_slot_auth_$(date +%s).patch`
   `git checkout -- scripts/pipeline-runner.sh`
4. 러너 재기동 후 새 PID + 새 inode 확인.

반대로 **살리기로** 한다면 `RUNNER_USE_SLOT_CREDENTIALS=0` 으로 코드 변경 없이
기존 고정 토큰 경로로 되돌릴 수 있다(diff 상 무조건 폴백).

---

## 7. Git Diff 전문 (`scripts/pipeline-runner.sh`, 5 hunk / +90 / −4, blob `94f08d44`, 05:29:47 CEST 스냅샷)

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

판정: **SLOT_AUTH_SMOKE_PASS**
