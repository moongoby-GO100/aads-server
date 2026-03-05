"""
AADS Conversations Microservice — standalone
대화 내용 조회 전용 API (conversations:* 카테고리)
PORT: 8101
"""
from fastapi import FastAPI, Query
from typing import Optional
import asyncpg, os, json

# Read DB URL from the production .env if available
_env_file = os.path.join(os.path.dirname(__file__), ".env.prod")
if not os.path.exists(_env_file):
    _env_file = os.path.join(os.path.dirname(__file__), ".env")

if os.path.exists(_env_file):
    with open(_env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                if k.strip() not in os.environ:
                    os.environ[k.strip()] = v.strip()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://aads:aads_dev_local@127.0.0.1:5433/aads"
)
# Normalize: container hostname → host IP
DATABASE_URL = DATABASE_URL.replace(
    "@aads-postgres:5432", "@127.0.0.1:5433"
).replace(
    "@postgres:5432", "@127.0.0.1:5433"
)

app = FastAPI(title="AADS Conversations", version="1.0.0")


async def _get_conn():
    return await asyncpg.connect(DATABASE_URL)


@app.get("/api/v1/conversations")
async def list_conversations(
    project: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0)
):
    conn = await _get_conn()
    try:
        base = "SELECT key, value, category, importance, updated_at FROM system_memory WHERE category LIKE 'conversation:%'"
        params = []
        idx = 1

        if project:
            base += f" AND category = ${idx}"
            params.append(f"conversation:{project}")
            idx += 1

        if keyword:
            base += f" AND value::text ILIKE ${idx}"
            params.append(f"%{keyword}%")
            idx += 1

        base += f" ORDER BY updated_at DESC LIMIT ${idx} OFFSET ${idx+1}"
        params.extend([limit, offset])

        rows = await conn.fetch(base, *params)

        count_q = "SELECT COUNT(*) FROM system_memory WHERE category LIKE 'conversation:%'"
        count_p = []
        cidx = 1
        if project:
            count_q += f" AND category = ${cidx}"
            count_p.append(f"conversation:{project}")
            cidx += 1
        if keyword:
            count_q += f" AND value::text ILIKE ${cidx}"
            count_p.append(f"%{keyword}%")

        total = await conn.fetchval(count_q, *count_p)

        conversations = []
        for row in rows:
            raw = row["value"]
            val = raw if isinstance(raw, dict) else json.loads(raw)
            conversations.append({
                "id": row["key"],
                "project": row["category"].replace("conversation:", ""),
                "source": val.get("source", "unknown"),
                "snapshot": val.get("snapshot", "")[:500],
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
    finally:
        await conn.close()


@app.get("/api/v1/conversations/stats")
async def conversation_stats():
    conn = await _get_conn()
    try:
        rows = await conn.fetch("""
            SELECT category, COUNT(*) as count, MAX(updated_at) as last_updated
            FROM system_memory
            WHERE category LIKE 'conversation:%'
            GROUP BY category
            ORDER BY count DESC
        """)

        stats = []
        total = 0
        for row in rows:
            c = row["count"]
            total += c
            stats.append({
                "project": row["category"].replace("conversation:", ""),
                "count": c,
                "last_updated": str(row["last_updated"])
            })

        return {
            "status": "ok",
            "total_conversations": total,
            "projects": stats
        }
    finally:
        await conn.close()
