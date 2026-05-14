#!/bin/bash
# Codex CLI device-auth 백그라운드 실행 + 코드 캡처
# 사용법: bash scripts/codex_device_auth.sh

OUTFILE="/tmp/codex_device_auth_output.txt"
PIDFILE="/tmp/codex_device_auth.pid"

# 기존 프로세스 정리
if [ -f "$PIDFILE" ]; then
    kill $(cat "$PIDFILE") 2>/dev/null
    rm -f "$PIDFILE"
fi
rm -f "$OUTFILE"

# device-auth를 백그라운드로 실행 (script로 TTY 에뮬레이션)
nohup script -q -c "codex login --device-auth" "$OUTFILE" > /dev/null 2>&1 &
echo $! > "$PIDFILE"

# 코드 출력 대기 (최대 15초)
for i in $(seq 1 15); do
    sleep 1
    if [ -f "$OUTFILE" ]; then
        CODE=$(grep -oP '[A-Z0-9]{4}-[A-Z0-9]{5}' "$OUTFILE" 2>/dev/null | head -1)
        if [ -n "$CODE" ]; then
            echo "DEVICE_CODE=$CODE"
            echo "URL=https://auth.openai.com/codex/device"
            echo "PID=$(cat $PIDFILE)"
            echo "STATUS=waiting_for_browser_auth"
            exit 0
        fi
    fi
done

echo "STATUS=timeout_no_code"
exit 1
