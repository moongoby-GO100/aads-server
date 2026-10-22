#!/usr/bin/env bash
# AADS 스캔을 origin/main 전용 clean 클론에서 한 번 실행한다. DB 에는 적재하지 않는다.
#
# 소스 = $AAG_STATE_DIR/src/aads-server (기본 /var/lib/aads/aag/src/aads-server).
# 호스트 체크아웃(/root/aads/aads-server)은 origin URL 조회 외에는 읽지도 쓰지도 않는다.
# 프런트(`../aads-dashboard/src`)는 같은 src 디렉터리의 aads-dashboard 전용 클론에서 읽는다.
#
# 이 스크립트는 스캔까지만 한다. 적재는 호출자가 aag_snapshot_push.py 로 한다 —
# 그래서 이 스크립트를 그대로 단독 실행하면 dry-run 이다.
#
# 종료코드: 0=스캔 완료(결함 유무와 무관) / 1=소스 준비 오류 / 3=소스 거부(dirty 등) /
#           4=스캔 실패 / 7=SKIP(다른 실행 중)
# 표준출력 마지막 줄: AAG_SCAN_AADS head=… expected=… graph=…
set -uo pipefail
export PYTHONDONTWRITEBYTECODE=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${AAG_STATE_DIR:-/var/lib/aads/aag}"
SRC_BASE="${AAG_SRC_BASE:-$STATE_DIR/src}"
SERVER_SRC="${AAG_AADS_SRC_DIR:-$SRC_BASE/aads-server}"
DASH_SRC="${AAG_DASHBOARD_SRC_DIR:-$(dirname "$SERVER_SRC")/aads-dashboard}"
HOST_SERVER="${AAG_HOST_SERVER_DIR:-/root/aads/aads-server}"
HOST_DASH="${AAG_HOST_DASHBOARD_DIR:-/root/aads/aads-dashboard}"
BRANCH="${AAG_SOURCE_BRANCH:-main}"
# 호스트 .git 이 9.5GB — 전체 이력 사본 대신 blob 을 필요할 때 받는 partial clone.
CLONE_FILTER="${AAG_SOURCE_CLONE_FILTER-blob:none}"
PREPARE_PY="${AAG_PREPARE_SOURCE_PY:-$HERE/aag_prepare_source.py}"
OUT_DIR="${AAG_AADS_OUT_DIR:-$STATE_DIR/aads}"
SCANNER_DIR="${AAG_SCANNER_DIR:-$SERVER_SRC/tools/aag}"

prepare() { # dest origin_url|origin_from
    local dest="$1" origin_arg="$2" origin_val="$3" rc=0
    python3 "$PREPARE_PY" --dest "$dest" "$origin_arg" "$origin_val" --branch "$BRANCH" \
        --clone-filter "$CLONE_FILTER" || rc=$?
    return "$rc"
}

if [[ -n "${AAG_AADS_ORIGIN_URL:-}" ]]; then
    server_origin=(--origin-url "$AAG_AADS_ORIGIN_URL")
else
    server_origin=(--origin-from "$HOST_SERVER")
fi
if [[ -n "${AAG_DASHBOARD_ORIGIN_URL:-}" ]]; then
    dash_origin=(--origin-url "$AAG_DASHBOARD_ORIGIN_URL")
else
    dash_origin=(--origin-from "$HOST_DASH")
fi

rc=0
prepare "$SERVER_SRC" "${server_origin[@]}" || rc=$?
((rc == 0)) || { echo "AAG_SCAN_AADS_ABORT stage=prepare_server rc=$rc"; exit "$rc"; }
# 프런트 소스가 없으면 스캐너가 rc=2 로 막는다(fail-closed) — 여기서 먼저 멈춘다.
prepare "$DASH_SRC" "${dash_origin[@]}" || rc=$?
((rc == 0)) || { echo "AAG_SCAN_AADS_ABORT stage=prepare_dashboard rc=$rc"; exit "$rc"; }

head="$(git -C "$SERVER_SRC" rev-parse HEAD)"
expected="$(git -C "$SERVER_SRC" rev-parse "refs/remotes/origin/$BRANCH")"
mkdir -p "$OUT_DIR"
graph="$OUT_DIR/aads-graph.json"
# 이전 산출물이 남아 있으면 스캔 실패 후에도 낡은 그래프가 적재될 수 있다.
rm -f "$graph"

# 스캐너 종료코드: 0=결함 없음 / 1=결함 있음 / 2=실행 불가. Python 예외도 1 이므로
# 아래에서 그래프 파일과 SHA 를 따로 확인한다.
AAG_PROJECT=AADS AAG_REPOSITORY_ID=aads-server \
AAG_TARGET_REF="refs/heads/$BRANCH" \
AADS_COMMIT_SHA="$head" AAG_EXPECTED_TARGET_REF_HEAD_SHA="$expected" \
    timeout 900 python3 "$SCANNER_DIR/scan_aads.py" \
        --root "$SERVER_SRC" --rules "$SCANNER_DIR/rules_aads.yml" \
        --out-dir "$OUT_DIR" --json >"$OUT_DIR/scan.stdout" 2>"$OUT_DIR/scan.stderr"
scan_rc=$?
if ((scan_rc >= 2)) || [[ ! -s "$graph" ]]; then
    echo "AAG_SCAN_AADS_ABORT stage=scan rc=$scan_rc detail=$(tail -3 "$OUT_DIR/scan.stderr" | tr '\n' ' ')"
    exit 4
fi
if ! python3 - "$graph" "$head" <<'PY'
import json
import sys

graph = json.load(open(sys.argv[1], encoding="utf-8"))
actual = (graph.get("source_identity") or {}).get("resolved_commit_sha")
if actual != sys.argv[2]:
    raise SystemExit("graph SHA %s != scanned HEAD %s" % (actual, sys.argv[2]))
PY
then
    echo "AAG_SCAN_AADS_ABORT stage=verify_graph_sha rc=4"
    exit 4
fi
echo "AAG_SCAN_AADS head=$head expected=$expected graph=$graph scan_rc=$scan_rc"
