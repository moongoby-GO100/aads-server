"""AADS-195: PC Agent 자동 업데이트 모듈.

서버에서 self_update 명령 수신 시, 또는 주기적 자동 감지로
git pull → 에이전트 재시작을 수행한다.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys

logger = logging.getLogger("pc-agent.updater")

# 프로젝트 루트 (pc_agent/ 의 상위)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git_pull() -> tuple[bool, str]:
    """git pull 실행. (변경 여부, 출력) 반환."""
    try:
        result = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = result.stdout.strip()
        changed = "Already up to date" not in output and result.returncode == 0
        return changed, output or result.stderr.strip()
    except Exception as e:
        return False, str(e)


def _restart_agent() -> None:
    """현재 프로세스를 새 Python 프로세스로 교체 (os.execv)."""
    logger.info("에이전트 재시작 중...")
    python = sys.executable
    script = os.path.join(PROJECT_ROOT, "pc_agent", "agent.py")
    os.execv(python, [python, script])


async def execute(params: dict) -> dict:
    """self_update 명령 핸들러 — git pull + 재시작."""
    force = params.get("force", False)

    changed, output = _git_pull()

    if not changed and not force:
        return {
            "status": "ok",
            "data": {"updated": False, "message": output},
        }

    # 변경 있으면 재시작
    logger.info("업데이트 감지, 재시작 예정: %s", output)

    # 비동기로 약간 딜레이 후 재시작 (결과 전송 시간 확보)
    loop = asyncio.get_event_loop()
    loop.call_later(2.0, _restart_agent)

    return {
        "status": "ok",
        "data": {"updated": True, "message": f"업데이트 완료, 2초 후 재시작\n{output}"},
    }


async def check_for_updates() -> bool:
    """git remote 변경 확인 (pull 없이). 변경 있으면 True."""
    try:
        # fetch만 (merge 안 함)
        await asyncio.to_thread(
            subprocess.run,
            ["git", "fetch", "--quiet"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=15,
        )
        # local vs remote 비교
        result = await asyncio.to_thread(
            subprocess.run,
            ["git", "rev-list", "HEAD..@{u}", "--count"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        count = int(result.stdout.strip() or "0")
        return count > 0
    except Exception as e:
        logger.debug("업데이트 확인 실패: %s", e)
        return False
