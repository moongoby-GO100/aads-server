#!/usr/bin/env python3
"""Test OAuth2 refresh token flow for Codex CLI."""
import json, urllib.request, urllib.parse, base64, time, sys
from datetime import datetime, timezone, timedelta

AUTH_FILE = "/root/.codex/auth.json"
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
                with open(AUTH_FILE, 'w') as f:
                    json.dump(auth, f, indent=2)
                print("AUTH_FILE_UPDATED")
            else:
                print("(--apply 옵션으로 auth.json 업데이트 가능)")
                
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ''
        print(f"REFRESH_FAILED: {e.code} {e.reason}")
        print(f"Body: {body[:500]}")
    except Exception as e:
        print(f"REFRESH_ERROR: {e}")

if __name__ == '__main__':
    main()
