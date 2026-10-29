"""서버가 읽기 전용 관찰로 발급하는 DOM 증거 artifact.

register_e2e 의 화면 증거는 호출자 자기신고(screen_verified)가 아니라 이 모듈이 발급한
`browser_artifacts` 행(artifact_type='dom_evidence')을 id 로 가리킬 때만 신뢰한다.
행에는 tenant/session/agent/대상 호스트/url/captured_at/만료/run·세션 id/검증 assertion 과
이를 묶는 해시만 남고 원본 DOM·쿠키·입력값은 남지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from app.core.db_pool import get_pool
from app.services.work_recipe.audit import mask_url
from app.services.work_recipe.guard import normalize_domain

ARTIFACT_TYPE = "dom_evidence"
ORIGIN = "server_observation"
DEFAULT_TTL_SECONDS = 900
MAX_TTL_SECONDS = 3600
_BINDING_KEYS = (
    "tenant_id", "chat_session_id", "agent", "target_host", "url", "captured_at", "expires_at",
    "run_id", "browser_session_id", "work_key", "route", "assertions", "screenshot_sha256",
)


class DomEvidenceError(ValueError):
    """str(exc) 가 호출자에게 그대로 돌아가는 오류 코드다."""


def binding_hash(binding: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        {key: binding.get(key) for key in _BINDING_KEYS},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_iso(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _host_of(value: Any) -> str:
    raw = str(value or "").strip()
    try:
        parts = urlsplit(raw if "://" in raw else f"//{raw}")
    except ValueError:
        return ""
    return normalize_domain(parts.hostname or "")


def host_within(host: str, base: str) -> bool:
    return bool(host and base and (host == base or host.endswith("." + base)))


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except ValueError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


async def validate_server_observation_url(url: str) -> None:
    """Public server captures cannot target loopback, private or metadata addresses."""
    import asyncio
    import ipaddress
    import socket

    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in {"http", "https"} or not host or parts.username or parts.password:
        raise DomEvidenceError("dom_evidence_url_invalid")
    try:
        resolved = await asyncio.to_thread(socket.getaddrinfo, host, parts.port or 443, type=socket.SOCK_STREAM)
    except (OSError, ValueError):
        raise DomEvidenceError("dom_evidence_dns_unavailable") from None
    if not resolved or any(not ipaddress.ip_address(item[4][0]).is_global for item in resolved):
        raise DomEvidenceError("dom_evidence_private_target_rejected")


async def issue_dom_evidence(
    *, tenant_id: Any, chat_session_id: str, agent: str, url: str,
    verify: Any, browser_session_id: str = "", browser_work_key: str = "",
    run_id: str = "", lane: str = "", ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    """읽기 전용 verify(assertion) 를 지금 화면에서 실제로 돌려 통과했을 때만 artifact 를 발급한다."""
    from app.services.work_recipe.executor import BrowserRecipeExecutor
    from app.services.work_recipe.orchestrator import GenericVerificationExecutor
    from app.services.work_recipe.registration import normalize_verify_steps
    from app.services.work_recipe.schema import WorkRecipe

    if lane not in {"", "server", "pc", "pc_agent"}:
        raise DomEvidenceError("dom_evidence_lane_invalid")
    if lane == "server" and (browser_session_id or browser_work_key):
        raise DomEvidenceError("dom_evidence_lane_conflict")
    if not browser_session_id:
        await validate_server_observation_url(url)
    assertions = normalize_verify_steps(verify)
    if not assertions:
        raise DomEvidenceError("dom_evidence_verify_required")
    target_host = _host_of(url)
    if urlsplit(str(url or "")).scheme not in {"http", "https"} or not target_host:
        raise DomEvidenceError("dom_evidence_url_invalid")
    ttl = max(60, min(int(ttl_seconds or DEFAULT_TTL_SECONDS), MAX_TTL_SECONDS))
    tenant = uuid.UUID(str(tenant_id))
    session = str(browser_session_id or "").strip()
    work_key = str(browser_work_key or "").strip()
    # PC 세션 없는 관찰은 서버 URL 검증을 거친다. PC 세션과 혼용하지 않는다.
    context = {"smart_browser": {"server_access_blocked": True}} if session else {}

    recipe = WorkRecipe.from_dict({
        "name": "dom_evidence_observation", "domain": target_host,
        "steps": [{"action": "snapshot", "selector": "body"}], "verify": assertions,
        "metadata": {"lane": "server"} if lane == "server" else {},
    })
    executor = BrowserRecipeExecutor(browser_session_id=session or None, browser_work_key=work_key or None, tenant_id=tenant_id)
    verifier = GenericVerificationExecutor(executor, recipe)
    try:
        page = await executor._get_page({"url": url, "context": context})
        if session:
            page.url = str(await page.evaluate("window.location.href") or "")
    except Exception as exc:
        raise DomEvidenceError(f"dom_evidence_page_unavailable:{type(exc).__name__}") from exc
    current_url = str(getattr(page, "url", "") or "")
    if not session:
        await validate_server_observation_url(current_url)
    if urlsplit(current_url).scheme not in {"http", "https"} or not host_within(_host_of(current_url), target_host):
        raise DomEvidenceError("dom_evidence_origin_mismatch")

    last: dict[str, Any] = {}
    for step in recipe.verify:
        last = await verifier({**step.to_dict(), "phase": "verify", "context": context})
        if last.get("ok") is not True:
            raise DomEvidenceError(f"dom_evidence_verify_failed:{last.get('error') or 'unknown'}")
    screenshot = _json_dict(_json_dict(last.get("evidence")).get("screenshot"))
    screenshot_url = str(screenshot.get("url") or "")
    screenshot_sha = str(screenshot.get("sha256") or "")
    if not screenshot_url or not screenshot_sha:
        raise DomEvidenceError("dom_evidence_screen_artifact_required")

    captured = datetime.now(timezone.utc)
    binding = {
        "tenant_id": str(tenant), "chat_session_id": str(chat_session_id), "agent": str(agent),
        "target_host": target_host, "url": mask_url(current_url),
        "captured_at": _iso(captured), "expires_at": _iso(captured + timedelta(seconds=ttl)),
        "run_id": str(run_id or ""), "browser_session_id": session, "work_key": work_key,
        "route": "pc_agent" if session else "browser_agent",
        "assertions": assertions, "screenshot_sha256": screenshot_sha,
    }
    digest = binding_hash(binding)
    metadata = {**binding, "origin": ORIGIN, "verified": True, "binding_hash": digest}
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO browser_artifacts
                (tenant_id, artifact_type, source_url, content_hash, storage_uri, metadata)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                RETURNING id""",
            tenant, ARTIFACT_TYPE, binding["url"], digest, screenshot_url, json.dumps(metadata),
        )
    return {
        "artifact_id": str(row["id"]), "screenshot_url": screenshot_url, "url": binding["url"],
        "captured_at": binding["captured_at"], "expires_at": binding["expires_at"],
        "binding_hash": digest, "assertions": assertions,
    }


async def verify_dom_evidence(
    *, tenant_id: Any, chat_session_id: str, claimed: Mapping[str, Any], domain: str,
    verify: Any = None, now: datetime | None = None,
) -> dict[str, Any]:
    """artifact_id 로 서버 발급 행을 다시 읽어 묶인 값이 모두 맞을 때만 신뢰 evidence 를 돌려준다."""
    from app.services.work_recipe.registration import normalize_verify_steps

    try:
        artifact_uuid = uuid.UUID(str(claimed.get("artifact_id")))
        tenant = uuid.UUID(str(tenant_id))
    except (TypeError, ValueError) as exc:
        raise DomEvidenceError("dom_evidence_invalid_id") from exc
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, source_url, content_hash, storage_uri, metadata FROM browser_artifacts
                WHERE id=$1 AND tenant_id=$2 AND artifact_type=$3""",
            artifact_uuid, tenant, ARTIFACT_TYPE,
        )
    if row is None:
        raise DomEvidenceError("dom_evidence_not_found")
    meta = _json_dict(row["metadata"])
    digest = binding_hash(meta)
    if (meta.get("origin") != ORIGIN or meta.get("verified") is not True
            or digest != row["content_hash"] or digest != meta.get("binding_hash")):
        raise DomEvidenceError("dom_evidence_hash_mismatch")
    if meta.get("tenant_id") != str(tenant):
        raise DomEvidenceError("dom_evidence_tenant_mismatch")
    if meta.get("chat_session_id") != str(chat_session_id):
        raise DomEvidenceError("dom_evidence_session_mismatch")
    current = now or datetime.now(timezone.utc)
    captured, expires = _parse_iso(meta.get("captured_at")), _parse_iso(meta.get("expires_at"))
    if captured is None or expires is None or captured > current + timedelta(seconds=5):
        raise DomEvidenceError("dom_evidence_hash_mismatch")
    if expires <= current:
        raise DomEvidenceError("dom_evidence_expired")
    base = _host_of(domain)
    if not base or not host_within(meta.get("target_host") or "", base) \
            or not host_within(_host_of(meta.get("url")), base):
        raise DomEvidenceError("dom_evidence_target_mismatch")
    for field, stored in (("url", row["source_url"]), ("screenshot_url", row["storage_uri"]),
                          ("captured_at", meta.get("captured_at"))):
        if claimed.get(field) not in (None, "") and str(claimed[field]) != str(stored):
            raise DomEvidenceError("dom_evidence_claim_mismatch")
    stored_assertions = meta.get("assertions") or []
    requested = normalize_verify_steps(verify) if verify else []
    if requested and requested != stored_assertions:
        raise DomEvidenceError("dom_evidence_assertion_mismatch")
    return {
        "artifact_id": str(row["id"]), "screenshot_url": str(row["storage_uri"]),
        "url": str(row["source_url"]), "captured_at": meta["captured_at"],
        "expires_at": meta["expires_at"], "binding_hash": digest,
        "browser_session_id": meta.get("browser_session_id", ""), "work_key": meta.get("work_key", ""),
        "assertions": stored_assertions, "lane": "pc" if meta.get("route") == "pc_agent" else "server",
    }
