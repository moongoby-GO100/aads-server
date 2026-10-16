"""채팅 도구 mockup_review_submit 구현 — 목업 검토 생성·리비전·제출·조회.

- action 은 create | revision | submit | status 뿐이다. approve·revoke 는 의도적으로 없다
  (승인은 기존 승인 경로로만 한다). 이 모듈은 승인·철회 서비스 함수를 참조하지 않는다.
- 모든 쓰기는 API 라우트(app/api/mockup_reviews.py)와 같은 경로를 탄다: 채팅 세션의 user_id 로
  테넌트 멤버십을 읽어 라우트와 같은 context 를 만들고, canonical_documents._authorize 의
  project_document_grants 검사를 통과한 뒤 mockup_review_service 의 같은 함수를 같은 트랜잭션에서 호출한다.
- 세션에 user_id 가 없거나 멤버십·grant 가 없으면 거절한다(fail-closed).
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from fastapi import HTTPException
from pydantic import ValidationError

from app.api import canonical_documents as docs
from app.auth import (
    ADMIN_EMAIL, TenantRole, _is_internal_tenant_principal, _normalize_email, tenant_role_allows,
)
from app.core.db_pool import get_pool
from app.models.mockup_review import (
    DocRefInput, ManifestInput, ReviewCreate, RevisionCreate, SubmitReview,
)
from app.services import mockup_review_service as svc

ACTIONS = ("create", "revision", "submit", "status")
_WRITE_ACTIONS = frozenset({"create", "revision", "submit"})
_MANIFEST_PREFIX = "docs/"
_STATUS_LIST_LIMIT = 10


class _Invalid(Exception):
    pass


def _invalid(message: str) -> dict[str, Any]:
    return {"error": "invalid_input", "message": message}


def _denied(code: str, message: str) -> dict[str, Any]:
    return {"error": code, "status": 403, "message": message}


def _validation_message(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())


def _http_error(exc: HTTPException) -> dict[str, Any]:
    detail = exc.detail
    if isinstance(detail, dict):
        return {"error": str(detail.get("code") or "request_failed"), "status": exc.status_code, "detail": detail}
    return {"error": str(detail), "status": exc.status_code}


def _auto_key(action: str, payload: dict[str, Any]) -> str:
    return "mrt-" + svc.sha256_hex(svc.canonical_json({"action": action, **payload}))[:40]


def _uuid(value: Any, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise _Invalid(f"{what} 는 UUID 여야 합니다") from exc


def _parse_doc_refs(raw: Any) -> list[DocRefInput]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise _Invalid("doc_refs 는 배열이어야 합니다")
    try:
        return [DocRefInput.model_validate(item) for item in raw]
    except ValidationError as exc:
        raise _Invalid("doc_refs: " + _validation_message(exc)) from exc


def _unwrap_manifest(data: Any) -> Any:
    """manifest.json 이 {revision:{manifest}} 또는 {manifest} 로 감싸져 있어도 본문을 꺼낸다."""
    if isinstance(data, dict) and "screens" not in data and "assets" not in data:
        inner = data.get("revision", data)
        if isinstance(inner, dict) and isinstance(inner.get("manifest"), dict):
            return inner["manifest"]
    return data


def _load_manifest(inp: dict[str, Any]) -> ManifestInput:
    path = str(inp.get("manifest_path") or "").strip()
    body = inp.get("manifest")
    if bool(path) == bool(body):
        raise _Invalid("manifest_path 와 manifest 중 정확히 하나가 필요합니다")
    if path:
        if not path.startswith(_MANIFEST_PREFIX):
            raise _Invalid("manifest_path 는 서버 docs/ 기준 상대경로여야 합니다")
        try:
            raw = docs._read_source(docs._safe_source(path) or "")
            body = json.loads(raw.decode("utf-8"))
        except HTTPException as exc:
            raise _Invalid(f"manifest_path 를 읽을 수 없습니다: {exc.detail}") from exc
        except (ValueError, UnicodeDecodeError) as exc:
            raise _Invalid("manifest_path 파일이 올바른 JSON 이 아닙니다") from exc
    elif isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError as exc:
            raise _Invalid("manifest 가 올바른 JSON 이 아닙니다") from exc
    try:
        return ManifestInput.model_validate(_unwrap_manifest(body))
    except ValidationError as exc:
        raise _Invalid("manifest: " + _validation_message(exc)) from exc


async def _chat_context(conn: Any, tenant_id: str, session_id: str) -> Optional[dict[str, Any]]:
    """채팅 세션의 사용자로 API 의 get_current_tenant_context 와 같은 모양의 context 를 만든다."""
    try:
        uuid.UUID(str(session_id))
    except (ValueError, AttributeError, TypeError):
        return None
    user_id = await conn.fetchval(
        "SELECT user_id FROM chat_sessions WHERE id=$1::uuid AND tenant_id=$2::uuid", session_id, tenant_id,
    )
    user_id = str(user_id or "").strip()
    if not user_id:
        return None
    row = await conn.fetchrow(
        "SELECT t.kind, tm.role, u.email AS user_email, u.role AS user_role "
        "FROM tenant_memberships tm JOIN tenants t ON t.id = tm.tenant_id "
        "JOIN saas_users u ON u.id = tm.user_id "
        "WHERE tm.user_id=$1 AND tm.tenant_id=$2::uuid AND tm.status='active' AND tm.deleted_at IS NULL "
        "AND t.status='active' AND t.deleted_at IS NULL LIMIT 1",
        user_id, tenant_id,
    )
    if not row:
        return None
    role = str(row["role"] or "").lower()
    internal = _is_internal_tenant_principal(row["user_email"], row["user_role"])
    is_admin = _normalize_email(row["user_email"] or "") == _normalize_email(ADMIN_EMAIL)
    return {
        "user": {
            "user_id": user_id,
            "is_internal_admin": bool(
                is_admin or (str(row["kind"] or "").lower() == "internal"
                             and role in {TenantRole.OWNER.value, TenantRole.ADMIN.value} and internal)
            ),
        },
        "tenant": {"id": tenant_id},
        "membership": {"role": role},
    }


async def _head_state(conn: Any, tenant: str, project: str, review_id: uuid.UUID) -> Any:
    head = await conn.fetchrow(
        "SELECT generation, latest_revision_id FROM mockup_review_heads "
        "WHERE id=$1 AND tenant_id=$2::uuid AND project_key=$3",
        review_id, tenant, project,
    )
    if not head:
        svc.fail(404, "review_not_found")
    return head


def _normalized(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(refs, key=lambda r: r["role"])


async def _same_doc_refs(conn: Any, tenant: str, project: str, revision_id: Any, refs: list[DocRefInput]) -> bool:
    stored = await conn.fetchval("SELECT doc_refs FROM mockup_review_revisions WHERE id=$1", revision_id)
    given = await svc.validate_doc_refs(conn, tenant, project, refs, require_current=True)
    return _normalized(given) == _normalized(svc._json(stored) or [])


def _view(project: str, result: dict[str, Any]) -> dict[str, Any]:
    out = dict(result)
    out["approved"] = False
    if out.get("review_id") and out.get("revision_id"):
        out["revision_url"] = svc.revision_url(project, out["review_id"], out["revision_id"])
    return out


def _status_view(project: str, full: dict[str, Any]) -> dict[str, Any]:
    keep = ("review_id", "project", "title", "change_type", "goal_id", "status", "generation",
            "latest_revision", "approved_revision", "pending_change_requests", "unresolved_change_requests")
    out = {k: full[k] for k in keep if k in full}
    out["revisions"] = full.get("revisions", [])[:_STATUS_LIST_LIMIT]
    out["events"] = full.get("events", [])[:_STATUS_LIST_LIMIT]
    return out


async def run_action(*, tenant_id: str, session_id: str, inp: dict[str, Any], pool: Any = None) -> dict[str, Any]:
    action = str(inp.get("action") or "").strip().lower()
    if action not in ACTIONS:
        return _invalid(f"action 은 {'|'.join(ACTIONS)} 중 하나여야 합니다 (승인·철회는 이 도구로 할 수 없습니다)")
    try:
        project = docs._project(str(inp.get("project") or ""))
    except HTTPException:
        return _invalid("project 가 올바르지 않습니다")
    try:
        # 입력 검증은 DB·파일 접근 전에 끝낸다 (manifest_path 읽기는 권한 검사 뒤)
        doc_refs = _parse_doc_refs(inp.get("doc_refs"))
        review_id = _uuid(inp["review_id"], "review_id") if inp.get("review_id") else None
        if action != "create" and review_id is None:
            raise _Invalid("review_id 가 필요합니다")
        if action == "create":
            create_body = _create_body(inp, session_id)
        revision_id = _uuid(inp["revision_id"], "revision_id") if inp.get("revision_id") else None
        parent_id = _uuid(inp["parent_revision_id"], "parent_revision_id") if inp.get("parent_revision_id") else None
        explicit_gen = inp.get("expected_generation")
        if explicit_gen is not None and (isinstance(explicit_gen, bool) or not isinstance(explicit_gen, int)
                                         or explicit_gen < 0):
            raise _Invalid("expected_generation 은 0 이상의 정수여야 합니다")
    except _Invalid as exc:
        return _invalid(str(exc))

    pool = pool or get_pool()
    try:
        async with pool.acquire() as conn, conn.transaction():
            context = await _chat_context(conn, tenant_id, session_id)
            if context is None:
                return _denied("project_access_denied", "채팅 세션의 사용자·테넌트 멤버십을 확인할 수 없어 거절했습니다")
            minimum = TenantRole.MEMBER if action in _WRITE_ACTIONS else TenantRole.VIEWER
            if not tenant_role_allows(context["membership"]["role"], minimum):
                return _denied("project_access_denied", f"{minimum.value} 역할이 필요합니다")
            tenant, actor = await docs._authorize(conn, context, project, "write" if action in _WRITE_ACTIONS else "read")
            elevated = docs._scope(context)[2]

            if action == "create":
                return _view(project, await svc.create_review(conn, tenant, project, actor, elevated, create_body))
            if action == "status":
                return _status_view(project, await svc.get_review(conn, tenant, project, review_id))

            head = await _head_state(conn, tenant, project, review_id)
            generation = head["generation"] if explicit_gen is None else explicit_gen
            if action == "revision":
                try:
                    manifest = _load_manifest(inp)
                    body = RevisionCreate(
                        manifest=manifest, doc_refs=doc_refs, parent_revision_id=parent_id,
                        manifest_hash=inp.get("manifest_hash") or None,
                        resolves=inp.get("resolves") or [], expected_generation=generation,
                        idempotency_key=str(inp.get("idempotency_key") or "") or _auto_key("revision", {
                            "review_id": str(review_id), "expected_generation": explicit_gen,
                            "manifest": manifest.model_dump(mode="json"),
                            "doc_refs": [r.model_dump(mode="json") for r in doc_refs],
                            "parent": str(parent_id or ""), "resolves": inp.get("resolves") or [],
                        }),
                    )
                except _Invalid as exc:
                    return _invalid(str(exc))
                except ValidationError as exc:
                    return _invalid(_validation_message(exc))
                return _view(project, await svc.create_revision(conn, tenant, project, review_id, actor, body))

            target = revision_id or head["latest_revision_id"]
            if not target:
                return _invalid("제출할 리비전이 없습니다 — 먼저 action=revision 으로 등록하세요")
            if doc_refs and not await _same_doc_refs(conn, tenant, project, target, doc_refs):
                svc.fail(422, "doc_refs_differ_from_revision", revision_id=str(target),
                         hint="doc_refs 는 리비전에 기록된다 — action=revision 으로 doc_refs 와 함께 새 리비전을 등록한 뒤 제출하세요")
            try:
                submit = SubmitReview(
                    revision_id=target, expected_generation=generation,
                    idempotency_key=str(inp.get("idempotency_key") or "") or _auto_key("submit", {
                        "review_id": str(review_id), "revision_id": str(target), "expected_generation": explicit_gen,
                    }),
                )
            except ValidationError as exc:
                return _invalid(_validation_message(exc))
            return _view(project, await svc.submit_review(conn, tenant, project, review_id, actor, submit))
    except HTTPException as exc:
        return _http_error(exc)


def _create_body(inp: dict[str, Any], session_id: Any) -> ReviewCreate:
    title = str(inp.get("title") or "").strip()
    change_type = str(inp.get("change_type") or "").strip()
    goal_id = str(inp.get("goal_id") or "").strip() or None
    if goal_id:
        _uuid(goal_id, "goal_id")
    chat_session = _uuid(session_id, "session_id")
    try:
        return ReviewCreate(
            title=title, change_type=change_type, goal_id=goal_id, session_id=chat_session,
            idempotency_key=str(inp.get("idempotency_key") or "") or _auto_key("create", {
                "title": title, "change_type": change_type, "goal_id": goal_id, "project": str(inp.get("project") or "").upper(),
                "session_id": str(chat_session),
            }),
        )
    except ValidationError as exc:
        raise _Invalid(_validation_message(exc)) from exc
