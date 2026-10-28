"""채팅 도구 canonical_document_register / canonical_document_lookup / canonical_document_review 구현.

- register: 정본 문서의 **초안 리비전**만 만든다. approve 경로는 호출하지 않고
  approved_revision_id 도 바꾸지 않는다(승인은 POST .../approve 로만).
- 생성은 app.api.canonical_documents.create_revision_in_tx (라우트와 같은 코드) 를 직접 호출한다.
- 판정·기록은 app.services.canonical_gate (shadow, fail-open) 를 거친다.
- lookup: 읽기 전용(SELECT 만).
- review: 최신 리비전을 초안→검토로만 넘긴다(PM·CEO 통합지시 세션 한정, 사유 10자 이상).
  승인·보관·본문 수정 경로는 없다. 전환 규칙은 API mark_review 와 같은 review_revision_in_tx 를 쓴다.
"""
from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from typing import Any, Optional

from fastapi import HTTPException
from pydantic import ValidationError

from app.api import canonical_documents as docs
from app.core.db_pool import get_pool
from app.services import canonical_gate, document_refs

logger = logging.getLogger(__name__)

TOOL_KEY_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_DATE_IN_KEY_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}-?(?:0[1-9]|1[0-2])(?:-?(?:0[1-9]|[12]\d|3[01]))?(?!\d)")
_VERSION_SEGMENT_RE = re.compile(r"(?:^|-)v\d+(?:-|$)")
_LOOKUP_LIMIT = 20

_STATUS_SQL = (
    "SELECT action FROM project_document_events WHERE revision_id=$1 "
    "AND action IN ('review','approved','archived') ORDER BY id DESC LIMIT 1"
)
_REVISION_COLS = "id,revision,version,title,content_hash,source_path,change_summary,created_at"


def validate_document_key(key: str) -> Optional[str]:
    """문제가 있으면 사유 문자열, 없으면 None. 소문자 kebab, 슬래시·날짜·버전 금지."""
    if not key or len(key) > 128:
        return "document_key 는 1~128자여야 합니다"
    if "/" in key or "\\" in key:
        return "document_key 에 슬래시를 쓸 수 없습니다"
    if _DATE_IN_KEY_RE.search(key):
        return "document_key 에 날짜를 넣을 수 없습니다 (날짜는 리비전이 기록합니다)"
    if _VERSION_SEGMENT_RE.search(key):
        return "document_key 에 버전(v1 등)을 넣을 수 없습니다 (버전은 version 필드)"
    if not TOOL_KEY_RE.fullmatch(key):
        return "document_key 는 소문자 kebab(a-z, 0-9, '-')여야 합니다"
    return None


def _invalid(message: str) -> dict[str, Any]:
    return {"error": "invalid_input", "message": message}


def _jsonable(row: Any) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    return {k: (v if v is None or isinstance(v, (int, bool, float, str)) else str(v)) for k, v in dict(row).items()}


async def register_document(
    *, tenant_id: str, session_id: str, inp: dict[str, Any], pool: Any = None,
) -> dict[str, Any]:
    try:
        project = docs._project(str(inp.get("project") or ""))
    except HTTPException:
        return _invalid("project 가 올바르지 않습니다")
    key = str(inp.get("document_key") or "")
    problem = validate_document_key(key)
    if problem:
        return _invalid(problem)
    content = str(inp.get("content") or "")
    file_path = str(inp.get("file_path") or "") or None
    if not content and not file_path:
        return _invalid("content 또는 file_path 중 하나는 필요합니다")

    actor = f"chat:{session_id[:8]}" if session_id else "canonical_document_tool"
    pool = pool or get_pool()
    try:
        async with pool.acquire() as conn, conn.transaction():
            head = await docs._head(conn, tenant_id, project, key, lock=True)
            is_new_document = head is None
            existing = 0
            if head:
                existing = int(await conn.fetchval(
                    "SELECT COALESCE(max(revision),0) FROM project_document_revisions WHERE head_id=$1",
                    head["id"],
                ) or 0)
            generation = int(inp["expected_generation"]) if inp.get("expected_generation") is not None else (
                int(head["generation"]) if head else 0
            )
            try:
                body = docs.RevisionInput(
                    document_key=key,
                    kind=inp.get("kind"),
                    title=str(inp.get("title") or ""),
                    version=str(inp.get("version") or f"1.0.{existing}"),
                    content=content,
                    source_path=file_path,
                    source_task_id=str(inp.get("source_task_id") or "") or None,
                    goal_id=str(inp.get("goal_id") or "") or None,
                    change_summary=str(inp.get("change_summary") or "") or None,
                    idempotency_key=str(inp.get("idempotency_key") or "") or None,
                    expected_generation=generation,
                )
            except ValidationError as exc:
                return _invalid("; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()))
            result = await docs.create_revision_in_tx(conn, tenant_id, actor, project, body)
            revision_no = await conn.fetchval(
                "SELECT revision FROM project_document_revisions WHERE id=$1", result["revision_id"],
            )
            matched_status = None
            if result.get("matched_by"):
                matched_status = "approved" if result.get("approved") else (
                    await conn.fetchval(_STATUS_SQL, result["revision_id"]) or "draft")
    except HTTPException as exc:
        return {"error": str(exc.detail), "status": exc.status_code,
                "message": "등록 실패 — 같은 내용·버전이 이미 있으면 version 을 지정하거나 내용을 바꾸세요"
                if exc.status_code == 409 else str(exc.detail)}

    out: dict[str, Any] = {
        "registered": True,
        "project": project,
        "document_key": key,
        "document_id": str(result["document_id"]),
        "revision_id": str(result["revision_id"]),
        "generation": result["generation"],
        "idempotent": result["idempotent"],
        "status": "draft",
        "approved": False,
        "note": "초안 리비전만 등록했습니다. 승인은 기존 승인 경로(POST /api/v1/projects/{project}/documents/{key}/approve)로만 합니다.",
    }
    view_status, view_authoritative = "draft", False
    if result.get("matched_by"):
        view_status, view_authoritative = matched_status, bool(result.get("approved"))
        out.update({
            "matched_by": result["matched_by"], "revision": result.get("revision"),
            "version": result.get("version"), "status": view_status, "approved": view_authoritative,
            "note": "같은 본문의 기존 리비전이 있어 새 리비전을 만들지 않았습니다(approved_revision 은 그대로).",
        })
    view = document_refs.canonical_view_ref(
        project, key, title=body.title, revision=int(revision_no) if revision_no is not None else None,
        status=view_status, authoritative=view_authoritative,
    )
    out["view"] = view
    out["report_line"] = view["report_line"]
    warnings = _rdoc_warnings(body.title, content, is_new_document)
    if warnings:
        out["rdoc_warnings"] = warnings
    gate = await canonical_gate.check_chat_register(
        tenant_id=tenant_id, project=project, document_key=key, source_path=body.source_path,
        ref=f"{project}/{key}", pool=pool,
    )
    if gate:
        out["canonical_gate"] = gate
    return out


REVIEW_REASON_MIN = 10
# 정규화(소문자, 영숫자만) 후 비교한다. CEO 통합지시 세션은 별도로 판정한다.
_REVIEW_ROLE_KEYS = frozenset({"pm", "projectmanager", "productmanager", "planner", "canonicalmanager",
                               "canonicaldocumentmanager"})


def _normalize_role(role_key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(role_key or "").lower())


async def _review_role_denial(conn: Any, tenant_id: str, session_id: str) -> Optional[str]:
    """검토 전환을 할 수 없는 세션이면 한글 사유, 허용이면 None. 세션을 확인하지 못하면 거절."""
    if not session_id:
        return "채팅 세션을 확인할 수 없어 검토 전환을 할 수 없습니다"
    from app.services.canonical_hierarchy_assignment import _is_ceo_integrated, _session_scope

    try:
        session = await _session_scope(conn, tenant_id, session_id)
    except Exception:
        logger.exception("canonical_document_review session lookup failed")
        return "채팅 세션을 확인할 수 없어 검토 전환을 할 수 없습니다"
    if not session:
        return "채팅 세션을 확인할 수 없어 검토 전환을 할 수 없습니다"
    role = _normalize_role(session["role_key"])
    if role in _REVIEW_ROLE_KEYS or (role == "ceo" and _is_ceo_integrated(session)):
        return None
    return "정본 문서의 초안→검토 전환은 PM(기획·정본관리자) 역할 세션과 CEO 통합지시 세션만 할 수 있습니다"


async def review_document(
    *, tenant_id: str, session_id: str, inp: dict[str, Any], pool: Any = None,
) -> dict[str, Any]:
    """최신 리비전을 초안→검토로 넘긴다. 승인·보관·본문 수정은 하지 않는다."""
    try:
        project = docs._project(str(inp.get("project") or ""))
    except HTTPException:
        return _invalid("project 가 올바르지 않습니다")
    key = str(inp.get("document_key") or "")
    if not docs.KEY.fullmatch(key):
        return _invalid("document_key 가 올바르지 않습니다")
    reason = str(inp.get("reason") or "").strip()
    if len(reason) < REVIEW_REASON_MIN:
        return _invalid(f"reason(검토로 올리는 사유)을 {REVIEW_REASON_MIN}자 이상 적어야 합니다")

    actor = f"chat:{session_id[:8]}" if session_id else "canonical_document_tool"
    pool = pool or get_pool()
    try:
        async with pool.acquire() as conn:
            denial = await _review_role_denial(conn, tenant_id, session_id)
            if denial:
                return {"error": "role_not_allowed", "message": denial}
            async with conn.transaction():
                head = await docs._head(conn, tenant_id, project, key, lock=True)
                if not head:
                    return {"error": "document_not_found", "message": "등록된 정본 문서가 없습니다"}
                if not head["latest_revision_id"]:
                    return {"error": "revision_not_found", "message": "검토로 넘길 리비전이 없습니다"}
                revision_id = head["latest_revision_id"]
                result = await docs.review_revision_in_tx(
                    conn, tenant_id, project, head, revision_id, int(head["generation"]), actor,
                )
    except HTTPException as exc:
        messages = {
            "revision_already_decided": "이미 승인 또는 보관된 판본이라 검토로 되돌릴 수 없습니다",
            "generation_conflict": "다른 변경과 충돌했습니다. 다시 조회한 뒤 시도하세요",
        }
        return {"error": str(exc.detail), "status": exc.status_code,
                "message": messages.get(str(exc.detail), str(exc.detail))}

    if not result["idempotent"]:
        logger.info("canonical_document_review project=%s key=%s revision=%s actor=%s reason=%s",
                    project, key, revision_id, actor, reason)
    return {
        "reviewed": True,
        "project": project,
        "document_key": key,
        "revision_id": str(revision_id),
        "status": "review",
        "idempotent": result["idempotent"],
        "approved": False,
        "reason": reason,
        "note": "검토 상태로 넘겼습니다. 승인은 CEO 가 기존 승인 경로로만 합니다.",
    }


def _rdoc_warnings(title: str, content: str, is_new_document: bool) -> list[str]:
    """신규 문서의 한글 표시 제목·첫 제목(R-DOC). 기존 문서는 소급 지적하지 않고 차단도 하지 않는다."""
    if not is_new_document:
        return []
    found: list[str] = []
    if not document_refs._HANGUL_RE.search(title or ""):
        found.append("title 에 한글이 없습니다 — 신규 문서의 화면 제목은 한글로 씁니다(R-DOC)")
    first_heading = next((ln for ln in content.splitlines() if ln.startswith("# ")), "")
    if content and first_heading and not document_refs._HANGUL_RE.search(first_heading):
        found.append("본문 첫 제목(H1)에 한글이 없습니다 — 신규 문서의 첫 제목은 한글로 씁니다(R-DOC)")
    return found


async def _document_summary(conn: Any, head: Any, project: str = "") -> dict[str, Any]:
    async def revision(revision_id: Any) -> Optional[dict[str, Any]]:
        if not revision_id:
            return None
        row = await conn.fetchrow(
            f"SELECT {_REVISION_COLS} FROM project_document_revisions WHERE head_id=$1 AND id=$2",
            head["id"], revision_id,
        )
        data = _jsonable(row)
        if data is not None:
            data["status"] = await conn.fetchval(_STATUS_SQL, revision_id) or "draft"
        return data

    latest = await revision(head["latest_revision_id"])
    approved_revision = await revision(head["approved_revision_id"])
    shown = approved_revision or latest
    summary = {
        "document_key": head["document_key"],
        "kind": head["kind"],
        "title": head["title"],
        "generation": head["generation"],
        "latest_revision": latest,
        "approved_revision": approved_revision,
        "approved": bool(head["approved_revision_id"]),
    }
    if project and shown is not None:
        summary["view"] = document_refs.canonical_view_ref(
            project, head["document_key"], title=shown.get("title") or head["title"],
            revision=shown.get("revision"), status=shown.get("status") or "draft",
            authoritative=bool(approved_revision),
        )
    if project and latest is not None:
        summary["approval_card"] = approval_card(head, latest, project)
    return summary


def approval_card(head: Any, latest: dict, project: str, *, can_approve: bool = False) -> dict[str, Any]:
    """Read-only presentation data; never an executable model action or permission request.

    Clients must refresh GET before confirmation, and again after POST (also on
    network failure). A 409 requires refresh and explicit human reconfirmation.
    """
    from urllib.parse import quote

    base = f"/api/v1/projects/{quote(project, safe='')}/documents/{quote(head['document_key'], safe='')}"
    approved = str(head["approved_revision_id"] or "") == str(latest["id"])
    return {
        "type": "canonical_document_approval", "schema_version": 1,
        "tenant_id": str(head["tenant_id"]), "project": project,
        "document_key": head["document_key"], "title": latest["title"],
        "revision_id": str(latest["id"]), "revision": latest["revision"],
        "version": latest["version"], "generation": head["generation"],
        "status": "approved" if approved else (latest.get("status") or "draft"),
        "approved": approved,
        "view": document_refs.canonical_view_ref(
            project, head["document_key"], title=latest["title"],
            revision=latest["revision"], status="approved" if approved else (latest.get("status") or "draft"),
            authoritative=approved),
        "content_endpoint": f"{base}/content?revision={latest['revision']}",
        "refresh_endpoint": base,
        "approval_endpoint": base + "/approve",
        "approval_body": {"revision_id": str(latest["id"]), "expected_generation": head["generation"]},
        "auth": "human_login_tenant_project_approve",
        "can_approve": can_approve,
    }


async def lookup_documents(*, tenant_id: str, inp: dict[str, Any], pool: Any = None) -> dict[str, Any]:
    """읽기 전용. document_key | query | file_path 중 하나로 등록 여부를 조회한다."""
    try:
        project = docs._project(str(inp.get("project") or ""))
    except HTTPException:
        return _invalid("project 가 올바르지 않습니다")
    key = str(inp.get("document_key") or "")
    query = str(inp.get("query") or "").strip()
    file_path = str(inp.get("file_path") or "").strip()
    if not (key or query or file_path):
        return _invalid("document_key, query, file_path 중 하나는 필요합니다")

    pool = pool or get_pool()
    async with pool.acquire() as conn:
        if key:
            head = await docs._head(conn, tenant_id, project, key)
            if not head:
                return {"project": project, "document_key": key, "registered": False}
            return {"project": project, "registered": True, **await _document_summary(conn, head, project)}

        if file_path:
            candidates = sorted({document_refs._norm(file_path), unicodedata.normalize("NFC", file_path)})
            rows = await conn.fetch(
                "SELECT DISTINCT h.* FROM project_document_heads h "
                "JOIN project_document_revisions r ON r.head_id=h.id "
                "WHERE h.tenant_id=$1::uuid AND h.project_key=$2 AND r.source_path=ANY($3::text[]) "
                "ORDER BY h.document_key LIMIT $4",
                tenant_id, project, candidates, _LOOKUP_LIMIT,
            )
        else:
            rows = await conn.fetch(
                "SELECT h.* FROM project_document_heads h "
                "LEFT JOIN project_document_revisions r ON r.id=h.latest_revision_id "
                "WHERE h.tenant_id=$1::uuid AND h.project_key=$2 "
                "AND (h.document_key ILIKE '%' || $3 || '%' OR h.title ILIKE '%' || $3 || '%' "
                "OR r.title ILIKE '%' || $3 || '%') "
                "ORDER BY h.updated_at DESC, h.id LIMIT $4",
                tenant_id, project, query, _LOOKUP_LIMIT,
            )
        matched_by = None
        if file_path and not rows:
            rows = await _heads_by_source_hash(conn, tenant_id, project, document_refs._norm(file_path))
            matched_by = "content_hash" if rows else None
        documents = [await _document_summary(conn, row, project) for row in rows]
        if matched_by:
            for document in documents:
                document["matched_by"] = matched_by
        out = {"project": project, "registered": bool(documents), "total": len(documents), "documents": documents}
        if matched_by:
            out["matched_by"] = matched_by
        return out


async def _heads_by_source_hash(conn: Any, tenant_id: str, project: str, path: str) -> list[Any]:
    """source_path 로 못 찾은 경우의 보조 조회: 원본 sha256 이 같은 리비전을 가진 head. 읽기 실패는 '없음'."""
    try:
        source = docs._source_syntax(path)
        target = docs._remote_target(project)
        raw = await docs._read_remote_source(target, source) if target else docs._read_source(source)
    except HTTPException:
        return []
    if not raw:
        return []
    return await conn.fetch(
        "SELECT DISTINCT h.* FROM project_document_heads h "
        "JOIN project_document_revisions r ON r.head_id=h.id "
        "WHERE h.tenant_id=$1::uuid AND h.project_key=$2 AND r.tenant_id=$1::uuid "
        "AND r.project_key=$2 AND r.content_hash=$3 ORDER BY h.document_key LIMIT $4",
        tenant_id, project, hashlib.sha256(raw).hexdigest(), _LOOKUP_LIMIT,
    )


# Lookup SQL already caps results at _LOOKUP_LIMIT=20. Cap each serialized card
# at 4 KiB and the event at 24 KiB to stay below the 32,000-char transport budget.
CARD_MAX_BYTES = 4096
CARDS_MAX_BYTES = 24576


def validated_approval_cards(cards: Any) -> list[dict]:
    """Validate persisted presentation data; reconstruct every URL and action body.

    Chat lookup has no human authorization context. Its cards always require an
    authenticated refresh before enabling approval; stored capability is rejected.
    """
    import json
    from uuid import UUID

    if not isinstance(cards, list):
        return []
    valid = []
    used = 0
    for card in cards[:_LOOKUP_LIMIT]:
        try:
            if not isinstance(card, dict):
                continue
            size = len(json.dumps(card, ensure_ascii=False).encode("utf-8"))
            if size > CARD_MAX_BYTES or used + size > CARDS_MAX_BYTES:
                continue
            UUID(card["tenant_id"])
            UUID(card["revision_id"])
            if not docs.KEY.fullmatch(card["document_key"]) or docs._project(card["project"]) != card["project"]:
                continue
            if any(type(card[k]) is not int or card[k] < minimum for k, minimum in
                   (("generation", 0), ("revision", 1))):
                continue
            if (not isinstance(card["title"], str) or not 1 <= len(card["title"]) <= 300
                    or not isinstance(card["version"], str) or len(card["version"]) > 50
                    or card["status"] not in ("draft", "review", "approved", "archived")
                    or type(card["approved"]) is not bool
                    or card["can_approve"] is not False):
                continue
            rebuilt = approval_card(
                {"tenant_id": card["tenant_id"], "document_key": card["document_key"],
                 "generation": card["generation"],
                 "approved_revision_id": card["revision_id"] if card["approved"] else None},
                {"id": card["revision_id"], "revision": card["revision"], "title": card["title"],
                 "version": card["version"], "status": card["status"]}, card["project"])
            if rebuilt != card:
                continue
            valid.append(rebuilt)
            used += size
        except (ValueError, TypeError, KeyError, HTTPException):
            continue
    return valid
