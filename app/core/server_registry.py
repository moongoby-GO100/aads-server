"""서버 정보 동적 레지스트리 — DB 기반 서버/프로젝트 매핑.

prompt_assets 하드코딩 대신 DB에서 서버 IP/경로/포트를 동적으로 조회.
캐시 TTL 600초.
"""
import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class ServerInfo:
    server_key: str
    ip: str
    port: int
    workdir: str
    project: str
    ssh_user: str = "root"
    description: str = ""


_cache: Dict[str, ServerInfo] = {}
_cache_ts: float = 0.0
_CACHE_TTL = 600


async def get_server(project: str) -> Optional[ServerInfo]:
    servers = await list_servers()
    return servers.get(project)


async def list_servers() -> Dict[str, ServerInfo]:
    global _cache, _cache_ts
    if _cache and (time.time() - _cache_ts) < _CACHE_TTL:
        return _cache
    try:
        from app.core.db_pool import get_pool

        pool = get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM server_registry")
            result: Dict[str, ServerInfo] = {}
            for r in rows:
                info = ServerInfo(
                    server_key=r["server_key"],
                    ip=r["ip"],
                    port=r["port"],
                    workdir=r["workdir"],
                    project=r["project"],
                    ssh_user=r.get("ssh_user", "root"),
                    description=r.get("description", ""),
                )
                result[r["project"]] = info
            _cache = result
            _cache_ts = time.time()
    except Exception as e:
        logger.warning(f"server_registry_load_failed: {e}")
    return _cache
