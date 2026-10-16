#!/usr/bin/env python3
"""외부 AI 구독 채팅창(GenSpark·ChatGPT·Gemini·Claude·Perplexity) 화면을 읽기 전용으로 수집한다.

M1 범위는 **읽기**뿐이다. 외부 사이트에 질문을 보내는 쓰기 동작은 만들지 않는다 —
브라우저 명령은 browser_navigate / browser_get_text 두 가지만 쓴다.

GenSpark 전용 수집기(genspark_conversation_collector.py)는 그대로 두고, 이 모듈이 그 헬퍼
(log·mint_token·api·resolve_agent_id·ensure_session·fetch_page_text)를 import 해 재사용한다.
레거시 수집기는 genspark 전용 실행기로 계속 쓸 수 있다.

원래 GenSpark 전용이었다. 2026-04-15 이후 수집이 멈춰 있었고, 더 나쁜 것은 멈추기 전의
상태였다 — 4월에 적재된 2,422건 중 2,406건(99%)이 대화가 아니라 **로그인 화면**이었다.

    "snapshot": "Genspark AI Workspace\\nLogin with email\\nGoogle\\nMicrosoft..."

브라우저 세션이 만료됐는데 수집기는 그걸 모르고, 받은 화면을 그대로 대화로
저장했다. 숫자는 쌓이니 겉보기에는 정상으로 보였다. 3월은 16,044건 중
92%가 실제 대화였으므로, 어느 시점부터 조용히 고장난 것이다.

그래서 이 수집기의 핵심은 긁어오는 부분이 아니라 **무엇을 저장하지 않을지**다.
로그인 화면·봇 검증 화면·너무 짧은 본문은 저장하지 않고 사유만 남긴다.
받아온 것을 의심 없이 저장하는 순간 같은 일이 반복된다. provider 를 늘려도 이 원칙은
그대로다 — provider 마다 로그인 화면 문구가 다를 뿐이다.

경로: PC Agent 가 CEO PC 의 실제 크롬을 연다. 일부 서비스는 Cloudflare 봇 검증을
걸어두어 헤드리스 브라우저로는 로그인 화면조차 못 본다. 실제 브라우저를 쓰는
것이지 검증을 우회하는 것이 아니다.

적재: system_memory(conversation:{project}) 는 기존 소비자 호환으로 유지하고,
external_ai_channels / external_ai_turns 에 이중 기록한다. DB 쓰기는 호스트에서
`docker exec -i <컨테이너> python3 -c <고정 스크립트>` 로 컨테이너의 DATABASE_URL·asyncpg 를
쓰며 본문은 stdin JSON 으로만 넘긴다(셸 보간·SQL 문자열 결합 없음).

수집 전 external_ai_channels.enabled=false 인 (provider, url) 은 열지 않는다. 조회가 실패하면
끌 수 없는 상태로 수집하지 않도록 수집 자체를 중단한다(fail-closed).

system_memory 쓰기(POST /context/system)는 X-Monitor-Key 를 요구한다. 레거시 api() 는 Bearer 만
보내므로 이 쓰기만 post_system_memory() 로 따로 한다. 키는 AADS_MONITOR_KEY 환경변수 또는 .env 에서만 읽는다.

실행: external_ai_collector.py [--provider genspark] [--channel AADS_MGR] [--dry-run]
종료코드: 0 정상 / 1 토큰 / 2 PC Agent·세션 / 3 전부 건너뜀 / 4 수집·저장 실패 1건 이상
          / 5 enabled 조회 실패
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import uuid
import urllib.request
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

_GS_NAME = "genspark_conversation_collector"


def _load_genspark() -> Any:
    """레거시 수집기를 같은 디렉터리에서 경로로 로드한다. 이미 로드돼 있으면 그 객체를 쓴다.

    `__main__` 블록은 __name__ 이 모듈명이라 실행되지 않는다.
    """
    mod = sys.modules.get(_GS_NAME)
    if mod is not None:
        return mod
    path = Path(__file__).resolve().with_name(f"{_GS_NAME}.py")
    spec = importlib.util.spec_from_file_location(_GS_NAME, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_GS_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


_gs = _load_genspark()

# genspark 수집기와 동일한 객체를 재사용한다(재정의 금지).
log = _gs.log
mint_token = _gs.mint_token
api = _gs.api
resolve_agent_id = _gs.resolve_agent_id
ensure_session = _gs.ensure_session
fetch_page_text = _gs.fetch_page_text

CONTAINER = _gs.CONTAINER
MIN_BODY_CHARS = int(os.getenv("EXTERNAL_AI_MIN_BODY_CHARS", str(_gs.MIN_BODY_CHARS)))
NAV_WAIT_SEC = int(os.getenv("EXTERNAL_AI_NAV_WAIT_SEC", str(_gs.NAV_WAIT_SEC)))
# fetch_page_text 는 genspark 모듈의 NAV_WAIT_SEC 를 호출 시점에 읽는다.
_gs.NAV_WAIT_SEC = NAV_WAIT_SEC
SNAPSHOT_DB_MAX_CHARS = 200_000

# 도메인 → provider. 호스트가 도메인과 같거나 그 하위 도메인일 때만 맞는다.
PROVIDERS: dict[str, str] = {
    "genspark.ai": "genspark",
    "chatgpt.com": "chatgpt",
    "chat.openai.com": "chatgpt",
    "gemini.google.com": "gemini",
    "claude.ai": "claude",
    "perplexity.ai": "perplexity",
}
PROVIDER_NAMES = tuple(sorted(set(PROVIDERS.values())))

# 저장하면 안 되는 화면(모든 provider 공통). 하나라도 걸리면 그 채널은 건너뛴다.
# 앞의 6개는 GenSpark 전용이던 시절의 문구지만 provider 를 늘려도 그대로 둔다.
_COMMON_REJECT_MARKERS = (
    "login with email",
    "sign in with your email",
    "performing security verification",
    "not a bot",
    "sign up now",
    "로그인이 필요",
    "verify you are human",
    "checking your browser before accessing",
)

# provider 별 로그인/봇검증 화면 문구. 실제 화면 문구를 대조해 확정한 것이 아니라
# 각 서비스 로그아웃 화면의 알려진 문구이므로, 새 문구가 보이면 여기에 더한다.
# genspark 의 한국어 문구는 2026-10-16 실제 화면(228자)에서 확인했다.
_PROVIDER_REJECT_MARKERS: dict[str, tuple[str, ...]] = {
    "genspark": (
        "암호를 잊으셨습니까",
        "계정이 없으세요",
    ),
    "chatgpt": (
        "log in or sign up",
        "stay logged out",
        "sign up for free to get smarter responses",
        "checking if the site connection is secure",
    ),
    "gemini": (
        "meet gemini, your personal ai assistant",
        "to continue to gemini",
        "choose an account to continue",
        "couldn't sign you in",
    ),
    "claude": (
        "continue with email",
        "your ideas, amplified",
        "log in to claude",
        "sign in to claude",
    ),
    "perplexity": (
        "sign in or create an account",
        "unlock pro search and history",
        "continue with sso",
        "sign in to perplexity",
    ),
}

# 컨테이너 안에서 도는 고정 스크립트. 입력은 stdin JSON 한 건이고 SQL 은 전부 $n 바인딩이다.
DB_WRITER = r'''
import asyncio, json, os, sys
import asyncpg

rec = json.loads(sys.stdin.read())
turn = rec.get("turn")


async def main():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        async with conn.transaction():
            channel_id = await conn.fetchval(
                """
                INSERT INTO external_ai_channels
                    (provider, project, channel_key, url, title,
                     last_collected_at, last_status, last_reject_reason)
                VALUES ($1, $2, $3, $4, $5,
                        CASE WHEN $6::boolean THEN now() ELSE NULL END, $7, $8)
                ON CONFLICT (provider, url) DO UPDATE SET
                    project = COALESCE(EXCLUDED.project, external_ai_channels.project),
                    channel_key = COALESCE(EXCLUDED.channel_key, external_ai_channels.channel_key),
                    title = COALESCE(EXCLUDED.title, external_ai_channels.title),
                    last_collected_at = CASE WHEN $6::boolean THEN now()
                                             ELSE external_ai_channels.last_collected_at END,
                    last_status = EXCLUDED.last_status,
                    last_reject_reason = EXCLUDED.last_reject_reason,
                    updated_at = now()
                RETURNING id
                """,
                rec["provider"], rec.get("project"), rec.get("channel_key"), rec["url"],
                rec.get("title"), turn is not None, rec["status"], rec.get("reject_reason"),
            )
            inserted = False
            if turn is not None:
                turn_id = await conn.fetchval(
                    """
                    INSERT INTO external_ai_turns
                        (channel_id, provider, project, snapshot, char_count, content_hash, source)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (channel_id, content_hash) DO NOTHING
                    RETURNING id
                    """,
                    channel_id, rec["provider"], rec.get("project"), turn["snapshot"],
                    turn["char_count"], turn["content_hash"], turn.get("source"),
                )
                inserted = turn_id is not None
                if not inserted:
                    await conn.execute(
                        "UPDATE external_ai_channels SET last_status = 'duplicate' WHERE id = $1",
                        channel_id,
                    )
        print(json.dumps({"channel_id": channel_id, "inserted": inserted}))
    finally:
        await conn.close()


asyncio.run(main())
'''

# enabled=false 채널 목록을 읽는 고정 스크립트. 입력 없음, SQL 에 값 보간 없음.
DB_DISABLED_READER = r'''
import asyncio, json, os
import asyncpg


async def main():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        rows = await conn.fetch(
            "SELECT provider, url FROM external_ai_channels WHERE enabled = false"
        )
        print(json.dumps({"disabled": [[r["provider"], r["url"]] for r in rows]}))
    finally:
        await conn.close()


asyncio.run(main())
'''


def detect_provider(url: str) -> Optional[str]:
    """URL 호스트로 provider 를 판별한다. 쿼리·경로에 도메인이 들어 있는 것은 무시한다."""
    try:
        host = (urlparse(str(url or "").strip()).hostname or "").lower()
    except ValueError:
        return None
    for domain, provider in PROVIDERS.items():
        if host == domain or host.endswith("." + domain):
            return provider
    return None


def content_hash(text: str) -> str:
    """공백 차이를 무시한 본문 해시. 같은 화면이면 같은 값이다."""
    normalized = " ".join((text or "").split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def select_channels(items: list[dict], provider: str = "") -> list[dict]:
    """PROVIDERS 에 해당하는 URL 의 채널만 골라 provider 를 붙여 돌려준다."""
    out = []
    for ch in items or []:
        found = detect_provider(str(ch.get("url") or ""))
        if not found or (provider and found != provider):
            continue
        out.append({**ch, "provider": found})
    return out


def load_channels(token: str, provider: str = "") -> list[dict]:
    rows = api("/api/v1/channels", token)
    items = rows.get("channels") if isinstance(rows, dict) else rows
    return select_channels(items or [], provider)


def reject_reason(text: str, provider: str = "") -> Optional[str]:
    """저장하면 안 되는 화면인지. 저장 거부 사유를 돌려준다."""
    body = (text or "").strip()
    if len(body) < MIN_BODY_CHARS:
        return f"본문 {len(body)}자 (< {MIN_BODY_CHARS}) — 내용이 없다"
    low = body.lower()
    for marker in _COMMON_REJECT_MARKERS + _PROVIDER_REJECT_MARKERS.get(provider, ()):
        if marker in low:
            return f"차단 문구 '{marker}' — 로그인/봇검증 화면으로 보인다"
    return None


def _run_db_script(script: str, stdin_text: str = "") -> dict:
    """컨테이너에서 고정 스크립트를 돌리고 마지막 JSON 줄을 돌려준다. 값은 stdin 으로만 간다."""
    proc = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "python3", "-c", script],
        input=stdin_text, text=True, capture_output=True, timeout=90,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or ["?"]
        raise RuntimeError(f"db script rc={proc.returncode}: {tail[0][:120]}")
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise RuntimeError("db script 출력 없음")


def persist_db(rec: dict) -> dict:
    """external_ai_channels/turns 에 한 건을 기록한다. 본문은 stdin JSON 으로만 전달한다."""
    return _run_db_script(DB_WRITER, json.dumps(rec, ensure_ascii=False))


def load_disabled() -> set[tuple[str, str]]:
    """external_ai_channels 에서 enabled=false 로 꺼진 (provider, url) 집합."""
    res = _run_db_script(DB_DISABLED_READER)
    return {(str(p), str(u)) for p, u in res.get("disabled", [])}


def filter_enabled(channels: list[dict], disabled: set[tuple[str, str]]) -> tuple[list[dict], list[dict]]:
    """(수집 대상, 꺼져서 빠진 채널). 테이블에 없는 채널은 기본값(enabled=true)이라 대상이다."""
    kept, off = [], []
    for ch in channels:
        (off if (str(ch.get("provider")), str(ch.get("url") or "")) in disabled else kept).append(ch)
    return kept, off


def monitor_key() -> str:
    """POST /context/system 용 X-Monitor-Key. 환경변수, 없으면 저장소 .env 에서만 읽는다(R-KEY)."""
    key = (os.getenv("AADS_MONITOR_KEY") or "").strip()
    if key:
        return key
    env_file = Path(os.getenv("AADS_ENV_FILE") or Path(__file__).resolve().parents[1] / ".env")
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("AADS_MONITOR_KEY="):
                key = line.split("=", 1)[1].strip().strip("\"'")
                if key:
                    return key
    except OSError:
        pass
    raise RuntimeError("AADS_MONITOR_KEY 없음 — 환경변수 또는 .env 필요")


def post_system_memory(token: str, category: str, key: str, value: dict, timeout: int = 60) -> Any:
    req = urllib.request.Request(
        f"{_gs.API}/api/v1/context/system",
        data=json.dumps({"category": category, "key": key, "value": value}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}",
                 "X-Monitor-Key": monitor_key()},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _db_record(channel: dict, status: str, reject: Optional[str] = None,
               text: Optional[str] = None) -> dict:
    provider = str(channel["provider"])
    project = str(channel.get("project") or channel.get("id") or "AADS").upper()
    rec: dict[str, Any] = {
        "provider": provider,
        "project": project,
        "channel_key": str(channel.get("id") or "") or None,
        "url": str(channel.get("url") or ""),
        "title": str(channel.get("name") or "") or None,
        "status": status,
        "reject_reason": reject,
        "turn": None,
    }
    if text is not None:
        rec["turn"] = {
            "snapshot": text[:SNAPSHOT_DB_MAX_CHARS],
            "char_count": len(text),
            "content_hash": content_hash(text),
            "source": f"{provider}_pc_bridge",
        }
    return rec


def record_status(channel: dict, status: str, reason: str, dry_run: bool) -> None:
    """건너뜀·실패를 채널 행에 남긴다. 기록 실패가 수집 결과를 바꾸지는 않는다."""
    if dry_run:
        return
    try:
        persist_db(_db_record(channel, status, reject=reason))
    except Exception as exc:
        log(f"  {str(channel.get('id') or '?'):12s} 상태 기록 실패: {str(exc)[:100]}")


def save(token: str, channel: dict, text: str, dry_run: bool) -> str:
    project = str(channel.get("project") or channel.get("id") or "AADS").lower()
    provider = str(channel.get("provider") or detect_provider(str(channel.get("url") or "")) or "other")
    channel_id = "".join(c.lower() for c in str(channel.get("id") or "unknown") if c.isalnum()) or "unknown"
    epoch = int(time.time())
    unique_suffix = uuid.uuid4().hex[:8]
    key_prefix = f"chat_{epoch}_{provider}_"
    # system_memory.key is VARCHAR(100). Keep the required channel component
    # while reserving space for the per-write UUID, even for unusually long IDs.
    channel_budget = max(1, 100 - len(key_prefix) - len(unique_suffix) - 1)
    channel_id = channel_id[:channel_budget]
    key = f"{key_prefix}{channel_id}_{unique_suffix}"
    value = {
        "snapshot": text[:20000],
        "char_count": len(text),
        "logged_at": time.strftime("%FT%T+09:00"),
        "source": f"{provider}_pc_bridge",
        "project": project.upper(),
        "session_id": str(channel.get("id") or ""),
        "channel_url": str(channel.get("url") or ""),
    }
    if dry_run:
        return f"DRY-RUN 저장 생략 ({len(text)}자)"
    post_system_memory(token, f"conversation:{project}", key, value)
    res = persist_db(_db_record({**channel, "provider": provider}, "saved", text=text))
    dup = "" if res.get("inserted") else " [external_ai_turns 중복 — 동일 화면]"
    return f"저장 {key} ({len(text)}자){dup}"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="", choices=("",) + PROVIDER_NAMES, help="provider 하나만 수집")
    ap.add_argument("--channel", default="", help="채널 id 하나만 수집")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    work_key = os.getenv("EXTERNAL_AI_WORK_KEY", "external-ai-collect")
    normalized_work_key = work_key.strip().lower()
    if normalized_work_key == "genspark-collect" or normalized_work_key.startswith(("extai-", "extai:")):
        log(f"공유 금지 work_key 거부: {work_key}")
        return 2

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

    channels = load_channels(token, args.provider)
    if args.channel:
        channels = [c for c in channels if str(c.get("id")) == args.channel]
    if channels:
        try:
            channels, off = filter_enabled(channels, load_disabled())
        except Exception as exc:
            log(f"enabled 조회 실패 — 수집 중단: {str(exc)[:120]}")
            return 5
        for ch in off:
            log(f"  {str(ch.get('id') or '?'):12s} [{ch['provider']}] enabled=false — 수집 안 함")
    if not channels:
        log("대상 채널 없음")
        return 0

    try:
        ensure_session(token, agent_id, work_key)
    except Exception as exc:
        log(f"브라우저 세션 준비 실패: {type(exc).__name__}: {str(exc)[:90]}")
        for ch in channels:
            record_status(ch, "failed", f"session: {type(exc).__name__}: {str(exc)[:80]}", args.dry_run)
        return 2

    saved = skipped = failed = 0
    for ch in channels:
        cid = str(ch.get("id") or "?")
        provider = ch["provider"]
        try:
            text = fetch_page_text(token, agent_id, str(ch.get("url")), work_key)
        except Exception as exc:
            failed += 1
            msg = f"{type(exc).__name__}: {str(exc)[:80]}"
            log(f"  {cid:12s} [{provider}] 수집 실패: {msg}")
            record_status(ch, "failed", msg, args.dry_run)
            continue
        reason = reject_reason(text, provider)
        if reason:
            skipped += 1
            log(f"  {cid:12s} [{provider}] 저장 안 함 — {reason}")
            record_status(ch, "skipped", reason, args.dry_run)
            continue
        try:
            log(f"  {cid:12s} [{provider}] {save(token, ch, text, args.dry_run)}")
            saved += 1
        except Exception as exc:
            failed += 1
            msg = f"{type(exc).__name__}: {str(exc)[:80]}"
            log(f"  {cid:12s} [{provider}] 저장 실패: {msg}")
            record_status(ch, "failed", msg, args.dry_run)

    log(f"완료: 저장 {saved} / 건너뜀 {skipped} / 실패 {failed}")
    # 실패가 한 건이라도 있으면 cron·종료코드 검증이 놓치지 않게 4 를 돌려준다.
    if failed:
        return 4
    # 전부 건너뛰었다면 세션이 끊긴 것이다. 조용히 성공으로 끝내지 않는다.
    return 3 if (saved == 0 and skipped > 0) else 0


if __name__ == "__main__":
    sys.exit(main())
