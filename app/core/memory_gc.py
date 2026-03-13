"""
ai_observations TTL 기반 가비지 컬렉션 + F4 Memory Consolidation.
30일 미사용 + confidence 감쇠 → 임계값 미만 자동 삭제.
ceo_preference 카테고리는 GC 대상에서 제외.

F4: 매일 04:00 UTC — 중복 병합, 자주 참조 사실 강화, 미참조 감쇠.
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

            # 2. 미참조 사실 감쇠
            decayed = await conn.execute(
                """
                UPDATE memory_facts
                SET confidence = confidence * $1
                WHERE (referenced_count = 0 OR last_referenced_at IS NULL
                       OR last_referenced_at < NOW() - INTERVAL '14 days')
                  AND created_at < NOW() - make_interval(days => $2)
                  AND superseded_by IS NULL
                  AND confidence > 0.1
                  AND category NOT IN ('ceo_instruction', 'decision')
                """,
                _FACTS_DECAY_FACTOR,
                _FACTS_DECAY_DAYS,
            )
            result["decayed"] = int(decayed.split()[-1]) if decayed else 0

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
