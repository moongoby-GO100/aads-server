"""AADS-195: 시스템 정보 수집."""
from __future__ import annotations

import logging
import platform
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)


async def execute(params: Dict[str, Any]) -> Dict[str, Any]:
    """시스템 정보 수집 반환."""
    try:
        info = {
            "hostname": platform.node(),
            "os": platform.system(),
            "os_version": platform.version(),
            "os_release": platform.release(),
            "architecture": platform.machine(),
            "processor": platform.processor(),
            "python_version": platform.python_version(),
            "cpu_count": os.cpu_count(),
        }

        # 디스크 정보 (psutil 있으면)
        try:
            import psutil
            mem = psutil.virtual_memory()
            info["memory_total_gb"] = round(mem.total / (1024 ** 3), 1)
            info["memory_used_gb"] = round(mem.used / (1024 ** 3), 1)
            info["memory_percent"] = mem.percent
            info["cpu_percent"] = psutil.cpu_percent(interval=1)

            disks = []
            for part in psutil.disk_partitions():
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                    disks.append({
                        "device": part.device,
                        "mountpoint": part.mountpoint,
                        "total_gb": round(usage.total / (1024 ** 3), 1),
                        "used_gb": round(usage.used / (1024 ** 3), 1),
                        "percent": usage.percent,
                    })
                except (PermissionError, OSError):
                    pass
            info["disks"] = disks
        except ImportError:
            info["note"] = "psutil 미설치 — 상세 리소스 정보 없음"

        return {"status": "success", "data": info}
    except Exception as e:
        logger.error("system_info_error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}
