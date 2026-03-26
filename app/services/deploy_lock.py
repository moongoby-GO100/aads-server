"""
deploy_lock.py — 3단계 동시 작업 잠금 시스템
1단계: 프로젝트별 작업 잠금 (Work Lock)
2단계: 파일별 잠금 (File Lock)
3단계: 배포 잠금 (Deploy Lock)

전 서버/전 서비스 적용. Redis 장애 시 graceful degradation (잠금 없이 진행).
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Optional

import redis as redis_lib

from app.config import settings

logger = logging.getLogger(__name__)

# ─── Redis 연결 ───────────────────────────────────────────────
_redis_client: Optional[redis_lib.Redis] = None


def _get_redis() -> Optional[redis_lib.Redis]:
    """Redis 클라이언트 반환. 연결 실패 시 None (graceful degradation)."""
    global _redis_client
    if _redis_client is not None:
        try:
            _redis_client.ping()
            return _redis_client
        except Exception:
            _redis_client = None

    try:
        url = getattr(settings, "UPSTASH_REDIS_URL", None) or "redis://aads-redis:6379/0"
        # Docker 내부에서는 aads-redis:6379, 외부에서는 localhost:6379
        if "localhost:6380" in url:
            url = "redis://aads-redis:6379/0"
        _redis_client = redis_lib.from_url(
            url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2
        )
        _redis_client.ping()
        logger.info("[deploy_lock] Redis 연결 성공: %s", url)
        return _redis_client
    except Exception as e:
        logger.warning("[deploy_lock] Redis 연결 실패 — 잠금 없이 진행: %s", e)
        _redis_client = None
        return None


# ─── 1단계: 프로젝트 작업 잠금 (Work Lock) ─────────────────────

# 프로젝트별 설정
PROJECT_CONFIG = {
    "AADS": {"max_concurrent": 2},
    "KIS": {"max_concurrent": 2},
    "GO100": {"max_concurrent": 2},
    "SF": {"max_concurrent": 2},
    "NTV2": {"max_concurrent": 2},
    "NAS": {"max_concurrent": 1},
}


def acquire_work_lock(
    project: str, session_id: str, timeout: int = 7200
) -> dict:
    """
    프로젝트 작업 잠금 획득.
    반환: {"acquired": bool, "holder": str|None, "queue_position": int}
    """
    r = _get_redis()
    if r is None:
        return {"acquired": True, "holder": None, "queue_position": 0}

    key = f"work_lock:{project}"
    max_concurrent = PROJECT_CONFIG.get(project, {}).get("max_concurrent", 2)

    try:
        # 현재 활성 세션 수 확인
        active = r.hgetall(key)
        # 만료된 세션 정리
        now = time.time()
        for sid, ts in list(active.items()):
            if now - float(ts) > timeout:
                r.hdel(key, sid)
                logger.info("[work_lock] 만료 세션 정리: %s/%s", project, sid)

        active = r.hgetall(key)
        if len(active) >= max_concurrent and session_id not in active:
            holders = list(active.keys())
            return {"acquired": False, "holder": holders[0], "queue_position": len(active)}

        # 잠금 획득
        r.hset(key, session_id, str(time.time()))
        r.expire(key, timeout)
        logger.info("[work_lock] 획득: %s/%s (활성: %d/%d)", project, session_id, len(active) + 1, max_concurrent)
        return {"acquired": True, "holder": None, "queue_position": 0}
    except Exception as e:
        logger.warning("[work_lock] Redis 오류 — 잠금 없이 진행: %s", e)
        return {"acquired": True, "holder": None, "queue_position": 0}


def release_work_lock(project: str, session_id: str) -> bool:
    """프로젝트 작업 잠금 해제."""
    r = _get_redis()
    if r is None:
        return True
    try:
        r.hdel(f"work_lock:{project}", session_id)
        logger.info("[work_lock] 해제: %s/%s", project, session_id)
        return True
    except Exception as e:
        logger.warning("[work_lock] 해제 실패: %s", e)
        return False


# ─── 2단계: 파일별 잠금 (File Lock) ───────────────────────────


def acquire_file_lock(
    project: str, file_path: str, session_id: str, timeout: int = 3600
) -> dict:
    """
    파일 잠금 획득.
    반환: {"acquired": bool, "holder": str|None}
    """
    r = _get_redis()
    if r is None:
        return {"acquired": True, "holder": None}

    key = f"file_lock:{project}:{file_path}"
    try:
        existing = r.get(key)
        if existing and existing != session_id:
            # 만료 확인
            ttl = r.ttl(key)
            if ttl > 0:
                return {"acquired": False, "holder": existing}

        r.set(key, session_id, ex=timeout)
        logger.info("[file_lock] 획득: %s:%s → %s", project, file_path, session_id)
        return {"acquired": True, "holder": None}
    except Exception as e:
        logger.warning("[file_lock] Redis 오류 — 잠금 없이 진행: %s", e)
        return {"acquired": True, "holder": None}


def release_file_lock(project: str, file_path: str, session_id: str) -> bool:
    """파일 잠금 해제. 본인 잠금만 해제 가능."""
    r = _get_redis()
    if r is None:
        return True
    try:
        key = f"file_lock:{project}:{file_path}"
        existing = r.get(key)
        if existing == session_id:
            r.delete(key)
            logger.info("[file_lock] 해제: %s:%s", project, file_path)
        return True
    except Exception as e:
        logger.warning("[file_lock] 해제 실패: %s", e)
        return False


def release_all_file_locks(project: str, session_id: str) -> int:
    """세션의 모든 파일 잠금 일괄 해제."""
    r = _get_redis()
    if r is None:
        return 0
    try:
        pattern = f"file_lock:{project}:*"
        count = 0
        for key in r.scan_iter(match=pattern, count=100):
            if r.get(key) == session_id:
                r.delete(key)
                count += 1
        if count:
            logger.info("[file_lock] 일괄 해제: %s/%s — %d건", project, session_id, count)
        return count
    except Exception as e:
        logger.warning("[file_lock] 일괄 해제 실패: %s", e)
        return 0


# ─── 3단계: 배포 잠금 (Deploy Lock) ──────────────────────────


def acquire_deploy_lock(
    project: str, session_id: str, timeout: int = 600
) -> dict:
    """
    배포 잠금 획득. 프로젝트당 1건만 배포 가능.
    반환: {"acquired": bool, "holder": str|None, "wait_seconds": int}
    """
    r = _get_redis()
    if r is None:
        return {"acquired": True, "holder": None, "wait_seconds": 0}

    key = f"deploy_lock:{project}"
    try:
        acquired = r.set(key, session_id, nx=True, ex=timeout)
        if acquired:
            logger.info("[deploy_lock] 획득: %s/%s", project, session_id)
            return {"acquired": True, "holder": None, "wait_seconds": 0}

        holder = r.get(key)
        ttl = r.ttl(key)
        logger.info("[deploy_lock] 대기: %s — holder=%s, ttl=%ds", project, holder, ttl)
        return {"acquired": False, "holder": holder, "wait_seconds": max(ttl, 0)}
    except Exception as e:
        logger.warning("[deploy_lock] Redis 오류 — 잠금 없이 진행: %s", e)
        return {"acquired": True, "holder": None, "wait_seconds": 0}


def release_deploy_lock(project: str, session_id: str) -> bool:
    """배포 잠금 해제. 본인 잠금만 해제 가능."""
    r = _get_redis()
    if r is None:
        return True
    try:
        key = f"deploy_lock:{project}"
        existing = r.get(key)
        if existing == session_id:
            r.delete(key)
            logger.info("[deploy_lock] 해제: %s/%s", project, session_id)
        return True
    except Exception as e:
        logger.warning("[deploy_lock] 해제 실패: %s", e)
        return False


@contextmanager
def deploy_lock_context(project: str, session_id: str, timeout: int = 600):
    """배포 잠금 컨텍스트 매니저. with 문으로 사용."""
    result = acquire_deploy_lock(project, session_id, timeout)
    if not result["acquired"]:
        raise RuntimeError(
            f"[deploy_lock] {project} 배포 중 — "
            f"holder={result['holder']}, 대기={result['wait_seconds']}s"
        )
    try:
        yield result
    finally:
        release_deploy_lock(project, session_id)


# ─── 상태 조회 API ────────────────────────────────────────────


def get_all_lock_status() -> dict:
    """전체 잠금 상태 조회. CEO 대시보드/텔레그램 보고용."""
    r = _get_redis()
    if r is None:
        return {"status": "redis_unavailable", "projects": {}}

    result = {}
    projects = ["AADS", "KIS", "GO100", "SF", "NTV2", "NAS"]
    try:
        for proj in projects:
            proj_status = {
                "work_lock": {},
                "file_locks": [],
                "deploy_lock": None,
            }
            # 작업 잠금
            work = r.hgetall(f"work_lock:{proj}")
            proj_status["work_lock"] = {
                "active_sessions": list(work.keys()),
                "count": len(work),
                "max": PROJECT_CONFIG.get(proj, {}).get("max_concurrent", 2),
            }
            # 파일 잠금
            for key in r.scan_iter(match=f"file_lock:{proj}:*", count=100):
                file_path = key.replace(f"file_lock:{proj}:", "")
                holder = r.get(key)
                ttl = r.ttl(key)
                proj_status["file_locks"].append({
                    "file": file_path, "holder": holder, "ttl": ttl
                })
            # 배포 잠금
            deploy_holder = r.get(f"deploy_lock:{proj}")
            if deploy_holder:
                deploy_ttl = r.ttl(f"deploy_lock:{proj}")
                proj_status["deploy_lock"] = {"holder": deploy_holder, "ttl": deploy_ttl}

            result[proj] = proj_status
        return {"status": "ok", "projects": result}
    except Exception as e:
        logger.warning("[lock_status] 조회 실패: %s", e)
        return {"status": "error", "error": str(e), "projects": {}}


# ─── Shell 스크립트 연동 헬퍼 ──────────────────────────────────


def shell_acquire_work_lock(project: str, job_id: str) -> bool:
    """pipeline-runner.sh에서 curl로 호출하는 간편 인터페이스."""
    result = acquire_work_lock(project, job_id)
    return result["acquired"]


def shell_acquire_deploy_lock(project: str, job_id: str) -> bool:
    """deploy.sh에서 curl로 호출하는 간편 인터페이스."""
    result = acquire_deploy_lock(project, job_id)
    return result["acquired"]
