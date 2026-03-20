"""
AI 자율 연구개선 — Phase 2: 자율 연구 에이전트
매일 07:00 UTC(16:00 KST)에 자동 실행.
각 서비스의 에러 로그, 품질 데이터, 사용 패턴을 분석하여
개선점을 발견하고 CEO에게 제안.
비용: ~$0.05~0.10/일 (Haiku 여러 회)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import List, Optional

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


@dataclass
class RemediationMethod:
    """단일 수정 시도."""
    name: str
    target_file: str
    description: str


@dataclass
class RemediationResult:
    """자동 수정 결과."""
    success: bool
    method: str
    before_value: str = ""
    after_value: str = ""
    rollback_done: bool = False
    error: str = ""


# 자동 수정 가능한 유형 (CEO 승인 불필요)
SAFE_TO_AUTO_FIX = {
    "confidence_threshold",
    "log_level_hidden_error",
    "fallback_path",
}

# CEO 승인 필요 유형
REQUIRES_CEO_APPROVAL = {
    "db_schema_change",
    "core_logic_change",
    "docker_compose_change",
    "api_key_change",
}


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

            # ── 5. quality_score NULL 비율 분석 ──
            null_stats = await conn.fetchrow("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN quality_score IS NULL THEN 1 ELSE 0 END) as null_cnt
                FROM chat_messages
                WHERE role = 'assistant'
                  AND created_at >= NOW() - interval '1 day'
            """)
            if null_stats and null_stats["total"] > 10:
                null_ratio = null_stats["null_cnt"] / null_stats["total"]
                if null_ratio > 0.5:
                    findings.append(ResearchFinding(
                        project="AADS",
                        category="confidence_threshold",
                        severity="high",
                        title=f"quality_score NULL 비율 높음: {null_ratio*100:.0f}%",
                        description=f"최근 24시간 {null_stats['null_cnt']}/{null_stats['total']} 메시지 quality_score=NULL",
                        suggestion="self_evaluator LLM 호출 실패 — 환경변수·모델 폴백 점검 필요",
                        data={"null_ratio": round(null_ratio, 3), "total": null_stats["total"]},
                    ))

            # ── 6. session_notes 빈값 비율 분석 ──
            notes_stats = await conn.fetchrow("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN content IS NULL OR content = '' THEN 1 ELSE 0 END) as empty_cnt
                FROM session_notes
                WHERE created_at >= NOW() - interval '3 days'
            """)
            if notes_stats and notes_stats["total"] > 5:
                empty_ratio = notes_stats["empty_cnt"] / notes_stats["total"]
                if empty_ratio > 0.5:
                    findings.append(ResearchFinding(
                        project="AADS",
                        category="log_level_hidden_error",
                        severity="medium",
                        title=f"session_notes 빈값 비율 높음: {empty_ratio*100:.0f}%",
                        description=f"최근 3일 {notes_stats['empty_cnt']}/{notes_stats['total']} 노트 content 빈값",
                        suggestion="memory_manager.py INSERT 쿼리 content 컬럼 누락 가능성",
                        data={"empty_ratio": round(empty_ratio, 3)},
                    ))

            # ── 7. 발견사항 저장 ──
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

        # ── 8. CEO 알림 (high severity만) ──
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

        # ── Auto-Remediation 시도 ──
        auto_fix_candidates = [
            f for f in findings
            if f.category in SAFE_TO_AUTO_FIX
        ]
        remediation_results = []
        for finding in auto_fix_candidates[:3]:
            rem = await _attempt_auto_remediation(pool, finding)
            remediation_results.append({
                "finding": finding.title,
                "success": rem.success,
                "method": rem.method,
            })
            if not rem.success and rem.method == "all_methods_exhausted":
                try:
                    from app.services.telegram_bot import get_telegram_bot
                    bot = get_telegram_bot()
                    if bot and bot.is_ready:
                        await bot.send_message(
                            f"⚠️ [Auto-Remediation 실패] {finding.title}\n"
                            f"모든 방법 소진 — CEO 검토 필요\n오류: {rem.error}"
                        )
                except Exception:
                    pass
        result["remediation_results"] = remediation_results

        logger.info(
            "research_agent_complete",
            findings=len(findings),
            high=len(high_findings),
        )

    except Exception as e:
        logger.error("research_agent_error", error=str(e))

    return result


async def _attempt_auto_remediation(pool, finding: ResearchFinding) -> RemediationResult:
    """발견사항에 대해 다방법 폴백 체인으로 자동 수정 시도.

    모든 방법 소진 전까지 시도. 성공 즉시 반환.
    """
    if finding.category not in SAFE_TO_AUTO_FIX:
        return RemediationResult(
            success=False,
            method="skipped_requires_approval",
            error=f"{finding.category}는 CEO 승인 필요",
        )

    methods = _get_remediation_methods(finding)
    if not methods:
        return RemediationResult(success=False, method="no_methods_available")

    for method in methods:
        backup_path = None
        try:
            if os.path.exists(method.target_file):
                backup_path = method.target_file + ".remediation_bak"
                shutil.copy2(method.target_file, backup_path)

            applied = await _apply_remediation(method, finding)
            if not applied:
                continue

            await _restart_service()
            await asyncio.sleep(60)
            verified = await _verify_fix(pool, finding)

            if verified:
                if backup_path and os.path.exists(backup_path):
                    os.remove(backup_path)
                logger.info("auto_remediation_success", method=method.name, finding=finding.title)
                return RemediationResult(success=True, method=method.name, after_value="fixed")
            else:
                if backup_path and os.path.exists(backup_path):
                    shutil.copy2(backup_path, method.target_file)
                    os.remove(backup_path)
                    await _restart_service()
                logger.warning("auto_remediation_rolled_back", method=method.name)

        except Exception as e:
            logger.error("auto_remediation_method_error", method=method.name, error=str(e))
            if backup_path and os.path.exists(backup_path):
                try:
                    shutil.copy2(backup_path, method.target_file)
                    os.remove(backup_path)
                except Exception:
                    pass

    return RemediationResult(
        success=False,
        method="all_methods_exhausted",
        error=f"{len(methods)}개 방법 모두 실패",
    )


def _get_remediation_methods(finding: ResearchFinding) -> List[RemediationMethod]:
    """발견사항 유형별 수정 방법 목록 반환."""
    methods = []
    if finding.category == "confidence_threshold":
        methods.append(RemediationMethod(
            name="lower_confidence_threshold",
            target_file="/app/app/core/memory_recall.py",
            description="confidence 임계값 0.40으로 하향",
        ))
    return methods


async def _apply_remediation(method: RemediationMethod, finding: ResearchFinding) -> bool:
    """수정 적용."""
    try:
        if method.name == "lower_confidence_threshold":
            with open(method.target_file, "r") as f:
                content = f.read()
            if '"0.40"' in content:
                return False  # 이미 적용됨
            new_content = content.replace('"0.55"', '"0.40"').replace('"0.50"', '"0.40"').replace('"0.45"', '"0.40"')
            with open(method.target_file, "w") as f:
                f.write(new_content)
            return True
        return False
    except Exception as e:
        logger.error("apply_remediation_error", error=str(e))
        return False


async def _restart_service() -> bool:
    """supervisorctl로 aads-server 무중단 재시작."""
    try:
        result = subprocess.run(
            ["supervisorctl", "restart", "aads-server"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            logger.error("restart_failed", stderr=result.stderr)
            return False
        await asyncio.sleep(10)  # 시작 대기
        return True
    except Exception as e:
        logger.error("restart_service_error", error=str(e))
        return False


async def _verify_fix(pool, finding: ResearchFinding) -> bool:
    """수정 후 DB 검증."""
    try:
        async with pool.acquire() as conn:
            if "quality_score NULL" in finding.title:
                row = await conn.fetchrow("""
                    SELECT COUNT(*) as total,
                           SUM(CASE WHEN quality_score IS NULL THEN 1 ELSE 0 END) as null_cnt
                    FROM chat_messages
                    WHERE role = 'assistant'
                      AND created_at >= NOW() - interval '30 minutes'
                """)
                if row and row["total"] > 0:
                    return (row["null_cnt"] / row["total"]) < 0.3
        return True
    except Exception as e:
        logger.error("verify_fix_error", error=str(e))
        return False


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
