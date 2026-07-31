"""OHVIS Loop Chat Handler — 채팅 루프 명령 파싱 및 처리.

채팅에서 "감시해", "루프 중지", "루프 상태" 등의 명령을 감지하여
loop_controller를 호출하고 응답 메시지를 반환한다.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from app.services.loop_controller import (
    cancel_loop,
    create_loop,
    list_active_loops,
    pause_loop,
    resume_loop,
)

logger = logging.getLogger("ohvis.loop_chat_handler")

LOOP_START_KW = (
    "감시해", "감시하고", "모니터링해", "모니터해", "반복해", "반복 실행",
    "루프 시작", "루프 돌려",
    "완료시까지", "완료할 때까지", "완료할때까지", "끝날 때까지", "끝날때까지",
    "될 때까지", "될때까지", "계속 진행", "계속 실행",
)
# "매 N분/초/시간" 패턴만 루프로 인식 (단독 "매 "는 오탐 위험)
_INTERVAL_START_RE = re.compile(r"매\s*\d+\s*(?:초|분|시간)")

# CEO 확인 프롬프트 승인 키워드 → loop_start (확인 없이 즉시 생성)
LOOP_CONFIRM_KW = ("루프 시작", "루프 진행", "루프 승인", "루프 생성")

# P1 보완: 확인 프롬프트에서 제시한 원본 지시를 승인 시점까지 보관한다.
# 승인 응답("루프 진행")만으로 루프를 만들면 original_command/interval이
# 소실되어 루프가 무의미한 명령을 반복 실행하는 사고가 발생한다.
_PENDING_CONFIRM: dict[str, tuple[float, str]] = {}
_PENDING_TTL_SEC = 900.0


def _remember_pending(session_id: str, content: str) -> None:
    if not session_id:
        return
    now = time.time()
    for k, (ts, _) in list(_PENDING_CONFIRM.items()):
        if now - ts > _PENDING_TTL_SEC:
            _PENDING_CONFIRM.pop(k, None)
    _PENDING_CONFIRM[session_id] = (now, content)


def _take_pending(session_id: str, content: str) -> str:
    """승인 응답이면 보관된 원본 지시로 치환한다. 없으면 원문 유지."""
    if not session_id:
        return content
    entry = _PENDING_CONFIRM.pop(session_id, None)
    if not entry:
        return content
    ts, original = entry
    if time.time() - ts > _PENDING_TTL_SEC:
        return content
    # 승인 키워드만 있는 짧은 응답일 때만 치환 (새 지시는 그대로 사용)
    stripped = content
    for kw in LOOP_CONFIRM_KW:
        stripped = stripped.replace(kw, "")
    if len(stripped.strip()) <= 10:
        return original
    return content
LOOP_STOP_KW = ("루프 중지", "루프 정지", "루프 취소", "감시 중지", "감시 취소", "루프 멈춰", "루프 중단")
LOOP_RESUME_KW = ("루프 재개", "루프 재시작", "감시 재개")
LOOP_STATUS_KW = ("루프 상태", "루프 목록", "감시 목록", "활성 루프")

_INTERVAL_RE = re.compile(r"매\s*(\d+)\s*(초|분|시간)")
_LOOP_ID_RE = re.compile(r"(?:루프|loop)\s*#?\s*(\d+)")


# ── 오탐 방지 가드 (AADS-LOOP-FP-001) ──────────────────────────────
# 기존 구현은 "루프|감시" 앵커와 "중지|상태" 액션이 문서 어디에든 각각
# 존재하면 루프 명령으로 판정했다. reply_to 인용문(최대 2000자)이나
# 루프 관련 보고 요청("루프 구현상태 보고해")까지 루프 명령으로 오분류되어
# "⚠️ 활성 루프가 없습니다."만 응답하고 본 답변이 차단되는 사고가 있었다.
# → ① 길이 제한 ② 서술/질의형 제외 ③ 앵커-액션 인접 강제 3중 가드.
_LOOP_CMD_MAX_LEN = 200

# 보고/설명/질의 요청은 루프 제어 명령이 아니다.
_NON_COMMAND_HINT = re.compile(
    r"보고해|보고하|보고드|보고 |설명|뭐지|뭔가|무엇|어떻게|어떤|구현|기획|"
    r"문서|분석|검토|점검|정리해|알려주|가르쳐|차이|의미"
)

_ANCHOR = r"(?:루프|loop|감시|모니터링|모니터)"
_SUFFIX = r"\s*(?:#?\s*\d+\s*)?(?:을|를|은|는|이|가)?\s*"
_STOP_CMD = re.compile(
    _ANCHOR + _SUFFIX + r"(?:중지|정지|취소|멈춰|멈춰줘|중단|삭제|stop|cancel)"
)
_RESUME_CMD = re.compile(_ANCHOR + _SUFFIX + r"(?:재개|재시작|resume)")
_STATUS_CMD = re.compile(_ANCHOR + _SUFFIX + r"(?:상태|목록|list|status)")


def detect_loop_intent(content: str) -> str | None:
    """채팅 입력에서 루프 제어 명령만 정확히 판정한다.

    호출자는 반드시 reply_to 인용문/재개 스캐폴드가 제거된
    사용자 원문(persisted_user_content)을 전달해야 한다.

    P0: _NON_COMMAND_HINT는 STOP/STATUS/RESUME 오탐 방지에만 적용.
        START에는 적용하지 않음 (CEO 화법 "…하고 보고해" 차단 방지).
    P1: START 판정 시 "loop_start_confirm" 반환 → 확인 프롬프트.
    """
    text = str(content or "").strip()
    if not text or len(text) > _LOOP_CMD_MAX_LEN:
        return None

    # CEO 승인 응답 → 즉시 루프 생성 (확인 프롬프트 스킵)
    if any(kw in text for kw in LOOP_CONFIRM_KW):
        return "loop_start"

    # START는 _NON_COMMAND_HINT 가드 없이 판정 (CEO 화법 호환)
    if any(kw in text for kw in LOOP_START_KW) or _INTERVAL_START_RE.search(text):
        return "loop_start_confirm"

    # STOP/RESUME/STATUS는 오탐 방지 가드 적용
    if _NON_COMMAND_HINT.search(text):
        return None
    if any(kw in text for kw in LOOP_STOP_KW) or _STOP_CMD.search(text):
        return "loop_stop"
    if any(kw in text for kw in LOOP_RESUME_KW) or _RESUME_CMD.search(text):
        return "loop_resume"
    if any(kw in text for kw in LOOP_STATUS_KW) or _STATUS_CMD.search(text):
        return "loop_status"
    return None


async def handle_loop_start_confirm(
    content: str, session_id: str, model_id: str | None = None,
) -> dict[str, Any]:
    """P1: 루프 생성 전 확인 프롬프트를 반환한다."""
    _remember_pending(session_id, content)
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

    interval_desc = f"{interval}초" if interval else "기본값"
    msg = (
        f"🔄 **루프로 진행할까요?**\n\n"
        f"- **유형**: {loop_type}\n"
        f"- **반복 간격**: {interval_desc}\n"
        f"- **명령**: {content[:80]}{'…' if len(content) > 80 else ''}\n\n"
        f"→ **'루프 진행'** 또는 **'루프 시작'**으로 승인하시면 루프를 생성합니다.\n"
        f"→ **'아니'** 또는 다른 지시를 하시면 단일 턴으로 처리합니다."
    )
    return {
        "ok": True,
        "pending_confirm": True,
        "loop_type": loop_type,
        "interval_seconds": interval,
        "message": msg,
    }


async def handle_loop_start(
    content: str, session_id: str, model_id: str | None = None,
) -> dict[str, Any]:
    # 승인 응답("루프 진행")이면 확인 프롬프트에 제시했던 원본 지시로 복원
    content = _take_pending(session_id, content)
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
