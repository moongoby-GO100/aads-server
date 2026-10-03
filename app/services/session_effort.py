"""세션별 능동 추론 강도(effort) — 설정 저장·자동 정책·실행별 기록·공급사 전달 판정.

원칙
- 강도는 **새 요청 시작**에만 정한다. 도구 호출·재시도마다 바꾸지 않는다(프롬프트 캐시 보호).
- 같은 실행(execution)이 다시 call_stream 을 부르면(추가지시 재호출·재개) 기록된 결정을 재사용한다.
- "적용됨"의 근거는 공급사로 나간 **실제 요청 값**뿐이다. 모델이 스스로 고른 값, CLI 설정 파일 변경은 근거가 아니다.
- 작업 중 사용자 변경은 pending_next_request 로 접수하고 다음 요청 시작에 반영한다. 진행 중 요청에 소급하지 않는다.
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, Tuple

logger = logging.getLogger(__name__)

POLICY_VERSION = "effort-policy-2026-10-03.1"

MODE_AUTO = "auto"
MODE_MANUAL = "manual"
MODES = (MODE_AUTO, MODE_MANUAL)

# 사용자·자동 정책이 고르는 단계. 공급사가 받는 전체 사다리는 LEVEL_LADDER.
POLICY_LEVELS = ("medium", "high", "xhigh")
LEVEL_LADDER = ("low", "medium", "high", "xhigh", "max")

APPLY_DIRECT_API = "direct_api"
APPLY_CLI_NEXT_RUN = "cli_next_run"
APPLY_UNSUPPORTED = "unsupported"

SETTING_APPLIED = "applied"
SETTING_PENDING = "pending_next_request"

# 같은 실행 오류가 이 횟수 이상 연속되면 xhigh 로 올린다.
REPEAT_ERROR_THRESHOLD = 3

PATH_ANTHROPIC = "anthropic"
PATH_OPENAI_CHAT = "openai_chat"
PATH_CLAUDE_CLI = "claude_cli"
PATH_CODEX_CLI = "codex_cli"

_FULL_LADDER_MODELS = (
    "fable-5", "mythos-5", "opus-5", "sonnet-5", "opus-4-8", "opus-4-7",
)
_NO_XHIGH_MODELS = ("opus-4-6", "sonnet-4-6")
_LOW_TO_HIGH_MODELS = ("opus-4-5",)
_OPENAI_EFFORT_MODELS = ("gpt-6-sol",)


# ── 공급사별 지원 범위 (M1 조사 결과를 코드로 고정) ───────────────────────────────

def supported_levels(path: str, model: Any) -> Tuple[str, ...]:
    """경로·모델별로 공급사가 받는 effort 값. 비어 있으면 파라미터 자체가 없다."""
    m = str(model or "").strip().lower()
    if path in (PATH_ANTHROPIC, PATH_CLAUDE_CLI):
        if any(k in m for k in _FULL_LADDER_MODELS):
            return LEVEL_LADDER
        if any(k in m for k in _NO_XHIGH_MODELS):
            return ("low", "medium", "high", "max")
        if any(k in m for k in _LOW_TO_HIGH_MODELS):
            return ("low", "medium", "high")
        return ()
    if path == PATH_OPENAI_CHAT:
        return LEVEL_LADDER if m in _OPENAI_EFFORT_MODELS else ()
    if path == PATH_CODEX_CLI:
        # codex-cli 0.159.3 은 -c model_reasoning_effort=<값> 을 그대로 받는다(값 검증 없음).
        # 이쪽에서 검증해 low~xhigh 만 넘긴다.
        return ("low", "medium", "high", "xhigh") if m else ()
    return ()


def clamp_to_supported(level: str, supported: Tuple[str, ...]) -> Tuple[Optional[str], str]:
    """지원 최근접 값. 동률이면 낮은 쪽. (값, 설명) — 값이 None 이면 지원 없음."""
    if not supported:
        return None, "공급사가 effort 파라미터를 받지 않음"
    if level in supported:
        return level, ""
    want = LEVEL_LADDER.index(level)
    ranked = sorted(supported, key=lambda s: (abs(LEVEL_LADDER.index(s) - want), LEVEL_LADDER.index(s)))
    chosen = ranked[0]
    return chosen, f"{level} 미지원 → 지원 최근접 {chosen}"


# ── 자동 정책 ────────────────────────────────────────────────────────────────

_XHIGH_INTENTS = frozenset({
    "design", "design_fix", "architect", "strategy", "planning", "decision", "discussion",
    "complex_analysis", "cto_strategy", "cto_strategy_secondary", "cto_impact",
    "cto_verify", "execution_verify", "qa",
})
_HIGH_INTENTS = frozenset({
    "diagnosis", "code_modify", "code_task", "code_exec", "code_explorer", "execute",
    "directive", "directive_gen", "pipeline_runner", "analyze_changes",
    "cto_code_analysis", "cto_code_analysis_page", "cto_directive", "cto_tech_debt",
    "service_inspection", "deep_research", "deep_research_secondary", "pc_control",
})

_DESIGN_RE = re.compile(r"(설계|아키텍처|architecture|마이그레이션 계획|전체 구조|핵심 검증|보안 검토|설계안)", re.I)
_ANALYZE_RE = re.compile(r"(원인|분석|왜 |root cause|구현|수정해|고쳐|리팩터|버그|디버그|fix|implement|debug)", re.I)
_PROGRESS_RE = re.compile(r"(진행 ?상황|진행률|현황|상태 ?(알려|확인|보고)|status|어디까지|보고해)", re.I)
_CONTINUE_RE = re.compile(r"^\s*(계속|이어서|계속해|계속 진행|go on|continue|proceed|ok|네|응)[\s.!?]*$", re.I)

_RANK = {lvl: i for i, lvl in enumerate(POLICY_LEVELS)}


def _max_level(a: str, b: str) -> str:
    return a if _RANK[a] >= _RANK[b] else b


def decide_auto_effort(
    *,
    intent: str,
    content: str,
    previous_level: Optional[str] = None,
    repeated_errors: int = 0,
) -> Tuple[str, str]:
    """(level, 짧은 공개 사유). 새 요청 시작에서만 호출한다."""
    text = str(content or "")
    intent = str(intent or "").strip().lower()

    if repeated_errors >= REPEAT_ERROR_THRESHOLD:
        return "xhigh", f"같은 실행 오류 {repeated_errors}회 반복"

    if _CONTINUE_RE.match(text) and previous_level in _RANK:
        return previous_level, "이어서 진행 요청 — 직전 강도 유지"

    if intent in _XHIGH_INTENTS:
        level, why = "xhigh", "복잡 설계·핵심 검증 유형"
    elif intent in _HIGH_INTENTS:
        level, why = "high", "원인 분석·구현 유형"
    else:
        level, why = "medium", "단순 조회·진행 보고 유형"

    if _DESIGN_RE.search(text) and _RANK[level] < _RANK["xhigh"]:
        return "xhigh", "요청 문구가 설계·핵심 검증을 가리킴"
    if _ANALYZE_RE.search(text) and _RANK[level] < _RANK["high"]:
        level, why = "high", "요청 문구가 원인 분석·구현을 가리킴"
    if _PROGRESS_RE.search(text) and len(text) <= 120 and not _DESIGN_RE.search(text):
        return "medium", "진행 상황 보고 요청"
    return level, why


# ── 실행별 결정·기록 ─────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class EffortDecision:
    mode: str
    requested_effort: str
    reason: str
    policy_version: str = POLICY_VERSION
    execution_id: Optional[str] = None
    session_id: Optional[str] = None
    decided_at: str = field(default_factory=_now_iso)
    effective_effort: Optional[str] = None
    applied_at: Optional[str] = None
    apply_path: Optional[str] = None
    provider_note: str = ""
    attempts: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def state(self) -> str:
        if self.apply_path is None:
            return "decided"
        return "applied" if self.apply_path != APPLY_UNSUPPORTED else "unsupported"

    def as_record(self) -> Dict[str, Any]:
        reason = self.reason
        if self.provider_note:
            reason = f"{reason} ({self.provider_note})"
        return {
            "requested_effort": self.requested_effort,
            "effective_effort": self.effective_effort,
            "mode": self.mode,
            "policy_version": self.policy_version,
            "reason": reason,
            "decided_at": self.decided_at,
            "applied_at": self.applied_at,
            "apply_path": self.apply_path,
            "state": self.state,
            "attempts": list(self.attempts),
        }

    def event(self) -> Dict[str, Any]:
        rec = self.as_record()
        rec.pop("attempts", None)
        return {"type": "effort_status", **rec}

    @classmethod
    def from_record(cls, rec: Dict[str, Any], *, execution_id: Optional[str], session_id: Optional[str]) -> "EffortDecision":
        d = cls(
            mode=str(rec.get("mode") or MODE_AUTO),
            requested_effort=str(rec.get("requested_effort") or "medium"),
            reason=str(rec.get("reason") or ""),
            policy_version=str(rec.get("policy_version") or POLICY_VERSION),
            execution_id=execution_id,
            session_id=session_id,
        )
        d.decided_at = str(rec.get("decided_at") or d.decided_at)
        d.effective_effort = rec.get("effective_effort")
        d.applied_at = rec.get("applied_at")
        d.apply_path = rec.get("apply_path")
        d.attempts = list(rec.get("attempts") or [])
        return d

    def note_attempt(
        self,
        *,
        path: str,
        model: Any,
        apply_path: str,
        sent: Optional[str],
        note: str = "",
    ) -> None:
        """공급사 요청마다 호출. 마지막 시도가 실행의 최종 적용 상태가 된다(fallback 재검증)."""
        now = _now_iso()
        self.attempts.append({
            "path": path, "model": str(model or ""), "apply_path": apply_path,
            "sent": sent, "at": now, "note": note,
        })
        self.attempts = self.attempts[-8:]
        self.effective_effort = sent
        self.apply_path = apply_path
        self.applied_at = now
        self.provider_note = note


_current_decision: contextvars.ContextVar[Optional[EffortDecision]] = contextvars.ContextVar(
    "_current_effort_decision", default=None,
)


def current_decision(session_id: Optional[str] = None) -> Optional[EffortDecision]:
    """이 턴의 결정. 다른 세션 호출이 컨텍스트를 물려받았다면(서브에이전트 등) 쓰지 않는다."""
    decision = _current_decision.get()
    if decision is None or session_id is None or decision.session_id is None:
        return decision
    return decision if str(decision.session_id) == str(session_id) else None


def set_current_decision(decision: Optional[EffortDecision]) -> contextvars.Token:
    return _current_decision.set(decision)


def reset_current_decision(token: contextvars.Token) -> None:
    try:
        _current_decision.reset(token)
    except ValueError:
        _current_decision.set(None)


# ── 공급사 전달 판정 ─────────────────────────────────────────────────────────

@dataclass
class Resolution:
    value: Optional[str]      # 요청 body 에 실을 값. None 이면 싣지 않는다.
    apply_path: str
    note: str


def resolve_for_provider(
    decision: Optional[EffortDecision],
    *,
    path: str,
    model: Any,
    has_tools: bool = False,
) -> Resolution:
    """결정된 강도를 이 공급사 호출에서 실제로 보낼 값으로 바꾼다. 모델이 바뀌면 다시 부른다."""
    if decision is None:
        return Resolution(None, APPLY_UNSUPPORTED, "세션 강도 결정 없음")
    supported = supported_levels(path, model)
    if path == PATH_OPENAI_CHAT and has_tools and supported:
        # Chat Completions 는 함수 도구와 reasoning_effort(none 제외)를 함께 받지 않는다.
        return Resolution("none", APPLY_DIRECT_API, f"{decision.requested_effort} 요청 — 도구 동반이라 none 강제")
    value, note = clamp_to_supported(decision.requested_effort, supported)
    if value is None:
        return Resolution(None, APPLY_UNSUPPORTED, note)
    apply_path = APPLY_DIRECT_API if path in (PATH_ANTHROPIC, PATH_OPENAI_CHAT) else APPLY_CLI_NEXT_RUN
    return Resolution(value, apply_path, note)


# ── 저장소 ───────────────────────────────────────────────────────────────────

class EffortStore(Protocol):
    async def get_session_setting(self, session_id: str) -> Optional[Dict[str, Any]]: ...
    async def promote_pending(self, session_id: str) -> Optional[Dict[str, Any]]: ...
    async def has_running_execution(self, session_id: str) -> bool: ...
    async def set_session_setting(
        self, session_id: str, tenant_id: str, mode: str, level: Optional[str], *, pending: bool,
    ) -> Optional[Dict[str, Any]]: ...
    async def get_execution_record(self, execution_id: str) -> Optional[Dict[str, Any]]: ...
    async def save_execution_record(self, execution_id: str, record: Dict[str, Any]) -> None: ...
    async def recent_executions(
        self, session_id: str, exclude_execution_id: Optional[str], limit: int,
    ) -> List[Dict[str, Any]]: ...
    async def latest_execution_record(self, session_id: str) -> Optional[Dict[str, Any]]: ...


def _json_load(value: Any) -> Any:
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
    return value


class PgEffortStore:
    """asyncpg 구현. 모든 세션 쓰기는 id + tenant_id 로 좁힌다."""

    @staticmethod
    def _pool():
        from app.core.db_pool import get_pool
        return get_pool()

    @staticmethod
    def _setting(row: Any) -> Dict[str, Any]:
        return {
            "effort_mode": row["effort_mode"] or MODE_AUTO,
            "effort_manual": row["effort_manual"],
            "effort_pending": _json_load(row["effort_pending"]),
        }

    async def get_session_setting(self, session_id: str) -> Optional[Dict[str, Any]]:
        async with self._pool().acquire() as conn:
            row = await conn.fetchrow(
                "SELECT effort_mode, effort_manual, effort_pending FROM chat_sessions WHERE id = $1",
                uuid.UUID(session_id),
            )
        return self._setting(row) if row else None

    async def promote_pending(self, session_id: str) -> Optional[Dict[str, Any]]:
        """대기 중 변경을 단일 UPDATE 로 현재 설정에 올린다. 대기가 없으면 그냥 현재 설정을 읽는다."""
        async with self._pool().acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE chat_sessions
                   SET effort_mode = effort_pending->>'mode',
                       effort_manual = NULLIF(effort_pending->>'level', ''),
                       effort_pending = NULL,
                       effort_updated_at = NOW()
                 WHERE id = $1 AND effort_pending IS NOT NULL
             RETURNING effort_mode, effort_manual, effort_pending
                """,
                uuid.UUID(session_id),
            )
            if row is None:
                row = await conn.fetchrow(
                    "SELECT effort_mode, effort_manual, effort_pending FROM chat_sessions WHERE id = $1",
                    uuid.UUID(session_id),
                )
        return self._setting(row) if row else None

    async def has_running_execution(self, session_id: str) -> bool:
        async with self._pool().acquire() as conn:
            return bool(await conn.fetchval(
                "SELECT 1 FROM chat_turn_executions "
                "WHERE session_id = $1 AND status IN ('running', 'retrying') AND completed_at IS NULL LIMIT 1",
                uuid.UUID(session_id),
            ))

    async def set_session_setting(
        self, session_id: str, tenant_id: str, mode: str, level: Optional[str], *, pending: bool,
    ) -> Optional[Dict[str, Any]]:
        async with self._pool().acquire() as conn:
            if pending:
                payload = json.dumps({"mode": mode, "level": level or "", "requested_at": _now_iso()})
                row = await conn.fetchrow(
                    "UPDATE chat_sessions SET effort_pending = $3::jsonb, effort_updated_at = NOW() "
                    "WHERE id = $1 AND tenant_id = $2 "
                    "RETURNING effort_mode, effort_manual, effort_pending",
                    uuid.UUID(session_id), uuid.UUID(tenant_id), payload,
                )
            else:
                row = await conn.fetchrow(
                    "UPDATE chat_sessions SET effort_mode = $3, effort_manual = $4, "
                    "effort_pending = NULL, effort_updated_at = NOW() "
                    "WHERE id = $1 AND tenant_id = $2 "
                    "RETURNING effort_mode, effort_manual, effort_pending",
                    uuid.UUID(session_id), uuid.UUID(tenant_id), mode, level,
                )
        return self._setting(row) if row else None

    async def get_execution_record(self, execution_id: str) -> Optional[Dict[str, Any]]:
        async with self._pool().acquire() as conn:
            value = await conn.fetchval(
                "SELECT effort_status FROM chat_turn_executions WHERE id = $1", uuid.UUID(execution_id),
            )
        rec = _json_load(value)
        return rec if isinstance(rec, dict) and rec else None

    async def save_execution_record(self, execution_id: str, record: Dict[str, Any]) -> None:
        async with self._pool().acquire() as conn:
            await conn.execute(
                "UPDATE chat_turn_executions SET effort_status = $2::jsonb, updated_at = NOW() WHERE id = $1",
                uuid.UUID(execution_id), json.dumps(record, ensure_ascii=False),
            )

    async def recent_executions(
        self, session_id: str, exclude_execution_id: Optional[str], limit: int,
    ) -> List[Dict[str, Any]]:
        async with self._pool().acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id::text AS id, status, error_message, effort_status
                  FROM chat_turn_executions
                 WHERE session_id = $1 AND ($2::uuid IS NULL OR id <> $2::uuid)
                 ORDER BY created_at DESC
                 LIMIT $3
                """,
                uuid.UUID(session_id),
                uuid.UUID(exclude_execution_id) if exclude_execution_id else None,
                limit,
            )
        out = []
        for r in rows:
            rec = _json_load(r["effort_status"])
            out.append({
                "id": r["id"], "status": r["status"], "error_message": r["error_message"],
                "effort": rec if isinstance(rec, dict) else None,
            })
        return out

    async def latest_execution_record(self, session_id: str) -> Optional[Dict[str, Any]]:
        async with self._pool().acquire() as conn:
            value = await conn.fetchval(
                "SELECT effort_status FROM chat_turn_executions "
                "WHERE session_id = $1 AND effort_status IS NOT NULL ORDER BY created_at DESC LIMIT 1",
                uuid.UUID(session_id),
            )
        rec = _json_load(value)
        return rec if isinstance(rec, dict) and rec else None


_default_store: Optional[EffortStore] = None


def get_store() -> EffortStore:
    global _default_store
    if _default_store is None:
        _default_store = PgEffortStore()
    return _default_store


# ── 오류 반복 판정 ───────────────────────────────────────────────────────────

_ERROR_FAIL_STATUSES = frozenset({"failed", "error", "interrupted"})


def _error_signature(message: Any) -> str:
    text = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27,}|\d+", "#", str(message or "").strip().lower())
    return text[:120]


def count_repeated_errors(recent: List[Dict[str, Any]]) -> int:
    """최신 실행부터 거슬러 올라가며 같은 오류 서명이 연속된 횟수. 성공 실행을 만나면 끊는다."""
    count = 0
    signature = ""
    for item in recent:
        sig = _error_signature(item.get("error_message"))
        if str(item.get("status") or "") not in _ERROR_FAIL_STATUSES or not sig:
            break
        if not signature:
            signature = sig
        elif sig != signature:
            break
        count += 1
    return count


# ── 요청 시작 결정 ───────────────────────────────────────────────────────────

async def begin_request(
    *,
    session_id: Optional[str],
    execution_id: Optional[str],
    intent: str,
    content: str,
    store: Optional[EffortStore] = None,
) -> Optional[EffortDecision]:
    """새 요청 시작에서 강도를 확정하고 실행 행에 남긴다.

    같은 execution_id 에 이미 기록이 있으면 재결정하지 않는다(추가지시 재호출·재개). 이때 대기 중
    변경도 승격하지 않는다 — 다음 새 요청이 받는다. 저장소 장애는 채팅을 막지 않는다(None).
    """
    if not session_id:
        return None
    store = store or get_store()
    try:
        if execution_id:
            existing = await store.get_execution_record(execution_id)
            if existing:
                return EffortDecision.from_record(existing, execution_id=execution_id, session_id=session_id)

        setting = await store.promote_pending(session_id)
        if setting is None:
            return None
        mode = setting.get("effort_mode") or MODE_AUTO
        manual = setting.get("effort_manual")
        if mode == MODE_MANUAL and manual in POLICY_LEVELS:
            decision = EffortDecision(
                mode=MODE_MANUAL, requested_effort=manual,
                reason="사용자 수동 고정", execution_id=execution_id, session_id=session_id,
            )
        else:
            recent = await store.recent_executions(session_id, execution_id, 6)
            previous = None
            for item in recent:
                prev_rec = item.get("effort") or {}
                if prev_rec.get("requested_effort") in POLICY_LEVELS:
                    previous = prev_rec["requested_effort"]
                    break
            level, why = decide_auto_effort(
                intent=intent, content=content,
                previous_level=previous, repeated_errors=count_repeated_errors(recent),
            )
            decision = EffortDecision(
                mode=MODE_AUTO, requested_effort=level, reason=why,
                execution_id=execution_id, session_id=session_id,
            )
        if execution_id:
            await store.save_execution_record(execution_id, decision.as_record())
        return decision
    except Exception as exc:  # noqa: BLE001 — 강도 기록 실패가 응답을 막으면 안 된다
        logger.warning("effort_begin_request_failed session=%s err=%s", str(session_id)[:8], str(exc)[:160])
        return None


async def persist_decision(decision: Optional[EffortDecision], store: Optional[EffortStore] = None) -> None:
    if decision is None or not decision.execution_id:
        return
    try:
        await (store or get_store()).save_execution_record(decision.execution_id, decision.as_record())
    except Exception as exc:  # noqa: BLE001
        logger.warning("effort_persist_failed execution=%s err=%s", str(decision.execution_id)[:8], str(exc)[:160])


async def record_applied(
    decision: Optional[EffortDecision],
    *,
    path: str,
    model: Any,
    resolution: Resolution,
    sent: Optional[str],
    store: Optional[EffortStore] = None,
) -> Optional[Dict[str, Any]]:
    """공급사 요청 body 에 실제로 실은 값(sent)을 근거로 기록한다. 새 이벤트 dict 를 돌려준다(변화 없으면 None).

    sent 가 None 이면 실제로 보낸 값이 없는 것이므로 unsupported 로만 남긴다.
    """
    if decision is None:
        return None
    apply_path = resolution.apply_path if sent is not None else APPLY_UNSUPPORTED
    last = decision.attempts[-1] if decision.attempts else None
    if last and (last["path"], last["model"], last["apply_path"], last["sent"]) == (
        path, str(model or ""), apply_path, sent,
    ):
        return None  # 도구 루프 등 같은 요청 반복 — 같은 값은 다시 기록·송출하지 않는다
    decision.note_attempt(path=path, model=model, apply_path=apply_path, sent=sent, note=resolution.note)
    await persist_decision(decision, store)
    return decision.event()


async def finalize_unreported(decision: Optional[EffortDecision], model: Any, store: Optional[EffortStore] = None) -> Optional[Dict[str, Any]]:
    """스트림이 끝났는데 어떤 공급사 경로도 적용 기록을 남기지 않았으면 unsupported 로 정직하게 닫는다."""
    if decision is None or decision.apply_path is not None:
        return None
    decision.note_attempt(
        path="unknown", model=model, apply_path=APPLY_UNSUPPORTED, sent=None,
        note="이 경로는 공급사 effort 파라미터를 전달하지 않음",
    )
    await persist_decision(decision, store)
    return decision.event()


# ── 세션 설정 API ────────────────────────────────────────────────────────────

class EffortSettingError(ValueError):
    pass


def validate_setting(mode: Any, level: Any) -> Tuple[str, Optional[str]]:
    mode = str(mode or "").strip().lower()
    if mode not in MODES:
        raise EffortSettingError("mode must be 'auto' or 'manual'")
    if mode == MODE_AUTO:
        if level not in (None, ""):
            raise EffortSettingError("level must be omitted when mode is 'auto'")
        return MODE_AUTO, None
    level = str(level or "").strip().lower()
    if level not in POLICY_LEVELS:
        raise EffortSettingError("level must be one of medium, high, xhigh when mode is 'manual'")
    return MODE_MANUAL, level


def _view(setting: Dict[str, Any]) -> Dict[str, Any]:
    pending = setting.get("effort_pending")
    return {
        "mode": setting.get("effort_mode") or MODE_AUTO,
        "level": setting.get("effort_manual"),
        "pending": (
            {"mode": pending.get("mode"), "level": pending.get("level") or None,
             "requested_at": pending.get("requested_at")}
            if isinstance(pending, dict) else None
        ),
        "policy_version": POLICY_VERSION,
    }


async def update_session_setting(
    *,
    session_id: str,
    tenant_id: str,
    mode: Any,
    level: Any,
    store: Optional[EffortStore] = None,
) -> Optional[Dict[str, Any]]:
    """PUT /chat/sessions/{id}/effort 본체. 세션이 테넌트 밖이면 None.

    진행 중 실행이 있으면 pending_next_request 로 접수만 하고 현재 설정·진행 중 요청은 건드리지 않는다.
    """
    mode, level = validate_setting(mode, level)
    store = store or get_store()
    pending = await store.has_running_execution(session_id)
    row = await store.set_session_setting(session_id, tenant_id, mode, level, pending=pending)
    if row is None:
        return None
    out = _view(row)
    out["status"] = SETTING_PENDING if pending else SETTING_APPLIED
    return out


async def session_view(
    session_id: str, store: Optional[EffortStore] = None,
) -> Optional[Dict[str, Any]]:
    """새로고침 복원용: 세션 설정 + 가장 최근 실행의 강도 기록."""
    store = store or get_store()
    setting = await store.get_session_setting(session_id)
    if setting is None:
        return None
    out = _view(setting)
    latest = await store.latest_execution_record(session_id)
    if latest:
        latest = dict(latest)
        latest.pop("attempts", None)
    out["latest_execution"] = latest
    return out
