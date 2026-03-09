"""
AADS-188C: Claude Agent SDK 훅 — PreToolUse / PostToolUse / stop 훅 구현.
위험 명령 차단, Langfuse span 기록, 세션 종료 시 메모리 자동 저장.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# ─── 위험 패턴 상수 ────────────────────────────────────────────────────────────

# Bash 위험 명령 패턴 (Red 등급)
_DANGEROUS_BASH_PATTERNS: List[str] = [
    r"rm\s+-[rf]{1,2}\s*/",           # rm -rf /...
    r"rm\s+-[rf]{1,2}\s+\.",          # rm -rf .
    r"DROP\s+(TABLE|DATABASE|SCHEMA)", # SQL DROP
    r"DELETE\s+FROM\s+\w",             # SQL DELETE
    r"\bshutdown\b",
    r"\bhalt\b",
    r"\breboot\b",
    r"\bmkfs\b",
    r"dd\s+if=",
    r">\s*/dev/(sda|nvme|hda)",
    r"chmod\s+[0-7]{3,4}\s+/",
    r"kill\s+-9\s+1\b",               # init 프로세스 kill
    r"pkill\s+-9\s+",
    r"truncate\s+--all",
    r":(){:|:&};:",                    # fork bomb
]

# Write/Edit 차단 경로
_SENSITIVE_WRITE_PATHS: List[str] = [
    ".env",
    ".env.",
    ".ssh/",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "/etc/passwd",
    "/etc/shadow",
    "/etc/sudoers",
    "credentials.json",
    "secrets",
    ".aws/credentials",
    ".netrc",
]


# ─── PreToolUse Hook ──────────────────────────────────────────────────────────

async def pre_tool_use_hook(
    input_data: Dict[str, Any],
    tool_use_id: str,
    context: Any,
) -> Dict[str, Any]:
    """
    도구 실행 전 검사:
    - Bash: 위험 명령 패턴 차단
    - Write/Edit: 민감 경로 차단
    - 모든 도구: Langfuse span 시작
    """
    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})

    # ── Bash 위험 명령 차단 ─────────────────────────────────────────────────
    if tool_name == "Bash":
        command = (
            tool_input.get("command", "")
            or tool_input.get("cmd", "")
            or ""
        )
        for pattern in _DANGEROUS_BASH_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                reason = f"위험 Bash 명령 차단: pattern={pattern!r} cmd={command[:120]!r}"
                logger.warning(f"pre_tool_use: {reason} | tool_use_id={tool_use_id}")
                return {"block": True, "reason": reason}

    # ── Write/Edit 민감 경로 차단 ───────────────────────────────────────────
    if tool_name in ("Write", "Edit"):
        file_path = (
            tool_input.get("file_path", "")
            or tool_input.get("path", "")
            or ""
        )
        for sensitive in _SENSITIVE_WRITE_PATHS:
            if sensitive in file_path:
                reason = f"민감 경로 Write 차단: path={file_path!r}"
                logger.warning(f"pre_tool_use: {reason} | tool_use_id={tool_use_id}")
                return {"block": True, "reason": reason}

    # ── Langfuse span 시작 (optional) ──────────────────────────────────────
    try:
        from app.core.langfuse_config import is_enabled as langfuse_is_enabled
        if langfuse_is_enabled():
            logger.debug(
                f"pre_tool_use: langfuse span 시작 | tool={tool_name} id={tool_use_id}"
            )
            # 실제 Langfuse span 객체는 context에 저장하여 PostToolUse에서 종료
            span_store = getattr(context, "_langfuse_spans", None)
            if span_store is not None:
                try:
                    from app.core.langfuse_config import create_trace
                    span = create_trace(
                        name=f"tool_{tool_name}",
                        input_data={"tool_use_id": tool_use_id, "input": tool_input},
                    )
                    span_store[tool_use_id] = span
                except Exception:
                    pass
    except Exception:
        pass

    return {}


# ─── PostToolUse Hook ─────────────────────────────────────────────────────────

async def post_tool_use_hook(
    input_data: Dict[str, Any],
    tool_use_id: str,
    context: Any,
) -> Dict[str, Any]:
    """
    도구 실행 후 처리:
    - Write/Edit 결과 → diff_preview SSE 이벤트 전송
    - Langfuse span 종료 + 비용 기록
    """
    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})
    tool_output = input_data.get("tool_output", {})

    # ── Write/Edit → diff_preview SSE ──────────────────────────────────────
    if tool_name in ("Write", "Edit"):
        file_path = (
            tool_input.get("file_path", "")
            or tool_input.get("path", "")
            or ""
        )
        try:
            sse_callback = getattr(context, "sse_callback", None)
            if sse_callback and callable(sse_callback):
                payload = json.dumps({
                    "type": "diff_preview",
                    "file_path": file_path,
                    "tool_use_id": tool_use_id,
                })
                await sse_callback(f"data: {payload}\n\n")
        except Exception as e:
            logger.debug(f"post_tool_use: diff_preview 전송 실패: {e}")

    # ── Langfuse span 종료 ──────────────────────────────────────────────────
    try:
        from app.core.langfuse_config import is_enabled as langfuse_is_enabled
        if langfuse_is_enabled():
            span_store = getattr(context, "_langfuse_spans", None)
            if span_store and tool_use_id in span_store:
                span = span_store.pop(tool_use_id)
                output_str = str(tool_output)[:500] if tool_output else ""
                try:
                    span.end(output={"result": output_str})
                except Exception:
                    pass
    except Exception:
        pass

    return {}


# ─── Stop Hook ────────────────────────────────────────────────────────────────

async def stop_hook(
    input_data: Dict[str, Any],
    context: Any,
) -> Dict[str, Any]:
    """
    세션 종료 시:
    - ai_observations 자동 저장 (AADS-186E-3)
    - HANDOVER용 세션 요약 노트 생성 (AADS-186E-2)
    """
    session_id = (
        getattr(context, "session_id", None)
        or input_data.get("session_id", "sdk_session")
    )
    messages = (
        getattr(context, "messages", [])
        or input_data.get("messages", [])
    )

    # ── ai_observations 자동 저장 ───────────────────────────────────────────
    try:
        from app.services.memory_manager import get_memory_manager
        mgr = get_memory_manager()
        if messages:
            await mgr.auto_observe_from_session(messages)
            logger.info(f"stop_hook: ai_observations 저장 완료 | session={session_id}")
    except Exception as e:
        logger.debug(f"stop_hook: ai_observations 저장 실패: {e}")

    # ── 세션 노트 저장 (HANDOVER용 요약) ───────────────────────────────────
    try:
        from app.services.memory_manager import get_memory_manager
        mgr = get_memory_manager()
        if messages and len(messages) >= 3:
            await mgr.save_session_note(
                session_id=str(session_id),
                messages=messages,
            )
            logger.info(f"stop_hook: 세션 노트 저장 완료 | session={session_id}")
    except Exception as e:
        logger.debug(f"stop_hook: 세션 노트 저장 실패: {e}")

    return {}
