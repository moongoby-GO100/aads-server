"""
AADS Channels API — 대화창(Genspark 채팅창) CRUD + 컨텍스트 자동주입
T-103: CEO가 자유롭게 대화창 추가/수정/삭제 + context_docs 등록 + context-package 조합

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
import urllib.request
import urllib.error
from datetime import datetime, timezone
from app.config import Settings

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
    지시서 전달 시 AI가 프로젝트 맥락을 자동으로 알 수 있게 함.
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
    now_kst = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M KST")

    sections = []
    sections.append(f"# {ch.get('name', channel_id)} 컨텍스트 패키지")
    sections.append(f"> 자동 생성: {now_kst}")
    sections.append(f"> 프로젝트: {ch.get('project', '-')} | 서버: {ch.get('server', '-')}")
    sections.append("")

    if system_prompt:
        sections.append("## 시스템 프롬프트")
        sections.append(system_prompt)
        sections.append("")

    for doc in context_docs:
        role = doc.get("role", "DOCUMENT")
        url = doc.get("url", "")
        if not url:
            continue
        content = _fetch_url(url)
        # HANDOVER는 최근 10줄만
        if role == "HANDOVER":
            lines = content.splitlines()
            if len(lines) > 50:
                content = "\n".join(lines[-50:])
                content = f"[최근 50줄만 표시]\n{content}"
        sections.append(f"## {role}")
        sections.append(content)
        sections.append("")

    package_text = "\n".join(sections)
    return {
        "channel_id": channel_id,
        "channel_name": ch.get("name", channel_id),
        "generated_at": now_kst,
        "context_package": package_text,
        "doc_count": len(context_docs),
    }
