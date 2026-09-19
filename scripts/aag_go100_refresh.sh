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
# ═══════════════════════════════════════════════════════════════════════
set -eo pipefail

REMOTE="${AAG_GO100_REMOTE:-root@5.104.86.14}"
REMOTE_DIR="${AAG_GO100_REMOTE_DIR:-/root/aag-go100}"
REPO_DIR="${AAG_GO100_REPO_DIR:-/root/aads/aads-server}"
TARGET_ROOT="${AAG_GO100_TARGET_ROOT:-/root/kis-autotrade-v4}"
OUT_DIR="${AAG_GO100_OUT_DIR:-/var/lib/aads/aag/go100}"
LOG="${AAG_GO100_LOG:-/var/log/aads-pipeline/aag-go100.log}"
SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=15)

mkdir -p "$(dirname "$LOG")" "$OUT_DIR" 2>/dev/null || true
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $*" | tee -a "$LOG"; }

# 중복 실행 방지 — 스캔이 주기보다 길어져도 겹치지 않는다.
exec 9>"/tmp/aag-go100-refresh.lock"
if ! flock -n 9; then
    log "already running — skip"
    exit 0
fi

log "START remote=${REMOTE} target=${TARGET_ROOT}"

# 1) 스캐너·규칙 정본을 원격으로 동기화 (사본 드리프트 방지)
ssh "${SSH_OPTS[@]}" "$REMOTE" "mkdir -p ${REMOTE_DIR}/out"
scp "${SSH_OPTS[@]}" -q \
    "${REPO_DIR}/tools/aag/scan_aads.py" \
    "${REPO_DIR}/tools/aag/brief.py" \
    "${REPO_DIR}/tools/aag/rules_go100.yml" \
    "${REMOTE}:${REMOTE_DIR}/"

# 2) 원격 스캔. 시간 상한을 반드시 건다 (R-BG).
#
# 종료코드 계약: 0=결함 없음 / 1=결함 있음 / 2=실행 불가(scan_aads.py:2070).
# 1 을 실패로 읽으면 결함이 하나라도 있는 한 갱신이 영원히 안 돈다 — 실제로
# 첫 실행이 그렇게 죽었다(2026-09-19). "점검 못 함"과 "위반 있음"을 구분한다.
set +e
ssh "${SSH_OPTS[@]}" "$REMOTE" \
    "cd ${REMOTE_DIR} && timeout 900 python3 scan_aads.py --root ${TARGET_ROOT} --rules ${REMOTE_DIR}/rules_go100.yml --out-dir ${REMOTE_DIR}/out --json" \
    > /tmp/aag-go100-scan.json 2>/tmp/aag-go100-scan.err
scan_rc=$?
set -e
if (( scan_rc >= 2 )); then
    log "SCAN_FAILED rc=${scan_rc} — $(grep -v SyntaxWarning /tmp/aag-go100-scan.err | tail -3 | tr '\n' ' ')"
    exit 1
fi

# 3) 산출물 회수. 세 파일이 모두 와야 성공으로 본다.
scp "${SSH_OPTS[@]}" -q \
    "${REMOTE}:${REMOTE_DIR}/out/go100-graph.json" \
    "${REMOTE}:${REMOTE_DIR}/out/go100-findings.md" \
    "${REMOTE}:${REMOTE_DIR}/out/go100-arch.mmd" \
    "${OUT_DIR}/"

for f in go100-graph.json go100-findings.md go100-arch.mmd; do
    [[ -s "${OUT_DIR}/${f}" ]] || { log "MISSING_ARTIFACT ${f}"; exit 1; }
done

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
#    생산자 부재였다. 적재가 실패해도 산출물 회수는 성공으로 남긴다(파일은
#    이미 최신이다). 대신 로그에 남겨 조용히 낡지 않게 한다.
if python3 "${REPO_DIR}/scripts/aag_snapshot_push.py" \
    "GO100=${OUT_DIR}/go100-graph.json" >>"$LOG" 2>&1; then
    log "SNAPSHOT_PUSHED"
else
    log "SNAPSHOT_PUSH_FAILED — aag_findings 가 local_graph 폴백으로 답한다"
fi

log "DONE ${summary}"
