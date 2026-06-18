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

# ─── 고위험 작업 승인 정책 ───────────────────────────────────────────────────

_HIGH_RISK_ACTION_APPROVAL_POLICY = {
    "approval_required": {
        "git_push": "git push는 CEO 명시 승인 또는 Pipeline Runner 승인 이후에만 실행",
        "deploy": "배포/재시작/컨테이너 전환은 CEO 명시 승인 또는 승인된 배포 단계에서만 실행",
        "ssh": "SSH 원격 실행은 대상/영향 범위가 명확한 CEO 요청이 있을 때만 실행",
        "docker": "docker build/run/exec/restart/pull/push는 승인된 운영 작업에서만 실행",
        "payment": "외부결제/환불/과금 작업은 별도 CEO 확인 없이는 실행 금지",
    },
    "always_deny": {
        "force_push": "force push는 차단",
        "destructive_delete": "루트/상위 경로 파괴 삭제는 차단",
        "destructive_sql": "DROP/TRUNCATE 등 파괴 SQL은 차단",
        "shutdown": "shutdown/reboot/halt 계열은 차단",
        "secret_write": "시크릿 경로 쓰기는 차단",
    },
}

_APPROVAL_REQUIRED_COMMAND_PATTERNS: List[tuple[str, tuple[str, ...]]] = [
    ("git_push", (r"\bgit\s+push\b",)),
    ("deploy", (r"\bdeploy\.sh\b", r"\bsystemctl\s+restart\b", r"\bsupervisorctl\s+restart\b")),
    ("docker", (r"\bdocker\s+(?:build|run|exec|restart|start|stop|pull|push)\b", r"\bdocker\s+compose\b")),
    ("ssh", (r"\bssh\b",)),
    ("payment", (r"\bpayment\b", r"\b결제\b", r"\brefund\b", r"\bcharge\b", r"\bbilling\b", r"\binvoice\b")),
]


def _detect_approval_required_action(tool_name: str, tool_input: dict[str, Any]) -> tuple[str, str] | None:
    command = ""
    if isinstance(tool_input, dict):
        command = str(tool_input.get("command", "") or tool_input.get("cmd", "") or "").strip()
    if tool_name == "git_remote_push":
        return "git_push", command or tool_name
    if tool_name not in ("Bash", "run_remote_command"):
        return None
    if not command:
        return None
    for category, patterns in _APPROVAL_REQUIRED_COMMAND_PATTERNS:
        for pattern in patterns:
            if re.search(pattern, command, re.IGNORECASE):
                return category, command[:160]
    return None


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
        approval_required = _detect_approval_required_action(tool_name, tool_input if isinstance(tool_input, dict) else {})
        if approval_required:
            category, preview = approval_required
            policy = _HIGH_RISK_ACTION_APPROVAL_POLICY["approval_required"].get(category, "고위험 작업은 승인 필요")
            logger.warning(f"pre_tool_use: approval_required_but_allowed_by_context | {policy}: {preview[:120]}")

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

    # ── write_remote_file / patch_remote_file 민감 경로 차단 ─────────────
    if tool_name in ("write_remote_file", "patch_remote_file"):
        file_path = ""
        if isinstance(tool_input, dict):
            file_path = tool_input.get("file_path", "") or tool_input.get("path", "") or ""
        for sensitive in _SENSITIVE_WRITE_PATHS:
            if sensitive in file_path:
                reason = f"원격 민감 경로 쓰기 차단: {file_path}"
                logger.warning(f"pre_tool_use: {reason}")
                return {"behavior": "deny", "message": reason}
        logger.info(f"pre_tool_use: Yellow 도구 자동 승인 | tool={tool_name} path={file_path}")

    # ── run_remote_command 위험 명령 차단 ─────────────────────────────────
    if tool_name == "run_remote_command":
        command = ""
        if isinstance(tool_input, dict):
            command = tool_input.get("command", "") or ""
        for pattern in _DANGEROUS_BASH_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                reason = f"원격 위험 명령 차단: {command[:120]}"
                logger.warning(f"pre_tool_use: {reason}")
                return {"behavior": "deny", "message": reason}
        # force push 차단
        if re.search(r"git\s+push\s+.*--force", command, re.IGNORECASE):
            reason = f"force push 차단: {command[:120]}"
            logger.warning(f"pre_tool_use: {reason}")
            return {"behavior": "deny", "message": reason}
        approval_required = _detect_approval_required_action(tool_name, tool_input if isinstance(tool_input, dict) else {})
        if approval_required:
            category, preview = approval_required
            policy = _HIGH_RISK_ACTION_APPROVAL_POLICY["approval_required"].get(category, "고위험 작업은 승인 필요")
            logger.warning(f"pre_tool_use: approval_required_but_allowed_by_context | {policy}: {preview[:120]}")
        logger.info(f"pre_tool_use: Yellow 도구 자동 승인 | tool={tool_name} cmd={command[:80]}")

    # ── git_remote_push force push 차단 ──────────────────────────────────
    if tool_name == "git_remote_push":
        policy = _HIGH_RISK_ACTION_APPROVAL_POLICY["approval_required"]["git_push"]
        logger.warning(f"pre_tool_use: approval_required_but_allowed_by_context | {policy} | tool={tool_name}")

    # ── query_project_database SQL 검증 ──────────────────────────────────
    if tool_name == "query_project_database":
        query = ""
        if isinstance(tool_input, dict):
            query = tool_input.get("query", "") or ""
        # validate_query 호출
        try:
            from app.api.ceo_chat_tools_db import validate_query
            error = validate_query(query)
            if error:
                reason = f"프로젝트 DB 쿼리 차단: {error} | query={query[:120]}"
                logger.warning(f"pre_tool_use: {reason}")
                return {"behavior": "deny", "message": reason}
        except ImportError:
            pass
        logger.info(f"pre_tool_use: Yellow 도구 자동 승인 | tool={tool_name} query={query[:80]}")

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
