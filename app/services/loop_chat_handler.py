"""OHVIS Loop Chat Handler — 채팅 루프 명령 파싱 및 처리.

채팅에서 "감시해", "루프 중지", "루프 상태" 등의 명령을 감지하여
loop_controller를 호출하고 응답 메시지를 반환한다.
"""
from __future__ import annotations

import re
import logging
from typing import Any

from app.services.loop_controller import (
    create_loop,
    pause_loop,
    resume_loop,
    cancel_loop,
    list_active_loops,
)

logger = logging.getLogger("ohvis.loop_chat_handler")

LOOP_START_KW = ("감시해", "감시하고", "모니터링해", "모니터해", "반복해", "반복 실행", "루프 시작", "루프 돌려")
LOOP_STOP_KW = ("루프 중지", "루프 정지", "루프 취소", "감시 중지", "감시 취소", "루프 멈춰", "루프 중단")
LOOP_RESUME_KW = ("루프 재개", "루프 재시작", "감시 재개")
LOOP_STATUS_KW = ("루프 상태", "루프 목록", "감시 목록", "활성 루프")

_INTERVAL_RE = re.compile(r"매\s*(\d+)\s*(초|분|시간)")
_LOOP_ID_RE = re.compile(r"(?:루프|loop)\s*#?\s*(\d+)")


_LOOP_ANCHOR = re.compile(r"루프|loop|감시|모니터")
_STOP_ACTION = re.compile(r"중지|정지|취소|멈춰|중단|삭제|stop|cancel")
_RESUME_ACTION = re.compile(r"재개|재시작|resume")
_STATUS_ACTION = re.compile(r"상태|목록|list|status")


def detect_loop_intent(content: str) -> str | None:
    if any(kw in content for kw in LOOP_START_KW):
        return "loop_start"
    has_anchor = bool(_LOOP_ANCHOR.search(content))
    if has_anchor and _STOP_ACTION.search(content):
        return "loop_stop"
    if has_anchor and _RESUME_ACTION.search(content):
        return "loop_resume"
    if has_anchor and _STATUS_ACTION.search(content):
        return "loop_status"
    return None


async def handle_loop_start(
    content: str, session_id: str, model_id: str | None = None,
) -> dict[str, Any]:
    loop_type = "monitor"
    if any(kw in content for kw in ("순차", "단계별")):
        loop_type = "sequential"
    elif not any(kw in content for kw in ("감시", "모니터")):
        loop_type = "task"

    interval = None
    m = _INTERVAL_RE.search(content)
    if m:
        num = int(m.group(1))
        unit = m.group(2)
        interval = num * {"초": 1, "분": 60, "시간": 3600}[unit]

    result = await create_loop(
        loop_type=loop_type,
        original_command=content,
        parsed_intent={"source": "chat", "keywords_matched": True},
        interval_seconds=interval,
        execution_model_id=model_id,
        session_id=session_id,
    )

    msg = (
        f"✅ **루프 #{result['id']} 생성 완료**\n\n"
        f"- **유형**: {result['loop_type']}\n"
        f"- **모델**: {result.get('execution_model_id') or 'default'}\n"
        f"- **비용 한도**: ${float(result['max_cost_usd']):.2f}\n"
        f"- **최대 반복**: {result['max_iterations']}회\n"
        f"- **간격**: {result.get('interval_seconds') or 'N/A'}초\n\n"
        f"스케줄러가 자동으로 실행합니다."
    )
    return {"ok": True, "loop_id": result["id"], "message": msg}


async def handle_loop_stop(content: str, session_id: str) -> dict[str, Any]:
    m = _LOOP_ID_RE.search(content)
    if m:
        loop_id = int(m.group(1))
    else:
        loops = await list_active_loops()
        session_loops = [lp for lp in loops if lp.get("session_id") == session_id]
        if not session_loops:
            all_active = [lp for lp in loops]
            if not all_active:
                return {"ok": False, "message": "⚠️ 활성 루프가 없습니다."}
            loop_id = all_active[-1]["id"]
        else:
            loop_id = session_loops[-1]["id"]

    if "취소" in content or "삭제" in content:
        result = await cancel_loop(loop_id)
        action = "취소"
    else:
        result = await pause_loop(loop_id)
        action = "중지"

    if result:
        return {"ok": True, "message": f"✅ 루프 #{loop_id} {action} 완료 (상태: {result['status']})"}
    return {"ok": False, "message": f"⚠️ 루프 #{loop_id}을 찾을 수 없거나 이미 종료되었습니다."}


async def handle_loop_resume(content: str, session_id: str) -> dict[str, Any]:
    m = _LOOP_ID_RE.search(content)
    if m:
        loop_id = int(m.group(1))
    else:
        return {"ok": False, "message": "⚠️ 재개할 루프 번호를 지정해주세요. (예: 루프 #1 재개)"}

    result = await resume_loop(loop_id)
    if result:
        return {"ok": True, "message": f"✅ 루프 #{loop_id} 재개 완료"}
    return {"ok": False, "message": f"⚠️ 루프 #{loop_id}을 재개할 수 없습니다. (paused 상태가 아닐 수 있음)"}


async def handle_loop_status(session_id: str) -> dict[str, Any]:
    loops = await list_active_loops()
    if not loops:
        return {"ok": True, "message": "📋 현재 활성 루프가 없습니다."}

    lines = ["📋 **활성 루프 목록**\n"]
    for lp in loops:
        lines.append(
            f"- **#{lp['id']}** [{lp['loop_type']}] "
            f"반복 {lp.get('current_iteration') or 0}/{lp.get('max_iterations') or '∞'} "
            f"비용 ${float(lp.get('total_cost_usd') or 0):.2f}/${float(lp.get('max_cost_usd') or 0):.2f} "
            f"({lp['status']})"
        )
    return {"ok": True, "message": "\n".join(lines)}
