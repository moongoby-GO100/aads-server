"""
AADS-188C: Claude Agent SDK 훅 — PreToolUse / PostToolUse / stop 훅 구현.
위험 명령 차단, 안전 명령 자동 승인 (root 환경 bypassPermissions 대체).
Langfuse span 기록, 세션 종료 시 메모리 자동 저장.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# SDK 타입 참조 (훅 응답은 dict 형태로 반환 — {"behavior": "allow"} 또는 {"behavior": "deny", "message": "..."})

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


# ─── PreToolUse Hook (SDK PermissionRequest 자동 승인) ──────────────────────

async def pre_tool_use_hook(
    hook_input: Any,
    tool_use_id: Optional[str] = None,
    context: Any = None,
) -> Any:
    """
    도구 실행 전 검사 + 자동 승인.
    root 환경에서 bypassPermissions 불가하므로 이 훅에서 안전한 도구를 자동 승인한다.

    SDK PreToolUseHookInput/PermissionRequestHookInput 호환:
    - 안전: PermissionResultAllow() 반환
    - 위험: PermissionResultDeny(message="이유") 반환
    """
    # SDK 타입 or dict 모두 지원
    if isinstance(hook_input, dict):
        tool_name = hook_input.get("tool_name", "") or hook_input.get("tool", {}).get("name", "")
        tool_input = hook_input.get("tool_input", {}) or hook_input.get("tool", {}).get("input", {})
    else:
        # SDK PermissionRequestHookInput / PreToolUseHookInput
        tool_obj = getattr(hook_input, "tool", None)
        tool_name = getattr(tool_obj, "name", "") if tool_obj else ""
        tool_input = getattr(tool_obj, "input", {}) if tool_obj else {}
        if not tool_input:
            tool_input = {}

    # ── Bash 위험 명령 차단 ─────────────────────────────────────────────────
    if tool_name == "Bash":
        command = ""
        if isinstance(tool_input, dict):
            command = tool_input.get("command", "") or tool_input.get("cmd", "") or ""
        for pattern in _DANGEROUS_BASH_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                reason = f"위험 Bash 명령 차단: {command[:120]}"
                logger.warning(f"pre_tool_use: {reason}")
                return {"behavior": "deny", "message": reason}

    # ── Write/Edit 민감 경로 차단 ───────────────────────────────────────────
    if tool_name in ("Write", "Edit"):
        file_path = ""
        if isinstance(tool_input, dict):
            file_path = tool_input.get("file_path", "") or tool_input.get("path", "") or ""
        for sensitive in _SENSITIVE_WRITE_PATHS:
            if sensitive in file_path:
                reason = f"민감 경로 Write 차단: {file_path}"
                logger.warning(f"pre_tool_use: {reason}")
                return {"behavior": "deny", "message": reason}

    # ── 안전 → 자동 승인 ─────────────────────────────────────────────────
    logger.debug(f"pre_tool_use: 자동 승인 | tool={tool_name}")
    return {"behavior": "allow"}


# ─── PostToolUse Hook ─────────────────────────────────────────────────────────

async def post_tool_use_hook(
    hook_input: Any,
    tool_use_id: Optional[str] = None,
    context: Any = None,
) -> Any:
    """도구 실행 후 처리 (로깅 전용)."""
    if isinstance(hook_input, dict):
        tool_name = hook_input.get("tool_name", "")
    else:
        tool_obj = getattr(hook_input, "tool", None)
        tool_name = getattr(tool_obj, "name", "") if tool_obj else ""

    logger.debug(f"post_tool_use: tool={tool_name}")
    return {}


# ─── Stop Hook ────────────────────────────────────────────────────────────────

async def stop_hook(
    hook_input: Any,
    context: Any = None,
) -> Any:
    """
    세션 종료 시:
    - ai_observations 자동 저장 (AADS-186E-3)
    - HANDOVER용 세션 요약 노트 생성 (AADS-186E-2)
    """
    if isinstance(hook_input, dict):
        session_id = hook_input.get("session_id", "sdk_session")
        messages = hook_input.get("messages", [])
    else:
        session_id = getattr(hook_input, "session_id", "sdk_session")
        messages = getattr(hook_input, "messages", [])

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
