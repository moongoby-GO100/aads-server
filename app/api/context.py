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
    value: Optional[Dict[str, Any]] = None
    data: Optional[Dict[str, Any]] = None   # "data" 필드도 허용 (원격 에이전트 호환)
    version: Optional[str] = None

    def get_value(self) -> Dict[str, Any]:
        """value 또는 data 필드에서 실제 값 반환"""
        return self.value if self.value is not None else (self.data or {})

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
    value = req.get_value()
    await memory_store.put_system(
        category=req.category,
        key=req.key,
        value=value,
        version=req.version,
        updated_by="agent"
    )

    # T-090: task_result 또는 '완료'/'completed' 포함 시 project_tasks upsert
    task_upsert_result = None
    content_str_lower = json.dumps(value, ensure_ascii=False).lower()
    is_task_event = (
        value.get("message_type") == "task_result" or
        "완료" in content_str_lower or
        "completed" in content_str_lower or
        req.category.startswith("cross_msg_REMOTE_")
    )
    if is_task_event and value.get("task_id"):
        task_upsert_result = await _upsert_task_result(value, req.category)

    # 저장 확인: 저장된 값 반환
    saved_data = await memory_store.get_system(req.category, req.key)
    resp = {"status": "ok", "saved": f"{req.category}/{req.key}", "data": saved_data}
    if task_upsert_result is not None:
        resp["task_upsert"] = task_upsert_result
    return resp


def _normalize_task_id_for_db(task_id: str, project: str) -> str:
    """T-107: DB 저장 시 접두사 ID로 정규화 (AADS-095, KIS-168 등)"""
    PREFIX_MAP = {
        "AADS": "AADS", "KIS": "KIS", "GO100": "GO100",
        "ShortFlow": "SF", "NewTalk": "NT", "SALES": "SALES", "NAS": "NAS",
    }
    task_id = (
        task_id.replace("\u2011", "-").replace("\u2013", "-").replace("\u2014", "-")
    )
    for p in PREFIX_MAP.values():
        if task_id.startswith(f"{p}-"):
            return task_id
    if task_id.startswith("T-"):
        prefix = PREFIX_MAP.get(project, "AADS")
        return f"{prefix}-{task_id[2:]}"
    return task_id


async def _upsert_task_result(value: Dict[str, Any], category: str) -> Dict[str, Any]:
    """T-090: task_result 메시지를 project_tasks 테이블에 upsert (UNIQUE(task_id, source) 기준)"""
    import re as _re
    task_id = str(value.get("task_id", "")).strip()
    if not task_id:
        return {"status": "skip", "reason": "task_id 없음"}

    # source 결정 (category: cross_msg_REMOTE_211_AADS_MGR 등)
    source = "REMOTE_211"
    m = _re.search(r"cross_msg_(REMOTE_\d+)", category)
    if m:
        source = m.group(1)
    elif "REMOTE_114" in category or "114" in str(value.get("server", "")):
        source = "REMOTE_114"
    elif "211" in str(value.get("server", "")):
        source = "REMOTE_211"

    # 프로젝트 정규화
    project = str(value.get("project") or "AADS").strip()
    _pmap = {
        "kis": "KIS", "go100": "GO100", "shortflow": "ShortFlow",
        "sf": "ShortFlow", "newtalk": "NewTalk", "ntv2": "NewTalk",
        "nas": "NAS", "aads": "AADS", "sales": "SALES",
    }
    project = _pmap.get(project.lower(), project) if project else "AADS"

    # T-107: task_id를 접두사 형식으로 정규화 (AADS-095, KIS-168 등)
    task_id = _normalize_task_id_for_db(task_id, project)

    status_raw = str(value.get("status", "reported")).lower()
    if status_raw in ("done", "finished", "success", "completed", "완료"):
        status = "completed"
    elif status_raw in ("running", "active", "queued", "pending"):
        status = "running"
    else:
        status = "reported"

    title = str(value.get("title") or task_id)[:500]
    summary = str(value.get("summary") or value.get("result") or title)[:500]

    def _parse_ts(s):
        if not s:
            return None
        from datetime import datetime as _dt
        if isinstance(s, _dt):
            return s
        try:
            return _dt.fromisoformat(str(s))
        except Exception:
            return None

    started_at = _parse_ts(value.get("started_at"))
    completed_at = _parse_ts(value.get("completed_at") or value.get("finished_at"))

    try:
        async with memory_store.pool.acquire() as conn:
            # 테이블 존재 확인
            tbl_exists = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='project_tasks')"
            )
            if not tbl_exists:
                return {"status": "skip", "reason": "project_tasks 테이블 없음"}

            await conn.execute(
                """
                INSERT INTO project_tasks
                    (task_id, project, source, title, status, summary, started_at, completed_at, raw_data)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (task_id, source) DO UPDATE
                    SET project = EXCLUDED.project,
                        title = EXCLUDED.title,
                        status = EXCLUDED.status,
                        summary = EXCLUDED.summary,
                        started_at = COALESCE(EXCLUDED.started_at, project_tasks.started_at),
                        completed_at = COALESCE(EXCLUDED.completed_at, project_tasks.completed_at),
                        raw_data = EXCLUDED.raw_data
                """,
                task_id, project, source, title, status, summary,
                started_at, completed_at,
                json.dumps(value, ensure_ascii=False),
            )
        # running→completed 전이: task_id가 달라도 같은 작업이면 running 레코드 업데이트
        # (auto_trigger는 "AADS-120"으로, done_watcher는 "AADS_20260306_..._BRIDGE"로 기록할 수 있음)
        if status == "completed":
            try:
                # 1) 같은 task_id의 running 레코드 → completed
                await conn.execute(
                    """UPDATE project_tasks SET status='completed',
                       completed_at=COALESCE($2, NOW()), summary=COALESCE($3, summary)
                       WHERE task_id=$1 AND status='running'""",
                    task_id, completed_at, summary
                )
                # 2) RESULT 파일명 기반 task_id에서 원본 task_id 추출 매칭
                #    done_watcher가 "AADS-120"으로 기록하면, auto_trigger의 "AADS-120" running도 업데이트
                #    반대로 파일명 기반이면 directive_lifecycle에서 실제 task_id 찾아서 매칭
                import re as _re2
                if _re2.match(r"[A-Z]+-\d+$", task_id):
                    # 실제 task_id (AADS-120 형식) → 파일명 기반 running 레코드도 완료 처리
                    await conn.execute(
                        """UPDATE project_tasks SET status='completed',
                           completed_at=COALESCE($2, NOW()), summary=COALESCE($3, summary)
                           WHERE task_id LIKE $1 || '%' AND status='running' AND project=$4""",
                        task_id, completed_at, summary, project
                    )
            except Exception:
                pass  # best-effort

        return {"status": "ok", "project": project, "task_id": task_id, "source": source}
    except Exception as e:
        return {"status": "error", "detail": str(e)}

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

# --- Public Summary (읽기 전용, 인증 불필요) ---
SENSITIVE_KEYS = [
    "api_key", "secret", "password", "token", "pat", "sk-ant", "sk-proj", "AIzaSy",
    "ADMIN_PASSWORD", "JWT_SECRET", "MONITOR_KEY", "ssh_key", "private_key"
]

def _sanitize(data: Any) -> Any:
    if isinstance(data, dict):
        sanitized = {}
        for k, v in data.items():
            if any(s.lower() in str(k).lower() for s in SENSITIVE_KEYS):
                sanitized["[SENSITIVE_KEY]"] = "[REDACTED]"
            else:
                sanitized[k] = _sanitize(v)
        return sanitized
    elif isinstance(data, list):
        return [_sanitize(item) for item in data]
    elif isinstance(data, str):
        if any(pattern in data for pattern in ["sk-ant", "sk-proj", "AIzaSy"]):
            return "[REDACTED]"
        return data
    return data

@router.get("/context/public-summary")
async def get_public_summary():
    """읽기 전용 공개 메모리 요약 (Monitor Key 불필요, 민감 데이터 자동 제거)"""
    async with memory_store.pool.acquire() as conn:
        # system_memory 전체 카테고리 조회
        sys_rows = await conn.fetch(
            "SELECT category, key, value FROM system_memory ORDER BY category, key"
        )
        # experience_memory 최근 10건
        exp_rows = await conn.fetch(
            "SELECT id, experience_type, domain, tags, content, access_count, rif_score, created_at "
            "FROM experience_memory ORDER BY created_at DESC LIMIT 10"
        )
        # procedural_memory 상위 10건
        proc_rows = await conn.fetch(
            "SELECT id, procedure_name, steps, success_rate, execution_count, agent_name, procedure_type "
            "FROM procedural_memory ORDER BY success_rate DESC LIMIT 10"
        )

    # system_memory 카테고리별 그룹화
    data: Dict[str, List] = {}
    for r in sys_rows:
        cat = r['category']
        if cat not in data:
            data[cat] = []
        raw_value = r['value']
        if isinstance(raw_value, str):
            try:
                raw_value = json.loads(raw_value)
            except Exception:
                pass
        data[cat].append({"key": r['key'], "value": _sanitize(raw_value)})

    category_list = sorted(data.keys())
    total_categories = len(category_list)

    # project:* 카테고리 추출
    active_projects = [c for c in category_list if c.startswith("project:")]

    # experience 정제
    recent_experiences = []
    for r in exp_rows:
        entry = dict(r)
        entry['content'] = _sanitize(entry.get('content'))
        if entry.get('created_at'):
            entry['created_at'] = str(entry['created_at'])
        recent_experiences.append(entry)

    # procedural 정제
    procedures = []
    for r in proc_rows:
        entry = dict(r)
        entry['content'] = _sanitize(entry.get('content'))
        if entry.get('updated_at'):
            entry['updated_at'] = str(entry['updated_at'])
        procedures.append(entry)

    return {
        "status": "ok",
        "memory_system": "5-layer PostgreSQL + pgvector",
        "total_categories": total_categories,
        "category_list": category_list,
        "data": data,
        "recent_experiences": recent_experiences,
        "procedures": procedures,
        "active_projects": active_projects,
        "note": "Sensitive data automatically removed"
    }

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
