"""
AADS Conversations API - 대화창 저장 내용 조회
데이터 소스: system_memory 테이블의 conversation:* 카테고리
"""
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from fastapi import APIRouter, Query
from dateutil.parser import isoparse

from app.memory.store import memory_store

KST = timezone(timedelta(hours=9))
CHUNK_KEY_PATTERN = r"^(.+)_([0-9]+)of([0-9]+)$"
CHUNK_KEY_REGEX = re.compile(CHUNK_KEY_PATTERN)
CHUNK_KEY_SQL_PATTERN = r"^(.+)_([0-9]+)of([0-9]+)$"
CHAT_MESSAGES_TRGM_EXTENSION_SQL = "CREATE EXTENSION IF NOT EXISTS pg_trgm"
CHAT_MESSAGES_TRGM_INDEX_SQL = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_chat_messages_content_gin "
    "ON chat_messages USING gin (content gin_trgm_ops)"
)


def _to_kst_str(dt_or_str: Any) -> Optional[str]:
    """datetime 또는 문자열을 KST 포맷으로 변환 (T-085)"""
    if not dt_or_str:
        return None
    if isinstance(dt_or_str, datetime):
        dt = dt_or_str if dt_or_str.tzinfo else dt_or_str.replace(tzinfo=timezone.utc)
    else:
        try:
            dt = isoparse(str(dt_or_str))
        except (TypeError, ValueError):
            return str(dt_or_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KST).strftime("%Y-%m-%dT%H:%M:%S+09:00")

router = APIRouter(prefix="/conversations", tags=["conversations"])

# T-089: 필수 채널 정의
REQUIRED_CHANNELS = [
    {"name": "AADS", "category": "conversation:aads"},
    {"name": "KIS", "category": "conversation:kis"},
    {"name": "SALES", "category": "conversation:sales"},
    {"name": "ShortFlow", "category": "conversation:sf"},
    {"name": "GO100", "category": "conversation:go100"},
    {"name": "NewTalk", "category": "conversation:newtalk"},
    {"name": "NAS", "category": "conversation:nas"},
    {"name": "통합지휘소", "category": "cross_msg"},
]

# channel name ↔ DB category 매핑
CHANNEL_MAP = {
    "aads": "conversation:aads",
    "kis": "conversation:kis",
    "sales": "conversation:sales",
    "sf": "conversation:sf",
    "shortflow": "conversation:sf",
    "ntv2": "conversation:ntv2",
    "newtalk": "conversation:newtalk",
    "go100": "conversation:go100",
    "nas": "conversation:nas",
}

CHANNEL_DISPLAY = {
    "aads": "AADS",
    "kis": "KIS",
    "sales": "SALES",
    "sf": "ShortFlow",
    "shortflow": "ShortFlow",
    "ntv2": "NewTalk",
    "newtalk": "NewTalk",
    "go100": "GO100",
    "nas": "NAS",
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


def _decode_memory_value(raw_value: Any) -> dict:
    if isinstance(raw_value, dict):
        return raw_value
    if isinstance(raw_value, str):
        return json.loads(raw_value)
    return json.loads(str(raw_value))


def _extract_chunk_meta(key: str) -> tuple[str, int, int]:
    match = CHUNK_KEY_REGEX.match(key)
    if not match:
        return key, 1, 1
    return match.group(1), int(match.group(2)), int(match.group(3))


def _build_conversation_where(
    *,
    project: Optional[str] = None,
    channel: Optional[str] = None,
    keyword: Optional[str] = None,
) -> tuple[str, list[Any]]:
    clauses = ["category LIKE 'conversation:%'"]
    params: list[Any] = []

    if project:
        params.append(f"conversation:{project.lower()}")
        clauses.append(f"category = ${len(params)}")

    if channel and channel.upper() != "ALL":
        params.append(_channel_to_category(channel))
        clauses.append(f"category = ${len(params)}")

    if keyword:
        params.append(f"%{keyword}%")
        clauses.append(f"value::text ILIKE ${len(params)}")

    return " WHERE " + " AND ".join(clauses), params


def _append_order_limit_offset(
    sql: str,
    params: Sequence[Any],
    *,
    order_by: str,
    limit: int,
    offset: int = 0,
) -> tuple[str, list[Any]]:
    paged_params = list(params)
    limit_idx = len(paged_params) + 1
    offset_idx = limit_idx + 1
    paged_params.extend([limit, offset])
    paged_sql = (
        f"{sql} ORDER BY {order_by} LIMIT ${limit_idx} OFFSET ${offset_idx}"
    )
    return paged_sql, paged_params


def _build_conversation_select_query(
    select_clause: str,
    *,
    order_by: str,
    limit: int,
    offset: int = 0,
    project: Optional[str] = None,
    channel: Optional[str] = None,
    keyword: Optional[str] = None,
) -> tuple[str, list[Any]]:
    where_sql, params = _build_conversation_where(
        project=project,
        channel=channel,
        keyword=keyword,
    )
    return _append_order_limit_offset(
        f"{select_clause}{where_sql}",
        params,
        order_by=order_by,
        limit=limit,
        offset=offset,
    )


def _build_conversation_count_query(
    *,
    project: Optional[str] = None,
    channel: Optional[str] = None,
    keyword: Optional[str] = None,
) -> tuple[str, list[Any]]:
    where_sql, params = _build_conversation_where(
        project=project,
        channel=channel,
        keyword=keyword,
    )
    return f"SELECT COUNT(*) FROM system_memory{where_sql}", params


def _chunked_messages_count_query() -> str:
    return f"""
        WITH parsed AS (
            SELECT key, regexp_match(key, '{CHUNK_KEY_SQL_PATTERN}') AS chunk_match
            FROM system_memory
            WHERE category = $1
        )
        SELECT COUNT(DISTINCT COALESCE(chunk_match[1], key))
        FROM parsed
    """


def _chunked_messages_page_query() -> str:
    return f"""
        WITH parsed AS (
            SELECT
                id,
                category,
                key,
                value,
                created_at,
                regexp_match(key, '{CHUNK_KEY_SQL_PATTERN}') AS chunk_match
            FROM system_memory
            WHERE category = $1
        ),
        normalized AS (
            SELECT
                id,
                category,
                key,
                value,
                created_at,
                COALESCE(chunk_match[1], key) AS base_key,
                COALESCE((chunk_match[2])::int, 1) AS chunk_idx,
                COALESCE((chunk_match[3])::int, 1) AS total_chunks
            FROM parsed
        ),
        ranked AS (
            SELECT
                id,
                category,
                key,
                value,
                created_at,
                base_key,
                chunk_idx,
                total_chunks,
                ROW_NUMBER() OVER (
                    PARTITION BY base_key
                    ORDER BY chunk_idx, created_at, id
                ) AS chunk_row_number,
                MAX(created_at) OVER (PARTITION BY base_key) AS base_created_at
            FROM normalized
        ),
        paged_base_keys AS (
            SELECT
                base_key,
                MAX(base_created_at) AS base_created_at
            FROM ranked
            GROUP BY base_key
            ORDER BY base_created_at DESC, base_key DESC
            LIMIT $2 OFFSET $3
        )
        SELECT
            r.id,
            r.category,
            r.key,
            r.value,
            r.created_at,
            r.base_key,
            r.chunk_idx,
            r.total_chunks,
            r.chunk_row_number,
            r.base_created_at
        FROM ranked r
        JOIN paged_base_keys pb ON pb.base_key = r.base_key
        ORDER BY pb.base_created_at DESC, pb.base_key DESC, r.chunk_row_number ASC
    """


def _merge_chunk_parts(parts: list[dict[str, Any]]) -> dict[str, Any]:
    first = parts[0]
    snapshot = "".join(part["content"].get("snapshot", "") for part in parts)
    total_chunks = max(part["total"] for part in parts)
    merged_count = len(parts)
    created_at = first.get("base_created_at", first["created_at"])
    return {
        "id": first["id"],
        "key": first["base_key"],
        "channel": _category_to_channel(first["category"]),
        "project": first["content"].get("project", ""),
        "source": first["content"].get("source", "genspark_bridge"),
        "snapshot": snapshot,
        "chunk": "1/1" if total_chunks == 1 else f"merged({merged_count})",
        "created_at": str(created_at),
    }


def _merge_chunks(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    청크 분할된 레코드를 합쳐서 하나의 메시지로 반환.
    key 패턴: chat_1234_1of2, chat_1234_2of2 → chat_1234
    """
    merged_rows: list[dict[str, Any]] = []
    current_base: Optional[str] = None
    current_parts: list[dict[str, Any]] = []

    for row in rows:
        base_key = row.get("base_key")
        chunk_idx = row.get("chunk_idx")
        total_chunks = row.get("total_chunks")
        if base_key is None or chunk_idx is None or total_chunks is None:
            base_key, chunk_idx, total_chunks = _extract_chunk_meta(row["key"])
        key = row["key"]
        content = _decode_memory_value(row["value"])

        if current_base is not None and base_key != current_base:
            merged_rows.append(_merge_chunk_parts(current_parts))
            current_parts = []

        current_base = base_key
        current_parts.append({
            "idx": chunk_idx,
            "total": total_chunks,
            "content": content,
            "created_at": row["created_at"],
            "base_created_at": row.get("base_created_at", row["created_at"]),
            "id": row["id"],
            "category": row["category"],
            "key": key,
            "base_key": base_key,
        })

    if current_parts:
        merged_rows.append(_merge_chunk_parts(current_parts))

    return merged_rows


async def ensure_chat_messages_search_index(conn) -> None:
    await conn.execute(CHAT_MESSAGES_TRGM_EXTENSION_SQL)
    await conn.execute(CHAT_MESSAGES_TRGM_INDEX_SQL)


@router.get("/channels")
async def list_channels():
    """
    채널(프로젝트)별 대화 건수 및 마지막 활동 시간.
    T-089: REQUIRED_CHANNELS 기반으로 누락 채널은 count=0, status="수집 미설정" 추가.
    통합지휘소: system_memory WHERE category LIKE 'cross_msg_%' 집계.
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
        # 통합지휘소: cross_msg_ 집계
        cross_msg_row = await conn.fetchrow("""
            SELECT COUNT(*) as count, MAX(created_at) as last_message
            FROM system_memory
            WHERE category LIKE 'cross_msg_%'
        """)

        # DB에서 실제 데이터 있는 채널 조회
        db_channels: dict = {}
        for row in rows:
            ch_name = _category_to_channel(row["category"])
            db_channels[ch_name.upper()] = {
                "name": ch_name,
                "category": row["category"],
                "count": row["count"],
                "last_message": _to_kst_str(row["last_message"]),
            }

        # 통합지휘소 추가
        cross_count = int(cross_msg_row["count"]) if cross_msg_row else 0
        cross_last = _to_kst_str(cross_msg_row["last_message"]) if cross_msg_row else None
        db_channels["통합지휘소"] = {
            "name": "통합지휘소",
            "category": "cross_msg",
            "count": cross_count,
            "last_message": cross_last,
        }

        # REQUIRED_CHANNELS 순서대로 응답 구성 (누락 채널은 수집 미설정 추가)
        channels = []
        for rc in REQUIRED_CHANNELS:
            key = rc["name"].upper() if rc["name"] != "통합지휘소" else "통합지휘소"
            if key in db_channels:
                channels.append(db_channels[key])
            else:
                channels.append({
                    "name": rc["name"],
                    "category": rc["category"],
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
            _chunked_messages_count_query(),
            category,
        )
        rows = await conn.fetch(
            _chunked_messages_page_query(),
            category,
            limit,
            offset,
        )
        messages = _merge_chunks([dict(r) for r in rows])
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
        search_query, params = _build_conversation_select_query(
            "SELECT id, category, key, value, created_at FROM system_memory",
            order_by="created_at DESC, key DESC",
            limit=limit,
            offset=0,
            channel=channel,
            keyword=q,
        )
        rows = await conn.fetch(search_query, *params)

        results = []
        for row in rows:
            content = _decode_memory_value(row["value"])
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
        list_query, list_params = _build_conversation_select_query(
            "SELECT key, value, category, updated_at FROM system_memory",
            order_by="updated_at DESC, key DESC",
            limit=limit,
            offset=offset,
            project=project,
            keyword=keyword,
        )
        rows = await conn.fetch(list_query, *list_params)

        count_query, count_params = _build_conversation_count_query(
            project=project,
            keyword=keyword,
        )
        total = await conn.fetchval(count_query, *count_params)

        conversations = []
        for row in rows:
            val = _decode_memory_value(row["value"])
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
