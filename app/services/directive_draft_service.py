"""OHVIS directive copilot: generate, version, and audit reviewable drafts."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

from app.core.anthropic_client import call_llm_with_fallback
from app.core.db_pool import get_pool


logger = logging.getLogger(__name__)

SUPPORTED_PROJECTS = {"AADS", "KIS", "GO100", "SF", "NTV2", "NAS"}
ALLOWED_EVENTS = {"inserted", "approved", "rejected", "sent", "archived"}
GENERATION_TIMEOUT_SECONDS = 45
# Same default as DraftCreateRequest.context_window (app/api/directive_drafts.py).
DEFAULT_CONTEXT_WINDOW = 8

_directive_model_cache: dict = {}
_directive_model_cache_ts: float = 0.0
_DIRECTIVE_MODEL_CACHE_TTL = 60
_DEFAULT_DIRECTIVE_MODELS = ["claude-sonnet-5-5", "codex:gpt-5.6-terra"]
_DIRECTIVE_MODEL_ALIASES = {
    # Legacy typo accepted by the first Ops-settings implementation.
    "codex:gpt-5.6-tela": "codex:gpt-5.6-terra",
    "gpt-5.6-tela": "gpt-5.6-terra",
}


def normalize_directive_model_id(value: str | None) -> str:
    model_id = str(value or "").strip()
    return _DIRECTIVE_MODEL_ALIASES.get(model_id.lower(), model_id)


def normalize_directive_models(values: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        model_id = normalize_directive_model_id(value)
        if model_id and model_id not in normalized:
            normalized.append(model_id)
    return normalized


async def _get_directive_model_config(role: str = "generation") -> dict:
    """DB에서 지시서 생성 모델 설정 조회 (60초 캐시)."""
    global _directive_model_cache, _directive_model_cache_ts
    now = time.monotonic()
    if role in _directive_model_cache and (now - _directive_model_cache_ts) < _DIRECTIVE_MODEL_CACHE_TTL:
        return _directive_model_cache[role]
    try:
        async with get_pool().acquire() as conn:
            row = await conn.fetchrow(
                "SELECT models, timeout_seconds, max_tokens FROM directive_model_config WHERE role = $1",
                role,
            )
        if row:
            raw = row["models"]
            models = json.loads(raw) if isinstance(raw, str) else (list(raw) if raw else [])
            models = normalize_directive_models(models) or list(_DEFAULT_DIRECTIVE_MODELS)
            config = {"models": models, "timeout_seconds": row["timeout_seconds"] or 60, "max_tokens": row["max_tokens"] or 2000}
        else:
            config = {"models": list(_DEFAULT_DIRECTIVE_MODELS), "timeout_seconds": 60, "max_tokens": 2000}
        _directive_model_cache[role] = config
        _directive_model_cache_ts = now
        return config
    except Exception:
        logger.warning("directive_model_config_read_failed role=%s", role)
        return {"models": list(_DEFAULT_DIRECTIVE_MODELS), "timeout_seconds": 60, "max_tokens": 2000}


def invalidate_directive_model_cache():
    """설정 변경 시 캐시 무효화."""
    global _directive_model_cache, _directive_model_cache_ts
    _directive_model_cache = {}
    _directive_model_cache_ts = 0.0


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
    selected_assistant_message_id: uuid.UUID | None = None
    composer_draft: str | None = None


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
    task_id = fields.get("TASK_ID", "")
    if expected_project and task_id != f"{expected_project}-DRAFT" and not re.fullmatch(
        re.escape(expected_project) + r"-DRAFT-[0-9a-f]{32}", task_id
    ):
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


def _effective_user_request(source: DraftSource) -> str:
    """Prefer the unsent composer text as the user's newest stated intent."""
    composer_draft = (source.composer_draft or "").strip()
    return composer_draft or _latest_user_request(source.messages)


def _user_request_text(messages: Iterable[dict[str, Any]]) -> str:
    return "\n".join(
        str(message.get("content") or "")
        for message in messages
        if message.get("role") == "user"
    ).strip()


def _selected_assistant_text(source: DraftSource) -> str:
    selected_id = source.selected_assistant_message_id
    if selected_id is None:
        return ""
    for message in source.messages:
        if (
            message.get("role") == "assistant"
            and str(message.get("id")) == str(selected_id)
        ):
            return str(message.get("content") or "").strip()
    return ""


def _assistant_action_focus(content: str) -> str:
    """Prefer the explicit follow-up section when a selected answer has one."""
    text = (content or "").strip()
    for marker in ("→ 다음 단계:", "→ 권장 조치:", "## 다음 단계", "## 조치안", "## 개선안"):
        marker_index = text.rfind(marker)
        if marker_index >= 0:
            focused = text[marker_index + len(marker):].strip()
            if focused:
                return focused
    return text


def _fallback_title(text: str) -> str:
    first_meaningful_line = next(
        (line.strip() for line in text.splitlines() if line.strip()),
        "선택한 AI 응답 후속 작업",
    )
    plain = re.sub(r"[#>*`_\[\]()]", " ", first_meaningful_line)
    plain = re.sub(r"^[-+\d.\s]+", "", plain)
    return re.sub(r"\s+", " ", plain).strip()[:80] or "선택한 AI 응답 후속 작업"


def _risk_source_text(source: DraftSource) -> str:
    request = _user_request_text(source.messages)
    if source.composer_draft:
        request = f"{request}\n{source.composer_draft}".strip()
    selected_response = _selected_assistant_text(source)
    if selected_response:
        return f"{request}\n{_assistant_action_focus(selected_response)}".strip()
    return request


def build_fallback_directive(source: DraftSource, risk_level: str) -> str:
    request = _effective_user_request(source)
    selected_response = _selected_assistant_text(source)
    response_focus = _assistant_action_focus(selected_response)
    title = (
        _fallback_title(response_focus)
        if response_focus
        else re.sub(r"\s+", " ", request).strip()[:80] or "최근 문답 후속 작업"
    )
    priority = "P1-HIGH" if risk_level == "high" else "P2-MEDIUM"
    size = "M" if risk_level != "low" else "S"
    project = source.project_key
    scope_warning = (
        "운영 배포·재시작·DB 변경은 대상과 롤백을 확인하고 별도 승인 게이트를 통과한다."
        if risk_level == "high"
        else "요청 범위 밖 파일과 기존 dirty 변경을 수정하거나 되돌리지 않는다."
    )
    objective = (
        "선택한 AI 응답의 제안·조치 항목을 실제 상태와 대조하고 실행 가능한 작업으로 반영한다."
        if response_focus
        else request[:1200]
    )
    if response_focus and source.composer_draft:
        objective += f" 입력창의 추가 요구사항도 함께 반영한다: {request[:900]}"
    evidence = (
        f"선택한 AI 응답의 후속 항목: {response_focus[:1200]}\n"
        f"  - 해당 응답의 원 사용자 요청: {request[:600]}"
        if response_focus
        else f"세션 '{source.session_title}'의 최근 사용자 질문과 AI 응답을 원문 기준으로 재검토한다."
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
        f"  - {objective}\n"
        "  현재 근거:\n"
        f"  - {evidence}\n"
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
    selected_id = source.selected_assistant_message_id
    transcript = "\n\n".join(
        (
            f"[{message['role'].upper()}"
            f"{' | SELECTED_RESPONSE' if str(message.get('id')) == str(selected_id) else ''}"
            f" | {message['id']}]\n{str(message.get('content') or '')[:3500]}"
        )
        for message in source.messages
    )[-14000:]
    selection_rule = (
        "- SELECTED_RESPONSE로 표시된 AI 응답의 분석·제안·다음 단계를 실행 지시로 변환한다. "
        "직전 USER 메시지는 의도와 범위 확인에만 사용하고, 사용자 질문을 그대로 다시 지시하지 않는다."
        if selected_id is not None
        else "- 전체 최근 문답에서 사용자의 최신 실행 의도를 기준으로 지시서를 작성한다."
    )
    composer_context = ""
    if source.composer_draft:
        composer_context = (
            "\n\n[COMPOSER_DRAFT | UNSENT]\n"
            f"{source.composer_draft[:50000]}\n"
            "- 위 내용은 아직 전송하지 않은 입력창 초안이지만, 사용자의 가장 최신 요구사항으로 취급해 지시서에 반영한다."
        )
    return f"""아래 세션의 최근 문답을 검토하여 실행 전 CEO가 수정·확인할 개발 지시서 초안을 작성하라.

규칙:
- 출력은 >>>DIRECTIVE_START 부터 >>>DIRECTIVE_END 까지 한 블록만 반환한다.
- 헤더 순서: TASK_ID, TITLE, PRIORITY, SIZE, MODEL, DESCRIPTION.
- TASK_ID는 정확히 {source.project_key}-DRAFT, MODEL은 AUTO로 쓴다.
- PRIORITY는 P0-CRITICAL/P1-HIGH/P2-MEDIUM/P3-LOW 중 하나다.
- SIZE는 XS/S/M/L/XL 중 하나다.
- DESCRIPTION은 "DESCRIPTION: |" 로 시작하는 블록으로 쓰고, 아래 일곱 줄을 콜론까지 그대로, 이 순서로 포함한다.
  목표:
  현재 근거:
  허용 범위:
  금지 범위:
  구현 요구사항:
  검증 기준:
  완료 보고:
- 일곱 줄 각각 바로 아래에 "  - " 로 시작하는 근거 줄을 최소 1개 둔다.
- 헤딩에 ##, **, 번호, 영문 표기를 덧붙이지 않는다. 위 문자열과 다르면 초안이 검증에서 폐기된다.
- 최근 AI 답변을 사실로 맹신하지 말고 실제 코드·DB·화면 재검증을 요구한다.
- 서로 다른 구현이 이미 있으면 canonical 정본, 소비자, 전환·제거·rollback 기준을 명시한다.
- 원문에 없는 파일명, 수치, 완료 사실, 실제 작업 ID를 만들어내지 않는다.
- 자동 전송이나 무단 배포를 지시하지 않는다.
{selection_rule}

프로젝트: {source.project_key}
위험도: {risk_level}
세션: {source.session_title}
워크스페이스: {source.workspace_name}

최근 문답:
{transcript}{composer_context}
"""


async def generate_directive_content(
    source: DraftSource,
    risk_level: str,
    *,
    tenant_id: str | None = None,
    user_id: str | None = None,
) -> tuple[str, str, str | None]:
    """Generate a validated directive or fail closed to a deterministic draft.
    Returns (content, generation_mode, model_used)."""
    config = await _get_directive_model_config()
    models = config["models"]
    timeout = config["timeout_seconds"]
    max_tok = config["max_tokens"]
    generation_prompt = _build_generation_prompt(source, risk_level)
    for model_candidate in models:
        configured_model = normalize_directive_model_id(model_candidate)
        try:
            raw = await asyncio.wait_for(
                _call_configured_model(
                    model_candidate=configured_model,
                    prompt=generation_prompt,
                    max_tokens=max_tok,
                    system="당신은 사실 기반 작업계약을 만드는 OHVIS 지시 코파일럿이다.",
                    tenant_id=tenant_id,
                    user_id=user_id,
                ),
                timeout=timeout,
            )
        except Exception as exc:
            logger.warning(
                "directive_draft_model_failed session=%s model=%s error=%s detail=%s",
                str(source.session_id)[:8],
                configured_model,
                type(exc).__name__,
                str(exc)[:200] or "-",
            )
            continue
        content = _extract_directive(raw, expected_project=source.project_key)
        if content is not None:
            return content, "generated", configured_model
        invalid_block = _DIRECTIVE_RE.search(raw or "")
        invalid_reasons = (
            validate_directive(
                invalid_block.group(0).strip(), expected_project=source.project_key
            )[1]
            if invalid_block
            else ["directive_block"]
        )
        logger.warning(
            "directive_draft_model_invalid session=%s model=%s reasons=%s",
            str(source.session_id)[:8],
            configured_model,
            ",".join(invalid_reasons[:10]) or "unknown",
        )
    return build_fallback_directive(source, risk_level), "fallback", None


async def _call_configured_model(
    *,
    model_candidate: str,
    prompt: str,
    max_tokens: int,
    system: str,
    tenant_id: str | None,
    user_id: str | None,
) -> str | None:
    """Route configured CLI model IDs to their real relay backends.

    ``call_llm_with_fallback`` is intentionally retained for API/LiteLLM model
    IDs.  Claude and Codex entries in the settings UI are CLI contracts; sending
    ``codex:*`` to LiteLLM produces HTTP 400 and using the Anthropic background
    retry chain can consume the whole HTTP request before Codex is attempted.
    """
    configured = normalize_directive_model_id(model_candidate)
    provider, separator, bare_model = configured.partition(":")
    provider = provider.lower() if separator else ""
    model = bare_model.strip() if separator else configured

    if provider == "codex" or (not provider and model.startswith("gpt-")):
        from app.services.model_selector import _stream_codex_relay

        stream = _stream_codex_relay(
            model,
            system,
            [{"role": "user", "content": prompt}],
            tools=None,
            session_id=None,
        )
        return await _collect_relay_text(stream, configured)

    if provider == "claude" or (not provider and model.startswith("claude")):
        from app.services.model_selector import _stream_cli_relay

        stream = _stream_cli_relay(
            model,
            system,
            [{"role": "user", "content": prompt}],
            tools=None,
            session_id=None,
        )
        return await _collect_relay_text(stream, configured)

    return await call_llm_with_fallback(
        prompt=prompt,
        model=configured,
        max_tokens=max_tokens,
        system=system,
        tenant_id=tenant_id,
        user_id=user_id,
    )


async def _collect_relay_text(stream: Any, configured_model: str) -> str:
    chunks: list[str] = []
    async for event in stream:
        event_type = str(event.get("type") or "")
        if event_type in {"delta", "text", "content"}:
            chunks.append(str(event.get("content") or ""))
        elif event_type == "error":
            raise RuntimeError(
                f"configured model relay failed: {configured_model}: "
                f"{str(event.get('content') or 'unknown error')[:240]}"
            )
    content = "".join(chunks).strip()
    if not content:
        raise RuntimeError(f"configured model relay returned no text: {configured_model}")
    return content


async def _fetch_recent_context_messages(
    conn,
    *,
    session_id: Any,
    tenant_id: Any,
    context_window: int = DEFAULT_CONTEXT_WINDOW,
    as_of: Any = None,
) -> list[Any]:
    """Recent user/assistant turns that a draft is built from, oldest first.

    Both the chat path and the milestone path use this so the two record the
    same kind of source. ``as_of`` pins the window to a past moment (backfill).
    """
    as_of_filter = "AND created_at <= $4" if as_of is not None else ""
    args: list[Any] = [session_id, tenant_id, max(2, min(context_window, 16))]
    if as_of is not None:
        args.append(as_of)
    return await conn.fetch(
        f"""
        SELECT id, role, content, created_at FROM (
            SELECT id, role, content, created_at
              FROM chat_messages
             WHERE session_id = $1 AND tenant_id = $2
               AND role IN ('user', 'assistant')
               AND COALESCE(content, '') <> ''
               {as_of_filter}
             ORDER BY created_at DESC
             LIMIT $3
        ) recent
        ORDER BY created_at ASC
        """,
        *args,
    )


def _source_ids_with_user_request(rows: Iterable[Any]) -> list[Any]:
    """Message ids that can be cited as a draft source, or [] when none can.

    Mirrors the chat path, which refuses to draft without a user request:
    a window of assistant-only turns is not a traceable origin.
    """
    messages = [dict(row) for row in rows]
    if not _user_request_text(messages):
        return []
    return [message["id"] for message in messages]


async def _load_source(
    *,
    tenant_id: str,
    session_id: str,
    context_window: int,
    message_ids: list[str] | None,
    composer_draft: str | None = None,
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
            parsed_ids = list(dict.fromkeys(uuid.UUID(value) for value in message_ids))
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
            if len(rows) != len(parsed_ids):
                raise ValueError("선택한 메시지 중 현재 세션에서 확인할 수 없는 항목이 있습니다.")
        else:
            rows = await _fetch_recent_context_messages(
                conn,
                session_id=session_uuid,
                tenant_id=tenant_uuid,
                context_window=context_window,
            )
    messages = [dict(row) for row in rows]
    normalized_composer_draft = (composer_draft or "").strip() or None
    if not messages and not normalized_composer_draft:
        raise ValueError("초안을 만들 최근 문답이 없습니다.")
    if not _user_request_text(messages) and not normalized_composer_draft:
        raise ValueError("초안을 만들 최근 사용자 요청이 없습니다.")
    selected_assistant_message_id = None
    if message_ids:
        selected_assistants = [
            message["id"] for message in messages if message.get("role") == "assistant"
        ]
        if len(selected_assistants) > 1:
            raise ValueError("한 번에 하나의 AI 응답만 지시 초안 대상으로 선택할 수 있습니다.")
        if selected_assistants:
            selected_assistant_message_id = selected_assistants[0]
    return DraftSource(
        session_id=session_uuid,
        workspace_id=session["workspace_id"],
        session_title=session["title"] or "제목 없는 세션",
        workspace_name=session["workspace_name"] or "워크스페이스",
        project_key=normalize_project_key(session["project_key"]),
        messages=messages,
        selected_assistant_message_id=selected_assistant_message_id,
        composer_draft=normalized_composer_draft,
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
    context_window: int = DEFAULT_CONTEXT_WINDOW,
    message_ids: list[str] | None = None,
    composer_draft: str | None = None,
) -> dict[str, Any]:
    source = await _load_source(
        tenant_id=tenant_id,
        session_id=session_id,
        context_window=context_window,
        message_ids=message_ids,
        composer_draft=composer_draft,
    )
    # 최근 문답 모드는 사용자 요청만 판정하고, 응답 선택 모드는 선택된 후속 조치도 포함한다.
    risk_level = classify_risk(_risk_source_text(source))
    content, generation_mode, model_used = await generate_directive_content(
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
        "source_mode": "selected_response" if source.selected_assistant_message_id else "recent_context",
        "selected_assistant_message_id": (
            str(source.selected_assistant_message_id)
            if source.selected_assistant_message_id
            else None
        ),
        "requires_human_review": True,
        "auto_submit": False,
        "model_used": model_used,
        "composer_draft_included": bool(source.composer_draft),
        "composer_draft_content": source.composer_draft,
    }
    tenant_uuid = uuid.UUID(tenant_id)
    actor_uuid = uuid.UUID(user_id) if user_id else None
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            draft = await conn.fetchrow(
                """
                INSERT INTO directive_drafts
                    (tenant_id, session_id, created_by, project_key, title, content,
                     risk_level, confidence, source_message_ids, classification, model_used)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::uuid[], $10::jsonb, $11)
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
                model_used,
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
                json.dumps({
                    "source_message_ids": [str(value) for value in source_ids],
                    "composer_draft": source.composer_draft,
                }, ensure_ascii=False),
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


def _safe_milestone_text(value: Any) -> str:
    """Keep milestone prose from becoming directive delimiters or header fields."""
    text = str(value or "").strip().replace(">>>DIRECTIVE_", "›››DIRECTIVE_")
    return re.sub(
        r"(?im)^(\s*)(TASK_ID|TITLE|PRIORITY|SIZE|MODEL|DESCRIPTION):",
        lambda match: f"{match.group(1)}{match.group(2)}：", text,
    )


async def _milestone_source_ids(
    conn, *, session_id: Any, tenant_id: Any, goal_id: Any, milestone_id: str
) -> list[Any]:
    """Recent turns of the linked session at transition time, as draft sources.

    Leaves the list empty (and says so) rather than citing unrelated messages.
    """
    rows = await _fetch_recent_context_messages(
        conn, session_id=session_id, tenant_id=tenant_id,
    )
    source_ids = _source_ids_with_user_request(rows)
    if not source_ids:
        logger.warning(
            "milestone_draft_source_messages_unresolved goal=%s milestone=%s session=%s rows=%d",
            goal_id, milestone_id, session_id, len(rows),
        )
    return source_ids


async def save_milestone_draft(conn, *, goal: Any, milestone: Any) -> None:
    """Save a review-only milestone directive in the existing draft store.

    The caller owns a short, separate draft transaction. The advisory lock
    serializes retries for this milestone without locking goals or milestones.
    """
    milestone_id = str(milestone["id"])
    tenant_id = goal["tenant_id"]
    project_key = normalize_project_key(goal["project"])
    priority = {"P0": "P0-CRITICAL", "P1": "P1-HIGH", "P2": "P2-MEDIUM", "P3": "P3-LOW"}.get(
        goal["priority"], "P2-MEDIUM"
    )
    title = " ".join(_safe_milestone_text(milestone["title"]).split())[:200]
    description = _safe_milestone_text(milestone["description"])
    criteria = _safe_milestone_text(milestone["completion_criteria"])
    content = (
        ">>>DIRECTIVE_START\n"
        f"TASK_ID: {project_key}-DRAFT-{uuid.UUID(milestone_id).hex}\n"
        f"TITLE: {title}\n"
        f"PRIORITY: {priority}\n"
        "SIZE: M\nMODEL: AUTO\nDESCRIPTION:\n"
        f"목표: {title}\n"
        f"현재 근거: 마일스톤 {milestone_id} 자동 전환\n"
        f"허용 범위: {description or title}\n"
        "금지 범위: 승인 전 자동 제출 및 실행\n"
        f"구현 요구사항: {description or title}\n"
        f"검증 기준: {criteria or '마일스톤 완료 기준을 담당자가 확인'}\n"
        "완료 보고: 검증 결과와 변경 내용을 보고\n"
        ">>>DIRECTIVE_END"
    )
    classification = json.dumps({
        "source_mode": "milestone_auto_advance",
        "goal_id": str(goal["id"]),
        "milestone_id": milestone_id,
        "requires_human_review": True,
        "auto_submit": False,
    }, ensure_ascii=False)
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", milestone_id)
    current = await conn.fetchrow(
        """SELECT id, session_id, artifact_id, content, status, current_revision
           FROM directive_drafts
           WHERE tenant_id = $1 AND classification->>'source_mode' = 'milestone_auto_advance'
             AND classification->>'milestone_id' = $2
           ORDER BY created_at LIMIT 1 FOR UPDATE""",
        tenant_id, milestone_id,
    )
    if current and (current["status"] != "draft" or current["content"] == content):
        return
    if current:
        latest_source = await conn.fetchval(
            """SELECT change_source FROM directive_draft_revisions
               WHERE draft_id = $1 ORDER BY revision DESC LIMIT 1""",
            current["id"],
        )
        if latest_source in {"user_edit", "artifact_edit"}:
            return
        draft_id = current["id"]
        revision = current["current_revision"] + 1
        source_ids = await _milestone_source_ids(
            conn, session_id=current["session_id"], tenant_id=tenant_id,
            goal_id=goal["id"], milestone_id=milestone_id,
        )
        await conn.execute(
            """UPDATE directive_drafts SET title=$2, content=$3,
               current_revision=$4, updated_at=NOW() WHERE id=$1""",
            draft_id, title, content, revision,
        )
        if current["artifact_id"]:
            await conn.execute(
                """UPDATE chat_artifacts SET title=$2, content=$3,
                   metadata=metadata || jsonb_build_object('revision', $4::integer),
                   updated_at=NOW() WHERE id=$1""",
                current["artifact_id"], f"지시 초안: {title}"[:200], content, revision,
            )
        action = "edited"
        change_source = "regenerated"
    else:
        from app.services.goal_manager import active_link_predicate, link_optional_columns

        link_columns = await link_optional_columns(conn)
        session = await conn.fetchrow(
            f"""SELECT s.id, s.workspace_id FROM goal_task_links l
               JOIN chat_sessions s ON s.id::text = l.task_id AND s.tenant_id = $2
               WHERE l.goal_id = $1::uuid AND l.task_type = 'chat_session'
                 {active_link_predicate(link_columns, 'l')}
               ORDER BY (l.milestone_id = $3::uuid) DESC NULLS LAST,
                        l.created_at DESC, l.id DESC LIMIT 1""",
            goal["id"], tenant_id, milestone["id"],
        )
        if not session:
            logger.warning("milestone_draft_skipped_no_active_session goal=%s milestone=%s",
                           goal["id"], milestone_id)
            return
        source_ids = await _milestone_source_ids(
            conn, session_id=session["id"], tenant_id=tenant_id,
            goal_id=goal["id"], milestone_id=milestone_id,
        )
        draft_id = await conn.fetchval(
            """INSERT INTO directive_drafts
               (tenant_id, session_id, project_key, title, content, risk_level,
                confidence, source_message_ids, classification)
               VALUES ($1,$2,$3,$4,$5,$6,1.0,$7::uuid[],$8::jsonb)
               RETURNING id""",
            tenant_id, session["id"], project_key, title, content,
            classify_risk(content), source_ids, classification,
        )
        artifact_id = await conn.fetchval(
            """INSERT INTO chat_artifacts
               (tenant_id, session_id, workspace_id, type, title, content, metadata)
               VALUES ($1,$2,$3,'report',$4,$5,$6::jsonb) RETURNING id""",
            tenant_id, session["id"], session["workspace_id"],
            f"지시 초안: {title}"[:200], content,
            json.dumps({"subtype": "directive_draft", "draft_id": str(draft_id),
                        "revision": 1, "status": "draft", "requires_human_review": True}),
        )
        await conn.execute(
            "UPDATE directive_drafts SET artifact_id=$2 WHERE id=$1", draft_id, artifact_id,
        )
        revision = 1
        action = "created"
        change_source = "fallback"
    await conn.execute(
        """INSERT INTO directive_draft_revisions
           (tenant_id, draft_id, revision, title, content, change_source, metadata)
           VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)""",
        tenant_id, draft_id, revision, title, content, change_source,
        json.dumps({
            "milestone_id": milestone_id,
            "source_message_ids": [str(value) for value in source_ids],
        }),
    )
    await conn.execute(
        """INSERT INTO directive_draft_events
           (tenant_id, draft_id, revision, action, metadata)
           VALUES ($1,$2,$3,$4,$5::jsonb)""",
        tenant_id, draft_id, revision, action,
        json.dumps({"source_mode": "milestone_auto_advance"}),
    )


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


async def load_draft_trace(conn, *, tenant_id: Any, draft_id: Any) -> dict[str, Any]:
    """Source messages → revisions → events for one draft, with link checks.

    Scoped to the tenant: a draft of another tenant is reported as not found.
    ``integrity`` counts cross-session, cross-tenant and cross-goal links so a
    caller can assert that all of them are zero.
    """
    tenant_uuid = uuid.UUID(str(tenant_id))
    draft = await conn.fetchrow(
        """SELECT id, tenant_id, session_id, status, current_revision,
                  source_message_ids, classification, created_at
             FROM directive_drafts WHERE id = $1 AND tenant_id = $2""",
        uuid.UUID(str(draft_id)), tenant_uuid,
    )
    if not draft:
        raise DraftNotFoundError("draft not found")
    draft_data = _serialize_row(draft)
    classification = draft_data.get("classification") or {}
    source_ids = list(draft["source_message_ids"] or [])
    # Unscoped lookup on purpose: it is how a foreign link would be detected.
    # Only ids and scope columns are read for messages outside this draft.
    message_rows = await conn.fetch(
        """SELECT id, session_id, tenant_id, role, created_at, LEFT(content, 120) AS preview
             FROM chat_messages WHERE id = ANY($1::uuid[])""",
        source_ids,
    ) if source_ids else []
    by_id = {row["id"]: row for row in message_rows}
    messages: list[dict[str, Any]] = []
    foreign_session = foreign_tenant = 0
    for message_id in source_ids:
        row = by_id.get(message_id)
        if row is None:
            continue
        same_tenant = row["tenant_id"] == draft["tenant_id"]
        same_session = row["session_id"] == draft["session_id"]
        foreign_tenant += not same_tenant
        foreign_session += same_tenant and not same_session
        entry = {"id": str(row["id"]), "role": row["role"],
                 "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                 "in_draft_session": same_tenant and same_session}
        if same_tenant and same_session:
            entry["preview"] = row["preview"]
        messages.append(entry)
    revisions = await conn.fetch(
        """SELECT revision, change_source, metadata, created_at
             FROM directive_draft_revisions
            WHERE draft_id = $1 AND tenant_id = $2 ORDER BY revision""",
        draft["id"], tenant_uuid,
    )
    events = await conn.fetch(
        """SELECT revision, action, created_at
             FROM directive_draft_events
            WHERE draft_id = $1 AND tenant_id = $2 ORDER BY created_at, id""",
        draft["id"], tenant_uuid,
    )
    revision_numbers = {row["revision"] for row in revisions}
    goal_mismatch = None
    goal_id = classification.get("goal_id")
    if goal_id:
        # The draft's goal must belong to the same tenant, own the milestone,
        # and still be linked to the draft's session.
        goal_ok = await conn.fetchval(
            """SELECT EXISTS (
                   SELECT 1 FROM goals g
                    WHERE g.id = $1::uuid AND g.tenant_id = $2
                      AND ($3::text IS NULL OR EXISTS (
                          SELECT 1 FROM milestones m
                           WHERE m.id = $3::uuid AND m.goal_id = g.id))
                      AND EXISTS (
                          SELECT 1 FROM goal_task_links l
                           WHERE l.goal_id = g.id AND l.task_type = 'chat_session'
                             AND l.task_id = $4::text))""",
            goal_id, tenant_uuid, classification.get("milestone_id"), str(draft["session_id"]),
        )
        goal_mismatch = 0 if goal_ok else 1
    revision_data = []
    for row in revisions:
        metadata = row["metadata"]
        if isinstance(metadata, str):
            metadata = json.loads(metadata or "{}")
        revision_data.append({
            "revision": row["revision"], "change_source": row["change_source"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "source_message_ids": list((metadata or {}).get("source_message_ids") or []),
        })
    integrity = {
        "source_message_count": len(source_ids),
        "resolved_message_count": len(messages),
        "missing_message_ids": [str(v) for v in source_ids if v not in by_id],
        "foreign_session_messages": foreign_session,
        "foreign_tenant_messages": foreign_tenant,
        "goal_mismatch": goal_mismatch,
        "events_without_revision": sum(
            1 for row in events if row["revision"] not in revision_numbers
        ),
    }
    return {
        "draft": {
            "id": draft_data["id"], "tenant_id": draft_data["tenant_id"],
            "session_id": draft_data["session_id"], "status": draft_data["status"],
            "current_revision": draft["current_revision"],
            "source_mode": classification.get("source_mode"),
            "goal_id": goal_id, "milestone_id": classification.get("milestone_id"),
        },
        "source_messages": messages,
        "revisions": revision_data,
        "events": [
            {"revision": row["revision"], "action": row["action"],
             "created_at": row["created_at"].isoformat() if row["created_at"] else None}
            for row in events
        ],
        "integrity": integrity,
        "linked": bool(messages) and bool(revision_data) and bool(events)
        and not foreign_session and not foreign_tenant and not goal_mismatch
        and not integrity["missing_message_ids"] and not integrity["events_without_revision"],
    }


async def trace_draft_sources(*, tenant_id: str, draft_id: str) -> dict[str, Any]:
    async with get_pool().acquire() as conn:
        return await load_draft_trace(conn, tenant_id=tenant_id, draft_id=draft_id)


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
