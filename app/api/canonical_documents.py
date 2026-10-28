"""Tenant and project scoped canonical documents. No legacy rows are mutated."""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import os
import posixpath
import re
import shlex
import unicodedata
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from app.auth import TenantRole, require_tenant_role
from app.core.db_pool import get_pool
from app.core.project_config import PROJECT_MAP, REMOTE_PROJECTS

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/projects/{project_key}/documents", tags=["canonical-documents"])
ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_ROOT = ROOT
# 운영 컨테이너(aads-server-green)의 /app/docs 는 빌드 시점 이미지 사본이다. /host/aads-server 로는
# reports/ 하나만 읽기 전용 마운트되어 있고 docs/ 는 마운트되지 않는다(docker inspect 실측 2026-10-28).
# 그래서 로컬(마운트 → 이미지)에서 못 찾거나 낡았을 수 있는 경우는 GitHub 원격 기본 브랜치를 본다.
HOST_SOURCE_ROOT = Path("/host/aads-server")
GITHUB_SOURCE_REPO = "moongoby-GO100/aads-server"
GITHUB_SOURCE_BRANCH = "main"
GITHUB_READ_TIMEOUT = 10.0
SOURCE_NOT_FOUND_MESSAGE = "이미지·마운트·원격 기본 브랜치 어디에서도 파일을 찾지 못했습니다"
SOURCE_REMOTE_FAILED_MESSAGE = SOURCE_NOT_FOUND_MESSAGE + "(원격 기본 브랜치 조회 실패: 네트워크 오류 또는 시간 초과)"
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


def _require_human_approver(context: dict) -> None:
    # Use the same identity resolution as project authorization and audit.
    _, actor, _ = _scope({**context, "membership": context.get("membership") or {}})
    if context.get("user", {}).get("auth_type") != "human_session" or actor.startswith("system:"):
        raise HTTPException(403, "human_login_required")
    if (context.get("membership") or {}).get("status") != "active":
        raise HTTPException(403, "active_membership_required")


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


def _source_roots() -> list[Path]:
    roots = [ROOT]
    if ROOT == _DEFAULT_ROOT and HOST_SOURCE_ROOT.is_dir():
        roots.insert(0, HOST_SOURCE_ROOT)
    return roots


def _locate_source_origin(candidate: Path) -> tuple[Path, str]:
    for root in _source_roots():
        try:
            resolved = (root / candidate).resolve(strict=True)
        except (OSError, ValueError):
            continue
        if resolved.is_file() and resolved.is_relative_to(root.resolve()):
            return resolved, ("host" if root == HOST_SOURCE_ROOT else "image")
    raise HTTPException(422, "invalid_source_path")


def _locate_source(candidate: Path) -> Path:
    return _locate_source_origin(candidate)[0]


def _source_syntax(path: str | None) -> str | None:
    """상대경로·docs/reports 하위·'..' 금지 검사 후 NFC 정규화한 POSIX 경로를 돌려준다(파일 접근 없음)."""
    if path is None:
        return None
    path = unicodedata.normalize("NFC", path)
    if (not path or len(path) > 512 or "://" in path or "\\" in path
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in path)):
        raise HTTPException(422, "invalid_source_path")
    candidate = Path(path)
    if (candidate.is_absolute() or not candidate.parts or candidate.parts[0] not in ("docs", "reports")
            or any(part in (".", "..") or part.startswith(".") for part in candidate.parts)):
        raise HTTPException(422, "invalid_source_path")
    return candidate.as_posix()


def _safe_source(path: str | None) -> str | None:
    source = _source_syntax(path)
    if source is not None:
        _locate_source(Path(source))
    return source


def _read_source(source: str) -> bytes:
    return _read_local_source(source)[0]


def _read_local_source(source: str) -> tuple[bytes, str]:
    """Read at most one byte past the limit, including if the file grows after stat. Returns (bytes, host|image)."""
    path, origin = _locate_source_origin(Path(source))
    try:
        if path.stat().st_size > MAX_DOCUMENT_BYTES:
            raise HTTPException(413, "document_too_large")
        with path.open("rb") as stream:
            raw = stream.read(MAX_DOCUMENT_BYTES + 1)
    except OSError as exc:
        raise HTTPException(422, "invalid_source_path") from exc
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise HTTPException(413, "document_too_large")
    return raw, origin


def _source_not_found(message: str = SOURCE_NOT_FOUND_MESSAGE) -> HTTPException:
    return HTTPException(422, {"code": "invalid_source_path", "message": message})


async def _github_fetch(source: str) -> bytes:
    from app.core.llm_key_provider import get_api_key

    headers = {"Accept": "application/vnd.github+json"}
    # tool_executor._read_github_file / chat_tools.read_github_file 과 같은 토큰 조회(레지스트리 → env). 새 키 없음.
    pat = await get_api_key("GITHUB_TOKEN", fallback_env="GITHUB_TOKEN") or os.getenv("GITHUB_PAT", "")
    if pat:
        headers["Authorization"] = f"token {pat}"
    # raw.githubusercontent.com 은 브랜치 ref 를 수 분간 캐시해 방금 푸시한 파일이 404 일 수 있어 contents API 를 쓴다.
    url = f"https://api.github.com/repos/{GITHUB_SOURCE_REPO}/contents/{quote(source, safe='/')}"
    async with httpx.AsyncClient(timeout=GITHUB_READ_TIMEOUT) as client:
        response = await client.get(url, headers=headers, params={"ref": GITHUB_SOURCE_BRANCH})
    if response.status_code == 404:
        raise _source_not_found()
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or data.get("type") != "file":
        raise _source_not_found()
    if int(data.get("size") or 0) > MAX_DOCUMENT_BYTES:
        raise HTTPException(413, "document_too_large")
    if data.get("encoding") != "base64":
        raise ValueError("unexpected contents encoding")
    raw = base64.b64decode(data.get("content") or "", validate=False)
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise HTTPException(413, "document_too_large")
    return raw


async def _read_origin_source(source: str) -> bytes:
    """GitHub 원격 기본 브랜치(origin/main)의 같은 경로 blob. 경로 검사는 로컬과 같고, 실패는 한글 사유의 422."""
    source = _source_syntax(source) or ""
    try:
        return await asyncio.wait_for(_github_fetch(source), timeout=GITHUB_READ_TIMEOUT)
    except HTTPException:
        raise
    except (httpx.HTTPError, asyncio.TimeoutError, ValueError, binascii.Error, OSError) as exc:
        logger.warning("canonical_source_remote_read_failed path=%s error=%s", source, exc.__class__.__name__)
        raise _source_not_found(SOURCE_REMOTE_FAILED_MESSAGE) from exc


async def _resolve_local_project_source(source: str) -> tuple[bytes, dict[str, Any]]:
    """로컬(host→image)을 먼저 읽고 원격 기본 브랜치와 비교한다. 내용이 다르면 원격이 정본이고 로컬은 낡은 것이다."""
    local = origin = None
    try:
        local, origin = _read_local_source(source)
    except HTTPException as exc:
        if exc.status_code != 422:
            raise
    remote = remote_error = None
    try:
        remote = await _read_origin_source(source)
    except HTTPException as exc:
        if exc.status_code != 422:
            raise
        remote_error = exc
    if remote is not None:
        if local == remote:
            return local, {"source_resolved_from": origin, "source_stale_local": False, "source_remote_checked": True}
        return remote, {"source_resolved_from": "remote", "source_stale_local": local is not None,
                        "source_remote_checked": True}
    if local is not None:
        return local, {"source_resolved_from": origin, "source_stale_local": False, "source_remote_checked": False}
    raise remote_error


def _finalize(content: str, source: str | None) -> tuple[str, str | None, str]:
    encoded = content.encode("utf-8")
    if not encoded or len(encoded) > MAX_DOCUMENT_BYTES:
        raise HTTPException(413, "document_size_out_of_range")
    if SECRET.search(content):
        raise HTTPException(422, "credential_content_rejected")
    return content, source, hashlib.sha256(encoded).hexdigest()


def _verified_text(raw: bytes, content: str) -> str:
    try:
        verified = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(422, "source_must_be_utf8") from exc
    if content and content != verified:
        raise HTTPException(409, "source_content_mismatch")
    return verified


def _body(content: str, source_path: str | None) -> tuple[str, str | None, str]:
    source = _safe_source(source_path)
    if source:
        content = _verified_text(_read_source(source), content)
    return _finalize(content, source)


def _remote_target(project: str) -> dict[str, Any] | None:
    """SSH 로 읽어야 하는 원격 프로젝트(GO100 등)의 서버·작업 디렉터리. 로컬 프로젝트(AADS)는 None."""
    target = PROJECT_MAP.get(project)
    if project in REMOTE_PROJECTS and target and target.get("workdir", "/") != "/":
        return target
    return None


# $1=저장소 상대경로 $2=작업 디렉터리 $3=최대 바이트. 작업 트리(낡고 dirty)가 아니라 origin/main 의 git 객체를 읽는다.
# fetch 는 원격 추적 ref 만 바꾼다(실패해도 기존 ref 로 계속). blob(100644/100755) 아니면 exit 3.
_REMOTE_READ_SCRIPT = (
    'GIT_TERMINAL_PROMPT=0 timeout 8 git -C "$2" fetch -q origin main </dev/null >/dev/null 2>&1; '
    'set -f; set -- "$1" "$2" "$3" $(git -C "$2" ls-tree -z origin/main -- "$1" 2>/dev/null | tr "\\0" "\\n"); '
    'case "$4 $5" in "100644 blob"|"100755 blob") ;; *) exit 3;; esac; '
    'git -C "$2" cat-file -e "$6" 2>/dev/null || exit 3; '
    'git -C "$2" cat-file blob "$6" | head -c "$3"'
)
REMOTE_READ_TIMEOUT = 20


async def _read_remote_source(target: dict[str, Any], source: str) -> bytes:
    """원격 저장소 origin/main 의 문서 blob 을 최대 MAX_DOCUMENT_BYTES+1 바이트만 읽는다. 로컬 셸을 거치지 않는다."""
    remote_cmd = "timeout 15 sh -c " + " ".join(shlex.quote(a) for a in (
        _REMOTE_READ_SCRIPT, "sh", source, target["workdir"], str(MAX_DOCUMENT_BYTES + 1)))
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
            "-p", str(target.get("port", "22")), f"root@{target['server']}", remote_cmd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        raise HTTPException(502, "remote_source_unavailable") from exc
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=REMOTE_READ_TIMEOUT)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise HTTPException(502, "remote_source_unavailable") from exc
    if proc.returncode == 3:
        raise HTTPException(422, "invalid_source_path")
    if proc.returncode != 0:
        raise HTTPException(502, "remote_source_unavailable")
    if len(stdout) > MAX_DOCUMENT_BYTES:
        raise HTTPException(413, "document_too_large")
    return stdout


async def _body_for_project(project: str, content: str, source_path: str | None) -> tuple[str, str | None, str]:
    return (await _body_for_project_ex(project, content, source_path))[:3]


async def _body_for_project_ex(
    project: str, content: str, source_path: str | None,
) -> tuple[str, str | None, str, dict[str, Any]]:
    """원격 프로젝트는 그 저장소에서 읽어 검증한다(로컬 같은 경로의 다른 프로젝트 파일로 검증하지 않는다).

    SSH 전송 실패(502)이고 호출자가 content 를 줬을 때만 검증된 경로 형식 + 제공 content 로 저장한다.
    파일 없음·경로 탈출·내용 불일치는 그대로 거부한다.
    """
    target = _remote_target(project)
    source = _source_syntax(source_path)
    meta: dict[str, Any] = {}
    if target is None:
        if source:
            raw, meta = await _resolve_local_project_source(source)
            content = _verified_text(raw, content)
    elif source:
        try:
            content = _verified_text(await _read_remote_source(target, source), content)
        except HTTPException as exc:
            if exc.status_code != 502 or not content:
                raise
    return (*_finalize(content, source), meta)


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


PROVENANCE_MESSAGE = "작성 출처(세션 또는 러너)와 작성 사유가 필요합니다"
CHANGE_SUMMARY_MIN = 10


def provenance_problem(*, source_session_id: Any, source_task_id: str | None, source_path: str | None,
                       change_summary: str | None, summary_min: int = 1) -> str | None:
    """작성 출처·사유가 빠졌으면 한글 사유, 충족이면 None. source_path(저장소 파일 가져오기)는 출처·사유 모두 면제한다."""
    if source_path:
        return None
    has_source = bool(source_session_id) or bool((source_task_id or "").strip())
    if not has_source or len((change_summary or "").strip()) < summary_min:
        return PROVENANCE_MESSAGE
    return None


async def _head(conn: Any, tenant: str, project: str, key: str, lock: bool = False):
    return await conn.fetchrow(
        "SELECT * FROM project_document_heads WHERE tenant_id=$1::uuid AND project_key=$2 "
        "AND document_key=$3" + (" FOR UPDATE" if lock else ""), tenant, project, key,
    )


async def create_revision_in_tx(conn: Any, tenant: str, actor: str, project: str, body: RevisionInput,
                                *, session_any_project: bool = False) -> dict:
    """Create a draft revision inside the caller's transaction. Never approves; approved_revision_id is untouched.

    session_any_project: 서버가 바인딩한 채팅 세션(도구 경로)은 문서 프로젝트와 워크스페이스 프로젝트가 달라도 테넌트만 맞으면 출처로 받는다.
    """
    content, source, digest, source_meta = await _body_for_project_ex(project, body.content, body.source_path)
    _safe_metadata(body.title, body.change_summary, body.source_task_id,
                   body.document_key, body.idempotency_key, source)
    if body.goal_id and not await conn.fetchval(
        "SELECT 1 FROM goals WHERE id=$1 AND tenant_id=$2::uuid AND project=$3",
        body.goal_id, tenant, project,
    ):
        raise HTTPException(404, "goal_not_found")
    if body.source_session_id:
        if session_any_project:
            session_ok = await conn.fetchval(
                "SELECT 1 FROM chat_sessions s WHERE s.id=$1 AND s.tenant_id=$2::uuid", body.source_session_id, tenant,
            )
        else:
            session_ok = await conn.fetchval(
                "SELECT 1 FROM chat_sessions s JOIN chat_workspaces w ON w.id=s.workspace_id "
                "WHERE s.id=$1 AND s.tenant_id=$2::uuid AND w.tenant_id=$2::uuid AND upper(w.project_key)=$3",
                body.source_session_id, tenant, project,
            )
        if not session_ok:
            raise HTTPException(404, "session_not_found")
    if body.source_task_id and not (
        await conn.fetchval(
            "SELECT 1 FROM goal_task_links l LEFT JOIN milestones m ON m.id=l.milestone_id "
            "JOIN goals g ON g.id=COALESCE(l.goal_id,m.goal_id) "
            "WHERE l.task_id=$1 AND l.tenant_id=$2::uuid AND g.tenant_id=$2::uuid "
            "AND g.project=$3 AND ($4::uuid IS NULL OR g.id=$4::uuid) LIMIT 1",
            body.source_task_id, tenant, project, body.goal_id,
        )
        # 목표에 연결되지 않은 러너 잡도 같은 테넌트·프로젝트의 실제 잡이면 작성 출처로 인정한다.
        or await conn.fetchval(
            "SELECT 1 FROM pipeline_jobs WHERE job_id=$1 AND tenant_id=$2::uuid AND upper(project)=$3 LIMIT 1",
            body.source_task_id, tenant, project,
        )
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
            return {"document_id": head["id"], "revision_id": same["id"], "generation": head["generation"], "idempotent": True, **source_meta}
        # source_path 없이 content 로만 등록된 기존 리비전과 원본이 같은 경우: 새 리비전·UPDATE 없이 기존 것을 돌려준다.
        by_hash = next((row for row in matches if row["content_hash"] == digest), None) if body.source_path else None
        if by_hash:
            return {"document_id": head["id"], "revision_id": by_hash["id"], "generation": head["generation"],
                    "idempotent": True, "matched_by": "content_hash", "revision": by_hash["revision"],
                    "version": by_hash["version"],
                    "approved": head["approved_revision_id"] == by_hash["id"], **source_meta}
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
    return {"document_id": head["id"], "revision_id": revision["id"], "generation": head["generation"] + 1, "idempotent": False, **source_meta}


@router.post("", status_code=201)
async def create_revision(project_key: str, body: RevisionInput, context: dict = WRITE):
    project = _project(project_key)
    async with get_pool().acquire() as conn, conn.transaction():
        tenant, actor = await _authorize(conn, context, project, "write")
        problem = provenance_problem(
            source_session_id=body.source_session_id, source_task_id=body.source_task_id,
            source_path=body.source_path, change_summary=body.change_summary,
        )
        if problem:
            raise HTTPException(422, problem)
        return await create_revision_in_tx(conn, tenant, actor, project, body)


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


@router.get("/review-queue")
async def review_queue(project_key: str, context: dict = VIEW):
    from app.services.canonical_review_digest import fetch_review_queue

    project = _project(project_key)
    async with get_pool().acquire() as conn:
        tenant, _ = await _authorize(conn, context, project, "read")
        items = await fetch_review_queue(conn, tenant, project)
    return {"project_key": project, "count": len(items),
            "documents": [{k: v for k, v in item.items() if k != "tenant_id"} for item in items]}


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
        from app.services.canonical_document_tools import approval_card
        card = None
        if row and not approved_only:
            can_approve = False
            try:
                _require_human_approver(context)
                await _authorize(conn, context, project, "approve")
                can_approve = True
            except HTTPException as exc:
                if exc.status_code != 403:
                    raise
            card = approval_card(head, {**dict(row), "status": status or "draft"}, project,
                                 can_approve=can_approve)
        return {"approval_card": card, "document": dict(head), "revision": dict(row) if row else None,
                "status": status or ("draft" if row else "missing"),
                "authoritative": bool(row and revision_id == head["approved_revision_id"])}


@router.get("/{document_key}/content")
async def get_document_content(project_key: str, document_key: str,
                               revision: int | None = Query(None, ge=1),
                               approved_only: bool = False, context: dict = VIEW):
    """정본 본문을 DB 에서만 읽는다(파일 경로 불사용). 뷰어의 /project-docs/content 응답 모양과 호환."""
    from app.services.document_refs import canonical_view_ref

    project = _project(project_key)
    async with get_pool().acquire() as conn:
        tenant, _ = await _authorize(conn, context, project, "read")
        head = await _head(conn, tenant, project, document_key)
        if not head:
            raise HTTPException(404, "document_not_found")
        if revision is not None:
            row = await conn.fetchrow(
                "SELECT * FROM project_document_revisions WHERE head_id=$1 AND tenant_id=$2::uuid "
                "AND project_key=$3 AND revision=$4", head["id"], tenant, project, revision,
            )
        else:
            revision_id = head["approved_revision_id"] if approved_only else head["latest_revision_id"]
            row = await conn.fetchrow(
                "SELECT * FROM project_document_revisions WHERE head_id=$1 AND id=$2",
                head["id"], revision_id,
            ) if revision_id else None
        if not row:
            raise HTTPException(404, "revision_not_found")
        status = await conn.fetchval(
            "SELECT action FROM project_document_events WHERE revision_id=$1 "
            "AND action IN ('review','approved','archived') ORDER BY id DESC LIMIT 1", row["id"],
        ) or "draft"
    authoritative = row["id"] == head["approved_revision_id"]
    content = row["content"] or ""
    ref = canonical_view_ref(project, document_key, title=row["title"], revision=row["revision"],
                             status=status, authoritative=authoritative)
    return {
        "project": project,
        "document_key": document_key,
        "file_path": f"{document_key}.md",
        "content": content,
        "size": len(content.encode("utf-8")),
        "encoding": "text",
        "mime_type": "text/markdown",
        "is_binary": False,
        "format": "markdown",
        "canonical": {
            "document_key": document_key,
            "revision": row["revision"],
            "revision_id": str(row["id"]),
            "version": row["version"],
            "status": status,
            "authoritative": authoritative,
            "title": row["title"],
            "content_hash": row["content_hash"],
            "source_path": row["source_path"],
        },
        "view": ref,
    }


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
        _require_human_approver(context)
        tenant, actor = await _authorize(conn, context, project, "approve")
        head = await _head(conn, tenant, project, document_key, lock=True)
        if not head:
            raise HTTPException(404, "document_not_found")
        if head["approved_revision_id"] == body.revision_id:
            return {"approved_revision_id": body.revision_id, "idempotent": True}
        if head["generation"] != body.expected_generation or head["latest_revision_id"] != body.revision_id:
            raise HTTPException(409, "generation_conflict")
        last = await conn.fetchval("SELECT action FROM project_document_events WHERE head_id=$1 AND revision_id=$2 "
                                   "AND action IN ('review','approved','archived') ORDER BY id DESC LIMIT 1",
                                   head["id"], body.revision_id)
        if last != "review":
            raise HTTPException(409, "revision_not_in_review")
        await conn.execute("UPDATE project_document_heads SET approved_revision_id=$2,generation=generation+1,updated_at=now() WHERE id=$1", head["id"], body.revision_id)
        await conn.execute("INSERT INTO project_document_events(tenant_id,project_key,head_id,revision_id,action,actor_id) "
                           "VALUES($1::uuid,$2,$3,$4,'approved',$5)", tenant, project, head["id"], body.revision_id, actor)
        return {"approved_revision_id": body.revision_id, "idempotent": False}


async def _archive_approved(conn: Any, tenant: str, project: str, head_id: Any, revision_id: Any, actor: str) -> None:
    await conn.execute("UPDATE project_document_heads SET approved_revision_id=NULL,generation=generation+1,updated_at=now() WHERE id=$1", head_id)
    await conn.execute("INSERT INTO project_document_events(tenant_id,project_key,head_id,revision_id,action,actor_id) "
                       "VALUES($1::uuid,$2,$3,$4,'archived',$5)", tenant, project, head_id, revision_id, actor)


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
        await _archive_approved(conn, tenant, project, head["id"], body.revision_id, actor)
        return {"archived": True}


async def approved_brief(conn: Any, tenant_id: str, project_key: str, limit: int = 8) -> list[dict]:
    """M2 read-only contract: bounded approved revisions, never draft content."""
    if not 1 <= limit <= 20:
        raise ValueError("limit out of range")
    rows = await conn.fetch(
        "SELECT h.document_key,h.kind,r.title,r.version,left(r.content,4000) AS excerpt,r.content_hash "
        "FROM project_document_heads h JOIN project_document_revisions r ON r.id=h.approved_revision_id "
        "AND r.head_id=h.id AND r.tenant_id=h.tenant_id AND r.project_key=h.project_key "
        "WHERE h.tenant_id=$1::uuid AND h.project_key=$2 ORDER BY h.updated_at DESC LIMIT $3",
        tenant_id, _project(project_key), limit,
    )
    return [dict(row) for row in rows]


def format_approved_brief(documents: list[dict], *, max_chars: int = 3000) -> str:
    """Use at most 3,000 characters as a conservative 3,000-token budget."""
    empty = "승인된 정본 문서 없음/조회 불가"
    if not documents:
        return empty
    parts = ["[프로젝트 승인 정본 문서]", "아래는 승인 포인터가 가리키는 문서의 발췌입니다."]
    for row in documents[:8]:
        raw_excerpt = str(row.get("excerpt") or "")
        raw_title = str(row.get("title") or "")
        excerpt = raw_excerpt[:1200]
        title = raw_title[:120]
        if not excerpt or SECRET.search(raw_excerpt) or SECRET.search(raw_title):
            continue
        item = f"\n- {str(row.get('document_key') or '')[:128]} ({str(row.get('version') or '')[:32]}): {title}\n{excerpt}"
        if len("\n".join(parts)) + 1 + len(item) > max_chars:
            break
        parts.append(item)
    return "\n".join(parts) if len(parts) > 2 else empty


async def review_revision_in_tx(
    conn: Any, tenant: str, project: str, head: Any, revision_id: Any, expected_generation: int, actor: str,
) -> dict:
    """초안→검토 전환 규칙(API·채팅 도구 공용). 호출자가 head 를 FOR UPDATE 로 잠근 트랜잭션 안에서 부른다."""
    last = await conn.fetchval("SELECT action FROM project_document_events WHERE head_id=$1 AND revision_id=$2 "
                               "ORDER BY id DESC LIMIT 1", head["id"], revision_id)
    if last == "review":
        return {"status": "review", "idempotent": True}
    if head["generation"] != expected_generation or head["latest_revision_id"] != revision_id:
        raise HTTPException(409, "generation_conflict")
    if last in ("approved", "archived"):
        raise HTTPException(409, "revision_already_decided")
    await conn.execute("UPDATE project_document_heads SET generation=generation+1,updated_at=now() WHERE id=$1", head["id"])
    await conn.execute("INSERT INTO project_document_events(tenant_id,project_key,head_id,revision_id,action,actor_id) "
                       "VALUES($1::uuid,$2,$3,$4,'review',$5)", tenant, project, head["id"], revision_id, actor)
    return {"status": "review", "idempotent": False}


@router.post("/{document_key}/review")
async def mark_review(project_key: str, document_key: str, body: DecisionInput, context: dict = WRITE):
    project = _project(project_key)
    async with get_pool().acquire() as conn, conn.transaction():
        tenant, actor = await _authorize(conn, context, project, "write")
        head = await _head(conn, tenant, project, document_key, lock=True)
        if not head:
            raise HTTPException(404, "document_not_found")
        return await review_revision_in_tx(conn, tenant, project, head, body.revision_id, body.expected_generation, actor)


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


@router.delete("/{document_key}/legacy-links/{goal_document_id}")
async def unlink_legacy_goal_document(project_key: str, document_key: str, goal_document_id: int,
                                      archive_head: bool = False, context: dict = WRITE):
    """Remove one legacy link and audit it. goal_documents and its file are never touched."""
    if goal_document_id <= 0:
        raise HTTPException(422, "invalid_goal_document_id")
    project = _project(project_key)
    async with get_pool().acquire() as conn, conn.transaction():
        tenant, actor = await _authorize(conn, context, project, "approve" if archive_head else "write")
        head = await _head(conn, tenant, project, document_key, lock=True)
        if not head:
            raise HTTPException(404, "document_not_found")
        # Transaction-local switch: the legacy_links trigger rejects every other DELETE.
        await conn.execute("SELECT set_config('aads.legacy_unlink','on',true)")
        removed = await conn.fetchrow(
            "DELETE FROM project_document_legacy_links l USING project_document_revisions r "
            "WHERE l.revision_id=r.id AND r.head_id=$1 AND l.tenant_id=$2::uuid AND l.project_key=$3 "
            "AND l.goal_document_id=$4 RETURNING l.revision_id", head["id"], tenant, project, goal_document_id,
        )
        if not removed:
            raise HTTPException(404, "legacy_link_not_found")
        await conn.execute(
            "INSERT INTO project_document_events(tenant_id,project_key,head_id,revision_id,action,actor_id,goal_document_id) "
            "VALUES($1::uuid,$2,$3,$4,'legacy_unlinked',$5,$6)", tenant, project, head["id"],
            removed["revision_id"], actor, goal_document_id,
        )
        remaining = await conn.fetchval(
            "SELECT count(*) FROM project_document_legacy_links l JOIN project_document_revisions r ON r.id=l.revision_id "
            "WHERE r.head_id=$1 AND l.tenant_id=$2::uuid AND l.project_key=$3", head["id"], tenant, project,
        )
        archived = False
        if archive_head and remaining == 0 and head["approved_revision_id"] is not None:
            await _archive_approved(conn, tenant, project, head["id"], head["approved_revision_id"], actor)
            archived = True
        return {"unlinked": True, "goal_document_id": goal_document_id, "revision_id": removed["revision_id"],
                "remaining_links": remaining, "archived": archived}
