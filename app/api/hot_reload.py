"""
AADS Hot Module Reload API
서버 재시작 없이 Python 모듈을 즉시 재로드합니다.
채팅창에서 코드 수정 후 3분 재배포 없이 즉각 반영 가능.
"""
from __future__ import annotations

import importlib
import logging
import sys
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()

# 재로드 가능한 모듈 접두사 목록 (안전 범위만 허용)
_RELOADABLE_PREFIXES = (
    "app.services.",
    "app.api.",
    "app.core.",
    "app.agents.",
    "app.graphs.",
    "app.mcp.",
    "app.memory.",
    "app.routers.",
)

# 재로드 금지 모듈 (인증, DB 풀, 설정 등 — 재로드 시 상태 손실 위험)
_BLOCKED_MODULES = {
    "app.auth",
    "app.config",
    "app.core.db_pool",
    "app.core.anthropic_client",
    "app.services.checkpointer",
    "app.main",
}


class HotReloadRequest(BaseModel):
    """Hot Reload 요청 모델."""
    modules: Optional[list[str]] = None
    """재로드할 모듈명 목록. None이면 services 전체 재로드."""


class HotReloadResponse(BaseModel):
    """Hot Reload 응답 모델."""
    reloaded: dict[str, str]
    """모듈명 → 'ok' 또는 'error: <메시지>'"""
    total: int
    skipped: int
    success: int
    failed: int


def _get_services_modules() -> list[str]:
    """현재 sys.modules에서 app.services.* 모듈 목록을 반환합니다."""
    return [
        name for name in sys.modules
        if name.startswith("app.services.") and not name.endswith(".bak_aads")
    ]


def _is_reloadable(module_name: str) -> bool:
    """해당 모듈이 재로드 허용 범위인지 확인합니다."""
    if module_name in _BLOCKED_MODULES:
        return False
    return any(module_name.startswith(prefix) for prefix in _RELOADABLE_PREFIXES)


@router.post("/ops/hot-reload", response_model=HotReloadResponse)
async def hot_reload(req: HotReloadRequest = None):
    """
    Hot Module Reload — 서버 재시작 없이 Python 모듈을 즉시 재로드합니다.

    - modules=None (기본): app.services.* 전체 재로드
    - modules=[...]: 지정 모듈만 재로드

    주의사항:
    - DB 풀, 인증, 설정 모듈은 보안/안정성을 위해 재로드 불가
    - 재로드된 모듈의 전역 상태(캐시 등)는 초기화됨
    """
    # 요청 바디가 없을 때 기본값 처리
    if req is None:
        req = HotReloadRequest()

    # 대상 모듈 결정
    if req.modules:
        # 지정 모듈 목록 검증
        target_modules = req.modules
    else:
        # services 전체 (현재 로드된 것만)
        target_modules = _get_services_modules()

    if not target_modules:
        return HotReloadResponse(
            reloaded={},
            total=0,
            skipped=0,
            success=0,
            failed=0,
        )

    results: dict[str, str] = {}
    skipped = 0

    for module_name in sorted(target_modules):
        # 재로드 허용 범위 확인
        if not _is_reloadable(module_name):
            results[module_name] = "skipped: 재로드 금지 모듈"
            skipped += 1
            logger.warning(f"hot_reload_blocked: {module_name}")
            continue

        # sys.modules에 없으면 스킵 (아직 로드된 적 없음)
        module = sys.modules.get(module_name)
        if module is None:
            results[module_name] = "skipped: 미로드 모듈"
            skipped += 1
            continue

        try:
            importlib.reload(module)
            results[module_name] = "ok"
            logger.info(f"hot_reload_ok: {module_name}")
        except Exception as e:
            results[module_name] = f"error: {e}"
            logger.error(f"hot_reload_error: {module_name} — {e}")

    # 집계
    ok_count = sum(1 for v in results.values() if v == "ok")
    fail_count = sum(1 for v in results.values() if v.startswith("error:"))

    logger.info(
        f"hot_reload_done: total={len(target_modules)} "
        f"success={ok_count} failed={fail_count} skipped={skipped}"
    )

    return HotReloadResponse(
        reloaded=results,
        total=len(target_modules),
        skipped=skipped,
        success=ok_count,
        failed=fail_count,
    )


@router.get("/ops/hot-reload/modules")
async def list_reloadable_modules():
    """
    현재 재로드 가능한 모듈 목록을 반환합니다.
    실제 hot-reload 전 대상 확인용.
    """
    loaded_services = _get_services_modules()
    all_reloadable = [
        name for name in sys.modules
        if _is_reloadable(name)
    ]

    return {
        "services_loaded": sorted(loaded_services),
        "all_reloadable": sorted(all_reloadable),
        "blocked": sorted(_BLOCKED_MODULES),
        "total_services": len(loaded_services),
    }
