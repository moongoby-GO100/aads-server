"""브레인스토밍 시각화 시스템 서비스."""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Any, Optional

from app.core.anthropic_client import call_llm_with_fallback
from app.core.db_pool import get_pool

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "claude-haiku-4-5-20251001"
_ROOT_X = 400.0
_ROOT_Y = 50.0
_LEVEL_GAP_Y = 150.0
_MIN_HORIZONTAL_GAP = 260.0


def _normalize_json_text(text: str) -> str:
    candidate = (text or "").strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    return candidate


def _extract_json_object(text: str, default: Any) -> Any:
    candidate = _normalize_json_text(text)
    if not candidate:
        return default
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start:end + 1])
            except json.JSONDecodeError:
                pass
        start = candidate.find("[")
        end = candidate.rfind("]")
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start:end + 1])
            except json.JSONDecodeError:
                pass
    return default


def _to_plain_dict(row: Any) -> dict[str, Any]:
    item = dict(row)
    return {
        "id": str(item["id"]),
        "session_id": str(item["session_id"]) if item.get("session_id") else None,
        "parent_id": str(item["parent_id"]) if item.get("parent_id") else None,
        "node_type": item.get("node_type"),
        "label": item.get("label"),
        "content": item.get("content"),
        "agent_role": item.get("agent_role"),
        "position_x": float(item.get("position_x") or 0),
        "position_y": float(item.get("position_y") or 0),
        "metadata": item.get("metadata") or {},
        "cost": float(item.get("cost") or 0),
        "created_at": item.get("created_at").isoformat() if item.get("created_at") else None,
    }


def _to_session_dict(row: Any) -> dict[str, Any]:
    item = dict(row)
    return {
        "id": str(item["id"]),
        "title": item["title"],
        "topic": item["topic"],
        "status": item["status"],
        "config": item.get("config") or {},
        "summary": item.get("summary"),
        "total_cost": float(item.get("total_cost") or 0),
        "created_at": item.get("created_at").isoformat() if item.get("created_at") else None,
        "updated_at": item.get("updated_at").isoformat() if item.get("updated_at") else None,
    }


async def _fetch_session(conn, session_id: str):
    row = await conn.fetchrow(
        """
        SELECT id, title, topic, status, config, summary, total_cost, created_at, updated_at
        FROM braming_sessions
        WHERE id = $1::uuid
        """,
        session_id,
    )
    if not row:
        raise ValueError(f"브레인스토밍 세션을 찾을 수 없습니다: {session_id}")
    return row


async def _fetch_node(conn, session_id: str, node_id: str):
    row = await conn.fetchrow(
        """
        SELECT id, session_id, parent_id, node_type, label, content, agent_role,
               position_x, position_y, metadata, cost, created_at
        FROM braming_nodes
        WHERE session_id = $1::uuid AND id = $2::uuid
        """,
        session_id,
        node_id,
    )
    if not row:
        raise ValueError(f"브레인스토밍 노드를 찾을 수 없습니다: {node_id}")
    return row


async def _fetch_root_node(conn, session_id: str):
    row = await conn.fetchrow(
        """
        SELECT id, session_id, parent_id, node_type, label, content, agent_role,
               position_x, position_y, metadata, cost, created_at
        FROM braming_nodes
        WHERE session_id = $1::uuid AND node_type = 'topic'
        ORDER BY created_at ASC
        LIMIT 1
        """,
        session_id,
    )
    if not row:
        raise ValueError(f"루트 topic 노드를 찾을 수 없습니다: {session_id}")
    return row


async def _insert_node(
    conn,
    *,
    session_id: str,
    parent_id: Optional[str],
    node_type: str,
    label: str,
    content: str,
    agent_role: Optional[str] = None,
    position_x: float = 0,
    position_y: float = 0,
    metadata: Optional[dict[str, Any]] = None,
    cost: float = 0,
):
    return await conn.fetchrow(
        """
        INSERT INTO braming_nodes (
            session_id, parent_id, node_type, label, content, agent_role,
            position_x, position_y, metadata, cost
        )
        VALUES (
            $1::uuid, $2::uuid, $3, $4, $5, $6,
            $7, $8, $9::jsonb, $10
        )
        RETURNING id, session_id, parent_id, node_type, label, content, agent_role,
                  position_x, position_y, metadata, cost, created_at
        """,
        session_id,
        parent_id,
        node_type,
        label,
        content,
        agent_role,
        position_x,
        position_y,
        json.dumps(metadata or {}, ensure_ascii=False),
        cost,
    )


def _estimate_cost(text: Optional[str], multiplier: float = 0.00002) -> float:
    return round(len((text or "").strip()) * multiplier, 6)


async def _call_json_llm(prompt: str, system: str, fallback: Any) -> Any:
    result = await call_llm_with_fallback(
        prompt=prompt,
        model=_DEFAULT_MODEL,
        max_tokens=1400,
        system=system,
    )
    if not result:
        return fallback
    return _extract_json_object(result, fallback)


def _build_tree_text(nodes: list[dict[str, Any]]) -> str:
    by_parent: dict[Optional[str], list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        by_parent[node["parent_id"]].append(node)

    for items in by_parent.values():
        items.sort(key=lambda item: item["created_at"] or "")

    def walk(parent_id: Optional[str], depth: int) -> list[str]:
        lines: list[str] = []
        prefix = "  " * depth
        for node in by_parent.get(parent_id, []):
            node_line = f"{prefix}- [{node['node_type']}] {node['label']}"
            if node.get("content"):
                node_line += f": {node['content'][:500]}"
            lines.append(node_line)
            lines.extend(walk(node["id"], depth + 1))
        return lines

    return "\n".join(walk(None, 0))


def _compute_layout(nodes: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    # 프론트엔드 dagre가 시각화 레이아웃을 담당한다.
    # 백엔드는 DB 저장용 기본 좌표만 반환한다.
    positions: dict[str, dict[str, float]] = {}
    for index, node in enumerate(nodes):
        positions[node["id"]] = {
            "x": node.get("position_x") or _ROOT_X,
            "y": node.get("position_y") or (_ROOT_Y + index * 50),
        }
    return positions


async def create_braming_session(topic: str, config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """새 브레인스토밍 세션 생성."""
    pool = get_pool()
    session_title = (topic or "").strip()
    if not session_title:
        raise ValueError("topic은 비어 있을 수 없습니다.")

    async with pool.acquire() as conn:
        session_row = await conn.fetchrow(
            """
            INSERT INTO braming_sessions (title, topic, config)
            VALUES ($1, $2, $3::jsonb)
            RETURNING id, title, topic, status, config, summary, total_cost, created_at, updated_at
            """,
            session_title[:200],
            session_title,
            json.dumps(config or {}, ensure_ascii=False),
        )
        root_row = await _insert_node(
            conn,
            session_id=str(session_row["id"]),
            parent_id=None,
            node_type="topic",
            label=session_title[:200],
            content=session_title,
            agent_role="ceo",
            position_x=_ROOT_X,
            position_y=_ROOT_Y,
            metadata={"root": True},
        )

    return {"session": _to_session_dict(session_row), "root_node": _to_plain_dict(root_row)}


async def generate_perspectives(session_id: str, topic: str) -> list[dict[str, Any]]:
    """주제를 분석해 관점 노드 생성."""
    pool = get_pool()
    async with pool.acquire() as conn:
        session = await _fetch_session(conn, session_id)
        root_node = await _fetch_root_node(conn, session_id)
        existing_rows = await conn.fetch(
            """
            SELECT id, session_id, parent_id, node_type, label, content, agent_role,
                   position_x, position_y, metadata, cost, created_at
            FROM braming_nodes
            WHERE session_id = $1::uuid AND node_type = 'perspective'
            ORDER BY created_at ASC
            """,
            session_id,
        )
        if existing_rows:
            return [_to_plain_dict(row) for row in existing_rows]

    prompt = f"""주제에 대해 서로 다른 관점 3~5개를 설계하세요.

주제: {topic or session['topic']}

반드시 아래 JSON 배열만 반환하세요.
[
  {{
    "label": "관점명",
    "content": "이 관점이 중요하고 무엇을 볼지 2~3문장",
    "agent_role": "strategist"
  }}
]"""
    system = "당신은 브레인스토밍 퍼실리테이터입니다. 중복 없는 관점만 제시하고 JSON만 반환하세요."
    generated = await _call_json_llm(prompt, system, [])
    if not isinstance(generated, list):
        generated = []

    cleaned_items: list[dict[str, str]] = []
    seen_labels: set[str] = set()
    for item in generated:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        content = str(item.get("content") or "").strip()
        agent_role = str(item.get("agent_role") or "strategist").strip() or "strategist"
        if not label or label.lower() in seen_labels:
            continue
        seen_labels.add(label.lower())
        cleaned_items.append({
            "label": label[:120],
            "content": content[:1200] or label,
            "agent_role": agent_role[:50],
        })

    if not cleaned_items:
        cleaned_items = [
            {"label": "사용자 가치", "content": "최종 사용자가 실제로 얻는 가치와 채택 장벽을 분석합니다.", "agent_role": "strategist"},
            {"label": "실행 가능성", "content": "구현 난이도, 운영 비용, 필요한 리소스를 검토합니다.", "agent_role": "architect"},
            {"label": "리스크", "content": "실패 시나리오, 품질 저하, 조직적 병목을 점검합니다.", "agent_role": "qa"},
        ]

    created_nodes: list[dict[str, Any]] = []
    async with pool.acquire() as conn:
        for index, item in enumerate(cleaned_items):
            x = _ROOT_X + ((index - ((len(cleaned_items) - 1) / 2)) * _MIN_HORIZONTAL_GAP)
            row = await _insert_node(
                conn,
                session_id=session_id,
                parent_id=str(root_node["id"]),
                node_type="perspective",
                label=item["label"],
                content=item["content"],
                agent_role=item["agent_role"],
                position_x=x,
                position_y=_ROOT_Y + _LEVEL_GAP_Y,
                metadata={"generated_from": "perspectives"},
                cost=_estimate_cost(item["content"]),
            )
            created_nodes.append(_to_plain_dict(row))
        await conn.execute(
            """
            UPDATE braming_sessions
            SET total_cost = total_cost + $2, updated_at = NOW()
            WHERE id = $1::uuid
            """,
            session_id,
            sum(item["cost"] for item in created_nodes),
        )
    return created_nodes


async def generate_ideas(session_id: str, perspective_node_id: str) -> list[dict[str, Any]]:
    """관점 기준으로 아이디어 노드 생성."""
    pool = get_pool()
    async with pool.acquire() as conn:
        perspective = await _fetch_node(conn, session_id, perspective_node_id)
        if perspective["node_type"] != "perspective":
            raise ValueError("idea 생성 대상은 perspective 노드여야 합니다.")
        existing_rows = await conn.fetch(
            """
            SELECT id, session_id, parent_id, node_type, label, content, agent_role,
                   position_x, position_y, metadata, cost, created_at
            FROM braming_nodes
            WHERE session_id = $1::uuid AND parent_id = $2::uuid AND node_type = 'idea'
            ORDER BY created_at ASC
            """,
            session_id,
            perspective_node_id,
        )
        if existing_rows:
            return [_to_plain_dict(row) for row in existing_rows]

    prompt = f"""다음 관점에서 실행 가능한 브레인스토밍 아이디어 2~3개를 제안하세요.

관점명: {perspective['label']}
관점 설명: {perspective['content']}

아래 JSON 배열만 반환하세요.
[
  {{
    "label": "짧은 아이디어 제목",
    "content": "구체적인 아이디어 설명 2~4문장"
  }}
]"""
    system = "당신은 실무형 브레인스토밍 코치입니다. 추상적인 미사여구 없이 실행 가능한 아이디어만 JSON으로 반환하세요."
    generated = await _call_json_llm(prompt, system, [])
    if not isinstance(generated, list):
        generated = []

    items: list[dict[str, str]] = []
    for item in generated:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        content = str(item.get("content") or "").strip()
        if label and content:
            items.append({"label": label[:120], "content": content[:1500]})

    if not items:
        items = [
            {"label": f"{perspective['label']} 핵심 아이디어 1", "content": perspective["content"] or perspective["label"]},
            {"label": f"{perspective['label']} 핵심 아이디어 2", "content": f"{perspective['label']} 관점에서 실행 단계를 더 세분화합니다."},
        ]

    created_nodes: list[dict[str, Any]] = []
    async with pool.acquire() as conn:
        base_x = float(perspective["position_x"] or _ROOT_X)
        base_y = float(perspective["position_y"] or (_ROOT_Y + _LEVEL_GAP_Y))
        for index, item in enumerate(items):
            x = base_x + ((index - ((len(items) - 1) / 2)) * _MIN_HORIZONTAL_GAP)
            row = await _insert_node(
                conn,
                session_id=session_id,
                parent_id=perspective_node_id,
                node_type="idea",
                label=item["label"],
                content=item["content"],
                agent_role=perspective["agent_role"] or "strategist",
                position_x=x,
                position_y=base_y + _LEVEL_GAP_Y,
                metadata={"generated_from": "ideas", "perspective": perspective["label"]},
                cost=_estimate_cost(item["content"]),
            )
            created_nodes.append(_to_plain_dict(row))
        await conn.execute(
            """
            UPDATE braming_sessions
            SET total_cost = total_cost + $2, updated_at = NOW()
            WHERE id = $1::uuid
            """,
            session_id,
            sum(item["cost"] for item in created_nodes),
        )
    return created_nodes


async def generate_counter(session_id: str, target_node_id: str) -> dict[str, Any]:
    """아이디어에 대한 반박 노드 생성."""
    pool = get_pool()
    async with pool.acquire() as conn:
        target = await _fetch_node(conn, session_id, target_node_id)
        sibling_context_rows = await conn.fetch(
            """
            SELECT label, content
            FROM braming_nodes
            WHERE session_id = $1::uuid AND node_type = 'perspective'
              AND id <> COALESCE($2::uuid, '00000000-0000-0000-0000-000000000000'::uuid)
            ORDER BY created_at ASC
            LIMIT 3
            """,
            session_id,
            target["parent_id"],
        )

    other_context = "\n".join(
        f"- {row['label']}: {(row['content'] or '')[:250]}" for row in sibling_context_rows
    )
    prompt = f"""다음 아이디어에 대한 강한 반박 또는 맹점 지적을 작성하세요.

대상 노드: [{target['node_type']}] {target['label']}
내용: {target['content']}

다른 관점 참고:
{other_context or '- 추가 참고 관점 없음'}

아래 JSON 객체만 반환하세요.
{{
  "label": "반박 제목",
  "content": "핵심 반박 내용 2~4문장"
}}"""
    system = "당신은 비판적 리뷰어입니다. 감정적 표현 없이 약점과 반례를 명확히 지적하고 JSON만 반환하세요."
    generated = await _call_json_llm(prompt, system, {})
    label = str(generated.get("label") or "핵심 반박").strip()[:120]
    content = str(generated.get("content") or "이 아이디어는 추가 검증이 필요합니다.").strip()[:1500]

    async with pool.acquire() as conn:
        row = await _insert_node(
            conn,
            session_id=session_id,
            parent_id=target_node_id,
            node_type="counter",
            label=label,
            content=content,
            agent_role="critic",
            position_x=float(target["position_x"] or _ROOT_X),
            position_y=float(target["position_y"] or _ROOT_Y) + _LEVEL_GAP_Y,
            metadata={"target_node_id": target_node_id},
            cost=_estimate_cost(content),
        )
        await conn.execute(
            """
            UPDATE braming_sessions
            SET total_cost = total_cost + $2, updated_at = NOW()
            WHERE id = $1::uuid
            """,
            session_id,
            float(row["cost"] or 0),
        )
    return _to_plain_dict(row)


async def expand_node(session_id: str, node_id: str) -> list[dict[str, Any]]:
    """선택된 노드를 확장."""
    pool = get_pool()
    async with pool.acquire() as conn:
        node = await _fetch_node(conn, session_id, node_id)
        await conn.execute(
            """
            UPDATE braming_nodes
            SET metadata = COALESCE(metadata, '{}'::jsonb) || $2::jsonb
            WHERE id = $1::uuid
            """,
            node_id,
            json.dumps({"picked": True}, ensure_ascii=False),
        )

    prompt = f"""다음 선택 노드를 더 깊게 확장하세요.

노드 유형: {node['node_type']}
제목: {node['label']}
내용: {node['content']}

아래 JSON 배열만 반환하세요.
[
  {{
    "label": "확장 아이디어 제목",
    "content": "구체적 확장 설명 2~4문장"
  }}
]"""
    system = "당신은 CEO가 선택한 아이디어를 실행 단계로 확장하는 전략가입니다. 2~3개의 하위 확장 아이디어만 JSON으로 반환하세요."
    generated = await _call_json_llm(prompt, system, [])
    if not isinstance(generated, list):
        generated = []

    items: list[dict[str, str]] = []
    for item in generated:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        content = str(item.get("content") or "").strip()
        if label and content:
            items.append({"label": label[:120], "content": content[:1500]})

    if not items:
        items = [
            {"label": f"{node['label']} 확장 1", "content": "핵심 실행 단계를 세분화합니다."},
            {"label": f"{node['label']} 확장 2", "content": "실험과 검증 방법을 구체화합니다."},
        ]

    created_nodes: list[dict[str, Any]] = []
    async with pool.acquire() as conn:
        base_x = float(node["position_x"] or _ROOT_X)
        base_y = float(node["position_y"] or _ROOT_Y)
        for index, item in enumerate(items):
            x = base_x + ((index - ((len(items) - 1) / 2)) * _MIN_HORIZONTAL_GAP)
            row = await _insert_node(
                conn,
                session_id=session_id,
                parent_id=node_id,
                node_type="expansion",
                label=item["label"],
                content=item["content"],
                agent_role=node["agent_role"] or "strategist",
                position_x=x,
                position_y=base_y + _LEVEL_GAP_Y,
                metadata={"generated_from": "expand", "picked_from": node_id},
                cost=_estimate_cost(item["content"]),
            )
            created_nodes.append(_to_plain_dict(row))
        await conn.execute(
            """
            UPDATE braming_sessions
            SET total_cost = total_cost + $2, updated_at = NOW()
            WHERE id = $1::uuid
            """,
            session_id,
            sum(item["cost"] for item in created_nodes),
        )
    return created_nodes


async def synthesize_session(session_id: str) -> dict[str, Any]:
    """세션 전체를 종합하여 synthesis 노드 생성."""
    pool = get_pool()
    async with pool.acquire() as conn:
        session = await _fetch_session(conn, session_id)
        node_rows = await conn.fetch(
            """
            SELECT id, session_id, parent_id, node_type, label, content, agent_role,
                   position_x, position_y, metadata, cost, created_at
            FROM braming_nodes
            WHERE session_id = $1::uuid
            ORDER BY created_at ASC
            """,
            session_id,
        )
        nodes = [_to_plain_dict(row) for row in node_rows]

    tree_text = _build_tree_text(nodes)
    prompt = f"""다음 브레인스토밍 세션을 종합하세요.

주제: {session['topic']}

노드 트리:
{tree_text}

아래 JSON 객체만 반환하세요.
{{
  "label": "최종 종합",
  "summary": "세션 전체 종합 요약",
  "content": "CEO가 바로 판단할 수 있는 형태의 종합 분석"
}}"""
    system = "당신은 CEO 보고용 종합 분석가입니다. 중복을 제거하고 실행 우선순위가 드러나게 정리하세요. JSON만 반환하세요."
    generated = await _call_json_llm(prompt, system, {})
    label = str(generated.get("label") or "최종 종합").strip()[:120]
    summary = str(generated.get("summary") or "").strip()[:3000]
    content = str(generated.get("content") or summary or "세션 종합 결과를 생성하지 못했습니다.").strip()[:5000]
    node_cost = _estimate_cost(content)

    async with pool.acquire() as conn:
        root_node = await _fetch_root_node(conn, session_id)
        row = await _insert_node(
            conn,
            session_id=session_id,
            parent_id=str(root_node["id"]),
            node_type="synthesis",
            label=label,
            content=content,
            agent_role="synthesizer",
            position_x=_ROOT_X,
            position_y=_ROOT_Y + _LEVEL_GAP_Y,
            metadata={"summary": summary},
            cost=node_cost,
        )
        await conn.execute(
            """
            UPDATE braming_sessions
            SET summary = $2,
                status = 'completed',
                total_cost = total_cost + $3,
                updated_at = NOW()
            WHERE id = $1::uuid
            """,
            session_id,
            summary or content[:1000],
            node_cost,
        )
    return {"session_id": session_id, "summary": summary or content[:1000], "node": _to_plain_dict(row)}


async def get_session_graph(session_id: str) -> dict[str, Any]:
    """React Flow 호환 그래프 반환."""
    pool = get_pool()
    async with pool.acquire() as conn:
        session_row = await _fetch_session(conn, session_id)
        node_rows = await conn.fetch(
            """
            SELECT id, session_id, parent_id, node_type, label, content, agent_role,
                   position_x, position_y, metadata, cost, created_at
            FROM braming_nodes
            WHERE session_id = $1::uuid
            ORDER BY created_at ASC
            """,
            session_id,
        )

    raw_nodes = [_to_plain_dict(row) for row in node_rows]
    positions = _compute_layout(raw_nodes)
    child_counts: dict[str, int] = defaultdict(int)
    for node in raw_nodes:
        if node["parent_id"]:
            child_counts[node["parent_id"]] += 1
    graph_nodes: list[dict[str, Any]] = []
    graph_edges: list[dict[str, Any]] = []
    for node in raw_nodes:
        position = positions.get(
            node["id"],
            {"x": node["position_x"] or _ROOT_X, "y": node["position_y"] or _ROOT_Y},
        )
        graph_nodes.append({
            "id": node["id"],
            "type": "default",
            "position": position,
            "data": {
                "label": node["label"],
                "content": node["content"],
                "nodeType": node["node_type"],
                "agentRole": node["agent_role"],
                "metadata": node["metadata"] or {},
                "cost": node["cost"],
                "createdAt": node["created_at"],
                "childCount": child_counts.get(node["id"], 0),
                "nodeId": node["id"],
            },
        })
        if node["parent_id"]:
            graph_edges.append({
                "id": f"{node['parent_id']}-{node['id']}",
                "source": node["parent_id"],
                "target": node["id"],
                "type": "smoothstep",
            })

    return {
        "session": _to_session_dict(session_row),
        "nodes": graph_nodes,
        "edges": graph_edges,
    }


async def list_sessions(limit: int = 20) -> list[dict[str, Any]]:
    """최근 세션 목록 반환."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, title, topic, status, config, summary, total_cost, created_at, updated_at
            FROM braming_sessions
            ORDER BY created_at DESC
            LIMIT $1
            """,
            limit,
        )
    return [_to_session_dict(row) for row in rows]
