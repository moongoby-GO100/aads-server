"""Tenant and project scoped canonical documents. No legacy rows are mutated."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from app.auth import TenantRole, require_tenant_role
from app.core.db_pool import get_pool

router = APIRouter(prefix="/projects/{project_key}/documents", tags=["canonical-documents"])
ROOT = Path(__file__).resolve().parents[2]
MAX_DOCUMENT_BYTES = 262144
KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
PROJECT = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,63}$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SECRET = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{16,}|AIza[0-9A-Za-z_-]{20,}|AKIA[0-9A-Z]{16})|"
    r"(?i:(?:api[_-]?key|auth[_-]?token|access[_-]?token|refresh[_-]?token|password|client[_-]?secret|secret[_-]?key|private[_-]?key|database[_-]?url)['\"]?\s*[:=]\s*['\"]?[^\s'\"]+)"
)
VIEW = Depends(require_tenant_role(TenantRole.VIEWER))
WRITE = Depends(require_tenant_role(TenantRole.MEMBER))


def _scope(context: dict) -> tuple[str, str, bool]:
    tenant = str(context["tenant"]["id"])
    user = context["user"]
    uid = str(user.get("user_id") or user.get("id") or "")
    if not uid:
        raise HTTPException(403, "user_identity_required")
    elevated = bool(user.get("is_internal_admin")) or context["membership"].get("role") in ("admin", "owner")
    return tenant, uid, elevated


def _project(project_key: str) -> str:
    value = project_key.upper()
    if not PROJECT.fullmatch(value):
        raise HTTPException(422, "invalid_project_key")
    return value


async def _authorize(conn: Any, context: dict, project: str, access: str) -> tuple[str, str]:
    tenant, actor, elevated = _scope(context)
    if not elevated:
        roles = {"read": ("read", "write", "approve"), "write": ("write", "approve"), "approve": ("approve",)}[access]
        allowed = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM project_document_grants WHERE tenant_id=$1::uuid "
            "AND project_key=$2 AND user_id=$3 AND access=ANY($4::text[]))",
            tenant, project, actor, list(roles),
        )
        if not allowed:
            raise HTTPException(403, "project_access_denied")
    return tenant, actor


def _safe_source(path: str | None) -> str | None:
    if path is None:
        return None
    if not path or len(path) > 512 or "://" in path or "\\" in path or "\x00" in path:
        raise HTTPException(422, "invalid_source_path")
    candidate = Path(path)
    if (candidate.is_absolute() or not candidate.parts or candidate.parts[0] not in ("docs", "reports")
            or any(part in (".", "..") or part.startswith(".") for part in candidate.parts)):
        raise HTTPException(422, "invalid_source_path")
    try:
        resolved = (ROOT / candidate).resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise HTTPException(422, "invalid_source_path") from exc
    if not resolved.is_file() or not resolved.is_relative_to(ROOT.resolve()):
        raise HTTPException(422, "invalid_source_path")
    return candidate.as_posix()


def _read_source(source: str) -> bytes:
    """Read at most one byte past the limit, including if the file grows after stat."""
    path = ROOT / source
    try:
        if path.stat().st_size > MAX_DOCUMENT_BYTES:
            raise HTTPException(413, "document_too_large")
        with path.open("rb") as stream:
            raw = stream.read(MAX_DOCUMENT_BYTES + 1)
    except OSError as exc:
        raise HTTPException(422, "invalid_source_path") from exc
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise HTTPException(413, "document_too_large")
    return raw


def _body(content: str, source_path: str | None) -> tuple[str, str | None, str]:
    source = _safe_source(source_path)
    if source:
        raw = _read_source(source)
        try:
            verified = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(422, "source_must_be_utf8") from exc
        if content and content != verified:
            raise HTTPException(409, "source_content_mismatch")
        content = verified
    encoded = content.encode("utf-8")
    if not encoded or len(encoded) > MAX_DOCUMENT_BYTES:
        raise HTTPException(413, "document_size_out_of_range")
    if SECRET.search(content):
        raise HTTPException(422, "credential_content_rejected")
    return content, source, hashlib.sha256(encoded).hexdigest()


def _safe_metadata(*values: str | None) -> None:
    if any(value and SECRET.search(value) for value in values):
        raise HTTPException(422, "credential_metadata_rejected")


class RevisionInput(BaseModel):
    document_key: str = Field(min_length=1, max_length=128)
    kind: Literal["plan", "prd", "spec", "design", "architecture", "contract", "tasks", "report", "reference"]
    title: str = Field(min_length=1, max_length=300)
    version: str = Field(max_length=50)
    content: str = Field(default="", max_length=262144)
    source_path: str | None = None
    source_task_id: str | None = Field(default=None, max_length=200)
    source_session_id: UUID | None = None
    goal_id: UUID | None = None
    change_summary: str | None = Field(default=None, max_length=1000)
    idempotency_key: str | None = Field(default=None, max_length=128)
    expected_generation: int = Field(ge=0)

    @model_validator(mode="after")
    def valid_identity(self):
        if not KEY.fullmatch(self.document_key) or not VERSION.fullmatch(self.version):
            raise ValueError("invalid document key or version")
        if not self.content and not self.source_path:
            raise ValueError("content or source_path required")
        return self


async def _head(conn: Any, tenant: str, project: str, key: str, lock: bool = False):
    return await conn.fetchrow(
        "SELECT * FROM project_document_heads WHERE tenant_id=$1::uuid AND project_key=$2 "
        "AND document_key=$3" + (" FOR UPDATE" if lock else ""), tenant, project, key,
    )


@router.post("", status_code=201)
async def create_revision(project_key: str, body: RevisionInput, context: dict = WRITE):
    project = _project(project_key)
    async with get_pool().acquire() as conn, conn.transaction():
        tenant, actor = await _authorize(conn, context, project, "write")
        content, source, digest = _body(body.content, body.source_path)
        _safe_metadata(body.title, body.change_summary, body.source_task_id,
                       body.document_key, body.idempotency_key, source)
        if body.goal_id and not await conn.fetchval(
            "SELECT 1 FROM goals WHERE id=$1 AND tenant_id=$2::uuid AND project=$3",
            body.goal_id, tenant, project,
        ):
            raise HTTPException(404, "goal_not_found")
        if body.source_session_id and not await conn.fetchval(
            "SELECT 1 FROM chat_sessions s JOIN chat_workspaces w ON w.id=s.workspace_id "
            "WHERE s.id=$1 AND s.tenant_id=$2::uuid AND w.tenant_id=$2::uuid AND upper(w.project_key)=$3",
            body.source_session_id, tenant, project,
        ):
            raise HTTPException(404, "session_not_found")
        if body.source_task_id and not await conn.fetchval(
            "SELECT 1 FROM goal_task_links l LEFT JOIN milestones m ON m.id=l.milestone_id "
            "JOIN goals g ON g.id=COALESCE(l.goal_id,m.goal_id) "
            "WHERE l.task_id=$1 AND l.tenant_id=$2::uuid AND g.tenant_id=$2::uuid "
            "AND g.project=$3 AND ($4::uuid IS NULL OR g.id=$4::uuid) LIMIT 1",
            body.source_task_id, tenant, project, body.goal_id,
        ):
            raise HTTPException(404, "task_not_found")
        await conn.execute(
            "INSERT INTO project_document_heads(tenant_id,project_key,document_key,kind,title) "
            "VALUES($1::uuid,$2,$3,$4,$5) ON CONFLICT(tenant_id,project_key,document_key) DO NOTHING",
            tenant, project, body.document_key, body.kind, body.title,
        )
        head = await _head(conn, tenant, project, body.document_key, lock=True)
        if head["kind"] != body.kind:
            raise HTTPException(409, "document_kind_conflict")
        matches = await conn.fetch(
            "SELECT * FROM project_document_revisions WHERE head_id=$1 AND "
            "(content_hash=$2 OR version=$3 OR ($4::text IS NOT NULL AND idempotency_key=$4))",
            head["id"], digest, body.version, body.idempotency_key,
        )
        if matches:
            keyed = next((row for row in matches if body.idempotency_key and row["idempotency_key"] == body.idempotency_key), None)
            if keyed and (keyed["content_hash"] != digest or keyed["version"] != body.version):
                raise HTTPException(409, "idempotency_key_conflict")
            versioned = next((row for row in matches if row["version"] == body.version), None)
            if versioned and versioned["content_hash"] != digest:
                raise HTTPException(409, "revision_conflict")
            same = next((row for row in matches if row["content_hash"] == digest and row["version"] == body.version), None)
            if same:
                return {"document_id": head["id"], "revision_id": same["id"], "generation": head["generation"], "idempotent": True}
            raise HTTPException(409, "revision_conflict")
        if body.expected_generation != head["generation"]:
            raise HTTPException(409, "generation_conflict")
        next_revision = await conn.fetchval(
            "SELECT COALESCE(max(revision),0)+1 FROM project_document_revisions WHERE head_id=$1", head["id"],
        )
        revision = await conn.fetchrow(
            "INSERT INTO project_document_revisions(head_id,tenant_id,project_key,revision,version,title,content,content_hash,source_path,source_kind,source_task_id,source_session_id,goal_id,change_summary,author_id,idempotency_key) "
            "VALUES($1,$2::uuid,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16) RETURNING id",
            head["id"], tenant, project, next_revision, body.version, body.title, content, digest,
            source, "repository" if source else "api", body.source_task_id, body.source_session_id,
            body.goal_id, body.change_summary, actor, body.idempotency_key,
        )
        await conn.execute(
            "UPDATE project_document_heads SET latest_revision_id=$2,generation=generation+1,title=$3,updated_at=now() WHERE id=$1",
            head["id"], revision["id"], body.title,
        )
        await conn.execute(
            "INSERT INTO project_document_events(tenant_id,project_key,head_id,revision_id,action,actor_id) "
            "VALUES($1::uuid,$2,$3,$4,$5,$6)", tenant, project, head["id"], revision["id"],
            "created" if head["generation"] == 0 else "revised", actor,
        )
        if body.goal_id:
            await conn.execute(
                "INSERT INTO project_document_goal_links(head_id,tenant_id,project_key,goal_id) "
                "VALUES($1,$2::uuid,$3,$4) ON CONFLICT DO NOTHING",
                head["id"], tenant, project, body.goal_id,
            )
        return {"document_id": head["id"], "revision_id": revision["id"], "generation": head["generation"] + 1, "idempotent": False}


@router.get("")
async def list_documents(project_key: str, q: str | None = Query(None, max_length=100),
                         kind: str | None = None, approved_only: bool = False,
                         limit: int = Query(50, ge=1, le=100), context: dict = VIEW):
    project = _project(project_key)
    async with get_pool().acquire() as conn:
        tenant, _ = await _authorize(conn, context, project, "read")
        rows = await conn.fetch(
            "SELECT h.id,h.document_key,h.kind,selected.title,h.generation,h.latest_revision_id,h.approved_revision_id,h.updated_at, "
            "COALESCE((SELECT e.action FROM project_document_events e WHERE e.revision_id=selected.id "
            "AND e.action IN ('review','approved','archived') ORDER BY e.id DESC LIMIT 1),'draft') AS status "
            "FROM project_document_heads h LEFT JOIN project_document_revisions selected "
            "ON selected.id=CASE WHEN $5::bool THEN h.approved_revision_id ELSE h.latest_revision_id END "
            "WHERE h.tenant_id=$1::uuid AND h.project_key=$2 "
            "AND ($3::text IS NULL OR h.kind=$3) "
            "AND ($4::text IS NULL OR selected.title ILIKE '%' || $4 || '%' OR h.document_key ILIKE '%' || $4 || '%' "
            "OR selected.content ILIKE '%' || $4 || '%') "
            "AND (NOT $5::bool OR selected.id IS NOT NULL) "
            "ORDER BY h.updated_at DESC,h.id LIMIT $6", tenant, project, kind, q, approved_only, limit,
        )
        return {"documents": [dict(row) for row in rows]}


@router.get("/{document_key}")
async def get_document(project_key: str, document_key: str, approved_only: bool = False,
                       context: dict = VIEW):
    project = _project(project_key)
    async with get_pool().acquire() as conn:
        tenant, _ = await _authorize(conn, context, project, "read")
        head = await _head(conn, tenant, project, document_key)
        if not head:
            raise HTTPException(404, "document_not_found")
        revision_id = head["approved_revision_id"] if approved_only else head["latest_revision_id"]
        row = await conn.fetchrow("SELECT * FROM project_document_revisions WHERE head_id=$1 AND id=$2", head["id"], revision_id) if revision_id else None
        status = await conn.fetchval(
            "SELECT action FROM project_document_events WHERE revision_id=$1 "
            "AND action IN ('review','approved','archived') ORDER BY id DESC LIMIT 1", revision_id,
        ) if revision_id else None
        return {"document": dict(head), "revision": dict(row) if row else None,
                "status": status or ("draft" if row else "missing"),
                "authoritative": bool(row and revision_id == head["approved_revision_id"])}


@router.get("/{document_key}/history")
async def document_history(project_key: str, document_key: str, context: dict = VIEW):
    project = _project(project_key)
    async with get_pool().acquire() as conn:
        tenant, _ = await _authorize(conn, context, project, "read")
        head = await _head(conn, tenant, project, document_key)
        if not head:
            raise HTTPException(404, "document_not_found")
        rows = await conn.fetch("SELECT r.id,r.revision,r.version,r.title,r.content_hash,r.source_path,r.change_summary,r.author_id,r.created_at, "
                                "COALESCE((SELECT e.action FROM project_document_events e WHERE e.revision_id=r.id "
                                "AND e.action IN ('review','approved','archived') ORDER BY e.id DESC LIMIT 1),'draft') AS status "
                                "FROM project_document_revisions r WHERE r.head_id=$1 ORDER BY r.revision DESC LIMIT 100", head["id"])
        return {"revisions": [dict(row) for row in rows]}


class DecisionInput(BaseModel):
    revision_id: UUID
    expected_generation: int = Field(ge=1)


@router.post("/{document_key}/approve")
async def approve_document(project_key: str, document_key: str, body: DecisionInput, context: dict = WRITE):
    project = _project(project_key)
    async with get_pool().acquire() as conn, conn.transaction():
        tenant, actor = await _authorize(conn, context, project, "approve")
        head = await _head(conn, tenant, project, document_key, lock=True)
        if not head:
            raise HTTPException(404, "document_not_found")
        if head["approved_revision_id"] == body.revision_id:
            return {"approved_revision_id": body.revision_id, "idempotent": True}
        if head["generation"] != body.expected_generation or head["latest_revision_id"] != body.revision_id:
            raise HTTPException(409, "generation_conflict")
        await conn.execute("UPDATE project_document_heads SET approved_revision_id=$2,generation=generation+1,updated_at=now() WHERE id=$1", head["id"], body.revision_id)
        await conn.execute("INSERT INTO project_document_events(tenant_id,project_key,head_id,revision_id,action,actor_id) "
                           "VALUES($1::uuid,$2,$3,$4,'approved',$5)", tenant, project, head["id"], body.revision_id, actor)
        return {"approved_revision_id": body.revision_id, "idempotent": False}


@router.post("/{document_key}/archive")
async def archive_document(project_key: str, document_key: str, body: DecisionInput, context: dict = WRITE):
    project = _project(project_key)
    async with get_pool().acquire() as conn, conn.transaction():
        tenant, actor = await _authorize(conn, context, project, "approve")
        head = await _head(conn, tenant, project, document_key, lock=True)
        if not head:
            raise HTTPException(404, "document_not_found")
        last = await conn.fetchval("SELECT action FROM project_document_events WHERE head_id=$1 AND revision_id=$2 "
                                   "AND action IN ('approved','archived') ORDER BY id DESC LIMIT 1", head["id"], body.revision_id)
        if head["approved_revision_id"] is None and last == "archived":
            return {"archived": True, "idempotent": True}
        if head["generation"] != body.expected_generation or head["approved_revision_id"] != body.revision_id:
            raise HTTPException(409, "generation_conflict")
        await conn.execute("UPDATE project_document_heads SET approved_revision_id=NULL,generation=generation+1,updated_at=now() WHERE id=$1", head["id"])
        await conn.execute("INSERT INTO project_document_events(tenant_id,project_key,head_id,revision_id,action,actor_id) "
                           "VALUES($1::uuid,$2,$3,$4,'archived',$5)", tenant, project, head["id"], body.revision_id, actor)
        return {"archived": True}


async def approved_brief(conn: Any, tenant_id: str, project_key: str, limit: int = 8) -> list[dict]:
    """M2 read-only contract: bounded approved revisions, never draft content."""
    if not 1 <= limit <= 20:
        raise ValueError("limit out of range")
    rows = await conn.fetch(
        "SELECT h.document_key,h.kind,r.title,r.version,left(r.content,4000) AS excerpt,r.content_hash "
        "FROM project_document_heads h JOIN project_document_revisions r ON r.id=h.approved_revision_id "
        "WHERE h.tenant_id=$1::uuid AND h.project_key=$2 ORDER BY h.updated_at DESC LIMIT $3",
        tenant_id, _project(project_key), limit,
    )
    return [dict(row) for row in rows]


@router.post("/{document_key}/review")
async def mark_review(project_key: str, document_key: str, body: DecisionInput, context: dict = WRITE):
    project = _project(project_key)
    async with get_pool().acquire() as conn, conn.transaction():
        tenant, actor = await _authorize(conn, context, project, "write")
        head = await _head(conn, tenant, project, document_key, lock=True)
        if not head:
            raise HTTPException(404, "document_not_found")
        last = await conn.fetchval("SELECT action FROM project_document_events WHERE head_id=$1 AND revision_id=$2 "
                                   "ORDER BY id DESC LIMIT 1", head["id"], body.revision_id)
        if last == "review":
            return {"status": "review", "idempotent": True}
        if head["generation"] != body.expected_generation or head["latest_revision_id"] != body.revision_id:
            raise HTTPException(409, "generation_conflict")
        if last in ("approved", "archived"):
            raise HTTPException(409, "revision_already_decided")
        await conn.execute("UPDATE project_document_heads SET generation=generation+1,updated_at=now() WHERE id=$1", head["id"])
        await conn.execute("INSERT INTO project_document_events(tenant_id,project_key,head_id,revision_id,action,actor_id) "
                           "VALUES($1::uuid,$2,$3,$4,'review',$5)", tenant, project, head["id"], body.revision_id, actor)
        return {"status": "review", "idempotent": False}


class GoalLinkInput(BaseModel):
    goal_id: UUID


@router.post("/{document_key}/goals")
async def link_goal(project_key: str, document_key: str, body: GoalLinkInput, context: dict = WRITE):
    project = _project(project_key)
    async with get_pool().acquire() as conn, conn.transaction():
        tenant, actor = await _authorize(conn, context, project, "write")
        head = await _head(conn, tenant, project, document_key, lock=True)
        if not head:
            raise HTTPException(404, "document_not_found")
        if not await conn.fetchval("SELECT 1 FROM goals WHERE id=$1 AND tenant_id=$2::uuid AND project=$3",
                                   body.goal_id, tenant, project):
            raise HTTPException(404, "goal_not_found")
        changed = await conn.fetchval(
            "INSERT INTO project_document_goal_links(head_id,tenant_id,project_key,goal_id) "
            "VALUES($1,$2::uuid,$3,$4) ON CONFLICT DO NOTHING RETURNING goal_id",
            head["id"], tenant, project, body.goal_id,
        )
        if changed:
            await conn.execute("INSERT INTO project_document_events(tenant_id,project_key,head_id,revision_id,action,actor_id) "
                               "VALUES($1::uuid,$2,$3,$4,'goal_linked',$5)",
                               tenant, project, head["id"], head["latest_revision_id"], actor)
        return {"linked": True, "idempotent": not bool(changed)}


@router.get("/for-goal/{goal_id}")
async def documents_for_goal(project_key: str, goal_id: UUID, approved_only: bool = True,
                             context: dict = VIEW):
    project = _project(project_key)
    async with get_pool().acquire() as conn:
        tenant, _ = await _authorize(conn, context, project, "read")
        if not await conn.fetchval("SELECT 1 FROM goals WHERE id=$1 AND tenant_id=$2::uuid AND project=$3",
                                   goal_id, tenant, project):
            raise HTTPException(404, "goal_not_found")
        rows = await conn.fetch(
            "SELECT h.document_key,h.kind,r.title,r.id AS revision_id,r.version,r.content_hash "
            "FROM project_document_goal_links l JOIN project_document_heads h ON h.id=l.head_id "
            "LEFT JOIN project_document_revisions r ON r.id=CASE WHEN $4::bool THEN h.approved_revision_id ELSE h.latest_revision_id END "
            "WHERE l.tenant_id=$1::uuid AND l.project_key=$2 AND l.goal_id=$3 "
            "AND (NOT $4::bool OR r.id IS NOT NULL) ORDER BY h.updated_at DESC LIMIT 100",
            tenant, project, goal_id, approved_only,
        )
        return {"documents": [dict(row) for row in rows], "authoritative": bool(approved_only and rows)}


@router.get("/brief/approved")
async def approved_document_brief(project_key: str, context: dict = VIEW):
    project = _project(project_key)
    async with get_pool().acquire() as conn:
        tenant, _ = await _authorize(conn, context, project, "read")
        documents = await approved_brief(conn, tenant, project)
        return {"documents": documents, "authoritative": bool(documents)}


@router.get("/inventory/legacy")
async def legacy_document_inventory(project_key: str, context: dict = VIEW):
    """Read-only mapping candidates. No legacy row is registered automatically."""
    project = _project(project_key)
    async with get_pool().acquire() as conn:
        tenant, _ = await _authorize(conn, context, project, "read")
        goal_rows = await conn.fetch(
            "SELECT d.id,d.goal_id,d.kind,d.doc_path,d.document_key,d.version "
            "FROM goal_documents d JOIN goals g ON g.id=d.goal_id "
            "WHERE g.tenant_id=$1::uuid AND g.project=$2 ORDER BY d.id LIMIT 100",
            tenant, project,
        )
        artifact_count = await conn.fetchval(
            "SELECT count(*) FROM project_artifacts WHERE tenant_id=$1::uuid AND upper(project_id)=$2",
            tenant, project,
        )
        chat_count = await conn.fetchval(
            "SELECT count(*) FROM chat_artifacts a JOIN chat_sessions s ON s.id=a.session_id "
            "JOIN chat_workspaces w ON w.id=s.workspace_id "
            "WHERE a.tenant_id=$1::uuid AND s.tenant_id=$1::uuid AND w.tenant_id=$1::uuid AND upper(w.project_key)=$2",
            tenant, project,
        )
    candidates = []
    for row in goal_rows:
        entry = dict(row)
        try:
            source = _safe_source(entry["doc_path"])
            raw = _read_source(source)
            valid = bool(raw) and not SECRET.search(raw.decode("utf-8"))
            entry["mapping"] = {"source_path": source, "sha256": hashlib.sha256(raw).hexdigest()} if valid else None
        except (HTTPException, OSError, UnicodeDecodeError):
            entry["mapping"] = None
        candidates.append(entry)
    return {"goal_document_candidates": candidates, "project_artifact_count": artifact_count,
            "chat_artifact_count": chat_count, "auto_registered": 0, "truncated": len(goal_rows) == 100}


class LegacyLinkInput(BaseModel):
    revision_id: UUID
    goal_document_id: int = Field(gt=0)


@router.post("/{document_key}/legacy-links")
async def link_legacy_goal_document(project_key: str, document_key: str,
                                    body: LegacyLinkInput, context: dict = WRITE):
    """Register a verified reference without changing goal_documents or its file."""
    project = _project(project_key)
    async with get_pool().acquire() as conn, conn.transaction():
        tenant, actor = await _authorize(conn, context, project, "write")
        head = await _head(conn, tenant, project, document_key, lock=True)
        if not head:
            raise HTTPException(404, "document_not_found")
        revision = await conn.fetchrow(
            "SELECT id,version,source_path,content_hash FROM project_document_revisions "
            "WHERE id=$1 AND head_id=$2 AND tenant_id=$3::uuid AND project_key=$4",
            body.revision_id, head["id"], tenant, project,
        )
        legacy = await conn.fetchrow(
            "SELECT d.id,d.goal_id,d.kind,d.doc_path,d.version FROM goal_documents d "
            "JOIN goals g ON g.id=d.goal_id WHERE d.id=$1 AND g.tenant_id=$2::uuid AND g.project=$3",
            body.goal_document_id, tenant, project,
        )
        if not revision or not legacy:
            raise HTTPException(404, "document_link_target_not_found")
        if legacy["kind"] != head["kind"] or legacy["version"] != revision["version"]:
            raise HTTPException(409, "legacy_document_identity_mismatch")
        path = _safe_source(legacy["doc_path"])
        if path != revision["source_path"]:
            raise HTTPException(409, "legacy_document_path_mismatch")
        _, _, digest = _body("", path)
        if digest != revision["content_hash"]:
            raise HTTPException(409, "legacy_document_hash_mismatch")
        existing = await conn.fetchrow(
            "SELECT revision_id,goal_document_id FROM project_document_legacy_links "
            "WHERE goal_document_id=$1 FOR UPDATE", body.goal_document_id,
        )
        if existing:
            if existing["revision_id"] == body.revision_id:
                return {"linked": True, "idempotent": True}
            raise HTTPException(409, "legacy_document_already_linked")
        await conn.execute(
            "INSERT INTO project_document_legacy_links(revision_id,goal_document_id,tenant_id,project_key,goal_id,validated_path,validated_hash) "
            "VALUES($1,$2,$3::uuid,$4,$5,$6,$7)", body.revision_id, body.goal_document_id,
            tenant, project, legacy["goal_id"], path, digest,
        )
        await conn.execute(
            "INSERT INTO project_document_goal_links(head_id,tenant_id,project_key,goal_id) "
            "VALUES($1,$2::uuid,$3,$4) ON CONFLICT DO NOTHING",
            head["id"], tenant, project, legacy["goal_id"],
        )
        await conn.execute(
            "INSERT INTO project_document_events(tenant_id,project_key,head_id,revision_id,action,actor_id) "
            "VALUES($1::uuid,$2,$3,$4,'legacy_linked',$5)", tenant, project, head["id"],
            body.revision_id, actor,
        )
        return {"linked": True, "idempotent": False}


@router.get("/{document_key}/legacy-links")
async def list_legacy_goal_document_links(project_key: str, document_key: str, context: dict = VIEW):
    project = _project(project_key)
    async with get_pool().acquire() as conn:
        tenant, _ = await _authorize(conn, context, project, "read")
        head = await _head(conn, tenant, project, document_key)
        if not head:
            raise HTTPException(404, "document_not_found")
        rows = await conn.fetch(
            "SELECT l.revision_id,l.goal_document_id,l.goal_id,l.validated_path,l.validated_hash,l.created_at "
            "FROM project_document_legacy_links l JOIN project_document_revisions r ON r.id=l.revision_id "
            "WHERE r.head_id=$1 AND l.tenant_id=$2::uuid AND l.project_key=$3 "
            "ORDER BY l.created_at DESC LIMIT 100", head["id"], tenant, project,
        )
        return {"links": [dict(row) for row in rows]}
