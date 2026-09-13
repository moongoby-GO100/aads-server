#!/usr/bin/env python3
"""GenSpark 매니저 대화를 수집해 system_memory 에 적재한다.

2026-04-15 이후 수집이 멈춰 있었다. 더 나쁜 것은 멈추기 전의 상태였다 —
4월에 적재된 2,422건 중 2,406건(99%)이 대화가 아니라 **로그인 화면**이었다.

    "snapshot": "Genspark AI Workspace\\nLogin with email\\nGoogle\\nMicrosoft..."

브라우저 세션이 만료됐는데 수집기는 그걸 모르고, 받은 화면을 그대로 대화로
저장했다. 숫자는 쌓이니 겉보기에는 정상으로 보였다. 3월은 16,044건 중
92%가 실제 대화였으므로, 어느 시점부터 조용히 고장난 것이다.

그래서 이 수집기의 핵심은 긁어오는 부분이 아니라 **무엇을 저장하지 않을지**다.
로그인 화면·봇 검증 화면·너무 짧은 본문은 저장하지 않고 사유만 남긴다.
받아온 것을 의심 없이 저장하는 순간 같은 일이 반복된다.

경로: PC Agent 가 CEO PC 의 실제 크롬을 연다. GenSpark 는 Cloudflare 봇 검증을
걸어두어 헤드리스 브라우저로는 로그인 화면조차 못 본다. 실제 브라우저를 쓰는
것이지 검증을 우회하는 것이 아니다.

실행: genspark_conversation_collector.py [--channel AADS_MGR] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from typing import Any, Optional

API = os.getenv("AADS_API_URL", "http://127.0.0.1:8100")
AGENT_ID = os.getenv("GENSPARK_PC_AGENT_ID", "")
CONTAINER = os.getenv("AADS_API_CONTAINER", "aads-server")
TENANT_ID = os.getenv("AADS_TENANT_ID", "2d701a8c-9596-4757-8588-faa4f7837112")
USER_ID = os.getenv("AADS_COLLECTOR_USER_ID", "79ee004e-1e2e-490f-aa05-b096814f180d")
USER_EMAIL = os.getenv("AADS_COLLECTOR_USER_EMAIL", "moongoby@naver.com")
MIN_BODY_CHARS = int(os.getenv("GENSPARK_MIN_BODY_CHARS", "400"))
NAV_WAIT_SEC = int(os.getenv("GENSPARK_NAV_WAIT_SEC", "12"))

# 저장하면 안 되는 화면. 하나라도 걸리면 그 채널은 건너뛴다.
_REJECT_MARKERS = (
    "login with email",
    "sign in with your email",
    "performing security verification",
    "not a bot",
    "sign up now",
    "로그인이 필요",
)

MINT = f'''
import sys
sys.path.insert(0, "/app")
from app.auth import create_token
print(create_token("{USER_ID}", "{USER_EMAIL}", is_admin=True, tenant_id="{TENANT_ID}"))
'''


def log(msg: str) -> None:
    print(f"[{time.strftime('%F %T')}] {msg}", flush=True)


def mint_token() -> str:
    proc = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "python3", "-"],
        input=MINT, text=True, capture_output=True, timeout=90,
    )
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if line and "." in line and " " not in line:
            return line
    raise RuntimeError("token mint failed")


def api(path: str, token: str, payload: Optional[dict] = None, timeout: int = 150) -> Any:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{API}{path}", data=data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def load_channels(token: str) -> list[dict]:
    rows = api("/api/v1/channels", token)
    items = rows.get("channels") if isinstance(rows, dict) else rows
    out = []
    for ch in items or []:
        url = str(ch.get("url") or "")
        if "genspark.ai" in url:
            out.append(ch)
    return out


def resolve_agent_id(token: str) -> str:
    if AGENT_ID:
        return AGENT_ID
    data = api("/api/v1/pc-agent/agents", token)
    agents = data.get("agents") or []
    if not agents:
        raise RuntimeError("온라인 PC Agent 없음 — 수집 불가")
    for a in agents:
        if "ceo" in str(a.get("agent_name") or "").lower():
            return str(a["agent_id"])
    return str(agents[0]["agent_id"])


def ensure_session(token: str, agent_id: str, work_key: str) -> None:
    """work_key 별 CDP 세션을 먼저 연다.

    route-execute 는 세션이 없는 새 work_key 에서 503 을 돌려준다(실측).
    채널마다 세션을 새로 만들지 않고 하나를 공유해 PC 크롬 창이 늘어나는
    것을 막는다.
    """
    api("/api/v1/browser-bridge/sessions/ensure-pc-cdp", token, {
        "agent_id": agent_id, "label": work_key, "work_key": work_key,
        "url": "about:blank", "isolated_profile": False, "activate": False,
    }, timeout=120)


def fetch_page_text(token: str, agent_id: str, url: str, work_key: str) -> str:
    api("/api/v1/browser-bridge/work-sessions/route-execute", token, {
        "work_key": work_key, "agent_id": agent_id,
        "command_type": "browser_navigate", "url": url,
        "params": {"url": url, "wait_seconds": NAV_WAIT_SEC},
        "command_timeout_seconds": 120,
    })
    res = api("/api/v1/browser-bridge/work-sessions/route-execute", token, {
        "work_key": work_key, "agent_id": agent_id,
        "command_type": "browser_get_text", "params": {"selector": "body"},
        "command_timeout_seconds": 120,
    })
    inner = (res.get("result") or {}).get("result") or {}
    return str(inner.get("text") or "")


def reject_reason(text: str) -> Optional[str]:
    """저장하면 안 되는 화면인지. 저장 거부 사유를 돌려준다."""
    body = (text or "").strip()
    if len(body) < MIN_BODY_CHARS:
        return f"본문 {len(body)}자 (< {MIN_BODY_CHARS}) — 내용이 없다"
    low = body.lower()
    for marker in _REJECT_MARKERS:
        if marker in low:
            return f"차단 문구 '{marker}' — 로그인/봇검증 화면으로 보인다"
    return None


def save(token: str, channel: dict, text: str, dry_run: bool) -> str:
    project = str(channel.get("project") or channel.get("id") or "AADS").lower()
    key = f"chat_{int(time.time())}"
    value = {
        "snapshot": text[:20000],
        "char_count": len(text),
        "logged_at": time.strftime("%FT%T+09:00"),
        "source": "genspark_pc_bridge",
        "project": project.upper(),
        "session_id": str(channel.get("id") or ""),
        "channel_url": str(channel.get("url") or ""),
    }
    if dry_run:
        return f"DRY-RUN 저장 생략 ({len(text)}자)"
    api("/api/v1/context/system", token,
        {"category": f"conversation:{project}", "key": key, "value": value})
    return f"저장 {key} ({len(text)}자)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="", help="채널 id 하나만 수집")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        token = mint_token()
    except Exception as exc:
        log(f"토큰 발급 실패: {exc}")
        return 1
    try:
        agent_id = resolve_agent_id(token)
    except Exception as exc:
        log(f"{exc}")
        return 2
    log(f"PC Agent={agent_id}")

    channels = load_channels(token)
    if args.channel:
        channels = [c for c in channels if str(c.get("id")) == args.channel]
    if not channels:
        log("대상 채널 없음")
        return 0

    work_key = os.getenv("GENSPARK_WORK_KEY", "genspark-collect")
    try:
        ensure_session(token, agent_id, work_key)
    except Exception as exc:
        log(f"브라우저 세션 준비 실패: {type(exc).__name__}: {str(exc)[:90]}")
        return 2

    saved = skipped = failed = 0
    for ch in channels:
        cid = str(ch.get("id") or "?")
        try:
            text = fetch_page_text(token, agent_id, str(ch.get("url")), work_key)
        except Exception as exc:
            failed += 1
            log(f"  {cid:12s} 수집 실패: {type(exc).__name__}: {str(exc)[:80]}")
            continue
        reason = reject_reason(text)
        if reason:
            skipped += 1
            log(f"  {cid:12s} 저장 안 함 — {reason}")
            continue
        try:
            log(f"  {cid:12s} {save(token, ch, text, args.dry_run)}")
            saved += 1
        except Exception as exc:
            failed += 1
            log(f"  {cid:12s} 저장 실패: {str(exc)[:80]}")

    log(f"완료: 저장 {saved} / 건너뜀 {skipped} / 실패 {failed}")
    # 전부 건너뛰었다면 세션이 끊긴 것이다. 조용히 성공으로 끝내지 않는다.
    return 3 if (saved == 0 and skipped > 0) else 0


if __name__ == "__main__":
    sys.exit(main())
