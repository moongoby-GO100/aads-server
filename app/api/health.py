import os
import secrets
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import httpx

from fastapi import APIRouter, HTTPException, Request
from app.services.sandbox import check_sandbox_health

router = APIRouter()
_RELAY_SECRET_PATHS = (
    Path(os.getenv("CLAUDE_RELAY_SHARED_SECRET_FILE", "/app/scripts/claude_relay_secret.txt")),
    Path("/root/aads/aads-server/scripts/claude_relay_secret.txt"),
)


def _load_relay_secret() -> str:
    secret = (os.getenv("CLAUDE_RELAY_SHARED_SECRET") or "").strip()
    if secret:
        return secret
    for path in _RELAY_SECRET_PATHS:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        if value:
            return value
    return ""


def _is_rate_limited(until) -> bool:
    if not until:
        return False
    if isinstance(until, datetime):
        target = until
    else:
        try:
            target = datetime.fromisoformat(str(until).replace("Z", "+00:00"))
        except Exception:
            return False
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    return target > datetime.now(timezone.utc)


def _require_relay_secret(request: Request) -> None:
    expected = _load_relay_secret()
    presented = (request.headers.get("x-claude-relay-secret") or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="relay secret not configured")
    if not presented or not secrets.compare_digest(presented, expected):
        raise HTTPException(status_code=403, detail="invalid relay secret")


def _mask_key(val: str) -> str:
    """키의 앞 15자 + '...' 마스킹."""
    if not val:
        return ""
    return val[:15] + "..." if len(val) > 15 else val


def _detect_key_type(val: str) -> str:
    if not val:
        return "none"
    if val.startswith("sk-ant-oat01"):
        return "oauth"
    if val.startswith("sk-ant-api03"):
        return "api_key"
    return "unknown"


@router.get("/health")
async def health_check():
    from app.main import app_state
    graph_ready = app_state.get("graph") is not None
    sandbox_health = await check_sandbox_health()
    return {
        "status": "ok" if graph_ready else "initializing",
        "graph_ready": graph_ready,
        "version": "0.1.0",
        "sandbox": sandbox_health,
    }


@router.get("/health/api-keys")
async def api_key_status():
    """현재 사용 중인 API 키 상태 조회 (마스킹)."""
    from app.config import settings as s
    from app.core.auth_provider import get_oauth_key_records_async

    # AADS 서버 자체 토큰
    auth1 = s.ANTHROPIC_AUTH_TOKEN.get_secret_value() if s.ANTHROPIC_AUTH_TOKEN else ""
    auth2 = s.ANTHROPIC_AUTH_TOKEN_2.get_secret_value() if s.ANTHROPIC_AUTH_TOKEN_2 else ""

    # LiteLLM 컨테이너의 토큰 (환경변수 직접 확인 불가 → .env.litellm 파일에서 읽기)
    litellm_key = ""
    litellm_env = "/root/aads/aads-server/.env.litellm"
    # Docker 내부에서는 호스트 파일 직접 접근 불가 → 볼륨 마운트된 경로 시도
    for path in [litellm_env, "/app/.env.litellm", ".env.litellm"]:
        try:
            with open(path) as f:
                for line in f:
                    if line.startswith("ANTHROPIC_API_KEY="):
                        litellm_key = line.split("=", 1)[1].strip()
                        break
            if litellm_key:
                break
        except FileNotFoundError:
            continue

    oauth_records = await get_oauth_key_records_async(include_rate_limited=True)
    primary = oauth_records[0] if oauth_records else {}
    primary_slot = primary.get("slot", "")
    primary_label = primary.get("label", "")
    primary_prefix = primary.get("prefix", "")
    relay_cli = {}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(3.0, connect=1.0)) as client:
            relay = await client.get("http://host.docker.internal:8199/health")
            if relay.status_code == 200:
                relay_cli = relay.json()
    except Exception:
        relay_cli = {}

    # LiteLLM 토큰이 어느 계정인지 매칭
    litellm_label = ""
    for record in oauth_records:
        if record.get("value") and litellm_key and record["value"] == litellm_key:
            litellm_label = record.get("label", "")
            break

    return {
        "anthropic": {
            "aads_token_1": {"prefix": _mask_key(auth1), "type": _detect_key_type(auth1), "active": bool(auth1)},
            "aads_token_2": {"prefix": _mask_key(auth2), "type": _detect_key_type(auth2), "active": bool(auth2)},
            "litellm": {"prefix": _mask_key(litellm_key), "type": _detect_key_type(litellm_key), "active": bool(litellm_key), "label": litellm_label},
            "cli": {
                "prefix": primary_prefix,
                "type": _detect_key_type(primary.get("value", "")),
                "account": relay_cli.get("oauth_slot", primary_slot),
                "active": bool(primary.get("value")),
                "label": relay_cli.get("oauth_label", primary_label),
                "status": relay_cli.get("status", "unknown"),
                "auth_mode": relay_cli.get("auth_mode", ""),
                "token_available": relay_cli.get("token_available", bool(primary.get("value"))),
            },
            "db_keys": [
                {
                    "label": record.get("label", ""),
                    "key_name": record.get("key_name", ""),
                    "prefix": record.get("prefix", ""),
                    "priority": record.get("priority", 0),
                    "slot": record.get("slot", ""),
                    "rate_limited_until": record.get("rate_limited_until").isoformat() if record.get("rate_limited_until") else None,
                    "is_rate_limited": _is_rate_limited(record.get("rate_limited_until")),
                    "last_used_at": record.get("last_used_at").isoformat() if record.get("last_used_at") else None,
                    "last_verified_at": record.get("last_verified_at").isoformat() if record.get("last_verified_at") else None,
                    "notes": record.get("notes", ""),
                    "is_current": str(record.get("slot", "")) == str(relay_cli.get("oauth_slot", primary_slot)),
                }
                for record in oauth_records
            ],
        },
        "google": {"active": bool(s.GOOGLE_API_KEY.get_secret_value() if s.GOOGLE_API_KEY else "")},
        "openai": {"active": bool(s.OPENAI_API_KEY.get_secret_value() if s.OPENAI_API_KEY else "")},
    }


@router.get("/health/claude-relay/oauth-state")
async def claude_relay_oauth_state(request: Request):
    """Claude relay 전용 OAuth 상태 조회. raw token 포함 — shared secret 필수."""
    _require_relay_secret(request)
    from app.core.auth_provider import get_oauth_key_records_async

    records = await get_oauth_key_records_async(include_rate_limited=True)
    return {
        "keys": [
            {
                "label": record.get("label", ""),
                "key_name": record.get("key_name", ""),
                "priority": record.get("priority", 0),
                "slot": record.get("slot", ""),
                "value": record.get("value", ""),
                "rate_limited_until": record.get("rate_limited_until").isoformat() if record.get("rate_limited_until") else None,
            }
            for record in records
        ]
    }


@router.get("/health/deep")
async def deep_health_check():
    """도구 의존성까지 검증하는 deep health check.
    배포 직후 호출하여 SSH/DB/메모리 등 전체 도구 동작 확인."""
    checks = {}

    # 1. SSH 바이너리 존재
    checks["ssh_binary"] = shutil.which("ssh") is not None

    # 2. SSH 키 접근
    checks["ssh_keys"] = os.path.exists("/root/.ssh/id_ed25519")

    # 3. SSH 서버 연결 (211, 114)
    for name, alias in [("server_211", "server-211"), ("server_114", "server-114")]:
        try:
            r = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=3", "-o", "BatchMode=yes", alias, "echo ok"],
                capture_output=True, text=True, timeout=5,
            )
            checks[name] = r.returncode == 0
        except Exception:
            checks[name] = False

    # 4. DB 연결
    try:
        import asyncpg
        url = os.getenv("DATABASE_URL", "")
        conn = await asyncpg.connect(url, timeout=5)
        await conn.fetchval("SELECT 1")
        await conn.close()
        checks["database"] = True
    except Exception:
        checks["database"] = False

    # 5. 메모리 시스템
    try:
        from app.core.memory_recall import build_memory_context
        ctx = await build_memory_context(project_id="AADS")
        checks["memory_system"] = len(ctx) > 0
    except Exception:
        checks["memory_system"] = False

    # 6. git 바이너리
    checks["git_binary"] = shutil.which("git") is not None

    all_ok = all(checks.values())
    return {
        "status": "ok" if all_ok else "degraded",
        "checks": checks,
        "failed": [k for k, v in checks.items() if not v],
    }


@router.get("/health/healer")
async def healer_status():
    """Unified Self-Healing Engine 상태 조회."""
    from app.services.unified_healer import get_healer_status
    return await get_healer_status()
