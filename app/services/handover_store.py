"""Tenant-scoped, project-global handover ledger service.

The database is the source of truth. Markdown remains a reversible compatibility
format for people, Git history, and existing project document viewers.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Iterable

import asyncpg

from app.core.project_config import normalize_project_label

ENTRY_TYPES = frozenset({"status", "decision", "task", "risk", "verification", "note"})
ENTRY_STATUSES = frozenset({"active", "resolved", "superseded", "archived"})
ENTRY_PRIORITIES = frozenset({"P0", "P1", "P2", "P3"})
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$", re.MULTILINE)
_KEY_RE = re.compile(r"[^a-z0-9._-]+")
_PROJECT_KEY_RE = re.compile(r"^[A-Z0-9][A-Z0-9_.-]{0,63}$")
MAX_BODY_CHARS = 1_000_000
MAX_METADATA_BYTES = 100_000


class HandoverConflictError(RuntimeError):
    """Raised when optimistic revision control detects a stale writer."""


class HandoverNotFoundError(LookupError):
    """Raised when an entry does not exist inside the requested tenant."""


def normalize_project_key(value: str) -> str:
    normalized = normalize_project_label(value)
    if not normalized:
        raise ValueError("project_key is required")
    normalized = str(normalized).upper()
    if not _PROJECT_KEY_RE.fullmatch(normalized):
        raise ValueError("project_key must contain only A-Z, 0-9, dot, underscore, or hyphen")
    return normalized


def make_entry_key(title: str, *, source_path: str | None = None, ordinal: int = 0) -> str:
    title_slug = _KEY_RE.sub("-", title.strip().lower()).strip("-")[:80] or "entry"
    material = f"{source_path or ''}\n{ordinal}\n{title.strip()}"
    suffix = hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]
    return f"{title_slug}-{suffix}"


def parse_markdown_sections(content: str, *, source_path: str | None = None) -> list[dict[str, Any]]:
    """Split legacy Markdown into stable, idempotently importable records."""
    text = (content or "").replace("\r\n", "\n").strip()
    if not text:
        return []
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        title = os.path.basename(source_path or "HANDOVER.md")
        return [{"entry_key": make_entry_key(title, source_path=source_path), "title": title, "body": text}]

    sections: list[dict[str, Any]] = []
    preamble = text[: matches[0].start()].strip()
    if preamble:
        title = os.path.basename(source_path or "HANDOVER.md") + " preamble"
        sections.append({"entry_key": make_entry_key(title, source_path=source_path), "title": title, "body": preamble})

    for ordinal, match in enumerate(matches, start=1):
        end = matches[ordinal].start() if ordinal < len(matches) else len(text)
        title = match.group(2).strip()
        body = text[match.end() : end].strip()
        if not body and match.group(1) == "#":
            continue
        sections.append(
            {
                "entry_key": make_entry_key(title, source_path=source_path, ordinal=ordinal),
                "title": title,
                "body": body or title,
            }
        )
    return sections


def render_handover_markdown(
    entries: Iterable[dict[str, Any]],
    *,
    project_key: str,
    generated_at: datetime | None = None,
) -> str:
    timestamp = (generated_at or datetime.now(timezone.utc)).astimezone().isoformat(timespec="seconds")
    lines = [
        f"# {normalize_project_key(project_key)} HANDOVER",
        "",
        "> 자동 생성 문서입니다. 정본은 AADS 중앙 handover ledger이며 이 파일을 직접 편집하지 마십시오.",
        f"> Generated: {timestamp}",
        "",
    ]
    for entry in entries:
        title = str(entry.get("title") or "Untitled")
        status = str(entry.get("status") or "active")
        priority = str(entry.get("priority") or "P2")
        entry_type = str(entry.get("entry_type") or "note")
        revision = int(entry.get("revision") or 1)
        lines.extend(
            [
                f"## [{priority}] {title}",
                "",
                f"- 상태: `{status}`",
                f"- 유형: `{entry_type}`",
                f"- 리비전: `{revision}`",
                f"- 키: `{entry.get('entry_key', '')}`",
                "",
                str(entry.get("body") or "").rstrip(),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def serialize_entry(row: Any) -> dict[str, Any]:
    item = dict(row)
    item.pop("search_vector", None)
    item["id"] = str(item["id"])
    item["tenant_id"] = str(item["tenant_id"])
    if item.get("source_session_id") is not None:
        item["source_session_id"] = str(item["source_session_id"])
    item["metadata"] = _json_object(item.get("metadata"))
    for key in ("created_at", "updated_at", "resolved_at"):
        if item.get(key) is not None and hasattr(item[key], "isoformat"):
            item[key] = item[key].isoformat()
    return item


@asynccontextmanager
async def _connection(pool: Any | None = None) -> AsyncIterator[Any]:
    if pool is not None:
        async with pool.acquire() as conn:
            yield conn
        return
    try:
        from app.core.db_pool import get_pool

        shared_pool = get_pool()
    except RuntimeError:
        shared_pool = None
    if shared_pool is not None:
        async with shared_pool.acquire() as conn:
            yield conn
        return

    dsn = os.getenv("DATABASE_URL", "")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not configured")
    conn = await asyncpg.connect(dsn=dsn, timeout=5)
    try:
        yield conn
    finally:
        await conn.close()


async def upsert_handover_entry(
    *,
    tenant_id: str,
    project_key: str,
    title: str,
    body: str,
    entry_key: str | None = None,
    entry_type: str = "note",
    summary: str | None = None,
    status: str = "active",
    priority: str = "P2",
    source_kind: str = "api",
    source_session_id: str | None = None,
    source_task_id: str | None = None,
    source_path: str | None = None,
    metadata: dict[str, Any] | None = None,
    changed_by: str | None = None,
    change_summary: str | None = None,
    expected_revision: int | None = None,
    event_type: str | None = None,
    pool: Any | None = None,
) -> tuple[dict[str, Any], bool]:
    project = normalize_project_key(project_key)
    title = title.strip()
    body = body.strip()
    if not title or not body:
        raise ValueError("title and body are required")
    if len(title) > 300:
        raise ValueError("title is too long")
    if len(body) > MAX_BODY_CHARS:
        raise ValueError(f"body exceeds {MAX_BODY_CHARS} characters")
    if summary is not None and len(summary) > 2_000:
        raise ValueError("summary is too long")
    if entry_type not in ENTRY_TYPES:
        raise ValueError(f"unsupported entry_type: {entry_type}")
    if status not in ENTRY_STATUSES:
        raise ValueError(f"unsupported status: {status}")
    priority = priority.upper()
    if priority not in ENTRY_PRIORITIES:
        raise ValueError(f"unsupported priority: {priority}")
    stable_key = (entry_key or make_entry_key(title, source_path=source_path)).strip()[:200]
    if not stable_key:
        raise ValueError("entry_key cannot be blank")
    metadata_obj = metadata or {}
    if len(json.dumps(metadata_obj, ensure_ascii=False).encode("utf-8")) > MAX_METADATA_BYTES:
        raise ValueError(f"metadata exceeds {MAX_METADATA_BYTES} bytes")

    async with _connection(pool) as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                """
                SELECT * FROM project_handover_entries
                 WHERE tenant_id = $1::uuid AND project_key = $2 AND entry_key = $3
                 FOR UPDATE
                """,
                tenant_id,
                project,
                stable_key,
            )
            if current is not None and expected_revision is not None and current["revision"] != expected_revision:
                raise HandoverConflictError(
                    f"revision conflict: expected {expected_revision}, current {current['revision']}"
                )

            current_obj = serialize_entry(current) if current is not None else None
            comparable = {
                "title": title,
                "summary": summary,
                "body": body,
                "entry_type": entry_type,
                "status": status,
                "priority": priority,
                "source_kind": source_kind,
                "source_session_id": source_session_id,
                "source_task_id": source_task_id,
                "source_path": source_path,
                "metadata": metadata_obj,
            }
            if current_obj is not None and all(current_obj.get(key) == value for key, value in comparable.items()):
                return current_obj, False

            if current is None:
                row = await conn.fetchrow(
                    """
                    INSERT INTO project_handover_entries (
                        tenant_id, project_key, entry_key, entry_type, title, summary, body,
                        status, priority, source_kind, source_session_id, source_task_id,
                        source_path, metadata, created_by, resolved_at
                    ) VALUES (
                        $1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                        NULLIF($11, '')::uuid, $12, $13, $14::jsonb, $15,
                        CASE WHEN $8 = 'resolved' THEN NOW() ELSE NULL END
                    )
                    RETURNING *
                    """,
                    tenant_id, project, stable_key, entry_type, title, summary, body,
                    status, priority, source_kind, source_session_id or "", source_task_id,
                    source_path, json.dumps(metadata_obj, ensure_ascii=False), changed_by,
                )
                resolved_event_type = event_type or ("imported" if source_kind == "markdown_import" else "created")
            else:
                previous_status = current["status"]
                row = await conn.fetchrow(
                    """
                    UPDATE project_handover_entries SET
                        entry_type=$4, title=$5, summary=$6, body=$7, status=$8,
                        priority=$9, source_kind=$10, source_session_id=NULLIF($11, '')::uuid,
                        source_task_id=$12, source_path=$13, metadata=$14::jsonb,
                        revision=revision + 1, updated_at=NOW(),
                        resolved_at=CASE WHEN $8='resolved' THEN COALESCE(resolved_at, NOW()) ELSE NULL END
                    WHERE tenant_id=$1::uuid AND project_key=$2 AND entry_key=$3
                    RETURNING *
                    """,
                    tenant_id, project, stable_key, entry_type, title, summary, body,
                    status, priority, source_kind, source_session_id or "", source_task_id,
                    source_path, json.dumps(metadata_obj, ensure_ascii=False),
                )
                resolved_event_type = event_type or ("status_changed" if previous_status != status else "updated")

            serialized = serialize_entry(row)
            await conn.execute(
                """
                INSERT INTO project_handover_events (
                    entry_id, tenant_id, project_key, event_type, revision,
                    snapshot, change_summary, changed_by
                ) VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6::jsonb, $7, $8)
                """,
                serialized["id"], tenant_id, project, resolved_event_type,
                serialized["revision"], json.dumps(serialized, ensure_ascii=False),
                change_summary, changed_by,
            )
            return serialized, True


async def list_handover_entries(
    *,
    tenant_id: str,
    project_key: str | None = None,
    status: str | None = None,
    entry_type: str | None = None,
    query: str | None = None,
    limit: int = 100,
    offset: int = 0,
    pool: Any | None = None,
) -> tuple[list[dict[str, Any]], int]:
    conditions = ["tenant_id = $1::uuid"]
    params: list[Any] = [tenant_id]
    if project_key:
        params.append(normalize_project_key(project_key))
        conditions.append(f"project_key = ${len(params)}")
    if status:
        if status not in ENTRY_STATUSES:
            raise ValueError(f"unsupported status: {status}")
        params.append(status)
        conditions.append(f"status = ${len(params)}")
    if entry_type:
        if entry_type not in ENTRY_TYPES:
            raise ValueError(f"unsupported entry_type: {entry_type}")
        params.append(entry_type)
        conditions.append(f"entry_type = ${len(params)}")
    search_param = 0
    if query and query.strip():
        params.append(query.strip())
        search_param = len(params)
        conditions.append(
            f"(search_vector @@ websearch_to_tsquery('simple', ${search_param}) "
            f"OR similarity(LOWER(title), LOWER(${search_param})) >= 0.25 "
            f"OR LOWER(COALESCE(source_path, '')) % LOWER(${search_param}))"
        )
    where = " AND ".join(conditions)
    rank = (
        f"ts_rank(search_vector, websearch_to_tsquery('simple', ${search_param})) + "
        f"similarity(LOWER(title), LOWER(${search_param}))"
        if search_param else "0"
    )
    params.extend([max(1, min(limit, 500)), max(0, offset)])
    limit_param = len(params) - 1
    offset_param = len(params)
    async with _connection(pool) as conn:
        rows = await conn.fetch(
            f"""
            SELECT *, {rank} AS search_rank
              FROM project_handover_entries
             WHERE {where}
             ORDER BY search_rank DESC, updated_at DESC
             LIMIT ${limit_param} OFFSET ${offset_param}
            """,
            *params,
        )
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM project_handover_entries WHERE {where}",
            *params[:-2],
        )
    items = []
    for row in rows:
        item = serialize_entry(row)
        item.pop("search_rank", None)
        items.append(item)
    return items, int(total or 0)


async def get_handover_entry(*, tenant_id: str, entry_id: str, pool: Any | None = None) -> dict[str, Any]:
    async with _connection(pool) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM project_handover_entries WHERE id=$1::uuid AND tenant_id=$2::uuid",
            entry_id,
            tenant_id,
        )
    if row is None:
        raise HandoverNotFoundError(entry_id)
    return serialize_entry(row)


async def list_handover_events(*, tenant_id: str, entry_id: str, pool: Any | None = None) -> list[dict[str, Any]]:
    async with _connection(pool) as conn:
        rows = await conn.fetch(
            """
            SELECT id, entry_id, project_key, event_type, revision, snapshot,
                   change_summary, changed_by, created_at
              FROM project_handover_events
             WHERE entry_id=$1::uuid AND tenant_id=$2::uuid
             ORDER BY revision DESC
            """,
            entry_id,
            tenant_id,
        )
    result = []
    for row in rows:
        item = dict(row)
        item["entry_id"] = str(item["entry_id"])
        item["snapshot"] = _json_object(item.get("snapshot"))
        if hasattr(item.get("created_at"), "isoformat"):
            item["created_at"] = item["created_at"].isoformat()
        result.append(item)
    return result
