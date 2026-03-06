"""
AADS Documents API — CEO 문서 저장/조회/삭제
T-102: 브릿지 문서감지 + CEO 문서 자동 저장 시스템

엔드포인트:
  GET    /api/v1/documents            — 문서 목록 (query: tag=plan|tech|research|status|directive)
  GET    /api/v1/documents/{doc_id}   — 문서 본문 (마크다운 반환)
  POST   /api/v1/documents            — 문서 등록 (브릿지 또는 수동)
  DELETE /api/v1/documents/{doc_id}   — 문서 삭제

데이터:
  - system_memory 테이블 (category: ceo_document)
  - 파일: /root/aads/aads-docs/reports/ceo-documents/{DOC_ID}_{slug}.md
  - 인덱스: /root/aads/aads-docs/reports/ceo-documents/_index.json
"""
import json
import logging
import os
import re
import hmac
from datetime import datetime, timezone, timedelta
from typing import Optional, List

import asyncpg
from fastapi import APIRouter, HTTPException, Header, Query
from pydantic import BaseModel

from app.config import Settings

logger = logging.getLogger(__name__)
settings = Settings()
router = APIRouter()

MONITOR_KEY = os.getenv("AADS_MONITOR_KEY", "")
CEO_DOCS_DIR = os.getenv("CEO_DOCS_DIR", "/root/aads/aads-docs/reports/ceo-documents")
INDEX_FILE = os.path.join(CEO_DOCS_DIR, "_index.json")

# 문서 ID 타입 접두어
DOC_TYPE_PREFIX = {
    "plan":      "PLAN",
    "tech":      "TECH",
    "research":  "RESEARCH",
    "status":    "STATUS",
    "directive": "DIRECTIVE",
}


# ─── 인증 ─────────────────────────────────────────────────────────────────
def _verify_key(x_monitor_key: str = None) -> bool:
    if not MONITOR_KEY:
        raise HTTPException(503, "Monitor key not configured")
    if not x_monitor_key or not hmac.compare_digest(x_monitor_key, MONITOR_KEY):
        raise HTTPException(401, "Invalid monitor key")
    return True


# ─── KST 타임스탬프 ───────────────────────────────────────────────────────
def _now_kst() -> str:
    kst = timezone(timedelta(hours=9))
    return datetime.now(kst).strftime("%Y-%m-%d %H:%M KST")


# ─── slug 생성 ────────────────────────────────────────────────────────────
def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:60]


# ─── DB 헬퍼 ─────────────────────────────────────────────────────────────
async def _get_conn():
    return await asyncpg.connect(dsn=settings.DATABASE_URL)


# ─── 인덱스 파일 읽기/쓰기 ────────────────────────────────────────────────
def _load_index() -> dict:
    os.makedirs(CEO_DOCS_DIR, exist_ok=True)
    if not os.path.exists(INDEX_FILE):
        return {"generated_at": "", "total_documents": 0, "documents": []}
    try:
        with open(INDEX_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"generated_at": "", "total_documents": 0, "documents": []}


def _save_index(idx: dict):
    os.makedirs(CEO_DOCS_DIR, exist_ok=True)
    idx["generated_at"] = _now_kst()
    idx["total_documents"] = len(idx.get("documents", []))
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=2)


# ─── 다음 문서 ID 생성 ────────────────────────────────────────────────────
def _next_doc_id(doc_type: str, existing_docs: list) -> str:
    prefix = DOC_TYPE_PREFIX.get(doc_type, doc_type.upper())
    existing_nums = []
    for d in existing_docs:
        did = d.get("id", "")
        if did.startswith(prefix + "-"):
            try:
                existing_nums.append(int(did.split("-")[1]))
            except (IndexError, ValueError):
                pass
    seq = max(existing_nums, default=0) + 1
    return f"{prefix}-{seq:03d}"


# ─── Pydantic 모델 ────────────────────────────────────────────────────────
class DocumentCreateRequest(BaseModel):
    type: str                        # plan | tech | research | status | directive
    title: str
    content: str                     # 마크다운 본문
    tags: Optional[List[str]] = None
    source_session: Optional[str] = "manual"
    doc_id: Optional[str] = None     # 수동 지정 시 (소급 저장용)


# ─── GET /documents ──────────────────────────────────────────────────────
@router.get("")
async def list_documents(
    tag: Optional[str] = Query(None, description="plan|tech|research|status|directive"),
    x_monitor_key: Optional[str] = Header(None),
):
    """문서 목록 조회 (tag 필터 가능, 인증 불필요)"""
    idx = _load_index()
    docs = idx.get("documents", [])
    if tag:
        docs = [d for d in docs if tag in d.get("tags", []) or d.get("type") == tag]
    return {
        "status": "ok",
        "total": len(docs),
        "generated_at": idx.get("generated_at", ""),
        "documents": docs,
    }


# ─── GET /documents/{doc_id} ─────────────────────────────────────────────
@router.get("/{doc_id}")
async def get_document(doc_id: str):
    """문서 본문 조회 (마크다운 반환)"""
    idx = _load_index()
    docs = idx.get("documents", [])
    doc_meta = next((d for d in docs if d.get("id") == doc_id), None)
    if not doc_meta:
        raise HTTPException(404, f"Document '{doc_id}' not found")

    filename = doc_meta.get("filename", "")
    filepath = os.path.join(CEO_DOCS_DIR, filename)
    if not os.path.exists(filepath):
        # DB에서 content 조회 시도
        try:
            conn = await _get_conn()
            try:
                row = await conn.fetchrow(
                    "SELECT value FROM system_memory WHERE category=$1 AND key=$2",
                    "ceo_document", doc_id
                )
                if row:
                    val = json.loads(row["value"])
                    return {
                        "status": "ok",
                        "doc_id": doc_id,
                        "meta": doc_meta,
                        "content": val.get("content", ""),
                        "source": "db",
                    }
            finally:
                await conn.close()
        except Exception as e:
            logger.warning("DB fallback failed for doc %s: %s", doc_id, e)
        raise HTTPException(404, f"Document file not found: {filename}")

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    return {
        "status": "ok",
        "doc_id": doc_id,
        "meta": doc_meta,
        "content": content,
        "source": "file",
    }


# ─── POST /documents ─────────────────────────────────────────────────────
@router.post("")
async def create_document(
    req: DocumentCreateRequest,
    x_monitor_key: Optional[str] = Header(None),
):
    """문서 등록 (Monitor Key 인증 필수)"""
    _verify_key(x_monitor_key)

    idx = _load_index()
    docs = idx.get("documents", [])

    # doc_id 결정
    if req.doc_id:
        doc_id = req.doc_id.upper()
        # 중복 확인 — 이미 있으면 덮어쓰기 허용
        docs = [d for d in docs if d.get("id") != doc_id]
    else:
        doc_id = _next_doc_id(req.type, docs)

    # 파일명 + 경로
    slug = _slugify(req.title)
    filename = f"{doc_id}_{slug}.md"
    filepath = os.path.join(CEO_DOCS_DIR, filename)

    # 마크다운 파일 저장
    os.makedirs(CEO_DOCS_DIR, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(req.content)

    # 태그
    tags = req.tags or [req.type]
    if req.type not in tags:
        tags = [req.type] + tags

    now = _now_kst()
    doc_entry = {
        "id": doc_id,
        "type": req.type,
        "title": req.title,
        "filename": filename,
        "created_at": now,
        "source_session": req.source_session or "manual",
        "summary": req.content[:200].replace("\n", " "),
        "tags": tags,
    }

    # 인덱스 업데이트
    docs.append(doc_entry)
    idx["documents"] = docs
    _save_index(idx)

    # system_memory DB 저장
    try:
        conn = await _get_conn()
        try:
            value = {**doc_entry, "content": req.content}
            await conn.execute("""
                INSERT INTO system_memory (category, key, value, version, updated_by, updated_at)
                VALUES ($1, $2, $3, $4, $5, NOW())
                ON CONFLICT (category, key) DO UPDATE
                SET value=$3, version=$4, updated_by=$5, updated_at=NOW()
            """, "ceo_document", doc_id, json.dumps(value, ensure_ascii=False), "1.0", "documents_api")
        finally:
            await conn.close()
    except Exception as e:
        logger.warning("DB save failed for doc %s: %s", doc_id, e)

    logger.info("Document saved: %s (%s) -> %s", doc_id, req.type, filepath)
    return {
        "status": "ok",
        "doc_id": doc_id,
        "filename": filename,
        "filepath": filepath,
        "meta": doc_entry,
    }


# ─── DELETE /documents/{doc_id} ──────────────────────────────────────────
@router.delete("/{doc_id}")
async def delete_document(
    doc_id: str,
    x_monitor_key: Optional[str] = Header(None),
):
    """문서 삭제 (Monitor Key 인증 필수)"""
    _verify_key(x_monitor_key)

    idx = _load_index()
    docs = idx.get("documents", [])
    doc_meta = next((d for d in docs if d.get("id") == doc_id), None)
    if not doc_meta:
        raise HTTPException(404, f"Document '{doc_id}' not found")

    # 파일 삭제
    filename = doc_meta.get("filename", "")
    filepath = os.path.join(CEO_DOCS_DIR, filename)
    if os.path.exists(filepath):
        os.remove(filepath)

    # 인덱스에서 제거
    idx["documents"] = [d for d in docs if d.get("id") != doc_id]
    _save_index(idx)

    # DB에서 제거
    try:
        conn = await _get_conn()
        try:
            await conn.execute(
                "DELETE FROM system_memory WHERE category=$1 AND key=$2",
                "ceo_document", doc_id
            )
        finally:
            await conn.close()
    except Exception as e:
        logger.warning("DB delete failed for doc %s: %s", doc_id, e)

    return {"status": "ok", "deleted": doc_id}
