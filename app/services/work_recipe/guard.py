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
