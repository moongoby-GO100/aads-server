#!/usr/bin/env python3
"""파일 auth.json 의 refresh_token 지문(SHA-256 앞 12자)만 출력한다. 값은 찍지 않는다."""
import hashlib
import json
import sys
from pathlib import Path

for name in ("CODEX_OAUTH_JINAH", "CODEX_OAUTH_MAIN"):
    path = Path("/root/.codex-accounts") / name / "auth.json"
    try:
        data = json.loads(path.read_text())
    except Exception as exc:  # noqa: BLE001
        print(f"{name}: read_fail {type(exc).__name__}")
        continue
    tokens = data.get("tokens") or {}
    refresh = tokens.get("refresh_token") or ""
    access = tokens.get("access_token") or ""
    print(
        f"{name}: refresh_fp={hashlib.sha256(refresh.encode()).hexdigest()[:12] if refresh else 'NONE'} "
        f"access_len={len(access)} last_refresh={data.get('last_refresh')} "
        f"account={(tokens.get('account_id') or '')[:8]}"
    )
sys.exit(0)
