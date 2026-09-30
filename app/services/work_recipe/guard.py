"""Risk Gate — 행위 등급 판정 · 도메인 허용목록 · 프롬프트 인젝션 방어 (FR-4, FR-6).

이 모듈은 **정책만** 담는다. DB 도 LLM 도 부르지 않으므로 판정 결과를 테스트로
고정할 수 있고, 실행 경로 어디에서든 부담 없이 부를 수 있다. 기록은
:mod:`audit`, 승인 레코드는 :mod:`approval` 이 맡는다.

설계에서 지킨 두 가지.

1. **등급은 올려잡는다.** 모르는 도메인, 모르는 action 은 외부 쓰기로 본다.
   틀려서 승인을 한 번 더 받는 비용은 작고, 틀려서 결제를 실행하는 비용은 크다.
2. **페이지 텍스트는 데이터다.** 페이지에서 나온 문자열을 도구 인자로 올리는
   편의 함수를 만들지 않는다. 있으면 언젠가 쓰인다 — 대신 반대 방향의
   :func:`assert_not_page_derived` 만 둔다(FR-6, 아키텍처 5절).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from app.services.work_recipe.schema import DEFAULT_RISK, RISK_LEVELS
from app.services.work_recipe.store import normalize_domain

# ---------------------------------------------------------------- 등급


class RiskLevel(str, Enum):
    """PRD 4절 행위 등급. 문자열 값은 schema.RISK_LEVELS 와 같아야 한다."""

    READ = "READ"
    WRITE_INTERNAL = "WRITE_INTERNAL"
    WRITE_EXTERNAL = "WRITE_EXTERNAL"
    IRREVERSIBLE = "IRREVERSIBLE"

    @property
    def rank(self) -> int:
        """낮음(0) → 높음(3)."""
        return RISK_LEVELS.index(self.value)

    @classmethod
    def from_any(cls, value: Any) -> "RiskLevel":
        """문자열/Enum 을 등급으로. 모르는 값이면 ValueError."""
        if isinstance(value, cls):
            return value
        text = str(value or DEFAULT_RISK).strip().upper()
        try:
            return cls(text)
        except ValueError as exc:
            raise ValueError(
                f"허용되지 않은 위험 등급입니다: {value!r} (허용: {', '.join(RISK_LEVELS)})"
            ) from exc


# 승인 카드가 필요한 하한 — PRD 4절: WRITE_EXTERNAL 부터 CEO 승인.
APPROVAL_THRESHOLD = RiskLevel.WRITE_EXTERNAL

# 사내 도메인. 여기서의 쓰기는 WRITE_INTERNAL 상한이다(자동 실행 + 감사).
INTERNAL_DOMAINS: frozenset[str] = frozenset(
    {"aads.newtalk.kr", "pick.newtalk.kr", "localhost", "127.0.0.1"}
)

# 되돌릴 수 없는 행위의 신호어. 사내/외부를 가리지 않고 IRREVERSIBLE 로 올린다 —
# 사내 시스템의 "삭제"도 되돌릴 수 없기는 마찬가지다.
IRREVERSIBLE_SIGNALS: tuple[str, ...] = (
    "결제", "구매", "주문확정", "주문 확정", "확정", "삭제", "탈퇴", "동의",
    "송금", "이체", "해지", "환불",
    "pay", "payment", "checkout", "purchase", "delete", "destroy",
    "confirm", "terminate", "withdraw", "refund",
)

# 바깥으로 나가는 쓰기의 신호어.
WRITE_SIGNALS: tuple[str, ...] = (
    "제출", "등록", "저장", "전송", "발송", "신청", "예약", "수정", "업로드", "보내기",
    "submit", "send", "save", "register", "create", "update", "upload",
    "publish", "apply", "post",
)

# action 자체로 등급이 정해지는 것들.
_IRREVERSIBLE_ACTIONS: frozenset[str] = frozenset({"payment", "pay", "purchase"})
_WRITE_ACTIONS: frozenset[str] = frozenset({"submit", "api_call", "upload"})
# 읽기로 끝나는 action — 신호어가 붙어도 이동/조회 이상은 하지 않는다.
_READ_ACTIONS: frozenset[str] = frozenset({"navigate", "snapshot", "download", "select"})

# 등급 판정에 쓰는 필드. `value` 는 일부러 뺀다 — 사용자가 입력한 **데이터**이지
# 행위의 의도가 아니다. 검색창에 "삭제"를 치는 것과 삭제 버튼을 누르는 것은 다르다.
#
# url 을 따로 두는 이유. 조회성 action(navigate/snapshot 등)은 주소에 "checkout"
# 이나 "delete" 가 들어 있어도 페이지를 여는 것까지다 — 확정은 그 다음 클릭이
# 한다. 주소만 보고 IRREVERSIBLE 로 올리면 결제 페이지를 **열어보는** 레시피가
# 전부 승인 대기에 걸려, 게이트가 소음이 되고 결국 무력화된다(R-ERRBOOK 3).
_INTENT_FIELDS: tuple[str, ...] = ("selector", "wait_for", "endpoint", "description")
_SIGNAL_FIELDS: tuple[str, ...] = ("action", "url", *_INTENT_FIELDS)


class GuardError(RuntimeError):
    """Risk Gate 가 실행을 막았다."""


class BlockedNavigation(GuardError):
    """허용목록 밖 도메인으로 이동하려 했다 (FR-6)."""

    def __init__(self, url: str, host: str, allowed: Iterable[str]) -> None:
        self.url = url
        self.host = host
        self.allowed = sorted(allowed)
        super().__init__(
            f"허용목록 밖 도메인으로 이동할 수 없습니다: {host or url!r} "
            f"(허용: {', '.join(self.allowed) or '없음'})"
        )


class InjectionBlocked(GuardError):
    """페이지에서 유래한 문자열이 도구 인자로 승격되려 했다 (FR-6)."""

    def __init__(self, field: str, excerpt: str) -> None:
        self.field = field
        self.excerpt = excerpt
        super().__init__(
            f"페이지 텍스트에서 유래한 값은 도구 인자로 쓸 수 없습니다 "
            f"(field={field or '?'}, excerpt={excerpt!r})"
        )


def _field_reader(step: Any):
    if isinstance(step, Mapping):
        return lambda key: step.get(key)
    return lambda key: getattr(step, key, None)


def _signal_text(step: Any, *, fields: Sequence[str] = _SIGNAL_FIELDS) -> str:
    read = _field_reader(step)
    parts = [str(read(key) or "") for key in fields]
    extra = read("extra")
    if isinstance(extra, Mapping):
        # `text`/`label` 처럼 레시피가 버튼 문구를 남겨둔 경우까지 본다.
        parts.extend(str(extra.get(key) or "") for key in ("text", "label", "name", "confirm"))
    return " ".join(parts).lower()


def _matches_any(text: str, signals: Sequence[str]) -> bool:
    return any(signal in text for signal in signals)


def is_internal_host(value: Any) -> bool:
    """사내 도메인(또는 그 하위 도메인)인가."""
    host = normalize_domain(value)
    if not host:
        return False
    return any(host == known or host.endswith("." + known) for known in INTERNAL_DOMAINS)


def classify_step(step: Any, *, domain: str | None = None) -> RiskLevel:
    """단계 하나의 행위 등급을 판정한다 (PRD 4절).

    Parameters
    ----------
    step:
        :class:`~app.services.work_recipe.schema.RecipeStep` 또는 같은 키를 가진 매핑.
    domain:
        단계에 url 이 없을 때 쓸 레시피 도메인. 없고 url 도 없으면 **외부**로 본다.

    레시피가 선언한 ``risk`` 는 **하한**이다. 작성자가 WRITE_EXTERNAL 이라 적었으면
    판정이 READ 로 나와도 내리지 않는다 — 사람이 위험하다고 본 것을 코드가
    뒤집을 이유가 없다.
    """
    read = _field_reader(step)
    action = str(read("action") or "").strip().lower()
    reads_only = action in _READ_ACTIONS
    text = _signal_text(step, fields=_INTENT_FIELDS if reads_only else _SIGNAL_FIELDS)
    declared = RiskLevel.from_any(read("risk") or DEFAULT_RISK)

    if action in _IRREVERSIBLE_ACTIONS or _matches_any(text, IRREVERSIBLE_SIGNALS):
        return RiskLevel.IRREVERSIBLE

    writes = action in _WRITE_ACTIONS or (
        not reads_only and _matches_any(text, WRITE_SIGNALS)
    )
    if not writes:
        computed = RiskLevel.READ
    else:
        host = normalize_domain(read("url") or "") or normalize_domain(domain or "")
        computed = RiskLevel.WRITE_INTERNAL if is_internal_host(host) else RiskLevel.WRITE_EXTERNAL

    return computed if computed.rank >= declared.rank else declared


def requires_approval(level: Any) -> bool:
    """WRITE_EXTERNAL 이상이면 승인 카드가 필요하다 (PRD 4절)."""
    return RiskLevel.from_any(level).rank >= APPROVAL_THRESHOLD.rank


def requires_confirmation(level: Any) -> bool:
    """IRREVERSIBLE 은 승인에 더해 재확인 문구를 요구한다 (PRD 4절)."""
    return RiskLevel.from_any(level) is RiskLevel.IRREVERSIBLE


def describe_step(step: Any, *, domain: str | None = None) -> str:
    """승인 카드에 띄울 한 줄 요약. **값(value)은 넣지 않는다**(R-KEY)."""
    read = _field_reader(step)
    action = str(read("action") or "?")
    target = str(read("url") or read("selector") or read("endpoint") or domain or "")
    description = str(read("description") or "")
    summary = f"{action} {target}".strip()
    return f"{summary} — {description}" if description else summary


# ------------------------------------------- 실행 직전 심사 (Auto-review)
#
# classify_step 은 레시피에 **적힌** 신호어를 정적으로 본다. 여기서는 실행 직전에
# 한 번 더, 같은 단계를 원 지시·custom rule·불변 안전요구에 대조한다. 결정론적
# 프로그램이다 — LLM 을 부르지 않고(llm_calls 는 항상 0), 호출자가 낸 자기신고
# (approved/screen_verified 등)는 판정에 쓰지 않는다. 신뢰하지 않는 것이 이 게이트의
# 존재 이유다.

AUTOREVIEW_ENV = "WORK_RECIPE_AUTOREVIEW"
MODE_OBSERVE = "observe"
MODE_ENFORCE = "enforce"

# 호출자가 payload 에 실어 보낼 수 있는 자기신고 키. 판정에는 쓰이지 않고 기록만 된다.
SELF_REPORTED_CLAIM_KEYS: tuple[str, ...] = (
    "approved", "approval", "screen_verified", "succeeded", "verified", "safe",
)


def autoreview_mode(value: Any = None) -> str:
    """``observe``(기본, 기록만) 또는 ``enforce``(차단). 모르는 값은 observe."""
    raw = os.getenv(AUTOREVIEW_ENV, "") if value is None else value
    return MODE_ENFORCE if str(raw or "").strip().lower() == MODE_ENFORCE else MODE_OBSERVE


class AutoReviewVerdict(str, Enum):
    ALLOW = "ALLOW"                # 무승인 실행
    PRE_APPROVED = "PRE_APPROVED"  # 서버가 발급한 사전승인 범위 안
    APPROVE_EACH = "APPROVE_EACH"  # 실행마다 검증된 승인 필요
    HANDOFF = "HANDOFF"            # 사람이 직접 끝내야 함 — 승인으로도 대체 불가


class ApprovalMissing(GuardError):
    """승인 근거 없이 승인이 필요한 단계를 실행하려 했다."""

    def __init__(
        self, level: str, reason: str = "", decision: "AutoReviewDecision | None" = None,
    ) -> None:
        self.level = level
        self.reason = reason
        self.decision = decision
        super().__init__(
            f"검증된 승인 근거가 없어 실행하지 않습니다: risk={level}"
            + (f" ({reason})" if reason else "")
        )


class AutoReviewBlocked(GuardError):
    """enforce 모드에서 Auto-review 가 실행을 막았다."""

    def __init__(self, decision: "AutoReviewDecision") -> None:
        self.decision = decision
        super().__init__(
            f"Auto-review 가 실행을 막았습니다: verdict={decision.verdict.value} "
            f"reasons={', '.join(decision.reasons) or '-'}"
        )


@dataclass(frozen=True)
class PreApproval:
    """서버가 발급한 사전승인 범위. **이 타입의 인스턴스만** 인정한다.

    payload/컨텍스트에서 온 dict 는 자기신고이므로 무시한다. 범위는 좁게 —
    도메인+action 을 모두 지정해야 하고, WRITE_EXTERNAL 을 넘는 등급은 덮지 못한다.
    """

    domain: str
    actions: frozenset[str] = frozenset()
    max_risk: RiskLevel = RiskLevel.WRITE_EXTERNAL
    source: str = ""

    def covers(self, host: str, action: str, level: RiskLevel) -> bool:
        if not self.actions or not host or action not in self.actions:
            return False
        domain = normalize_domain(self.domain)
        if not domain or not (host == domain or host.endswith("." + domain)):
            return False
        return level.rank <= min(self.max_risk.rank, RiskLevel.WRITE_EXTERNAL.rank)


@dataclass(frozen=True)
class AutoReviewDecision:
    verdict: AutoReviewVerdict
    level: RiskLevel          # 실효 등급 — 정적 등급 아래로 내려가지 않는다
    static_level: RiskLevel
    mode: str
    reasons: tuple[str, ...] = ()
    ignored_claims: tuple[str, ...] = ()
    ignored_rules: tuple[str, ...] = ()
    llm_calls: int = 0

    @property
    def enforced(self) -> bool:
        return self.mode == MODE_ENFORCE

    @property
    def needs_human(self) -> bool:
        return self.verdict in (AutoReviewVerdict.APPROVE_EACH, AutoReviewVerdict.HANDOFF)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "level": self.level.value,
            "static_level": self.static_level.value,
            "mode": self.mode,
            "enforced": self.enforced,
            "reasons": list(self.reasons),
            "ignored_claims": list(self.ignored_claims),
            "ignored_rules": list(self.ignored_rules),
            "llm_calls": self.llm_calls,
        }


_CREDENTIAL_SUBMIT_ACTIONS: frozenset[str] = frozenset({"click", "press", "submit", "api_call"})
_LOGIN_SIGNALS: tuple[str, ...] = (
    "login", "log-in", "log_in", "logon", "log-on", "signin", "sign-in", "sign_in",
    "signon", "sign-on", "로그인", "로그온",
)
_CREDENTIAL_FIELD_SIGNALS: tuple[str, ...] = ("password", "passwd", "비밀번호", "패스워드")
_SUBMIT_KEYS: frozenset[str] = frozenset({"", "enter", "return"})

# 승인으로도 대체할 수 없는 불변 안전요구 (dots 기본 정책과 같은 취지).
_PASSWORD_CHANGE_SIGNALS: tuple[str, ...] = (
    "비밀번호 변경", "비밀번호변경", "비밀번호 재설정", "비밀번호재설정", "비번 변경", "비번변경",
    "패스워드 변경", "패스워드변경",
    "change password", "change-password", "change_password", "changepassword",
    "password change", "password-change", "password_change", "passwordchange",
    "reset password", "reset-password", "reset_password", "resetpassword",
    "update-password", "update_password", "updatepassword",
    "new-password", "new_password", "newpassword",
)
_TRANSFER_SIGNALS: tuple[str, ...] = (
    "송금", "이체", "transfer", "remit",
    "send money", "send-money", "send_money", "sendmoney",
)
# 조회성 복합어는 이체/송금 실행이 아니다. 정확히 이 낱말만 지우고 나머지를 본다 —
# "이체내역 … 이체 실행" 처럼 실행 표현이 함께 있으면 그대로 남아 걸린다.
_TRANSFER_BENIGN: tuple[str, ...] = (
    "이체내역", "이체 내역", "이체조회", "이체 조회", "송금내역", "송금 내역", "송금조회", "송금 조회",
    "transfer history", "transfer-history", "transfer_history",
    "transfer list", "transfer-list", "transfer_list",
    "transfer log", "transfer-log", "transfer_log",
)


def _is_credential_submit(step: Any, action: str) -> bool:
    """로그인/자격증명 제출인가. 정적 판정은 이걸 READ 로 흘린다."""
    if action not in _CREDENTIAL_SUBMIT_ACTIONS:
        return False
    read = _field_reader(step)
    text = _signal_text(step)
    if _matches_any(text, _LOGIN_SIGNALS):
        return True
    if action == "press":
        key = str(read("key") or read("value") or "").strip().lower()
        intent = _signal_text(step, fields=_INTENT_FIELDS)
        return key in _SUBMIT_KEYS and _matches_any(intent, _CREDENTIAL_FIELD_SIGNALS)
    return False


def _handoff_signals(step: Any, action: str) -> list[str]:
    """비밀번호 변경·송금/이체 신호. 조회성 action 은 보지 않는다."""
    if action in _READ_ACTIONS:
        return []
    text = _signal_text(step)
    found: list[str] = []
    if _matches_any(text, _PASSWORD_CHANGE_SIGNALS):
        found.append("password_change")
    for benign in _TRANSFER_BENIGN:
        text = text.replace(benign, " ")
    if _matches_any(text, _TRANSFER_SIGNALS):
        found.append("money_transfer")
    return found


def _host_within(host: str, domains: Iterable[str]) -> bool:
    return any(host == known or host.endswith("." + known) for known in domains)


_HOST_IN_TEXT = re.compile(r"[a-z0-9][a-z0-9.-]{0,120}\.[a-z]{2,24}")


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable):
        return [str(item) for item in value]
    return [str(value)]


def _parse_instruction(instruction: Any) -> tuple[frozenset[str], frozenset[str], bool]:
    """원 지시에서 (허용 도메인, 허용 action, 읽기 전용 여부). 적혀 있지 않으면 비어 있다."""
    if instruction is None:
        return frozenset(), frozenset(), False
    goal = ""
    domains: list[str] = []
    actions: list[str] = []
    read_only = False
    if isinstance(instruction, Mapping):
        goal = str(instruction.get("goal") or "")
        domains = _as_list(instruction.get("domains")) + _as_list(instruction.get("allowed_domains"))
        domains += _as_list(instruction.get("domain"))
        actions = _as_list(instruction.get("allowed_actions"))
        read_only = bool(instruction.get("read_only"))
    else:
        goal = str(instruction)
    domains += _HOST_IN_TEXT.findall(goal.lower())
    hosts = {normalize_domain(item) for item in domains}
    hosts.discard("")
    return (
        frozenset(hosts),
        frozenset(item.strip().lower() for item in actions if item.strip()),
        read_only,
    )


_RULE_EFFECTS: dict[str, AutoReviewVerdict] = {
    "approve_each": AutoReviewVerdict.APPROVE_EACH,
    "require_approval": AutoReviewVerdict.APPROVE_EACH,
    "ask": AutoReviewVerdict.APPROVE_EACH,
    "handoff": AutoReviewVerdict.HANDOFF,
    "deny": AutoReviewVerdict.HANDOFF,
    "block": AutoReviewVerdict.HANDOFF,
}


def _apply_custom_rules(
    rules: Iterable[Any], step: Any, action: str, host: str,
) -> tuple[list[AutoReviewVerdict], list[str], list[str]]:
    """custom rule 은 **조이는 쪽으로만** 작동한다.

    ``allow`` 처럼 판정을 낮추려는 효과는 무시하고 ignored 로 남긴다 — 사용자가
    적은 규칙이라도 핵심 요구를 끌 수 없다(dots FAQ 와 같은 원칙).
    """
    text = _signal_text(step)
    hits: list[AutoReviewVerdict] = []
    reasons: list[str] = []
    ignored: list[str] = []
    for index, raw in enumerate(rules or ()):
        if not isinstance(raw, Mapping):
            ignored.append(f"rule[{index}]:not_a_mapping")
            continue
        name = str(raw.get("name") or f"rule[{index}]")
        effect = _RULE_EFFECTS.get(str(raw.get("effect") or "").strip().lower())
        if effect is None:
            ignored.append(f"{name}:effect_cannot_relax_core_requirements")
            continue
        domains = {normalize_domain(item) for item in _as_list(raw.get("domains"))} - {""}
        actions = {item.strip().lower() for item in _as_list(raw.get("actions"))} - {""}
        signals = [item.strip().lower() for item in _as_list(raw.get("signals")) if item.strip()]
        if not (domains or actions or signals):
            ignored.append(f"{name}:no_match_criteria")
            continue
        if domains and not (host and _host_within(host, domains)):
            continue
        if actions and action not in actions:
            continue
        if signals and not _matches_any(text, signals):
            continue
        hits.append(effect)
        reasons.append(f"custom_rule:{name}:{effect.value}")
    return hits, reasons, ignored


def _bump(level: RiskLevel) -> RiskLevel:
    return RiskLevel(RISK_LEVELS[min(level.rank + 1, len(RISK_LEVELS) - 1)])


def _max_level(left: RiskLevel, right: RiskLevel) -> RiskLevel:
    return left if left.rank >= right.rank else right


def autoreview_step(
    step: Any,
    *,
    instruction: Any = None,
    custom_rules: Iterable[Any] = (),
    claims: Mapping[str, Any] | None = None,
    pre_approvals: Iterable[Any] = (),
    domain: str | None = None,
    mode: str | None = None,
) -> AutoReviewDecision:
    """실행 **직전** 단계를 심사해 ALLOW/PRE_APPROVED/APPROVE_EACH/HANDOFF 로 판정한다.

    - ``classify_step`` 의 정적 등급을 하한으로 깔고 그 위에 얹는다(내리지 않는다).
    - ``claims`` (approved/screen_verified 등 호출자 자기신고)는 판정에 쓰지 않는다.
      어떤 키를 무시했는지만 결정에 남긴다.
    - ``custom_rules`` 는 조이는 효과(approve_each/handoff)만 받아들인다.
    - ``pre_approvals`` 는 :class:`PreApproval` 인스턴스만 인정한다. 자격증명 제출·
      HANDOFF·지시 범위 밖·custom rule 에 걸린 단계는 사전승인으로 덮지 못한다.
    - 비밀번호 변경·송금/이체 신호가 있으면 무조건 HANDOFF.
    """
    read = _field_reader(step)
    action = str(read("action") or "").strip().lower()
    static = classify_step(step, domain=domain)
    host = normalize_domain(read("url") or "") or normalize_domain(domain or "")
    resolved_mode = autoreview_mode(mode)

    level = static
    reasons: list[str] = []
    needs_each = requires_approval(static)
    if needs_each:
        reasons.append(f"static_level:{static.value}")

    credential_submit = _is_credential_submit(step, action)
    if credential_submit:
        needs_each = True
        level = _max_level(level, RiskLevel.WRITE_EXTERNAL)
        reasons.append("credential_submit")

    handoff = _handoff_signals(step, action)
    reasons.extend(f"handoff:{item}" for item in handoff)

    scope_violation = False
    domains, allowed_actions, read_only = _parse_instruction(instruction)
    if domains:
        if not host:
            scope_violation = True
            reasons.append("intent:target_domain_unknown")
        elif not _host_within(host, domains):
            scope_violation = True
            reasons.append("intent:domain_out_of_scope")
    if allowed_actions and action not in allowed_actions:
        scope_violation = True
        reasons.append("intent:action_out_of_scope")
    if read_only and (static.rank > RiskLevel.READ.rank or credential_submit or handoff):
        scope_violation = True
        reasons.append("intent:write_in_read_only_scope")
    if scope_violation:
        needs_each = True
        level = _bump(level)

    rule_hits, rule_reasons, ignored_rules = _apply_custom_rules(custom_rules, step, action, host)
    reasons.extend(rule_reasons)
    if AutoReviewVerdict.HANDOFF in rule_hits:
        handoff.append("custom_rule")
    if rule_hits:
        needs_each = True

    ignored_claims = tuple(sorted(str(key) for key, value in (claims or {}).items() if value))

    if handoff:
        verdict = AutoReviewVerdict.HANDOFF
        level = RiskLevel.IRREVERSIBLE
    elif needs_each:
        grants = [item for item in pre_approvals or () if isinstance(item, PreApproval)]
        covered = (
            static.rank <= RiskLevel.WRITE_EXTERNAL.rank
            and not (credential_submit or scope_violation or rule_hits)
            and any(item.covers(host, action, level) for item in grants)
        )
        if covered:
            verdict = AutoReviewVerdict.PRE_APPROVED
            reasons.append("pre_approved_scope")
        else:
            verdict = AutoReviewVerdict.APPROVE_EACH
            level = _max_level(level, RiskLevel.WRITE_EXTERNAL)
    else:
        verdict = AutoReviewVerdict.ALLOW

    return AutoReviewDecision(
        verdict=verdict,
        level=level,
        static_level=static,
        mode=resolved_mode,
        reasons=tuple(reasons),
        ignored_claims=ignored_claims,
        ignored_rules=tuple(ignored_rules),
        llm_calls=0,
    )


# ------------------------------------------------------- 도메인 허용목록


@dataclass(frozen=True)
class DomainAllowlist:
    """레시피가 선언한 도메인 + 사내 도메인만 허용한다 (FR-6).

    하위 도메인은 허용한다(``pick.newtalk.kr`` 이 허용되면 ``a.pick.newtalk.kr`` 도).
    그 밖의 호스트로 가는 navigate 는 :class:`BlockedNavigation` 으로 멈춘다.
    """

    domains: frozenset[str]

    @classmethod
    def for_recipe(cls, recipe: Any, *, extra: Iterable[str] = ()) -> "DomainAllowlist":
        """레시피(또는 도메인 문자열)에서 허용목록을 만든다."""
        declared: list[str] = []
        if isinstance(recipe, str):
            declared.append(recipe)
        elif recipe is not None:
            declared.append(str(getattr(recipe, "domain", "") or ""))
            metadata = getattr(recipe, "metadata", None)
            if isinstance(metadata, Mapping):
                allowed = metadata.get("allowed_domains") or []
                if isinstance(allowed, str):
                    declared.append(allowed)
                elif isinstance(allowed, Sequence):
                    declared.extend(str(item) for item in allowed)
        declared.extend(str(item) for item in extra)

        hosts = {normalize_domain(item) for item in declared}
        hosts.discard("")
        return cls(domains=frozenset(hosts | INTERNAL_DOMAINS))

    def is_allowed(self, url_or_host: Any) -> bool:
        host = normalize_domain(url_or_host)
        if not host:
            return False
        return any(host == known or host.endswith("." + known) for known in self.domains)

    def assert_navigation(self, url: Any) -> str:
        """허용목록 안이면 호스트를 돌려주고, 밖이면 BlockedNavigation."""
        host = normalize_domain(url)
        if not self.is_allowed(url):
            raise BlockedNavigation(str(url or ""), host, self.domains)
        return host


# --------------------------------------------------- 프롬프트 인젝션 방어

BLOCK_MARKER = "[BLOCKED]"

# 중첩 반복(`(?:A|B)*` 뒤에 `.*?`)을 쓰지 않는다 — 대용량 페이지에서 백트래킹이
# 폭발한다(R-BG 3). 사이의 거리는 `[^\n]{0,N}` 으로 **상한을 박아** 둔다.
_INJECTION_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "instruction_override",
        "이전 지시를 무시하라는 유도",
        re.compile(r"(?:이전|앞|위|기존)[^\n]{0,12}(?:지시|명령|프롬프트|규칙)[^\n]{0,12}(?:무시|잊)"),
    ),
    (
        "instruction_override_en",
        "ignore/disregard previous instructions",
        re.compile(
            r"\b(?:ignore|disregard|forget|override)\b[^\n]{0,24}"
            r"\b(?:previous|prior|earlier|above|all)\b[^\n]{0,24}"
            r"\b(?:instruction|instructions|prompt|prompts|rule|rules)\b",
            re.I,
        ),
    ),
    (
        "credential_exfiltration",
        "쿠키/토큰/비밀번호 외부 전송 유도",
        re.compile(r"(?:쿠키|세션|토큰|비밀번호|비번|계정정보)[^\n]{0,40}(?:전송|보내|전달|업로드|제출)"),
    ),
    (
        "credential_exfiltration_en",
        "send cookies/credentials",
        re.compile(
            r"\b(?:send|post|upload|forward|exfiltrate|leak)\b[^\n]{0,40}"
            r"\b(?:cookie|cookies|session|token|credential|credentials|password)\b",
            re.I,
        ),
    ),
    (
        "address_exfiltration",
        "아래 주소로 보내라는 유도",
        re.compile(r"(?:아래|다음|이)[^\n]{0,8}(?:주소|url|링크|엔드포인트)[^\n]{0,12}(?:전송|보내|전달|접속)", re.I),
    ),
    (
        "secret_reference",
        "시크릿 요구",
        re.compile(r"\b(?:api[ _-]?key|secret[ _-]?key|access[ _-]?token|private[ _-]?key)\b", re.I),
    ),
    (
        "anthropic_key",
        "Anthropic 키 형태 문자열",
        re.compile(r"sk-ant-[A-Za-z0-9_\-]{4,}"),
    ),
    (
        "generic_api_key",
        "sk- 접두 키 형태 문자열",
        re.compile(r"\bsk-[A-Za-z0-9]{16,}"),
    ),
    (
        "browser_storage",
        "브라우저 저장소 접근 유도",
        re.compile(r"document\.cookie|localStorage|sessionStorage", re.I),
    ),
    (
        "system_prompt_probe",
        "시스템 프롬프트 탈취 시도",
        re.compile(r"시스템\s{0,4}프롬프트|system\s{0,4}prompt|developer\s{0,4}message", re.I),
    ),
    (
        "role_hijack",
        "역할 탈취 시도",
        re.compile(r"\byou are now\b|\bfrom now on you\b|지금부터 (?:너는|당신은)", re.I),
    ),
    (
        "tool_command",
        "도구 호출 지시",
        re.compile(r"다음 명령을 실행|아래 명령을 실행|\bexecute the following\b|\bcall the tool\b", re.I),
    ),
)


def sanitize_page_text(text: Any) -> tuple[str, list[str]]:
    """페이지에서 긁은 텍스트를 **데이터로** 쓸 수 있게 만든다 (FR-6).

    지시형 패턴을 ``[BLOCKED]`` 로 지우고, 무엇을 지웠는지 사유 목록을 함께
    돌려준다. 사유는 감사 로그(:func:`audit.record_injection_block`)에 남는다.

    Returns
    -------
    (정제된 텍스트, 탐지 사유 목록)
    """
    cleaned = "" if text is None else str(text)
    reasons: list[str] = []
    for label, note, pattern in _INJECTION_PATTERNS:
        cleaned, hits = pattern.subn(BLOCK_MARKER, cleaned)
        if hits:
            reasons.append(f"{label}: {note} (x{hits})")
    return cleaned, reasons


def wrap_untrusted(text: Any) -> str:
    """정제한 페이지 텍스트를 신뢰 경계 태그로 감싼다 (아키텍처 5절).

    LLM 에 페이지 내용을 보여줘야 할 때는 반드시 이 형태로만 넘긴다.
    """
    cleaned, _ = sanitize_page_text(text)
    return f"<untrusted_page_content>\n{cleaned}\n</untrusted_page_content>"


def _normalize_for_compare(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text)).strip().casefold()


def assert_not_page_derived(
    value: Any,
    page_texts: Any,
    *,
    field: str = "",
    min_length: int = 8,
) -> None:
    """도구 인자가 페이지 텍스트에서 나온 것이면 :class:`InjectionBlocked`.

    승격 경로를 만드는 대신 **막는 쪽만** 둔다. 실행기는 레시피에 선언된 값과
    사용자 입력으로만 인자를 만들고, 그 사실을 이 함수로 증명한다.

    ``min_length`` 보다 짧은 값은 검사하지 않는다 — "1", "OK" 같은 값이 페이지
    어딘가에 우연히 들어 있는 것은 유래의 증거가 못 된다.
    """
    if isinstance(page_texts, (str, bytes)):
        pages = [page_texts]
    elif isinstance(page_texts, Iterable):
        pages = list(page_texts)
    else:
        pages = [page_texts]
    haystacks = [_normalize_for_compare(page) for page in pages if page]
    if not haystacks:
        return
    _walk_and_assert(value, haystacks, field or "value", min_length)


def _walk_and_assert(value: Any, haystacks: list[str], field: str, min_length: int) -> None:
    if isinstance(value, str):
        needle = _normalize_for_compare(value)
        if len(needle) < min_length:
            return
        for haystack in haystacks:
            if needle and needle in haystack:
                raise InjectionBlocked(field, value[:120])
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _walk_and_assert(item, haystacks, f"{field}.{key}", min_length)
        return
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for index, item in enumerate(value):
            _walk_and_assert(item, haystacks, f"{field}[{index}]", min_length)
