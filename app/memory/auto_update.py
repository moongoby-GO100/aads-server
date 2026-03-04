"""
프로젝트 완료, 커밋, 배포 등 이벤트 발생 시 system_memory 자동 업데이트
"""
from app.memory.store import memory_store
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

async def on_project_completed(project_id: str, outcome: str, cost: float):
    """프로젝트 완료 시 시스템 메모리 업데이트"""
    # 최근 완료 프로젝트 기록
    await memory_store.put_system("status", "last_completed_project", {
        "project_id": project_id,
        "outcome": outcome,
        "cost_usd": cost,
        "completed_at": datetime.now().isoformat()
    })
    # 누적 통계 업데이트
    stats = await memory_store.get_system("status", "cumulative_stats")
    if stats and stats.get("value"):
        import json
        current = json.loads(stats["value"]) if isinstance(stats["value"], str) else stats["value"]
        current["total_projects"] = current.get("total_projects", 0) + 1
        current["total_cost_usd"] = round(current.get("total_cost_usd", 0) + cost, 4)
        if outcome == "success":
            current["success_count"] = current.get("success_count", 0) + 1
    else:
        current = {"total_projects": 1, "total_cost_usd": round(cost, 4), "success_count": 1 if outcome == "success" else 0}
    await memory_store.put_system("status", "cumulative_stats", current)
    logger.info(f"System memory updated: project {project_id} completed")

async def on_commit_pushed(repo: str, commit_sha: str, message: str):
    """커밋 푸시 시 시스템 메모리 업데이트"""
    await memory_store.put_system("repos", repo, {
        "last_commit": commit_sha,
        "last_message": message[:200],
        "pushed_at": datetime.now().isoformat()
    })

async def on_health_check(api_ok: bool, postgres_ok: bool, tests: int, coverage: str):
    """헬스체크 결과 시스템 메모리 업데이트"""
    await memory_store.put_system("status", "health", {
        "api": "ok" if api_ok else "error",
        "postgres": "ok" if postgres_ok else "error",
        "tests": tests,
        "coverage": coverage,
        "checked_at": datetime.now().isoformat()
    })
