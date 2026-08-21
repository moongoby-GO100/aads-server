"""
AADS P2: Knowledge Graph — 엔티티/관계 추출 + 그래프 기반 검색.

memory_facts에서 엔티티(서비스, 파일, 도구, 개념)와 관계를 추출하여
kg_entities / kg_relations 테이블에 저장.
memory_recall에서 그래프 기반 컨텍스트 검색에 활용.

엔티티 타입: service, file, tool, concept, project, table, error
관계 타입: uses, depends_on, modifies, caused, resolved_by, related_to
"""
from __future__ import annotations

import re
import uuid
import asyncio
import structlog
from typing import Any, Dict, List, Optional, Tuple

from app.core.project_config import normalize_project_label

logger = structlog.get_logger(__name__)


def _get_pool():
    from app.core.db_pool import get_pool
    return get_pool()


# ── 엔티티 추출 패턴 ──────────────────────────────────────────────────────────

_SERVICE_NAMES = {
    "aads-server", "aads-dashboard", "litellm", "postgres", "redis",
    "nginx", "supervisord", "telegram-bot", "watchdog", "bridge",
    "pipeline-runner", "claude-code", "shortflow", "newtalk",
}

_TOOL_NAMES = {
    "read_remote_file", "write_remote_file", "patch_remote_file",
    "run_remote_command", "query_database", "query_db",
    "pipeline_runner_submit", "pipeline_runner_status", "pipeline_runner_approve",
    "git_remote_push", "git_remote_commit", "git_remote_add",
    "search_naver", "gemini_grounding_search", "fetch_url",
    "browser_navigate", "browser_snapshot", "capture_screenshot",
    "fact_check", "run_agent_team", "run_debate",
    "execute_sandbox", "send_telegram", "schedule_task",
}

_PROJECT_NAMES = {"AADS", "KIS", "GO100", "SF", "NTV2", "NAS"}

_FILE_PATTERN = re.compile(
    r'(?:app/|scripts/|src/|docs/|tests/)'
    r'[\w/\-]+\.(?:py|ts|tsx|js|jsx|md|yml|yaml|sql|json|sh)',
)

_TABLE_PATTERN = re.compile(
    r'\b(?:memory_facts|ai_observations|ai_meta_memory|chat_messages|chat_sessions|'
    r'session_notes|experience_memory|procedural_memory|kg_entities|kg_relations|'
    r'directive_lifecycle|ceo_facts|project_memory|system_memory|'
    r'chat_artifacts|project_artifacts|go100_user_memory)\b'
)

_ERROR_PATTERN = re.compile(
    r'(?:Error|Exception|실패|오류|에러|차단|timeout|crash)[:：]\s*(.{10,80})',
    re.IGNORECASE,
)


def extract_entities(text: str, project: Optional[str] = None) -> List[Dict[str, str]]:
    """텍스트에서 엔티티를 패턴 기반으로 추출."""
    entities = []
    text_lower = text.lower()

    for svc in _SERVICE_NAMES:
        if svc in text_lower:
            entities.append({"type": "service", "name": svc})

    for tool in _TOOL_NAMES:
        if tool in text_lower:
            entities.append({"type": "tool", "name": tool})

    for proj in _PROJECT_NAMES:
        if proj in text.upper():
            entities.append({"type": "project", "name": proj})

    for match in _FILE_PATTERN.findall(text):
        entities.append({"type": "file", "name": match})

    for match in _TABLE_PATTERN.findall(text):
        entities.append({"type": "table", "name": match})

    for match in _ERROR_PATTERN.findall(text):
        err_name = match.strip()[:80]
        if len(err_name) > 10:
            entities.append({"type": "error", "name": err_name})

    seen = set()
    unique = []
    for e in entities:
        key = (e["type"], e["name"])
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return unique


def infer_relations(
    entities: List[Dict[str, str]],
    category: str,
) -> List[Tuple[Dict, Dict, str]]:
    """엔티티 쌍에서 관계를 추론. (source, target, relation_type) 반환."""
    relations = []
    if len(entities) < 2:
        return relations

    category_relation_map = {
        "file_change": "modifies",
        "error_resolution": "resolved_by",
        "error_pattern": "caused",
        "config_change": "modifies",
        "decision": "depends_on",
        "architecture_decision": "depends_on",
        "ceo_instruction": "related_to",
        "timeline_event": "related_to",
    }
    rel_type = category_relation_map.get(category, "related_to")

    for i, src in enumerate(entities):
        for tgt in entities[i + 1:]:
            if src["type"] == tgt["type"] and src["name"] == tgt["name"]:
                continue
            if src["type"] in ("project",) and tgt["type"] in ("project",):
                continue
            relations.append((src, tgt, rel_type))

    return relations[:10]


# ── DB 조작 ────────────────────────────────────────────────────────────────────

async def upsert_entity(
    conn,
    entity_type: str,
    name: str,
    project: Optional[str] = None,
    description: str = "",
) -> Optional[int]:
    """엔티티 UPSERT → id 반환."""
    project = normalize_project_label(project)
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO kg_entities (entity_type, name, project, description)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (entity_type, name)
            DO UPDATE SET
                mention_count = kg_entities.mention_count + 1,
                updated_at = NOW(),
                project = COALESCE(EXCLUDED.project, kg_entities.project)
            RETURNING id
            """,
            entity_type, name[:300], project, description[:500],
        )
        return row["id"] if row else None
    except Exception as e:
        logger.debug("kg_upsert_entity_error", error=str(e), name=name[:50])
        return None


async def upsert_relation(
    conn,
    source_id: int,
    target_id: int,
    relation_type: str,
    evidence: str = "",
    fact_id: Optional[str] = None,
    project: Optional[str] = None,
) -> Optional[int]:
    """관계 UPSERT → id 반환."""
    project = normalize_project_label(project)
    try:
        fact_ids = [uuid.UUID(fact_id)] if fact_id else []
        row = await conn.fetchrow(
            """
            INSERT INTO kg_relations (source_entity_id, target_entity_id, relation_type, evidence, fact_ids, project)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (source_entity_id, target_entity_id, relation_type)
            DO UPDATE SET
                weight = kg_relations.weight + 0.1,
                evidence = CASE WHEN LENGTH(EXCLUDED.evidence) > LENGTH(kg_relations.evidence)
                           THEN EXCLUDED.evidence ELSE kg_relations.evidence END,
                fact_ids = (
                    SELECT ARRAY(SELECT DISTINCT unnest(kg_relations.fact_ids || EXCLUDED.fact_ids))
                )
            RETURNING id
            """,
            source_id, target_id, relation_type,
            evidence[:300], fact_ids, project,
        )
        return row["id"] if row else None
    except Exception as e:
        logger.debug("kg_upsert_relation_error", error=str(e))
        return None


# ── 사실 → 그래프 처리 ────────────────────────────────────────────────────────

async def process_fact_for_graph(
    fact_id: str,
    category: str,
    subject: str,
    detail: str,
    project: Optional[str] = None,
) -> Dict[str, int]:
    """하나의 memory_fact를 엔티티/관계로 변환하여 그래프에 저장."""
    text = f"{subject} {detail}"
    entities = extract_entities(text, project)

    if not entities:
        return {"entities": 0, "relations": 0}

    entity_ids = {}
    async with _get_pool().acquire() as conn:
        for e in entities:
            eid = await upsert_entity(conn, e["type"], e["name"], project)
            if eid:
                entity_ids[(e["type"], e["name"])] = eid

        relations = infer_relations(entities, category)
        rel_count = 0
        for src, tgt, rel_type in relations:
            src_id = entity_ids.get((src["type"], src["name"]))
            tgt_id = entity_ids.get((tgt["type"], tgt["name"]))
            if src_id and tgt_id and src_id != tgt_id:
                rid = await upsert_relation(
                    conn, src_id, tgt_id, rel_type,
                    evidence=subject[:200], fact_id=fact_id, project=project,
                )
                if rid:
                    rel_count += 1

    return {"entities": len(entity_ids), "relations": rel_count}


# ── 그래프 검색 (memory_recall 연동) ──────────────────────────────────────────

async def query_graph_context(
    project: Optional[str] = None,
    top_k: int = 5,
) -> str:
    """프로젝트의 핵심 엔티티 허브 + 관계 요약을 텍스트로 반환.
    memory_recall의 _build_knowledge_graph_context()에서 호출."""
    try:
        async with _get_pool().acquire() as conn:
            if project:
                hub_entities = await conn.fetch(
                    """
                    SELECT e.id, e.entity_type, e.name, e.mention_count,
                           (SELECT COUNT(*) FROM kg_relations r
                            WHERE r.source_entity_id = e.id OR r.target_entity_id = e.id
                           ) AS rel_count
                    FROM kg_entities e
                    WHERE e.project = $1 OR e.project IS NULL
                    ORDER BY e.mention_count DESC, rel_count DESC
                    LIMIT $2
                    """,
                    normalize_project_label(project), top_k,
                )
            else:
                hub_entities = await conn.fetch(
                    """
                    SELECT e.id, e.entity_type, e.name, e.mention_count,
                           (SELECT COUNT(*) FROM kg_relations r
                            WHERE r.source_entity_id = e.id OR r.target_entity_id = e.id
                           ) AS rel_count
                    FROM kg_entities e
                    ORDER BY e.mention_count DESC
                    LIMIT $1
                    """,
                    top_k,
                )

            if not hub_entities:
                return ""

            lines = []
            hub_ids = [r["id"] for r in hub_entities]

            relations = await conn.fetch(
                """
                SELECT
                    s.name AS src_name, s.entity_type AS src_type,
                    t.name AS tgt_name, t.entity_type AS tgt_type,
                    r.relation_type, r.weight
                FROM kg_relations r
                JOIN kg_entities s ON s.id = r.source_entity_id
                JOIN kg_entities t ON t.id = r.target_entity_id
                WHERE r.source_entity_id = ANY($1::int[])
                   OR r.target_entity_id = ANY($1::int[])
                ORDER BY r.weight DESC
                LIMIT 15
                """,
                hub_ids,
            )

            for e in hub_entities:
                lines.append(
                    f"- [{e['entity_type']}] {e['name']} "
                    f"(언급 {e['mention_count']}회, 관계 {e['rel_count']}건)"
                )

            if relations:
                lines.append("연결:")
                for r in relations:
                    lines.append(
                        f"  {r['src_name']} —[{r['relation_type']}]→ {r['tgt_name']}"
                    )

            return "\n".join(lines)

    except Exception as e:
        logger.warning("kg_query_graph_context_failed", error=str(e))
        return ""


# ── 시맨틱 검색 (임베딩 기반) ──────────────────────────────────────────────────

async def search_entities_by_text(
    query_text: str,
    project: Optional[str] = None,
    top_k: int = 5,
) -> str:
    """쿼리 텍스트와 벡터 유사도가 높은 엔티티 + 1-hop 관계를 반환.
    엔티티 embedding이 없으면 mention_count 기반 fallback."""
    try:
        from app.services.chat_embedding_service import embed_texts
        embeddings = await embed_texts([query_text[:500]])
        if not embeddings or not embeddings[0]:
            return await query_graph_context(project=project, top_k=top_k)

        query_vec = str(embeddings[0])
        async with _get_pool().acquire() as conn:
            has_embeddings = await conn.fetchval(
                "SELECT COUNT(*) FROM kg_entities WHERE embedding IS NOT NULL"
            )
            if not has_embeddings:
                return await query_graph_context(project=project, top_k=top_k)

            if project:
                rows = await conn.fetch(
                    """
                    SELECT id, entity_type, name, mention_count,
                           1 - (embedding <=> $1::vector) AS similarity
                    FROM kg_entities
                    WHERE embedding IS NOT NULL
                      AND (project = $2 OR project IS NULL)
                    ORDER BY embedding <=> $1::vector
                    LIMIT $3
                    """,
                    query_vec, normalize_project_label(project), top_k,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, entity_type, name, mention_count,
                           1 - (embedding <=> $1::vector) AS similarity
                    FROM kg_entities
                    WHERE embedding IS NOT NULL
                    ORDER BY embedding <=> $1::vector
                    LIMIT $2
                    """,
                    query_vec, top_k,
                )

            if not rows:
                return await query_graph_context(project=project, top_k=top_k)

            lines = []
            hub_ids = [r["id"] for r in rows]

            relations = await conn.fetch(
                """
                SELECT s.name AS src_name, t.name AS tgt_name,
                       r.relation_type, r.weight
                FROM kg_relations r
                JOIN kg_entities s ON s.id = r.source_entity_id
                JOIN kg_entities t ON t.id = r.target_entity_id
                WHERE r.source_entity_id = ANY($1::int[])
                   OR r.target_entity_id = ANY($1::int[])
                ORDER BY r.weight DESC
                LIMIT 10
                """,
                hub_ids,
            )

            for e in rows:
                sim_pct = int(float(e["similarity"]) * 100)
                lines.append(
                    f"- [{e['entity_type']}] {e['name']} "
                    f"(유사도 {sim_pct}%, 언급 {e['mention_count']}회)"
                )

            if relations:
                lines.append("연결:")
                for r in relations:
                    lines.append(
                        f"  {r['src_name']} —[{r['relation_type']}]→ {r['tgt_name']}"
                    )

            return "\n".join(lines)

    except Exception as e:
        logger.warning("kg_semantic_search_failed", error=str(e))
        return await query_graph_context(project=project, top_k=top_k)


# ── 엔티티 임베딩 배치 생성 ──────────────────────────────────────────────────

async def embed_all_entities() -> Dict[str, int]:
    """embedding이 없는 모든 kg_entities에 임베딩 벡터를 생성."""
    try:
        from app.services.chat_embedding_service import embed_texts

        async with _get_pool().acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, entity_type, name, description
                FROM kg_entities
                WHERE embedding IS NULL
                ORDER BY mention_count DESC
                LIMIT 200
                """,
            )

            if not rows:
                return {"embedded": 0, "total": 0}

            texts = [
                f"{r['entity_type']}: {r['name']} — {r['description'] or ''}"[:200]
                for r in rows
            ]
            embeddings = await embed_texts(texts)

            updated = 0
            for row, emb in zip(rows, embeddings):
                if emb:
                    await conn.execute(
                        "UPDATE kg_entities SET embedding = $1 WHERE id = $2",
                        str(emb), row["id"],
                    )
                    updated += 1

            total = await conn.fetchval(
                "SELECT COUNT(*) FROM kg_entities WHERE embedding IS NOT NULL"
            )
            return {"embedded": updated, "total": total}

    except Exception as e:
        logger.warning("kg_embed_entities_failed", error=str(e))
        return {"embedded": 0, "total": 0, "error": str(e)}


# ── 통계 ──────────────────────────────────────────────────────────────────────

async def get_graph_stats() -> Dict[str, Any]:
    """지식그래프 통계."""
    try:
        async with _get_pool().acquire() as conn:
            row = await conn.fetchrow("""
                SELECT
                    (SELECT COUNT(*) FROM kg_entities) AS entity_count,
                    (SELECT COUNT(*) FROM kg_relations) AS relation_count,
                    (SELECT COUNT(DISTINCT entity_type) FROM kg_entities) AS type_count,
                    (SELECT COUNT(*) FROM kg_entities WHERE embedding IS NOT NULL) AS embedded_count
            """)
            return {
                "entities": row["entity_count"] if row else 0,
                "relations": row["relation_count"] if row else 0,
                "types": row["type_count"] if row else 0,
                "embedded": row["embedded_count"] if row else 0,
            }
    except Exception as e:
        logger.warning("kg_stats_failed", error=str(e))
        return {"entities": 0, "relations": 0, "types": 0, "embedded": 0}
