"""조회 전용(READ) 자동 기록 초안의 자동 승인 정책.

auto_record 가 서로 다른 채팅 세션 2개 이상의 성공으로 만든 pending 초안 중, 아래를
**모두** 만족하는 것만 시스템 주체로 승인한다. 하나라도 어긋나면 pending 그대로
두어 기존 CEO 승인 경로를 탄다(쓰기·제출·결제·로그인은 절대 자동 승인하지 않는다).

  1. 레시피가 조회 동작(navigate/snapshot)뿐이고 모든 단계 등급이 READ
  2. 비밀값 입력·로그인 단계·자격증명 참조 없음
  3. 같은 서명이 서로 다른 채팅 세션 2개 이상에서 화면 검증 성공(traces 장부)
  4. 도메인·경로가 결제·금융·관리자 콘솔 차단 목록에 없음

기능 플래그 SMART_BROWSER_AUTO_APPROVE_READ 는 기본 꺼짐이다. 승인 근거(세션 id·증거
행 ref)는 registration.decision_reason 에 JSON 으로 남는다.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlparse

from app.core.db_pool import get_pool
from app.services.work_recipe import auto_record, registration, store
from app.services.work_recipe import recorder as recorder_module
from app.services.work_recipe.guard import RiskLevel, classify_step
from app.services.work_recipe.schema import parse_recipe

logger = logging.getLogger(__name__)

FLAG_ENV = "SMART_BROWSER_AUTO_APPROVE_READ"
MIN_SESSIONS_ENV = "SMART_BROWSER_AUTO_APPROVE_MIN_SESSIONS"

AUTO_DECIDER = "system:auto_read_policy"
POLICY_VERSION = "auto_read_v1"
MIN_DISTINCT_SESSIONS = 2  # 환경변수로도 이 아래로는 못 내린다.

# 자동 승인이 허용되는 단계 동작. click/fill/select/press 는 상태를 바꿀 수 있어 제외한다.
READ_ONLY_ACTIONS: frozenset[str] = frozenset({"navigate", "snapshot"})
_SECRET_STEP_KEYS: tuple[str, ...] = ("credential", "credential_ref", "secret")

# 결제·금융·관리자 콘솔 — READ 여도 자동 승인에서 뺀다. 호스트 라벨은 부분 문자열,
# 경로 토큰은 정확 일치로 본다(경로의 "pay" 가 "payload" 에 걸리지 않도록).
BLOCKED_HOST_FRAGMENTS: tuple[str, ...] = (
    "pay", "bank", "billing", "checkout", "finance", "wallet", "admin", "backoffice",
    "console", "toss", "payco", "paypal", "stripe", "kcp", "inicis",
)
BLOCKED_PATH_TOKENS: frozenset[str] = frozenset(
    {
        "pay", "payment", "payments", "checkout", "billing", "invoice", "bank", "banking",
        "wallet", "card", "cards", "transfer", "finance", "admin", "administrator",
        "console", "backoffice", "manage", "manager", "cms", "login", "signin", "signup",
        "auth", "account", "accounts", "settings", "password",
    }
)
BLOCKED_PATH_SUBSTRINGS: tuple[str, ...] = (
    "결제", "송금", "이체", "계좌", "카드", "관리자", "로그인", "비밀번호", "정산",
)
_PATH_TOKEN_SPLIT = re.compile(r"[^0-9A-Za-z가-힣]+")


def is_enabled() -> bool:
    return str(os.getenv(FLAG_ENV, "0")).strip().lower() in {"1", "true", "on", "yes"}


def _min_sessions() -> int:
    try:
        return max(MIN_DISTINCT_SESSIONS, int(os.getenv(MIN_SESSIONS_ENV, str(MIN_DISTINCT_SESSIONS))))
    except ValueError:
        return MIN_DISTINCT_SESSIONS


# ------------------------------------------------------------------ 판정 (순수 함수)


@dataclass
class Verdict:
    eligible: bool
    reasons: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


def blocked_domain_reason(domain: str) -> str | None:
    host = store.normalize_domain(domain).split(":", 1)[0]
    if not host:
        return "domain_missing"
    if host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        return "domain_private_host"
    try:
        ipaddress.ip_address(host)
        return "domain_ip_literal"
    except ValueError:
        pass
    if any(fragment in host for fragment in BLOCKED_HOST_FRAGMENTS):
        return "domain_blocked"
    return None


def blocked_path_reason(path: str) -> str | None:
    decoded = unquote(str(path or "")).lower()
    tokens = {token for token in _PATH_TOKEN_SPLIT.split(decoded) if token}
    if tokens & BLOCKED_PATH_TOKENS or any(sub in decoded for sub in BLOCKED_PATH_SUBSTRINGS):
        return "path_blocked"
    return None


def _spec_reasons(spec: Any, domain: str) -> list[str]:
    reasons: list[str] = []
    try:
        recipe = parse_recipe(spec)
    except Exception:
        return ["spec_unparseable"]

    if recipe.max_risk() != RiskLevel.READ.value:
        reasons.append("risk_not_read")
    if any(item.secret for item in recipe.inputs):
        reasons.append("secret_input")

    for raw in [*(spec.get("steps") or []), *(spec.get("verify") or [])]:
        if not isinstance(raw, dict) or any(raw.get(key) for key in _SECRET_STEP_KEYS):
            reasons.append("secret_or_malformed_step")
        elif "{{" in json.dumps(raw, ensure_ascii=False):
            reasons.append("variable_step")
    for step in [*recipe.steps, *recipe.verify]:
        if step.action not in READ_ONLY_ACTIONS:
            reasons.append(f"action_not_read_only:{step.action}")
            continue
        if classify_step(step, domain=domain) is not RiskLevel.READ:
            reasons.append("step_not_read")
        if step.action == "navigate":
            parts = urlparse(str(step.url or ""))
            if parts.scheme not in {"http", "https"}:
                reasons.append("navigate_url_invalid")
                continue
            if store.normalize_domain(parts.netloc) != store.normalize_domain(domain):
                reasons.append("navigate_cross_domain")
            if auto_record._LOGIN_PATH.search(parts.path) or recorder_module._CREDENTIAL_HINT.search(
                parts.path
            ):
                reasons.append("login_step")
            reason = blocked_path_reason(parts.path)
            if reason:
                reasons.append(reason)
    if not any(step.action == "snapshot" for step in recipe.steps):
        reasons.append("no_screen_evidence_step")
    return list(dict.fromkeys(reasons))


def evaluate(row: dict[str, Any], traces: list[dict[str, Any]]) -> Verdict:
    """등록 요청 행 + 성공 장부 행으로 자동 승인 가능 여부를 판정한다. DB 를 건드리지 않는다."""
    domain = store.normalize_domain(str(row.get("domain") or ""))
    spec = row.get("spec")
    reasons: list[str] = []

    if not isinstance(spec, dict):
        return Verdict(False, ["spec_missing"])
    if str(row.get("status") or "") != "pending":
        reasons.append("not_pending")
    dry_max = (row.get("dry_run") or {}).get("max_risk")
    if dry_max != RiskLevel.READ.value:
        reasons.append("dry_run_not_read")
    domain_reason = blocked_domain_reason(domain)
    if domain_reason:
        reasons.append(domain_reason)
    reasons += _spec_reasons(spec, domain)

    signature = str((spec.get("metadata") or {}).get("screen_e2e", {}).get("signature") or "")
    if not signature:
        reasons.append("signature_missing")

    # 장부에 남은 관찰 단계도 조회뿐이어야 한다 — 초안이 아니라 실제로 본 것을 다시 확인한다.
    sessions: dict[str, dict[str, Any]] = {}
    for trace in traces:
        session = str(trace.get("chat_session_id") or "").strip()
        steps = trace.get("steps")
        if isinstance(steps, str):
            try:
                steps = json.loads(steps)
            except ValueError:
                steps = None
        if not session or not isinstance(steps, list):
            continue
        if any(not isinstance(s, dict) or s.get("action") != "navigate" for s in steps):
            reasons.append("trace_has_interaction")
            continue
        if auto_record.signature_of(domain, steps) != signature:
            continue
        sessions.setdefault(session, trace)
    if len(sessions) < _min_sessions():
        reasons.append("sessions_below_minimum")

    reasons = list(dict.fromkeys(reasons))
    ordered = sorted(sessions)
    evidence = {
        "policy": POLICY_VERSION,
        "signature": signature,
        "domain": domain,
        "max_risk": dry_max,
        "session_ids": ordered,
        "evidence_refs": [
            f"smart_browser_auto_traces:{sessions[s].get('id')}" for s in ordered
        ],
    }
    return Verdict(not reasons, reasons, evidence)


# ------------------------------------------------------------------ 실행


async def _load_traces(tenant: uuid.UUID, domain: str, signature: str) -> list[dict[str, Any]]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, chat_session_id, steps FROM smart_browser_auto_traces
             WHERE tenant_id=$1 AND domain=$2 AND signature=$3
             ORDER BY first_seen_at, id
            """,
            tenant, domain, signature,
        )
    return [dict(r) for r in rows]


async def _notify(name: str, domain: str) -> None:
    try:
        from app.services.telegram_bot import get_telegram_bot

        bot = get_telegram_bot()
        if bot and bot.is_ready:
            await bot.send_message(f"자동 승인: {name}/{domain}")
    except Exception as exc:
        logger.warning("smart_browser_auto_approve_notify_failed err=%s", type(exc).__name__)


async def maybe_auto_approve(registration_id: Any, *, tenant_id: Any) -> dict[str, Any]:
    """pending 초안 하나를 정책으로 평가해 통과하면 승인한다. 어떤 실패도 pending 을 유지한다."""
    if not is_enabled():
        return {"status": "disabled"}
    try:
        tenant = uuid.UUID(str(tenant_id))
        row = await registration.get_registration(registration_id, tenant_id=tenant)
        if row is None:
            return {"status": "not_found"}
        signature = str(((row.get("spec") or {}).get("metadata") or {}).get("screen_e2e", {}).get("signature") or "")
        traces = await _load_traces(tenant, str(row.get("domain") or ""), signature) if signature else []
        verdict = evaluate(row, traces)
        if not verdict.eligible:
            logger.info(
                "smart_browser_auto_approve_skipped reg=%s reasons=%s", registration_id, verdict.reasons
            )
            return {"status": "pending", "reasons": verdict.reasons}
        decided = await registration.decide_registration(
            registration_id,
            tenant_id=tenant,
            decision="approve",
            decided_by=AUTO_DECIDER,
            reason=json.dumps(verdict.evidence, ensure_ascii=False, sort_keys=True),
        )
        logger.info("smart_browser_auto_approved reg=%s name=%s", registration_id, row.get("name"))
        await _notify(str(row.get("name") or ""), str(row.get("domain") or ""))
        return {"status": "auto_approved", "recipe_id": (decided.get("recipe") or {}).get("id")}
    except registration.RegistrationError as exc:
        return {"status": "pending", "reasons": [str(exc)]}  # 사람이 먼저 결정했거나 이미 처리됨
    except Exception as exc:
        logger.warning("smart_browser_auto_approve_failed err=%s", type(exc).__name__)
        return {"status": "pending", "reasons": ["policy_error"]}
