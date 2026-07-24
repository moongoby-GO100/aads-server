#!/usr/bin/env python3
"""Regression check for AADS chat auth flow."""
import subprocess
import sys
sys.path.insert(0, '/app')
from app.auth import create_token, verify_token

# CEO moongoby@naver.com
user_id = '79ee004e-1e2e-490f-aa05-b096814f180d'
email = 'moongoby@naver.com'
tenant_id = '2d701a8c-9596-4757-8588-faa4f7837112'

# 1) Create token like login endpoint does (is_admin=False for SaaS users)
token = create_token(user_id, email, tenant_id=tenant_id)
print("1) Token created")

# 2) Verify token
payload = verify_token(token)
print(f"2) Verify result: sub={payload.get('sub')}, email={payload.get('email')}, is_admin={payload.get('is_admin')}, tenant_id={payload.get('tenant_id')}")

# 3) Test /chat/workspaces with Bearer token
r = subprocess.run(
    ['curl', '-s', '-w', '\nHTTP_CODE:%{http_code}',
     '-H', f'Authorization: Bearer {token}',
     'http://localhost:8080/api/v1/chat/workspaces'],
    capture_output=True, text=True, timeout=10
)
lines = r.stdout.strip().split('\n')
http_code = [l for l in lines if l.startswith('HTTP_CODE:')]
body = '\n'.join(l for l in lines if not l.startswith('HTTP_CODE:'))
print(f"3) GET /chat/workspaces: {http_code[0] if http_code else 'unknown'}")
if 'HTTP_CODE:200' not in r.stdout:
    print(f"   Body: {body[:200]}")

# 4) Test with cookie
r2 = subprocess.run(
    ['curl', '-s', '-w', '\nHTTP_CODE:%{http_code}',
     '-b', f'aads_token={token}',
     'http://localhost:8080/api/v1/chat/workspaces'],
    capture_output=True, text=True, timeout=10
)
lines2 = r2.stdout.strip().split('\n')
http_code2 = [l for l in lines2 if l.startswith('HTTP_CODE:')]
print(f"4) GET /chat/workspaces (cookie): {http_code2[0] if http_code2 else 'unknown'}")

# 5) Test admin login
admin_token = create_token('admin', 'admin@aads.dev', is_admin=True, tenant_id=tenant_id)
r3 = subprocess.run(
    ['curl', '-s', '-w', '\nHTTP_CODE:%{http_code}',
     '-H', f'Authorization: Bearer {admin_token}',
     'http://localhost:8080/api/v1/chat/workspaces'],
    capture_output=True, text=True, timeout=10
)
lines3 = r3.stdout.strip().split('\n')
http_code3 = [l for l in lines3 if l.startswith('HTTP_CODE:')]
print(f"5) GET /chat/workspaces (admin): {http_code3[0] if http_code3 else 'unknown'}")
