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

    # B3: ensure is_error column exists
    try:
        async with pool.acquire() as conn_ddl:
            await conn_ddl.execute(
                "ALTER TABLE tool_results_archive ADD COLUMN IF NOT EXISTS is_error BOOLEAN DEFAULT FALSE"
            )
    except Exception:
        pass

    try:
        async with pool.acquire() as conn:
          async with conn.transaction():
            # 1. 자주 참조된 사실 강화
            boosted = await conn.execute(
                """
                UPDATE memory_facts
                SET confidence = LEAST(1.0, confidence + $1),
                    updated_at = NOW()
                WHERE referenced_count > 0
                  AND last_referenced_at > NOW() - INTERVAL '14 days'
                  AND superseded_by IS NULL
                """,
                _REFERENCED_BOOST,
            )
            result["boosted"] = int(boosted.split()[-1]) if boosted else 0

            # P5: referenced_count 비례 추가 강화 (자주 참조된 사실일수록 더 크게 강화)
            await conn.execute(
                """
                UPDATE memory_facts
                SET confidence = LEAST(1.0, confidence +
                    CASE
                        WHEN referenced_count >= 20 THEN 0.08
                        WHEN referenced_count >= 10 THEN 0.05
                        WHEN referenced_count >= 5  THEN 0.03
                        ELSE 0.0
                    END
                ),
                updated_at = NOW()
                WHERE referenced_count >= 5
                  AND last_referenced_at > NOW() - INTERVAL '14 days'
                  AND superseded_by IS NULL
                  AND confidence < 0.98
                """,
            )

            # 2. 미참조 사실 감쇠 (C3: 카테고리별 적응형 감쇠율)
            total_decayed = 0
            for cat, rate in DECAY_RATES.items():
                cat_decayed = await conn.execute(
                    """
                    UPDATE memory_facts
                    SET confidence = confidence * $1
                    WHERE category = $3
                      AND (referenced_count = 0 OR last_referenced_at IS NULL
                           OR last_referenced_at < NOW() - INTERVAL '14 days')
                      AND created_at < NOW() - make_interval(days => $2)
                      AND superseded_by IS NULL
                      AND confidence > $4
                    """,
                    rate,
                    _FACTS_DECAY_DAYS,
                    cat,
                    GC_DELETE_THRESHOLD,
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
                  AND confidence > $4
                  AND category NOT IN (SELECT unnest($3::text[]))
                """,
                _DECAY_DEFAULT,
                _FACTS_DECAY_DAYS,
                known_cats,
                GC_DELETE_THRESHOLD,
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
    """중복 사실 병합: pgvector self-join으로 유사도 >= 0.92인 쌍 일괄 병합.
    H-14: O(n²) Python 루프 → 단일 SQL로 전환."""
    merged_count = 0
    try:
        # 1) detail 보강: 중복 쌍에서 더 긴 detail을 primary에 반영
        await conn.execute(
            """
            WITH duplicate_pairs AS (
              SELECT
                CASE WHEN f1.confidence >= f2.confidence THEN f1.id ELSE f2.id END AS keep_id,
                CASE WHEN f1.confidence >= f2.confidence THEN f2.id ELSE f1.id END AS drop_id,
                CASE WHEN f1.confidence >= f2.confidence
                     THEN LENGTH(COALESCE(f1.detail,'')) < LENGTH(COALESCE(f2.detail,''))
                     ELSE LENGTH(COALESCE(f2.detail,'')) < LENGTH(COALESCE(f1.detail,''))
                END AS swap_detail
              FROM memory_facts f1
              JOIN memory_facts f2
                ON f1.category = f2.category
               AND f1.id < f2.id
               AND f1.superseded_by IS NULL
               AND f2.superseded_by IS NULL
               AND f1.embedding IS NOT NULL
               AND f2.embedding IS NOT NULL
              WHERE 1 - (f1.embedding <=> f2.embedding) >= $1
            ),
            detail_swap AS (
              SELECT dp.keep_id, f_drop.detail AS better_detail
              FROM duplicate_pairs dp
              JOIN memory_facts f_drop ON dp.drop_id = f_drop.id
              WHERE dp.swap_detail = true
            )
            UPDATE memory_facts f
            SET detail = ds.better_detail
            FROM detail_swap ds
            WHERE f.id = ds.keep_id
            """,
            _CONSOLIDATION_SIMILARITY,
        )

        # 2) 중복 마킹: drop_id를 superseded_by = keep_id로 설정
        result = await conn.execute(
            """
            WITH duplicate_pairs AS (
              SELECT
                CASE WHEN f1.confidence >= f2.confidence THEN f1.id ELSE f2.id END AS keep_id,
                CASE WHEN f1.confidence >= f2.confidence THEN f2.id ELSE f1.id END AS drop_id
              FROM memory_facts f1
              JOIN memory_facts f2
                ON f1.category = f2.category
               AND f1.id < f2.id
               AND f1.superseded_by IS NULL
               AND f2.superseded_by IS NULL
               AND f1.embedding IS NOT NULL
               AND f2.embedding IS NOT NULL
              WHERE 1 - (f1.embedding <=> f2.embedding) >= $1
            )
            UPDATE memory_facts
            SET superseded_by = dp.keep_id
            FROM duplicate_pairs dp
            WHERE memory_facts.id = dp.drop_id
            """,
            _CONSOLIDATION_SIMILARITY,
        )

        merged_count = int(result.split()[-1]) if result else 0
        if merged_count > 0:
            logger.info("memory_facts_merged_pgvector", count=merged_count)

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
    """B3: 도구 효율 분석 — tool_results_archive 기반 성공률 주간 분석."""
    try:
        async with pool.acquire() as conn:
            # is_error 컬럼 존재 확인
            col_exists = await conn.fetchval(
                """SELECT COUNT(*) FROM information_schema.columns
                   WHERE table_name='tool_results_archive' AND column_name='is_error'"""
            )
            if not col_exists:
                return

            rows = await conn.fetch(
                """SELECT tool_name,
                          COUNT(*) as total,
                          SUM(CASE WHEN is_error THEN 1 ELSE 0 END) as errors,
                          AVG(output_tokens) as avg_tokens
                   FROM tool_results_archive
                   WHERE created_at > NOW() - INTERVAL '7 days'
                   GROUP BY tool_name
                   HAVING COUNT(*) >= 3
                   ORDER BY errors DESC"""
            )
            if not rows:
                return

            insights = []
            for r in rows:
                error_rate = float(r["errors"]) / float(r["total"]) if r["total"] else 0
                if error_rate > 0.3:
                    insights.append(
                        f"{r['tool_name']}: 에러율 {error_rate:.0%} ({r['errors']}/{r['total']}건) — 사용 주의"
                    )
                elif r["avg_tokens"] and float(r["avg_tokens"]) > 3000:
                    insights.append(
                        f"{r['tool_name']}: 평균 {r['avg_tokens']:.0f} 토큰 — 고비용 도구"
                    )

            if insights:
                insight_text = "주간 도구 효율 분석:\n" + "\n".join(f"- {i}" for i in insights)
                async with pool.acquire() as conn2:
                    await conn2.execute(
                        """INSERT INTO ai_observations (category, key, value, confidence)
                           VALUES ('tool_strategy', 'weekly_tool_efficiency', $1, 0.8)
                           ON CONFLICT (category, key, COALESCE(project, '')) DO UPDATE SET
                               value = EXCLUDED.value,
                               confidence = 0.8,
                               updated_at = NOW()""",
                        insight_text,
                    )
                logger.info("b3_tool_efficiency_updated", insights=len(insights))
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
        # 프로젝트 목록 조회 후 커넥션 즉시 반환 (LLM 호출 중 점유 방지)
        async with pool.acquire() as conn:
            projects = await conn.fetch(
                """
                SELECT DISTINCT project FROM memory_facts
                WHERE project IS NOT NULL AND superseded_by IS NULL
                """
            )

        # ── C1: 프로젝트별 인사이트 생성 (별도 커넥션으로 LLM 호출) ──
        for p_row in projects:
            project = p_row["project"]
            try:
                async with pool.acquire() as conn_insight:
                    insights_count = await _generate_project_insights(conn_insight, project)
                    result["insights"] += insights_count
            except Exception as e:
                logger.debug("sleep_agent_insight_error", project=project, error=str(e))

        # ── C2: 프롬프트 자동 최적화 (별도 커넥션) ──
        try:
            async with pool.acquire() as conn_opt:
                opt_count = await _analyze_quality_and_optimize(conn_opt)
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
        import os as _os
        api_key = _os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            logger.warning("sleep_agent_no_api_key", project=project, hint="ANTHROPIC_API_KEY not set")
            return 0

        from app.core.anthropic_client import get_client
        client = get_client()

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
            logger.debug("sleep_agent_invalid_json", project=project, text=text[:100])
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

    except json.JSONDecodeError as e_json:
        logger.warning("sleep_agent_json_parse_error", project=project, error=str(e_json))
        return 0
    except Exception as e:
        logger.warning("sleep_agent_haiku_error", project=project, error=str(e))
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
            import os as _os
            if not _os.getenv("ANTHROPIC_API_KEY", ""):
                logger.warning("sleep_agent_c2_no_api_key", workspace=workspace)
                continue

            from app.core.anthropic_client import get_client
            client = get_client()

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


async def background_session_compaction():
    """2시간마다: 200건 이상 미압축 세션 자동 압축."""
    try:
        from app.core.db_pool import get_pool
        pool = get_pool()
        async with pool.acquire() as conn:
            sessions = await conn.fetch("""
                SELECT session_id, COUNT(*) as msg_count
                FROM chat_messages
                WHERE is_compacted = false OR is_compacted IS NULL
                GROUP BY session_id
                HAVING COUNT(*) > 200
                ORDER BY COUNT(*) DESC
                LIMIT 5
            """)

        if not sessions:
            return

        logger.info(f"background_compaction: {len(sessions)} sessions need compaction")

        from app.services.compaction_service import check_and_compact
        for s in sessions:
            sid = str(s['session_id'])
            try:
                async with pool.acquire() as conn:
                    msgs = await conn.fetch(
                        "SELECT role, content FROM chat_messages WHERE session_id = $1 AND (is_compacted = false OR is_compacted IS NULL) ORDER BY created_at LIMIT 500",
                        s['session_id']
                    )
                    msg_list = [{"role": r["role"], "content": r["content"]} for r in msgs]

                if msg_list:
                    await check_and_compact(sid, msg_list)
                    logger.info(f"background_compaction: session {sid[:8]} compacted ({s['msg_count']} msgs)")
            except Exception as e:
                logger.warning(f"background_compaction_error: session {sid[:8]}: {e}")
    except Exception as e:
        logger.warning(f"background_session_compaction error: {e}")
