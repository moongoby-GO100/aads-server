"""방향 게이트 — 무엇을 할지가 바뀔 때 대표님 승인을 받는다.

## 왜 실매매 게이트로는 부족한가

`live_trading_guard` 는 **파일 쓰기와 명령 실행**만 본다. 그래서 이런
것들이 승인 없이 지나간다.

- 마일스톤을 새로 만들거나 순서를 바꾸는 것
- 목표 기준을 바꾸는 것 (예: 3% 를 실현손익 대신 평가손익으로)
- 담당을 추가·교체하는 것

즉 **방향은 자유롭게 바뀌고, 그 방향대로 파일을 고치려는 순간에야 멈춘다.**
그때는 이미 담당 여럿이 그 방향으로 몇 시간 일한 뒤다. 되돌리는 비용이
훨씬 크다.

## 그리고 전체 정지

대표님이 "멈춰" 라고 하실 수 있어야 한다. 지금은 창을 하나씩 열어 말을
거는 것 말고는 방법이 없고, 주도가 이미 담당 셋에게 지시를 뿌렸다면
그들에게는 닿지 않는다.

`system_memory` 에 정지 플래그를 두고 **모든 쓰기 도구**를 막는다.
조회는 통과한다 — 멈춘 상태에서도 무슨 일이 있었는지는 봐야 한다.

## 규칙은 하나다

**확실할 때만 막는다.** 애매하면 통과시킨다. 정상 작업을 막으면 담당들이
게이트를 우회할 길을 찾고, 그게 규칙이 무시되기 시작하는 방식이다.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional, Tuple

import structlog

logger = structlog.get_logger(__name__)

_ENABLED = os.getenv("DIRECTION_GUARD_ENABLED", "true").lower() in ("1", "true", "yes")

# 정지 플래그. 대표님이 걸면 모든 쓰기가 막힌다.
HALT_KEY = "goal_orchestration_halt"

# 방향을 바꾸는 표. 여기 쓰는 것은 "무엇을 할지" 가 바뀐다는 뜻이다.
_DIRECTION_TABLES = re.compile(
    r"\b(milestones|goals|goal_task_links|prompt_assets)\b", re.IGNORECASE
)
# 담당 배정 자체를 바꾸는 것.
_ROLE_CHANGE = re.compile(r"chat_sessions[\s\S]{0,120}\brole_key\b", re.IGNORECASE)

_WRITE_SQL = re.compile(r"^\s*(INSERT|UPDATE|DELETE)\b", re.IGNORECASE)

# 정지 중에 막을 도구. 조회는 통과시킨다.
_HALT_BLOCKED = {
    "db_safe_write", "write_remote_file", "patch_remote_file", "deploy_safe",
    "pipeline_runner_submit", "delegate_to_agent", "spawn_subagent",
    "spawn_parallel_subagents", "run_remote_command", "pc_execute",
    "execute_sandbox", "device_command", "ask_session",
}


async def is_halted() -> bool:
    """대표님이 전체 정지를 걸어 두셨는가."""
    try:
        from app.core.db_pool import get_pool

        val = await get_pool().fetchval(
            "SELECT value FROM system_memory "
            "WHERE category = 'goal_orchestration' AND key = $1",
            HALT_KEY,
        )
        if not val:
            return False
        if isinstance(val, str):
            val = val.strip().strip('"')
        return str(val).lower() in ("1", "true", "on", "halted")
    except Exception as exc:
        # 조회 실패는 **통과**시킨다. 정지 플래그를 못 읽는다고 모든 작업을
        # 막으면 DB 가 흔들릴 때마다 전체가 선다. 실매매 게이트와 반대
        # 방향인데, 그쪽은 돈이 걸려 있고 이쪽은 진행이 걸려 있다.
        logger.debug("direction_guard_halt_check_failed", error=str(exc)[:120])
        return False


def classify(tool_name: str, tool_input: Dict[str, Any]) -> Tuple[bool, str]:
    """(막을 것인가, 사유). 확실할 때만 막는다."""
    if not _ENABLED:
        return False, ""

    if tool_name == "db_safe_write":
        sql = str(tool_input.get("sql") or tool_input.get("query") or "")
        if not _WRITE_SQL.search(sql):
            return False, ""
        if _DIRECTION_TABLES.search(sql):
            return True, "목표·마일스톤·역할 프롬프트를 바꾸려 함 (방향 변경)"
        if _ROLE_CHANGE.search(sql):
            return True, "담당 배정을 바꾸려 함 (방향 변경)"

    return False, ""


async def check(tool_name: str, tool_input: Dict[str, Any]) -> Optional[str]:
    """막아야 하면 도구 결과로 돌려줄 문자열, 아니면 None."""
    if not _ENABLED:
        return None

    if tool_name in _HALT_BLOCKED and await is_halted():
        logger.warning("direction_guard_halted tool=%s", tool_name)
        return json.dumps({
            "error": "orchestration_halted",
            "blocked": True,
            "message": (
                "대표님이 전체 정지를 걸어 두셨습니다. 조사·조회는 계속할 수 "
                "있지만 변경과 지시는 막힙니다. 지금까지 파악한 것을 정리해 "
                "보고하고 대표님 지시를 기다리세요."
            ),
        }, ensure_ascii=False)

    should_block, why = classify(tool_name, tool_input)
    if not should_block:
        return None

    request_id = ""
    try:
        from app.services.live_trading_guard import request_approval

        request_id = await request_approval(
            tool_name, tool_input, why,
            session_id=str(tool_input.get("session_id") or ""),
            tenant_id=str(tool_input.get("tenant_id") or ""),
        ) or ""
    except Exception as exc:
        logger.warning("direction_guard_request_failed", error=str(exc)[:160])

    logger.info("direction_guard_blocked tool=%s why=%s", tool_name, why)
    return json.dumps({
        "error": "direction_change_needs_approval",
        "blocked": True,
        "approval_request_id": request_id,
        "message": (
            f"{why}. 대표님 승인이 필요합니다.\n\n"
            "무엇을 왜 바꾸려는지, 바꾸면 어떤 담당의 작업이 영향을 받는지 "
            "정리해서 보고하세요. 승인 화면(/approvals)에 요청이 올라갔습니다. "
            "다른 담당을 시켜 우회하는 것도 같은 검사를 받습니다."
        ),
    }, ensure_ascii=False)
