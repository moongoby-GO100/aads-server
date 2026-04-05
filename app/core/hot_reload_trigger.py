"""
AADS Hot Reload 자동 트리거 유틸리티
write_remote_file / patch_remote_file 완료 후 자동으로 hot reload를 트리거합니다.
X-Monitor-Key 헤더로 내부 인증 우회.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# AADS 서버 내부 포트
_AADS_INTERNAL_URL = "http://localhost:8080"  # 컨테이너 내부 gunicorn 포트 (nginx 경유 불필요)


def _file_path_to_module(file_path: str) -> str | None:
    """파일 경로를 Python 모듈명으로 변환합니다.
    
    예: app/services/context_builder.py → app.services.context_builder
    """
    if not file_path.endswith(".py"):
        return None

    path = file_path.replace("\\", "/").strip("/")

    # /app/app/... 또는 /app/... 형태 정규화
    for prefix in ("/app/app/", "/app/", "app/app/"):
        if path.startswith(prefix.lstrip("/")):
            path = path[len(prefix.lstrip("/")):]
            break

    # .py 제거 후 모듈명 변환
    module = path[:-3].replace("/", ".")
    if not module.startswith("app."):
        module = "app." + module

    return module


async def trigger_hot_reload_for_file(project: str, file_path: str) -> None:
    """파일 수정 후 hot reload를 자동 트리거합니다.
    
    AADS 프로젝트의 .py 파일 수정 시에만 동작.
    실패해도 예외를 발생시키지 않음 (로그만 남김).
    """
    if project != "AADS":
        return
    if not file_path.endswith(".py"):
        return

    module_name = _file_path_to_module(file_path)
    if not module_name:
        logger.warning(f"hot_reload_trigger: 모듈명 변환 실패 — {file_path}")
        return

    # 재로드 금지 모듈 체크 (main.py, config 등)
    blocked = {"app.main", "app.config", "app.core.db_pool", "app.core.anthropic_client", "app.services.checkpointer"}
    if module_name in blocked:
        logger.info(f"hot_reload_trigger: 재로드 금지 모듈 스킵 — {module_name}")
        return

    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{_AADS_INTERNAL_URL}/api/v1/ops/hot-reload",
                json={"modules": [module_name]},
                headers={"x-monitor-key": "internal-hot-reload"},
            )
            if resp.status_code == 200:
                data = resp.json()
                ok = data.get("success", 0)
                failed = data.get("failed", 0)
                results = data.get("reloaded", {})
                status = results.get(module_name, "unknown")
                logger.info(f"hot_reload_trigger_ok: {module_name} → {status} (ok={ok}, failed={failed})")
            else:
                logger.warning(f"hot_reload_trigger_http_error: {module_name} → HTTP {resp.status_code}")
    except ImportError:
        logger.warning("hot_reload_trigger: httpx 미설치 — hot reload 스킵")
    except Exception as e:
        logger.warning(f"hot_reload_trigger_error: {module_name} — {e}")
