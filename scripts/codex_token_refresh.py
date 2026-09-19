#!/usr/bin/env python3
"""Test OAuth2 refresh token flow for Codex CLI."""
import json, os, urllib.request, urllib.parse, base64, time, sys
from datetime import datetime, timezone, timedelta

# 대상 파일을 인자/환경변수로 받는다. 계정 홈(/root/.codex-accounts/<KEY>)도
# 같은 방식으로 갱신해야 하는데 경로가 박혀 있으면 MAIN 밖으로 못 나간다.
AUTH_FILE = os.getenv("CODEX_AUTH_FILE", "") or next(
    (a for a in sys.argv[1:] if not a.startswith("-")), "/root/.codex/auth.json"
)
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
KST = timezone(timedelta(hours=9))

def load_auth():
    with open(AUTH_FILE) as f:
        return json.load(f)

def decode_exp(token):
    parts = token.split('.')
    if len(parts) < 3:
        return 0
    payload = json.loads(base64.urlsafe_b64decode(parts[1] + '=='))
    return payload.get('exp', 0)

def main():
    auth = load_auth()
    tokens = auth.get('tokens', {})
    rt = tokens.get('refresh_token', '')
    at = tokens.get('access_token', '')
    
    if not rt:
        print("ERROR: No refresh_token found")
        sys.exit(1)
    
    old_exp = decode_exp(at)
    now = int(time.time())
    old_exp_kst = datetime.fromtimestamp(old_exp, tz=KST).strftime('%Y-%m-%d %H:%M KST')
    print(f"현재 access_token 만료: {old_exp_kst} (남은: {(old_exp-now)//86400}일)")

    # --apply 없이 토큰 엔드포인트를 부르면 안 된다. refresh_token 은 1회용이고
    # 호출하는 순간 서버에서 회전한다 — 새 값을 저장하지 않으면 파일에 남은 옛
    # refresh_token 은 그 자리에서 무효가 되고 재로그인 외에 복구가 없다.
    # (2026-09-19: CODEX_OAUTH_JINAH 가 회전 어긋남으로 죽은 것과 같은 실패다.)
    if '--apply' not in sys.argv:
        print("DRY_RUN: 회전 소모를 막기 위해 요청을 보내지 않았다. 갱신하려면 --apply 를 붙여라.")
        return
    
    # Try refresh
    data = urllib.parse.urlencode({
        'grant_type': 'refresh_token',
        'client_id': CLIENT_ID,
        'refresh_token': rt,
    }).encode()
    
    req = urllib.request.Request(TOKEN_URL, data=data, headers={
        'Content-Type': 'application/x-www-form-urlencoded',
    })
    
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            new_at = result.get('access_token', '')
            new_rt = result.get('refresh_token', '')
            new_id = result.get('id_token', '')
            
            new_exp = decode_exp(new_at)
            new_exp_kst = datetime.fromtimestamp(new_exp, tz=KST).strftime('%Y-%m-%d %H:%M KST')
            
            print(f"REFRESH_SUCCESS")
            print(f"새 access_token 만료: {new_exp_kst} (남은: {(new_exp-now)//86400}일)")
            print(f"새 refresh_token: {'있음' if new_rt else '없음(기존유지)'}")
            
            if '--apply' in sys.argv:
                tokens['access_token'] = new_at
                if new_rt:
                    tokens['refresh_token'] = new_rt
                if new_id:
                    tokens['id_token'] = new_id
                auth['tokens'] = tokens
                auth['last_refresh'] = datetime.now(timezone.utc).isoformat()
                # 원본을 바로 truncate 하지 않는다. 쓰는 도중에 죽으면 방금 회전한
                # 토큰도, 옛 토큰도 남지 않아 재로그인 외에 복구가 없다.
                tmp = AUTH_FILE + '.new'
                with open(tmp, 'w') as f:
                    json.dump(auth, f, indent=2)
                os.chmod(tmp, 0o600)
                os.replace(tmp, AUTH_FILE)
                print("AUTH_FILE_UPDATED")
                
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ''
        print(f"REFRESH_FAILED: {e.code} {e.reason}")
        print(f"Body: {body[:500]}")
    except Exception as e:
        print(f"REFRESH_ERROR: {e}")

if __name__ == '__main__':
    main()
