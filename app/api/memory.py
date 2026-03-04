"""
AADS Memory API — go100_user_memory 협업 엔드포인트
T-038: 매니저 간 협업 API 4개 엔드포인트

엔드포인트:
  POST /memory/log          — 메모리 기록 (bridge.py 기존 사용)
  GET  /memory/search       — 메모리 검색
  GET  /memory/ceo-decisions — CEO 결정사항 조회
  POST /memory/cross-message — 매니저 간 교차 메시지 전송
  GET  /memory/inbox/{agent_id} — 수신함 조회
"""
from fastapi import APIRouter, HTTPException, Header, Request
from typing import Optional, Dict, Any
from pydantic import BaseModel
import os, json, hmac, asyncpg, logging
from datetime import datetime, timezone, timedelta
from app.config import Settings

logger = logging.getLogger(__name__)
settings = Settings()
router = APIRouter()

MONITOR_KEY = os.getenv("AADS_MONITOR_KEY", "")

# ─── 중요도 매핑 (message_type → importance) ─────────────────────────────
MSG_IMPORTANCE = {
    "alert":      9.0,
    "handover":   8.0,
    "request":    7.5,
    "discussion": 6.5,
    "notify":     6.0,
}

# ─── 인증 ─────────────────────────────────────────────────────────────────
def _verify_key(x_monitor_key: str = None) -> bool:
    if not MONITOR_KEY:
        raise HTTPException(503, "Monitor key not configured")
    if not x_monitor_key or not hmac.compare_digest(x_monitor_key, MONITOR_KEY):
        raise HTTPException(401, "Invalid monitor key")
    return True

# ─── DB 커넥션 헬퍼 ──────────────────────────────────────────────────────
async def _get_conn():
    """asyncpg 커넥션 생성 (pool 없이 단건)"""
    return await asyncpg.connect(dsn=settings.DATABASE_URL)

# ─── Pydantic 모델 ────────────────────────────────────────────────────────
class MemoryLogRequest(BaseModel):
    user_id: int = 2
    memory_type: str
    content: Dict[str, Any]
    importance: float = 5.0
    expires_at: Optional[str] = None

class CrossMessageRequest(BaseModel):
    from_agent: str
    to_agent: str
    message_type: str  # alert | handover | request | discussion | notify
    topic: str
    body: str
    requires_response: bool = False

# ─── POST /memory/log ─────────────────────────────────────────────────────
@router.post("/memory/log")
async def memory_log(
    req: MemoryLogRequest,
    request: Request,
    x_monitor_key: Optional[str] = Header(None),
):
    """go100_user_memory에 메모리 기록 (bridge.py 호환)"""
    _verify_key(x_monitor_key)
    conn = await _get_conn()
    try:
        expires_at = None
        if req.expires_at:
            try:
                expires_at = datetime.fromisoformat(req.expires_at.replace("Z", "+00:00"))
            except Exception:
                expires_at = None
        row = await conn.fetchrow(
            """
            INSERT INTO go100_user_memory
              (user_id, memory_type, content, importance, expires_at)
            VALUES ($1, $2, $3::jsonb, $4, $5)
            RETURNING id, created_at
            """,
            req.user_id,
            req.memory_type,
            json.dumps(req.content),
            req.importance,
            expires_at,
        )
        return {
            "status": "ok",
            "saved": f"go100_user_memory/{row['id']}",
            "id": row['id'],
            "created_at": str(row['created_at']),
        }
    except Exception as e:
        logger.error(f"memory_log error: {e}")
        raise HTTPException(500, f"DB error: {e}")
    finally:
        await conn.close()

# ─── GET /memory/search ───────────────────────────────────────────────────
@router.get("/memory/search")
async def memory_search(
    agent_id: Optional[str] = None,
    memory_type: Optional[str] = None,
    keyword: Optional[str] = None,
    min_importance: float = 0.0,
    days: int = 7,
    limit: int = 20,
    x_monitor_key: Optional[str] = Header(None),
):
    """
    go100_user_memory 검색.
    agent_id: content->>'agent_id' 일치
    memory_type: 정확 일치
    keyword: content::text ILIKE '%keyword%'
    min_importance: >= 값
    days: 최근 N일
    limit: 최대 건수 (기본 20)
    """
    # GET: 인증 불필요 (T-038-FIX: 매니저 공개 조회)
    conn = await _get_conn()
    try:
        params = [2]  # $1 user_id=2
        idx = 2
        where = ["user_id = $1", f"importance >= ${idx}"]
        params.append(min_importance)
        idx += 1

        where.append(f"created_at > NOW() - make_interval(days => ${idx})")
        params.append(days)
        idx += 1

        if agent_id:
            where.append(f"content->>'agent_id' = ${idx}")
            params.append(agent_id)
            idx += 1

        if memory_type:
            where.append(f"memory_type = ${idx}")
            params.append(memory_type)
            idx += 1

        if keyword:
            where.append(f"content::text ILIKE ${idx}")
            params.append(f"%{keyword}%")
            idx += 1

        sql = f"""
            SELECT id, user_id, memory_type, content, importance, expires_at, created_at
            FROM go100_user_memory
            WHERE {" AND ".join(where)}
            ORDER BY created_at DESC
            LIMIT ${idx}
        """
        params.append(limit)

        rows = await conn.fetch(sql, *params)
        data = []
        for r in rows:
            d = dict(r)
            if isinstance(d.get('content'), str):
                try:
                    d['content'] = json.loads(d['content'])
                except Exception:
                    pass
            d['created_at'] = str(d['created_at'])
            if d.get('expires_at'):
                d['expires_at'] = str(d['expires_at'])
            data.append(d)

        return {"status": "ok", "count": len(data), "data": data}
    except Exception as e:
        logger.error(f"memory_search error: {e}")
        raise HTTPException(500, f"DB error: {e}")
    finally:
        await conn.close()

# ─── GET /memory/ceo-decisions ────────────────────────────────────────────
@router.get("/memory/ceo-decisions")
async def memory_ceo_decisions(
    days: int = 30,
    x_monitor_key: Optional[str] = Header(None),
):
    """
    CEO 결정사항 조회:
      memory_type LIKE '%directive%' OR memory_type LIKE '%decision%' OR importance >= 8.5
      AND created_at > NOW() - INTERVAL '{days} days'
    """
    # GET: 인증 불필요 (T-038-FIX)
    conn = await _get_conn()
    try:
        rows = await conn.fetch(
            """
            SELECT id, user_id, memory_type, content, importance, expires_at, created_at
            FROM go100_user_memory
            WHERE user_id = 2
              AND (
                memory_type LIKE '%directive%'
                OR memory_type LIKE '%decision%'
                OR importance >= 8.5
              )
              AND created_at > NOW() - ($1 || ' days')::INTERVAL
            ORDER BY created_at DESC
            """,
            str(days),
        )
        data = []
        for r in rows:
            d = dict(r)
            if isinstance(d.get('content'), str):
                try:
                    d['content'] = json.loads(d['content'])
                except Exception:
                    pass
            d['created_at'] = str(d['created_at'])
            if d.get('expires_at'):
                d['expires_at'] = str(d['expires_at'])
            data.append(d)

        return {"status": "ok", "days": days, "count": len(data), "data": data}
    except Exception as e:
        logger.error(f"memory_ceo_decisions error: {e}")
        raise HTTPException(500, f"DB error: {e}")
    finally:
        await conn.close()

# ─── POST /memory/cross-message ───────────────────────────────────────────
@router.post("/memory/cross-message")
async def memory_cross_message(
    req: CrossMessageRequest,
    request: Request,
    x_monitor_key: Optional[str] = Header(None),
):
    """
    매니저 간 교차 메시지 전송.
    내부적으로 go100_user_memory에 직접 저장.
    memory_type: cross_msg_{from_agent}_{to_agent}
    """
    _verify_key(x_monitor_key)
    importance = MSG_IMPORTANCE.get(req.message_type.lower(), 6.0)

    kst = timezone(timedelta(hours=9))
    logged_at = datetime.now(kst).strftime("%Y-%m-%dT%H:%M:%S+09:00")

    memory_type = f"cross_msg_{req.from_agent}_{req.to_agent}"
    content = {
        "agent_id": req.from_agent,
        "event_type": "cross_message",
        "details": {
            "from_agent": req.from_agent,
            "to_agent": req.to_agent,
            "message_type": req.message_type,
            "topic": req.topic,
            "body": req.body,
            "requires_response": req.requires_response,
            "logged_at": logged_at,
        },
    }

    conn = await _get_conn()
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO go100_user_memory
              (user_id, memory_type, content, importance)
            VALUES ($1, $2, $3::jsonb, $4)
            RETURNING id, created_at
            """,
            2,
            memory_type,
            json.dumps(content),
            importance,
        )
        return {
            "status": "ok",
            "saved": f"go100_user_memory/{row['id']}",
            "id": row['id'],
            "memory_type": memory_type,
            "importance": importance,
            "created_at": str(row['created_at']),
        }
    except Exception as e:
        logger.error(f"cross_message error: {e}")
        raise HTTPException(500, f"DB error: {e}")
    finally:
        await conn.close()

# ─── GET /memory/inbox/{agent_id} ─────────────────────────────────────────
@router.get("/memory/inbox/{agent_id}")
async def memory_inbox(
    agent_id: str,
    unread_only: bool = False,
    days: int = 7,
    x_monitor_key: Optional[str] = Header(None),
):
    """
    특정 에이전트의 수신함 조회.
    memory_type LIKE '%_{agent_id}' OR memory_type LIKE 'broadcast_%'
    """
    # GET: 인증 불필요 (T-038-FIX)
    conn = await _get_conn()
    try:
        rows = await conn.fetch(
            """
            SELECT id, user_id, memory_type, content, importance, expires_at, created_at
            FROM go100_user_memory
            WHERE user_id = 2
              AND (
                memory_type LIKE $1
                OR memory_type LIKE 'broadcast_%'
              )
              AND created_at > NOW() - ($2 || ' days')::INTERVAL
            ORDER BY created_at DESC
            """,
            f"%_{agent_id}",
            str(days),
        )
        data = []
        for r in rows:
            d = dict(r)
            if isinstance(d.get('content'), str):
                try:
                    d['content'] = json.loads(d['content'])
                except Exception:
                    pass
            d['created_at'] = str(d['created_at'])
            if d.get('expires_at'):
                d['expires_at'] = str(d['expires_at'])
            data.append(d)

        return {
            "status": "ok",
            "agent_id": agent_id,
            "days": days,
            "count": len(data),
            "data": data,
        }
    except Exception as e:
        logger.error(f"memory_inbox error: {e}")
        raise HTTPException(500, f"DB error: {e}")
    finally:
        await conn.close()
