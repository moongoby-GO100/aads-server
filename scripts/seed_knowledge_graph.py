"""
P2 지식그래프 시드 — 기존 memory_facts에서 엔티티/관계 추출.
고가치 카테고리(decision, architecture_decision, ceo_instruction, error_pattern,
error_resolution)에서 배치 처리.

사용법: docker exec aads-server python3 scripts/seed_knowledge_graph.py
"""
import asyncio
import os
import sys

sys.path.insert(0, "/app")


async def seed():
    import asyncpg
    from app.core.db_pool import init_pool, close_pool
    from app.core.knowledge_graph import process_fact_for_graph

    await init_pool()
    conn = await asyncpg.connect(os.getenv("DATABASE_URL"))

    categories = [
        "decision", "architecture_decision", "ceo_instruction",
        "error_pattern", "error_resolution", "api_contract",
        "feature_change", "data_model_change",
    ]

    total_entities = 0
    total_relations = 0

    for cat in categories:
        rows = await conn.fetch(
            """
            SELECT id::text, category, subject, detail, project
            FROM memory_facts
            WHERE category = $1
              AND subject IS NOT NULL
              AND LENGTH(subject) > 5
            ORDER BY confidence DESC, referenced_count DESC
            LIMIT 500
            """,
            cat,
        )
        print(f"[{cat}] {len(rows)}건 처리 중...")

        for row in rows:
            result = await process_fact_for_graph(
                fact_id=str(row["id"]),
                category=row["category"],
                subject=row["subject"] or "",
                detail=row["detail"] or "",
                project=row["project"],
            )
            total_entities += result["entities"]
            total_relations += result["relations"]

    await conn.close()

    final_conn = await asyncpg.connect(os.getenv("DATABASE_URL"))
    e_count = await final_conn.fetchval("SELECT COUNT(*) FROM kg_entities")
    r_count = await final_conn.fetchval("SELECT COUNT(*) FROM kg_relations")
    await final_conn.close()

    await close_pool()

    print(f"\n=== 시드 완료 ===")
    print(f"처리: 엔티티 {total_entities}건, 관계 {total_relations}건")
    print(f"DB: kg_entities {e_count}건, kg_relations {r_count}건")


if __name__ == "__main__":
    asyncio.run(seed())
