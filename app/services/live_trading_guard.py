"""실매매 조건 변경을 CEO 승인 전까지 막는다.

2026-09-14. CEO 지시: "실매매조건은 나의 승인후 진행해야지".

**프롬프트로 막지 않는다.** 역할 지침에 "승인 후" 라고 적어 두긴 했지만
그것만으로는 안 막힌다. 같은 날 하루에만 이런 일이 있었다.

    · "릴리스 중 병행 빌드 금지" 를 AGENTS.md 에 써넣은 세션이
      같은 날 그 규칙을 어겨 배포 #414 를 죽였다.
    · `pgrep -f` 자기 매칭을 오류 사전에 등록해 두고도 **다섯 번** 밟았다.
    · 자동 재시도가 스스로를 먹어 resume 요청이 시간당 1,256회까지 갔다.

AGENTS.md 에 이렇게 적혀 있다 — "에이전트는 자기가 쓴 규칙도 어긴다.
두 번 이상 어긴 규칙은 코드 차단으로 옮긴다."

실매매는 돈이 걸린다. 첫 번째부터 코드로 막는다.

## 무엇을 막나

도구가 **실매매 동작을 바꾸는 경로**를 건드릴 때만 막는다. 조사·분석·
백테스트·읽기는 그대로 돈다 — 담당들이 일을 못 하면 하네스가 무의미하다.

    막음:  live_trading/ · order_executor · card_service · risk
           전략 파라미터 · 주문 로직 · 유니버스 설정
    통과:  읽기 · 조회 · 백테스트 · 문서 · 분석 스크립트

## 막으면 어떻게 되나

도구가 실행되지 않고, `agent_permission_requests` 에 승인 요청이 남고,
담당에게는 "승인 대기 중" 이 돌아간다. CEO 가 승인하면 그때 실행한다.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional, Tuple

import structlog

logger = structlog.get_logger(__name__)

# 기능 스위치. 배포 없이 끌 수 있어야 한다 — 게이트가 정상 작업을 막는
# 상황이 오면 즉시 풀 수 있어야 하고, 그 반대(몰래 꺼져 있음)도 로그로
# 드러나야 한다.
_ENABLED = os.getenv("LIVE_TRADING_GATE_ENABLED", "true").lower() == "true"

# 실매매 동작을 바꾸는 경로. 변경 원장 실측(2026-09-14)에서 뽑았다.
#   live_trading/scalping_entry_engine.py   변경 22건
#   live_trading/live_engine.py             변경 21건
#   routers/go100/card_trades_router.py     변경 20건
#   execution/order_executor.py             변경 13건
#   strategy/card_service.py                변경 11건
# 구분자가 밑줄일 때도 하이픈일 때도 잡아야 한다. 파일은
# `kiwoom_scalping_runner.py` 인데 서비스는 `go100-kiwoom-scalping` 이다.
# 2026-09-14 첫 시험에서 `systemctl restart go100-kiwoom-scalping` 이
# 그대로 통과했다 — 파일만 보고 서비스 이름을 안 봤다.
_GUARDED_PATH = re.compile(
    r"(live[_-]trading|order[_-]executor|card[_-]trades|"
    r"card[_-]service|scalping|kiwoom|position[_-]manager|"
    r"account[_-]sync|live[_-]promotion|paper[_-]trading|"
    r"[/_-]risk|risk[_-])",
    re.IGNORECASE,
)

# 파일을 바꾸거나 원격에서 무언가를 실행하는 도구만 본다.
# 읽기 도구는 아무리 민감한 경로를 봐도 막지 않는다.
_WRITE_TOOLS = {
    "write_remote_file", "patch_remote_file", "db_safe_write",
    "deploy_safe", "pipeline_runner_submit", "delegate_to_agent",
}
_COMMAND_TOOLS = {"run_remote_command", "pc_execute", "execute_sandbox", "device_command"}

# 명령 안에서 쓰기로 보는 것. 조회는 통과시킨다.
_MUTATING_CMD = re.compile(
    r"(^|[;&|]\s*)(rm|mv|cp|tee|sed\s+-i|systemctl\s+(restart|stop|start)|"
    r"docker\s+(restart|stop|rm)|git\s+(push|checkout|reset)|"
    r"psql[^|]*\b(update|insert|delete|alter|drop)\b|"
    r"python[0-9.]*\s+[^|]*\b(deploy|apply|promote|activate)\b)",
    re.IGNORECASE,
)
_REDIRECT_WRITE = re.compile(r">\s*[^|&\s]|>>\s*[^|&\s]")


def _text_of(tool_input: Dict[str, Any]) -> str:
    parts = []
    for key in ("file_path", "path", "command", "query", "sql", "target", "task", "project"):
        v = tool_input.get(key)
        if isinstance(v, str):
            parts.append(v)
    return "\n".join(parts)


def classify(tool_name: str, tool_input: Dict[str, Any]) -> Tuple[bool, str]:
    """(막을 것인가, 사유). 확실할 때만 막는다.

    애매하면 통과시킨다 — 정상 작업을 막으면 담당들이 게이트를 우회할
    길을 찾는다. 그게 규칙이 무시되기 시작하는 방식이다.
    """
    if not _ENABLED:
        return False, ""

    blob = _text_of(tool_input)
    if not blob or not _GUARDED_PATH.search(blob):
        return False, ""

    if tool_name in _WRITE_TOOLS:
        return True, f"{tool_name} 이(가) 실매매 경로를 변경하려 함"

    if tool_name in _COMMAND_TOOLS:
        cmd = str(tool_input.get("command") or "")
        if _MUTATING_CMD.search(cmd) or _REDIRECT_WRITE.search(cmd):
            return True, "실매매 경로에서 변경 명령을 실행하려 함"
        # 조회 명령(cat, grep, ls, tail…)은 통과.
        return False, ""

    return False, ""


async def request_approval(
    tool_name: str,
    tool_input: Dict[str, Any],
    reason: str,
    *,
    session_id: str = "",
    tenant_id: str = "",
) -> Optional[str]:
    """승인 요청을 남긴다. 요청 id 를 돌려준다.

    같은 세션이 같은 도구로 같은 대상을 반복 시도하면 요청이 쌓인다.
    그래서 대기 중인 동일 요청이 있으면 그것을 재사용한다.
    """
    from app.core.db_pool import get_pool

    summary = (_text_of(tool_input) or "")[:400]
    work_key = f"{session_id[:8]}:{tool_name}:{hash(summary) & 0xFFFFFFF:07x}"
    try:
        pool = get_pool()
        existing = await pool.fetchval(
            "SELECT id::text FROM agent_permission_requests "
            "WHERE work_key = $1 AND decision IS NULL "
            "ORDER BY created_at DESC LIMIT 1",
            work_key,
        )
        if existing:
            return existing
        return await pool.fetchval(
            """
            INSERT INTO agent_permission_requests
                (tenant_id, work_key, origin, action_type, action_summary,
                 risk_level, requested_by, approval_scope, max_executions, created_at)
            VALUES ($1::uuid, $2, 'chat_session', $3, $4, 'high', $5, 'single', 1, now())
            RETURNING id::text
            """,
            tenant_id or None, work_key, tool_name,
            f"[실매매] {reason}\n{summary}", session_id or "unknown",
        )
    except Exception as exc:
        logger.warning("live_trading_gate_request_failed", error=str(exc))
        return None


async def is_approved(tool_name: str, tool_input: Dict[str, Any], session_id: str = "") -> bool:
    """이미 승인된 요청인지."""
    from app.core.db_pool import get_pool

    summary = (_text_of(tool_input) or "")[:400]
    work_key = f"{session_id[:8]}:{tool_name}:{hash(summary) & 0xFFFFFFF:07x}"
    try:
        row = await get_pool().fetchrow(
            "SELECT decision, max_executions FROM agent_permission_requests "
            "WHERE work_key = $1 AND decision = 'approved' "
            "AND (expires_at IS NULL OR expires_at > now()) "
            "ORDER BY decided_at DESC LIMIT 1",
            work_key,
        )
        return bool(row)
    except Exception:
        return False


async def check(tool_name: str, tool_input: Dict[str, Any]) -> Optional[str]:
    """도구 실행 전 검사. 막을 것이면 담당에게 보여줄 JSON 을 돌려준다.

    통과면 None. 호출자는 None 일 때만 도구를 실행한다.
    """
    blocked, reason = classify(tool_name, tool_input)
    if not blocked:
        return None

    session_id = str(tool_input.get("session_id") or "")
    tenant_id = str(tool_input.get("tenant_id") or "")

    if await is_approved(tool_name, tool_input, session_id):
        logger.info("live_trading_gate_approved_pass tool=%s session=%s",
                    tool_name, session_id[:8])
        return None

    request_id = await request_approval(
        tool_name, tool_input, reason, session_id=session_id, tenant_id=tenant_id
    )
    logger.warning(
        "live_trading_gate_blocked tool=%s session=%s request=%s reason=%s",
        tool_name, session_id[:8], (request_id or "-")[:8], reason,
    )
    return json.dumps(
        {
            "error": "live_trading_approval_required",
            "blocked": True,
            "reason": reason,
            "request_id": request_id,
            "message": (
                "실매매 조건 변경은 CEO 승인이 필요합니다. 실행하지 않았습니다.\n"
                "무엇을 왜 바꾸려는지, 예상 효과와 되돌리는 방법까지 정리해 "
                "CEO 에게 보고하고 승인을 기다리세요.\n"
                "우회 시도(다른 도구·다른 경로로 같은 변경)는 하지 마세요."
            ),
        },
        ensure_ascii=False,
    )
