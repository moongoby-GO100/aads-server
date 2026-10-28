"""User-facing Smart Browser artifact state derived from persisted task evidence."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
import urllib.parse
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_LEARNING_STATES = {"learned", "reused", "rediscover", "approval_required", "idle"}
_FRESHNESS_STATES = {"CURRENT", "STALE", "CONFLICT", "UNAVAILABLE", "NOT_APPLICABLE"}


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def build_browser_artifact_status(
    *, task: Mapping[str, Any], frame: Mapping[str, Any] | None,
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    result = _dict(task.get("result"))
    metadata = _dict((frame or {}).get("metadata"))
    knowledge = _dict(result.get("site_knowledge")) or _dict(metadata.get("site_knowledge"))
    freshness = _dict(result.get("freshness_gate")) or _dict(metadata.get("freshness_gate"))
    learning_state = str(knowledge.get("learning_state") or "idle")
    if learning_state not in _LEARNING_STATES:
        learning_state = "idle"
    freshness_status = str(freshness.get("status") or "NOT_APPLICABLE").upper()
    if freshness_status not in _FRESHNESS_STATES:
        freshness_status = "UNAVAILABLE"
    latest_error = str(task.get("error") or "")
    for event in events:
        payload = _dict(event.get("payload"))
        if not latest_error and event.get("event_type") in {"failed", "error"}:
            latest_error = str(payload.get("user_message") or payload.get("reason") or "작업이 중단되었습니다.")
    status = str(task.get("status") or "")
    approval_required = bool(task.get("requires_approval")) or status in {"approval_required", "auth_required"}
    retry_action = freshness.get("retry_action") or result.get("retry_action")
    if not retry_action and (latest_error or freshness_status != "CURRENT"):
        retry_action = {"action": "retry_browser_task", "task_id": str(task.get("id") or "")}
    source = str(metadata.get("source") or result.get("execution_source") or "")
    actor = {
        "self_hosted_playwright": "Browser",
        "pc_agent_browser_screenshot": "Windows PC",
        "human_gateway": "Human",
    }.get(source, "Browser" if source else "대기")
    progress = metadata.get("progress_percent", result.get("progress_percent", task.get("progress_percent")))
    try:
        progress_percent = max(0, min(100, int(progress))) if progress is not None else None
    except (TypeError, ValueError):
        progress_percent = None
    return {
        "current_url": str((frame or {}).get("current_url") or task.get("target_url") or ""),
        "execution_actor": actor,
        "current_step": str((frame or {}).get("current_step") or task.get("current_step") or "대기 중"),
        "progress_percent": progress_percent,
        "learning_state": learning_state,
        "freshness_status": freshness_status,
        "evidence_count": len(knowledge.get("evidence_refs") or freshness.get("facts") or []),
        "approval_required": approval_required,
        "last_error": latest_error,
        "retry_action": retry_action,
        "can_retry": bool(retry_action),
    }


# --- 접근 통제 스크린샷 아티팩트 -------------------------------------------------
# 채팅 결과에 base64 본문을 싣지 않고, tenant/session/work_key 에 묶인 참조만 돌려준다.
# 저장소는 API 슬롯(컨테이너) 로컬 디스크다 — 블루/그린 전환 시 이전 슬롯의 참조는 읽을 수 없다.
SCREENSHOT_ARTIFACT_DIR = Path(os.getenv("AADS_BROWSER_ARTIFACT_DIR", "/tmp/aads-browser-artifacts"))
SCREENSHOT_ARTIFACT_TTL_SECONDS = int(os.getenv("AADS_BROWSER_ARTIFACT_TTL_SECONDS", "86400") or "86400")
SCREENSHOT_ARTIFACT_MAX_FILES = int(os.getenv("AADS_BROWSER_ARTIFACT_MAX_FILES", "200") or "200")
SCREENSHOT_ARTIFACT_MAX_BYTES = 12 * 1024 * 1024
ARTIFACT_URL_PREFIX = "/api/v1/browser-tasks/artifacts/"
_ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class ArtifactError(ValueError):
    """아티팩트 저장/조회 거부. code 는 호출자가 사용자 메시지로 매핑한다."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def redact_artifact_url(url: str) -> str:
    """토큰·세션 값이 실리기 쉬운 userinfo/query/fragment 를 제거한 origin+path 만 남긴다."""
    try:
        parts = urllib.parse.urlsplit(str(url or ""))
    except ValueError:
        return ""
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return ""
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    return f"{parts.scheme}://{host}{parts.path}"[:300]


def _artifact_paths(artifact_id: str) -> tuple[Path, Path]:
    return (SCREENSHOT_ARTIFACT_DIR / f"{artifact_id}.png", SCREENSHOT_ARTIFACT_DIR / f"{artifact_id}.json")


def _prune_artifacts(now: float) -> None:
    try:
        metas = sorted(SCREENSHOT_ARTIFACT_DIR.glob("*.json"), key=lambda path: path.stat().st_mtime)
    except OSError:
        return
    excess = max(0, len(metas) - SCREENSHOT_ARTIFACT_MAX_FILES + 1)
    for index, meta_path in enumerate(metas):
        try:
            expired = now - meta_path.stat().st_mtime > SCREENSHOT_ARTIFACT_TTL_SECONDS
            if expired or index < excess:
                meta_path.with_suffix(".png").unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
        except OSError:
            continue


def save_screenshot_artifact(
    *, png: bytes, tenant_id: str, session_id: str = "", work_key: str = "",
    url: str = "", target_id: str = "", source: str = "",
) -> dict[str, Any]:
    """PNG 를 tenant 에 묶어 저장하고 참조(본문 없음)를 돌려준다. tenant 미확인이면 거부한다."""
    tenant = str(tenant_id or "").strip()
    if not tenant:
        raise ArtifactError("tenant_required", "tenant 를 확인할 수 없어 스크린샷 참조를 만들지 않습니다")
    if not png or not png.startswith(_PNG_MAGIC):
        raise ArtifactError("not_png", "PNG 가 아닌 데이터는 저장하지 않습니다")
    if len(png) > SCREENSHOT_ARTIFACT_MAX_BYTES:
        raise ArtifactError("too_large", "스크린샷이 너무 큽니다")
    now = time.time()
    SCREENSHOT_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    _prune_artifacts(now)
    artifact_id = secrets.token_urlsafe(24)
    png_path, meta_path = _artifact_paths(artifact_id)
    meta = {
        "artifact_id": artifact_id,
        "tenant_id": tenant,
        "session_id": str(session_id or ""),
        "work_key": str(work_key or ""),
        "target_id": str(target_id or ""),
        "url": redact_artifact_url(url),
        "source": str(source or ""),
        "bytes": len(png),
        "sha256": hashlib.sha256(png).hexdigest(),
        "created_at": now,
    }
    png_path.write_bytes(png)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    for path in (png_path, meta_path):
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return {
        "artifact_id": artifact_id,
        "ref": f"{ARTIFACT_URL_PREFIX}{artifact_id}",
        **{key: meta[key] for key in ("url", "target_id", "bytes", "sha256", "work_key")},
        "expires_in_seconds": SCREENSHOT_ARTIFACT_TTL_SECONDS,
    }


def load_screenshot_artifact(
    *, artifact_id: str, tenant_id: str, session_id: str = "", work_key: str = "",
) -> tuple[bytes, dict[str, Any]]:
    """tenant(필수)·session·work_key 가 저장 시점과 일치할 때만 본문을 돌려준다.

    존재하지 않음과 접근 거부를 구분하지 않고 모두 not_found 로 응답한다(참조 열거 방지).
    """
    artifact_id = str(artifact_id or "")
    tenant = str(tenant_id or "").strip()
    if not tenant or not _ARTIFACT_ID_RE.match(artifact_id):
        raise ArtifactError("not_found")
    png_path, meta_path = _artifact_paths(artifact_id)
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        png = png_path.read_bytes()
    except (OSError, ValueError):
        raise ArtifactError("not_found") from None
    if time.time() - float(meta.get("created_at") or 0) > SCREENSHOT_ARTIFACT_TTL_SECONDS:
        raise ArtifactError("not_found")
    if meta.get("tenant_id") != tenant:
        raise ArtifactError("not_found")
    if meta.get("session_id") and session_id and meta["session_id"] != str(session_id):
        raise ArtifactError("not_found")
    if meta.get("work_key") and work_key and meta["work_key"] != str(work_key):
        raise ArtifactError("not_found")
    if hashlib.sha256(png).hexdigest() != meta.get("sha256"):
        raise ArtifactError("not_found")
    return png, meta
