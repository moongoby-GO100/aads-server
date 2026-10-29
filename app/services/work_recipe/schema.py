"""작업 레시피 스키마 — YAML 파서/직렬화기와 `{{var}}` 치환기.

PRD 4절 위험 등급표(READ / WRITE_INTERNAL / WRITE_EXTERNAL / IRREVERSIBLE)를
그대로 쓴다. 허용되지 않은 action·risk 값은 파싱 시점에 ValueError 로 막는다.
레시피는 사람이 손으로 고치는 문서이므로, 틀린 값이 DB 에 저장된 뒤 재생
시점에 터지면 원인을 찾기 어렵다 — 검증은 전부 파싱에서 끝낸다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import yaml

# PRD FR-1: 레시피가 표현할 수 있는 동작
ALLOWED_ACTIONS: tuple[str, ...] = (
    "navigate",
    "click",
    "fill",
    "select",
    "press",
    "upload",
    "download",
    "snapshot",
    "api_call",
    "dismiss_overlays",
)

# dismiss_overlays: 허용 목록(domain+root+close)에 있는 모달만 닫는다. 동의·결제·보안 경고는
# 닫는 행위 자체가 의사표시이므로 selector 에 이런 표지가 있으면 레시피로 선언할 수 없다.
OVERLAY_DEFAULT_MAX_DISMISS = 3
OVERLAY_MAX_DISMISS_LIMIT = 5
OVERLAY_SELECTOR_MAX_LEN = 300
_OVERLAY_SENSITIVE_PATTERN = (
    r"consent|agree|terms|privacy|gdpr|cookie-?(?:banner|consent)|payment|checkout|billing|"
    r"security|warning|certificate|captcha|otp|auth|login|"
    r"동의|약관|결제|보안|경고|인증|로그인"
)
# 버튼 라벨로 닫기 대신 누르면 안 되는 동작(오늘 숨김·신청·연동·확인·저장). 사이트 공지 팝업 닫기가 라벨 검사에 쓴다.
_OVERLAY_ACTION_PATTERN = (
    r"하루\s*동안|다시\s*보지|오늘\s*(?:하루\s*)?(?:그만|보지)|신청|연동|확인|저장|구독|"
    r"don'?t\s+show|do\s+not\s+show|never\s+show|\bapply\b|\bconnect\b|\bconfirm\b|\bsave\b|\bsubscribe\b|\bsubmit\b"
)
# 모달 자체가 동의·결제·보안·인증 성격인가(닫는 행위가 의사표시) — 제목/selector 검사용.
OVERLAY_SENSITIVE_MARKERS = re.compile(_OVERLAY_SENSITIVE_PATTERN, re.IGNORECASE)
OVERLAY_FORBIDDEN_MARKERS = re.compile(
    f"{_OVERLAY_SENSITIVE_PATTERN}|{_OVERLAY_ACTION_PATTERN}",
    re.IGNORECASE,
)

# 서버 레인 '사이트 공지 팝업 닫기' — 도메인별 허용목록. 허용목록 밖 호스트에서는 동작하지 않는다.
SITE_POPUP_MAX_CLOSE_DEFAULT = 10
SITE_POPUP_MAX_CLOSE_LIMIT = 20
# 팝업 안에서 누를 수 있는 버튼은 라벨이 정확히 이 중 하나일 때뿐이다(대소문자·앞뒤 공백 무시).
SITE_POPUP_CLOSE_LABELS: tuple[str, ...] = ("닫기", "close", "×")


@dataclass(frozen=True)
class SitePopupPolicy:
    domain: str
    popup_selector: str


SITE_POPUP_ALLOWLIST: dict[str, SitePopupPolicy] = {
    "my.a-bly.com": SitePopupPolicy(
        domain="my.a-bly.com",
        popup_selector='[role="dialog"], [role="alertdialog"], [role="alert"]',
    ),
}


def site_popup_policy_for(host: str) -> SitePopupPolicy | None:
    """호스트가 허용목록 도메인(또는 그 하위)이면 정책, 아니면 None."""
    for domain, policy in SITE_POPUP_ALLOWLIST.items():
        if overlay_host_matches(host, domain):
            return policy
    return None


def clamp_site_popup_max_close(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return SITE_POPUP_MAX_CLOSE_DEFAULT
    return max(1, min(number, SITE_POPUP_MAX_CLOSE_LIMIT))
_OVERLAY_SELECTOR_CHARS = re.compile(r"""^[A-Za-z0-9_#.\[\]="'~*^$|:+> ()\-,]+$""")

# PRD 4절 위험 등급표 — 순서가 곧 등급(낮음 → 높음)이다.
RISK_LEVELS: tuple[str, ...] = (
    "READ",
    "WRITE_INTERNAL",
    "WRITE_EXTERNAL",
    "IRREVERSIBLE",
)

DEFAULT_RISK = "READ"

# `{{ var }}` / `{{var.sub}}` 를 잡는다. 중첩 반복을 쓰지 않아 백트래킹이 없다(R-BG 3).
_VAR_PATTERN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}")

# 치환 대상이 되는 문자열 필드
_TEMPLATED_FIELDS = ("url", "selector", "value", "wait_for", "endpoint")


def risk_rank(risk: str) -> int:
    """위험 등급의 서열. 모르는 값이면 ValueError."""
    normalized = str(risk or DEFAULT_RISK).strip().upper()
    if normalized not in RISK_LEVELS:
        raise ValueError(
            f"허용되지 않은 risk 값입니다: {risk!r} (허용: {', '.join(RISK_LEVELS)})"
        )
    return RISK_LEVELS.index(normalized)


def extract_variables(text: Any) -> list[str]:
    """문자열에서 `{{var}}` 변수명을 순서대로 뽑는다. 문자열이 아니면 빈 목록."""
    if not isinstance(text, str) or "{{" not in text:
        return []
    return [match.group(1) for match in _VAR_PATTERN.finditer(text)]


def render_template(text: Any, values: Mapping[str, Any], *, declared: Sequence[str] | None = None) -> Any:
    """`{{var}}` 를 values 로 치환한다.

    - declared 가 주어지면 거기에 없는 변수는 ValueError (inputs 미선언 방어).
    - declared 에 있어도 values 에 값이 없으면 ValueError (필수 입력 누락).
    - 문자열이 아니면 그대로 돌려준다.
    """
    if not isinstance(text, str) or "{{" not in text:
        return text

    declared_set = set(declared) if declared is not None else None

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if declared_set is not None and name not in declared_set:
            raise ValueError(f"inputs 에 선언되지 않은 변수입니다: {{{{{name}}}}}")
        if name not in values:
            raise ValueError(f"변수 값이 주어지지 않았습니다: {{{{{name}}}}}")
        value = values[name]
        return "" if value is None else str(value)

    return _VAR_PATTERN.sub(_replace, text)


@dataclass
class RecipeInput:
    """레시피가 요구하는 입력 변수."""

    name: str
    required: bool = True
    default: Any = None
    secret: bool = False
    description: str = ""

    @classmethod
    def from_any(cls, raw: Any) -> "RecipeInput":
        if isinstance(raw, str):
            return cls(name=raw.strip())
        if not isinstance(raw, Mapping):
            raise ValueError(f"inputs 항목은 문자열이나 매핑이어야 합니다: {raw!r}")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError("inputs 항목에 name 이 없습니다")
        if not _VAR_PATTERN.fullmatch("{{" + name + "}}"):
            raise ValueError(f"사용할 수 없는 입력 변수명입니다: {name!r}")
        return cls(
            name=name,
            required=bool(raw.get("required", True)),
            default=raw.get("default"),
            secret=bool(raw.get("secret", False)),
            description=str(raw.get("description") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": self.name, "required": self.required}
        if self.default is not None:
            payload["default"] = self.default
        if self.secret:
            payload["secret"] = True
        if self.description:
            payload["description"] = self.description
        return payload


def _upload_value(value: Any, extra: dict[str, Any], *, seq: int) -> tuple[Any, dict[str, Any]]:
    """upload 의 file_path/file_paths 는 실행 입력(value)과 같은 값이다 — value 로 모으고 따로 남기지 않는다."""
    extra = dict(extra)
    paths = extra.pop("file_paths", None)
    single = extra.pop("file_path", None)
    supplied = paths if paths not in (None, "", []) else single
    if supplied in (None, "", []):
        return value, extra
    if isinstance(supplied, (list, tuple)):
        if not all(isinstance(item, str) and item for item in supplied):
            raise ValueError(f"steps[{seq}]: file_paths 는 비어 있지 않은 문자열 목록이어야 합니다")
        supplied = list(supplied)
    elif not isinstance(supplied, str):
        raise ValueError(f"steps[{seq}]: file_path 는 문자열이어야 합니다")
    if value in (None, "", []):
        return supplied, extra
    same = [value] if isinstance(value, str) else value
    wanted = [supplied] if isinstance(supplied, str) else supplied
    if same != wanted:
        raise ValueError(f"steps[{seq}]: upload value 와 file_path(s) 가 서로 다릅니다")
    return value, extra


@dataclass
class RecipeStep:
    """레시피의 한 단계."""

    seq: int
    action: str
    selector: str | None = None
    url: str | None = None
    value: Any = None
    wait_for: str | None = None
    risk: str = DEFAULT_RISK
    save_as: str | None = None
    endpoint: str | None = None
    timeout: float | None = None
    description: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Any, *, seq: int) -> "RecipeStep":
        if not isinstance(raw, Mapping):
            raise ValueError(f"steps[{seq}] 는 매핑이어야 합니다: {raw!r}")

        action = str(raw.get("action") or "").strip()
        if action not in ALLOWED_ACTIONS:
            raise ValueError(
                f"steps[{seq}]: 허용되지 않은 action 입니다: {raw.get('action')!r} "
                f"(허용: {', '.join(ALLOWED_ACTIONS)})"
            )

        risk = str(raw.get("risk") or DEFAULT_RISK).strip().upper()
        risk_rank(risk)  # 허용값 검증 — 틀리면 ValueError

        timeout_raw = raw.get("timeout")
        timeout: float | None = None
        if timeout_raw is not None:
            try:
                timeout = float(timeout_raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"steps[{seq}]: timeout 이 숫자가 아닙니다: {timeout_raw!r}") from exc
            if timeout <= 0:
                raise ValueError(f"steps[{seq}]: timeout 은 0보다 커야 합니다: {timeout_raw!r}")

        known = {
            "action", "selector", "url", "value", "wait_for", "risk",
            "save_as", "endpoint", "timeout", "description", "seq",
        }
        extra = {k: v for k, v in raw.items() if k not in known}
        value = raw.get("value")
        if action == "upload":
            value, extra = _upload_value(value, extra, seq=seq)
        if action == "dismiss_overlays":
            extra = normalize_overlay_spec(extra, seq=seq)
            if risk_rank(risk) > risk_rank(DEFAULT_RISK):
                raise ValueError(f"steps[{seq}]: dismiss_overlays 는 risk=READ 만 허용합니다")

        return cls(
            seq=seq,
            action=action,
            selector=_opt_str(raw.get("selector")),
            url=_opt_str(raw.get("url")),
            value=value,
            wait_for=_opt_str(raw.get("wait_for")),
            risk=risk,
            save_as=_opt_str(raw.get("save_as")),
            endpoint=_opt_str(raw.get("endpoint")),
            timeout=timeout,
            description=str(raw.get("description") or ""),
            extra=extra,
        )

    def to_dict(self, *, include_seq: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {"action": self.action}
        if include_seq:
            payload["seq"] = self.seq
        for key in ("selector", "url", "wait_for", "save_as", "endpoint"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        if self.value is not None:
            payload["value"] = self.value
        payload["risk"] = self.risk
        if self.timeout is not None:
            payload["timeout"] = self.timeout
        if self.description:
            payload["description"] = self.description
        payload.update(self.extra)
        return payload

    def variables(self) -> list[str]:
        """이 단계가 참조하는 `{{var}}` 목록."""
        names: list[str] = []
        for key in _TEMPLATED_FIELDS:
            names.extend(extract_variables(getattr(self, key)))
        return names

    def render(self, values: Mapping[str, Any], *, declared: Sequence[str] | None = None) -> "RecipeStep":
        """치환된 사본을 만든다. 원본은 그대로 둔다(재생마다 재사용되므로)."""
        rendered = RecipeStep(
            seq=self.seq,
            action=self.action,
            selector=render_template(self.selector, values, declared=declared),
            url=render_template(self.url, values, declared=declared),
            value=render_template(self.value, values, declared=declared),
            wait_for=render_template(self.wait_for, values, declared=declared),
            risk=self.risk,
            save_as=self.save_as,
            endpoint=render_template(self.endpoint, values, declared=declared),
            timeout=self.timeout,
            description=self.description,
            extra=dict(self.extra),
        )
        return rendered


@dataclass
class WorkRecipe:
    """작업 레시피 한 벌."""

    name: str
    domain: str
    version: int = 1
    description: str = ""
    inputs: list[RecipeInput] = field(default_factory=list)
    steps: list[RecipeStep] = field(default_factory=list)
    verify: list[RecipeStep] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    # ---- 파싱 ----------------------------------------------------------

    @classmethod
    def from_dict(cls, raw: Any) -> "WorkRecipe":
        if not isinstance(raw, Mapping):
            raise ValueError("레시피는 매핑(YAML 문서 하나)이어야 합니다")

        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError("레시피에 name 이 없습니다")
        domain = str(raw.get("domain") or "").strip()
        if not domain:
            raise ValueError("레시피에 domain 이 없습니다")

        version_raw = raw.get("version", 1)
        try:
            version = int(version_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"version 은 정수여야 합니다: {version_raw!r}") from exc
        if version < 1:
            raise ValueError(f"version 은 1 이상이어야 합니다: {version_raw!r}")

        inputs_raw = raw.get("inputs") or []
        if not isinstance(inputs_raw, Sequence) or isinstance(inputs_raw, (str, bytes)):
            raise ValueError("inputs 는 목록이어야 합니다")
        inputs = [RecipeInput.from_any(item) for item in inputs_raw]
        seen: set[str] = set()
        for item in inputs:
            if item.name in seen:
                raise ValueError(f"inputs 에 같은 변수가 두 번 선언됐습니다: {item.name}")
            seen.add(item.name)

        steps = _parse_steps(raw.get("steps") or [], label="steps")
        if not steps:
            raise ValueError("레시피에 steps 가 하나도 없습니다")
        verify = _parse_steps(raw.get("verify") or [], label="verify", start=len(steps) + 1)

        metadata = raw.get("metadata") or {}
        if not isinstance(metadata, Mapping):
            raise ValueError("metadata 는 매핑이어야 합니다")

        if "login_contract" in metadata:
            from app.services.work_recipe.login_contract import normalize_login_contract
            metadata = {**metadata, "login_contract": normalize_login_contract(metadata["login_contract"])}

        recipe = cls(
            name=name,
            domain=domain,
            version=version,
            description=str(raw.get("description") or ""),
            inputs=inputs,
            steps=steps,
            verify=verify,
            metadata=dict(metadata),
        )
        recipe.validate_variables()
        return recipe

    @classmethod
    def from_yaml(cls, text: str) -> "WorkRecipe":
        try:
            loaded = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(f"레시피 YAML 을 읽을 수 없습니다: {exc}") from exc
        return cls.from_dict(loaded)

    # ---- 직렬화 --------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "domain": self.domain,
            "version": self.version,
        }
        if self.description:
            payload["description"] = self.description
        payload["inputs"] = [item.to_dict() for item in self.inputs]
        payload["steps"] = [step.to_dict() for step in self.steps]
        if self.verify:
            payload["verify"] = [step.to_dict() for step in self.verify]
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.to_dict(), allow_unicode=True, sort_keys=False)

    # ---- 검증/조회 -----------------------------------------------------

    def declared_names(self) -> list[str]:
        return [item.name for item in self.inputs]

    def validate_variables(self) -> None:
        """steps/verify 가 쓰는 변수가 전부 선언돼 있는지 확인.

        선언 출처는 둘이다 — inputs, 그리고 **앞선 단계의** save_as.
        뒤 단계의 save_as 를 앞에서 당겨 쓰는 것은 실행 순서상 불가능하므로
        누적 집합으로 검사한다.
        """
        available = set(self.declared_names())
        for step in self.steps:
            _assert_step_vars(step, available)
            if step.save_as:
                available.add(step.save_as)
        for step in self.verify:
            _assert_step_vars(step, available)
            if step.save_as:
                available.add(step.save_as)

    def resolve_inputs(self, values: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """기본값을 채운 입력 사전. 필수 입력이 비면 ValueError."""
        given = dict(values or {})
        resolved: dict[str, Any] = {}
        missing: list[str] = []
        for item in self.inputs:
            if item.name in given and given[item.name] is not None:
                resolved[item.name] = given[item.name]
            elif item.default is not None:
                resolved[item.name] = item.default
            elif item.required:
                missing.append(item.name)
            else:
                resolved[item.name] = None
        if missing:
            raise ValueError(f"필수 입력이 없습니다: {', '.join(missing)}")
        # 선언되지 않은 값도 넘어올 수 있으나 치환은 declared 로 제한되므로 무시한다.
        return resolved

    def max_risk(self) -> str:
        """레시피 전체에서 가장 높은 위험 등급."""
        highest = 0
        for step in [*self.steps, *self.verify]:
            highest = max(highest, risk_rank(step.risk))
        return RISK_LEVELS[highest]


def overlay_host_matches(host: str, allowed_domain: str) -> bool:
    """호스트가 허용 도메인과 같거나 그 하위인가."""
    host = str(host or "").strip().lower().rstrip(".")
    allowed = str(allowed_domain or "").strip().lower().rstrip(".")
    return bool(host and allowed) and (host == allowed or host.endswith("." + allowed))


def _overlay_selector(value: Any, *, seq: int, field_name: str) -> str:
    selector = str(value or "").strip()
    if not selector or len(selector) > OVERLAY_SELECTOR_MAX_LEN or not _OVERLAY_SELECTOR_CHARS.fullmatch(selector):
        raise ValueError(f"steps[{seq}]: overlays.{field_name} selector 가 비었거나 안전하지 않습니다")
    if "," in selector:
        raise ValueError(f"steps[{seq}]: overlays.{field_name} 는 selector 목록(,)을 쓸 수 없습니다")
    if OVERLAY_FORBIDDEN_MARKERS.search(selector):
        raise ValueError(
            f"steps[{seq}]: overlays.{field_name} 는 동의·결제·보안 경고·인증 모달을 대상으로 할 수 없습니다"
        )
    return selector


def _overlay_max_dismiss(extra: Mapping[str, Any], *, seq: int) -> int:
    max_raw = extra.get("max_dismiss", OVERLAY_DEFAULT_MAX_DISMISS)
    try:
        max_dismiss = int(max_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"steps[{seq}]: max_dismiss 가 정수가 아닙니다: {max_raw!r}") from exc
    if not 1 <= max_dismiss <= OVERLAY_MAX_DISMISS_LIMIT:
        raise ValueError(f"steps[{seq}]: max_dismiss 는 1~{OVERLAY_MAX_DISMISS_LIMIT} 이어야 합니다")
    return max_dismiss


def normalize_overlay_spec(extra: Mapping[str, Any], *, seq: int) -> dict[str, Any]:
    """dismiss_overlays 의 overlays/max_dismiss 를 검증해 정규화한다. 모르는 키는 거부."""
    unknown = sorted(set(extra) - {"overlays", "max_dismiss"})
    if unknown:
        raise ValueError(f"steps[{seq}]: dismiss_overlays 가 모르는 필드: {', '.join(map(str, unknown))}")
    # overlays 를 아예 적지 않으면 실행기가 현재 호스트의 공용 목록(browser_site_overlays, approved)을 쓴다.
    # 적었는데 비었거나 형식이 틀리면 지금처럼 거부한다.
    if "overlays" not in extra:
        return {"max_dismiss": _overlay_max_dismiss(extra, seq=seq)}
    overlays_raw = extra["overlays"]
    if not isinstance(overlays_raw, Sequence) or isinstance(overlays_raw, (str, bytes)) or not overlays_raw:
        raise ValueError(f"steps[{seq}]: dismiss_overlays 는 overlays 허용 목록이 필요합니다")
    overlays: list[dict[str, str]] = []
    for index, item in enumerate(overlays_raw, start=1):
        if not isinstance(item, Mapping) or set(item) - {"domain", "root", "close"}:
            raise ValueError(f"steps[{seq}]: overlays[{index}] 는 domain/root/close 만 가진 매핑이어야 합니다")
        domain = str(item.get("domain") or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?", domain) or "." not in domain:
            raise ValueError(f"steps[{seq}]: overlays[{index}].domain 이 올바른 도메인이 아닙니다")
        overlays.append({
            "domain": domain,
            "root": _overlay_selector(item.get("root"), seq=seq, field_name="root"),
            "close": _overlay_selector(item.get("close"), seq=seq, field_name="close"),
        })
    return {"overlays": overlays, "max_dismiss": _overlay_max_dismiss(extra, seq=seq)}


def _assert_step_vars(step: "RecipeStep", available: set[str]) -> None:
    for name in step.variables():
        root = name.split(".", 1)[0]
        if name not in available and root not in available:
            raise ValueError(
                f"steps[{step.seq}] 가 선언되지 않은 변수를 씁니다: {{{{{name}}}}} "
                f"(inputs 또는 앞선 단계의 save_as 에 없음)"
            )


def _parse_steps(raw: Any, *, label: str, start: int = 1) -> list[RecipeStep]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError(f"{label} 는 목록이어야 합니다")
    return [RecipeStep.from_dict(item, seq=start + index) for index, item in enumerate(raw)]


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def parse_recipe(source: str | Mapping[str, Any]) -> WorkRecipe:
    """YAML 문자열이나 이미 파싱된 매핑에서 레시피를 만든다."""
    if isinstance(source, Mapping):
        return WorkRecipe.from_dict(source)
    return WorkRecipe.from_yaml(str(source))


def dump_recipe(recipe: WorkRecipe) -> str:
    """레시피를 YAML 문자열로 직렬화한다."""
    return recipe.to_yaml()
