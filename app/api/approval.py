"""
T-039: CEO 승인 큐 API
에러 → 승인 요청 → CEO 텔레그램 승인/반려 → 자동 실행
"""
import os
import shlex
import subprocess
import urllib.request
import urllib.error
import json
import structlog
from datetime import datetime, timezone
from typing import Optional, List
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from app.api.context import verify_monitor_key
from app.memory.store import memory_store

logger = structlog.get_logger()
router = APIRouter()


class ApprovalRequest(BaseModel):
    error_log_id: Optional[int] = None
    title: str
    description: str
    suggested_action: str
    action_type: str           # auto_command, claude_code, manual
    action_command: Optional[str] = None
    target_server: str         # 68, 211, 114, NAS
    severity: str = "medium"   # critical, high, medium, low


# --- 승인 요청 생성 ---
@router.post("/approval/request")
async def create_approval_request(
    req: ApprovalRequest,
    auth: bool = Depends(verify_monitor_key),
):
    """승인 요청 생성 → 텔레그램 알림 발송."""
    async with memory_store.pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO approval_queue
                (error_log_id, title, description, suggested_action,
                 action_type, action_command, target_server, severity)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING id
        """, req.error_log_id, req.title, req.description,
            req.suggested_action, req.action_type,
            req.action_command, req.target_server, req.severity)

        approval_id = row["id"]

        # 텔레그램 알림 발송
        telegram_msg_id = await _send_telegram_approval(approval_id, req)

        if telegram_msg_id:
            await conn.execute(
                "UPDATE approval_queue SET telegram_message_id=$1 WHERE id=$2",
                telegram_msg_id, approval_id
            )

        return {
            "status": "ok",
            "approval_id": approval_id,
            "telegram_sent": telegram_msg_id is not None,
        }


# --- CEO 승인 ---
@router.post("/approval/{approval_id}/approve")
async def approve_action(
    approval_id: int,
    auth: bool = Depends(verify_monitor_key),
):
    """CEO 승인 → 자동 실행."""
    async with memory_store.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM approval_queue WHERE id=$1", approval_id
        )
        if not row:
            raise HTTPException(404, "Approval request not found")
        if row["status"] != "pending":
            raise HTTPException(400, f"Already {row['status']}")

        await conn.execute("""
            UPDATE approval_queue
            SET status='approved', responded_at=NOW()
            WHERE id=$1
        """, approval_id)

        # 승인 후 자동 실행
        result = await _execute_approved_action(row)

        status = "executed" if result["success"] else "failed"
        await conn.execute("""
            UPDATE approval_queue
            SET status=$2, executed_at=NOW(), execution_result=$3
            WHERE id=$1
        """, approval_id, status, result["output"][:2000])

        return {
            "status": status,
            "approval_id": approval_id,
            "execution_result": result,
        }


# --- CEO 반려 ---
@router.post("/approval/{approval_id}/reject")
async def reject_action(
    approval_id: int,
    auth: bool = Depends(verify_monitor_key),
):
    """CEO 반려."""
    async with memory_store.pool.acquire() as conn:
        result = await conn.execute("""
            UPDATE approval_queue
            SET status='rejected', responded_at=NOW()
            WHERE id=$1 AND status='pending'
        """, approval_id)
        if result == "UPDATE 0":
            raise HTTPException(404, "Not found or already processed")
        return {"status": "rejected", "approval_id": approval_id}


# --- 승인 대기 목록 조회 ---
@router.get("/approval/pending")
async def list_pending(auth: bool = Depends(verify_monitor_key)):
    async with memory_store.pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, title, description, suggested_action, action_type,
                   target_server, severity, requested_at
            FROM approval_queue WHERE status='pending'
            ORDER BY
                CASE severity
                    WHEN 'critical' THEN 0
                    WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2
                    WHEN 'low' THEN 3
                END,
                requested_at ASC
        """)
        return {"status": "ok", "count": len(rows), "pending": [dict(r) for r in rows]}


# --- 전체 이력 조회 ---
@router.get("/approval/history")
async def approval_history(limit: int = 50, auth: bool = Depends(verify_monitor_key)):
    async with memory_store.pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, title, target_server, severity, status,
                   action_type, requested_at, responded_at, executed_at
            FROM approval_queue
            ORDER BY requested_at DESC LIMIT $1
        """, limit)
        return {"status": "ok", "count": len(rows), "history": [dict(r) for r in rows]}


# --- 텔레그램 승인 요청 발송 ---
async def _send_telegram_approval(approval_id: int, req: ApprovalRequest) -> Optional[int]:
    """CEO에게 승인/반려 인라인 버튼 포함 텔레그램 메시지 발송."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    if not bot_token or not chat_id:
        logger.warning("telegram_not_configured")
        return None

    severity_emoji = {
        "critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"
    }
    emoji = severity_emoji.get(req.severity, "⚪")

    # HTML 파싱 사용 (Markdown 특수문자 충돌 방지)
    def _esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    text = (
        f"{emoji} <b>AADS Watchdog 승인 요청 #{approval_id}</b>\n\n"
        f"<b>서버</b>: {_esc(req.target_server)}\n"
        f"<b>제목</b>: {_esc(req.title)}\n"
        f"<b>설명</b>: {_esc(req.description[:300])}\n\n"
        f"<b>제안 조치</b>: {_esc(req.suggested_action[:300])}\n"
        f"<b>실행 방식</b>: {_esc(req.action_type)}\n"
    )
    if req.action_command:
        text += f"<b>명령</b>: <code>{_esc(req.action_command[:200])}</code>\n"

    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ 승인", "callback_data": f"approve_{approval_id}"},
            {"text": "❌ 반려", "callback_data": f"reject_{approval_id}"},
        ]]
    }

    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": keyboard,
    }).encode()

    try:
        req_obj = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_obj, timeout=10) as resp:
            data = json.loads(resp.read())
            if data.get("ok"):
                return data["result"]["message_id"]
        return None
    except Exception as e:
        logger.error("telegram_send_failed", error=str(e))
        return None


# --- 승인된 작업 실행 ---
async def _execute_approved_action(row) -> dict:
    """action_type에 따라 실행."""
    action_type = row["action_type"]
    command = row["action_command"]
    target = row["target_server"]

    if action_type == "auto_command":
        return await _run_command(command, target)
    elif action_type == "claude_code":
        return await _run_claude_code(command, target)
    elif action_type == "manual":
        return {"success": True, "output": "Manual action approved. CEO will handle."}
    return {"success": False, "output": f"Unknown action_type: {action_type}"}


async def _run_command(command: str, target_server: str) -> dict:
    """서버에서 명령 실행. 68=로컬, 그 외=SSH."""
    SAFE_PREFIXES = [
        "docker restart", "docker compose",
        "systemctl reload", "systemctl restart",
        "curl",
        "sudo systemctl restart",
        "sudo systemctl reload",
        "npm run build",
        "pm2 restart",
    ]

    if not command:
        return {"success": False, "output": "No command specified"}

    if not any(command.strip().startswith(p) for p in SAFE_PREFIXES):
        return {"success": False, "output": f"Blocked unsafe command: {command[:100]}"}

    try:
        if target_server == "68":
            result = subprocess.run(
                shlex.split(command), shell=False, capture_output=True, text=True, timeout=120
            )
        else:
            ssh_key_map = {
                "211": "/root/.ssh/id_ed25519_newtalk",
                "114": "/root/.ssh/id_ed25519_newtalk",
            }
            ssh_host_map = {
                "211": os.getenv("SERVER_211_HOST", "211.188.51.113"),
                "114": os.getenv("SERVER_114_HOST", ""),
            }
            key = ssh_key_map.get(target_server, "")
            host = ssh_host_map.get(target_server, "")

            if not host:
                return {"success": False, "output": f"No SSH config for server {target_server}"}

            ssh_cmd = [
                "ssh", "-i", key,
                "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=10",
                f"root@{host}",
                shlex.quote(command),
            ]
            result = subprocess.run(
                ssh_cmd, shell=False, capture_output=True, text=True, timeout=120
            )

        return {
            "success": result.returncode == 0,
            "output": (result.stdout + result.stderr)[:2000],
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "output": "Command timed out (120s)"}
    except Exception as e:
        return {"success": False, "output": str(e)}


async def _run_claude_code(instruction: str, target_server: str) -> dict:
    """Claude Code에 지시서를 전달하여 자동 실행."""
    try:
        claude_cmd = f'claude -p "{instruction}" --allowedTools "Bash,Write,Read" --max-turns 20'

        from app.core.project_config import get_server_by_number
        work_dir = get_server_by_number(target_server).get("workdir", "/root")

        result = subprocess.run(
            claude_cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=600,
            cwd=work_dir,
            env={**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": os.getenv("CLAUDE_CODE_OAUTH_TOKEN", "")},
        )

        return {
            "success": result.returncode == 0,
            "output": (
                (result.stdout[-2000:] if result.stdout else "") +
                (result.stderr[-500:] if result.stderr else "")
            ),
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "output": "Claude Code timed out (600s)"}
    except Exception as e:
        return {"success": False, "output": str(e)}
