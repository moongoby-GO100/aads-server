#!/usr/bin/env python3
"""배포 후 시각 QA 가 로그인 상태로 화면을 찍도록 storage_state 를 갱신한다.

QA 브라우저는 인증 없이 페이지를 열어 왔다. 그래서 /, /chat, /ops 세 장이
전부 로그인 화면이었고(2026-09-13 실측: 스크린샷 4장이 23,241바이트로 바이트
동일), 감리는 로그인 UI 를 세 번 채점했다.

캡처 스크립트는 storage_state 를 이미 지원한다. 이 스크립트가 그 파일을
만든다. 토큰은 유효기간이 있으므로 QA 직전에 매번 새로 만든다 — 크론으로
돌리면 만료된 채 조용히 로그인 화면으로 되돌아간다.

토큰은 stdin 으로만 오가고 argv 에 실리지 않는다. 파일은 0600 으로 쓴다.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

OUT_PATH = Path(os.getenv(
    "AADS_QA_STORAGE_STATE",
    "/root/aads/aads-server/browser-bridge-state/qa-storage-state.json",
))
BASE_URL = os.getenv("AADS_QA_BASE_URL", "https://aads.newtalk.kr")
CONTAINER = os.getenv("AADS_QA_TOKEN_CONTAINER", "aads-server")
USER_ID = os.getenv("AADS_QA_USER_ID", "79ee004e-1e2e-490f-aa05-b096814f180d")
USER_EMAIL = os.getenv("AADS_QA_USER_EMAIL", "moongoby@naver.com")
TENANT_ID = os.getenv("AADS_QA_TENANT_ID", "2d701a8c-9596-4757-8588-faa4f7837112")

MINT = f'''
import sys
sys.path.insert(0, "/app")
from app.auth import create_token
print(create_token("{USER_ID}", "{USER_EMAIL}", is_admin=True, tenant_id="{TENANT_ID}"))
'''


def mint_token() -> str:
    proc = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "python3", "-"],
        input=MINT, text=True, capture_output=True, timeout=90,
    )
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if line and "." in line and " " not in line:
            return line
    raise RuntimeError(f"token mint failed: {(proc.stderr or '')[-200:]}")


def main() -> int:
    try:
        token = mint_token()
    except Exception as exc:
        print(f"[qa-storage-state] 토큰 발급 실패: {exc}", file=sys.stderr)
        return 1

    from playwright.sync_api import sync_playwright

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT_PATH.with_suffix(".tmp")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            ctx = browser.new_context(viewport={"width": 1920, "height": 1080})
            page = ctx.new_page()
            # e2e-auth.html 이 토큰을 localStorage + 쿠키에 심고 리다이렉트한다.
            page.goto(f"{BASE_URL}/e2e-auth.html?token={token}&redirect=/chat",
                      wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(9000)
            landed = page.url
            # 로그인 화면으로 되돌아갔으면 실패다. 그대로 저장하면 QA 가
            # 로그인 화면을 찍으면서도 성공한 것처럼 보인다.
            if "/login" in landed:
                print(f"[qa-storage-state] 로그인 실패 — 도착 URL={landed}", file=sys.stderr)
                browser.close()
                return 2
            ctx.storage_state(path=str(tmp))
            browser.close()
    except Exception as exc:
        print(f"[qa-storage-state] 브라우저 로그인 실패: {exc}", file=sys.stderr)
        return 1

    try:
        state = json.loads(tmp.read_text())
    except Exception as exc:
        print(f"[qa-storage-state] 저장 파일 파싱 실패: {exc}", file=sys.stderr)
        return 1
    origins = state.get("origins") or []
    cookies = state.get("cookies") or []
    if not origins and not cookies:
        print("[qa-storage-state] 인증 흔적 없음 — 저장하지 않는다", file=sys.stderr)
        tmp.unlink(missing_ok=True)
        return 2

    os.chmod(tmp, 0o600)
    tmp.replace(OUT_PATH)
    print(f"[qa-storage-state] 갱신 완료: {OUT_PATH} "
          f"(cookies={len(cookies)}, origins={len(origins)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
