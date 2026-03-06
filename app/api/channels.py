"""
AADS Channels API — 대화창(Genspark 채팅창) CRUD + 컨텍스트 자동주입
T-103: CEO가 자유롭게 대화창 추가/수정/삭제 + context_docs 등록 + context-package 조합
AADS-115: 매니저 Context API 주입 강화 — 자동 맥락 복원 + 세션 시작 컨텍스트 패키지

엔드포인트:
  GET    /api/v1/channels                        — 대화창 목록
  POST   /api/v1/channels                        — 대화창 추가 (context_docs 포함)
  GET    /api/v1/channels/{id}                   — 대화창 상세
  PUT    /api/v1/channels/{id}                   — 대화창 수정
  DELETE /api/v1/channels/{id}                   — 대화창 삭제
  GET    /api/v1/channels/{id}/context-package   — 컨텍스트 패키지 조합 반환 (브릿지용)

저장: system_memory 테이블 (category: channels)
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List
import json
import asyncpg
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from app.config import Settings

# ─── AADS-115: context_docs URL 캐시 (TTL 300초) ────────────────────────────
# { url: {"content": str, "fetched_at": float, "etag": str|None} }
_URL_CACHE: dict = {}
_CACHE_TTL = 300  # seconds

KST = timezone(timedelta(hours=9))

router = APIRouter()
_settings = Settings()


async def _get_conn():
    db_url = _settings.DATABASE_URL or os.getenv("DATABASE_URL", "")
    if not db_url:
        raise HTTPException(503, "DATABASE_URL not configured")
    return await asyncpg.connect(db_url)


class ContextDoc(BaseModel):
    role: str  # CONTEXT, HANDOVER, CEO_DIRECTIVES, RULES
    url: str


class ChannelCreate(BaseModel):
    id: str
    name: str
    description: str
    url: str
    status: str = "active"
    project: Optional[str] = None
    server: Optional[str] = None
    context_docs: Optional[List[ContextDoc]] = None
    system_prompt: Optional[str] = None


class ChannelUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    url: Optional[str] = None
    status: Optional[str] = None
    project: Optional[str] = None
    server: Optional[str] = None
    context_docs: Optional[List[ContextDoc]] = None
    system_prompt: Optional[str] = None


def _row_to_channel(key: str, value) -> dict:
    if isinstance(value, str):
        value = json.loads(value)
    return {
        "id": value.get("id", key),
        "name": value.get("name", ""),
        "description": value.get("description", ""),
        "url": value.get("url", ""),
        "status": value.get("status", "active"),
        "project": value.get("project"),
        "server": value.get("server"),
        "context_docs": value.get("context_docs", []),
        "system_prompt": value.get("system_prompt"),
        "created_at": value.get("created_at"),
        "updated_at": value.get("updated_at"),
    }


def _fetch_url(url: str, timeout: int = 5) -> str:
    """URL 콘텐츠를 동기적으로 fetch."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AADS-ContextFetcher/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return f"[{url} 로드 실패: {e}]"


def _fetch_url_cached(url: str, timeout: int = 5, force_refresh: bool = False) -> str:
    """AADS-115: TTL 300초 캐시로 URL fetch. 실패 시 마지막 캐시 반환."""
    now = time.time()
    cached = _URL_CACHE.get(url)

    # HANDOVER.md 변경 감지: URL에 HANDOVER 포함 시 ETag 비교
    if cached and not force_refresh:
        age = now - cached["fetched_at"]
        if age < _CACHE_TTL:
            return cached["content"]
        # TTL 만료 → 갱신 시도 (실패 시 캐시 반환)

    try:
        headers = {"User-Agent": "AADS-ContextFetcher/1.0"}
        if cached and cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 304 and cached:
                # 변경 없음 — 캐시 TTL 갱신
                _URL_CACHE[url]["fetched_at"] = now
                return cached["content"]
            content = resp.read().decode("utf-8", errors="replace")
            etag = resp.headers.get("ETag")
            _URL_CACHE[url] = {"content": content, "fetched_at": now, "etag": etag}
            return content
    except Exception as e:
        if cached:
            # graceful degradation: 마지막 성공 캐시 사용
            return cached["content"]
        return f"[{url} 로드 실패: {e}]"


def _invalidate_handover_cache() -> int:
    """HANDOVER.md 캐시 즉시 무효화. 무효화된 항목 수 반환."""
    count = 0
    for url in list(_URL_CACHE.keys()):
        if "HANDOVER" in url.upper():
            del _URL_CACHE[url]
            count += 1
    return count


async def get_recent_completed_tasks(limit: int = 5) -> list:
    """AADS-115: 최근 완료 태스크 N건 조회 (task_id, title, completed_at, summary 100자)."""
    conn = await _get_conn()
    try:
        rows = await conn.fetch(
            """
            SELECT task_id, project, title, completed_at, error_detail
            FROM directive_lifecycle
            WHERE status = 'completed' AND completed_at IS NOT NULL
            ORDER BY completed_at DESC
            LIMIT $1
            """,
            limit,
        )
        result = []
        for r in rows:
            title = r["title"] or ""
            summary = title[:100] if len(title) <= 100 else title[:97] + "..."
            completed_kst = None
            if r["completed_at"]:
                try:
                    completed_kst = r["completed_at"].astimezone(
                        timezone(timedelta(hours=9))
                    ).strftime("%Y-%m-%d %H:%M KST")
                except Exception:
                    completed_kst = str(r["completed_at"])
            result.append({
                "task_id": r["task_id"],
                "project": r["project"],
                "title": title,
                "completed_at": completed_kst,
                "summary": summary,
            })
        return result
    except Exception:
        return []
    finally:
        await conn.close()


async def get_active_errors() -> list:
    """AADS-115: 현재 미해결 에러 목록 (error_type, count)."""
    conn = await _get_conn()
    try:
        rows = await conn.fetch(
            """
            SELECT
                COALESCE(
                    CASE
                        WHEN error_detail ILIKE '%credit%' OR error_detail ILIKE '%balance%' THEN 'credit_exhausted'
                        WHEN error_detail ILIKE '%auth%' OR error_detail ILIKE '%token%expired%' THEN 'auth_expired'
                        WHEN error_detail ILIKE '%permission%' THEN 'permission_denied'
                        WHEN error_detail ILIKE '%timeout%' THEN 'timeout'
                        ELSE 'task_failure'
                    END,
                    'unknown'
                ) AS error_type,
                COUNT(*) AS count
            FROM directive_lifecycle
            WHERE status = 'failed'
              AND completed_at > NOW() - INTERVAL '24 hours'
            GROUP BY error_type
            ORDER BY count DESC
            """
        )
        return [{"error_type": r["error_type"], "count": int(r["count"])} for r in rows]
    except Exception:
        return []
    finally:
        await conn.close()


async def get_pipeline_status() -> dict:
    """AADS-115: 파이프라인 상태 요약 (healthy, stalled_count, blocked_count)."""
    conn = await _get_conn()
    try:
        stalled_queue = await conn.fetchval(
            "SELECT COUNT(*) FROM directive_lifecycle "
            "WHERE status='queued' AND queued_at < NOW() - INTERVAL '10 min'"
        )
        stalled_running = await conn.fetchval(
            "SELECT COUNT(*) FROM directive_lifecycle "
            "WHERE status='running' AND started_at < NOW() - INTERVAL '60 min'"
        )
        blocked_tasks = await conn.fetchval(
            "SELECT metric_value FROM system_metrics "
            "WHERE server='68' AND metric_name='blocked_tasks_count' "
            "ORDER BY recorded_at DESC LIMIT 1"
        )
        stalled_count = int(stalled_queue or 0) + int(stalled_running or 0)
        blocked_count = int(blocked_tasks or 0)
        return {
            "healthy": stalled_count == 0 and blocked_count == 0,
            "stalled_count": stalled_count,
            "blocked_count": blocked_count,
        }
    except Exception:
        return {"healthy": None, "stalled_count": -1, "blocked_count": -1}
    finally:
        await conn.close()


def _build_session_restore_prompt(
    channel: dict,
    recent_tasks: list,
    pipeline: dict,
    active_errors: list,
    now_kst: str,
) -> str:
    """AADS-115: CEO-DIRECTIVES 9-6 형식 세션 복원 프롬프트 자동 생성."""
    lines = [
        "## 세션 복원 프롬프트 (AADS CEO-DIRECTIVES 9-6)",
        f"> 자동 생성: {now_kst}",
        "",
        "### 역할",
        "당신은 AADS 프로젝트의 CEO 직속 지휘 AI(웹 Claude)입니다.",
        "CEO와 직접 대화하며 전략 수립, 지시서 작성, 교차검증을 담당합니다.",
        "서버에 직접 접근할 수 없으며, HANDOVER.md를 통해 맥락을 유지합니다.",
        "",
        "### 핵심 규칙 (CEO-DIRECTIVES)",
        "- R-001: 작업 완료 후 반드시 HANDOVER.md 업데이트",
        "- R-002: 지시서는 DIRECTIVE_START/END 형식으로 작성",
        "- R-003: Task ID는 프로젝트 접두사 체계 사용 (AADS-xxx, KIS-xxx 등)",
        "- R-004: 완료 조건 4가지: 코드 작성, 테스트 통과, Git push, HANDOVER 업데이트",
        "- R-013: Task ID 접두사 체계 엄수",
        "",
        "### 최근 완료 작업 (최근 3건)",
    ]
    for t in recent_tasks[:3]:
        lines.append(f"- [{t['task_id']}] {t['title']} — {t['completed_at']}")

    lines += [
        "",
        "### 파이프라인 상태",
        f"- 건강: {'정상' if pipeline.get('healthy') else '이상 발생'}",
        f"- 정체: {pipeline.get('stalled_count', '?')}건",
        f"- 차단: {pipeline.get('blocked_count', '?')}건",
    ]

    if active_errors:
        lines += ["", "### 미해결 에러"]
        for e in active_errors:
            lines.append(f"- {e['error_type']}: {e['count']}건")

    lines += [
        "",
        "### 참조 문서",
        "- HANDOVER: https://raw.githubusercontent.com/moongoby-GO100/aads-docs/main/HANDOVER.md",
        "- CEO-DIRECTIVES: https://raw.githubusercontent.com/moongoby-GO100/aads-docs/main/CEO-DIRECTIVES.md",
        "",
        "> 이 프롬프트는 대화창 컨텍스트 압축 시 자동 재주입됩니다 (BRIDGE-CONTEXT-RESTORE).",
    ]
    return "\n".join(lines)


async def get_server_environment(server: str) -> dict:
    """system_memory에서 서버 환경 스냅샷 조회 (category=server_environment, key=env_{server})."""
    conn = await _get_conn()
    try:
        row = await conn.fetchrow(
            "SELECT value FROM system_memory WHERE category = 'server_environment' AND key = $1",
            f"env_{server}",
        )
        if not row:
            return {"collected_at": "스냅샷 없음", "runtimes": {}, "projects": {}, "databases": {}, "services": {}}
        raw = row["value"]
        return json.loads(raw) if isinstance(raw, str) else dict(raw)
    except Exception:
        return {"collected_at": "조회 실패", "runtimes": {}, "projects": {}, "databases": {}, "services": {}}
    finally:
        await conn.close()


def format_runtimes(runtimes: dict) -> str:
    if not runtimes:
        return "- (데이터 없음)"
    lines = []
    for k, v in runtimes.items():
        lines.append(f"- **{k}**: {v}")
    return "\n".join(lines)


def format_projects(projects: dict) -> str:
    if not projects:
        return "- (데이터 없음)"
    lines = []
    for path, info in projects.items():
        if not isinstance(info, dict):
            lines.append(f"- `{path}`: {info}")
            continue
        exists = info.get("exists", True)
        if not exists:
            lines.append(f"- `{path}`: 디렉터리 없음")
            continue
        branch = info.get("git_branch", "-")
        last3 = info.get("git_last3", "-")
        lines.append(f"- `{path}` (브랜치: {branch})")
        if last3 and last3 != "-":
            for l in last3.splitlines()[:3]:
                lines.append(f"  - {l}")
    return "\n".join(lines) if lines else "- (데이터 없음)"


def format_databases(databases: dict) -> str:
    if not databases:
        return "- (데이터 없음)"
    lines = []
    for db_key, info in databases.items():
        lines.append(f"- **{db_key}**")
        if isinstance(info, dict):
            schema = info.get("schema", "")
            if schema:
                for l in schema.splitlines()[:10]:
                    lines.append(f"  {l}")
    return "\n".join(lines) if lines else "- (데이터 없음)"


def format_services(services: dict) -> str:
    if not services:
        return "- (데이터 없음)"
    lines = []
    systemd = services.get("systemd_active", "")
    docker = services.get("docker", "")
    if systemd:
        lines.append("**systemd 활성 서비스:**")
        for l in systemd.splitlines()[:15]:
            lines.append(f"  {l}")
    if docker:
        lines.append("**Docker 컨테이너:**")
        for l in docker.splitlines()[:10]:
            lines.append(f"  {l}")
    return "\n".join(lines) if lines else "- (데이터 없음)"


@router.get("/channels")
async def get_channels():
    """대화창 목록 반환."""
    conn = await _get_conn()
    try:
        rows = await conn.fetch(
            "SELECT key, value FROM system_memory WHERE category = 'channels' ORDER BY key"
        )
        channels = [_row_to_channel(r["key"], r["value"]) for r in rows]
        return {"channels": channels, "total": len(channels)}
    finally:
        await conn.close()


@router.post("/channels", status_code=201)
async def create_channel(req: ChannelCreate):
    """대화창 추가."""
    conn = await _get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        value = {
            "id": req.id,
            "name": req.name,
            "description": req.description,
            "url": req.url,
            "status": req.status,
            "project": req.project,
            "server": req.server,
            "context_docs": [d.model_dump() for d in req.context_docs] if req.context_docs else [],
            "system_prompt": req.system_prompt,
            "created_at": now,
            "updated_at": now,
        }
        try:
            await conn.execute(
                """
                INSERT INTO system_memory (category, key, value, updated_by)
                VALUES ('channels', $1, $2::jsonb, 'ceo')
                """,
                req.id,
                json.dumps(value),
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(409, f"채널 '{req.id}' 이미 존재합니다")
        return {"status": "created", "channel": _row_to_channel(req.id, value)}
    finally:
        await conn.close()


@router.get("/channels/{channel_id}")
async def get_channel(channel_id: str):
    """대화창 상세 반환."""
    conn = await _get_conn()
    try:
        row = await conn.fetchrow(
            "SELECT value FROM system_memory WHERE category = 'channels' AND key = $1",
            channel_id,
        )
        if not row:
            raise HTTPException(404, f"채널 '{channel_id}' 없음")
        return {"channel": _row_to_channel(channel_id, row["value"])}
    finally:
        await conn.close()


@router.put("/channels/{channel_id}")
async def update_channel(channel_id: str, req: ChannelUpdate):
    """대화창 수정."""
    conn = await _get_conn()
    try:
        row = await conn.fetchrow(
            "SELECT value FROM system_memory WHERE category = 'channels' AND key = $1",
            channel_id,
        )
        if not row:
            raise HTTPException(404, f"채널 '{channel_id}' 없음")
        raw = row["value"]
        value = json.loads(raw) if isinstance(raw, str) else dict(raw)
        if req.name is not None:
            value["name"] = req.name
        if req.description is not None:
            value["description"] = req.description
        if req.url is not None:
            value["url"] = req.url
        if req.status is not None:
            value["status"] = req.status
        if req.project is not None:
            value["project"] = req.project
        if req.server is not None:
            value["server"] = req.server
        if req.context_docs is not None:
            value["context_docs"] = [d.model_dump() for d in req.context_docs]
        if req.system_prompt is not None:
            value["system_prompt"] = req.system_prompt
        value["updated_at"] = datetime.now(timezone.utc).isoformat()
        await conn.execute(
            """
            UPDATE system_memory SET value = $1::jsonb, updated_at = NOW(), updated_by = 'ceo'
            WHERE category = 'channels' AND key = $2
            """,
            json.dumps(value),
            channel_id,
        )
        return {"status": "updated", "channel": _row_to_channel(channel_id, value)}
    finally:
        await conn.close()


@router.delete("/channels/{channel_id}")
async def delete_channel(channel_id: str):
    """대화창 삭제."""
    conn = await _get_conn()
    try:
        result = await conn.execute(
            "DELETE FROM system_memory WHERE category = 'channels' AND key = $1",
            channel_id,
        )
        if result == "DELETE 0":
            raise HTTPException(404, f"채널 '{channel_id}' 없음")
        return {"status": "deleted", "id": channel_id}
    finally:
        await conn.close()


@router.get("/channels/{channel_id}/context-package")
async def get_context_package(channel_id: str):
    """
    브릿지용: 채널의 context_docs URL들을 fetch하여 하나의 마크다운으로 조합 반환.
    AADS-110: 채널에 연결된 서버의 환경 스냅샷을 자동 포함.
    지시서 전달 시 AI가 프로젝트 맥락 + 서버 환경을 자동으로 인식.
    """
    conn = await _get_conn()
    try:
        row = await conn.fetchrow(
            "SELECT value FROM system_memory WHERE category = 'channels' AND key = $1",
            channel_id,
        )
        if not row:
            raise HTTPException(404, f"채널 '{channel_id}' 없음")
        raw = row["value"]
        ch = json.loads(raw) if isinstance(raw, str) else dict(raw)
    finally:
        await conn.close()

    context_docs = ch.get("context_docs", [])
    system_prompt = ch.get("system_prompt", "")
    server = ch.get("server", "68")
    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")

    # AADS-110: 서버 환경 스냅샷 조회
    # AADS-115: recent_completed_tasks, active_errors, pipeline_status 병렬 조회
    import asyncio as _asyncio
    env_snapshot, recent_tasks, active_errors, pipeline_status = await _asyncio.gather(
        get_server_environment(server),
        get_recent_completed_tasks(limit=5),
        get_active_errors(),
        get_pipeline_status(),
    )

    sections = []
    sections.append(f"# {ch.get('name', channel_id)} 컨텍스트 패키지")
    sections.append(f"> 자동 생성: {now_kst}")
    sections.append(f"> 프로젝트: {ch.get('project', '-')} | 서버: {server}")
    sections.append("")

    if system_prompt:
        sections.append("## 시스템 프롬프트")
        sections.append(system_prompt)
        sections.append("")

    # AADS-110: 서버 환경 스냅샷 섹션
    sections.append(f"## 서버 {server} 실시간 환경 (스냅샷: {env_snapshot.get('collected_at', '?')})")
    sections.append("")
    sections.append("### 런타임")
    sections.append(format_runtimes(env_snapshot.get("runtimes", {})))
    sections.append("")
    sections.append("### 프로젝트 디렉터리")
    sections.append(format_projects(env_snapshot.get("projects", {})))
    sections.append("")
    sections.append("### DB 스키마")
    sections.append(format_databases(env_snapshot.get("databases", {})))
    sections.append("")
    sections.append("### 서비스 상태")
    sections.append(format_services(env_snapshot.get("services", {})))
    sections.append("")
    sections.append("---")

    for doc in context_docs:
        role = doc.get("role", "DOCUMENT")
        url = doc.get("url", "")
        if not url:
            continue
        # AADS-115: 캐시 fetch 사용 (TTL 300s, graceful degradation)
        content = _fetch_url_cached(url)
        # HANDOVER는 최근 50줄만
        if role == "HANDOVER":
            doc_lines = content.splitlines()
            if len(doc_lines) > 50:
                content = "\n".join(doc_lines[-50:])
                content = f"[최근 50줄만 표시]\n{content}"
        sections.append(f"## {role}")
        sections.append(content)
        sections.append("")

    package_text = "\n".join(sections)

    # AADS-115: 세션 복원 프롬프트 생성
    session_restore_prompt = _build_session_restore_prompt(
        ch, recent_tasks, pipeline_status, active_errors, now_kst
    )

    return {
        "channel_id": channel_id,
        "channel_name": ch.get("name", channel_id),
        "generated_at": now_kst,
        "server": server,
        "env_snapshot_at": env_snapshot.get("collected_at", "없음"),
        "context_package": package_text,
        "doc_count": len(context_docs),
        # AADS-115: 신규 필드
        "recent_completed_tasks": recent_tasks,
        "active_errors": active_errors,
        "pipeline_status": pipeline_status,
        "session_restore_prompt": session_restore_prompt,
    }
