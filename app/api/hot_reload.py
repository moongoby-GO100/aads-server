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
    # AADS-FOOD-QUEUE-DRAIN-AGENT-ONLINE-MISMATCH-P0: importlib.reload()는 모듈 최상단의
    # `pc_agent_manager = PCAgentManager()`를 재실행해 새 빈 싱글톤을 만든다. 이미
    # `from ... import pc_agent_manager`로 바인딩을 끝낸 app.api.pc_agent(재로드 대상 아님,
    # 실제 WebSocket 연결을 들고 있음)는 옛 인스턴스를 계속 참조하지만, app.main 안의
    # 지연 import(`async def` 내부에서 매 호출마다 재-import)는 재로드 직후부터 항상 빈
    # 새 인스턴스를 집어 diagnostics(온라인)와 wait_for_agent_online(오프라인)이 서로
    # 영구히 어긋난다. 재로드를 막아 싱글톤을 프로세스 생애주기 동안 하나로 유지한다.
    "app.services.pc_agent_manager",
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
    active_tasks_pre: int = 0
    active_tasks_post: int = 0
    tasks_lost: int = 0


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
    - reload-safe dict 패턴으로 활성 태스크 상태는 보존됨
    """
    if req is None:
        req = HotReloadRequest()

    if req.modules:
        target_modules = req.modules
    else:
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

    # ── Pre-reload: 활성 스트리밍 중간 상태를 DB에 저장 ──
    _pre_active = 0
    _pre_streaming = 0
    try:
        _chat_svc = sys.modules.get("app.services.chat_service")
        if _chat_svc:
            _bg = getattr(_chat_svc, '_active_bg_tasks', {})
            _ss = getattr(_chat_svc, '_streaming_state', {})
            _pre_active = len(_bg)
            _pre_streaming = len(_ss)
            if _pre_active > 0:
                logger.info(
                    f"hot_reload_pre: {_pre_active} active tasks, "
                    f"{_pre_streaming} streaming — saving interim state"
                )
                _isave = getattr(_chat_svc, '_interim_save_streaming', None)
                if _isave:
                    for _sid, _st in list(_ss.items()):
                        try:
                            await _isave(_sid, _st)
                        except Exception as _e:
                            logger.warning(f"hot_reload_interim_save_err: {_sid[:8]} — {_e}")
    except Exception as _e:
        logger.warning(f"hot_reload_pre_err: {_e}")

    for module_name in sorted(target_modules):
        if not _is_reloadable(module_name):
            results[module_name] = "skipped: 재로드 금지 모듈"
            skipped += 1
            logger.warning(f"hot_reload_blocked: {module_name}")
            continue

        module = sys.modules.get(module_name)
        if module is None:
            try:
                module = importlib.import_module(module_name)
                results[module_name] = "ok (fresh import)"
                logger.info(f"hot_reload_fresh_import: {module_name}")
                continue
            except Exception as e:
                results[module_name] = f"error: import failed — {e}"
                skipped += 1
                continue

        try:
            importlib.reload(module)
            results[module_name] = "ok"
            logger.info(f"hot_reload_ok: {module_name}")
        except Exception as e:
            results[module_name] = f"error: {e}"
            logger.error(f"hot_reload_error: {module_name} — {e}")

    # ── Post-reload: 활성 태스크 생존 확인 ──
    _post_active = 0
    _tasks_lost = 0
    if _pre_active > 0:
        try:
            _cs = sys.modules.get("app.services.chat_service")
            if _cs:
                _post_active = len(getattr(_cs, '_active_bg_tasks', {}))
                _tasks_lost = max(0, _pre_active - _post_active)
                if _tasks_lost > 0:
                    logger.error(
                        f"hot_reload_TASK_LOSS: pre={_pre_active} "
                        f"post={_post_active} lost={_tasks_lost}"
                    )
                else:
                    logger.info(f"hot_reload_post: all {_post_active} tasks survived")
        except Exception as _e:
            logger.warning(f"hot_reload_post_err: {_e}")

    # 집계
    ok_count = sum(1 for v in results.values() if v == "ok")
    fail_count = sum(1 for v in results.values() if v.startswith("error:"))

    logger.info(
        f"hot_reload_done: total={len(target_modules)} "
        f"success={ok_count} failed={fail_count} skipped={skipped} "
        f"tasks_pre={_pre_active} tasks_post={_post_active} tasks_lost={_tasks_lost}"
    )

    return HotReloadResponse(
        reloaded=results,
        total=len(target_modules),
        skipped=skipped,
        success=ok_count,
        failed=fail_count,
        active_tasks_pre=_pre_active,
        active_tasks_post=_post_active,
        tasks_lost=_tasks_lost,
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
