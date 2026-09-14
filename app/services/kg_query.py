"""지식 그래프 조회 — 화면과 채팅이 같이 쓴다.

2026-09-14. 벡터 검색은 "그 문장이 있는 문단" 을 준다. 그래프는 "그래서
뭐가 어떻게 됐나" 를 이어서 준다. 둘은 대체가 아니라 보완이다.

그래프는 `scripts/build_kg.py` 가 구조화된 기록(오류 사전·변경 원장·배포
원장·문서 언급)에서 만든다. 여기서는 읽기만 한다.

한 가지를 지킨다. **관계가 없으면 빈 결과를 준다.** 억지로 비슷한 것을
끌어오면 근거로 쓸 수 없게 된다 — 그래프의 값어치는 "확실히 이어져
있다" 는 것 하나다.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger(__name__)

# 질문에서 그래프로 찾아볼 만한 것: 파일 경로, 커밋 SHA, 오류 키
_FILE_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_/.-]{2,90}\.(?:py|tsx|ts|sh|sql|md)")
_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
_ERRKEY_RE = re.compile(r"\b[a-z_]+\.[a-z_]{3,60}\b")

_RELATION_LABEL = {
    "modifies": "고침",
    "deployed_in": "배포됨",
    "documents": "설명함",
    "resolved_by": "이 커밋으로 해결",
    "affects": "영향 받음",
    "caused": "원인",
    "depends_on": "의존",
    "related_to": "관련",
}


def relation_label(rel: str) -> str:
    return _RELATION_LABEL.get(rel, rel)


async def find_nodes(query: str, limit: int = 8) -> List[Dict[str, Any]]:
    """이름으로 노드를 찾는다. 정확히 같은 것을 먼저."""
    from app.core.db_pool import get_pool

    q = (query or "").strip()
    if len(q) < 2:
        return []
    try:
        rows = await get_pool().fetch(
            """
            SELECT id, entity_type, name, coalesce(description, '') AS description,
                   coalesce(project, '') AS project,
                   (SELECT count(*) FROM kg_relations r
                     WHERE r.source_entity_id = e.id OR r.target_entity_id = e.id) AS degree
            FROM kg_entities e
            WHERE name = $1 OR name ILIKE '%' || $1 || '%'
            ORDER BY (name = $1) DESC, degree DESC, length(name) ASC
            LIMIT $2
            """,
            q, max(1, limit),
        )
    except Exception as exc:
        logger.debug("kg_find_failed", error=str(exc))
        return []
    return [dict(r) for r in rows]


async def neighbors(entity_id: int, limit: int = 40) -> Dict[str, List[Dict[str, Any]]]:
    """한 노드에서 나가는 선과 들어오는 선."""
    from app.core.db_pool import get_pool

    pool = get_pool()
    try:
        out = await pool.fetch(
            """
            SELECT r.relation_type, e.id, e.entity_type, e.name,
                   coalesce(e.description, '') AS description,
                   coalesce(r.evidence, '') AS evidence, r.weight
            FROM kg_relations r JOIN kg_entities e ON e.id = r.target_entity_id
            WHERE r.source_entity_id = $1
            ORDER BY r.weight DESC, r.relation_type LIMIT $2
            """,
            entity_id, limit,
        )
        inc = await pool.fetch(
            """
            SELECT r.relation_type, e.id, e.entity_type, e.name,
                   coalesce(e.description, '') AS description,
                   coalesce(r.evidence, '') AS evidence, r.weight
            FROM kg_relations r JOIN kg_entities e ON e.id = r.source_entity_id
            WHERE r.target_entity_id = $1
            ORDER BY r.weight DESC, r.relation_type LIMIT $2
            """,
            entity_id, limit,
        )
    except Exception as exc:
        logger.debug("kg_neighbors_failed", error=str(exc))
        return {"outgoing": [], "incoming": []}
    return {
        "outgoing": [dict(r) | {"label": relation_label(r["relation_type"])} for r in out],
        "incoming": [dict(r) | {"label": relation_label(r["relation_type"])} for r in inc],
    }


def extract_candidates(text: str, limit: int = 6) -> List[str]:
    """질문에서 그래프로 찾아볼 이름을 뽑는다.

    아무 단어나 넣으면 엉뚱한 노드가 걸린다. 파일 경로·커밋 SHA·오류 키처럼
    **모양이 분명한 것만** 본다.
    """
    text = text or ""
    found: List[str] = []
    for rx in (_FILE_RE, _SHA_RE, _ERRKEY_RE):
        for m in rx.finditer(text):
            v = m.group(0)
            if v not in found:
                found.append(v)
            if len(found) >= limit:
                return found
    return found


async def context_for_question(question: str, *, max_nodes: int = 2) -> str:
    """채팅에 붙일 그래프 근거. 관계가 없으면 빈 문자열.

    억지로 비슷한 것을 끌어오지 않는다 — 근거로 쓸 수 없게 된다.
    """
    names = extract_candidates(question)
    if not names:
        return ""

    blocks: List[str] = []
    for name in names[:max_nodes]:
        nodes = await find_nodes(name, limit=1)
        if not nodes:
            continue
        node = nodes[0]
        rel = await neighbors(int(node["id"]), limit=12)
        lines: List[str] = []
        for r in rel["outgoing"]:
            lines.append(f"  - {relation_label(r['relation_type'])} → [{r['entity_type']}] {r['name']}")
        for r in rel["incoming"]:
            lines.append(f"  - [{r['entity_type']}] {r['name']} ← {relation_label(r['relation_type'])}")
        if not lines:
            continue
        blocks.append(f"[{node['entity_type']}] {node['name']}\n" + "\n".join(lines[:12]))

    if not blocks:
        return ""
    return (
        "<knowledge_graph>\n"
        "## 확인된 연결 (기계 기록 기반 — 추측 아님)\n"
        + "\n\n".join(blocks)
        + "\n</knowledge_graph>"
    )


async def list_nodes(
    entity_type: Optional[str] = None,
    *,
    limit: int = 50,
    offset: int = 0,
    q: Optional[str] = None,
) -> Dict[str, Any]:
    """둘러볼 목록. 연결이 많은 것부터.

    2026-09-14 — 처음에는 검색창만 뒀는데, **뭘 쳐야 할지 모르면 못 쓴다.**
    연결이 많은 노드가 곧 "여기서 시작하면 볼 게 많은 곳" 이다.

    연결 0인 노드는 빼지 않는다. 파일 3,041개 중 대부분이 아직 연결이
    없는데, 그걸 숨기면 "그래프에 다 들어있다" 고 착각하게 된다.
    """
    from app.core.db_pool import get_pool

    where = ["TRUE"]
    args: List[Any] = []
    if entity_type:
        args.append(entity_type)
        where.append(f"e.entity_type = ${len(args)}")
    if q and len(q.strip()) >= 2:
        args.append(q.strip())
        where.append(f"e.name ILIKE '%' || ${len(args)} || '%'")
    args.extend([max(1, min(limit, 200)), max(0, offset)])

    sql = f"""
        SELECT e.id, e.entity_type, e.name, coalesce(e.description,'') AS description,
               coalesce(e.project,'') AS project,
               (SELECT count(*) FROM kg_relations r
                 WHERE r.source_entity_id = e.id OR r.target_entity_id = e.id) AS degree
        FROM kg_entities e
        WHERE {' AND '.join(where)}
        ORDER BY degree DESC, e.name ASC
        LIMIT ${len(args) - 1} OFFSET ${len(args)}
    """
    try:
        pool = get_pool()
        rows = await pool.fetch(sql, *args)
        total = await pool.fetchval(
            f"SELECT count(*) FROM kg_entities e WHERE {' AND '.join(where)}",
            *args[:-2],
        )
    except Exception as exc:
        logger.debug("kg_list_failed", error=str(exc))
        return {"items": [], "total": 0}
    return {
        "items": [
            {"id": int(r["id"]), "type": r["entity_type"], "name": r["name"],
             "description": r["description"], "project": r["project"],
             "degree": int(r["degree"])}
            for r in rows
        ],
        "total": int(total or 0),
    }


async def stats() -> Dict[str, Any]:
    from app.core.db_pool import get_pool

    pool = get_pool()
    try:
        nodes = await pool.fetch(
            "SELECT entity_type, count(*) AS n FROM kg_entities GROUP BY 1 ORDER BY 2 DESC"
        )
        rels = await pool.fetch(
            "SELECT relation_type, count(*) AS n FROM kg_relations GROUP BY 1 ORDER BY 2 DESC"
        )
    except Exception:
        return {"nodes": [], "relations": [], "total_nodes": 0, "total_relations": 0}
    return {
        "nodes": [{"type": r["entity_type"], "count": int(r["n"])} for r in nodes],
        "relations": [{"type": r["relation_type"], "label": relation_label(r["relation_type"]),
                       "count": int(r["n"])} for r in rels],
        "total_nodes": sum(int(r["n"]) for r in nodes),
        "total_relations": sum(int(r["n"]) for r in rels),
    }
