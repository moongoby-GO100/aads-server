"""
ai_observations TTL 기반 가비지 컬렉션 + F4 Memory Consolidation.
30일 미사용 + confidence 감쇠 → 임계값 미만 자동 삭제.
ceo_preference 카테고리는 GC 대상에서 제외.

F4: 매일 04:00 UTC — 중복 병합, 자주 참조 사실 강화, 미참조 감쇠.
C1: Sleep-Time Agent — 매일 05:00 UTC — 프로젝트별 인사이트 생성 + 프롬프트 최적화.
C3: Adaptive forgetting curves — 카테고리별 차별 감쇠율 적용.
"""
from __future__ import annotations

import json
import os
import uuid

import structlog

logger = structlog.get_logger(__name__)

GC_MAX_AGE_DAYS = int(os.getenv("MEMORY_GC_MAX_AGE_DAYS", "30"))
GC_DECAY_FACTOR = float(os.getenv("MEMORY_GC_DECAY_FACTOR", "0.9"))
GC_DELETE_THRESHOLD = float(os.getenv("MEMORY_GC_DELETE_THRESHOLD", "0.1"))

# GC 제외 카테고리 (CEO 선호, 핵심 규칙 등)
_PROTECTED_CATEGORIES = (
    "ceo_preference",
    "ceo_directive",
    "compaction_directive",
)

# F4 설정
_CONSOLIDATION_SIMILARITY = float(os.getenv("CONSOLIDATION_SIMILARITY", "0.92"))
_REFERENCED_BOOST = float(os.getenv("CONSOLIDATION_REFERENCED_BOOST", "0.05"))
_FACTS_DECAY_DAYS = int(os.getenv("FACTS_DECAY_DAYS", "14"))
_FACTS_DECAY_FACTOR = float(os.getenv("FACTS_DECAY_FACTOR", "0.95"))

# C3: Adaptive forgetting curves — 카테고리별 차별 감쇠율
DECAY_RATES = {
    "config_change": 0.85,       # fast decay — configs change often
    "file_change": 0.90,         # medium-fast
    "error_resolution": 0.92,    # medium
    "timeline_event": 0.95,      # slow
    "decision": 0.98,            # very slow — decisions are long-lived
    "ceo_instruction": 0.99,     # near-permanent
    "error_pattern": 0.93,       # medium — patterns evolve
}
_DECAY_DEFAULT = 0.95  # unknown categories

# C1: Sleep-Time Agent 설정
_HAIKU_MODEL = os.getenv("SLEEP_AGENT_MODEL", "claude-haiku-4-5-20251001")
_MAX_INSIGHTS_PER_PROJECT = 3


async def gc_observations(pool) -> dict:
    """ai_observations 가비지 컬렉션.

    1단계: max_age_days 이상 미갱신 항목의 confidence 감쇠 (× decay_factor)
    2단계: confidence < delete_threshold인 오래된 항목 삭제

    Returns:
        {"decayed": int, "deleted": int}
    """
    result = {"decayed": 0, "deleted": 0}
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                # 1단계: confidence 감쇠
                decayed = await conn.execute(
                    """
                    UPDATE ai_observations
                    SET confidence = confidence * $1
                    WHERE updated_at < NOW() - make_interval(days => $2)
                      AND confidence > $3
                      AND category NOT IN (SELECT unnest($4::text[]))
                    """,
                    GC_DECAY_FACTOR,
                    GC_MAX_AGE_DAYS,
                    GC_DELETE_THRESHOLD,
                    list(_PROTECTED_CATEGORIES),
                )
                result["decayed"] = int(decayed.split()[-1]) if decayed else 0

                # 2단계: 임계값 미만 삭제
                deleted = await conn.execute(
                    """
                    DELETE FROM ai_observations
                    WHERE confidence < $1
                      AND updated_at < NOW() - make_interval(days => $2)
                      AND category NOT IN (SELECT unnest($3::text[]))
                    """,
                    GC_DELETE_THRESHOLD,
                    GC_MAX_AGE_DAYS,
                    list(_PROTECTED_CATEGORIES),
                )
                result["deleted"] = int(deleted.split()[-1]) if deleted else 0

        logger.info("memory_gc_complete", **result)
    except Exception as e:
        logger.error("memory_gc_error", error=str(e))
    return result


async def consolidate_memory_facts(pool) -> dict:
    """F4: Memory Consolidation — 매일 실행.

    1. 자주 참조된 사실 confidence 강화 (+0.05)
    2. 미참조 사실 confidence 감쇠 (×0.95)
    3. 중복 사실 병합 (임베딩 유사도 > 0.92 + 같은 카테고리)
    4. 프로젝트별 상태 스냅샷 생성

    Returns:
        {"boosted": int, "decayed": int, "merged": int, "snapshots": int}
    """
    result = {"boosted": 0, "decayed": 0, "merged": 0, "snapshots": 0}

    try:
        async with pool.acquire() as conn:
            # 1. 자주 참조된 사실 강화
            boosted = await conn.execute(
                """
                UPDATE memory_facts
                SET confidence = LEAST(1.0, confidence + $1),
                    updated_at = NOW()
                WHERE referenced_count > 0
                  AND last_referenced_at > NOW() - INTERVAL '7 days'
                  AND superseded_by IS NULL
                """,
                _REFERENCED_BOOST,
            )
            result["boosted"] = int(boosted.split()[-1]) if boosted else 0

            # 2. 미참조 사실 감쇠 (C3: 카테고리별 적응형 감쇠율)
            total_decayed = 0
            for cat, rate in DECAY_RATES.items():
                cat_decayed = await conn.execute(
                    """
                    UPDATE memory_facts
                    SET confidence = confidence * $1
                    WHERE category = $4
                      AND (referenced_count = 0 OR last_referenced_at IS NULL
                           OR last_referenced_at < NOW() - INTERVAL '14 days')
                      AND created_at < NOW() - make_interval(days => $2)
                      AND superseded_by IS NULL
                      AND confidence > 0.1
                    """,
                    rate,
                    _FACTS_DECAY_DAYS,
                    GC_DELETE_THRESHOLD,
                    cat,
                )
                total_decayed += int(cat_decayed.split()[-1]) if cat_decayed else 0

            # 미지정 카테고리에 기본 감쇠율 적용
            known_cats = list(DECAY_RATES.keys())
            default_decayed = await conn.execute(
                """
                UPDATE memory_facts
                SET confidence = confidence * $1
                WHERE (referenced_count = 0 OR last_referenced_at IS NULL
                       OR last_referenced_at < NOW() - INTERVAL '14 days')
                  AND created_at < NOW() - make_interval(days => $2)
                  AND superseded_by IS NULL
                  AND confidence > 0.1
                  AND category NOT IN (SELECT unnest($3::text[]))
                """,
                _DECAY_DEFAULT,
                _FACTS_DECAY_DAYS,
                known_cats,
            )
            total_decayed += int(default_decayed.split()[-1]) if default_decayed else 0
            result["decayed"] = total_decayed

            # 3. 낮은 confidence 사실 삭제
            deleted = await conn.execute(
                """
                DELETE FROM memory_facts
                WHERE confidence < 0.1
                  AND created_at < NOW() - INTERVAL '30 days'
                  AND category NOT IN ('ceo_instruction', 'decision')
                """,
            )
            _del_count = int(deleted.split()[-1]) if deleted else 0
            if _del_count > 0:
                logger.info("memory_facts_gc_deleted", count=_del_count)

            # 4. 중복 병합 (같은 카테고리 + 높은 임베딩 유사도)
            result["merged"] = await _merge_duplicate_facts(conn)

            # 5. 프로젝트별 상태 스냅샷 (Haiku로 요약)
            result["snapshots"] = await _generate_project_snapshots(conn)

        # B3: Tool efficiency analysis
        try:
            await _analyze_tool_efficiency(pool)
        except Exception as e_b3:
            logger.debug("b3_tool_efficiency_error", error=str(e_b3))

        logger.info("memory_consolidation_complete", **result)
    except Exception as e:
        logger.error("memory_consolidation_error", error=str(e))

    return result


async def _merge_duplicate_facts(conn) -> int:
    """중복 사실 병합: 같은 카테고리에서 임베딩 유사도 > 0.92인 쌍 병합."""
    merged_count = 0
    try:
        # 카테고리별로 최근 사실 조회
        categories = await conn.fetch(
            "SELECT DISTINCT category FROM memory_facts WHERE superseded_by IS NULL AND embedding IS NOT NULL"
        )

        for cat_row in categories:
            cat = cat_row["category"]
            facts = await conn.fetch(
                """
                SELECT id, subject, detail, confidence, embedding, created_at
                FROM memory_facts
                WHERE category = $1 AND superseded_by IS NULL AND embedding IS NOT NULL
                ORDER BY confidence DESC, created_at DESC
                LIMIT 50
                """,
                cat,
            )

            # 쌍별 유사도 비교 (O(n²) but n ≤ 50)
            seen_merged = set()
            for i, f1 in enumerate(facts):
                if str(f1["id"]) in seen_merged:
                    continue
                for f2 in facts[i + 1:]:
                    if str(f2["id"]) in seen_merged:
                        continue

                    # DB에서 유사도 계산
                    sim_row = await conn.fetchrow(
                        "SELECT 1 - ($1::vector <=> $2::vector) AS sim",
                        str(f1["embedding"]), str(f2["embedding"]),
                    )
                    sim = float(sim_row["sim"]) if sim_row else 0

                    if sim >= _CONSOLIDATION_SIMILARITY:
                        # f2를 f1에 병합 (f1이 confidence 높은 쪽)
                        await conn.execute(
                            "UPDATE memory_facts SET superseded_by = $1 WHERE id = $2",
                            f1["id"], f2["id"],
                        )
                        # f1의 detail에 f2 정보 보강 (짧으면)
                        if len(f1["detail"]) < len(f2["detail"]):
                            await conn.execute(
                                "UPDATE memory_facts SET detail = $1 WHERE id = $2",
                                f2["detail"], f1["id"],
                            )
                        seen_merged.add(str(f2["id"]))
                        merged_count += 1

    except Exception as e:
        logger.debug("merge_duplicate_facts_error", error=str(e))

    return merged_count


async def _generate_project_snapshots(conn) -> int:
    """프로젝트별 현재 상태 스냅샷을 memory_facts에 저장."""
    snapshot_count = 0
    try:
        projects = await conn.fetch(
            """
            SELECT DISTINCT project FROM memory_facts
            WHERE project IS NOT NULL AND superseded_by IS NULL
            """
        )

        for p_row in projects:
            project = p_row["project"]
            # 프로젝트의 최근 사실 가져오기
            recent = await conn.fetch(
                """
                SELECT category, subject FROM memory_facts
                WHERE project = $1 AND superseded_by IS NULL AND confidence > 0.3
                ORDER BY created_at DESC LIMIT 20
                """,
                project,
            )

            if len(recent) < 3:
                continue

            # 간단한 스냅샷 생성 (Haiku 없이 — 비용 절감)
            categories = {}
            for r in recent:
                cat = r["category"]
                categories.setdefault(cat, []).append(r["subject"])

            snapshot_lines = [f"[{project}] 상태 스냅샷:"]
            for cat, subjects in categories.items():
                snapshot_lines.append(f"  {cat}: {', '.join(subjects[:3])}")

            snapshot_text = "\n".join(snapshot_lines)

            # 새 스냅샷 삽입 후 ID 획득
            new_snapshot_id = await conn.fetchval(
                """
                INSERT INTO memory_facts (project, category, subject, detail, confidence)
                VALUES ($1, 'project_snapshot', $2, $3, 0.6)
                RETURNING id
                """,
                project,
                f"{project} 일일 스냅샷",
                snapshot_text,
            )

            # 기존 스냅샷을 새 스냅샷으로 supersede (FK 유효)
            await conn.execute(
                """
                UPDATE memory_facts SET superseded_by = $2
                WHERE project = $1 AND category = 'project_snapshot' AND superseded_by IS NULL AND id != $2
                """,
                project, new_snapshot_id,
            )
            snapshot_count += 1

    except Exception as e:
        logger.debug("project_snapshots_error", error=str(e))

    return snapshot_count


async def _analyze_tool_efficiency(pool) -> None:
    """B3: Tool efficiency analysis — 도구 사용 통계를 ai_observations에 저장.

    tool_results_archive에서 최근 7일 데이터를 분석:
    - 도구별 성공률 (error/Error/실패 미포함 비율)
    - 가장 많이 사용된 도구 Top 5
    결과를 ai_observations에 tool_strategy 카테고리로 upsert.
    """
    try:
        async with pool.acquire() as conn:
            # 도구별 사용 횟수 + 에러 횟수 (최근 7일)
            rows = await conn.fetch(
                """
                SELECT tool_name,
                       COUNT(*) AS total,
                       COUNT(*) FILTER (
                           WHERE raw_output ILIKE '%error%'
                              OR raw_output ILIKE '%Error%'
                              OR raw_output ILIKE '%실패%'
                       ) AS error_count
                FROM tool_results_archive
                WHERE created_at > NOW() - INTERVAL '7 days'
                GROUP BY tool_name
                ORDER BY COUNT(*) DESC
                """
            )

            if not rows:
                return

            # Top 5 most-used tools
            top5 = []
            for r in rows[:5]:
                total = int(r["total"])
                errors = int(r["error_count"])
                success_rate = ((total - errors) / total * 100) if total > 0 else 0
                top5.append(f"{r['tool_name']}: {total}회 (성공률 {success_rate:.0f}%)")

            summary = "주간 도구 사용 분석 Top5:\n" + "\n".join(top5)

            # 전체 통계
            total_all = sum(int(r["total"]) for r in rows)
            error_all = sum(int(r["error_count"]) for r in rows)
            overall_rate = ((total_all - error_all) / total_all * 100) if total_all > 0 else 0
            summary += f"\n전체: {total_all}회, 성공률 {overall_rate:.0f}%"

            # ai_observations에 upsert
            await conn.execute(
                """
                INSERT INTO ai_observations (category, key, value, confidence, updated_at)
                VALUES ('tool_strategy', 'weekly_tool_efficiency', $1, 0.7, NOW())
                ON CONFLICT (category, key) DO UPDATE SET
                    value = EXCLUDED.value,
                    confidence = EXCLUDED.confidence,
                    updated_at = NOW()
                """,
                summary,
            )

            logger.info("b3_tool_efficiency_saved", tools=len(rows), total=total_all)

    except Exception as e:
        logger.debug("b3_tool_efficiency_error", error=str(e))


async def sleep_time_consolidation(pool) -> dict:
    """C1: Sleep-Time Agent — 백그라운드 메모리 통합.

    매일 05:00 UTC (04:00 consolidation 이후) 실행.
    1. 프로젝트별 관련 사실 클러스터에서 인사이트 생성 (Haiku)
    2. C2: quality_score 추세 분석 → 프롬프트 교정 지시 생성

    Returns:
        {"insights": int, "optimizations": int}
    """
    result = {"insights": 0, "optimizations": 0}

    try:
        async with pool.acquire() as conn:
            # ── C1: 프로젝트별 인사이트 생성 ──────────────────────────────
            projects = await conn.fetch(
                """
                SELECT DISTINCT project FROM memory_facts
                WHERE project IS NOT NULL AND superseded_by IS NULL
                """
            )

            for p_row in projects:
                project = p_row["project"]
                try:
                    insights_count = await _generate_project_insights(conn, project)
                    result["insights"] += insights_count
                except Exception as e:
                    logger.debug("sleep_agent_insight_error", project=project, error=str(e))

            # ── C2: 프롬프트 자동 최적화 ──────────────────────────────────
            try:
                opt_count = await _analyze_quality_and_optimize(conn)
                result["optimizations"] = opt_count
            except Exception as e:
                logger.debug("sleep_agent_optimization_error", error=str(e))

        logger.info("sleep_time_consolidation_complete", **result)
    except Exception as e:
        logger.error("sleep_time_consolidation_error", error=str(e))

    return result


async def _generate_project_insights(conn, project: str) -> int:
    """C1: 프로젝트의 상위 사실들로부터 Haiku 인사이트 생성. 최대 3건."""
    # 프로젝트의 활성 사실 조회 (confidence 상위, 최근 것)
    facts = await conn.fetch(
        """
        SELECT category, subject, detail FROM memory_facts
        WHERE project = $1 AND superseded_by IS NULL AND confidence > 0.3
        ORDER BY confidence DESC, created_at DESC
        LIMIT 30
        """,
        project,
    )

    if len(facts) < 5:
        return 0

    # 기존 project_insight가 최근 24시간 내 생성된 경우 스킵
    existing = await conn.fetchval(
        """
        SELECT COUNT(*) FROM memory_facts
        WHERE project = $1 AND category = 'project_insight'
          AND created_at > NOW() - INTERVAL '24 hours'
          AND superseded_by IS NULL
        """,
        project,
    )
    if existing and int(existing) >= _MAX_INSIGHTS_PER_PROJECT:
        return 0

    # 사실 요약 텍스트 구성
    fact_lines = []
    for f in facts:
        fact_lines.append(f"[{f['category']}] {f['subject']}: {f['detail'][:100]}")
    facts_text = "\n".join(fact_lines)

    prompt = f"""다음은 [{project}] 프로젝트의 최근 핵심 사실 목록입니다:

{facts_text}

위 사실들을 분석하여 1~3개의 고수준 인사이트를 생성하세요.
- 반복 패턴, 공통 원인, 핵심 교훈을 도출
- 각 인사이트는 1~2문장으로 한국어 작성
- JSON 배열로 반환: [{{"insight": "...", "subject": "20자 이내 요약"}}]
- 마크다운 코드블록 없이 JSON만 반환"""

    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic()

        response = await client.messages.create(
            model=_HAIKU_MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3].strip()

        insights = json.loads(text)
        if not isinstance(insights, list):
            return 0

        saved = 0
        for item in insights[:_MAX_INSIGHTS_PER_PROJECT]:
            insight_text = item.get("insight", "")
            subject = item.get("subject", insight_text[:20])
            if not insight_text:
                continue

            # 새 인사이트 삽입
            new_id = await conn.fetchval(
                """
                INSERT INTO memory_facts (project, category, subject, detail, confidence)
                VALUES ($1, 'project_insight', $2, $3, 0.9)
                RETURNING id
                """,
                project,
                subject[:300],
                insight_text,
            )

            # 이전 인사이트를 supersede
            if new_id:
                await conn.execute(
                    """
                    UPDATE memory_facts SET superseded_by = $2
                    WHERE project = $1 AND category = 'project_insight'
                      AND superseded_by IS NULL AND id != $2
                      AND created_at < NOW() - INTERVAL '24 hours'
                    """,
                    project, new_id,
                )
                saved += 1

        logger.info("sleep_agent_insights_generated", project=project, count=saved)
        return saved

    except Exception as e:
        logger.debug("sleep_agent_haiku_error", project=project, error=str(e))
        return 0


async def _analyze_quality_and_optimize(conn) -> int:
    """C2: quality_score 추세 분석 → 프롬프트 교정 지시 생성.

    평균 quality_score < 0.5인 프로젝트에 대해 Haiku로 교정 지시를 생성하고
    ai_meta_memory에 prompt_optimization 카테고리로 저장.
    """
    # 프로젝트별 최근 7일 평균 quality_score 조회
    rows = await conn.fetch(
        """
        SELECT ws.name AS workspace_name,
               AVG(cm.quality_score) AS avg_score,
               COUNT(*) AS msg_count
        FROM chat_messages cm
        JOIN chat_sessions cs ON cm.session_id = cs.id
        JOIN chat_workspaces ws ON cs.workspace_id = ws.id
        WHERE cm.quality_score IS NOT NULL
          AND cm.created_at > NOW() - INTERVAL '7 days'
          AND cm.role = 'assistant'
        GROUP BY ws.name
        HAVING COUNT(*) >= 5
        ORDER BY AVG(cm.quality_score) ASC
        """
    )

    if not rows:
        return 0

    optimization_count = 0
    for row in rows:
        avg_score = float(row["avg_score"]) if row["avg_score"] else 1.0
        workspace = row["workspace_name"] or "unknown"
        msg_count = int(row["msg_count"])

        if avg_score >= 0.5:
            continue

        # 낮은 품질 세션의 구체적 문제 파악을 위해 낮은 점수 메시지 샘플링
        low_samples = await conn.fetch(
            """
            SELECT cm.quality_details
            FROM chat_messages cm
            JOIN chat_sessions cs ON cm.session_id = cs.id
            JOIN chat_workspaces ws ON cs.workspace_id = ws.id
            WHERE ws.name = $1
              AND cm.quality_score IS NOT NULL AND cm.quality_score < 0.5
              AND cm.created_at > NOW() - INTERVAL '7 days'
              AND cm.role = 'assistant'
            ORDER BY cm.quality_score ASC
            LIMIT 5
            """,
            workspace,
        )

        details_text = ""
        for s in low_samples:
            if s["quality_details"]:
                detail = s["quality_details"]
                if isinstance(detail, str):
                    details_text += detail[:200] + "\n"
                else:
                    details_text += json.dumps(detail, ensure_ascii=False)[:200] + "\n"

        prompt = f"""[{workspace}] 워크스페이스의 최근 7일 AI 응답 품질이 낮습니다.
평균 점수: {avg_score:.2f}/1.0 (메시지 {msg_count}건)

품질 저하 상세:
{details_text if details_text else '(상세 정보 없음)'}

이 문제를 개선하기 위한 구체적인 교정 지시를 1~2문장으로 작성하세요.
예: "DB 쿼리 결과를 반드시 도구로 확인한 후 답변할 것" 또는 "코드 수정 시 변경 전후를 명시할 것"
교정 지시만 반환하세요 (설명 불필요)."""

        try:
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic()

            response = await client.messages.create(
                model=_HAIKU_MODEL,
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )

            instruction = response.content[0].text.strip()
            if not instruction:
                continue

            # ai_meta_memory에 prompt_optimization으로 저장
            await conn.execute(
                """
                INSERT INTO ai_meta_memory (category, key, value, confidence, updated_at)
                VALUES ('prompt_optimization', $1, $2, 0.8, NOW())
                ON CONFLICT (key) DO UPDATE SET
                    value = EXCLUDED.value,
                    confidence = LEAST(ai_meta_memory.confidence + 0.1, 1.0),
                    updated_at = NOW()
                """,
                f"quality_fix_{workspace}",
                json.dumps({"workspace": workspace, "avg_score": avg_score,
                            "instruction": instruction, "msg_count": msg_count},
                           ensure_ascii=False),
            )
            optimization_count += 1
            logger.info("sleep_agent_prompt_optimization",
                        workspace=workspace, avg_score=avg_score)

        except Exception as e:
            logger.debug("sleep_agent_optimization_haiku_error",
                         workspace=workspace, error=str(e))

    return optimization_count
