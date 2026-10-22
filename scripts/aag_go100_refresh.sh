#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# AAG GO100 스냅샷 갱신 (contabo116 에서 실행 → contabo14 스캔 → 결과 회수)
#
# 왜 116 에서 도는가: 116→14 SSH 는 이미 열려 있고(claude-lease-push 가 쓴다),
# 반대 방향은 보장되지 않는다. 스캐너 정본은 이 저장소의 tools/aag 이므로
# 매 실행마다 14 로 밀어넣어 사본이 낡지 않게 한다.
#
# 산출물은 어느 Git 저장소에도 쓰지 않는다. GO100 저장소에 쓰면 러너의
# UNTRACKED_IGNORED_RESCUE 가 남의 커밋으로 끌어갈 수 있고, AADS 저장소의
# reports/aag 에 쓰면 매시간 tracked 파일이 dirty 가 되어 배포를 막는다.
#
# 실행: cron (매시간 25분). 수동 1회 실행도 같은 명령이다.
#   bash /root/aads/aads-server/scripts/aag_go100_refresh.sh
#
# 종료코드 계약 (aag_all_projects_refresh.sh 가 그대로 해석한다)
#   0  스캔 + 산출물 회수 + DB 적재까지 모두 성공 — 유일한 freshness 증거
#   1  스캔·회수·검증 실패
#   7  SKIP — 중복 실행(lock/owner) 또는 장중 회피(평일 08:00~15:40 KST).
#      실패도 성공도 아니다. 스캔하지 않았으므로 freshness 증거가 아니다.
#   8  스캔·회수는 성공했으나 DB 적재 실패 (aag_findings 는 낡은 채로 남는다)
# 장중 회피는 우회 옵션이 없다. 수동 강제 scan 도 같은 게이트를 지난다.
# ═══════════════════════════════════════════════════════════════════════
set -eo pipefail

REMOTE="${AAG_GO100_REMOTE:-root@5.104.86.14}"
REMOTE_DIR="${AAG_GO100_REMOTE_DIR:-/root/aag-go100}"
REPO_DIR="${AAG_GO100_REPO_DIR:-/root/aads/aads-server}"
TARGET_ROOT="${AAG_GO100_TARGET_ROOT:-/root/kis-autotrade-v4}"
OUT_DIR="${AAG_GO100_OUT_DIR:-/var/lib/aads/aag/go100}"
LOG="${AAG_GO100_LOG:-/var/log/aads-pipeline/aag-go100.log}"
STATE_DIR="${AAG_STATE_DIR:-/var/lib/aads/aag}"
TMP_DIR="${AAG_GO100_TMP_DIR:-/tmp}"
SCHED_PY="${AAG_SCHEDULER_STATE_PY:-${REPO_DIR}/scripts/aag_scheduler_state.py}"
SCHED_OWNER="go100-refresh:$$:$(date +%s)"
SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=15)
EXIT_SKIPPED=7
EXIT_INGEST_FAILED=8

mkdir -p "$(dirname "$LOG")" "$OUT_DIR" 2>/dev/null || true
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $*" | tee -a "$LOG"; }

sched() { AAG_STATE_DIR="$STATE_DIR" python3 "$SCHED_PY" "$@"; }

# 중복 실행 방지 — 스캔이 주기보다 길어져도 겹치지 않는다.
# skip 은 exit 0 이 아니다. 0 으로 끝나면 상위가 success 로 읽는다(2026-10-20).
exec 9>"${TMP_DIR}/aag-go100-refresh.lock"
if ! flock -n 9; then
    sched skip GO100 --reason already_running >/dev/null 2>&1 || true
    log "SKIPPED reason=already_running rc=${EXIT_SKIPPED} — not a success, no freshness evidence"
    exit "$EXIT_SKIPPED"
fi

# owner/heartbeat 기록 + 장중 회피 게이트. 게이트가 깨지면 열린 채로 두지 않는다
# (장중에 원격 scan 이 도는 쪽이 더 나쁘다).
sched_rc=0
sched begin GO100 --owner "$SCHED_OWNER" --pid "$$" --market-guard >/dev/null 2>>"$LOG" || sched_rc=$?
if (( sched_rc == EXIT_SKIPPED )); then
    log "SKIPPED reason=$(sched status GO100 --field last_skip_reason 2>/dev/null) rc=${EXIT_SKIPPED} — not a success, no freshness evidence"
    exit "$EXIT_SKIPPED"
elif (( sched_rc != 0 )); then
    log "SCHEDULER_STATE_ERROR rc=${sched_rc} — refusing to scan without the market-hours gate"
    exit 1
fi

OUTCOME=failed
OUTCOME_REASON="aborted"
on_exit() {
    local rc=$?
    sched finish GO100 --owner "$SCHED_OWNER" --outcome "$OUTCOME" \
        --reason "$OUTCOME_REASON" --exit-code "$rc" >/dev/null 2>&1 || true
}
trap on_exit EXIT
beat() { sched heartbeat GO100 --owner "$SCHED_OWNER" >/dev/null 2>&1 || true; }

log "START remote=${REMOTE} target=${TARGET_ROOT}"

# 1) 스캐너·규칙 정본을 원격으로 동기화 (사본 드리프트 방지)
ssh "${SSH_OPTS[@]}" "$REMOTE" "mkdir -p ${REMOTE_DIR}/out"
scp "${SSH_OPTS[@]}" -q \
    "${REPO_DIR}/tools/aag/scan_aads.py" \
    "${REPO_DIR}/tools/aag/v2_contract.py" \
    "${REPO_DIR}/tools/aag/brief.py" \
    "${REPO_DIR}/tools/aag/rules_go100.yml" \
    "${REMOTE}:${REMOTE_DIR}/"
beat

# 2) 원격 스캔. 시간 상한을 반드시 건다 (R-BG).
#
# 종료코드 계약: 0=결함 없음 / 1=결함 있음 / 2=실행 불가(scan_aads.py:2070).
# 1 을 실패로 읽으면 결함이 하나라도 있는 한 갱신이 영원히 안 돈다 — 실제로
# 첫 실행이 그렇게 죽었다(2026-09-19). "점검 못 함"과 "위반 있음"을 구분한다.
scan_marker="${REMOTE_DIR}/.scan-started"
remote_sha=$(ssh "${SSH_OPTS[@]}" "$REMOTE" \
    "git -C ${TARGET_ROOT} rev-parse HEAD")
ssh "${SSH_OPTS[@]}" "$REMOTE" "touch ${scan_marker}"

set +e
ssh "${SSH_OPTS[@]}" "$REMOTE" \
    "cd ${REMOTE_DIR} && AAG_PROJECT=GO100 AADS_COMMIT_SHA=${remote_sha} AAG_EXPECTED_TARGET_REF_HEAD_SHA=${remote_sha} timeout 900 python3 scan_aads.py --root ${TARGET_ROOT} --rules ${REMOTE_DIR}/rules_go100.yml --out-dir ${REMOTE_DIR}/out --json" \
    > ${TMP_DIR}/aag-go100-scan.json 2>${TMP_DIR}/aag-go100-scan.err
scan_rc=$?
set -e
beat
if (( scan_rc >= 2 )); then
    OUTCOME_REASON="scan_failed rc=${scan_rc}"
    log "SCAN_FAILED rc=${scan_rc} — $(grep -v SyntaxWarning ${TMP_DIR}/aag-go100-scan.err | tail -3 | tr '\n' ' ')"
    exit 1
fi

# Python 예외도 rc=1 이므로 종료코드만으로는 "결함 있음"과 구분할 수 없다.
# 이번 실행이 실제로 새 그래프를 썼는지 확인해 이전 산출물 재적재를 차단한다.
if ! ssh "${SSH_OPTS[@]}" "$REMOTE" \
    "test ${REMOTE_DIR}/out/go100-graph.json -nt ${scan_marker}"; then
    OUTCOME_REASON="stale_artifact"
    log "SCAN_FAILED rc=${scan_rc} — fresh graph artifact was not produced"
    exit 1
fi

# 3) 산출물 회수. 세 파일이 모두 와야 성공으로 본다.
scp "${SSH_OPTS[@]}" -q \
    "${REMOTE}:${REMOTE_DIR}/out/go100-graph.json" \
    "${REMOTE}:${REMOTE_DIR}/out/go100-findings.md" \
    "${REMOTE}:${REMOTE_DIR}/out/go100-arch.mmd" \
    "${OUT_DIR}/"
beat

for f in go100-graph.json go100-findings.md go100-arch.mmd; do
    [[ -s "${OUT_DIR}/${f}" ]] || { OUTCOME_REASON="missing_artifact"; log "MISSING_ARTIFACT ${f}"; exit 1; }
done

if ! python3 - "${OUT_DIR}/go100-graph.json" "${remote_sha}" <<'PY'
import json
import sys

graph = json.load(open(sys.argv[1], encoding="utf-8"))
identity = graph.get("source_identity") or {}
actual = identity.get("resolved_commit_sha") or graph.get("commit_sha")
if actual != sys.argv[2]:
    raise SystemExit("stale graph SHA: %s != %s" % (actual, sys.argv[2]))
PY
then
    OUTCOME_REASON="sha_mismatch"
    log "SCAN_FAILED — graph source SHA does not match the scanned repository HEAD"
    exit 1
fi

summary=$(python3 -c "
import json,collections,sys
g=json.load(open('${OUT_DIR}/go100-graph.json'))
c=collections.Counter(f.get('severity') for f in g['findings'])
print('generated=%s findings=%d %s routes=%s' % (
    g['generated_at'], len(g['findings']),
    ' '.join('%s=%d' % kv for kv in sorted(c.items())),
    g['stats'].get('mounted_routes')))
" 2>/dev/null || echo "summary_unavailable")

# 4) DB 적재. 이게 없으면 스캔은 도는데 `aag_findings` 는 영원히 local_graph
#    폴백으로만 답한다 — 2026-09-19 `aag_graph_snapshots` 0건의 원인이 바로
#    생산자 부재였다. 적재 실패는 성공이 아니다 — 산출물 파일은 최신이어도
#    DB 의 pointer 는 낡은 채이므로 exit 8 로 상위에 알리고 freshness 증거로
#    세지 않는다. (예전에는 exit 0 이라 상위가 result=success 로 적었다.)
push_rc=0
python3 "${REPO_DIR}/scripts/aag_snapshot_push.py" \
    "GO100=${OUT_DIR}/go100-graph.json" >>"$LOG" 2>&1 || push_rc=$?
if (( push_rc == 0 )); then
    log "SNAPSHOT_PUSHED"
elif (( push_rc == 9 )); then
    # 적재는 됐으나 DB 가 run 을 거부했다(source_behind 등). pointer 는 그대로다.
    OUTCOME_REASON="snapshot_rejected"
    log "SNAPSHOT_REJECTED rc=${EXIT_INGEST_FAILED} — DB 가 이 run 을 거부했다. RESULT 줄을 확인하라"
    exit "$EXIT_INGEST_FAILED"
else
    OUTCOME_REASON="snapshot_push_failed"
    log "SNAPSHOT_PUSH_FAILED rc=${EXIT_INGEST_FAILED} — aag_findings 가 local_graph 폴백으로 답한다"
    exit "$EXIT_INGEST_FAILED"
fi

OUTCOME=success
OUTCOME_REASON="scan_fetch_publish_ok"
log "DONE ${summary}"
