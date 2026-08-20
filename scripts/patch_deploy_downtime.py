#!/usr/bin/env python3
"""deploy.sh에 downtime_seconds 자동 측정/기록을 주입하는 1회성 패치 스크립트.

배경: migration 123으로 deploy_history.downtime_seconds 컬럼은 추가됐으나
      값을 기록하는 코드가 없어 항상 DEFAULT 0이었다. (2026-08-20 인시던트 재발 방지 P1-C)

멱등성: 이미 패치된 파일(DOWNTIME_FILE 존재)이면 아무것도 하지 않고 종료한다.
사용법: python3 scripts/patch_deploy_downtime.py [deploy.sh 경로]
"""
import sys
import shutil
import pathlib

TARGET = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/root/aads/aads-server/deploy.sh")

DOWNTIME_BLOCK = '''
# ── 다운타임 자동 측정 (2026-08-20 Blue/Green 동시 다운 인시던트 재발 방지) ──
# nginx를 통한 실제 사용자 경로를 1초 주기로 폴링해 실패 구간을 누적한다.
# 해상도는 프로브 응답시간 + 1초(대략 ±4초). 게이트가 아니라 계측 용도다.
DOWNTIME_FILE="${COMPOSE_DIR}/.deploy_downtime"
DOWNTIME_PROBE_URL="${DOWNTIME_PROBE_URL:-http://127.0.0.1/api/v1/health}"
DOWNTIME_PROBE_HOST="${DOWNTIME_PROBE_HOST:-aads.newtalk.kr}"
DOWNTIME_MONITOR_PID=""

start_downtime_monitor() {
    echo 0 > "$DOWNTIME_FILE" 2>/dev/null || true
    (
        total=0
        while true; do
            t0=$(date +%s)
            if curl -fsS -m 5 -o /dev/null -H "Host: ${DOWNTIME_PROBE_HOST}" "$DOWNTIME_PROBE_URL" 2>/dev/null; then
                :
            else
                t1=$(date +%s)
                total=$(( total + (t1 - t0) + 1 ))
                echo "$total" > "$DOWNTIME_FILE" 2>/dev/null || true
            fi
            sleep 1
        done
    ) &
    DOWNTIME_MONITOR_PID=$!
}

stop_downtime_monitor() {
    if [[ -n "${DOWNTIME_MONITOR_PID:-}" ]]; then
        kill "$DOWNTIME_MONITOR_PID" >/dev/null 2>&1 || true
        DOWNTIME_MONITOR_PID=""
    fi
}

get_downtime_seconds() {
    local v="0"
    if [[ -f "$DOWNTIME_FILE" ]]; then
        v=$(tr -d '[:space:]' < "$DOWNTIME_FILE" 2>/dev/null || echo 0)
    fi
    if [[ ! "$v" =~ ^[0-9]+$ ]]; then
        v="0"
    fi
    echo "$v"
}
'''

ANCHOR_START_EPOCH = 'DEPLOY_START_EPOCH=$(date +%s)\n'

OLD_LOCALS = '''    local status_sql
    local err_sql
'''
NEW_LOCALS = '''    local status_sql
    local err_sql
    local downtime
'''

OLD_DURATION = '''    if [[ "$status" == "started" ]]; then
        duration=0
    fi
'''
NEW_DURATION = '''    if [[ "$status" == "started" ]]; then
        duration=0
    fi
    downtime=$(get_downtime_seconds)
    if [[ "$status" == "started" ]]; then
        downtime=0
    fi
'''

OLD_INSERT = (
    'INSERT INTO deploy_history(deploy_type,project,trigger_by,git_commit,git_message,'
    "status,duration_s,error_msg,created_at) VALUES('$type_sql','AADS','deploy.sh',"
    "'$commit_sql','$msg_sql','$status_sql',$duration,'$err_sql',NOW())"
)
NEW_INSERT = (
    'INSERT INTO deploy_history(deploy_type,project,trigger_by,git_commit,git_message,'
    "status,duration_s,error_msg,downtime_seconds,created_at) VALUES('$type_sql','AADS','deploy.sh',"
    "'$commit_sql','$msg_sql','$status_sql',$duration,'$err_sql',$downtime,NOW())"
)

OLD_ERR_TRAP = '''    local command="${2:-unknown}"
    record_deploy "failed" "$MODE" "unexpected error exit=${exit_code} line=${line_no}: ${command:0:300}"
'''
NEW_ERR_TRAP = '''    local command="${2:-unknown}"
    stop_downtime_monitor
    record_deploy "failed" "$MODE" "unexpected error exit=${exit_code} line=${line_no}: ${command:0:300}"
'''

OLD_EXIT_TRAP = 'trap "rm -f $LOCKFILE" EXIT\n'
NEW_EXIT_TRAP = 'trap "stop_downtime_monitor; rm -f $LOCKFILE" EXIT\n'

OLD_STARTED = 'record_deploy "started" "$MODE" ""\n'
NEW_STARTED = 'record_deploy "started" "$MODE" ""\nstart_downtime_monitor\n'

OLD_SUCCESS = 'record_deploy "success" "$MODE" ""\n'
NEW_SUCCESS = 'stop_downtime_monitor\nrecord_deploy "success" "$MODE" ""\n'


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"[FAIL] {label}: 앵커 {count}회 매치 (정확히 1회여야 함)")
    print(f"[OK] {label}")
    return text.replace(old, new, 1)


def main() -> None:
    if not TARGET.exists():
        raise SystemExit(f"[FAIL] 대상 파일 없음: {TARGET}")

    src = TARGET.read_text(encoding="utf-8")

    if "DOWNTIME_FILE=" in src:
        print("[SKIP] 이미 패치됨 — 변경 없음")
        return

    src = replace_once(src, ANCHOR_START_EPOCH, ANCHOR_START_EPOCH + DOWNTIME_BLOCK, "다운타임 측정 블록 삽입")
    src = replace_once(src, OLD_LOCALS, NEW_LOCALS, "record_deploy local 선언")
    src = replace_once(src, OLD_DURATION, NEW_DURATION, "record_deploy downtime 계산")
    src = replace_once(src, OLD_INSERT, NEW_INSERT, "deploy_history INSERT 컬럼 추가")
    src = replace_once(src, OLD_ERR_TRAP, NEW_ERR_TRAP, "ERR trap 모니터 정지")
    src = replace_once(src, OLD_EXIT_TRAP, NEW_EXIT_TRAP, "EXIT trap 모니터 정지")
    src = replace_once(src, OLD_STARTED, NEW_STARTED, "배포 시작 시 모니터 기동")
    src = replace_once(src, OLD_SUCCESS, NEW_SUCCESS, "배포 성공 전 모니터 정지")

    backup = TARGET.with_suffix(TARGET.suffix + ".bak_downtime")
    shutil.copy2(TARGET, backup)
    TARGET.write_text(src, encoding="utf-8")
    print(f"[DONE] 패치 완료 — 백업: {backup}")


if __name__ == "__main__":
    main()
