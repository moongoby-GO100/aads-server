#!/usr/bin/env python3
"""Codex CLI 인증 상태 모니터링 — cron으로 매일 실행.
토큰 만료 3일 전부터 텔레그램 경고 발송.
"""
import json, base64, time, os, sys, subprocess
from datetime import datetime, timezone, timedelta

AUTH_FILE = os.path.expanduser("~/.codex/auth.json")
KST = timezone(timedelta(hours=9))
WARN_DAYS = 3
HOSTNAME = os.uname().nodename

def decode_jwt_exp(token):
    try:
        parts = token.split('.')
        if len(parts) < 3:
            return 0
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + '=='))
        return payload.get('exp', 0)
    except Exception:
        return 0

def check_login_status():
    try:
        codex_path = os.environ.get("CODEX_PATH", "/root/.nvm/versions/node/v20.20.0/bin/codex")
        r = subprocess.run([codex_path, "login", "status"],
                           capture_output=True, text=True, timeout=10)
        return "Logged in" in (r.stdout + r.stderr)
    except Exception:
        return False

def send_telegram_alert(message):
    """AADS 텔레그램 알림 (aads-server API 경유)."""
    try:
        import urllib.request
        data = json.dumps({"message": message, "level": "warning"}).encode()
        req = urllib.request.Request(
            "http://localhost:8080/api/v1/ops/alert",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass

def main():
    now = int(time.time())
    now_kst = datetime.fromtimestamp(now, tz=KST).strftime('%Y-%m-%d %H:%M KST')

    if not os.path.exists(AUTH_FILE):
        msg = f"[Codex Auth] {HOSTNAME}: auth.json 없음! 즉시 재인증 필요. ({now_kst})"
        print(msg)
        send_telegram_alert(msg)
        sys.exit(1)

    with open(AUTH_FILE) as f:
        auth = json.load(f)

    tokens = auth.get('tokens', {})
    at = tokens.get('access_token', '')
    rt = tokens.get('refresh_token', '')

    if not at:
        msg = f"[Codex Auth] {HOSTNAME}: access_token 없음! 즉시 재인증 필요. ({now_kst})"
        print(msg)
        send_telegram_alert(msg)
        sys.exit(1)

    exp = decode_jwt_exp(at)
    remaining_sec = exp - now
    remaining_days = remaining_sec / 86400
    exp_kst = datetime.fromtimestamp(exp, tz=KST).strftime('%Y-%m-%d %H:%M KST')

    logged_in = check_login_status()

    status = {
        "host": HOSTNAME,
        "logged_in": logged_in,
        "access_token_exp": exp_kst,
        "remaining_days": round(remaining_days, 1),
        "has_refresh_token": bool(rt),
        "last_refresh": auth.get("last_refresh", "N/A"),
        "check_time": now_kst,
    }

    print(json.dumps(status, indent=2, ensure_ascii=False))

    if remaining_days <= 0:
        msg = f"🚨 [Codex Auth] {HOSTNAME}: access_token 만료됨! 즉시 `codex login --device-auth` 실행 필요. ({now_kst})"
        print(msg)
        send_telegram_alert(msg)
        sys.exit(2)
    elif remaining_days <= WARN_DAYS:
        msg = f"⚠️ [Codex Auth] {HOSTNAME}: access_token {remaining_days:.1f}일 후 만료 ({exp_kst}). `codex login --device-auth` 준비 필요."
        print(msg)
        send_telegram_alert(msg)
        sys.exit(0)
    elif not logged_in:
        msg = f"⚠️ [Codex Auth] {HOSTNAME}: codex login status 비정상. 토큰은 {remaining_days:.1f}일 남음. 점검 필요."
        print(msg)
        send_telegram_alert(msg)
        sys.exit(0)
    else:
        print(f"✅ 정상 — {remaining_days:.1f}일 남음")

if __name__ == '__main__':
    main()
