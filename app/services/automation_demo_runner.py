"""대신 시연 실행기(demo_runner) — 설계서 7.3, PRD FR-08~12·24~26, AC-03·04·09.

- 서버 레인(헤드리스 Playwright)에서만 실행한다. PC 쪽 실행은 실패 카드에서 '제안'만 한다(D2).
- 매 단계 실행 **전** guard 판정. WRITE_EXTERNAL 이상이면 실행하지 않고 paused_risk 로 멈춘다.
  사용자가 [진행]을 고른 그 단계(번호 + 단계 내용 지문)만, 이 시연 안에서 한 번 실행이 허용된다.
  새 시연(다시 해 보기·이유별 재시연)은 승인을 비우고, 단계 내용이 바뀌면 지문이 달라 승인이 무효다.
  승인된 로그인 조각의 단계도 되돌릴 수 없는 행위면 멈춘다(사람 승인 없이는 실행하지 않는다).
- 단계는 기존 `WorkRecipeRecorder` 로 모은다. 로그인 값은 `{{secret.username}}`/`{{secret.password}}`
  참조로만 계획·기록되고, 실제 값은 실행 직전 메모리에서만 채운다. 화면 캡처 전 비밀번호 칸을 가린다.
- 실패 6분류. transient 만 같은 단계를 1회 다시 한다. 로그인 제출 단계는 다시 내지 않는다.
- 상한: 단계 25, 1회 5분, 초안당 시연 3회(시작 횟수는 흐름 쪽에서 센다).
- 다시 시작하면 마지막으로 성공한 조각부터 이어 간다. 로그인 세션이 살아 있으면 로그인 조각을 건너뛴다.
  이미 끝난 외부 쓰기·되돌릴 수 없는 단계는 다시 실행하지 않는다(제출·결제 중복 방지).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.services import automation_draft_service as svc
from app.services import automation_fragment_splitter as splitter
from app.services.work_recipe.audit import mask_secrets
from app.services.work_recipe.guard import APPROVAL_THRESHOLD, RiskLevel, classify_step
from app.services.work_recipe.recorder import WorkRecipeRecorder
from app.services.work_recipe.store import normalize_domain

logger = logging.getLogger(__name__)

MAX_STEPS = 25
RUN_SECONDS = 300
MAX_DEMOS = 3
PROGRESS_EVENT = "automation_draft.progress"
RUNNER_ACTOR = "system:demo_runner"
PREVIEW_ROWS = 5
_CELL_LIMIT = 200

# 계획에만 쓰는 키. 실행기·녹화기에는 넘기지 않는다.
_PLAN_KEYS = frozenset({"label", "purpose", "credential_field", "login_submit", "role", "approved_fragment"})
_SECRET_TEMPLATE = re.compile(r"^\{\{\s*secret\.(username|password)\s*\}\}$")
_ANY_TEMPLATE = re.compile(r"\{\{[^{}]+\}\}")
_LOGIN_URL = re.compile(r"login|signin|sign-in|sign_in|/auth|로그인", re.IGNORECASE)
_AUTH_FAILED_TEXT = (
    "비밀번호가 일치하지", "비밀번호를 잘못", "아이디 또는 비밀번호", "아이디나 비밀번호", "로그인 정보가 올바르지",
    "incorrect password", "invalid password", "wrong password", "invalid username or password",
    "incorrect username or password",
)
# 사람 확인 신호. 그림·문자 확인은 어느 단계에서나, 인증번호·본인확인은 로그인 단계에서만 본다
# (일반 화면의 '추가 인증 설정' 같은 메뉴 글자를 사람 확인으로 오판하지 않게).
_CAPTCHA_TEXT = ("captcha", "캡차", "보안문자", "자동입력방지", "i'm not a robot", "로봇이 아닙니다")
_LOGIN_CHALLENGE_TEXT = ("인증번호", "일회용 비밀번호", "2차 인증", "추가 인증", "본인인증", "휴대폰 본인확인",
                         "verification code", "two-factor", "2-step verification")
_OTP_WORD = re.compile(r"\botp\b", re.IGNORECASE)
_BLOCKED_TEXT = ("403", "forbidden", "access denied", "permission denied")
_LAYOUT_TEXT = ("waiting for locator", "waiting for selector", "locator(", "no node found",
                "strict mode violation", "element is not", "not attached")
_TRANSIENT_TEXT = ("timeout", "net::err", "err_", "network", "dns", "connection", "econnreset", "socket")
_ARIA_ROW = re.compile(r'^\s*-\s*row\s+"([^"\n]*)"')

# 비밀번호 칸을 캡처 전에 가린다. 값은 바꾸지 않는다(로그인이 깨지지 않게).
MASK_CSS = (
    "input[type=password]{-webkit-text-security:disc!important;color:transparent!important;"
    "text-shadow:none!important;background:#202020!important;}"
)


# ── 사람 문장 ──────────────────────────────────────────────────────────────

def _label(step: Mapping[str, Any], fallback: str = "") -> str:
    text = " ".join(str(step.get("label") or fallback or "").split())[:30]
    return "" if svc.find_secret(text) else text


def describe(step: Mapping[str, Any], *, site_name: str = "") -> str:
    """진행 이벤트 문장(설계서 7.3). 셀렉터·값은 넣지 않는다."""
    action = str(step.get("action") or "")
    if step.get("purpose") == "login" and action in ("fill", "press", "click"):
        return "로그인하는 중"
    label = _label(step)
    if action == "navigate":
        return f"'{label or site_name or '사이트'}' 화면으로 가는 중"
    if action == "click":
        return f"'{label}' 메뉴를 누르는 중" if label else "메뉴를 누르는 중"
    if action == "fill":
        if step.get("role") == "date":
            return "날짜를 고르는 중"
        return f"'{label}' 칸을 채우는 중" if label else "칸을 채우는 중"
    if action == "select":
        return f"'{label}' 고르는 중" if label else "항목을 고르는 중"
    if action == "download":
        return "파일을 받는 중"
    if action in ("snapshot", "api_call"):
        return "결과를 확인하는 중"
    return "다음 단계를 하는 중"


# ── 계획 ───────────────────────────────────────────────────────────────────

def _credential_field(step: Mapping[str, Any]) -> str | None:
    explicit = step.get("credential_field")
    if explicit in ("username", "password"):
        return str(explicit)
    match = _SECRET_TEMPLATE.match(str(step.get("value") or ""))
    return match.group(1) if match else None


def default_plan(intent: Mapping[str, Any], *, has_credential: bool, login_fragment: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """처음 시연할 계획. 승인된 같은 계정 로그인 조각이 있으면 그 단계로 로그인한다."""
    url = str(intent.get("url") or "") or f"https://{intent.get('domain') or ''}"
    site = str(intent.get("site_name") or intent.get("domain") or "")
    plan: list[dict[str, Any]] = []
    spec = (login_fragment or {}).get("spec") if login_fragment else None
    if isinstance(spec, Mapping) and spec.get("steps"):
        for raw in spec["steps"]:
            step = {k: v for k, v in dict(raw).items() if k != "seq"}
            step.update(purpose="login", approved_fragment=str(login_fragment.get("name") or ""),
                        label=str(step.get("description") or "로그인"))
            plan.append(step)
        mark_login_submit(plan)
    elif has_credential:
        plan += [
            {"action": "navigate", "url": url, "purpose": "login", "label": site},
            {"action": "fill", "purpose": "login", "credential_field": "username", "label": "아이디",
             "selector": "input[type=email], input[autocomplete=username], input[name*=id i], "
                         "input[name*=user i], input[type=text] >> nth=0",
             "value": "{{secret.username}}"},
            {"action": "fill", "purpose": "login", "credential_field": "password", "label": "비밀번호",
             "selector": "input[type=password] >> nth=0", "value": "{{secret.password}}"},
            {"action": "press", "purpose": "login", "login_submit": True, "label": "로그인",
             "selector": "input[type=password] >> nth=0", "value": "Enter"},
        ]
    if not plan or not has_credential:
        plan.append({"action": "navigate", "url": url, "purpose": "navigate", "label": site})
    plan.append({"action": "snapshot", "purpose": "query", "label": "결과", "selector": "body"})
    return number(plan)


def mark_login_submit(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """재사용 로그인 조각의 제출 단계를 되살린다.

    저장된 조각에는 계획용 표시(login_submit)가 없을 수 있다. 로그인 값(템플릿)을 채운 뒤의
    누르기·클릭은 모두 로그인 제출로 본다 — 실패 화면 검사를 받고, 자동 재시도하지 않는다.
    """
    filled = False
    for step in steps:
        action = str(step.get("action") or "")
        if action == "fill" and (_credential_field(step) or _ANY_TEMPLATE.search(str(step.get("value") or ""))):
            filled = True
        elif filled and action in ("press", "click"):
            step["login_submit"] = True
    return steps


def number(plan: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if len(plan) > MAX_STEPS:
        raise ValueError("too_many_steps")
    return [{**dict(step), "seq": index} for index, step in enumerate(plan, start=1)]


def segments(plan: Sequence[Mapping[str, Any]]) -> list[tuple[str, list[int]]]:
    """연속된 같은 purpose 묶음 = 조각. (purpose, plan 인덱스 목록)."""
    result: list[tuple[str, list[int]]] = []
    for index, step in enumerate(plan):
        purpose = str(step.get("purpose") or "navigate")
        if not result or result[-1][0] != purpose:
            result.append((purpose, []))
        result[-1][1].append(index)
    return result


def resume_index(plan: Sequence[Mapping[str, Any]], done: set[int], *, session_alive: bool) -> int:
    """FR-26: 마지막으로 성공한 조각부터. 로그인 조각은 세션이 살아 있을 때만 건너뛴다."""
    segs = segments(plan)
    last_done = -1
    for position, (_, indexes) in enumerate(segs):
        if all(int(plan[i]["seq"]) in done for i in indexes):
            last_done = position
        else:
            break
    if last_done < 0:
        return 0
    login_starts = [indexes[0] for position, (purpose, indexes) in enumerate(segs)
                    if purpose == "login" and position <= last_done]
    purpose, indexes = segs[last_done]
    if purpose == "login":
        if session_alive and last_done + 1 < len(segs):
            return segs[last_done + 1][1][0]
        return indexes[0]
    if not session_alive and login_starts:
        return login_starts[0]
    return indexes[0]


def needs_pause(step: Mapping[str, Any], level: RiskLevel) -> bool:
    """WRITE_EXTERNAL 이상이면 멈춘다. 승인된 로그인 조각의 단계는 되돌릴 수 없는 행위가 아닐 때만 통과."""
    if level.rank < APPROVAL_THRESHOLD.rank:
        return False
    return not (step.get("approved_fragment") and level is not RiskLevel.IRREVERSIBLE)


def step_fingerprint(step: Mapping[str, Any]) -> str:
    """[진행] 승인을 묶는 단계 내용 지문. 셀렉터·주소·값·이름 중 하나라도 바뀌면 달라진다."""
    payload = {k: step.get(k) for k in ("action", "selector", "url", "value", "endpoint", "label", "description",
                                        "purpose", "role")}
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def skip_on_resume(step: Mapping[str, Any], level: RiskLevel) -> bool:
    """이미 끝난 단계 중 다시 실행하면 안 되는 것 — 외부 쓰기(로그인 제외)와 되돌릴 수 없는 행위."""
    if level is RiskLevel.IRREVERSIBLE:
        return True
    return level.rank >= APPROVAL_THRESHOLD.rank and step.get("purpose") != "login"


def step_risk(step: Mapping[str, Any], domain: str) -> RiskLevel:
    payload = {k: v for k, v in step.items() if k not in _PLAN_KEYS or k == "label"}
    payload["description"] = str(step.get("label") or step.get("description") or "")
    return classify_step(payload, domain=domain)


# ── 실패 분류 ──────────────────────────────────────────────────────────────

def _evidence_text(result: Mapping[str, Any]) -> tuple[str, str]:
    evidence = result.get("evidence") if isinstance(result.get("evidence"), Mapping) else {}
    dom = evidence.get("dom") if isinstance(evidence.get("dom"), Mapping) else {}
    return str(evidence.get("url") or ""), str(dom.get("text") or "")


def is_challenge(text: str, *, login_step: bool) -> bool:
    lowered = text[:4000].lower()
    if any(term in lowered for term in _CAPTCHA_TEXT):
        return True
    return login_step and (any(term in lowered for term in _LOGIN_CHALLENGE_TEXT) or bool(_OTP_WORD.search(lowered)))


def inspect_success(step: Mapping[str, Any], result: Mapping[str, Any], *, pre_url: str) -> str | None:
    """성공으로 돌아왔어도 화면이 실패를 말하는 경우(로그인 되돌아옴·인증 요구·차단 화면)."""
    url, text = _evidence_text(result)
    access = result.get("server_access")
    if isinstance(access, Mapping) and access.get("blocked"):
        return "blocked"
    if is_challenge(text, login_step=step.get("purpose") == "login"):
        return "challenge"
    if step.get("login_submit"):
        lowered = text.lower()
        if any(marker in lowered for marker in _AUTH_FAILED_TEXT):
            return "auth_failed"
        path = url.split("?", 1)[0]
        if _LOGIN_URL.search(path) and (not pre_url or path == pre_url.split("?", 1)[0]):
            return "auth_failed"
    return None


def classify_error(step: Mapping[str, Any], result: Mapping[str, Any]) -> str:
    """실패 결과 → 6분류 코드."""
    recovery = result.get("recovery") if isinstance(result.get("recovery"), Mapping) else {}
    reason = str(recovery.get("reason") or "")
    error = str(result.get("error") or "").lower()
    _, text = _evidence_text(result)
    if reason == "server_ip_blocked" or reason == "permission_insufficient":
        return "blocked"
    login_step = step.get("purpose") == "login"
    if reason in ("CAPTCHA_REQUIRED", "OTP_REQUIRED") or is_challenge(f"{text}\n{error}", login_step=login_step):
        return "challenge"
    if any(marker in error for marker in _BLOCKED_TEXT):
        return "blocked"
    if any(marker in error for marker in _LAYOUT_TEXT):
        return "layout_changed"
    if any(marker in error for marker in _TRANSIENT_TEXT) or reason == "network_failure":
        return "transient"
    if step.get("action") in ("click", "fill", "select", "press"):
        return "layout_changed"
    return "transient"


# ── 결과 미리보기 ──────────────────────────────────────────────────────────

def preview_rows(output: Any, *, table: bool) -> list[str]:
    """화면 결과에서 최대 5행. 표를 원하면 표 행만, 아니면 보이는 글줄."""
    if isinstance(output, Sequence) and not isinstance(output, (str, bytes)):
        lines = [" | ".join(map(str, row)) if isinstance(row, Sequence) and not isinstance(row, str) else str(row)
                 for row in output]
    else:
        text = str(output or "")
        lines = [m.group(1) for m in (_ARIA_ROW.match(line) for line in text.splitlines()) if m]
        if not lines and table:
            lines = [line for line in text.splitlines() if "\t" in line or "|" in line]
        if not lines and not table:
            lines = [line for line in text.splitlines() if line.strip()]
    rows: list[str] = []
    for line in lines:
        clean = mask_secrets(" ".join(str(line).split()))[:_CELL_LIMIT]
        if clean and not svc.find_secret(clean):
            rows.append(clean)
        if len(rows) >= PREVIEW_ROWS:
            break
    return rows


# ── 실행 ───────────────────────────────────────────────────────────────────

@dataclass
class Outcome:
    status: str  # done | paused | failed
    failure_code: str | None = None
    reason: str = ""
    seq: int | None = None
    risk: str | None = None
    label: str = ""


@dataclass
class DemoDeps:
    """바깥 의존성. 테스트는 실제 사이트 없이 모의 실행기로 바꿔 끼운다."""

    executor_factory: Callable[[dict[str, Any], set[int]], Any] | None = None
    load_credential: Callable[[str, str], Awaitable[dict[str, Any] | None]] | None = None
    find_login_fragment: Callable[..., Awaitable[dict[str, Any] | None]] | None = None
    emit: Callable[[str, str, dict[str, Any]], Awaitable[None]] | None = None
    open_frames: Callable[[dict[str, Any]], Awaitable[str | None]] | None = None
    push_frame: Callable[..., Awaitable[None]] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


def _obj(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _render(step: Mapping[str, Any], secrets: Mapping[str, str], domain: str) -> dict[str, Any]:
    """실행기로 보낼 payload. 비밀값은 여기서만 채우고 어디에도 저장하지 않는다."""
    payload = {k: v for k, v in step.items() if k not in _PLAN_KEYS}
    payload["description"] = str(step.get("label") or step.get("description") or "")
    field_name = _credential_field(step)
    if field_name:
        payload["value"] = secrets.get(field_name, "")
    payload["context"] = {"domain": domain}
    return payload


def _record_raw(step: Mapping[str, Any], level: RiskLevel) -> dict[str, Any]:
    """녹화기로 넘길 단계. 값은 계획의 템플릿 그대로({{secret.*}}) — 실제 비밀값이 아니다."""
    raw = {k: v for k, v in step.items() if k not in _PLAN_KEYS and k != "seq"}
    raw["description"] = str(step.get("label") or step.get("description") or "")
    field_name = _credential_field(step)
    if field_name:
        raw.update(credential=True, credential_variable=f"secret.{field_name}", value=f"{{{{secret.{field_name}}}}}")
    elif not (step.get("purpose") == "login" and step.get("action") == "navigate"):
        raw["risk"] = level.value
    if step.get("login_submit"):
        raw["login_submit"] = True  # 조각 재사용 때 제출 단계를 알아보게 남긴다(값 아님)
    return raw


class _Run:
    def __init__(self, draft: dict[str, Any], deps: DemoDeps) -> None:
        self.draft = draft
        self.deps = deps
        self.intent = draft["intent"]
        self.domain = normalize_domain(self.intent.get("domain") or "")
        self.ref = draft["recording_ref"]
        self.plan: list[dict[str, Any]] = []
        self.done: set[int] = set()
        self.recorded: dict[int, dict[str, Any]] = {}
        # [진행] 승인: 단계 번호 → 단계 내용 지문. 실행하면 소모된다.
        self.approved: dict[int, str] = {}
        # 실행기 게이트가 보는 허용 단계 번호. 같은 집합 객체를 실행기에 넘기고 여기서 갱신한다.
        self.allowed: set[int] = set()
        self.last_url = ""
        self.last_shot = ""
        self.query_output: Any = None
        self.task_id: str | None = None
        self.retried = False

    async def emit(self, sentence: str, status: str, **extra: Any) -> None:
        payload = {"draft_id": str(self.draft["id"]), "status": status, "text": sentence,
                   "total": len(self.plan), **extra}
        if svc.find_secret(payload):
            payload["text"] = ""
        self.deps.events.append(payload)
        session = self.draft.get("session_id")
        if self.deps.emit and session:
            try:
                await self.deps.emit(str(session), PROGRESS_EVENT, payload)
            except Exception:  # noqa: BLE001 — 진행 표시는 시연을 막지 않는다
                logger.debug("demo_progress_emit_failed", exc_info=True)

    async def frame(self, result: Mapping[str, Any], sentence: str) -> None:
        evidence = result.get("evidence") if isinstance(result.get("evidence"), Mapping) else {}
        shot = evidence.get("screenshot") if isinstance(evidence.get("screenshot"), Mapping) else {}
        url = str(shot.get("url") or "")
        if url:
            self.last_shot = url
        if not (url and self.task_id and self.deps.push_frame):
            return
        try:
            await self.deps.push_frame(
                tenant_id=str(self.draft["tenant_id"]), task_id=self.task_id, frame_url=url,
                current_url=mask_secrets(str(evidence.get("url") or "")), current_step=sentence,
            )
        except Exception:  # noqa: BLE001
            logger.debug("demo_live_frame_failed", exc_info=True)

    def user_approved(self, step: Mapping[str, Any]) -> bool:
        seq = int(step["seq"])
        return seq in self.approved and self.approved[seq] == step_fingerprint(step)

    def build_allowed(self) -> set[int]:
        """실행기 게이트용 허용 목록: 지문이 맞는 사용자 승인 + 멈출 필요 없는 승인 로그인 조각 단계."""
        self.allowed.clear()
        for step in self.plan:
            seq = int(step["seq"])
            if self.user_approved(step):
                self.allowed.add(seq)
            elif step.get("approved_fragment") and not needs_pause(step, step_risk(step, self.domain)):
                self.allowed.add(seq)
        return self.allowed

    def consume_approval(self, seq: int) -> None:
        """[진행] 승인은 한 번 실행하면 끝난다. 같은 단계를 다시 하려면 다시 묻는다."""
        if self.approved.pop(seq, None) is not None:
            self.allowed.discard(seq)

    async def execute(self, executor: Any, step: dict[str, Any], secrets: Mapping[str, str]) -> Outcome | None:
        level = step_risk(step, self.domain)
        seq = int(step["seq"])
        site = str(self.intent.get("site_name") or "")
        if seq in self.done and skip_on_resume(step, level):
            return None  # 이미 제출·결제한 단계는 이어 하기에서 다시 내지 않는다
        if needs_pause(step, level) and not self.user_approved(step):
            self.consume_approval(seq)  # 내용이 바뀐 단계의 옛 승인은 버린다
            return Outcome("paused", seq=seq, risk=level.value, label=_label(step, describe(step, site_name=site)))
        sentence = describe(step, site_name=site)
        await self.emit(sentence, "running", step=seq)
        attempts = 0
        while True:
            attempts += 1
            result = await executor(_render(step, secrets, self.domain))
            result = result if isinstance(result, Mapping) else {"ok": False, "error": "invalid_result"}
            if result.get("ok"):
                code = inspect_success(step, result, pre_url=self.last_url)
                await self.frame(result, sentence)
                if code:
                    return Outcome("failed", failure_code=code, reason="screen_check", seq=seq)
                break
            recovery = result.get("recovery") if isinstance(result.get("recovery"), Mapping) else {}
            if result.get("status") == "blocked" and recovery.get("reason") in ("approval_required", "handoff_required"):
                risk = str(result.get("risk") or level.value)
                return Outcome("paused", seq=seq, risk=risk, label=_label(step, sentence))
            code = classify_error(step, result)
            login_step = step.get("purpose") == "login" and (step.get("login_submit") or _credential_field(step))
            if code == "transient" and attempts == 1 and not login_step:
                self.retried = True
                continue
            return Outcome("failed", failure_code=code, reason="step_failed", seq=seq)
        self.consume_approval(seq)
        url, _ = _evidence_text(result)
        if url:
            self.last_url = url
        if step.get("purpose") == "query" or step.get("action") in ("snapshot", "download", "api_call"):
            self.query_output = result.get("output")
        recorder = WorkRecipeRecorder(f"{splitter.domain_slug(self.domain)}_demo", self.domain or "site")
        recorded = recorder.record_step(_record_raw(step, level))
        entry: dict[str, Any] = {"purpose": step.get("purpose") or "navigate", "step": recorded.to_dict() if recorded else {}}
        if step.get("approved_fragment"):
            entry["approved_fragment"] = step["approved_fragment"]
        self.recorded[seq] = entry
        self.done.add(seq)
        return None

    async def drive(self, executor: Any, secrets: Mapping[str, str], start: int) -> Outcome:
        for step in self.plan[start:]:
            outcome = await self.execute(executor, step, secrets)
            if outcome is not None:
                return outcome
        return Outcome("done")


async def _default_executor(draft: dict[str, Any], allowed: set[int]) -> Any:
    from app.services.work_recipe.executor import BrowserRecipeExecutor

    class DemoBrowserExecutor(BrowserRecipeExecutor):
        """서버 레인 전용. 사용자가 [진행]을 고른 단계만 승인된 것으로 본다."""

        def __init__(self) -> None:
            super().__init__(approval_verifier=self._verify)

        @staticmethod
        def _verify(payload: Mapping[str, Any], required: RiskLevel) -> bool:
            try:
                return int(payload.get("seq") or 0) in allowed
            except (TypeError, ValueError):
                return False

        async def _collect_evidence(self, page: Any) -> dict[str, Any]:
            try:
                await page.add_style_tag(content=MASK_CSS)
            except Exception as exc:  # noqa: BLE001 — 가리지 못하면 캡처하지 않는다
                return {"url": mask_secrets(getattr(page, "url", "") or ""),
                        "screenshot": {"status": "skipped", "reason": f"mask_failed:{type(exc).__name__}"}}
            return await super()._collect_evidence(page)

    return DemoBrowserExecutor()


async def _default_credential(tenant_id: str, credential_id: str) -> dict[str, Any] | None:
    from app.services.agent_vault_service import get_agent_credential_by_id

    return await get_agent_credential_by_id(tenant_id=tenant_id, credential_id=credential_id)


async def _default_emit(session_id: str, event_type: str, payload: dict[str, Any]) -> None:
    from app.services.ohvis_task_manager import emit_task_sse

    await emit_task_sse(session_id, event_type, payload)


async def _default_open_frames(draft: dict[str, Any]) -> str | None:
    from app.services.browser_task_gateway import create_browser_task

    intent = _obj(draft.get("intent"))
    task = await create_browser_task(
        tenant_id=str(draft["tenant_id"]), user_id=str(draft["owner_user"]),
        work_key=f"automation-demo-{str(draft['id'])[:8]}",
        target_url=str(intent.get("url") or f"https://{intent.get('domain') or ''}"),
        session_id=str(draft["session_id"]) if draft.get("session_id") else None,
        current_step="대신 해 보는 중",
    )
    return str(task.get("id") or "") or None


async def _default_push_frame(**kwargs: Any) -> None:
    from app.services.browser_task_gateway import upsert_browser_task_live_frame

    await upsert_browser_task_live_frame(**kwargs)


def default_deps() -> DemoDeps:
    return DemoDeps(
        executor_factory=_default_executor, load_credential=_default_credential,
        find_login_fragment=splitter.find_reusable_login, emit=_default_emit,
        open_frames=_default_open_frames, push_frame=_default_push_frame,
    )


async def _maybe(value: Any) -> Any:
    return await value if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future) else value


async def run_demo(draft_id: str, *, tenant_id: str, deps: DemoDeps | None = None) -> Outcome:
    """demonstrating 초안 하나를 끝(결과 확인·멈춤·실패)까지 몰고 간다. 다른 상태면 아무것도 하지 않는다."""
    deps = deps or default_deps()
    draft = await svc.get_draft(draft_id, tenant_id=tenant_id)
    if draft is None or draft.get("state") != "demonstrating":
        return Outcome("skipped")
    draft = {**draft, "intent": _obj(draft.get("intent")), "recording_ref": _obj(draft.get("recording_ref"))}
    run = _Run(draft, deps)
    ref = run.ref
    credential_id = str(draft.get("credential_id") or "")
    try:
        if ref.get("plan"):
            run.plan = number(ref["plan"])
        else:
            login = None
            if credential_id and deps.find_login_fragment:
                login = await deps.find_login_fragment(
                    tenant_id=tenant_id, domain=run.domain, credential_id=credential_id,
                )
            run.plan = default_plan(run.intent, has_credential=bool(credential_id), login_fragment=login)
    except ValueError:
        return await _finish(run, Outcome("failed", failure_code="layout_changed", reason="plan_too_long"))

    same_credential = str(ref.get("credential_used") or "") == credential_id
    run.done = {int(s) for s in ref.get("completed") or []} if same_credential else set()
    run.recorded = {int(k): v for k, v in (ref.get("recorded") or {}).items()} if same_credential else {}
    approvals = ref.get("approved_risk") if isinstance(ref.get("approved_risk"), Mapping) else {}
    run.approved = {int(k): str(v) for k, v in approvals.items()}
    run.build_allowed()
    run.task_id = ref.get("browser_task_id")

    secrets: dict[str, str] = {}
    if any(_credential_field(step) for step in run.plan):
        loaded = await deps.load_credential(tenant_id, credential_id) if credential_id and deps.load_credential else None
        if not loaded or not loaded.get("password"):
            return await _finish(run, Outcome("failed", failure_code="auth_failed", reason="credential_unavailable"))
        secrets = {"username": str(loaded.get("username") or ""), "password": str(loaded.get("password") or "")}

    if not run.task_id and deps.open_frames:
        try:
            run.task_id = await deps.open_frames(draft)
        except Exception:  # noqa: BLE001 — 썸네일이 없어도 시연은 한다
            logger.debug("demo_live_task_failed", exc_info=True)

    try:
        executor = await _maybe(deps.executor_factory(draft, run.allowed)) if deps.executor_factory else None
    except Exception:  # noqa: BLE001 — 브라우저를 못 띄우면 시연 중 상태에 묶어 두지 않는다
        logger.warning("demo_executor_unavailable draft=%s", draft_id, exc_info=True)
        executor = None
    if executor is None:
        secrets.clear()
        return await _finish(run, Outcome("failed", failure_code="transient", reason="executor_unavailable"))
    alive = bool(getattr(executor, "session_alive", lambda: False)())
    start = resume_index(run.plan, run.done, session_alive=alive)
    try:
        async with asyncio.timeout(RUN_SECONDS):
            outcome = await run.drive(executor, secrets, start)
    except TimeoutError:
        outcome = Outcome("failed", failure_code="transient", reason="demo_timeout")
    except Exception:  # noqa: BLE001 — 예기치 못한 오류도 실패 카드로 끝낸다(값은 남기지 않는다)
        logger.warning("demo_runner_step_error draft=%s", draft_id, exc_info=False)
        outcome = Outcome("failed", failure_code="transient", reason="runner_error")
    finally:
        secrets.clear()
        close = getattr(executor, "close", None)
        if callable(close):
            try:
                await _maybe(close())
            except Exception:  # noqa: BLE001
                logger.debug("demo_executor_close_failed", exc_info=True)
    if outcome.status == "done":
        table = str(run.intent.get("output_kind") or "") == "table"
        rows = preview_rows(run.query_output, table=table)
        if not rows and (table or not str(run.query_output or "").strip()):
            outcome = Outcome("failed", failure_code="empty_result", reason="no_rows")
        run.query_output = rows
    return await _finish(run, outcome)


def _max_risk(run: _Run) -> str:
    from app.services.work_recipe.schema import RISK_LEVELS, risk_rank

    ranks = [risk_rank(str((entry.get("step") or {}).get("risk") or "READ")) for entry in run.recorded.values()]
    return RISK_LEVELS[max(ranks, default=0)]


async def _finish(run: _Run, outcome: Outcome) -> Outcome:
    draft = run.draft
    rows = run.query_output if isinstance(run.query_output, list) else []
    ref = {
        **run.ref,
        "plan": run.plan,
        "completed": sorted(run.done),
        "recorded": {str(k): v for k, v in sorted(run.recorded.items())},
        "approved_risk": {str(k): v for k, v in sorted(run.approved.items())},
        "credential_used": str(draft.get("credential_id") or ""),
        "browser_task_id": run.task_id,
        "paused_seq": outcome.seq if outcome.status == "paused" else None,
        "paused_label": outcome.label if outcome.status == "paused" else "",
        "last_failure": outcome.failure_code,
        "auto_retried": run.retried,
    }
    evidence = {
        **_obj(draft.get("evidence")),
        "final_screenshot_url": run.last_shot,
        "preview_rows": rows[:PREVIEW_ROWS],
        "output_kind": str(run.intent.get("output_kind") or "screen"),
        "screen_verified": False,
    }
    fields: dict[str, Any] = {"recording_ref": ref, "evidence": evidence, "max_risk": _max_risk(run)}
    tenant = str(draft["tenant_id"])
    draft_id = str(draft["id"])
    try:
        await svc.update_fields(
            draft_id, tenant_id=tenant, expected_state="demonstrating", fields=fields,
            actor=RUNNER_ACTOR, event_type="demo_step", detail={"status": outcome.status},
        )
        if outcome.status == "done":
            await svc.transition(draft_id, "demonstrating", "result_review", RUNNER_ACTOR, "demo_done", tenant_id=tenant)
            await run.emit("결과를 확인해 주세요", "done")
        elif outcome.status == "paused":
            await svc.transition(
                draft_id, "demonstrating", "paused_risk", RUNNER_ACTOR, "demo_paused_risk", tenant_id=tenant,
                detail={"seq": outcome.seq, "risk": outcome.risk},
            )
            await run.emit("확인이 필요해요", "paused", step=outcome.seq)
        else:
            await svc.transition(
                draft_id, "demonstrating", "failed", RUNNER_ACTOR, outcome.reason or "demo_failed",
                tenant_id=tenant, failure_code=outcome.failure_code, detail={"seq": outcome.seq},
            )
            await run.emit(svc.FAILURE_BODIES.get(outcome.failure_code or "", svc.GENERIC_FAILURE_BODY), "failed")
    except (svc.TransitionConflict, svc.DraftNotFound):
        # 사용자가 [그만두기] 등으로 이미 상태를 옮겼다. 결과를 덮어쓰지 않는다.
        logger.info("demo_runner_state_moved draft=%s", draft_id)
    return outcome


# ── 백그라운드 실행 ────────────────────────────────────────────────────────

_TASKS: set[asyncio.Task[Any]] = set()


def launch(draft_id: str, *, tenant_id: str) -> asyncio.Task[Any]:
    """요청 처리와 분리해 시연을 돌린다. 5분 상한은 run_demo 안의 timeout 이 건다."""

    async def _guarded() -> None:
        try:
            await run_demo(draft_id, tenant_id=tenant_id)
        except Exception:  # noqa: BLE001 — 백그라운드 예외가 조용히 사라지지 않게 남긴다
            logger.exception("demo_runner_crashed draft=%s", draft_id)

    task = asyncio.get_running_loop().create_task(_guarded(), name=f"automation-demo-{draft_id[:8]}")
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task
