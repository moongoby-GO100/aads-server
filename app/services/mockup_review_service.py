"""Mockup review domain logic: immutable revisions, server-side asset hashing, gated approval.

Every write locks the head row (FOR UPDATE), replays an earlier result for the same idempotency key,
then checks expected_generation. Approval, change requests and task verification re-read the asset
bytes from the allowed internal store; a client-supplied hash is only ever an assertion.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import posixpath
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from uuid import UUID

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder

from app.api.canonical_documents import SECRET
from app.models.mockup_review import (
    STATES, ApproveReview, ChangeCreate, ReviewCreate, RevisingStart, RevisionCreate, RevokeReview,
    SubmitReview, VerifyBundle,
)

ROOT = Path(__file__).resolve().parents[2]
ASSET_SCHEME = "internal://"
MAX_ASSET_BYTES = 25 * 1024 * 1024
VERIFY_TTL = timedelta(minutes=5)
EXTENSION_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
    ".gif": "image/gif", ".svg": "image/svg+xml", ".html": "text/html", ".css": "text/css",
    ".js": "text/javascript", ".woff2": "font/woff2",
}
TEXT_MIME = {"text/html", "text/css", "text/javascript", "image/svg+xml"}
BLOCKED_BEFORE_SOURCES = {"login_redirect", "login_screen", "error_page", "blocked"}
VISUAL_EVIDENCE = {"browser_capture", "snapshot"}
_BAD_URI_CHARS = re.compile(r"[\\?#%:\x00-\x1f\x7f]")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:")
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_CSS_URL = re.compile(r"url\(\s*(['\"]?)([^'\")]*)\1\s*\)")
_CSS_IMPORT = re.compile(r"@import\s+['\"]([^'\"]+)['\"]")
_RESOURCE_ATTRS = {
    "link": ("href",), "script": ("src",), "img": ("src", "srcset"), "source": ("src", "srcset"),
    "video": ("src", "poster"), "audio": ("src",), "iframe": ("src",), "embed": ("src",),
    "object": ("data",), "input": ("src",), "track": ("src",), "image": ("href", "xlink:href"),
    "use": ("href", "xlink:href"),
}


def fail(http_status: int, code: str, **extra: Any) -> None:
    raise HTTPException(http_status, {"code": code, **extra})


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: bytes | str) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------- asset store

def asset_roots() -> dict[str, Path]:
    configured = os.getenv("MOCKUP_REVIEW_ASSET_ROOT")
    return {"docs": ROOT / "docs", "mockup_assets": Path(configured) if configured else ROOT / "data" / "mockup_assets"}


class AssetProblem(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code, self.detail = code, detail


def parse_asset_uri(uri: str) -> tuple[str, str]:
    """Only internal://<root>/<relative path> is accepted; there is no network fetch at all."""
    if not uri.startswith(ASSET_SCHEME):
        raise AssetProblem("asset_uri_not_allowed", "only internal:// object store URIs are accepted")
    rest = uri[len(ASSET_SCHEME):]
    parts = rest.split("/")
    if (_BAD_URI_CHARS.search(rest) or len(parts) < 2 or any(p in ("", ".", "..") or p.startswith(".") for p in parts)
            or parts[0] not in asset_roots()):
        raise AssetProblem("asset_uri_not_allowed", "unknown root or unsafe path")
    return parts[0], "/".join(parts[1:])


def read_asset(uri: str) -> bytes:
    alias, rel = parse_asset_uri(uri)
    root = asset_roots()[alias].resolve()
    try:
        path = (root / rel).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AssetProblem("asset_not_found", uri) from exc
    if not path.is_relative_to(root) or not path.is_file():
        raise AssetProblem("asset_uri_not_allowed", "path escapes the allowed root")
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_ASSET_BYTES + 1)
    except OSError as exc:
        raise AssetProblem("asset_not_found", uri) from exc
    if len(data) > MAX_ASSET_BYTES:
        raise AssetProblem("asset_too_large", uri)
    return data


async def _read_all(assets: list[dict]) -> list[bytes | AssetProblem]:
    async def one(asset: dict) -> bytes | AssetProblem:
        try:
            return await asyncio.to_thread(read_asset, asset["uri"])
        except AssetProblem as exc:
            return exc
    return list(await asyncio.gather(*(one(a) for a in assets)))


class _RefCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.refs: list[str] = []
        self.css: list[str] = []
        self._style = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        values = {k: v for k, v in attrs if v is not None}
        for attr in _RESOURCE_ATTRS.get(tag, ()):
            if attr in values:
                if attr == "srcset":
                    self.refs.extend(p.split()[0] for p in values[attr].split(",") if p.strip())
                else:
                    self.refs.append(values[attr])
        if "style" in values:
            self.css.append(values["style"])
        self._style = tag == "style"

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self._style = False

    def handle_data(self, data: str) -> None:
        if self._style:
            self.css.append(data)


def _css_refs(text: str) -> list[str]:
    text = _CSS_COMMENT.sub("", text)
    return [m.group(2) for m in _CSS_URL.finditer(text)] + _CSS_IMPORT.findall(text)


def _child_uri(parent_uri: str, ref: str) -> str | None:
    ref = ref.strip()
    if not ref or ref.startswith("#") or ref.lower().startswith("data:"):
        return None
    if ref.startswith("//") or _SCHEME.match(ref.lower()):
        raise AssetProblem("external_reference_not_allowed", ref[:200])
    if ref.startswith("/"):
        raise AssetProblem("absolute_reference_not_allowed", ref[:200])
    path = unquote(re.split(r"[?#]", ref, maxsplit=1)[0])
    if not path or "\\" in path or re.search(r"[\x00-\x1f\x7f]", path):
        raise AssetProblem("invalid_asset_reference", ref[:200])
    alias, rel = parse_asset_uri(parent_uri)
    joined = posixpath.normpath(posixpath.join(alias, posixpath.dirname(rel), path))
    if joined.split("/")[0] != alias or joined.startswith(".."):
        raise AssetProblem("reference_escapes_root", ref[:200])
    return ASSET_SCHEME + joined


def child_references(asset: dict, data: bytes) -> set[str]:
    """Normalized internal URIs of every sub-resource a text asset pulls in."""
    mime = asset["mime"]
    text = data.decode("utf-8", errors="replace")
    raw: list[str] = []
    if mime == "text/html":
        collector = _RefCollector()
        collector.feed(text)
        collector.close()
        raw.extend(collector.refs)
        for block in collector.css:
            raw.extend(_css_refs(block))
    elif mime == "text/css":
        raw.extend(_css_refs(text))
    found = set()
    for ref in raw:
        target = _child_uri(asset["uri"], ref)
        if target:
            found.add(target)
    return found


# ------------------------------------------------------------------- manifest

def viewport_class(viewport: str) -> str:
    if viewport in ("desktop", "mobile", "tablet"):
        return viewport
    width = int(viewport.split("x")[0])
    return "mobile" if width <= 500 else "tablet" if width < 1024 else "desktop"


async def build_manifest(body: RevisionCreate, change_type: str) -> dict:
    """Normalize the submitted manifest; hashes/sizes always come from the actual bytes."""
    manifest = body.manifest
    screen_ids = {s.screen_id for s in manifest.screens}
    if len(screen_ids) != len(manifest.screens):
        fail(422, "duplicate_screen_id")
    asset_ids = [a.asset_id for a in manifest.assets]
    if len(set(asset_ids)) != len(asset_ids):
        fail(422, "duplicate_asset_id")
    if len({a.uri for a in manifest.assets}) != len(manifest.assets):
        fail(422, "duplicate_asset_uri")
    for asset in manifest.assets:
        if asset.screen_id and asset.screen_id not in screen_ids:
            fail(422, "unknown_screen", asset_id=asset.asset_id, screen_id=asset.screen_id)
    for item in manifest.evidence:
        if item.screen_id and item.screen_id not in screen_ids:
            fail(422, "unknown_screen", evidence_id=item.evidence_id, screen_id=item.screen_id)
        if SECRET.search(item.detail):
            fail(422, "credential_content_rejected", evidence_id=item.evidence_id)
    if manifest.backend_only_rationale and SECRET.search(manifest.backend_only_rationale):
        fail(422, "credential_content_rejected")

    declared = [a.model_dump(mode="json") for a in manifest.assets]
    blobs = await _read_all(declared)
    assets = []
    by_uri: dict[str, bytes] = {}
    for item, data in zip(declared, blobs):
        if isinstance(data, AssetProblem):
            fail(422, data.code, asset_id=item["asset_id"], detail=data.detail)
        expected_mime = EXTENSION_MIME.get(posixpath.splitext(item["uri"])[1].lower())
        if expected_mime is None or expected_mime != item["mime"]:
            fail(422, "asset_mime_mismatch", asset_id=item["asset_id"], expected=expected_mime)
        digest = sha256_hex(data)
        if digest != item["sha256"]:
            fail(422, "asset_hash_mismatch", asset_id=item["asset_id"], server_sha256=digest)
        if len(data) != item["byte_size"]:
            fail(422, "asset_size_mismatch", asset_id=item["asset_id"], server_byte_size=len(data))
        if item["mime"] in TEXT_MIME:
            try:
                if SECRET.search(data.decode("utf-8")):
                    fail(422, "credential_content_rejected", asset_id=item["asset_id"])
            except UnicodeDecodeError:
                fail(422, "asset_not_utf8", asset_id=item["asset_id"])
        if item["captured_at"]:
            item["captured_at"] = _utc(datetime.fromisoformat(item["captured_at"].replace("Z", "+00:00")))
        by_uri[item["uri"]] = data
        assets.append(item)
    for item in assets:
        try:
            missing = child_references(item, by_uri[item["uri"]]) - set(by_uri)
        except AssetProblem as exc:
            fail(422, exc.code, asset_id=item["asset_id"], detail=exc.detail)
        if missing:
            fail(422, "unhashed_child_reference", asset_id=item["asset_id"], references=sorted(missing)[:20])

    evidence = []
    for item in manifest.evidence:
        data = item.model_dump(mode="json")
        data["recorded_at"] = _utc(item.recorded_at)
        evidence.append(data)
    screens = []
    for screen in manifest.screens:
        data = screen.model_dump(mode="json")
        data["requirement_ids"] = sorted(set(data["requirement_ids"]))
        screens.append(data)
    return {
        "change_type": change_type,
        "design_tokens_version": manifest.design_tokens_version,
        "source_sha": manifest.source_sha,
        "backend_only_rationale": manifest.backend_only_rationale,
        "screens": sorted(screens, key=lambda s: s["screen_id"]),
        "assets": sorted(assets, key=lambda a: a["asset_id"]),
        "evidence": sorted(evidence, key=lambda e: e["evidence_id"]),
    }


def manifest_hash(manifest: dict) -> str:
    return sha256_hex(canonical_json(manifest))


async def recheck_assets(manifest: dict) -> list[dict]:
    """Re-read every declared asset; return the ones whose bytes no longer match the stored hash."""
    assets = manifest["assets"]
    changed = []
    for asset, data in zip(assets, await _read_all(assets)):
        if isinstance(data, AssetProblem):
            changed.append({"asset_id": asset["asset_id"], "reason": data.code})
        elif sha256_hex(data) != asset["sha256"] or len(data) != asset["byte_size"]:
            changed.append({"asset_id": asset["asset_id"], "reason": "hash_changed"})
    return changed


def completeness(manifest: dict, doc_refs: list[dict]) -> list[dict]:
    issues: list[dict] = []
    present = {ref["role"] for ref in doc_refs}
    for role in ("plan", "prd", "spec"):
        if role not in present:
            issues.append({"code": "document_ref_missing", "role": role})
    if manifest["change_type"] == "none":
        if len((manifest.get("backend_only_rationale") or "").strip()) < 20:
            issues.append({"code": "backend_only_rationale_missing"})
        return issues
    if not manifest["screens"]:
        issues.append({"code": "screens_missing"})
    primary = [a for a in manifest["assets"] if a["role"] == "primary"]
    for asset in primary:
        if asset["redaction_status"] == "pending":
            issues.append({"code": "redaction_pending", "asset_id": asset["asset_id"], "screen_id": asset["screen_id"]})
        if asset["phase"] == "before" and asset["capture_source"] in BLOCKED_BEFORE_SOURCES:
            issues.append({"code": "blocked_evidence", "asset_id": asset["asset_id"], "screen_id": asset["screen_id"],
                           "detail": "login/error screen cannot be a Before capture"})
    for screen in manifest["screens"]:
        sid = screen["screen_id"]
        mine = [a for a in primary if a["screen_id"] == sid]
        mockups = [a for a in mine if a["phase"] == "mockup"]
        default_mockups = {(viewport_class(a["viewport"]), a["fixture_id"], a["viewport"])
                           for a in mockups if a["state"] == "default"}
        for needed in ("desktop", "mobile"):
            if not any(v[0] == needed for v in default_mockups):
                issues.append({"code": "mockup_missing", "screen_id": sid, "viewport": needed})
        for state in STATES[1:]:
            if state not in screen["states_not_applicable"] and not any(a["state"] == state for a in mockups):
                issues.append({"code": "state_missing", "screen_id": sid, "state": state})
        if manifest["change_type"] == "modify":
            before = {(a["viewport"], a["fixture_id"]) for a in mine if a["phase"] == "before" and a["state"] == "default"}
            for viewport_name, fixture, viewport in sorted(default_mockups):
                if (viewport, fixture) not in before:
                    issues.append({"code": "before_missing", "screen_id": sid, "viewport": viewport,
                                   "fixture_id": fixture})
        visual = [e for e in manifest["evidence"] if e["success"] and e["kind"] in VISUAL_EVIDENCE
                  and (e["screen_id"] == sid or (e["screen_id"] is None and e["route"] == screen["route"]))]
        if not visual:
            issues.append({"code": "visual_evidence_missing", "screen_id": sid})
    return issues


# ------------------------------------------------------------------ documents

async def validate_doc_refs(conn: Any, tenant: str, project: str, refs: list, *, require_current: bool) -> list[dict]:
    out = []
    for ref in sorted(refs, key=lambda r: r["role"] if isinstance(r, dict) else r.role):
        item = ref if isinstance(ref, dict) else ref.model_dump(mode="json")
        if item.get("exempt_reason"):
            out.append({"role": item["role"], "exempt_reason": item["exempt_reason"],
                        "exempt_policy_ref": item["exempt_policy_ref"]})
            continue
        row = await conn.fetchrow(
            "SELECT r.id,r.revision,r.version,r.content_hash,h.document_key,h.kind,h.latest_revision_id "
            "FROM project_document_revisions r JOIN project_document_heads h ON h.id=r.head_id "
            "AND h.tenant_id=r.tenant_id AND h.project_key=r.project_key "
            "WHERE r.id=$1::uuid AND r.tenant_id=$2::uuid AND r.project_key=$3",
            item["revision_id"], tenant, project,
        )
        if not row:
            fail(422, "document_ref_not_found", role=item["role"], revision_id=str(item["revision_id"]))
        if row["document_key"] != item["document_key"] or row["kind"] != item["role"]:
            fail(422, "document_ref_mismatch", role=item["role"], document_key=row["document_key"], kind=row["kind"])
        if row["content_hash"] != item["content_hash"]:
            fail(422, "document_ref_hash_mismatch", role=item["role"], server_content_hash=row["content_hash"])
        if require_current and row["latest_revision_id"] != row["id"]:
            fail(409, "document_revision_stale", role=item["role"], document_key=row["document_key"],
                 latest_revision_id=str(row["latest_revision_id"]))
        out.append({"role": item["role"], "document_key": row["document_key"], "revision_id": str(row["id"]),
                    "revision": row["revision"], "version": row["version"], "content_hash": row["content_hash"]})
    return out


# ---------------------------------------------------------------- shared bits

def request_hash(action: str, payload: dict) -> str:
    return sha256_hex(canonical_json({"action": action, "payload": payload}))


async def _lock(conn: Any, tenant: str, project: str, review_id: UUID) -> Any:
    head = await conn.fetchrow(
        "SELECT * FROM mockup_review_heads WHERE id=$1 AND tenant_id=$2::uuid AND project_key=$3 FOR UPDATE",
        review_id, tenant, project,
    )
    if not head:
        fail(404, "review_not_found")
    return head


async def _replay(conn: Any, head: Any, key: str, action: str, rhash: str) -> dict | None:
    row = await conn.fetchrow(
        "SELECT request_hash,response FROM mockup_review_events WHERE head_id=$1 AND idempotency_key=$2",
        head["id"], key,
    )
    if not row:
        return None
    if row["request_hash"] != rhash:
        fail(409, "idempotency_key_conflict", action=action)
    return {**_json(row["response"]), "idempotent": True}


async def _stale(conn: Any, head: Any, code: str, **extra: Any) -> None:
    latest = await conn.fetchrow(
        "SELECT revision,manifest_hash FROM mockup_review_revisions WHERE id=$1", head["latest_revision_id"],
    ) if head["latest_revision_id"] else None
    fail(409, code, latest_revision_id=str(head["latest_revision_id"]) if latest else None,
         latest_revision=latest["revision"] if latest else None,
         manifest_hash=latest["manifest_hash"] if latest else None,
         generation=head["generation"], status=head["status"], **extra)


async def _event(conn: Any, head: Any, action: str, actor: str, *, generation_after: int, revision_id: Any = None,
                 key: str | None = None, rhash: str | None = None, payload: dict | None = None,
                 build_response: Any = None) -> tuple[int, dict | None]:
    event_id = await conn.fetchval("SELECT nextval(pg_get_serial_sequence('mockup_review_events','id'))")
    response = jsonable_encoder(build_response(event_id)) if build_response else None
    await conn.execute(
        "INSERT INTO mockup_review_events(id,tenant_id,project_key,head_id,revision_id,action,actor_id,"
        "idempotency_key,request_hash,generation_after,payload,response) "
        "VALUES($1,$2::uuid,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12::jsonb)",
        event_id, head["tenant_id"], head["project_key"], head["id"], revision_id, action, actor, key, rhash,
        generation_after, json.dumps(jsonable_encoder(payload or {})),
        json.dumps(response) if response is not None else None,
    )
    return event_id, response


_HEAD_COLUMNS = {"status", "latest_revision_id", "approved_revision_id", "approval_event_id", "generation",
                 "pointer_generation"}


async def _update_head(conn: Any, head_id: Any, **columns: Any) -> None:
    assert set(columns) <= _HEAD_COLUMNS
    names = list(columns)
    sets = ",".join(f"{name}=${i + 2}" for i, name in enumerate(names))
    await conn.execute(f"UPDATE mockup_review_heads SET {sets},updated_at=now() WHERE id=$1",
                       head_id, *(columns[n] for n in names))


async def _hold_bindings(conn: Any, head_id: Any) -> int:
    """Unstarted tasks are held; running tasks are flagged for checkpoint review. Nothing is killed."""
    result = await conn.execute(
        "UPDATE mockup_review_task_bindings SET updated_at=now(),status=CASE status "
        "WHEN 'bound' THEN 'held' WHEN 'running' THEN 'review_required' ELSE status END "
        "WHERE head_id=$1 AND status IN ('bound','running')", head_id)
    return int(result.rsplit(" ", 1)[-1])


async def _session_scope(conn: Any, tenant: str, project: str, session_id: UUID, actor: str, elevated: bool) -> Any:
    row = await conn.fetchrow(
        "SELECT s.id,s.user_id FROM chat_sessions s JOIN chat_workspaces w ON w.id=s.workspace_id "
        "WHERE s.id=$1 AND s.tenant_id=$2::uuid AND w.tenant_id=$2::uuid AND upper(w.project_key)=$3",
        session_id, tenant, project,
    )
    if not row:
        fail(404, "session_not_found")
    if row["user_id"] and row["user_id"] != actor and not elevated:
        fail(403, "session_access_denied")
    return row


def _revision_ref(row: Any) -> dict | None:
    if not row:
        return None
    return {"revision_id": str(row["id"]), "revision": row["revision"], "manifest_hash": row["manifest_hash"],
            "pointer_applied": row["pointer_applied"], "created_by": row["created_by"],
            "created_at": row["created_at"].isoformat()}


async def head_view(conn: Any, head: Any) -> dict:
    latest = await conn.fetchrow("SELECT * FROM mockup_review_revisions WHERE id=$1", head["latest_revision_id"]) \
        if head["latest_revision_id"] else None
    approved = await conn.fetchrow("SELECT * FROM mockup_review_revisions WHERE id=$1", head["approved_revision_id"]) \
        if head["approved_revision_id"] else None
    pending = await conn.fetchval(
        "SELECT count(*) FROM mockup_review_change_requests WHERE head_id=$1 AND status<>'resolved'", head["id"])
    approved_ref = _revision_ref(approved)
    if approved_ref:
        approved_ref["approval_id"] = head["approval_event_id"]
    return {
        "review_id": str(head["id"]), "project": head["project_key"], "title": head["title"],
        "change_type": head["change_type"], "goal_id": str(head["goal_id"]) if head["goal_id"] else None,
        "session_id": str(head["session_id"]) if head["session_id"] else None,
        "status": head["status"], "generation": head["generation"], "pointer_generation": head["pointer_generation"],
        "latest_revision": _revision_ref(latest), "approved_revision": approved_ref,
        "pending_change_requests": pending, "created_at": head["created_at"].isoformat(),
        "updated_at": head["updated_at"].isoformat(),
    }


# ----------------------------------------------------------------- operations

async def create_review(conn: Any, tenant: str, project: str, actor: str, elevated: bool, body: ReviewCreate) -> dict:
    rhash = request_hash("create", body.model_dump(mode="json"))
    if SECRET.search(body.title):
        fail(422, "credential_metadata_rejected")
    if body.goal_id and not await conn.fetchval(
        "SELECT 1 FROM goals WHERE id=$1 AND tenant_id=$2::uuid AND project=$3", body.goal_id, tenant, project,
    ):
        fail(404, "goal_not_found")
    if body.session_id:
        await _session_scope(conn, tenant, project, body.session_id, actor, elevated)
    row = await conn.fetchrow(
        "INSERT INTO mockup_review_heads(tenant_id,project_key,title,change_type,goal_id,session_id,created_by,"
        "create_idempotency_key,create_request_hash) VALUES($1::uuid,$2,$3,$4,$5,$6,$7,$8,$9) "
        "ON CONFLICT(tenant_id,project_key,create_idempotency_key) DO NOTHING RETURNING *",
        tenant, project, body.title, body.change_type, body.goal_id, body.session_id, actor, body.idempotency_key, rhash,
    )
    if row is None:
        existing = await conn.fetchrow(
            "SELECT * FROM mockup_review_heads WHERE tenant_id=$1::uuid AND project_key=$2 AND create_idempotency_key=$3",
            tenant, project, body.idempotency_key)
        if existing["create_request_hash"] != rhash:
            fail(409, "idempotency_key_conflict", action="create")
        return {**await head_view(conn, existing), "idempotent": True}
    await _event(conn, row, "created", actor, generation_after=0, payload={"change_type": body.change_type})
    return {**await head_view(conn, row), "idempotent": False}


async def create_revision(conn: Any, tenant: str, project: str, review_id: UUID, actor: str,
                          body: RevisionCreate) -> dict:
    head = await _lock(conn, tenant, project, review_id)
    rhash = request_hash("revision", body.model_dump(mode="json"))
    replayed = await _replay(conn, head, body.idempotency_key, "revision", rhash)
    if replayed:
        return replayed
    archive = False
    if body.expected_generation != head["generation"]:
        if body.archive_if_stale and body.expected_generation < head["generation"]:
            archive = True
        else:
            await _stale(conn, head, "generation_conflict")
    if not archive and head["latest_revision_id"] and (body.parent_revision_id or head["latest_revision_id"]) != head["latest_revision_id"]:
        await _stale(conn, head, "stale_revision")

    batch = await _resolution_batch(conn, head, body, archive)
    manifest = await build_manifest(body, head["change_type"])
    digest = manifest_hash(manifest)
    if body.manifest_hash and body.manifest_hash != digest:
        fail(422, "manifest_hash_mismatch", server_manifest_hash=digest)
    doc_refs = await validate_doc_refs(conn, tenant, project, body.doc_refs, require_current=False)
    number = await conn.fetchval("SELECT COALESCE(max(revision),0)+1 FROM mockup_review_revisions WHERE head_id=$1",
                                 head["id"])
    parent = head["latest_revision_id"]
    revision = await conn.fetchrow(
        "INSERT INTO mockup_review_revisions(head_id,tenant_id,project_key,revision,parent_revision_id,manifest,"
        "manifest_hash,doc_refs,design_tokens_version,source_sha,pointer_applied,created_by,idempotency_key) "
        "VALUES($1,$2::uuid,$3,$4,$5,$6::jsonb,$7,$8::jsonb,$9,$10,$11,$12,$13) RETURNING *",
        head["id"], tenant, project, number, body.parent_revision_id or parent, json.dumps(manifest), digest,
        json.dumps(doc_refs), manifest["design_tokens_version"], manifest["source_sha"], not archive, actor,
        body.idempotency_key,
    )
    base = {"review_id": str(head["id"]), "revision_id": str(revision["id"]), "revision": number,
            "manifest_hash": digest, "doc_refs": doc_refs}
    if archive:
        _, response = await _event(
            conn, head, "revision_archived_stale", actor, generation_after=head["generation"], revision_id=revision["id"],
            key=body.idempotency_key, rhash=rhash, payload={"expected_generation": body.expected_generation},
            build_response=lambda _eid: {**base, "pointer_applied": False, "reason": "stale_generation",
                                         "generation": head["generation"], "status": head["status"],
                                         "idempotent": False})
        return response

    now = datetime.now(timezone.utc)
    for item in body.resolves:
        await conn.execute(
            "UPDATE mockup_review_change_requests SET status='resolved',outcome=$3,outcome_reason=$4,"
            "resolved_revision_id=$5,resolved_at=$6 WHERE head_id=$1 AND change_request_id=$2",
            head["id"], item.change_request_id, item.outcome, item.reason, revision["id"], now)
    remaining = await conn.fetchval(
        "SELECT count(*) FROM mockup_review_change_requests WHERE head_id=$1 AND status<>'resolved'", head["id"])
    await conn.execute(
        "UPDATE mockup_review_change_requests SET rebased_to_revision_id=$2 WHERE head_id=$1 AND status='open'",
        head["id"], revision["id"])
    held = await _hold_bindings(conn, head["id"])
    status = "changes_requested" if remaining else "draft"
    generation = head["generation"] + 1
    await _update_head(conn, head["id"], status=status, latest_revision_id=revision["id"], generation=generation,
                       pointer_generation=generation)
    _, response = await _event(
        conn, head, "revision_created", actor, generation_after=generation, revision_id=revision["id"],
        key=body.idempotency_key, rhash=rhash,
        payload={"revision": number, "parent_revision_id": parent, "resolves": [r.model_dump(mode="json") for r in body.resolves],
                 "bindings_held": held, "batch": sorted(batch)},
        build_response=lambda _eid: {**base, "pointer_applied": True, "generation": generation, "status": status,
                                     "parent_revision_id": str(parent) if parent else None,
                                     "unapproved": True, "approval_inherited": False, "bindings_held": held,
                                     "idempotent": False})
    return response


async def _resolution_batch(conn: Any, head: Any, body: RevisionCreate, archive: bool) -> set[str]:
    if archive:
        if body.resolves:
            fail(422, "invalid_resolution", detail="a stale archived revision cannot resolve change requests")
        return set()
    rows = await conn.fetch(
        "SELECT change_request_id,status FROM mockup_review_change_requests WHERE head_id=$1 AND status<>'resolved' "
        "ORDER BY id", head["id"])
    in_revision = {str(r["change_request_id"]) for r in rows if r["status"] == "in_revision"}
    batch = in_revision or {str(r["change_request_id"]) for r in rows}
    given = {str(r.change_request_id) for r in body.resolves}
    if given - batch:
        fail(422, "invalid_resolution", unknown=sorted(given - batch))
    if batch - given:
        fail(422, "unresolved_change_requests", missing=sorted(batch - given))
    return batch


async def start_revising(conn: Any, tenant: str, project: str, review_id: UUID, actor: str,
                         body: RevisingStart) -> dict:
    head = await _lock(conn, tenant, project, review_id)
    rhash = request_hash("revising", body.model_dump(mode="json"))
    replayed = await _replay(conn, head, body.idempotency_key, "revising", rhash)
    if replayed:
        return replayed
    if body.expected_generation != head["generation"]:
        await _stale(conn, head, "generation_conflict")
    if head["status"] != "changes_requested":
        fail(409, "invalid_state", status=head["status"], required="changes_requested")
    ids = await conn.fetch(
        "UPDATE mockup_review_change_requests SET status='in_revision' WHERE head_id=$1 AND status='open' "
        "RETURNING change_request_id", head["id"])
    generation = head["generation"] + 1
    await _update_head(conn, head["id"], status="revising", generation=generation)
    batch = sorted(str(r["change_request_id"]) for r in ids)
    _, response = await _event(
        conn, head, "revising_started", actor, generation_after=generation, revision_id=head["latest_revision_id"],
        key=body.idempotency_key, rhash=rhash, payload={"change_request_ids": batch},
        build_response=lambda _eid: {"review_id": str(head["id"]), "status": "revising", "generation": generation,
                                     "change_request_ids": batch, "idempotent": False})
    return response


async def submit_review(conn: Any, tenant: str, project: str, review_id: UUID, actor: str, body: SubmitReview) -> dict:
    head = await _lock(conn, tenant, project, review_id)
    rhash = request_hash("submit", body.model_dump(mode="json"))
    replayed = await _replay(conn, head, body.idempotency_key, "submit", rhash)
    if replayed:
        return replayed
    if body.expected_generation != head["generation"] or head["latest_revision_id"] != body.revision_id:
        await _stale(conn, head, "stale_revision")
    if head["status"] != "draft":
        fail(409, "invalid_state", status=head["status"], required="draft")
    revision = await conn.fetchrow("SELECT * FROM mockup_review_revisions WHERE id=$1", head["latest_revision_id"])
    manifest = _json(revision["manifest"])
    changed = await recheck_assets(manifest)
    if changed:
        fail(409, "asset_tampered", assets=changed)
    doc_refs = await validate_doc_refs(conn, tenant, project, _json(revision["doc_refs"]), require_current=True)
    issues = completeness(manifest, doc_refs)
    if issues:
        fail(422, "missing_artifacts", missing=issues, blocked_evidence=any(i["code"] == "blocked_evidence" for i in issues))
    generation = head["generation"] + 1
    await _update_head(conn, head["id"], status="review_ready", generation=generation)
    _, response = await _event(
        conn, head, "submitted", actor, generation_after=generation, revision_id=revision["id"],
        key=body.idempotency_key, rhash=rhash, payload={"manifest_hash": revision["manifest_hash"]},
        build_response=lambda _eid: {"review_id": str(head["id"]), "revision_id": str(revision["id"]),
                                     "revision": revision["revision"], "manifest_hash": revision["manifest_hash"],
                                     "status": "review_ready", "generation": generation, "approved": False,
                                     "idempotent": False})
    return response


async def request_changes(conn: Any, tenant: str, project: str, review_id: UUID, actor: str, elevated: bool,
                          body: ChangeCreate) -> dict:
    comment = body.comment
    if not comment.strip():
        fail(422, "empty_comment")
    if SECRET.search(comment):
        fail(422, "credential_content_rejected")
    head = await _lock(conn, tenant, project, review_id)
    rhash = request_hash("changes", body.model_dump(mode="json"))
    replayed = await _replay(conn, head, body.idempotency_key, "changes", rhash)
    if replayed:
        return replayed
    existing = await conn.fetchrow(
        "SELECT * FROM mockup_review_change_requests WHERE head_id=$1 AND change_request_id=$2",
        head["id"], body.change_request_id)
    if existing:
        if (existing["comment"], existing["source_message_id"], existing["base_revision_id"], existing["screen_id"]) != (
                comment, body.source_message_id, body.base_revision_id, body.screen_id):
            fail(409, "change_request_conflict")
        return {"review_id": str(head["id"]), "change_request_id": str(body.change_request_id),
                "status": existing["status"], "generation": head["generation"], "idempotent": True}
    if head["status"] not in ("review_ready", "changes_requested", "revising", "approved"):
        fail(409, "invalid_state", status=head["status"], required="submitted revision")
    if (body.base_revision_id != head["latest_revision_id"]
            or not head["pointer_generation"] <= body.expected_generation <= head["generation"]):
        await _stale(conn, head, "stale_revision")

    base = await conn.fetchrow("SELECT manifest FROM mockup_review_revisions WHERE id=$1", body.base_revision_id)
    if body.screen_id and body.screen_id not in {s["screen_id"] for s in _json(base["manifest"])["screens"]}:
        fail(422, "unknown_screen", screen_id=body.screen_id)
    message = await conn.fetchrow(
        "SELECT session_id,role FROM chat_messages WHERE id=$1 AND tenant_id=$2::uuid AND deleted_at IS NULL",
        body.source_message_id, tenant)
    if not message:
        fail(404, "source_message_not_found")
    if head["session_id"] and head["session_id"] != message["session_id"]:
        fail(422, "source_message_session_mismatch")
    await _session_scope(conn, tenant, project, message["session_id"], actor, elevated)
    if message["role"] != "user":
        fail(422, "source_message_not_user_message")

    queued = head["status"] == "revising"
    await conn.execute(
        "INSERT INTO mockup_review_change_requests(change_request_id,head_id,tenant_id,project_key,source_message_id,"
        "session_id,base_revision_id,screen_id,comment,requested_by,idempotency_key,queued) "
        "VALUES($1,$2,$3::uuid,$4,$5,$6,$7,$8,$9,$10,$11,$12)",
        body.change_request_id, head["id"], tenant, project, body.source_message_id, message["session_id"],
        body.base_revision_id, body.screen_id, comment, actor, body.idempotency_key, queued)
    held = 0
    generation = head["generation"]
    status = head["status"]
    if head["status"] in ("review_ready", "approved"):
        generation += 1
        status = "changes_requested"
        held = await _hold_bindings(conn, head["id"])
        await _update_head(conn, head["id"], status=status, generation=generation)
    position = await conn.fetchval(
        "SELECT count(*) FROM mockup_review_change_requests WHERE head_id=$1 AND status<>'resolved'", head["id"])
    _, response = await _event(
        conn, head, "changes_requested", actor, generation_after=generation, revision_id=body.base_revision_id,
        key=body.idempotency_key, rhash=rhash,
        payload={"change_request_id": str(body.change_request_id), "queued": queued, "bindings_held": held},
        build_response=lambda _eid: {
            "review_id": str(head["id"]), "change_request_id": str(body.change_request_id),
            "source_message_id": str(body.source_message_id), "base_revision_id": str(body.base_revision_id),
            "status": status, "request_status": "open", "queued": queued, "pending_change_requests": position,
            "generation": generation, "approved": False, "implementation_command": False,
            "bindings_held": held, "idempotent": False})
    return response


async def _approval_gate(conn: Any, tenant: str, project: str, revision: Any) -> None:
    manifest = _json(revision["manifest"])
    changed = await recheck_assets(manifest)
    if changed:
        fail(409, "asset_tampered", assets=changed)
    doc_refs = await validate_doc_refs(conn, tenant, project, _json(revision["doc_refs"]), require_current=True)
    issues = completeness(manifest, doc_refs)
    if issues:
        fail(422, "missing_artifacts", missing=issues, blocked_evidence=any(i["code"] == "blocked_evidence" for i in issues))


async def approve_review(conn: Any, tenant: str, project: str, review_id: UUID, actor: str, body: ApproveReview) -> dict:
    head = await _lock(conn, tenant, project, review_id)
    rhash = request_hash("approve", body.model_dump(mode="json"))
    replayed = await _replay(conn, head, body.idempotency_key, "approve", rhash)
    if replayed:
        return replayed
    revision = await conn.fetchrow("SELECT * FROM mockup_review_revisions WHERE id=$1 AND head_id=$2",
                                   head["latest_revision_id"], head["id"]) if head["latest_revision_id"] else None
    if (revision is None or revision["id"] != body.revision_id or revision["manifest_hash"] != body.manifest_hash
            or body.expected_generation != head["generation"]):
        await _stale(conn, head, "stale_revision")
    if head["status"] != "review_ready":
        fail(409, "invalid_state", status=head["status"], required="review_ready")
    await _approval_gate(conn, tenant, project, revision)
    generation = head["generation"] + 1
    held = await _hold_bindings(conn, head["id"]) if head["approved_revision_id"] else 0
    event_id, response = await _event(
        conn, head, "approved", actor, generation_after=generation, revision_id=revision["id"],
        key=body.idempotency_key, rhash=rhash,
        payload={"manifest_hash": revision["manifest_hash"], "previous_approved_revision_id": head["approved_revision_id"],
                 "bindings_held": held},
        build_response=lambda eid: {"review_id": str(head["id"]), "approval_id": eid,
                                    "revision_id": str(revision["id"]), "revision": revision["revision"],
                                    "manifest_hash": revision["manifest_hash"], "status": "approved",
                                    "generation": generation, "approved_by": actor, "bindings_held": held,
                                    "idempotent": False})
    await _update_head(conn, head["id"], status="approved", approved_revision_id=revision["id"],
                       approval_event_id=event_id, generation=generation)
    return response


async def revoke_review(conn: Any, tenant: str, project: str, review_id: UUID, actor: str, body: RevokeReview) -> dict:
    head = await _lock(conn, tenant, project, review_id)
    rhash = request_hash("revoke", body.model_dump(mode="json"))
    replayed = await _replay(conn, head, body.idempotency_key, "revoke", rhash)
    if replayed:
        return replayed
    if body.expected_generation != head["generation"]:
        await _stale(conn, head, "generation_conflict")
    if head["approved_revision_id"] is None:
        fail(409, "not_approved", status=head["status"])
    if head["approval_event_id"] != body.approval_id:
        await _stale(conn, head, "stale_approval", approval_id=head["approval_event_id"])
    revoked = head["approved_revision_id"]
    status = "revoked" if head["status"] == "approved" else head["status"]
    generation = head["generation"] + 1
    held = await _hold_bindings(conn, head["id"])
    _, response = await _event(
        conn, head, "revoked", actor, generation_after=generation, revision_id=revoked, key=body.idempotency_key,
        rhash=rhash, payload={"approval_id": body.approval_id, "reason": body.reason, "bindings_held": held},
        build_response=lambda _eid: {"review_id": str(head["id"]), "revoked_revision_id": str(revoked),
                                     "approval_id": body.approval_id, "status": status, "generation": generation,
                                     "bindings_held": held, "idempotent": False})
    await _update_head(conn, head["id"], status=status, approved_revision_id=None, approval_event_id=None,
                       generation=generation)
    return response


async def verify_bundle(conn: Any, tenant: str, project: str, review_id: UUID, actor: str, body: VerifyBundle) -> dict:
    """Execution-time gate. Returns {"allowed": bool, ...}; a denial is audited and the caller answers 409."""
    head = await _lock(conn, tenant, project, review_id)
    reasons: list[str] = []
    detail: dict[str, Any] = {}
    in_scope = await conn.fetchval(
        "SELECT 1 FROM goal_task_links l LEFT JOIN milestones m ON m.id=l.milestone_id "
        "JOIN goals g ON g.id=COALESCE(l.goal_id,m.goal_id) WHERE l.task_id=$1 AND l.tenant_id=$2::uuid AND l.link_state='active' "
        "AND g.tenant_id=$2::uuid AND g.project=$3 AND ($4::uuid IS NULL OR g.id=$4::uuid) LIMIT 1",
        body.task_id, tenant, project, head["goal_id"])
    if not in_scope:
        reasons.append("task_out_of_scope")
    approved = head["approved_revision_id"]
    if approved is None:
        reasons.append("approval_revoked" if head["status"] == "revoked" else "not_approved")
    elif approved != body.revision_id:
        reasons.append("revision_mismatch")
    elif approved != head["latest_revision_id"]:
        reasons.append("superseded_by_newer_revision")
    elif head["status"] in ("changes_requested", "revising"):
        reasons.append("change_requested_hold")
    elif head["status"] != "approved":
        reasons.append("not_approved")
    revision = await conn.fetchrow("SELECT * FROM mockup_review_revisions WHERE id=$1 AND head_id=$2",
                                   body.revision_id, head["id"])
    if revision is None:
        reasons.append("revision_not_found")
    elif revision["manifest_hash"] != body.manifest_hash:
        reasons.append("manifest_hash_mismatch")
    if not reasons:
        changed = await recheck_assets(_json(revision["manifest"]))
        if changed:
            reasons.append("asset_tampered")
            detail["assets"] = changed
        try:
            await validate_doc_refs(conn, tenant, project, _json(revision["doc_refs"]), require_current=True)
        except HTTPException as exc:
            reasons.append("document_revision_changed")
            detail["document"] = exc.detail
    binding = None
    if not reasons:
        binding = await conn.fetchrow(
            "SELECT * FROM mockup_review_task_bindings WHERE head_id=$1 AND revision_id=$2 AND task_id=$3",
            head["id"], body.revision_id, body.task_id)
        if body.phase == "checkpoint" and (binding is None or binding["status"] != "running"):
            reasons.append("binding_missing" if binding is None else "binding_held")
    if reasons:
        event_id, _ = await _event(
            conn, head, "verify_denied", actor, generation_after=head["generation"], revision_id=body.revision_id
            if revision is not None else None,
            payload={"task_id": body.task_id, "phase": body.phase, "reasons": reasons, **detail})
        return {"allowed": False, "code": "approval_required", "reasons": reasons, "audit_event_id": event_id,
                "status": head["status"], "generation": head["generation"], **detail}
    target = "bound" if body.phase == "submit" else "running"
    if binding is None:
        await conn.execute(
            "INSERT INTO mockup_review_task_bindings(tenant_id,project_key,head_id,revision_id,approval_event_id,"
            "task_id,status,bound_by) VALUES($1::uuid,$2,$3,$4,$5,$6,$7,$8)",
            tenant, project, head["id"], body.revision_id, head["approval_event_id"], body.task_id, target, actor)
    else:
        target = "running" if body.phase != "submit" else binding["status"]
        await conn.execute("UPDATE mockup_review_task_bindings SET status=$2,last_verified_at=now(),updated_at=now() "
                           "WHERE id=$1", binding["id"], target)
    if binding is None or binding["status"] != target:
        await _event(conn, head, "verify_allowed", actor, generation_after=head["generation"],
                     revision_id=body.revision_id,
                     payload={"task_id": body.task_id, "phase": body.phase, "binding_status": target})
    return {"allowed": True, "verified": True, "review_id": str(head["id"]), "revision_id": str(body.revision_id),
            "manifest_hash": body.manifest_hash, "approval_id": head["approval_event_id"],
            "approval_generation": head["generation"], "binding_status": target,
            "expires_at": (datetime.now(timezone.utc) + VERIFY_TTL).isoformat()}


# -------------------------------------------------------------------- queries

REVISION_STATUS_SQL = (
    "SELECT r.id,r.revision,r.parent_revision_id,r.manifest_hash,r.pointer_applied,r.created_by,r.created_at,"
    "CASE WHEN NOT r.pointer_applied THEN 'archived_stale' "
    "ELSE COALESCE((SELECT CASE e.action WHEN 'submitted' THEN 'review_ready' WHEN 'approved' THEN 'approved' "
    "WHEN 'revoked' THEN 'revoked' WHEN 'changes_requested' THEN 'changes_requested' "
    "WHEN 'revising_started' THEN 'revising' END FROM mockup_review_events e WHERE e.revision_id=r.id "
    "AND e.action IN ('submitted','approved','revoked','changes_requested','revising_started') "
    "ORDER BY e.id DESC LIMIT 1),'draft') END AS status,"
    "(r.id=h.latest_revision_id) AS is_latest,(r.id=h.approved_revision_id) AS is_approved "
    "FROM mockup_review_revisions r JOIN mockup_review_heads h ON h.id=r.head_id "
)


def _rev_row(row: Any, project: str, review_id: Any) -> dict:
    return {"revision_id": str(row["id"]), "revision": row["revision"], "status": row["status"],
            "parent_revision_id": str(row["parent_revision_id"]) if row["parent_revision_id"] else None,
            "manifest_hash": row["manifest_hash"], "pointer_applied": row["pointer_applied"],
            "is_latest": row["is_latest"], "is_approved": row["is_approved"], "created_by": row["created_by"],
            "created_at": row["created_at"].isoformat(),
            "url": revision_url(project, review_id, row["id"])}


def revision_url(project: str, review_id: Any, revision_id: Any) -> str:
    return f"/api/v1/projects/{project}/mockup-reviews/{review_id}/revisions/{revision_id}"


def _change_row(row: Any) -> dict:
    out = {k: (str(v) if isinstance(v, UUID) else v.isoformat() if isinstance(v, datetime) else v)
           for k, v in dict(row).items() if k not in ("tenant_id", "project_key", "idempotency_key")}
    out["sequence"] = out.pop("id")
    return out


async def get_review(conn: Any, tenant: str, project: str, review_id: UUID) -> dict:
    head = await conn.fetchrow(
        "SELECT * FROM mockup_review_heads WHERE id=$1 AND tenant_id=$2::uuid AND project_key=$3",
        review_id, tenant, project)
    if not head:
        fail(404, "review_not_found")
    revisions = await conn.fetch(REVISION_STATUS_SQL + "WHERE r.head_id=$1 ORDER BY r.revision DESC LIMIT 200",
                                 head["id"])
    changes = await conn.fetch("SELECT * FROM mockup_review_change_requests WHERE head_id=$1 ORDER BY id", head["id"])
    events = await conn.fetch(
        "SELECT id,action,revision_id,actor_id,generation_after,payload,created_at FROM mockup_review_events "
        "WHERE head_id=$1 ORDER BY id DESC LIMIT 100", head["id"])
    view = await head_view(conn, head)
    return {**view, "revisions": [_rev_row(r, project, head["id"]) for r in revisions],
            "change_requests": [_change_row(c) for c in changes],
            "unresolved_change_requests": [_change_row(c) for c in changes if c["status"] != "resolved"],
            "events": [{"event_id": e["id"], "action": e["action"],
                        "revision_id": str(e["revision_id"]) if e["revision_id"] else None, "actor_id": e["actor_id"],
                        "generation_after": e["generation_after"], "payload": _json(e["payload"]),
                        "created_at": e["created_at"].isoformat()} for e in events]}


async def get_revision(conn: Any, tenant: str, project: str, review_id: UUID, revision_id: UUID) -> dict:
    row = await conn.fetchrow(
        REVISION_STATUS_SQL.replace("SELECT r.id,", "SELECT r.manifest,r.doc_refs,r.design_tokens_version,"
                                    "r.source_sha,r.id,", 1)
        + "WHERE r.id=$1 AND r.head_id=$2 AND h.tenant_id=$3::uuid AND h.project_key=$4",
        revision_id, review_id, tenant, project)
    if not row:
        fail(404, "revision_not_found")
    manifest = _json(row["manifest"])
    base = revision_url(project, review_id, revision_id)
    for asset in manifest["assets"]:
        asset["url"] = f"{base}/assets/{asset['asset_id']}"
    return {**_rev_row(row, project, review_id), "manifest": manifest, "doc_refs": _json(row["doc_refs"]),
            "design_tokens_version": row["design_tokens_version"], "source_sha": row["source_sha"]}


async def get_asset(conn: Any, tenant: str, project: str, review_id: UUID, revision_id: UUID,
                    asset_id: str) -> tuple[bytes, str]:
    row = await conn.fetchrow(
        "SELECT r.manifest FROM mockup_review_revisions r JOIN mockup_review_heads h ON h.id=r.head_id "
        "WHERE r.id=$1 AND r.head_id=$2 AND h.tenant_id=$3::uuid AND h.project_key=$4",
        revision_id, review_id, tenant, project)
    if not row:
        fail(404, "revision_not_found")
    asset = next((a for a in _json(row["manifest"])["assets"] if a["asset_id"] == asset_id), None)
    if asset is None:
        fail(404, "asset_not_found")
    try:
        data = await asyncio.to_thread(read_asset, asset["uri"])
    except AssetProblem as exc:
        fail(409, "asset_tampered", assets=[{"asset_id": asset_id, "reason": exc.code}])
    if sha256_hex(data) != asset["sha256"]:
        fail(409, "asset_tampered", assets=[{"asset_id": asset_id, "reason": "hash_changed"}])
    return data, asset["mime"]


async def change_report(conn: Any, tenant: str, project: str, review_id: UUID) -> dict:
    """Deterministic, queryable change report: original text, links, outcome and reason per request."""
    review = await get_review(conn, tenant, project, review_id)
    numbers = {r["revision_id"]: r["revision"] for r in review["revisions"]}
    url = lambda rid: revision_url(project, review["review_id"], rid)  # noqa: E731
    items = []
    for c in review["change_requests"]:
        resolved = c["resolved_revision_id"]
        items.append({
            "change_request_id": c["change_request_id"], "sequence": c["sequence"],
            "source_message_id": c["source_message_id"], "requested_by": c["requested_by"],
            "requested_at": c["created_at"], "screen_id": c["screen_id"], "queued": c["queued"],
            "original_text": c["comment"], "status": c["status"],
            "base_revision": {"revision_id": c["base_revision_id"], "revision": numbers.get(c["base_revision_id"]),
                              "url": url(c["base_revision_id"])},
            "new_revision": {"revision_id": resolved, "revision": numbers.get(resolved), "url": url(resolved)}
            if resolved else None,
            "outcome": c["outcome"], "outcome_reason": c["outcome_reason"], "resolved_at": c["resolved_at"],
        })
    lines = [f"# 목업 수정 보고 — {review['title']}", "",
             f"- review_id: `{review['review_id']}` · 상태: `{review['status']}` · generation {review['generation']}",
             f"- 최신 revision: {review['latest_revision']['revision'] if review['latest_revision'] else '없음'}"
             f" · 승인본: {review['approved_revision']['revision'] if review['approved_revision'] else '없음'}"
             " (수정 요청은 승인도 구현 명령도 아니다)", ""]
    for heading, test in (("반영", lambda i: i["outcome"] == "applied"), ("미반영", lambda i: i["outcome"] == "not_applied"),
                          ("대기·진행 중", lambda i: i["outcome"] is None)):
        group = [i for i in items if test(i)]
        lines += [f"## {heading} ({len(group)})", ""]
        for i in group:
            lines.append(f"### 요청 {i['sequence']} · `{i['change_request_id']}`")
            lines.append(f"- 요청 메시지: `{i['source_message_id']}` · 접수 {i['requested_at']} · 화면 {i['screen_id'] or '전체'}")
            lines.append(f"- 기준 revision: [r{i['base_revision']['revision']}]({i['base_revision']['url']})")
            if i["new_revision"]:
                lines.append(f"- 신규 revision: [r{i['new_revision']['revision']}]({i['new_revision']['url']})")
            if i["outcome"]:
                lines.append(f"- 결과: {i['outcome']} — {i['outcome_reason']}")
            lines += ["- 원문:", *("  > " + part for part in i["original_text"].replace("\r", "").split("\n")), ""]
    lines += ["## Revision 목록", "", *(f"- [r{r['revision']}]({r['url']}) · {r['status']} · `{r['manifest_hash'][:12]}`"
                                       for r in sorted(review["revisions"], key=lambda r: r["revision"]))]
    return {"review_id": review["review_id"], "status": review["status"], "generation": review["generation"],
            "change_requests": items, "revisions": review["revisions"], "markdown": "\n".join(lines) + "\n"}
