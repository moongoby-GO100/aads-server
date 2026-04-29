"""통합 디바이스 API — PC/Android/iOS 에이전트 WebSocket + REST."""
from __future__ import annotations

import hashlib
import io
import logging
import os
import secrets
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.auth import get_current_user
from app.services.device_manager import device_manager

logger = logging.getLogger(__name__)
router = APIRouter()

HEARTBEAT_TIMEOUT = 50.0
_LOCAL_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_HOST_PROJECT_ROOT = Path("/root/aads/aads-server")
PROJECT_ROOT = _HOST_PROJECT_ROOT if (_HOST_PROJECT_ROOT / "android_agent").is_dir() else _LOCAL_PROJECT_ROOT
ANDROID_AGENT_DIR = PROJECT_ROOT / "android_agent"
ANDROID_DIST_DIR = ANDROID_AGENT_DIR / "dist"
ANDROID_APK_NAME = "aads-agent-debug.apk"
ANDROID_APK_CANDIDATES = (
    ANDROID_DIST_DIR / ANDROID_APK_NAME,
    ANDROID_AGENT_DIR / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk",
)

_PAIRING_TABLE_READY = False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _public_ws_base_url() -> str:
    configured = os.environ.get("AADS_DEVICE_WS_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    public_base = os.environ.get("AADS_PUBLIC_BASE_URL", "https://aads.newtalk.kr").rstrip("/")
    if public_base.startswith("https://"):
        public_base = "wss://" + public_base[len("https://"):]
    elif public_base.startswith("http://"):
        public_base = "ws://" + public_base[len("http://"):]
    return public_base + "/api/v1/devices/ws"


def _download_base_url() -> str:
    public_base = os.environ.get("AADS_PUBLIC_BASE_URL", "https://aads.newtalk.kr").rstrip("/")
    return public_base + "/api/v1/devices/android"


def _find_android_apk() -> Path | None:
    for candidate in ANDROID_APK_CANDIDATES:
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


async def _get_pool_or_none():
    try:
        from app.core.db_pool import get_pool

        return get_pool()
    except Exception as e:
        logger.warning("device_db_pool_unavailable: %s", e)
        return None


async def _ensure_pairing_table() -> bool:
    global _PAIRING_TABLE_READY
    if _PAIRING_TABLE_READY:
        return True
    pool = await _get_pool_or_none()
    if pool is None:
        return False
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS device_pairing_tokens (
                    id SERIAL PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    device_type TEXT NOT NULL DEFAULT 'android',
                    token_hash TEXT UNIQUE NOT NULL,
                    label TEXT DEFAULT '',
                    created_by TEXT DEFAULT '',
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    expires_at TIMESTAMPTZ,
                    last_used_at TIMESTAMPTZ,
                    revoked_at TIMESTAMPTZ
                )
                """
            )
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_device_pairing_tokens_agent
                ON device_pairing_tokens(agent_id, device_type)
                """
            )
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_device_pairing_tokens_active
                ON device_pairing_tokens(token_hash)
                WHERE revoked_at IS NULL
                """
            )
        _PAIRING_TABLE_READY = True
        return True
    except Exception as e:
        logger.exception("device_pairing_table_init_failed: %s", e)
        return False


async def _verify_token(token: str) -> bool:
    if not token:
        return False
    expected = os.environ.get("PC_AGENT_TOKEN", "")
    if expected and token == expected:
        return True
    if await _ensure_pairing_table():
        pool = await _get_pool_or_none()
        if pool is not None:
            try:
                async with pool.acquire() as conn:
                    row = await conn.fetchrow(
                        """
                        UPDATE device_pairing_tokens
                        SET last_used_at = NOW()
                        WHERE token_hash = $1
                          AND revoked_at IS NULL
                          AND (expires_at IS NULL OR expires_at > NOW())
                        RETURNING id
                        """,
                        _token_hash(token),
                    )
                    if row is not None:
                        return True
            except Exception as e:
                logger.warning("device_pairing_token_verify_failed: %s", e)
    try:
        from app.core.db_pool import get_pool

        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM api_tokens WHERE token=$1 AND is_active=true", token
            )
            return row is not None
    except Exception:
        return bool(expected and token == expected)


@router.websocket("/devices/ws/{agent_id}")
async def ws_device(
    websocket: WebSocket,
    agent_id: str,
    token: str = Query(""),
    device_type: str = Query("pc"),
):
    if not await _verify_token(token):
        await websocket.close(code=4001, reason="인증 실패")
        return

    await websocket.accept()
    device_info = None

    try:
        raw = await websocket.receive_json()
        if raw.get("type") != "register":
            await websocket.close(code=4002, reason="첫 메시지는 register여야 합니다")
            return

        payload = raw.get("payload", {})
        actual_type = payload.get("device_type", device_type)
        device_info = device_manager.register_device(
            agent_id, websocket, actual_type, payload
        )

        await websocket.send_json({"type": "registered", "payload": {"agent_id": agent_id}})

        while True:
            import asyncio
            try:
                data = await asyncio.wait_for(
                    websocket.receive_json(), timeout=HEARTBEAT_TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.warning("디바이스 %s 하트비트 타임아웃", agent_id)
                break

            msg_type = data.get("type", "")

            if msg_type == "heartbeat":
                device_manager.update_heartbeat(agent_id)
                await websocket.send_json({"type": "heartbeat", "id": data.get("id", "")})

            elif msg_type == "result":
                command_id = data.get("id", "")
                device_manager.receive_result(command_id, data.get("payload", {}))

            elif msg_type == "stream_frame":
                await device_manager.broadcast_frame(
                    agent_id, data.get("payload", {}).get("frame", "")
                )

            elif msg_type == "network_info":
                pass

    except WebSocketDisconnect:
        logger.info("디바이스 %s 연결 종료", agent_id)
    except Exception:
        logger.exception("디바이스 %s WebSocket 오류", agent_id)
    finally:
        device_manager.unregister_device(agent_id)


class CommandRequest(BaseModel):
    agent_id: str = ""
    command_type: str
    params: dict[str, Any] = {}
    timeout: float = 30.0


class AndroidPairingRequest(BaseModel):
    agent_id: str = Field(default="", max_length=80)
    label: str = Field(default="", max_length=120)
    device_type: str = Field(default="android", pattern="^(android|pc|ios)$")
    expires_hours: int = Field(default=24, ge=1, le=720)


async def _require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    if not current_user.get("is_admin"):
        raise HTTPException(status_code=403, detail="admin 권한이 필요합니다")
    return current_user


@router.get("/devices")
async def list_devices(device_type: str = Query(None)):
    devices = device_manager.get_devices(device_type)
    return {"devices": devices, "count": len(devices)}


@router.post("/devices/execute")
async def execute_command(req: CommandRequest):
    result = await device_manager.send_command(
        req.agent_id, req.command_type, req.params, req.timeout
    )
    return result.model_dump()


@router.get("/devices/{agent_id}/status")
async def device_status(agent_id: str):
    info = device_manager.get_device(agent_id)
    if info is None:
        return {"status": "disconnected", "agent_id": agent_id}
    return {"status": "connected", **info.model_dump()}


@router.get("/devices/{agent_id}/capabilities")
async def device_capabilities(agent_id: str):
    caps = device_manager.get_device_capabilities(agent_id)
    return {"agent_id": agent_id, "capabilities": caps}


@router.get("/devices/android/manifest")
async def android_agent_manifest():
    apk_path = _find_android_apk()
    apk_available = apk_path is not None
    source_count = 0
    if ANDROID_AGENT_DIR.exists():
        source_count = sum(1 for path in ANDROID_AGENT_DIR.rglob("*") if path.is_file())
    return {
        "name": "AADS Android Agent",
        "package": "kr.newtalk.aads.agent",
        "version": "0.1.0",
        "device_type": "android",
        "server_ws_base_url": _public_ws_base_url(),
        "install_page_url": _download_base_url() + "/install",
        "apk_download_url": _download_base_url() + "/download",
        "source_zip_url": _download_base_url() + "/source.zip",
        "pairing_api": "/api/v1/devices/android/pairing",
        "apk_available": apk_available,
        "apk_size": apk_path.stat().st_size if apk_path else 0,
        "source_file_count": source_count,
        "build_command": "cd android_agent && ./build_debug_apk.sh",
    }


@router.post("/devices/android/pairing")
async def create_android_pairing(
    req: AndroidPairingRequest,
    current_user: dict = Depends(_require_admin),
):
    if not await _ensure_pairing_table():
        raise HTTPException(status_code=503, detail="DB pool 또는 페어링 테이블을 사용할 수 없습니다")

    agent_id = req.agent_id.strip() or f"android-{secrets.token_hex(3)}"
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=req.expires_hours)
    token_hash = _token_hash(token)
    pool = await _get_pool_or_none()
    if pool is None:
        raise HTTPException(status_code=503, detail="DB pool을 사용할 수 없습니다")

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO device_pairing_tokens (
                agent_id, device_type, token_hash, label, created_by, expires_at
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            agent_id,
            req.device_type,
            token_hash,
            req.label,
            current_user.get("email", ""),
            expires_at,
        )

    server_url = _public_ws_base_url()
    full_ws_url = f"{server_url}/{agent_id}?token={token}&device_type={req.device_type}"
    payload = {
        "server_url": server_url,
        "agent_id": agent_id,
        "token": token,
        "device_type": req.device_type,
    }
    return {
        "agent_id": agent_id,
        "device_type": req.device_type,
        "expires_at": expires_at.isoformat(),
        "pairing_payload": payload,
        "full_ws_url": full_ws_url,
        "install_page_url": _download_base_url() + "/install",
        "apk_download_url": _download_base_url() + "/download",
        "note": "token은 이 응답에서만 평문으로 반환됩니다. 서버에는 SHA-256 해시만 저장됩니다.",
    }


@router.post("/devices/android/pairing/{agent_id}/revoke")
async def revoke_android_pairing(
    agent_id: str,
    current_user: dict = Depends(_require_admin),
):
    if not await _ensure_pairing_table():
        raise HTTPException(status_code=503, detail="DB pool 또는 페어링 테이블을 사용할 수 없습니다")
    pool = await _get_pool_or_none()
    if pool is None:
        raise HTTPException(status_code=503, detail="DB pool을 사용할 수 없습니다")
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE device_pairing_tokens
            SET revoked_at = NOW()
            WHERE agent_id = $1
              AND revoked_at IS NULL
            """,
            agent_id,
        )
    return {"agent_id": agent_id, "result": result, "revoked_by": current_user.get("email", "")}


@router.get("/devices/android/download")
async def download_android_apk():
    apk_path = _find_android_apk()
    if apk_path is None:
        raise HTTPException(
            status_code=404,
            detail="APK가 아직 빌드되지 않았습니다. 서버에서 `cd android_agent && ./build_debug_apk.sh` 실행 후 다시 다운로드하세요.",
        )
    return FileResponse(
        apk_path,
        media_type="application/vnd.android.package-archive",
        filename=ANDROID_APK_NAME,
    )


@router.get("/devices/android/source.zip")
async def download_android_source_zip():
    if not ANDROID_AGENT_DIR.exists():
        raise HTTPException(status_code=404, detail="android_agent 프로젝트가 없습니다")

    excluded_dirs = {".gradle", "build", ".git"}
    excluded_suffixes = {".apk", ".aab", ".keystore", ".jks"}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in ANDROID_AGENT_DIR.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(ANDROID_AGENT_DIR)
            if any(part in excluded_dirs for part in rel.parts):
                continue
            if path.suffix in excluded_suffixes:
                continue
            zf.write(path, Path("android_agent") / rel)
    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="aads-android-agent-source.zip"'},
    )


@router.get("/devices/android/install", response_class=HTMLResponse)
async def android_install_page():
    apk_path = _find_android_apk()
    apk_status = (
        f"APK ready ({apk_path.stat().st_size:,} bytes)"
        if apk_path
        else "APK not built yet. Build command: cd android_agent && ./build_debug_apk.sh"
    )
    html = f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AADS Android Agent</title>
  <style>
    body {{ font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; line-height: 1.5; color: #111827; }}
    main {{ max-width: 760px; margin: 0 auto; }}
    a.button {{ display: inline-block; padding: 12px 16px; margin: 8px 8px 8px 0; background: #111827; color: white; text-decoration: none; border-radius: 8px; }}
    code, pre {{ background: #f3f4f6; border-radius: 6px; padding: 2px 6px; }}
    pre {{ padding: 12px; overflow-x: auto; }}
  </style>
</head>
<body>
<main>
  <h1>AADS Android Agent</h1>
  <p>{apk_status}</p>
  <p>
    <a class="button" href="/api/v1/devices/android/download">APK 다운로드</a>
    <a class="button" href="/api/v1/devices/android/source.zip">소스 ZIP 다운로드</a>
  </p>
  <h2>페어링</h2>
  <p>관리자 로그인 후 <code>POST /api/v1/devices/android/pairing</code>으로 페어링 토큰을 만들고,
  앱의 QR/manual 입력칸에 반환된 <code>pairing_payload</code> JSON 또는 <code>full_ws_url</code>을 붙여넣으십시오.</p>
  <pre>POST /api/v1/devices/android/pairing
Authorization: Bearer &lt;admin-jwt&gt;
{{"label":"CEO phone","expires_hours":24}}</pre>
  <p>WebSocket base: <code>{_public_ws_base_url()}</code></p>
</main>
</body>
</html>"""
    return HTMLResponse(html)
