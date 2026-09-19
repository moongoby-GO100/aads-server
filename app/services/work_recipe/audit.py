"""감사 기록 — 무엇을 왜 했는지 남긴다 (FR-5, FR-7).

``recipe_run_steps`` 는 **append-only** 다. 이 모듈에는 그 테이블을 고치거나
지우는 함수가 없고, 앞으로도 두지 않는다. 사후에 고칠 수 있는 기록은 증거가
아니기 때문이다 — DB 트리거가 같은 것을 한 번 더 막는다(AC-5).

시크릿은 저장 **전에** 지운다(R-KEY). URL 쿼리스트링의 token/key/password,
``sk-`` 로 시작하는 20자 이상 문자열이 대상이다. 로그·스크린샷 경로가 그대로
대시보드에 뜨는 경로가 있으므로, 마스킹은 읽는 쪽이 아니라 쓰는 쪽에서 한다.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Sequence
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.core.db_pool import get_pool
from app.services.work_recipe import approval as approval_module
from app.services.work_recipe.guard import (
    BlockedNavigation,
    DomainAllowlist,
    RiskLevel,
    classify_step,
    describe_step,
    requires_approval,
)
from app.services.work_recipe.player import STATUS_BLOCKED, StepResult
from app.services.work_recipe.schema import ALLOWED_ACTIONS
from app.services.work_recipe.store import DatabaseRunRecorder, normalize_domain

MASK = "***"

# 쿼리스트링에서 값을 지울 키. 부분 일치로 본다(`access_token`, `api_key` 포함).
SECRET_QUERY_KEYS: tuple[str, ...] = (
    "token", "key", "password", "passwd", "pwd", "secret", "auth", "session", "sig",
)

# `sk-` + 17자 이상 = 20자 이상 (R-KEY: 길이 20 이상 sk- 접두 문자열).
_PREFIX_TOKEN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-ant-[A-Za-z0-9_\-]{8,}|sk-[A-Za-z0-9_\-]{17,}|ghp_[A-Za-z0-9]{16,})"
)
_BEARER_PATTERN = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-=+/]{12,}")
_AUTH_HEADER_PATTERN = re.compile(r"(?i)(authorization\s*[:=]\s*)([^\s,;]+(?:\s+[^\s,;]+)?)")
_LONG_TOKEN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9+/=_-])(?:[A-Fa-f0-9]{20,}|[A-Za-z0-9+/]{20,}={0,2})(?![A-Za-z0-9+/=_-])"
)
_URL_PATTERN = re.compile(r"https?://[^\s\]\[<>{}\"']+")

SENSITIVE_FIELD_KEYS: frozenset[str] = frozenset(
    {"password", "passwd", "token", "secret", "api_key", "apikey", "authorization"}
)

STEP_STATUSES: tuple[str, ...] = ("success", "failed", "blocked", "skipped")


# ------------------------------------------------------------- 마스킹


def mask_secrets(text: Any) -> str:
    """문자열에서 키 형태 토큰을 지운다."""
    if text is None:
        return ""
    masked = _URL_PATTERN.sub(lambda match: mask_url(match.group(0)), str(text))
    return _mask_plain_secrets(masked)


def _mask_plain_secrets(text: Any) -> str:
    """URL 재탐색 없이 한 문자열의 헤더·토큰만 지운다."""
    masked = str(text)
    masked = _AUTH_HEADER_PATTERN.sub(rf"\1{MASK}", masked)
    masked = _BEARER_PATTERN.sub(rf"\1 {MASK}", masked)
    masked = _PREFIX_TOKEN_PATTERN.sub(
        lambda match: ("ghp_" if match.group(0).startswith("ghp_") else "sk-") + MASK,
        masked,
    )
    masked = _LONG_TOKEN_PATTERN.sub(MASK, masked)
    return masked


def mask_url(url: Any) -> str:
    """URL 쿼리스트링의 시크릿 값을 지운다. 경로·호스트는 그대로 둔다."""
    if not url:
        return ""
    raw = str(url)
    try:
        parts = urlsplit(raw)
    except ValueError:
        return mask_secrets(raw)
    if not parts.query:
        return _mask_plain_secrets(raw)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    cleaned = [
        (key, MASK if _is_secret_key(key) else _mask_plain_secrets(value))
        for key, value in pairs
    ]
    # safe="*" — 마스크(`***`)가 %2A 로 인코딩돼 읽히지 않는 것을 막는다.
    rebuilt = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(cleaned, safe="*"), parts.fragment)
    )
    return _mask_plain_secrets(rebuilt)


def _is_secret_key(key: str) -> bool:
    lowered = str(key or "").lower()
    return any(token in lowered for token in SECRET_QUERY_KEYS)


def mask_audit_payload(value: Any, *, _key: str = "") -> Any:
    """DB 기록 직전 감사 payload 전체를 재귀적으로 마스킹한다.

    민감한 이름의 dict 필드는 값의 형태와 관계없이 통째로 가리고, 그 밖의
    mapping/sequence 안쪽 문자열도 같은 규칙을 적용한다. tuple 등은 JSONB로
    안전하게 직렬화되도록 list로 정규화한다.
    """
    normalized_key = re.sub(r"[^a-z0-9]+", "_", str(_key).lower()).strip("_")
    if normalized_key in SENSITIVE_FIELD_KEYS or _is_secret_key(normalized_key):
        return MASK
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return mask_secrets(value)
    if isinstance(value, Mapping):
        return {
            mask_secrets(key): mask_audit_payload(item, _key=str(key))
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [mask_audit_payload(item) for item in value]
    return mask_secrets(value)


def selector_hash(selector: Any) -> str:
    """선택자는 해시로 남긴다 (PRD 5절 selector_hash).

    선택자 원문에는 사내 화면 구조와 때때로 값이 섞여 들어온다. 재발 추적에는
    "같은 선택자였나"만 있으면 되므로 원문 대신 지문을 남긴다.
    """
    text = str(selector or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _normalize_action(action: Any) -> str:
    text = str(action or "").strip().lower()
    if text not in ALLOWED_ACTIONS:
        raise ValueError(
            f"감사 기록에 쓸 수 없는 action 입니다: {action!r} "
            f"(허용: {', '.join(ALLOWED_ACTIONS)})"
        )
    return text


def _split_result(result: Any, *, error: str) -> tuple[str, Any]:
    """`result` 를 (status, output) 으로 가른다."""
    if result is None:
        return ("failed" if error else "success"), None
    if isinstance(result, Mapping):
        status = str(result.get("status") or ("failed" if error else "success"))
        return (status if status in STEP_STATUSES else "success"), dict(result)
    text = str(result).strip().lower()
    if text in STEP_STATUSES:
        return text, None
    return ("failed" if error else "success"), {"result": mask_secrets(result)}


def _json_ready(value: Any) -> Any:
    return mask_audit_payload(value)


# --------------------------------------------------------- append-only 기록


async def record_step(
    run_id: Any,
    seq: int,
    action: Any,
    url: Any = "",
    risk_level: Any = RiskLevel.READ,
    result: Any = None,
    error: Any = "",
    screenshot_path: Any = "",
    approval_id: Any = None,
    *,
    phase: str = "step",
    selector: Any = None,
    attempts: int = 1,
    duration_ms: int = 0,
    llm_calls: int = 0,
) -> None:
    """단계 하나를 ``recipe_run_steps`` 에 **INSERT 만** 한다 (FR-5).

    run_id 가 없으면(기록기 없이 돌린 재생) 조용히 넘어간다 — 감사 대상이 아닌
    실행까지 막으면 테스트/드라이런이 DB 를 요구하게 된다.
    """
    if run_id in (None, ""):
        return
    error_text = mask_secrets(error)
    status, output = _split_result(result, error=error_text)
    level = RiskLevel.from_any(risk_level)

    async with get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO recipe_run_steps (
                run_id, seq, phase, action, risk, status,
                attempts, duration_ms, error, output, llm_calls,
                url, selector_hash, screenshot_path, approval_id
            ) VALUES (
                $1::uuid, $2, $3, $4, $5, $6,
                $7, $8, $9, $10::jsonb, $11,
                $12, $13, $14, $15::uuid
            )
            """,
            str(run_id),
            int(seq),
            mask_secrets(phase or "step"),
            _normalize_action(action),
            level.value,
            status,
            int(attempts),
            int(duration_ms),
            error_text,
            json.dumps(_json_ready(output), ensure_ascii=False),
            int(llm_calls),
            mask_url(url),
            selector_hash(selector),
            mask_secrets(screenshot_path or ""),
            str(approval_id) if approval_id else None,
        )


async def record_credential_use(
    run_id: Any,
    domain: Any,
    recipe_name: Any,
    credential_ref: Any,
) -> None:
    """vault 를 쓴 사실을 남긴다 — 사용 1회 = 감사 1행 (FR-7).

    값은 절대 남기지 않는다. 남기는 것은 "어느 런이 어느 도메인의 어느 항목을
    썼나"까지다. 이 행이 없으면 크리덴셜 범위 제한은 검증할 수 없는 규칙이 된다.
    """
    if run_id in (None, ""):
        return
    async with get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO recipe_credential_uses (
                run_id, domain, recipe_name, credential_ref, used_at
            ) VALUES ($1::uuid, $2, $3, $4, NOW())
            """,
            str(run_id),
            mask_secrets(normalize_domain(domain)),
            mask_secrets(recipe_name or ""),
            str(mask_audit_payload(credential_ref))[:120],
        )


async def record_injection_block(
    run_id: Any,
    seq: int,
    reasons: Any,
    *,
    url: Any = "",
    detail: str = "",
) -> None:
    """인젝션 탐지 1건 = 차단 로그 1행 (AC-4).

    action 은 ``snapshot`` 으로 남긴다 — 페이지 텍스트를 읽는 순간에만 탐지되고,
    ``recipe_run_steps`` 의 action 목록은 P0 가 CHECK 로 못박아 두었다.
    """
    reason_list = list(reasons or [])
    await record_step(
        run_id,
        seq,
        "snapshot",
        url=url,
        risk_level=RiskLevel.READ,
        result={"status": "blocked", "injection_reasons": reason_list},
        error=detail or "; ".join(reason_list)[:500],
    )


async def mark_run_blocked(run_id: Any, reason: str, *, step_seq: int | None = None,
                           risk_level: Any = "") -> None:
    """런을 blocked 로 닫는다.

    ``recipe_runs`` 는 상태 테이블이라 갱신 대상이다(append-only 는
    ``recipe_run_steps`` 쪽 계약이다). 승인 대기로 끊긴 런이 'running' 인 채로
    남으면 승인 로그와 실행 로그의 1:1 대응(NFR-2)이 깨진다.
    """
    if run_id in (None, ""):
        return
    async with get_pool().acquire() as conn:
        await conn.execute(
            """
            UPDATE recipe_runs
               SET status = 'blocked',
                   error = $2,
                   blocked_step_seq = COALESCE($3, blocked_step_seq),
                   blocked_risk = COALESCE(NULLIF($4, ''), blocked_risk),
                   finished_at = COALESCE(finished_at, NOW()),
                   updated_at = NOW()
             WHERE id = $1::uuid
               AND status = 'running'
            """,
            str(run_id),
            mask_secrets(reason)[:500],
            int(step_seq) if step_seq is not None else None,
            str(risk_level or ""),
        )


# --------------------------------------------------- player 훅 (Risk Gate)


class GuardedRunRecorder:
    """RecipePlayer 에 승인 게이트와 감사 기록을 붙이는 recorder.

    player 는 recorder 의 훅을 ``getattr`` 로 찾는다(P0 가 정한 계약). 여기에
    ``before_step`` 을 더해 **단계가 실행되기 전에** 등급을 판정하고, 필요하면
    승인 카드를 발행하며 실행을 끊는다. player 본체는 훅 호출 3줄만 늘었다.

    DB 호출 넷은 전부 주입 가능하다 — 테스트가 DB 없이 같은 경로를 돌려야
    게이트가 실제로 동작하는지 증명할 수 있기 때문이다.

    Notes
    -----
    승인 게이트를 쓸 때는 player 의 ``max_risk`` 를 ``IRREVERSIBLE`` 로 열어라.
    P0 의 max_risk 는 "승인 체계가 없으니 일단 멈춘다"는 임시 상한이었고,
    그대로 두면 승인 카드가 발행되기 전에 P0 쪽에서 먼저 막아 버린다.
    """

    def __init__(
        self,
        *,
        domain: str = "",
        allowlist: DomainAllowlist | None = None,
        tenant_id: Any = None,
        triggered_by: str = "",
        task_id: str | None = None,
        requested_by: str = "",
        run_starter: Any = None,
        run_finisher: Any = None,
        step_recorder: Any = None,
        approval_requester: Any = None,
        run_blocker: Any = None,
    ) -> None:
        self._db = DatabaseRunRecorder(
            tenant_id=tenant_id, triggered_by=triggered_by, task_id=task_id
        )
        self.domain = normalize_domain(domain)
        self.allowlist = allowlist
        self.requested_by = requested_by
        self.tenant_id = tenant_id
        self._run_starter = run_starter or self._db.start_run
        self._run_finisher = run_finisher or self._db.finish_run
        self._step_recorder = step_recorder or record_step
        self._approval_requester = approval_requester or approval_module.request_approval
        self._run_blocker = run_blocker or mark_run_blocked
        # 단계별 판정 결과 — record_step 이 같은 등급·승인 id 를 남기도록 들고 있는다.
        self.levels: dict[tuple[str, int], RiskLevel] = {}
        self.approvals: dict[tuple[str, int], str] = {}
        self.recipe_name = ""

    # ---- player 훅 ----------------------------------------------------

    async def start_run(self, *, recipe: Any, inputs: Mapping[str, Any], recipe_id: Any = None) -> Any:
        if self.allowlist is None:
            self.allowlist = DomainAllowlist.for_recipe(recipe)
        if not self.domain:
            self.domain = normalize_domain(getattr(recipe, "domain", "") or "")
        self.recipe_name = str(getattr(recipe, "name", "") or "")
        return await _maybe_await(
            self._run_starter(
                recipe=recipe, inputs=mask_audit_payload(inputs), recipe_id=recipe_id
            )
        )

    async def before_step(self, *, run_id: Any, step: Any, phase: str = "step") -> None:
        """단계 실행 **직전** 게이트 (FR-4/FR-6)."""
        level = classify_step(step, domain=self.domain)
        key = (phase, int(getattr(step, "seq", 0) or 0))
        self.levels[key] = level

        if self.allowlist is not None and str(getattr(step, "action", "")) == "navigate":
            try:
                self.allowlist.assert_navigation(getattr(step, "url", ""))
            except BlockedNavigation as exc:
                await self._block(run_id, step, phase, level, str(exc))
                raise

        if not requires_approval(level):
            return

        try:
            approval_id = await _maybe_await(
                self._approval_requester(
                    run_id,
                    int(getattr(step, "seq", 0) or 0),
                    level.value,
                    describe_step(step, domain=self.domain),
                    action=str(getattr(step, "action", "") or ""),
                    domain=self.domain,
                    requested_by=self.requested_by,
                    tenant_id=self.tenant_id,
                )
            )
        except approval_module.ApprovalRequired as exc:
            await self._block(
                run_id, step, phase, level,
                f"승인 대기(approval_id={exc.approval_id}): {exc.summary}",
                approval_id=exc.approval_id,
            )
            raise
        self.approvals[key] = str(approval_id)

    async def record_step(self, *, run_id: Any, step: Any) -> None:
        """단계 실행 **직후** 감사 기록 (FR-5)."""
        key = (str(getattr(step, "phase", "step")), int(getattr(step, "seq", 0) or 0))
        level = self.levels.get(key, RiskLevel.from_any(getattr(step, "risk", "READ")))
        await _maybe_await(
            self._step_recorder(
                run_id,
                int(getattr(step, "seq", 0) or 0),
                getattr(step, "action", ""),
                url=self._step_url(step),
                risk_level=level.value,
                result={"status": getattr(step, "status", "success"),
                        "output": getattr(step, "output", None),
                        "narration": getattr(step, "narration", ""),
                        "route": getattr(step, "route", ""),
                        "evidence": getattr(step, "evidence", {}),
                        "recovery": getattr(step, "recovery", {})},
                error=getattr(step, "error", ""),
                screenshot_path=self._screenshot_of(step),
                approval_id=self.approvals.get(key),
                phase=key[0],
                attempts=int(getattr(step, "attempts", 0) or 0),
                duration_ms=int(getattr(step, "duration_ms", 0) or 0),
                llm_calls=int(getattr(step, "llm_calls", 0) or 0),
            )
        )

    async def finish_run(self, *, run_id: Any, result: Any) -> None:
        await _maybe_await(self._run_finisher(run_id=run_id, result=result))

    # ---- 내부 ----------------------------------------------------------

    async def _block(
        self,
        run_id: Any,
        step: Any,
        phase: str,
        level: RiskLevel,
        reason: str,
        *,
        approval_id: Any = None,
    ) -> None:
        """막힌 단계를 blocked 로 남기고 런을 닫는다.

        player 는 이 훅이 던지는 예외를 그대로 올려보내므로(그게 "실행하지
        않는다"의 구현이다), 기록은 여기서 끝내야 한다.
        """
        seq = int(getattr(step, "seq", 0) or 0)
        url = str(getattr(step, "url", "") or "")
        blocked = StepResult(
            seq=seq,
            action=str(getattr(step, "action", "") or ""),
            risk=level.value,
            status=STATUS_BLOCKED,
            phase=phase,
            error=reason,
            # 막힌 단계도 "어디로 가려 했나"까지 남는다 — 그게 조사의 출발점이다.
            output={"url": url} if url else None,
        )
        if approval_id:
            self.approvals[(phase, seq)] = str(approval_id)
        self.levels[(phase, seq)] = level
        await self.record_step(run_id=run_id, step=blocked)
        await _maybe_await(
            self._run_blocker(run_id, reason, step_seq=seq, risk_level=level.value)
        )

    def _step_url(self, step: Any) -> str:
        url = getattr(step, "url", "") or ""
        if url:
            return str(url)
        output = getattr(step, "output", None)
        if isinstance(output, Mapping):
            return str(output.get("url") or "")
        return ""

    def _screenshot_of(self, step: Any) -> str:
        output = getattr(step, "output", None)
        if isinstance(output, Mapping):
            return str(output.get("screenshot_path") or output.get("screenshot") or "")
        return ""


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value
