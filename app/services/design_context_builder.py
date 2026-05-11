"""Design Context Pack builder for Design Modification Studio."""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from app.core.db_pool import get_pool


DEFAULT_VIEWPORT_MATRIX = ["390x844", "768x1024", "1440x900"]
DESIGN_MD_CANDIDATES = ("DESIGN.md", "docs/DESIGN.md", "docs/design/DESIGN.md")
MAX_DESIGN_MD_CHARS = 40000

_SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|auth[_-]?token|access[_-]?token|refresh[_-]?token|secret|password|credential|authorization|bearer)",
    re.IGNORECASE,
)
_ENV_SECRET_LINE_RE = re.compile(
    r"(?im)^([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)[A-Z0-9_]*)\s*=\s*(.+)$"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}")
_TOKEN_LIKE_RE = re.compile(
    r"\b(?:sk|pk|rk|ghp|github_pat|xox[baprs])[-_A-Za-z0-9]{16,}\b"
)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")


class DesignContextRequestNotFound(Exception):
    """Raised when the modification request does not exist."""


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _to_iso(value: Any) -> str | None:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is None:
        return None
    return str(value)


def _coerce_json(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return fallback
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return fallback
        return parsed if isinstance(parsed, type(fallback)) else fallback
    return fallback


def _coerce_dict(value: Any) -> dict[str, Any]:
    parsed = _coerce_json(value, {})
    return parsed if isinstance(parsed, dict) else {}


def _coerce_list(value: Any) -> list[Any]:
    parsed = _coerce_json(value, [])
    return parsed if isinstance(parsed, list) else []


def _redact_text(value: str) -> str:
    redacted = _ENV_SECRET_LINE_RE.sub(r"\1=[redacted]", value)
    redacted = _BEARER_RE.sub("Bearer [redacted]", redacted)
    redacted = _JWT_RE.sub("[redacted-token]", redacted)
    redacted = _TOKEN_LIKE_RE.sub("[redacted-token]", redacted)
    return redacted


def _safe_value(value: Any) -> Any:
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            safe[key_text] = "[redacted]" if _SENSITIVE_KEY_RE.search(key_text) else _safe_value(item)
        return safe
    if isinstance(value, list):
        return [_safe_value(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _missing(key: str, message: str, severity: str = "warning") -> dict[str, str]:
    return {"key": key, "message": message, "severity": severity}


def _normalize_viewports(value: Any) -> list[str]:
    candidates: list[Any]
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list):
        candidates = value
    elif isinstance(value, dict):
        candidates = list(value.values())
    else:
        candidates = []

    viewports: list[str] = []
    for item in candidates:
        if isinstance(item, dict):
            text = item.get("viewport") or item.get("size") or item.get("name")
        else:
            text = item
        normalized = str(text or "").strip()
        if normalized and normalized not in viewports:
            viewports.append(normalized)
    return viewports


def _viewport_matrix(screen_metadata: dict[str, Any], snapshots: list[dict[str, Any]]) -> tuple[list[str], str]:
    for key in ("viewport_matrix", "viewports", "viewport"):
        viewports = _normalize_viewports(screen_metadata.get(key))
        if viewports:
            return viewports, "screen_metadata"

    snapshot_viewports = _normalize_viewports([snapshot.get("viewport") for snapshot in snapshots])
    if snapshot_viewports:
        return snapshot_viewports, "baseline_snapshots"

    return list(DEFAULT_VIEWPORT_MATRIX), "default"


def _read_design_md(repo_path: str | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not repo_path:
        return None, None

    root = Path(repo_path).expanduser().resolve(strict=False)
    for relative_path in DESIGN_MD_CANDIDATES:
        candidate = (root / relative_path).resolve(strict=False)
        if candidate != root and root not in candidate.parents:
            continue
        if not candidate.is_file():
            continue
        try:
            content = candidate.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        truncated = len(content) > MAX_DESIGN_MD_CHARS
        content = content[:MAX_DESIGN_MD_CHARS]
        design_md = {
            "path": candidate.as_posix(),
            "content": _redact_text(content),
            "truncated": truncated,
            "chars": len(content),
        }
        source = {
            "type": "design_md",
            "path": candidate.as_posix(),
            "truncated": truncated,
            "chars": len(content),
        }
        return design_md, source

    return None, None


def _serialize_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row.get("id")),
        "viewport": str(row.get("viewport") or ""),
        "image_url": str(row.get("image_url") or ""),
        "dom_summary": _safe_value(_coerce_json(row.get("dom_summary"), {})),
        "captured_at": _to_iso(row.get("captured_at")),
    }


def _serialize_context_pack_row(row: dict[str, Any], context: dict[str, Any], sources: list[Any], missing_context: list[Any]) -> dict[str, Any]:
    return {
        "id": str(row.get("id")),
        "request_id": str(row.get("request_id")),
        "context": context,
        "sources": sources,
        "missing_context": missing_context,
        "prompt_chars": int(row.get("prompt_chars") or 0),
        "created_at": _to_iso(row.get("created_at")),
    }


async def build_context_pack(request_id: UUID) -> dict[str, Any]:
    """Build and persist a Design Context Pack for a modification request draft."""
    pool = get_pool()

    async with pool.acquire() as conn:
        request_row = await conn.fetchrow(
            """
            SELECT r.id, r.project_key, r.screen_id, r.user_prompt, r.normalized_card,
                   r.request_type, r.allowed_scope, r.forbidden_scope, r.acceptance_criteria,
                   r.status, r.created_at, r.updated_at,
                   p.project_key AS project_project_key,
                   p.display_name AS project_display_name,
                   p.frontend_stack AS project_frontend_stack,
                   p.adapter_key AS project_adapter_key,
                   p.repo_path AS project_repo_path,
                   p.status AS project_status,
                   p.metadata AS project_metadata,
                   p.created_at AS project_created_at,
                   p.updated_at AS project_updated_at,
                   s.route AS screen_route,
                   s.name AS screen_name,
                   s.purpose AS screen_purpose,
                   s.primary_actions AS screen_primary_actions,
                   s.component_paths AS screen_component_paths,
                   s.metadata AS screen_metadata
            FROM design_modification_requests r
            LEFT JOIN design_projects p
                ON p.project_key = r.project_key
            LEFT JOIN design_screens s
                ON s.id = r.screen_id
            WHERE r.id = $1::uuid
            """,
            request_id,
        )
        if request_row is None:
            raise DesignContextRequestNotFound(str(request_id))

        request = dict(request_row)
        project_key = str(request.get("project_key") or "").strip().upper()
        repo_path = str(request.get("project_repo_path") or "").strip()

        snapshot_rows = await conn.fetch(
            """
            SELECT id, viewport, image_url, dom_summary, captured_at
            FROM design_visual_snapshots
            WHERE request_id = $1::uuid
              AND phase = 'before'
            ORDER BY captured_at DESC
            LIMIT 12
            """,
            request_id,
        )
        snapshots = [dict(row) for row in snapshot_rows]

        token_row = await conn.fetchrow(
            """
            SELECT id, version, mode, tokens, created_by, created_at
            FROM design_token_sets
            WHERE project_key = $1
            ORDER BY created_at DESC
            LIMIT 1
            """,
            project_key,
        )

        missing_context: list[dict[str, str]] = []
        sources: list[dict[str, Any]] = [
            {"type": "modification_request", "table": "design_modification_requests", "id": str(request_id)}
        ]

        project_available = bool(request.get("project_project_key"))
        project_metadata = _safe_value(_coerce_dict(request.get("project_metadata")))
        project = {
            "key": project_key,
            "display_name": str(request.get("project_display_name") or project_key),
            "frontend_stack": str(request.get("project_frontend_stack") or "unknown"),
            "adapter_key": str(request.get("project_adapter_key") or "legacy-css"),
            "repo_path": repo_path,
            "status": str(request.get("project_status") or "unknown"),
            "metadata": project_metadata,
            "created_at": _to_iso(request.get("project_created_at")),
            "updated_at": _to_iso(request.get("project_updated_at")),
        }
        if project_available:
            sources.append({"type": "project", "table": "design_projects", "project_key": project_key})
        else:
            missing_context.append(_missing("project_metadata", "design_projects row was not found"))

        screen_metadata = _safe_value(_coerce_dict(request.get("screen_metadata")))
        component_paths = _safe_value(_coerce_list(request.get("screen_component_paths")))
        screen_available = bool(request.get("screen_id") and request.get("screen_route"))
        screen = None
        if screen_available:
            screen = {
                "id": str(request.get("screen_id")),
                "route": str(request.get("screen_route") or ""),
                "name": str(request.get("screen_name") or ""),
                "purpose": str(request.get("screen_purpose") or ""),
                "primary_actions": _safe_value(_coerce_list(request.get("screen_primary_actions"))),
                "component_paths": component_paths,
                "metadata": screen_metadata,
            }
            sources.append({"type": "screen", "table": "design_screens", "id": str(request.get("screen_id"))})
        else:
            missing_context.append(_missing("screen", "request is not linked to a design_screens row"))

        if not component_paths:
            missing_context.append(_missing("component_paths", "screen.component_paths is empty"))

        baseline_snapshots = [_serialize_snapshot(row) for row in snapshots]
        baseline_snapshot = baseline_snapshots[0] if baseline_snapshots else None
        baseline_screenshot_url = baseline_snapshot["image_url"] if baseline_snapshot else None
        if baseline_snapshot:
            sources.append(
                {
                    "type": "baseline_screenshot",
                    "table": "design_visual_snapshots",
                    "id": baseline_snapshot["id"],
                    "viewport": baseline_snapshot["viewport"],
                }
            )
        else:
            missing_context.append(_missing("baseline_screenshot_url", "no before snapshot is stored"))

        viewport_matrix, viewport_matrix_source = _viewport_matrix(screen_metadata, snapshots)

        design_md, design_md_source = _read_design_md(repo_path)
        if design_md_source:
            sources.append(design_md_source)
        else:
            missing_context.append(_missing("design_md", "DESIGN.md was not found under the project repo path"))

        design_tokens = None
        if token_row is not None:
            token = dict(token_row)
            tokens = _safe_value(_coerce_dict(token.get("tokens")))
            design_tokens = {
                "id": str(token.get("id")),
                "version": str(token.get("version") or ""),
                "mode": str(token.get("mode") or ""),
                "tokens": tokens,
                "created_by": str(token.get("created_by") or ""),
                "created_at": _to_iso(token.get("created_at")),
            }
            sources.append(
                {
                    "type": "design_tokens",
                    "table": "design_token_sets",
                    "id": design_tokens["id"],
                    "version": design_tokens["version"],
                    "mode": design_tokens["mode"],
                }
            )
            if not tokens:
                missing_context.append(_missing("design_tokens", "latest design_token_sets row has empty tokens"))
        else:
            missing_context.append(_missing("design_tokens", "no design_token_sets row is stored for the project"))

        allowed_scope = _safe_value(_coerce_json(request.get("allowed_scope"), {}))
        forbidden_scope = _safe_value(_coerce_json(request.get("forbidden_scope"), {}))
        acceptance_criteria = _safe_value(_coerce_list(request.get("acceptance_criteria")))
        if _is_empty(allowed_scope):
            missing_context.append(_missing("allowed_scope", "request.allowed_scope is empty"))
        if _is_empty(forbidden_scope):
            missing_context.append(_missing("forbidden_scope", "request.forbidden_scope is empty"))
        if not acceptance_criteria:
            missing_context.append(_missing("acceptance_criteria", "request.acceptance_criteria is empty"))

        context = {
            "project": project,
            "screen": screen,
            "current_context": {
                "baseline_screenshot_url": baseline_screenshot_url,
                "baseline_snapshots": baseline_snapshots,
                "viewport_matrix": viewport_matrix,
                "viewport_matrix_source": viewport_matrix_source,
                "component_path_candidates": component_paths,
            },
            "design_contract": {
                "design_md": design_md,
                "design_tokens": design_tokens,
            },
            "modification_request": {
                "id": str(request.get("id")),
                "project_key": project_key,
                "screen_id": str(request.get("screen_id")) if request.get("screen_id") else None,
                "user_prompt": _redact_text(str(request.get("user_prompt") or "")),
                "normalized_card": _safe_value(_coerce_dict(request.get("normalized_card"))),
                "request_type": str(request.get("request_type") or "other"),
                "status": str(request.get("status") or "draft"),
                "created_at": _to_iso(request.get("created_at")),
                "updated_at": _to_iso(request.get("updated_at")),
            },
            "allowed_scope": allowed_scope,
            "forbidden_scope": forbidden_scope,
            "acceptance_criteria": acceptance_criteria,
            "safety": {
                "secrets_redacted": True,
                "env_files_included": False,
            },
        }
        prompt_chars = len(json.dumps(context, ensure_ascii=False, default=_json_default))

        inserted_row = await conn.fetchrow(
            """
            INSERT INTO design_context_packs (
                request_id, context, sources, missing_context, prompt_chars
            )
            VALUES ($1::uuid, $2::jsonb, $3::jsonb, $4::jsonb, $5)
            RETURNING id, request_id, context, sources, missing_context, prompt_chars, created_at
            """,
            request_id,
            json.dumps(context, ensure_ascii=False, default=_json_default),
            json.dumps(sources, ensure_ascii=False, default=_json_default),
            json.dumps(missing_context, ensure_ascii=False, default=_json_default),
            prompt_chars,
        )

    context_pack = _serialize_context_pack_row(dict(inserted_row), context, sources, missing_context)
    return {
        "context_pack": context_pack,
        "source_count": len(sources),
        "missing_context_count": len(missing_context),
    }
