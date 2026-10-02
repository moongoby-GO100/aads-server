"""일반 browser_* 호출을 관찰해 반복 성공 경로를 승인 대기 레시피 초안으로 올린다.

흐름
  1. ToolExecutor 가 browser_* 8종 실행 직후 :func:`observe` 를 부른다.
  2. 같은 (채팅 세션, browser_work_key | browser_session_id) 묶음의 단계를 메모리에 모은다.
  3. 화면 증거(snapshot/screenshot)가 오류 화면이 아닐 때만 '성공 시퀀스' 로 확정한다.
  4. 확정된 시퀀스를 DB 에 남기고, 같은 서명이 **서로 다른 채팅 세션 2개 이상**에서
     성공하면 registration.request_registration 으로 pending 초안 1건을 만든다.

비밀값 규칙: fill 값은 어떤 경로로도 이 모듈의 상태·DB·로그에 들어오지 않는다.
쿠키·토큰이 실리기 쉬운 URL query/fragment 도 저장하지 않는다. 자동 승인은 기본 꺼짐이며
조회 전용 초안만 auto_approve 정책이 처리한다(SMART_BROWSER_AUTO_APPROVE_READ).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from app.core.db_pool import get_pool
from app.services.work_recipe import recorder as recorder_module
from app.services.work_recipe import registration, store
from app.services.work_recipe.schema import RecipeInput

logger = logging.getLogger(__name__)

FLAG_ENV = "SMART_BROWSER_AUTO_RECORD"
MIN_SESSIONS_ENV = "SMART_BROWSER_AUTO_RECORD_MIN_SESSIONS"
MIN_STEPS_ENV = "SMART_BROWSER_AUTO_RECORD_MIN_STEPS"

RECORDED_TOOLS: dict[str, str] = {
    "browser_navigate": "navigate",
    "browser_click": "click",
    "browser_fill": "fill",
    "browser_press_key": "press",
    "browser_select_option": "select",
    "browser_check": "check",
    "browser_snapshot": "snapshot",
    "browser_screenshot": "screenshot",
}

_SUCCESS_MARKERS: dict[str, tuple[str, ...]] = {
    "navigate": ("[탐색 완료]",),
    "click": ("[클릭 완료]",),
    "fill": ("[입력 완료]",),
    "press": ("[키 입력 완료]",),
    "select": ("[옵션 선택 완료]",),
    "check": ("[체크 상태 설정 완료]",),
    "snapshot": ("[ARIA 스냅샷", "[UI 요소 추출"),
    "screenshot": ("[스크린샷 PNG",),
}

_MAX_STEPS = 40
_MAX_BUNDLES = 300
_BUNDLE_TTL_SECONDS = 1800.0
_MAX_SELECTOR_CHARS = 300
_MIN_EVIDENCE_CHARS = 20
_MIN_SCREENSHOT_B64 = 200

# 오류 화면 — 중첩 반복 없이 길이가 한정된 lookahead 만 쓴다(R-BG 3).
_ERROR_SCREEN = re.compile(
    r"access denied|forbidden|captcha|are you (?:a )?(?:human|robot)|verify you are human"
    r"|unusual traffic|login failed|incorrect password|wrong password"
    r"|invalid (?:password|credentials|username)"
    r"|로그인(?:에)? 실패|아이디 또는 비밀번호|비밀번호가 (?:일치|올바르)"
    r"|접근(?:이)? (?:거부|제한)|권한이 없|보안문자|자동입력 방지"
    r"|page not found|not found|internal server error|bad gateway|service unavailable"
    r"|gateway time-?out|페이지를 찾을 수 없|서버 오류"
    r"|\b(?:400|401|403|404|405|408|429|500|502|503|504)\b"
    r"(?=[^\n]{0,40}(?:error|not found|forbidden|denied|unauthorized|bad|unavailable|timeout|오류))",
    re.IGNORECASE,
)
_LOGIN_PATH = re.compile(r"/(?:login|signin|sign-in|auth)(?:/|$)", re.IGNORECASE)
_URL_LINE = re.compile(r"^URL:\s*(\S+)", re.MULTILINE)

_UUID_SEGMENT = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
_HEX_SEGMENT = re.compile(r"[0-9a-f]{8,}", re.IGNORECASE)
_TOKEN_CHARS = re.compile(r"[A-Za-z0-9_\-.~%]+")
_NAMED_KEY = re.compile(r"[A-Za-z][A-Za-z0-9+]{1,24}")

# recorder._CREDENTIAL_HINT 가 놓치는 민감 입력칸.
_EXTRA_SECRET_HINT = re.compile(
    r"secret|token|passcode|card|cvv|cvc|ssn|주민|카드|\bpin\b|type\s*=\s*['\"]?password",
    re.IGNORECASE,
)


def is_enabled() -> bool:
    return str(os.getenv(FLAG_ENV, "1")).strip().lower() not in {"0", "false", "off", "no", ""}


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


# ------------------------------------------------------------------ 정규화


def _segment_kind(segment: str) -> str:
    if segment.isdigit():
        return ":n"
    if _UUID_SEGMENT.fullmatch(segment):
        return ":uuid"
    if _HEX_SEGMENT.fullmatch(segment):
        return ":hex"
    return segment


def _secret_like_segment(segment: str) -> bool:
    if len(segment) < 24 or _UUID_SEGMENT.fullmatch(segment):
        return False
    return (
        bool(_TOKEN_CHARS.fullmatch(segment))
        and any(ch.isdigit() for ch in segment)
        and any(ch.isalpha() for ch in segment)
    )


def _split_url(url: Any) -> tuple[str, str, str] | None:
    """(domain, path, query 없는 URL) — query/fragment 는 버린다. 기록할 수 없는 URL 이면 None."""
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or "@" in parsed.netloc:
        return None
    path = parsed.path or "/"
    if any(_secret_like_segment(seg) for seg in path.split("/") if seg):
        return None
    return store.normalize_domain(parsed.netloc), path, f"{parsed.scheme}://{parsed.netloc}{path}"


def path_pattern(path: str) -> str:
    return "/" + "/".join(_segment_kind(seg) for seg in str(path or "/").split("/") if seg)


def _clean_selector(selector: Any) -> str:
    return " ".join(str(selector or "").split())[:_MAX_SELECTOR_CHARS]


def _is_secret_field(selector: str) -> bool:
    return bool(
        recorder_module._CREDENTIAL_HINT.search(selector) or _EXTRA_SECRET_HINT.search(selector)
    )


def step_signature_items(steps: list[dict[str, Any]]) -> list[list[Any]]:
    """URL path 패턴 + action + selector. fill 값·snapshot 은 서명에 넣지 않는다."""
    items: list[list[Any]] = []
    for step in steps:
        action = str(step.get("action") or "")
        if action in {"snapshot", "screenshot"}:
            continue
        if action == "click" and "checked" in step:
            action = "check"
        url_path = ""
        if action == "navigate":
            parts = _split_url(step.get("url"))
            url_path = path_pattern(parts[1]) if parts else ""
        key = str(step.get("value") or "") if action == "press" else ""
        items.append([action, url_path, _clean_selector(step.get("selector")), key])
    return items


def signature_of(domain: str, steps: list[dict[str, Any]]) -> str:
    canonical = json.dumps(
        [store.normalize_domain(domain), step_signature_items(steps)],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _screen_problem(text: str, *, domain: str, first_path: str) -> str | None:
    """오류 화면이면 사유를, 아니면 None. 증거 본문은 저장하지 않는다."""
    head = text.split("DATA:", 1)[0]
    if _ERROR_SCREEN.search(head):
        return "error_screen"
    match = _URL_LINE.search(head)
    if match:
        parts = _split_url(match.group(1))
        if parts is None:
            return "unrecordable_url"
        if parts[0] != domain:
            return "domain_changed"
        if _LOGIN_PATH.search(parts[1]) and not _LOGIN_PATH.search(first_path):
            return "login_redirect"
    return None


# ------------------------------------------------------------------ 상태 기계


@dataclass
class CompletedSequence:
    domain: str
    steps: list[dict[str, Any]]
    signature: str


@dataclass
class _Trace:
    domain: str
    first_path: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    fill_count: int = 0
    touched: float = 0.0


class TraceBook:
    """(chat_session_id, 묶음 키) 별 진행 중 시퀀스. 프로세스 메모리에만 둔다."""

    def __init__(self, clock: Any = time.monotonic) -> None:
        self._clock = clock
        self._traces: OrderedDict[tuple[str, str], _Trace] = OrderedDict()

    def __len__(self) -> int:
        return len(self._traces)

    def clear(self) -> None:
        self._traces.clear()

    def feed(
        self,
        tool_name: str,
        inp: dict[str, Any],
        result: Any,
        *,
        session_id: str,
    ) -> CompletedSequence | None:
        action = RECORDED_TOOLS.get(tool_name)
        session_id = str(session_id or "").strip()
        if not action or not session_id:
            return None
        bundle = str(inp.get("browser_work_key") or inp.get("browser_session_id") or "").strip()
        key = (session_id, bundle)
        now = self._clock()
        self._evict(now)

        text = result if isinstance(result, str) else ""
        succeeded = text.lstrip().startswith(_SUCCESS_MARKERS[action])
        trace = self._traces.get(key)

        if action in {"snapshot", "screenshot"}:
            if trace is None or not trace.steps or not succeeded:
                return None
            return self._finish(key, trace, action, text)

        if not succeeded:
            return None  # 실패한 단계는 기록하지 않는다. 직전 성공 단계는 유지.

        if action == "navigate":
            parts = _split_url(inp.get("url"))
            if parts is None:
                self._traces.pop(key, None)
                return None
            if _ERROR_SCREEN.search(text):
                self._traces.pop(key, None)  # 이동한 곳이 오류 화면이면 이 시퀀스는 성공이 아니다.
                return None
            domain, path, clean_url = parts
            if trace is None or trace.domain != domain:
                trace = _Trace(domain=domain, first_path=path)
                self._traces[key] = trace
            step = {"action": "navigate", "url": clean_url}
        else:
            if trace is None:
                return None
            step = self._interaction_step(action, inp, trace)
            if step is None:
                self._traces.pop(key, None)  # 기록할 수 없는 입력 — 시퀀스 폐기
                return None

        if len(trace.steps) >= _MAX_STEPS:
            self._traces.pop(key, None)
            return None
        trace.steps.append(step)
        trace.touched = now
        self._traces.move_to_end(key)
        return None

    @staticmethod
    def _interaction_step(
        action: str, inp: dict[str, Any], trace: _Trace
    ) -> dict[str, Any] | None:
        selector = _clean_selector(inp.get("selector"))
        if action == "press":
            pressed = str(inp.get("key") or "")
            if not _NAMED_KEY.fullmatch(pressed):
                return None  # 한 글자 키 입력은 비밀번호 타이핑일 수 있다.
            return {"action": "press", "selector": selector, "value": pressed}
        if not selector:
            return None
        if action == "fill":
            # 값은 읽지도 않는다 — 입력칸 종류만 남긴다.
            trace.fill_count += 1
            step: dict[str, Any] = {
                "action": "fill",
                "selector": selector,
                "variable": f"fill_{trace.fill_count}",
            }
            if _is_secret_field(selector):
                step["credential"] = True
            return step
        if action == "select":
            return {"action": "select", "selector": selector, "value": str(inp.get("value") or "")[:200]}
        if action == "check":
            return {"action": "click", "selector": selector, "checked": bool(inp.get("checked", True))}
        return {"action": "click", "selector": selector}

    def _finish(
        self, key: tuple[str, str], trace: _Trace, action: str, text: str
    ) -> CompletedSequence | None:
        self._traces.pop(key, None)  # 성공이든 실패든 이 시퀀스는 여기서 끝난다.
        if trace.steps[0]["action"] != "navigate":
            return None
        if _screen_problem(text, domain=trace.domain, first_path=trace.first_path):
            return None
        if action == "snapshot":
            if len(text.split("\n", 1)[-1].strip()) < _MIN_EVIDENCE_CHARS:
                return None
        elif len(text.split("DATA:", 1)[-1].strip()) < _MIN_SCREENSHOT_B64:
            return None
        if len(trace.steps) < _int_env(MIN_STEPS_ENV, 2):
            return None
        steps = [dict(step) for step in trace.steps]
        return CompletedSequence(
            domain=trace.domain, steps=steps, signature=signature_of(trace.domain, steps)
        )

    def _evict(self, now: float) -> None:
        for key in [k for k, t in self._traces.items() if now - t.touched > _BUNDLE_TTL_SECONDS]:
            self._traces.pop(key, None)
        while len(self._traces) >= _MAX_BUNDLES:
            self._traces.popitem(last=False)


_book = TraceBook()


def get_book() -> TraceBook:
    return _book


# ------------------------------------------------------------------ 저장 · 초안화


def _recipe_name(domain: str, signature: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", domain.lower()).strip("_") or "site"
    return f"auto_{slug}_{signature[:8]}"


def _build_recording(
    seq: CompletedSequence, tenant_id: Any, session_id: str, session_count: int
) -> recorder_module.WorkRecipeRecorder:
    recording = recorder_module.start_recording(
        _recipe_name(seq.domain, seq.signature),
        seq.domain,
        tenant_id,
        session_id=session_id,
        e2e_evidence={
            "auto_recorded": True,
            "signature": seq.signature,
            "session_count": session_count,
        },
    )
    interactive = any(step["action"] != "navigate" for step in seq.steps)
    write_risk = recording._login_risk() if interactive else "READ"
    for step in seq.steps:
        payload = {k: v for k, v in step.items() if k not in {"variable", "credential"}}
        payload["risk"] = write_risk
        if step["action"] == "fill":
            if step.get("credential"):
                payload["credential"] = True
            else:
                payload["value"] = "{{" + step["variable"] + "}}"
        recorded = recording.record_step(payload)
        if step["action"] == "fill" and not step.get("credential") and recorded is not None:
            recording.inputs.append(
                RecipeInput(
                    name=step["variable"], secret=False, description="recorded fill value (not stored)"
                )
            )
    recording.record_step(
        {"action": "snapshot", "selector": "body", "risk": "READ", "description": "screen evidence"}
    )
    return recording


def _equivalent_recipe_exists(rows: list[dict[str, Any]], signature: str, domain: str) -> bool:
    for row in rows:
        spec = row.get("spec")
        if not isinstance(spec, dict):
            continue
        steps = [dict(s) for s in spec.get("steps") or [] if isinstance(s, dict)]
        if signature_of(domain, steps) == signature:
            return True
    return False


async def record_success(
    seq: CompletedSequence, *, tenant_id: Any, session_id: str
) -> dict[str, Any]:
    """성공 시퀀스를 남기고, 서로 다른 세션 N개에 도달했으면 초안을 만든다(멱등)."""
    tenant = uuid.UUID(str(tenant_id))
    min_sessions = _int_env(MIN_SESSIONS_ENV, 2)
    async with get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO smart_browser_auto_traces
                (tenant_id, domain, signature, chat_session_id, steps)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            ON CONFLICT (tenant_id, domain, signature, chat_session_id)
            DO UPDATE SET success_count = smart_browser_auto_traces.success_count + 1,
                          last_seen_at = NOW()
            """,
            tenant, seq.domain, seq.signature, session_id,
            json.dumps(seq.steps, ensure_ascii=False),
        )
        sessions = int(
            await conn.fetchval(
                """
                SELECT COUNT(DISTINCT chat_session_id) FROM smart_browser_auto_traces
                 WHERE tenant_id=$1 AND domain=$2 AND signature=$3
                """,
                tenant, seq.domain, seq.signature,
            )
            or 0
        )
        if sessions < min_sessions:
            return {"status": "recorded", "sessions": sessions}

        name = _recipe_name(seq.domain, seq.signature)
        claimed = await conn.fetchrow(
            """
            INSERT INTO smart_browser_auto_drafts
                (tenant_id, domain, signature, recipe_name, status)
            VALUES ($1, $2, $3, $4, 'claimed')
            ON CONFLICT (tenant_id, domain, signature) DO NOTHING
            RETURNING signature
            """,
            tenant, seq.domain, seq.signature, name,
        )
    if claimed is None:
        return {"status": "already_drafted", "sessions": sessions}

    try:
        existing = await store.list_recipes(tenant_id=tenant, domain=seq.domain)
        if _equivalent_recipe_exists(existing, seq.signature, seq.domain):
            outcome, registration_id = "skipped_existing", None
        else:
            recording = _build_recording(seq, tenant, session_id, sessions)
            request = await recording.finish_recording(created_by=f"auto-record:{session_id}")
            outcome, registration_id = "drafted", request.get("id")
        async with get_pool().acquire() as conn:
            await conn.execute(
                """
                UPDATE smart_browser_auto_drafts
                   SET status=$4, registration_id=$5::uuid
                 WHERE tenant_id=$1 AND domain=$2 AND signature=$3
                """,
                tenant, seq.domain, seq.signature, outcome, registration_id,
            )
    except Exception:
        async with get_pool().acquire() as conn:  # 선점 해제 — 다음 성공 때 다시 시도한다.
            await conn.execute(
                """
                DELETE FROM smart_browser_auto_drafts
                 WHERE tenant_id=$1 AND domain=$2 AND signature=$3 AND status='claimed'
                """,
                tenant, seq.domain, seq.signature,
            )
        raise
    result = {"status": outcome, "sessions": sessions, "registration_id": registration_id}
    if registration_id:
        from app.services.work_recipe import auto_approve  # 순환 import 방지 — auto_approve 가 이 모듈을 쓴다

        if auto_approve.is_enabled():
            result["auto_approve"] = await auto_approve.maybe_auto_approve(
                registration_id, tenant_id=tenant
            )
    return result


async def observe(
    tool_name: str,
    inp: dict[str, Any],
    result: Any,
    *,
    session_id: str,
    tenant_id: Any,
) -> dict[str, Any] | None:
    """browser_* 한 번의 실행 결과를 관찰한다. 어떤 실패도 도구 결과에 영향을 주지 않는다."""
    if not is_enabled():
        return None
    try:
        seq = _book.feed(tool_name, inp, result, session_id=session_id)
        if seq is None or not tenant_id:
            return None
        return await record_success(seq, tenant_id=tenant_id, session_id=session_id)
    except Exception as exc:
        # 입력값(fill value 등)이 섞일 수 있어 예외 문구가 아니라 종류만 남긴다.
        logger.warning("smart_browser_auto_record_failed tool=%s err=%s", tool_name, type(exc).__name__)
        return None


async def list_pending_drafts(tenant_id: Any, *, limit: int = 50) -> list[dict[str, Any]]:
    """smart_browser list 에 노출할 승인 대기 초안. 스펙·단계는 내보내지 않는다."""
    rows = await registration.list_registrations(tenant_id=tenant_id, status="pending")
    drafts: list[dict[str, Any]] = []
    for row in rows[:limit]:
        dry_run = row.get("dry_run") or {}
        spec = row.get("spec") or {}
        metadata = spec.get("metadata") or {}
        evidence = metadata.get("screen_e2e") or {}
        drafts.append(
            {
                "registration_id": str(row.get("id") or ""),
                "name": row.get("name"),
                "domain": row.get("domain"),
                "proposed_version": dry_run.get("proposed_version"),
                "max_risk": dry_run.get("max_risk"),
                "step_count": len(dry_run.get("steps") or []),
                "auto_recorded": bool(evidence.get("auto_recorded")),
                "status": row.get("status"),
                "requested_at": str(row.get("requested_at") or ""),
            }
        )
    return drafts
