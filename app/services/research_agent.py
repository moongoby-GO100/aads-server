"""
AI 자율 연구개선 — Phase 2: 자율 연구 에이전트
매일 07:00 UTC(16:00 KST)에 자동 실행.
각 서비스의 에러 로그, 품질 데이터, 사용 패턴을 분석하여
개선점을 발견하고 CEO에게 제안.
비용: ~$0.05~0.10/일 (Haiku 여러 회)
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

_HAIKU_MODEL = "claude-haiku-4-5-20251001"


@dataclass
class ResearchFinding:
    """연구 발견사항."""
    project: str
    category: str  # performance, error, quality, cost, security
    severity: str  # high, medium, low
    title: str
    description: str
    suggestion: str
    data: dict = field(default_factory=dict)


async def run_daily_research(pool) -> dict:
    """매일 자동 실행되는 연구 에이전트.

    분석 항목:
    1. 에러 패턴 분석 — 반복 에러, 새 에러 유형 감지
    2. 품질 추세 분석 — 워크스페이스별 품질 변화
    3. 비용 효율 분석 — 모델별 비용 vs 품질
    4. Pipeline 실패율 분석
    5. 발견사항 저장 + CEO 알림

    Returns: {"findings": [...], "notifications_sent": int}
    """
    result = {"findings": [], "notifications_sent": 0}
    findings = []

    try:
        async with pool.acquire() as conn:
            # ── 1. 에러 패턴 분석 ──
            error_patterns = await conn.fetch("""
                SELECT subject, COUNT(*) as cnt,
                       MAX(created_at) as last_seen
                FROM memory_facts
                WHERE category = 'error_pattern'
                  AND created_at >= NOW() - interval '3 days'
                GROUP BY subject
                HAVING COUNT(*) >= 2
                ORDER BY cnt DESC
                LIMIT 10
            """)

            for ep in error_patterns:
                findings.append(ResearchFinding(
                    project="AADS",
                    category="error",
                    severity="high" if ep["cnt"] >= 5 else "medium",
                    title=f"반복 에러 패턴 ({ep['cnt']}회)",
                    description=str(ep["subject"])[:200],
                    suggestion="해당 영역 코드 리뷰 및 방어 로직 강화 필요",
                    data={"count": ep["cnt"], "last_seen": str(ep["last_seen"])},
                ))

            # ── 2. 품질 추세 분석 ──
            quality_trend = await conn.fetch("""
                SELECT
                    DATE(created_at) as day,
                    AVG(quality_score) as avg_score,
                    COUNT(*) as cnt
                FROM chat_messages
                WHERE role = 'assistant'
                  AND quality_score IS NOT NULL
                  AND created_at >= NOW() - interval '7 days'
                GROUP BY DATE(created_at)
                ORDER BY day
            """)

            if len(quality_trend) >= 3:
                scores = [float(r["avg_score"]) for r in quality_trend]
                # 하락 추세 감지
                if len(scores) >= 3 and scores[-1] < scores[-3] - 0.05:
                    findings.append(ResearchFinding(
                        project="AADS",
                        category="quality",
                        severity="high",
                        title="품질 하락 추세 감지",
                        description=f"3일간 품질 점수 하락: {scores[-3]:.2f} → {scores[-1]:.2f}",
                        suggestion="최근 응답 패턴 분석 및 프롬프트 조정 필요",
                        data={"scores": [round(s, 3) for s in scores]},
                    ))

            # ── 3. 비용 효율 분석 ──
            cost_analysis = await conn.fetch("""
                SELECT
                    model_used,
                    AVG(quality_score) as avg_quality,
                    AVG(cost::float) as avg_cost,
                    COUNT(*) as cnt
                FROM chat_messages
                WHERE role = 'assistant'
                  AND quality_score IS NOT NULL
                  AND cost IS NOT NULL
                  AND cost > 0
                  AND created_at >= NOW() - interval '7 days'
                GROUP BY model_used
                HAVING COUNT(*) >= 3
                ORDER BY avg_cost DESC
            """)

            for ca in cost_analysis:
                quality = float(ca["avg_quality"] or 0)
                cost = float(ca["avg_cost"] or 0)
                if cost > 0.05 and quality < 0.6:
                    findings.append(ResearchFinding(
                        project="AADS",
                        category="cost",
                        severity="medium",
                        title=f"비용 비효율: {ca['model_used']}",
                        description=f"평균 비용 ${cost:.4f}, 평균 품질 {quality:.2f} — 비용 대비 품질 낮음",
                        suggestion="더 저렴한 모델로 다운그레이드 또는 프롬프트 최적화",
                        data={"model": ca["model_used"], "avg_cost": round(cost, 4), "avg_quality": round(quality, 3)},
                    ))

            # ── 4. Pipeline 실패율 분석 ──
            pipeline_stats = await conn.fetch("""
                SELECT
                    status,
                    COUNT(*) as cnt
                FROM pipeline_jobs
                WHERE created_at >= NOW() - interval '7 days'
                GROUP BY status
            """)

            total_jobs = sum(r["cnt"] for r in pipeline_stats)
            error_jobs = sum(r["cnt"] for r in pipeline_stats if r["status"] == "error")
            if total_jobs > 0 and error_jobs / total_jobs > 0.3:
                findings.append(ResearchFinding(
                    project="AADS",
                    category="performance",
                    severity="high",
                    title=f"Pipeline 실패율 높음: {error_jobs}/{total_jobs}",
                    description=f"최근 7일 Pipeline 실패율 {error_jobs/total_jobs*100:.0f}%",
                    suggestion="실패 작업 로그 분석 및 Runner 안정성 개선 필요",
                    data={"total": total_jobs, "errors": error_jobs},
                ))

            # ── 5. 발견사항 저장 ──
            for f in findings:
                await conn.execute(
                    """INSERT INTO ai_observations
                       (project, category, content, confidence, tags)
                       VALUES ($1, $2, $3, 0.7, ARRAY['research_agent', $4])""",
                    f.project,
                    "research_finding",
                    json.dumps({
                        "title": f.title,
                        "description": f.description,
                        "suggestion": f.suggestion,
                        "severity": f.severity,
                        "data": f.data,
                    }, ensure_ascii=False),
                    f.category,
                )

        result["findings"] = [
            {"title": f.title, "severity": f.severity, "category": f.category,
             "suggestion": f.suggestion}
            for f in findings
        ]

        # ── 6. CEO 알림 (high severity만) ──
        high_findings = [f for f in findings if f.severity == "high"]
        if high_findings:
            try:
                from app.services.telegram_bot import get_telegram_bot
                bot = get_telegram_bot()
                if bot and bot.is_ready:
                    msg_parts = ["🔬 [AI 연구 에이전트] 일일 발견사항\n"]
                    for f in high_findings[:5]:
                        msg_parts.append(f"🔴 **{f.title}**\n{f.description}\n→ {f.suggestion}\n")
                    await bot.send_message("\n".join(msg_parts)[:3800])
                    result["notifications_sent"] = len(high_findings)
            except Exception as tg_err:
                logger.debug("research_agent_telegram_error", error=str(tg_err))

        logger.info(
            "research_agent_complete",
            findings=len(findings),
            high=len(high_findings),
        )

    except Exception as e:
        logger.error("research_agent_error", error=str(e))

    return result


async def get_recent_findings(pool, days: int = 3, limit: int = 10) -> list:
    """최근 연구 발견사항 조회."""
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT content, created_at
                   FROM ai_observations
                   WHERE category = 'research_finding'
                     AND created_at >= NOW() - (($1 || ' days')::interval)
                   ORDER BY created_at DESC
                   LIMIT $2""",
                str(days), limit,
            )
            results = []
            for r in rows:
                content = r["content"]
                if isinstance(content, str):
                    try:
                        content = json.loads(content)
                    except (json.JSONDecodeError, TypeError):
                        content = {"title": content[:100]}
                results.append({**content, "created_at": str(r["created_at"])})
            return results
    except Exception as e:
        logger.debug("get_findings_error", error=str(e))
        return []
