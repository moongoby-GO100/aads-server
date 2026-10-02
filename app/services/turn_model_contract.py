"""채팅 턴 모델 계약 — 턴이 어떤 모델로 실행돼야 하는지를 턴 시작 시 한 곳에서 정한다.

2026-10-02 하루에 모델 바꿔치기 수정이 5건 들어갔다. 턴 모델을 정하는 지점이 선택창
저장값 / 세션 current_model / intent 정책 강등 / 재시도 예비 모델 / 재개 / CLI 보고 모델
여섯 곳에 흩어져 서로 덮어썼기 때문이다. 이 모듈의 계약 객체가 그 중 "요청 모델"을
확정하고, 나머지 경로는 읽기만 한다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SOURCE_USER_SELECT = "user_select"
SOURCE_AUTO_REACTION_DEFAULT = "auto_reaction_default"
SOURCE_RESUME_ORIGIN = "resume_origin"
SOURCE_SESSION_FALLBACK = "session_fallback"
# 선택창 지정 없이 운영 기본 모델로 시작한 일반 자동 라우팅 턴.
SOURCE_AUTO_ROUTED = "auto_routed"

# chat_service / model_selector 의 _AUTO_ROUTED_DB_DEFAULT_MODELS 와 같아야 한다
# (tests/unit/test_turn_model_contract.py 가 일치를 검사한다).
AUTO_ROUTED_DB_DEFAULT_MODELS = frozenset({"auto-default-llm", "qwen-turbo"})
_AUTO_SELECTION_TOKENS = frozenset({"", "mixture", "auto"}) | AUTO_ROUTED_DB_DEFAULT_MODELS

# 정책 강등을 면제하는 출처: 사용자가 고른 모델, 운영이 정한 자동응답 기본값, 원 턴 모델.
_POLICY_EXEMPT_SOURCES = frozenset({
    SOURCE_USER_SELECT,
    SOURCE_AUTO_REACTION_DEFAULT,
    SOURCE_RESUME_ORIGIN,
})


def is_auto_selection(model_override: Optional[str]) -> bool:
    """선택창이 "자동"(또는 미지정)이면 True — 사용자가 모델을 고른 것이 아니다."""
    return str(model_override or "").strip() in _AUTO_SELECTION_TOKENS


def model_key(model: object) -> str:
    return str(model or "").strip().lower()


@dataclass
class TurnModelContract:
    requested_model: str
    source: str
    user_pinned: bool
    # 이 턴에서 모델이 바뀐 모든 이력. chat_turn_executions.fallback_chain 에 그대로 저장한다.
    fallback_chain: List[Dict[str, Any]] = field(default_factory=list)
    # 서브에이전트·보조 호출이 쓴 모델. actual_model 에 섞이지 않게 따로 둔다.
    aux_models: List[str] = field(default_factory=list)

    @property
    def policy_exempt(self) -> bool:
        return self.user_pinned or self.source in _POLICY_EXEMPT_SOURCES

    def note_switch(
        self,
        prev_model: object,
        new_model: object,
        *,
        reason: Optional[str],
        kind: str,
    ) -> Optional[Dict[str, Any]]:
        """모델이 실제로 바뀔 때만 이력에 남긴다. 같은 모델이면 None."""
        if model_key(prev_model) == model_key(new_model):
            return None
        last = self.fallback_chain[-1] if self.fallback_chain else None
        if last and last.get("kind") == kind and model_key(last.get("from")) == model_key(prev_model) \
                and model_key(last.get("to")) == model_key(new_model):
            return None
        entry = {
            "from": prev_model,
            "to": new_model,
            "reason": reason or "요청 오류",
            "kind": kind,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        self.fallback_chain.append(entry)
        logger.warning(
            "turn_model_switch requested=%s source=%s pinned=%s kind=%s %s -> %s reason=%s",
            self.requested_model, self.source, self.user_pinned, kind, prev_model, new_model, entry["reason"],
        )
        return entry

    def apply_to_intent_result(self, intent_result: Any) -> None:
        """call_stream 이 계약을 읽을 수 있게 붙인다. 모델 값은 건드리지 않는다."""
        intent_result.turn_model_contract = self
        intent_result.policy_exempt = self.policy_exempt

    def note_done(self, event: Dict[str, Any], actual_model: object) -> None:
        """done 이벤트에서 보조 모델과 불일치를 로그로 분리한다.

        actual_model 은 메인 응답 모델이고, used_models 는 서브에이전트까지 포함한
        호출 전체다. 둘을 같은 필드에 섞으면 보조 모델이 메인 모델처럼 보인다.
        """
        used = [str(m) for m in (event.get("used_models") or []) if m]
        actual_key = model_key(actual_model)
        aux = [m for m in used if model_key(m) != actual_key]
        if aux:
            self.aux_models = aux
        mismatch = bool(event.get("model_mismatch"))
        requested_differs = (
            actual_key not in ("", "unverified")
            and model_key(self.requested_model) not in ("", actual_key)
        )
        if aux or mismatch:
            logger.info(
                "turn_model_aux requested=%s actual=%s aux_models=%s used_models=%s "
                "model_verified=%s model_mismatch=%s",
                self.requested_model, actual_model, aux, used,
                event.get("model_verified"), mismatch,
            )
        if requested_differs and not self.fallback_chain:
            logger.warning(
                "turn_model_actual_differs requested=%s actual=%s source=%s pinned=%s "
                "model_mismatch=%s — 전환 기록 없음",
                self.requested_model, actual_model, self.source, self.user_pinned, mismatch,
            )

    def as_log(self) -> str:
        return (
            f"requested={self.requested_model} source={self.source} "
            f"pinned={self.user_pinned} switches={len(self.fallback_chain)}"
        )


async def normalize_selected_model(model_override: str) -> str:
    """선택창 저장값 정규화. `openai:<id>` 는 같은 id 의 검증된 codex_cli 행이 있으면 `codex:<id>`.

    api.openai.com 직결은 도구 호출을 못 해 첫 시도부터 400 으로 죽는다(2026-10-02, 32745da4).
    이 정규화를 call_stream 안에서 하면 요청 모델 기록과 실행 모델이 어긋나므로
    계약 생성 시점에 한 번 한다. call_stream 의 같은 처리는 이미 codex 인 값에는 no-op 이다.
    """
    value = str(model_override or "").strip()
    provider, sep, raw_id = value.partition(":")
    if not sep or provider.strip().lower() != "openai" or not raw_id.strip():
        return value
    try:
        from app.services.model_selector import _prefer_codex_for_openai_pinned

        routed = await _prefer_codex_for_openai_pinned("openai", raw_id.strip())
    except Exception as exc:  # 조회 실패 시 원래 값을 유지한다(기존 동작과 동일)
        logger.warning("selected_model_normalize_failed model=%s error=%s", value, type(exc).__name__)
        return value
    return f"codex:{raw_id.strip()}" if routed == "codex" else value


def build_turn_contract(
    *,
    model_override: Optional[str],
    intent_override: Optional[str],
    intent_model: Optional[str],
    operational_default: Optional[str],
) -> TurnModelContract:
    """새 턴의 계약. model_override 는 normalize_selected_model 을 거친 값이어야 한다."""
    override = str(model_override or "").strip()
    if override and not is_auto_selection(override):
        return TurnModelContract(requested_model=override, source=SOURCE_USER_SELECT, user_pinned=True)
    requested = str(operational_default or intent_model or "").strip()
    source = SOURCE_AUTO_REACTION_DEFAULT if intent_override == "auto_reaction" else SOURCE_AUTO_ROUTED
    return TurnModelContract(requested_model=requested, source=source, user_pinned=False)


def build_resume_contract(
    *,
    resolved_model: str,
    origin_requested_model: Optional[str],
    origin_user_pinned: bool,
    fallback_tier: Optional[str] = None,
    override_applied: Optional[str] = None,
    override_reason: str = "first_response_timeout",
) -> TurnModelContract:
    """재개 턴의 계약.

    원 턴 기록(requested_model)이 있으면 그것이 요청 모델이다(source=resume_origin).
    없을 때만 세션/메시지/워크스페이스/DB 기본값으로 대체하고 그 사실을 fallback_chain 에 남긴다.
    """
    origin = str(origin_requested_model or "").strip()
    if origin:
        contract = TurnModelContract(
            requested_model=origin, source=SOURCE_RESUME_ORIGIN, user_pinned=origin_user_pinned,
        )
        if override_applied:
            contract.note_switch(origin, override_applied, reason=override_reason, kind="resume_override")
        return contract
    contract = TurnModelContract(
        requested_model=str(resolved_model or "").strip(),
        source=SOURCE_SESSION_FALLBACK,
        user_pinned=False,
    )
    contract.fallback_chain.append({
        "from": None,
        "to": contract.requested_model,
        "reason": f"origin_requested_model_missing:{fallback_tier or 'session_current'}",
        "kind": "session_fallback",
        "at": datetime.now(timezone.utc).isoformat(),
    })
    logger.warning(
        "turn_model_session_fallback model=%s tier=%s — 원 턴 requested_model 기록 없음",
        contract.requested_model, fallback_tier or "session_current",
    )
    return contract


def pinned_failure_notice(model: object, reason: Optional[str], *, retried: bool = True) -> str:
    """고정 모델이 최종 실패했을 때 사용자에게 붙이는 안내. reason 은 분류된 고정 문구만 받는다."""
    tail = "같은 모델로 재시도했지만 실패했습니다." if retried else "인증·권한 오류는 재시도하지 않습니다."
    return (
        f"\n\n[{model} 응답 실패 ({reason or '요청 오류'}) — "
        f"직접 선택하신 모델이라 다른 모델로 바꾸지 않았습니다. {tail}]"
    )
