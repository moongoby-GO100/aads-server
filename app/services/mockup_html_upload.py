"""Register one immutable HTML draft using the existing review transaction and gates."""
from __future__ import annotations

import os
from uuid import UUID, uuid4

from app.models.mockup_review import HtmlReviewCreate, ReviewCreate, RevisionCreate
from app.services import mockup_review_service as svc

MAX_HTML_BYTES = 2 * 1024 * 1024


async def register(conn, tenant: str, project: str, actor: str, elevated: bool, body: HtmlReviewCreate,
                   created_files: list | None = None) -> dict:
    data = body.html.encode("utf-8")
    if len(data) > MAX_HTML_BYTES:
        svc.fail(413, "html_too_large")
    if not body.html.strip() or "<html" not in body.html.lower():
        svc.fail(422, "html_document_required")
    if svc.SECRET.search(body.html):
        svc.fail(422, "credential_content_rejected")
    # Serialize the whole file+head+revision operation, including identical concurrent retries.
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                       f"html:{tenant}:{project}:{body.idempotency_key}")
    fingerprint = svc.request_hash("html", body.model_dump(mode="json"))
    # Include the content fingerprint in the create request hash without changing public metadata.
    head_body = ReviewCreate(**body.model_dump(include={"title", "change_type", "session_id", "goal_id", "idempotency_key"}))
    existing = await conn.fetchrow(
        "SELECT * FROM mockup_review_heads WHERE tenant_id=$1::uuid AND project_key=$2 AND create_idempotency_key=$3",
        tenant, project, body.idempotency_key)
    if existing:
        if body.session_id:
            await svc._session_scope(conn, tenant, project, body.session_id, actor, elevated)
        if existing["create_request_hash"] != fingerprint:
            svc.fail(409, "idempotency_key_conflict")
        head = await svc.head_view(conn, existing)
        revision_id = await conn.fetchval(
            "SELECT id FROM mockup_review_revisions WHERE head_id=$1 AND idempotency_key=$2",
            existing["id"], body.idempotency_key)
        return {**head, "revision_id": str(revision_id), "asset_id": "html", "idempotent": True}
    # UUID-only server paths: no user-controlled path, no overwrite, no symlink following.
    root = svc.asset_roots()["uploaded_mockups"]
    root.mkdir(parents=True, exist_ok=True)
    folder = root / uuid4().hex
    folder.mkdir(mode=0o700)
    path = folder / "mockup.html"
    if created_files is not None:
        created_files.append(path)
    uri = f"internal://uploaded_mockups/{folder.name}/mockup.html"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        # Drafts are intentionally incomplete for approval until required evidence/docs are supplied.
        revision_body = RevisionCreate(idempotency_key=body.idempotency_key, expected_generation=0,
            manifest={"assets": [{"asset_id": "html", "role": "child", "uri": uri,
                                  "sha256": svc.sha256_hex(data), "byte_size": len(data), "mime": "text/html"}]})
        # Reuse the dependency validator to require a single self-contained HTML file.
        try:
            if svc.child_references({"mime": "text/html", "uri": uri}, data):
                svc.fail(422, "html_must_be_self_contained")
        except svc.AssetProblem as exc:
            svc.fail(422, exc.code)
        head = await svc.create_review(conn, tenant, project, actor, elevated, head_body)
        await conn.execute("UPDATE mockup_review_heads SET create_request_hash=$2 WHERE id=$1",
                           UUID(head["review_id"]), fingerprint)
        revision = await svc.create_revision(conn, tenant, project, UUID(head["review_id"]), actor,
                                             revision_body, elevated=elevated)
        return {**revision, "asset_id": "html", "title": body.title}
    except BaseException:
        path.unlink(missing_ok=True)
        folder.rmdir()
        raise
