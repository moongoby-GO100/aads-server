"""Quality metrics API — 품질 통계, 회귀 감지, 주간 리포트."""

from fastapi import APIRouter, Query

router = APIRouter(prefix="/quality", tags=["quality"])


@router.get("/stats")
async def get_quality_stats(days: int = Query(7, ge=1, le=90)):
    """품질 통계 — 점수 분포, 평균, 추이."""
    from app.core.db_pool import get_pool
    from app.services.eval_pipeline import aggregate_quality_stats

    pool = get_pool()
    return await aggregate_quality_stats(pool, days=days)


@router.get("/regression")
async def get_quality_regression(window_days: int = Query(3, ge=1, le=30)):
    """품질 회귀 감지."""
    from app.core.db_pool import get_pool
    from app.services.eval_pipeline import detect_quality_regression

    pool = get_pool()
    return await detect_quality_regression(pool, window_days=window_days)


@router.get("/report")
async def get_weekly_report():
    """주간 품질 리포트."""
    from app.core.db_pool import get_pool
    from app.services.eval_pipeline import generate_weekly_report

    pool = get_pool()
    return {"report": await generate_weekly_report(pool)}
