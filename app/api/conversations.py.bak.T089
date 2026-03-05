"""
AADS Conversations API - 대화창 저장 내용 조회
데이터 소스: system_memory 테이블의 conversation:* 카테고리
"""
from fastapi import APIRouter, Query
from typing import Optional
import json
import re
from datetime import datetime, timezone, timedelta
from app.memory.store import memory_store

KST = timezone(timedelta(hours=9))


def _to_kst_str(dt_or_str) -> str:
    """datetime 또는 문자열을 KST 포맷으로 변환 (T-085)"""
    if not dt_or_str:
        return None
    if isinstance(dt_or_str, datetime):
        dt = dt_or_str if dt_or_str.tzinfo else dt_or_str.replace(tzinfo=timezone.utc)
        return dt.astimezone(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00")
    s = str(dt_or_str)
    try:
        s_clean = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s_clean)
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00")
    except Exception:
        return s

router = APIRouter(prefix="/conversations", tags=["conversations"])

# channel name ↔ DB category 매핑
CHANNEL_MAP = {
    "aads": "conversation:aads",
    "kis": "conversation:kis",
    "sales": "conversation:sales",
    "sf": "conversation:sf",
    "shortflow": "conversation:sf",
    "ntv2": "conversation:ntv2",
    "newtalk": "conversation:ntv2",
}

CHANNEL_DISPLAY = {
    "aads": "AADS",
    "kis": "KIS",
    "sales": "SALES",
    "sf": "ShortFlow",
    "ntv2": "NewTalk",
}


def _category_to_channel(category: str) -> str:
    """conversation:kis → KIS"""
    proj = category.replace("conversation:", "")
    return CHANNEL_DISPLAY.get(proj, proj.upper())


def _channel_to_category(channel: str) -> Optional[str]:
    ch = channel.lower()
    if ch in CHANNEL_MAP:
        return CHANNEL_MAP[ch]
    return f"conversation:{ch}"


def _merge_chunks(rows: list) -> list:
    """
    청크 분할된 레코드를 합쳐서 하나의 메시지로 반환.
    key 패턴: chat_1234_1of2, chat_1234_2of2 → chat_1234
    """
    groups: dict = {}
    order: list = []
    chunk_pattern = re.compile(r"^(.+)_(\d+)of(\d+)$")

    for row in rows:
        key = row["key"]
        m = chunk_pattern.match(key)
        if m:
            base = m.group(1)
            idx = int(m.group(2))
            total_chunks = int(m.group(3))
        else:
            base = key
            idx = 1
            total_chunks = 1

        if base not in groups:
            groups[base] = []
            order.append(base)
        val = row["value"]
        content = val if isinstance(val, dict) else json.loads(val)
        groups[base].append({
            "idx": idx,
            "total": total_chunks,
            "content": content,
            "created_at": row["created_at"],
            "id": row["id"],
            "category": row["category"],
            "key": key,
            "base_key": base,
        })

    result = []
    for base in order:
        parts = sorted(groups[base], key=lambda x: x["idx"])
        snapshot = "".join(p["content"].get("snapshot", "") for p in parts)
        first = parts[0]
        result.append({
            "id": first["id"],
            "key": base,
            "channel": _category_to_channel(first["category"]),
            "project": first["content"].get("project", ""),
            "source": first["content"].get("source", "genspark_bridge"),
            "snapshot": snapshot,
            "chunk": f"1/1" if len(parts) == 1 else f"merged({len(parts)})",
            "created_at": str(first["created_at"]),
        })
    return result


@router.get("/channels")
async def list_channels():
    """
    채널(프로젝트)별 대화 건수 및 마지막 활동 시간.
    Response: {"channels": [{"name":"KIS","count":N,"last_message":"..."},...]}
    GO100 채널은 system_memory에 데이터가 없으면 "수집 미설정" 상태로 항상 포함 (T-081)
    """
    async with memory_store.pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT category, COUNT(*) as count,
                   MAX(created_at) as last_message
            FROM system_memory
            WHERE category LIKE 'conversation:%'
            GROUP BY category
            ORDER BY MAX(created_at) DESC
        """)
        channels = []
        present = set()
        for row in rows:
            ch_name = _category_to_channel(row["category"])
            present.add(ch_name.upper())
            channels.append({
                "name": ch_name,
                "category": row["category"],
                "count": row["count"],
                # T-085: last_message를 KST ISO 형식으로 통일
                "last_message": _to_kst_str(row["last_message"]),
            })
        # T-081: GO100, T-086: NewTalk/NAS — 데이터 없으면 "수집 미설정" 으로 항상 표시
        for missing_name, missing_cat in [
            ("GO100", "conversation:go100"),
            ("NewTalk", "conversation:ntv2"),
            ("NAS", "conversation:nas"),
        ]:
            if missing_name.upper() not in present:
                channels.append({
                    "name": missing_name,
                    "category": missing_cat,
                    "count": 0,
                    "last_message": None,
                    "status": "수집 미설정",
                })
        return {"channels": channels}


@router.get("/messages")
async def get_messages(
    channel: str = Query(..., description="채널명: KIS, SALES, AADS, ShortFlow, NewTalk"),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
):
    """
    특정 채널의 대화 메시지 목록. 청크 분할된 메시지는 하나로 합쳐서 반환.
    Response: {"channel":"KIS","total":N,"messages":[...]}
    """
    category = _channel_to_category(channel)
    async with memory_store.pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM system_memory WHERE category = $1", category
        )
        rows = await conn.fetch(
            "SELECT id, category, key, value, created_at FROM system_memory "
            "WHERE category = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
            category, limit + 50, offset  # 청크 병합을 위해 여유분 포함
        )
        messages = _merge_chunks([dict(r) for r in rows])
        # limit 재적용 (청크 병합 후)
        messages = messages[:limit]
        return {
            "channel": channel.upper(),
            "total": total,
            "limit": limit,
            "offset": offset,
            "messages": messages,
        }


@router.get("/search")
async def search_conversations(
    q: str = Query(..., description="검색 키워드"),
    channel: str = Query("ALL", description="채널 필터: ALL 또는 KIS/SALES/AADS/ShortFlow"),
    limit: int = Query(20, le=100),
):
    """
    키워드로 대화 내용 검색.
    Response: {"results":[{"id":N,"channel":"KIS","snippet":"...","created_at":"..."}]}
    """
    async with memory_store.pool.acquire() as conn:
        if channel.upper() == "ALL":
            rows = await conn.fetch(
                "SELECT id, category, key, value, created_at FROM system_memory "
                "WHERE category LIKE 'conversation:%' AND value::text ILIKE $1 "
                "ORDER BY created_at DESC LIMIT $2",
                f"%{q}%", limit
            )
        else:
            cat = _channel_to_category(channel)
            rows = await conn.fetch(
                "SELECT id, category, key, value, created_at FROM system_memory "
                "WHERE category = $1 AND value::text ILIKE $2 "
                "ORDER BY created_at DESC LIMIT $3",
                cat, f"%{q}%", limit
            )

        results = []
        for row in rows:
            val = row["value"]
            content = val if isinstance(val, dict) else json.loads(val)
            snapshot = content.get("snapshot", "")
            # 검색어 주변 스니펫 추출
            idx = snapshot.lower().find(q.lower())
            if idx >= 0:
                start = max(0, idx - 80)
                end = min(len(snapshot), idx + 150)
                snippet = ("..." if start > 0 else "") + snapshot[start:end] + ("..." if end < len(snapshot) else "")
            else:
                snippet = snapshot[:200]
            results.append({
                "id": row["id"],
                "channel": _category_to_channel(row["category"]),
                "key": row["key"],
                "snippet": snippet,
                "created_at": str(row["created_at"]),
            })
        return {"query": q, "channel": channel.upper(), "results": results}


@router.get("")
async def list_conversations(
    project: Optional[str] = Query(None, description="프로젝트 필터: aads, kis, sf, sales, nas, ntv2, go100"),
    keyword: Optional[str] = Query(None, description="키워드 검색"),
    limit: int = Query(50, le=200),
    offset: int = Query(0)
):
    """
    저장된 대화 내용 조회.
    데이터 소스: system_memory 테이블의 conversation:* 카테고리
    """
    async with memory_store.pool.acquire() as conn:
        base_query = "SELECT key, value, category, updated_at FROM system_memory WHERE category LIKE 'conversation:%'"
        params = []
        idx = 1

        if project:
            base_query += f" AND category = ${idx}"
            params.append(f"conversation:{project}")
            idx += 1

        if keyword:
            base_query += f" AND value::text ILIKE ${idx}"
            params.append(f"%{keyword}%")
            idx += 1

        base_query += f" ORDER BY updated_at DESC LIMIT ${idx} OFFSET ${idx + 1}"
        params.extend([limit, offset])

        rows = await conn.fetch(base_query, *params)

        # 총 건수
        count_query = "SELECT COUNT(*) FROM system_memory WHERE category LIKE 'conversation:%'"
        count_params = []
        cidx = 1
        if project:
            count_query += f" AND category = ${cidx}"
            count_params.append(f"conversation:{project}")
            cidx += 1
        if keyword:
            count_query += f" AND value::text ILIKE ${cidx}"
            count_params.append(f"%{keyword}%")

        total = await conn.fetchval(count_query, *count_params)

        conversations = []
        for row in rows:
            raw = row["value"]
            val = raw if isinstance(raw, dict) else json.loads(raw)
            conversations.append({
                "id": row["key"],
                "project": row["category"].replace("conversation:", ""),
                "source": val.get("source", "unknown"),
                "snapshot": val.get("snapshot", "")[:500],  # 미리보기 500자
                "full_text": val.get("snapshot", ""),
                "logged_at": val.get("logged_at", ""),
                "char_count": val.get("char_count", 0),
                "updated_at": str(row["updated_at"])
            })

        return {
            "status": "ok",
            "total": total,
            "limit": limit,
            "offset": offset,
            "conversations": conversations
        }


@router.get("/stats")
async def conversation_stats():
    """프로젝트별 대화 건수 통계"""
    async with memory_store.pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT category, COUNT(*) as count,
                   MAX(updated_at) as last_updated
            FROM system_memory
            WHERE category LIKE 'conversation:%'
            GROUP BY category
            ORDER BY count DESC
        """)

        today_rows = await conn.fetch("""
            SELECT category, COUNT(*) as count
            FROM system_memory
            WHERE category LIKE 'conversation:%'
              AND created_at >= CURRENT_DATE
            GROUP BY category
        """)
        today_map = {r["category"]: r["count"] for r in today_rows}

        stats = []
        total = 0
        today_total = 0
        for row in rows:
            count = row["count"]
            today_count = today_map.get(row["category"], 0)
            total += count
            today_total += today_count
            stats.append({
                "project": row["category"].replace("conversation:", ""),
                "name": _category_to_channel(row["category"]),
                "count": count,
                "today": today_count,
                "last_updated": str(row["last_updated"])
            })

        return {
            "status": "ok",
            "total": total,
            "total_conversations": total,
            "today": today_total,
            "projects": stats,
            "channels": [
                {
                    "name": s["name"],
                    "total": s["count"],
                    "today": s["today"],
                    "last_active": s["last_updated"],
                }
                for s in stats
            ],
        }
