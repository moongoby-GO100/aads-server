"""사이트별 공지 팝업 공용 목록(browser_site_overlays) — 조회·저장·가림 처리.

세 소비자가 같은 approved 행만 읽는다: 레시피 실행기 dismiss_overlays, '사이트 공지 팝업 닫기' 도구,
채팅 browser_click 의 ELEMENT_OBSCURED 처리. candidate/rejected/disabled 는 어떤 경로에서도 누르지 않는다.
저장 시 schema.normalize_overlay_spec 의 셀렉터 검증과 OVERLAY_FORBIDDEN_MARKERS 검사를 그대로 적용한다.
DB 스키마: migrations/20261028_browser_site_overlays.sql
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from app.core.db_pool import get_pool
from app.services.work_recipe.audit import mask_secrets
from app.services.work_recipe.schema import (
    OVERLAY_FORBIDDEN_MARKERS,
    OVERLAY_SENSITIVE_MARKERS,
    SITE_POPUP_CLOSE_LABELS,
    SitePopupPolicy,
    normalize_overlay_spec,
    overlay_host_matches,
)

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 30.0
STATUSES = ("candidate", "approved", "rejected", "disabled")
SOURCES = ("seed", "manual", "recorded")
API_SOURCES = ("manual", "recorded")
LABEL_MAX_LEN = 200
EVIDENCE_KEYS = ("screenshot_url", "page_url", "note")
EVIDENCE_VALUE_MAX_LEN = 500
OBSCURED_TEXT_LEN = 40
_CHECK_TEXT_LEN = 600
_GONE_POLL_SECONDS = 0.15
_GONE_POLL_ROUNDS = 8
_PROBE_TIMEOUT_SECONDS = 8.0

_GLOBAL_TENANT = uuid.UUID(int=0)
# 결정 → (허용되는 현재 상태들, 새 상태)
_TRANSITIONS: dict[str, tuple[tuple[str, ...], str]] = {
    "approved": (("candidate", "disabled"), "approved"),
    "rejected": (("candidate",), "rejected"),
    "disabled": (("approved",), "disabled"),
}


class SiteOverlayError(ValueError):
    """저장·상태 변경 거부. code 는 API 가 상태 코드로 옮긴다."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class SiteOverlay:
    id: str
    tenant_id: str | None
    domain: str
    root_selector: str
    close_selector: str
    label: str = ""


# ── 검증 ─────────────────────────────────────────────────────────────────────


def validate_overlay_fields(domain: Any, root_selector: Any, close_selector: Any) -> tuple[str, str, str]:
    """레시피 overlays 와 같은 검증. 통과하면 정규화된 (domain, root, close)."""
    try:
        spec = normalize_overlay_spec(
            {"overlays": [{"domain": domain, "root": root_selector, "close": close_selector}]}, seq=0,
        )
    except ValueError as exc:
        message = re.sub(r"^steps\[\d+\]:\s*", "", str(exc))
        raise SiteOverlayError("invalid_overlay", message) from exc
    item = spec["overlays"][0]
    return item["domain"], item["root"], item["close"]


def sanitize_label(label: Any) -> str:
    return mask_secrets(re.sub(r"\s+", " ", str(label or "")).strip())[:LABEL_MAX_LEN]


def sanitize_evidence(evidence: Any) -> dict[str, str]:
    """screenshot_url·page_url·note 문자열만 남기고 비밀값은 가린다. 그 밖의 키는 거부."""
    if evidence in (None, ""):
        return {}
    if not isinstance(evidence, Mapping):
        raise SiteOverlayError("invalid_evidence", "evidence 는 객체여야 합니다")
    unknown = sorted(set(map(str, evidence)) - set(EVIDENCE_KEYS))
    if unknown:
        raise SiteOverlayError("invalid_evidence", f"evidence 에 허용되지 않는 키: {', '.join(unknown)}")
    return {
        key: mask_secrets(str(evidence[key]))[:EVIDENCE_VALUE_MAX_LEN]
        for key in EVIDENCE_KEYS
        if evidence.get(key) not in (None, "")
    }


# ── 조회(approved 전용, 짧은 캐시) ────────────────────────────────────────────

_cache: dict[str, Any] = {"at": 0.0, "rows": []}


def invalidate_cache() -> None:
    _cache["at"] = 0.0
    _cache["rows"] = []


async def _load_approved() -> list[SiteOverlay]:
    async with get_pool().acquire() as conn:
        records = await conn.fetch(
            "SELECT id, tenant_id, domain, root_selector, close_selector, label "
            "FROM browser_site_overlays WHERE status = 'approved' ORDER BY domain, root_selector"
        )
    rows: list[SiteOverlay] = []
    for record in records:
        try:
            domain, root, close = validate_overlay_fields(
                record["domain"], record["root_selector"], record["close_selector"],
            )
        except SiteOverlayError:
            logger.warning("browser_site_overlays_invalid_row_skipped id=%s", record["id"])
            continue
        rows.append(SiteOverlay(
            id=str(record["id"]),
            tenant_id=str(record["tenant_id"]) if record["tenant_id"] else None,
            domain=domain,
            root_selector=root,
            close_selector=close,
            label=str(record["label"] or ""),
        ))
    return rows


async def _approved_rows() -> list[SiteOverlay]:
    now = time.monotonic()
    if _cache["rows"] and now - float(_cache["at"]) < CACHE_TTL_SECONDS:
        return list(_cache["rows"])
    rows = await _load_approved()
    _cache["rows"], _cache["at"] = list(rows), now
    return rows


async def approved_overlays_for_host(host: str, tenant_id: Any = None) -> list[SiteOverlay]:
    """호스트(또는 상위 도메인)와 맞는 approved 행. 전역 행 + 해당 테넌트 행. DB 오류면 빈 목록(닫지 않는다)."""
    host = str(host or "").strip().lower().rstrip(".")
    if not host:
        return []
    try:
        rows = await _approved_rows()
    except Exception as exc:  # noqa: BLE001
        logger.warning("browser_site_overlays_lookup_failed error=%s", type(exc).__name__)
        return []
    tenant = str(tenant_id or "").strip().lower()
    return [
        row for row in rows
        if (row.tenant_id is None or row.tenant_id.lower() == tenant) and overlay_host_matches(host, row.domain)
    ]


def overlays_to_step_specs(rows: list[SiteOverlay]) -> list[dict[str, str]]:
    """레시피 단계의 overlays 항목({domain, root, close}) 형태로."""
    return [{"domain": r.domain, "root": r.root_selector, "close": r.close_selector} for r in rows]


async def site_popup_policy_for_host(host: str, tenant_id: Any = None) -> SitePopupPolicy | None:
    """'사이트 공지 팝업 닫기' 도구용 정책 — approved 루트들을 한 셀렉터 목록으로 묶는다."""
    rows = await approved_overlays_for_host(host, tenant_id)
    if not rows:
        return None
    roots = list(dict.fromkeys(r.root_selector for r in rows))
    return SitePopupPolicy(domain=rows[0].domain, popup_selector=", ".join(roots))


# ── 관리(목록·후보 등록·상태 변경) ─────────────────────────────────────────────


def _tenant_uuid(value: Any) -> uuid.UUID | None:
    if value in (None, "", "null"):
        return None
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _row_dict(record: Any) -> dict[str, Any]:
    data = dict(record)
    for key in ("id", "tenant_id"):
        if data.get(key) is not None:
            data[key] = str(data[key])
    evidence = data.get("evidence")
    if isinstance(evidence, str):
        try:
            data["evidence"] = json.loads(evidence)
        except ValueError:
            data["evidence"] = {}
    for key in ("approved_at", "created_at", "updated_at"):
        if data.get(key) is not None:
            data[key] = data[key].isoformat()
    return data


_COLUMNS = (
    "id, tenant_id, domain, root_selector, close_selector, label, status, source, evidence, "
    "approved_by, approved_at, created_at, updated_at"
)


async def list_overlays(
    *, tenant_id: Any, status: str | None = None, domain: str | None = None, limit: int = 200,
) -> list[dict[str, Any]]:
    if status and status not in STATUSES:
        raise SiteOverlayError("invalid_status", f"status 는 {', '.join(STATUSES)} 중 하나여야 합니다")
    async with get_pool().acquire() as conn:
        records = await conn.fetch(
            f"SELECT {_COLUMNS} FROM browser_site_overlays "
            "WHERE (tenant_id IS NULL OR tenant_id = $1::uuid) "
            "AND ($2::text IS NULL OR status = $2) AND ($3::text IS NULL OR domain = $3) "
            "ORDER BY domain, root_selector LIMIT $4",
            _tenant_uuid(tenant_id), status or None, (str(domain).strip().lower() or None) if domain else None,
            max(1, min(int(limit), 500)),
        )
    return [_row_dict(r) for r in records]


async def add_candidate(
    *, tenant_id: Any, domain: Any, root_selector: Any, close_selector: Any, label: Any = "",
    source: str = "manual", evidence: Any = None,
) -> dict[str, Any]:
    """후보 등록 — status 는 항상 candidate. 승인은 별도 결정 경로뿐이다."""
    if source not in API_SOURCES:
        raise SiteOverlayError("invalid_source", f"source 는 {', '.join(API_SOURCES)} 중 하나여야 합니다")
    domain_ok, root_ok, close_ok = validate_overlay_fields(domain, root_selector, close_selector)
    clean_evidence = sanitize_evidence(evidence)
    scope = _tenant_uuid(tenant_id)
    async with get_pool().acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM browser_site_overlays "
            "WHERE COALESCE(tenant_id, $1::uuid) = COALESCE($2::uuid, $1::uuid) AND domain = $3 AND root_selector = $4",
            _GLOBAL_TENANT, scope, domain_ok, root_ok,
        )
        if exists:
            raise SiteOverlayError("overlay_exists", "같은 (테넌트, 도메인, root) 항목이 이미 있습니다")
        record = await conn.fetchrow(
            "INSERT INTO browser_site_overlays "
            "(tenant_id, domain, root_selector, close_selector, label, status, source, evidence) "
            f"VALUES ($1::uuid, $2, $3, $4, $5, 'candidate', $6, $7::jsonb) RETURNING {_COLUMNS}",
            scope, domain_ok, root_ok, close_ok, sanitize_label(label), source, json.dumps(clean_evidence, ensure_ascii=False),
        )
    return _row_dict(record)


async def decide_overlay(*, overlay_id: Any, tenant_id: Any, decision: str, decided_by: str) -> dict[str, Any]:
    """approved | rejected | disabled. 승인자는 인증된 주체로만 정해진다(호출자가 보장)."""
    verdict = str(decision or "").strip().lower()
    if verdict not in _TRANSITIONS:
        raise SiteOverlayError("invalid_decision", "decision 은 approved | rejected | disabled 이어야 합니다")
    allowed_from, new_status = _TRANSITIONS[verdict]
    try:
        key = uuid.UUID(str(overlay_id))
    except ValueError as exc:
        raise SiteOverlayError("overlay_not_found") from exc
    async with get_pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            f"SELECT {_COLUMNS} FROM browser_site_overlays "
            "WHERE id = $1 AND (tenant_id IS NULL OR tenant_id = $2::uuid) FOR UPDATE",
            key, _tenant_uuid(tenant_id),
        )
        if row is None:
            raise SiteOverlayError("overlay_not_found")
        if row["status"] not in allowed_from:
            raise SiteOverlayError("invalid_transition", f"{row['status']} 상태에서는 {verdict} 할 수 없습니다")
        if new_status == "approved":
            validate_overlay_fields(row["domain"], row["root_selector"], row["close_selector"])
        record = await conn.fetchrow(
            "UPDATE browser_site_overlays SET status = $2, "
            "approved_by = CASE WHEN $2 = 'approved' THEN $3 ELSE approved_by END, "
            "approved_at = CASE WHEN $2 = 'approved' THEN now() ELSE approved_at END, "
            f"updated_at = now() WHERE id = $1 RETURNING {_COLUMNS}",
            key, new_status, str(decided_by or "")[:200],
        )
    invalidate_cache()
    return _row_dict(record)


# ── 채팅 클릭 가림 진단·승인된 팝업 닫기 ───────────────────────────────────────

_OBSCURE_SCRIPT = """(el, args) => {
    const norm = s => String(s || '').replace(/\\s+/g, ' ').trim();
    const isVisible = n => {
        if (!n || !n.isConnected) return false;
        const r = n.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) return false;
        const cs = getComputedStyle(n);
        return cs.display !== 'none' && cs.visibility !== 'hidden' && parseFloat(cs.opacity || '1') > 0;
    };
    const labelOf = n => norm(n.innerText || n.textContent || n.getAttribute('aria-label')
        || n.getAttribute('title') || n.value || '');
    const estimate = n => {
        const role = n.getAttribute('role');
        if (role && /^[a-z]+$/.test(role)) return '[role="' + role + '"]';
        if (n.tagName === 'DIALOG') return 'dialog';
        if (n.getAttribute('aria-modal') === 'true') return '[aria-modal="true"]';
        if (n.id && /^[A-Za-z][A-Za-z0-9_-]{0,60}$/.test(n.id)) return '#' + n.id;
        const c = n.classList && n.classList[0];
        if (c && /^[A-Za-z][A-Za-z0-9_-]{0,60}$/.test(c)) return n.tagName.toLowerCase() + '.' + c;
        return n.tagName.toLowerCase();
    };
    if (args.mode === 'close_point') {
        const layer = window.__aadsObscuringLayer;
        if (!layer || !isVisible(layer)) return {close: null};
        let btn = null, via = '';
        try {
            btn = Array.from(layer.querySelectorAll(args.close)).find(isVisible) || null;
        } catch (e) { btn = null; }
        if (btn) via = 'selector';
        if (!btn) {
            btn = Array.from(layer.querySelectorAll('button, [role="button"], a, input[type="button"]'))
                .find(b => args.labels.includes(labelOf(b).toLowerCase()) && isVisible(b)) || null;
            if (btn) via = 'label';
        }
        if (!btn) return {close: null};
        const r = btn.getBoundingClientRect();
        const x = Math.round(r.left + r.width / 2), y = Math.round(r.top + r.height / 2);
        const hit = document.elementFromPoint(x, y);
        return {close: {via, label: labelOf(btn), x, y, hit: !!hit && (hit === btn || btn.contains(hit))}};
    }
    if (args.mode === 'gone') {
        const layer = window.__aadsObscuringLayer;
        return {gone: !layer || !isVisible(layer)};
    }
    if (args.mode === 'cleanup') {
        delete window.__aadsObscuringLayer;
        return {};
    }
    delete window.__aadsObscuringLayer;
    const r = el.getBoundingClientRect();
    const x = Math.round(r.left + r.width / 2), y = Math.round(r.top + r.height / 2);
    const hit = document.elementFromPoint(x, y);
    if (!hit || hit === el || el.contains(hit)) return {obscured: false};
    let matchedIndex = -1, layer = null;
    for (let i = 0; i < args.roots.length; i++) {
        let m = null;
        try { m = hit.closest(args.roots[i]); } catch (e) { m = null; }
        if (m && (!layer || layer.contains(m))) { layer = m; matchedIndex = i; }
    }
    if (!layer) {
        layer = hit.closest('dialog, [role="dialog"], [role="alertdialog"], [role="alert"], [aria-modal="true"]');
    }
    if (!layer) {
        for (let n = hit; n && n.nodeType === 1 && n !== document.body; n = n.parentElement) {
            const p = getComputedStyle(n).position;
            if (p === 'fixed' || p === 'sticky') layer = n;
        }
    }
    const node = layer || hit;
    const text = norm(node.innerText || node.textContent || '');
    const heading = node.querySelector && node.querySelector('h1, h2, h3, h4, h5, h6, [role="heading"]');
    if (matchedIndex >= 0) window.__aadsObscuringLayer = node;
    return {
        obscured: true,
        role: node.getAttribute('role') || (node.tagName === 'DIALOG' || node.getAttribute('aria-modal') === 'true' ? 'dialog' : ''),
        text: text.slice(0, args.textLen),
        check_text: (norm(node.getAttribute('aria-label')) + ' ' + norm(heading ? heading.textContent : '') + ' ' + text).slice(0, args.checkLen),
        root_selector: estimate(node),
        matched_index: matchedIndex,
    };
}"""


def _label_ok(info: Mapping[str, Any]) -> bool:
    """selector 로 찾은 닫기는 금지 문구만 아니면 된다(아이콘 전용 버튼은 빈 라벨). label 로 찾은 것은 정확한 닫기 라벨만."""
    label = re.sub(r"\s+", " ", str(info.get("label") or "")).strip().lower()
    if OVERLAY_FORBIDDEN_MARKERS.search(label):
        return False
    if info.get("via") == "label":
        return label in SITE_POPUP_CLOSE_LABELS
    return len(label) <= 20


async def _wait_gone(page: Any) -> bool:
    for _ in range(_GONE_POLL_ROUNDS):
        await asyncio.sleep(_GONE_POLL_SECONDS)
        state = await _page_eval(page, {"mode": "gone"})
        if isinstance(state, Mapping) and state.get("gone"):
            return True
    return False


async def _page_eval(page: Any, args: dict[str, Any]) -> Any:
    """el 인자가 필요 없는 모드는 documentElement 를 el 로 넘겨 같은 스크립트를 쓴다."""
    return await page.evaluate(
        "(args) => (" + _OBSCURE_SCRIPT + ")(document.documentElement, args)", args,
    )


async def _close_layer(page: Any, rule: SiteOverlay) -> str:
    """가린 팝업의 close 만 누르고 사라짐을 확인한다. 성공이면 '', 아니면 사유 코드."""
    labels = list(SITE_POPUP_CLOSE_LABELS)
    for _ in range(2):
        state = await _page_eval(page, {"mode": "close_point", "close": rule.close_selector, "labels": labels})
        close = state.get("close") if isinstance(state, Mapping) else None
        if not isinstance(close, Mapping):
            return "close_control_missing"
        if not _label_ok(close):
            return "close_label_forbidden"
        if not close.get("hit"):
            return "close_obscured"
        await page.mouse.click(close["x"], close["y"])
        if await _wait_gone(page):
            return ""
    return "not_closed_after_click"


async def handle_obscured_click(page: Any, target: Any, *, tenant_id: Any = None) -> dict[str, Any]:
    """클릭이 다른 요소에 가려졌을 때 가린 레이어를 진단하고, approved 목록과 일치하면 close 만 눌러 닫는다.

    결과: {obscured_by, auto_dismissed, retry, reason}. 절대 예외를 던지지 않는다.
    retry=True 일 때만 호출자가 원래 클릭을 정확히 1회 재시도한다.
    """
    out: dict[str, Any] = {"obscured_by": None, "auto_dismissed": [], "retry": False, "reason": ""}
    try:
        host = urlsplit(str(getattr(page, "url", "") or "")).hostname or ""
        rules = await approved_overlays_for_host(host, tenant_id)
        info = await asyncio.wait_for(
            target.evaluate(_OBSCURE_SCRIPT, {
                "mode": "probe", "roots": [r.root_selector for r in rules],
                "textLen": OBSCURED_TEXT_LEN, "checkLen": _CHECK_TEXT_LEN,
            }),
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
        if not isinstance(info, Mapping) or not info.get("obscured"):
            return out
        out["obscured_by"] = {
            "role": str(info.get("role") or "")[:40],
            "text": mask_secrets(str(info.get("text") or "")[:OBSCURED_TEXT_LEN]),
            "root_selector": str(info.get("root_selector") or "")[:120],
        }
        index = info.get("matched_index")
        if not isinstance(index, int) or not 0 <= index < len(rules):
            out["reason"] = "not_in_approved_list"
            return out
        if OVERLAY_SENSITIVE_MARKERS.search(str(info.get("check_text") or "")):
            out["reason"] = "sensitive_popup"
            return out
        rule = rules[index]
        reason = await _close_layer(page, rule)
        if reason:
            out["reason"] = reason
            return out
        out["auto_dismissed"] = [{"domain": rule.domain, "root": rule.root_selector, "verified_closed": True}]
        out["retry"] = True
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"probe_failed:{type(exc).__name__}"
        logger.info("browser_click_obscured_probe_failed error=%s", type(exc).__name__)
    finally:
        try:
            await _page_eval(page, {"mode": "cleanup"})
        except Exception:  # noqa: BLE001
            pass
    return out
