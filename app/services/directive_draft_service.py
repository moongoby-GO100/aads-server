"""OHVIS directive copilot: generate, version, and audit reviewable drafts."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

from app.core.anthropic_client import call_llm_with_fallback
from app.core.db_pool import get_pool


logger = logging.getLogger(__name__)

SUPPORTED_PROJECTS = {"AADS", "KIS", "GO100", "SF", "NTV2", "NAS"}
ALLOWED_EVENTS = {"inserted", "approved", "rejected", "sent", "archived"}
GENERATION_TIMEOUT_SECONDS = 45
_DIRECTIVE_RE = re.compile(r">>>DIRECTIVE_START\s*.*?\s*>>>DIRECTIVE_END", re.DOTALL)
_FIELD_RE = re.compile(r"^(TASK_ID|TITLE|PRIORITY|SIZE|MODEL):\s*(.+)$", re.MULTILINE)
_REQUIRED_DESCRIPTION_HEADINGS = (
    "목표:",
    "현재 근거:",
    "허용 범위:",
    "금지 범위:",
    "구현 요구사항:",
    "검증 기준:",
    "완료 보고:",
)
_HIGH_RISK_RE = re.compile(
    r"(배포|deploy|restart|재시작|push|docker|ssh|마이그레이션|migration|DB\s*(?:변경|수정)|삭제|delete)",
    re.IGNORECASE,
)
_WRITE_RE = re.compile(
    r"(구현|수정|고쳐|추가|작성|저장|반영|commit|커밋|코드|파일|schema|스키마)",
    re.IGNORECASE,
)


class DraftNotFoundError(LookupError):
    pass


class DraftConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class DraftSource:
    session_id: uuid.UUID
    workspace_id: uuid.UUID | None
    session_title: str
    workspace_name: str
    project_key: str
    messages: list[dict[str, Any]]


def normalize_project_key(value: str | None) -> str:
    normalized = re.sub(r"[^A-Z0-9_-]", "", (value or "").strip().upper())
    aliases = {
        "NEWTALK": "NTV2",
        "NEWTALKV2": "NTV2",
        "NEWTALK-V2": "NTV2",
        "SHORTFLOW": "SF",
    }
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in SUPPORTED_PROJECTS else "CUSTOM"


def classify_risk(text: str) -> str:
    if _HIGH_RISK_RE.search(text or ""):
        return "high"
    if _WRITE_RE.search(text or ""):
        return "medium"
    return "low"


def _field_map(content: str) -> dict[str, str]:
    return {key: value.strip() for key, value in _FIELD_RE.findall(content or "")}


def validate_directive(
    content: str, *, expected_project: str | None = None
) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not _DIRECTIVE_RE.search(content or ""):
        errors.append("directive_block")
    fields = _field_map(content)
    for required in ("TASK_ID", "TITLE", "PRIORITY", "SIZE", "MODEL"):
        if not fields.get(required):
            errors.append(required.lower())
    if fields.get("PRIORITY") not in {"P0-CRITICAL", "P1-HIGH", "P2-MEDIUM", "P3-LOW"}:
        errors.append("priority_value")
    if fields.get("SIZE") not in {"XS", "S", "M", "L", "XL"}:
        errors.append("size_value")
    if fields.get("MODEL") != "AUTO":
        errors.append("model_value")
    if expected_project and fields.get("TASK_ID") != f"{expected_project}-DRAFT":
        errors.append("task_id_value")
    if "DESCRIPTION:" not in (content or ""):
        errors.append("description")
    for heading in _REQUIRED_DESCRIPTION_HEADINGS:
        if heading not in (content or ""):
            errors.append(f"description_{heading[:-1].replace(' ', '_')}")
    return not errors, errors


def _extract_directive(raw: str | None, *, expected_project: str | None = None) -> str | None:
    if not raw:
        return None
    match = _DIRECTIVE_RE.search(raw)
    if not match:
        return None
    content = match.group(0).strip()
    valid, _ = validate_directive(content, expected_project=expected_project)
    return content if valid else None


def _latest_user_request(messages: Iterable[dict[str, Any]]) -> str:
    for message in reversed(list(messages)):
        if message.get("role") == "user" and str(message.get("content") or "").strip():
            return str(message["content"]).strip()
    return "현재 세션의 최근 문답을 검토하여 필요한 작업을 수행한다."


def _user_request_text(messages: Iterable[dict[str, Any]]) -> str:
    return "\n".join(
        str(message.get("content") or "")
        for message in messages
        if message.get("role") == "user"
    ).strip()


def build_fallback_directive(source: DraftSource, risk_level: str) -> str:
    request = _latest_user_request(source.messages)
    title = re.sub(r"\s+", " ", request).strip()[:80] or "최근 문답 후속 작업"
    priority = "P1-HIGH" if risk_level == "high" else "P2-MEDIUM"
    size = "M" if risk_level != "low" else "S"
    project = source.project_key
    scope_warning = (
        "운영 배포·재시작·DB 변경은 대상과 롤백을 확인하고 별도 승인 게이트를 통과한다."
        if risk_level == "high"
        else "요청 범위 밖 파일과 기존 dirty 변경을 수정하거나 되돌리지 않는다."
    )
    return (
        ">>>DIRECTIVE_START\n"
        f"TASK_ID: {project}-DRAFT\n"
        f"TITLE: {title}\n"
        f"PRIORITY: {priority}\n"
        f"SIZE: {size}\n"
        "MODEL: AUTO\n"
        "DESCRIPTION: |\n"
        "  목표:\n"
        f"  - {request[:1200]}\n"
        "  현재 근거:\n"
        f"  - 세션 '{source.session_title}'의 최근 사용자 질문과 AI 응답을 원문 기준으로 재검토한다.\n"
        "  허용 범위:\n"
        f"  - 프로젝트: {project}; 이번 요청과 직접 관련된 코드, 테스트, 문서만 변경한다.\n"
        "  금지 범위:\n"
        f"  - {scope_warning}\n"
        "  구현 요구사항:\n"
        "  - 기존 정본과 호출 경로를 먼저 확인하고 중복 구현을 만들지 않는다.\n"
        "  - 새 경로를 추가하면 기존 경로의 제거·호환·롤백 정책을 명시한다.\n"
        "  검증 기준:\n"
        "  - 관련 테스트, 실제 API 또는 화면, 운영 반영 여부를 각각 검증한다.\n"
        "  완료 보고:\n"
        "  - 변경 파일, 테스트, 커밋, 푸시, 배포, 문서 기록, 미완료 항목을 분리한다.\n"
        ">>>DIRECTIVE_END"
    )


def _build_generation_prompt(source: DraftSource, risk_level: str) -> str:
    transcript = "\n\n".join(
        f"[{message['role'].upper()} | {message['id']}]\n{str(message.get('content') or '')[:3500]}"
        for message in source.messages
    )[-14000:]
    return f"""아래 세션의 최근 문답을 검토하여 실행 전 CEO가 수정·확인할 개발 지시서 초안을 작성하라.

규칙:
- 출력은 >>>DIRECTIVE_START 부터 >>>DIRECTIVE_END 까지 한 블록만 반환한다.
- 헤더 순서: TASK_ID, TITLE, PRIORITY, SIZE, MODEL, DESCRIPTION.
- TASK_ID는 정확히 {source.project_key}-DRAFT, MODEL은 AUTO로 쓴다.
- PRIORITY는 P0-CRITICAL/P1-HIGH/P2-MEDIUM/P3-LOW 중 하나다.
- SIZE는 XS/S/M/L/XL 중 하나다.
- DESCRIPTION에는 목표, 현재 근거, 허용 범위, 금지 범위, 구현 요구사항, 검증 기준, 완료 보고를 포함한다.
- 최근 AI 답변을 사실로 맹신하지 말고 실제 코드·DB·화면 재검증을 요구한다.
- 서로 다른 구현이 이미 있으면 canonical 정본, 소비자, 전환·제거·rollback 기준을 명시한다.
- 원문에 없는 파일명, 수치, 완료 사실, 실제 작업 ID를 만들어내지 않는다.
- 자동 전송이나 무단 배포를 지시하지 않는다.

프로젝트: {source.project_key}
위험도: {risk_level}
세션: {source.session_title}
워크스페이스: {source.workspace_name}

최근 문답:
{transcript}
"""


async def generate_directive_content(
    source: DraftSource,
    risk_level: str,
    *,
    tenant_id: str | None = None,
    user_id: str | None = None,
) -> tuple[str, str]:
    """Generate a validated directive or fail closed to a deterministic draft."""
    try:
        raw = await asyncio.wait_for(
            call_llm_with_fallback(
                prompt=_build_generation_prompt(source, risk_level),
                model="claude-haiku-4-5-20251001",
                max_tokens=1800,
                system="당신은 사실 기반 작업계약을 만드는 OHVIS 지시 코파일럿이다.",
                tenant_id=tenant_id,
                user_id=user_id,
            ),
            timeout=GENERATION_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning(
            "directive_draft_generation_fallback session=%s error=%s",
            str(source.session_id)[:8],
            type(exc).__name__,
        )
        raw = None
    content = _extract_directive(raw, expected_project=source.project_key)
    if content is not None:
        return content, "generated"
    return build_fallback_directive(source, risk_level), "fallback"


async def _load_source(
    *,
    tenant_id: str,
    session_id: str,
    context_window: int,
    message_ids: list[str] | None,
) -> DraftSource:
    tenant_uuid = uuid.UUID(tenant_id)
    session_uuid = uuid.UUID(session_id)
    async with get_pool().acquire() as conn:
        session = await conn.fetchrow(
            """
            SELECT s.id, s.title, s.workspace_id, w.name AS workspace_name, w.project_key
              FROM chat_sessions s
              LEFT JOIN chat_workspaces w ON w.id = s.workspace_id
             WHERE s.id = $1 AND s.tenant_id = $2
            """,
            session_uuid,
            tenant_uuid,
        )
        if not session:
            raise DraftNotFoundError("session not found")
        if message_ids:
            parsed_ids = [uuid.UUID(value) for value in message_ids]
            rows = await conn.fetch(
                """
                SELECT id, role, content, created_at
                  FROM chat_messages
                 WHERE session_id = $1 AND tenant_id = $2 AND id = ANY($3::uuid[])
                 ORDER BY created_at ASC
                """,
                session_uuid,
                tenant_uuid,
                parsed_ids,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, role, content, created_at FROM (
                    SELECT id, role, content, created_at
                      FROM chat_messages
                     WHERE session_id = $1 AND tenant_id = $2
                       AND role IN ('user', 'assistant')
                       AND COALESCE(content, '') <> ''
                     ORDER BY created_at DESC
                     LIMIT $3
                ) recent
                ORDER BY created_at ASC
                """,
                session_uuid,
                tenant_uuid,
                max(2, min(context_window, 16)),
            )
    messages = [dict(row) for row in rows]
    if not messages:
        raise ValueError("초안을 만들 최근 문답이 없습니다.")
    if not _user_request_text(messages):
        raise ValueError("초안을 만들 최근 사용자 요청이 없습니다.")
    return DraftSource(
        session_id=session_uuid,
        workspace_id=session["workspace_id"],
        session_title=session["title"] or "제목 없는 세션",
        workspace_name=session["workspace_name"] or "워크스페이스",
        project_key=normalize_project_key(session["project_key"]),
        messages=messages,
    )


def _serialize_row(row: Any) -> dict[str, Any]:
    result = dict(row)
    for key, value in list(result.items()):
        if isinstance(value, uuid.UUID):
            result[key] = str(value)
        elif isinstance(value, list):
            result[key] = [str(item) if isinstance(item, uuid.UUID) else item for item in value]
        elif key in {"metadata", "classification"} and isinstance(value, str):
            try:
                result[key] = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                pass
    return result


def _serialize_artifact(row: Any) -> dict[str, Any]:
    result = _serialize_row(row)
    result["artifact_type"] = result.pop("type")
    return result


async def create_draft(
    *,
    tenant_id: str,
    user_id: str | None,
    session_id: str,
    context_window: int = 8,
    message_ids: list[str] | None = None,
) -> dict[str, Any]:
    source = await _load_source(
        tenant_id=tenant_id,
        session_id=session_id,
        context_window=context_window,
        message_ids=message_ids,
    )
    # AI 답변의 "배포하지 않음" 같은 설명이 위험도를 올리지 않도록 사용자 요청만 판정한다.
    risk_level = classify_risk(_user_request_text(source.messages))
    content, generation_mode = await generate_directive_content(
        source,
        risk_level,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    fields = _field_map(content)
    title = fields.get("TITLE", "지시 초안")[:200]
    source_ids = [message["id"] for message in source.messages]
    classification = {
        "risk_level": risk_level,
        "generation_mode": generation_mode,
        "context_message_count": len(source.messages),
        "requires_human_review": True,
        "auto_submit": False,
    }
    tenant_uuid = uuid.UUID(tenant_id)
    actor_uuid = uuid.UUID(user_id) if user_id else None
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            draft = await conn.fetchrow(
                """
                INSERT INTO directive_drafts
                    (tenant_id, session_id, created_by, project_key, title, content,
                     risk_level, confidence, source_message_ids, classification)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::uuid[], $10::jsonb)
                RETURNING *
                """,
                tenant_uuid,
                source.session_id,
                actor_uuid,
                source.project_key,
                title,
                content,
                risk_level,
                0.75 if generation_mode == "generated" else 0.45,
                source_ids,
                json.dumps(classification, ensure_ascii=False),
            )
            metadata = {
                "subtype": "directive_draft",
                "draft_id": str(draft["id"]),
                "revision": 1,
                "status": "draft",
                "project_key": source.project_key,
                "risk_level": risk_level,
                "requires_human_review": True,
            }
            artifact = await conn.fetchrow(
                """
                INSERT INTO chat_artifacts
                    (tenant_id, session_id, workspace_id, type, title, content, metadata)
                VALUES ($1, $2, $3, 'report', $4, $5, $6::jsonb)
                RETURNING *
                """,
                tenant_uuid,
                source.session_id,
                source.workspace_id,
                f"지시 초안: {title}"[:200],
                content,
                json.dumps(metadata, ensure_ascii=False),
            )
            await conn.execute(
                "UPDATE directive_drafts SET artifact_id = $1 WHERE id = $2",
                artifact["id"],
                draft["id"],
            )
            await conn.execute(
                """
                INSERT INTO directive_draft_revisions
                    (tenant_id, draft_id, revision, title, content, change_source, editor_user_id, metadata)
                VALUES ($1, $2, 1, $3, $4, $5, $6, $7::jsonb)
                """,
                tenant_uuid,
                draft["id"],
                title,
                content,
                generation_mode,
                actor_uuid,
                json.dumps({"source_message_ids": [str(value) for value in source_ids]}, ensure_ascii=False),
            )
            await conn.execute(
                """
                INSERT INTO directive_draft_events
                    (tenant_id, draft_id, revision, action, actor_user_id, metadata)
                VALUES ($1, $2, 1, 'created', $3, $4::jsonb)
                """,
                tenant_uuid,
                draft["id"],
                actor_uuid,
                json.dumps({"generation_mode": generation_mode}, ensure_ascii=False),
            )
    result = _serialize_row(draft)
    result["artifact_id"] = str(artifact["id"])
    result["artifact"] = _serialize_artifact(artifact)
    return result


async def list_drafts(*, tenant_id: str, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM directive_drafts
             WHERE tenant_id = $1 AND session_id = $2
             ORDER BY updated_at DESC
             LIMIT $3
            """,
            uuid.UUID(tenant_id),
            uuid.UUID(session_id),
            max(1, min(limit, 100)),
        )
    return [_serialize_row(row) for row in rows]


async def update_draft(
    *,
    tenant_id: str,
    user_id: str | None,
    draft_id: str,
    title: str | None,
    content: str | None,
    expected_revision: int | None,
    change_source: str = "user_edit",
) -> dict[str, Any]:
    tenant_uuid = uuid.UUID(tenant_id)
    draft_uuid = uuid.UUID(draft_id)
    actor_uuid = uuid.UUID(user_id) if user_id else None
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                "SELECT * FROM directive_drafts WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
                draft_uuid,
                tenant_uuid,
            )
            if not current:
                raise DraftNotFoundError("directive draft not found")
            if expected_revision is not None and current["current_revision"] != expected_revision:
                raise DraftConflictError(f"revision conflict: current={current['current_revision']}")
            next_content = (content if content is not None else current["content"]).strip()
            valid, errors = validate_directive(
                next_content, expected_project=current["project_key"]
            )
            if not valid:
                raise ValueError(f"지시서 필수 형식이 누락되었습니다: {', '.join(errors)}")
            content_title = _field_map(next_content).get("TITLE")
            next_title = (
                title if title is not None else content_title or current["title"]
            ).strip()[:200]
            next_revision = current["current_revision"] + 1
            updated = await conn.fetchrow(
                """
                UPDATE directive_drafts
                   SET title = $3, content = $4, current_revision = $5, updated_at = NOW()
                 WHERE id = $1 AND tenant_id = $2
                RETURNING *
                """,
                draft_uuid,
                tenant_uuid,
                next_title,
                next_content,
                next_revision,
            )
            await conn.execute(
                """
                INSERT INTO directive_draft_revisions
                    (tenant_id, draft_id, revision, title, content, change_source, editor_user_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                tenant_uuid,
                draft_uuid,
                next_revision,
                next_title,
                next_content,
                change_source,
                actor_uuid,
            )
            await conn.execute(
                """
                INSERT INTO directive_draft_events
                    (tenant_id, draft_id, revision, action, actor_user_id)
                VALUES ($1, $2, $3, 'edited', $4)
                """,
                tenant_uuid,
                draft_uuid,
                next_revision,
                actor_uuid,
            )
            if current["artifact_id"]:
                await conn.execute(
                    """
                    UPDATE chat_artifacts
                       SET title = $3, content = $4,
                           metadata = metadata || jsonb_build_object('revision', $5::integer),
                           updated_at = NOW()
                     WHERE id = $1 AND tenant_id = $2
                    """,
                    current["artifact_id"],
                    tenant_uuid,
                    f"지시 초안: {next_title}"[:200],
                    next_content,
                    next_revision,
                )
    return _serialize_row(updated)


async def update_draft_from_artifact(
    *, tenant_id: str, user_id: str | None, artifact: dict[str, Any], title: str | None, content: str | None
) -> dict[str, Any]:
    metadata = artifact.get("metadata") or {}
    draft_id = metadata.get("draft_id")
    if not draft_id:
        raise DraftNotFoundError("artifact is not linked to a directive draft")
    clean_title = title
    if clean_title and clean_title.startswith("지시 초안:"):
        clean_title = clean_title.split(":", 1)[1].strip()
    await update_draft(
        tenant_id=tenant_id,
        user_id=user_id,
        draft_id=str(draft_id),
        title=clean_title,
        content=content,
        expected_revision=int(metadata.get("revision") or 1),
        change_source="artifact_edit",
    )
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM chat_artifacts WHERE id = $1 AND tenant_id = $2",
            uuid.UUID(str(artifact["id"])),
            uuid.UUID(tenant_id),
        )
    if not row:
        raise DraftNotFoundError("artifact not found")
    return _serialize_artifact(row)


async def record_event(
    *, tenant_id: str, user_id: str | None, draft_id: str, action: str, metadata: dict[str, Any] | None = None
) -> dict[str, Any]:
    if action not in ALLOWED_EVENTS:
        raise ValueError(f"unsupported action: {action}")
    tenant_uuid = uuid.UUID(tenant_id)
    draft_uuid = uuid.UUID(draft_id)
    actor_uuid = uuid.UUID(user_id) if user_id else None
    status = action if action in {"approved", "rejected", "sent", "archived"} else None
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            draft = await conn.fetchrow(
                "SELECT * FROM directive_drafts WHERE id = $1 AND tenant_id = $2 FOR UPDATE",
                draft_uuid,
                tenant_uuid,
            )
            if not draft:
                raise DraftNotFoundError("directive draft not found")
            if status:
                draft = await conn.fetchrow(
                    """
                    UPDATE directive_drafts SET status = $3, updated_at = NOW()
                     WHERE id = $1 AND tenant_id = $2 RETURNING *
                    """,
                    draft_uuid,
                    tenant_uuid,
                    status,
                )
                if draft["artifact_id"]:
                    await conn.execute(
                        """
                        UPDATE chat_artifacts
                           SET metadata = metadata || jsonb_build_object('status', $3::text), updated_at = NOW()
                         WHERE id = $1 AND tenant_id = $2
                        """,
                        draft["artifact_id"],
                        tenant_uuid,
                        status,
                    )
            await conn.execute(
                """
                INSERT INTO directive_draft_events
                    (tenant_id, draft_id, revision, action, actor_user_id, metadata)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                """,
                tenant_uuid,
                draft_uuid,
                draft["current_revision"],
                action,
                actor_uuid,
                json.dumps(metadata or {}, ensure_ascii=False),
            )
    return _serialize_row(draft)
