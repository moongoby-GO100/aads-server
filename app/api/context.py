"""
AADS Context API - System Memory CRUD + HANDOVER 자동생성
인증: X-Monitor-Key (읽기/쓰기) 또는 JWT (읽기/쓰기)
"""
from fastapi import APIRouter, HTTPException, Header, Depends, Request
from typing import Optional, Dict, List, Any
from pydantic import BaseModel
from app.memory.store import memory_store
import hmac, os, json, time
from collections import defaultdict

router = APIRouter()

MONITOR_KEY = os.getenv("AADS_MONITOR_KEY", "")

# --- Rate Limiting (POST /context/system: 분당 30회/IP) ---
_rate_limit_store: Dict[str, List[float]] = defaultdict(list)
POST_RATE_LIMIT = 30  # 분당 최대 요청 수
RATE_LIMIT_WINDOW = 60.0  # 초

def check_rate_limit(request: Request):
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW
    # 만료된 타임스탬프 제거
    _rate_limit_store[ip] = [t for t in _rate_limit_store[ip] if t > window_start]
    if len(_rate_limit_store[ip]) >= POST_RATE_LIMIT:
        raise HTTPException(429, "Too Many Requests: POST /context/system 분당 30회 제한")
    _rate_limit_store[ip].append(now)

def verify_monitor_key(x_monitor_key: str = Header(None)):
    if not MONITOR_KEY:
        raise HTTPException(503, "Monitor key not configured")
    if not x_monitor_key or not hmac.compare_digest(x_monitor_key, MONITOR_KEY):
        raise HTTPException(401, "Invalid monitor key")
    return True

class SystemMemoryRequest(BaseModel):
    category: str
    key: str
    value: Dict[str, Any]
    version: Optional[str] = None

# --- 읽기 엔드포인트 (Monitor Key) ---
@router.get("/context/system")
async def get_all_system_memory(auth: bool = Depends(verify_monitor_key)):
    """전체 시스템 메모리 조회 (카테고리별 그룹)"""
    data = await memory_store.get_all_system()
    return {"status": "ok", "categories": list(data.keys()), "data": data}

@router.get("/context/system/{category}")
async def get_system_category(category: str, auth: bool = Depends(verify_monitor_key)):
    """특정 카테고리 시스템 메모리 조회"""
    data = await memory_store.get_system_by_category(category)
    if not data:
        raise HTTPException(404, f"Category '{category}' not found")
    return {"status": "ok", "category": category, "count": len(data), "data": data}

@router.get("/context/system/{category}/{key}")
async def get_system_entry(category: str, key: str, auth: bool = Depends(verify_monitor_key)):
    """특정 키의 시스템 메모리 조회"""
    data = await memory_store.get_system(category, key)
    if not data:
        raise HTTPException(404, f"Key '{category}/{key}' not found")
    return {"status": "ok", "data": data}

# --- 쓰기 엔드포인트 (Monitor Key 인증 필수) ---
@router.post("/context/system")
async def put_system_memory(
    req: SystemMemoryRequest,
    request: Request,
    auth: bool = Depends(verify_monitor_key),
    _rate: None = Depends(check_rate_limit),
):
    """시스템 메모리 저장/업데이트 (Monitor Key 인증 필수)"""
    await memory_store.put_system(
        category=req.category,
        key=req.key,
        value=req.value,
        version=req.version,
        updated_by="agent"
    )
    # 저장 확인: 저장된 값 반환
    saved_data = await memory_store.get_system(req.category, req.key)
    return {"status": "ok", "saved": f"{req.category}/{req.key}", "data": saved_data}

# --- HANDOVER 자동생성 ---
@router.get("/context/handover")
async def generate_handover(auth: bool = Depends(verify_monitor_key)):
    """DB에서 HANDOVER.md 형식 자동생성"""
    data = await memory_store.get_all_system()
    md = _build_handover_markdown(data)
    return {"status": "ok", "format": "markdown", "content": md}

def _build_handover_markdown(data: Dict) -> str:
    lines = []
    lines.append("# AADS HANDOVER (Auto-Generated from System Memory)")
    lines.append(f"")

    category_titles = {
        "status": "## 1. System Status",
        "repos": "## 2. Repositories & URLs",
        "architecture": "## 3. Architecture Decisions",
        "agents": "## 4. Agent Configuration",
        "phase": "## 5. Phase Progress",
        "costs": "## 6. Cost Tracking",
        "ceo_directives": "## 7. CEO Directives Summary",
        "pending": "## 8. Pending Items",
        "history": "## 9. Version History"
    }

    for cat, title in category_titles.items():
        if cat in data:
            lines.append(title)
            for entry in data[cat]:
                k = entry.get('key', '')
                v = entry.get('value', {})
                if isinstance(v, str):
                    try:
                        v = json.loads(v)
                    except:
                        pass
                lines.append(f"### {k}")
                if isinstance(v, dict):
                    for vk, vv in v.items():
                        lines.append(f"- **{vk}**: {vv}")
                else:
                    lines.append(f"{v}")
                lines.append("")

    # 미분류 카테고리
    for cat, entries in data.items():
        if cat not in category_titles:
            lines.append(f"## {cat}")
            for entry in entries:
                lines.append(f"### {entry.get('key','')}")
                lines.append(f"{json.dumps(entry.get('value',{}), ensure_ascii=False, indent=2)}")
                lines.append("")

    return "\n".join(lines)

# --- 프로젝트 메모리 조회 ---
@router.get("/context/projects/{project_id}/memories")
async def get_project_memories(project_id: str, memory_type: Optional[str] = None, auth: bool = Depends(verify_monitor_key)):
    data = await memory_store.get_project_memories(project_id, memory_type)
    return {"status": "ok", "project_id": project_id, "count": len(data), "data": data}

# --- 경험 메모리 조회 ---
@router.get("/context/experiences")
async def get_experiences(experience_type: Optional[str] = None, domain: Optional[str] = None, auth: bool = Depends(verify_monitor_key)):
    async with memory_store.pool.acquire() as conn:
        query = "SELECT id, experience_type, domain, tags, content, access_count, rif_score, created_at FROM experience_memory WHERE 1=1"
        params = []
        idx = 1
        if experience_type:
            query += f" AND experience_type=${idx}"
            params.append(experience_type)
            idx += 1
        if domain:
            query += f" AND domain=${idx}"
            params.append(domain)
            idx += 1
        query += " ORDER BY rif_score DESC, created_at DESC LIMIT 50"
        rows = await conn.fetch(query, *params)
    return {"status": "ok", "count": len(rows), "data": [dict(r) for r in rows]}
