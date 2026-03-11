import shutil
import subprocess

from fastapi import APIRouter
from app.services.sandbox import check_sandbox_health

router = APIRouter()


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


@router.get("/health/deep")
async def deep_health_check():
    """도구 의존성까지 검증하는 deep health check.
    배포 직후 호출하여 SSH/DB/메모리 등 전체 도구 동작 확인."""
    checks = {}

    # 1. SSH 바이너리 존재
    checks["ssh_binary"] = shutil.which("ssh") is not None

    # 2. SSH 키 접근
    import os
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
