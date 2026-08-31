#!/usr/bin/env bash
set -euo pipefail

max_wait_sec="${1:-7200}"
expected_max_concurrent="${AADS_RELAY_EXPECTED_MAX_CONCURRENT:-12}"
expected_acquire_timeout_sec="${AADS_RELAY_EXPECTED_ACQUIRE_TIMEOUT_SEC:-45}"
poll_sec="${AADS_RELAY_IDLE_POLL_SEC:-5}"
idle_streak_needed="${AADS_RELAY_IDLE_STREAK_NEEDED:-3}"
started_at="$(date +%s)"
idle_streak=0

while (( $(date +%s) - started_at < max_wait_sec )); do
    health="$(curl --max-time 3 -fsS http://127.0.0.1:8199/health 2>/dev/null || true)"
    lease_count="$(printf '%s' "$health" | python3 -c 'import json,sys; print(int(json.load(sys.stdin).get("lease_count", -1)))' 2>/dev/null || echo -1)"
    if [[ "$lease_count" == "0" ]]; then
        idle_streak=$((idle_streak + 1))
    else
        idle_streak=0
    fi
    if (( idle_streak >= idle_streak_needed )); then
        systemctl restart claude-relay.service
        for _ in $(seq 1 15); do
            health="$(curl --max-time 3 -fsS http://127.0.0.1:8199/health 2>/dev/null || true)"
            if printf '%s' "$health" | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert d.get("max_concurrent") == int("'"$expected_max_concurrent"'")
assert float(d.get("acquire_timeout_sec", 0)) == float("'"$expected_acquire_timeout_sec"'")
assert "acquire_metrics" in d
' 2>/dev/null; then
                logger -t aads-relay-config-apply "idle restart applied max_concurrent=${expected_max_concurrent} acquire_timeout_sec=${expected_acquire_timeout_sec}"
                exit 0
            fi
            sleep 1
        done
        logger -t aads-relay-config-apply "idle restart completed but runtime verification failed"
        exit 1
    fi
    sleep "$poll_sec"
done

logger -t aads-relay-config-apply "idle restart skipped: no 15-second idle window within ${max_wait_sec}s"
exit 2
