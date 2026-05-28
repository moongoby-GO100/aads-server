"""AADS-195: PC Agent 자동 업데이트 모듈.

서버에서 self_update 명령 수신 시, 또는 주기적 자동 감지로
HTTP 버전 확인 → zip 다운로드 → 에이전트 재시작을 수행한다.

주의: agent 코드는 ZIP으로 배포됨 (git 아님). 모든 업데이트는 HTTP 기반.
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import sys
from pathlib import Path
from urllib import request as _req

logger = logging.getLogger("pc-agent.updater")

# 경로 상수
INSTALL_DIR = Path(os.environ.get(
    "KAKAOBOT_INSTALL_DIR",
    os.path.join(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"), "KakaoBot"),
))
AGENT_DIR = INSTALL_DIR / "agent"
VERSION_FILE = AGENT_DIR / "VERSION"
HTTP_BASE = "https://aads.newtalk.kr"


def _get_local_version() -> str:
    """로컬 에이전트 버전 읽기."""
    if VERSION_FILE.exists():
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    return "0.0.0"


def _restart_agent() -> None:
    """에이전트 재시작 (frozen EXE: exit → launcher 자동 재시작)."""
    logger.info("에이전트 재시작 중...")
    if getattr(sys, "frozen", False):
        logger.info("frozen EXE 감지 — 종료 후 launcher 자동 재시작")
        try:
            import agent as _agent_module
            release_mutex = getattr(_agent_module, "release_single_instance", None)
            if callable(release_mutex):
                release_mutex()
        except Exception as exc:
            logger.debug("재시작 전 mutex 해제 실패 (무시): %s", exc)
        sys.exit(42)
    python = sys.executable
    agent_py = str(AGENT_DIR / "agent.py")
    os.execv(python, [python, agent_py])


async def execute(params: dict) -> dict:
    """self_update 명령 핸들러 — HTTP 버전 확인 + 재시작."""
    force = params.get("force", False)

    has_update = await check_for_updates()

    if not has_update and not force:
        return {
            "status": "ok",
            "data": {"updated": False, "message": "이미 최신 버전입니다"},
        }

    # launcher가 exit=42 감지 후 check_update()로 직접 버전 비교하므로 VERSION 리셋 불필요
    # VERSION 리셋 시 다운로드 실패 → "0.0.0" 고착 → 무한 재시작 루프 위험 제거
    logger.info("업데이트 감지, 재시작 예정")

    async def _delayed_restart():
        await asyncio.sleep(2.0)
        _restart_agent()

    try:
        asyncio.ensure_future(_delayed_restart())
    except Exception:
        loop = asyncio.get_event_loop()
        loop.call_later(2.0, _restart_agent)

    return {
        "status": "ok",
        "data": {"updated": True, "message": "업데이트 감지, 2초 후 재시작"},
    }


async def check_for_updates() -> bool:
    """서버 HTTP API로 최신 버전 확인. 변경 있으면 True."""
    try:
        token = os.getenv("AADS_AGENT_TOKEN", "")
        req = _req.Request(f"{HTTP_BASE}/api/v1/kakao-bot/agent/version")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        req.add_header("User-Agent", "KakaoBot-Updater/1.0")
        req.add_header("Cache-Control", "no-cache")

        resp = await asyncio.to_thread(_req.urlopen, req, timeout=10)
        info = _json.loads(resp.read().decode())
        remote_ver = info.get("version", "0.0.0")
        local_ver = _get_local_version()

        logger.info(
            "버전 체크: local=%r (path=%s, exists=%s) remote=%r",
            local_ver, VERSION_FILE, VERSION_FILE.exists(), remote_ver,
        )
        if remote_ver != local_ver:
            logger.info("업데이트 감지: 로컬=%s 서버=%s", local_ver, remote_ver)
            return True
        return False
    except Exception as e:
        logger.debug("업데이트 확인 실패: %s", e)
        return False
