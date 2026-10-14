"""신한은행 기업 간편계좌 분할 레시피 — 금융 전용 게이트.

신한 로직은 전부 여기에 둔다. orchestrator·registration 에는 이 모듈을 부르는 훅만 있다.
비은행 레시피는 등록·실행 경로가 기존과 같아야 하므로, 이 모듈은 신한 호스트가 아닌
레시피에는 아무것도 바꾸지 않는다(``validate_manifest`` 는 None, 런타임 검사는 통과).

3단계 매니페스트(``metadata.shinhan_manual.stage_step_counts``)가 steps 를 세 단계로 자른다.
각 단계는 마지막 단계 성공 시 영수증(해시 체인)을 남기고, 다음 단계는 앞 영수증이 이어져야 실행된다.

영수증 한계(C5): 영수증은 PC 에이전트가 돌려준 결과를 서버가 받아 적은 **자기보고 값**이다.
변조를 막지 못하고, 실행 권한도 아니다 — 실행 권한은 서버측 승인 검증(work_recipes) 하나뿐이다.
``open`` 마커는 비로그인 화면에서도 충족되므로 성공 판정 근거로 쓰지 않는다.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote, urlsplit

from app.browser_bridge.service import get_browser_bridge_service
from app.core.db_pool import get_pool
from app.services.work_recipe import store
from app.services.work_recipe.schema import (
    RecipeStep,
    WorkRecipe,
    extract_variables,
    parse_recipe,
    render_template,
    risk_rank,
)

SHINHAN_DOMAIN = "shinhan.com"
MANIFEST_KEY = "shinhan_manual"
STAGE_NAMES: tuple[str, str, str] = ("open_login", "vault_fill", "login_confirm")
FINANCIAL_ACTIONS = frozenset({"navigate", "click", "fill", "snapshot"})
GENESIS_HASH = "0" * 64
RECEIPT_PHASE = "stage_receipt"

# 이체·송금·결제·계좌변경 계열 식별자. 영문은 앞뒤가 영문자가 아닐 때만 맞춘다.
_FORBIDDEN_IDENT = re.compile(
    r"trnsfr|transfer|remit|wire(?![a-z])|send[_\-\s]?money|pymt|"
    r"(?<![a-z])pay(?:ment|ments)?(?![a-z])|chgacnt|acnt[_\-]?chg|"
    r"이체|송금|결제|출금|계좌\s*변경",
    re.IGNORECASE,
)
_UNSAFE_URL_CHARS = re.compile(r"[\\\s\x00-\x1f]")
_SPECIAL_SCHEMES = frozenset({"http", "https", "ws", "wss", "ftp"})
_IDNA_IGNORED = re.compile("[\u00ad\u034f\u180b-\u180d\u200b-\u200d\u2060\ufe00-\ufe0f\ufeff]")
_DOT_VARIANTS = {ord(c): "." for c in "\u3002\uff0e\uff61"}
_PURE_TEMPLATE = re.compile(r"^\s*\{\{\s*[A-Za-z_][A-Za-z0-9_.]*\s*\}\}\s*$")


class FinancialManualBlocked(ValueError):
    """금융 게이트가 fail-closed 로 막았다. code 는 사유 식별자(비밀값 없음)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ----------------------------------------------------------------- A. 금융 판정


def url_host(value: Any) -> str:
    """URL(또는 도메인 문자열)을 파싱한 host. 해석할 수 없거나 위험한 문자가 있으면 빈 문자열."""
    text = str(value or "").strip()
    if not text or _UNSAFE_URL_CHARS.search(text):
        return ""
    if "//" not in text:
        text = "//" + text
    try:
        host = urlsplit(text).hostname
    except ValueError:
        return ""
    return (host or "").rstrip(".").lower()


def is_shinhan_host(host: str) -> bool:
    return host == SHINHAN_DOMAIN or host.endswith("." + SHINHAN_DOMAIN)


def is_shinhan_url(value: Any) -> bool:
    return is_shinhan_host(url_host(value))


def is_shinhan_recipe(recipe: WorkRecipe) -> bool:
    """도메인이나 step/verify 의 URL 중 하나라도 *.shinhan.com 이면 금융 레시피다(브라우저 해석 기준)."""
    if is_shinhan_navigation_target(recipe.domain):
        return True
    return any(is_shinhan_navigation_target(step.url)
               for step in [*recipe.steps, *recipe.verify] if step.url)


def navigation_host(value: Any) -> str:
    """브라우저(WHATWG URL)가 실제로 여는 host. 엄격한 ``url_host`` 와 달리 읽히는 쪽으로 정규화한다.

    탭·개행 제거, 역슬래시→슬래시, 특수 스킴 뒤 슬래시 개수 무시(``https:///h``, ``https:h``),
    userinfo·포트 제거, 퍼센트 인코딩 해제(``%73``), IDNA 폴딩(전각 문자·``。``)을 적용한다.
    호스트를 못 찾으면 빈 문자열이다 — 거부 판정 쪽에서만 쓰므로 모호한 입력은 읽히는 대로 센다.
    """
    text = re.sub(r"[\t\r\n]", "", str(value or ""))
    text = text.strip("".join(chr(c) for c in range(0x21))).replace("\\", "/")
    scheme = re.match(r"([A-Za-z][A-Za-z0-9+.\-]*):", text)
    if scheme and scheme.group(1).lower() in _SPECIAL_SCHEMES:
        rest = text[scheme.end():].lstrip("/")
    elif scheme and text[scheme.end():].startswith("//"):
        rest = text[scheme.end() + 2:]
    else:
        rest = text.lstrip("/")
    authority = re.split(r"[/?#]", rest, maxsplit=1)[0].rpartition("@")[2]
    if authority.startswith("["):
        return ""
    host = authority.partition(":")[0]
    for _ in range(3):
        decoded = unquote(host)
        if decoded == host:
            break
        host = decoded
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        host = unicodedata.normalize("NFKC", _IDNA_IGNORED.sub("", host))
        host = host.translate(_DOT_VARIANTS)
    return host.rstrip(".").lower()


def is_shinhan_navigation_target(value: Any) -> bool:
    """브라우저가 실제로 여는 대상 기준의 신한 판정(거부용)."""
    return is_shinhan_host(navigation_host(value))


def assert_unregistered_runtime_host(recipe: WorkRecipe, inputs: Mapping[str, Any]) -> None:
    """금융으로 등록되지 않은 레시피가 입력 해석 후 *.shinhan.com 에 닿으면 막는다(A2, 사전 검사).

    URL 에 템플릿이 있는 단계만 본다. 그런 단계를 렌더링하지 못하면 건너뛰지 않고 막는다(fail-closed).
    앞선 단계의 save_as 를 쓰는 URL 은 실행 전에는 값이 없으므로 여기서 판정할 수 없다 —
    그 단계는 ``assert_unregistered_navigation`` 이 실행 직전에 최종 URL 로 막는다.
    """
    planned = [*recipe.steps, *recipe.verify]
    if not any("{{" in (step.url or "") for step in planned):
        return
    try:
        values = recipe.resolve_inputs(inputs)
    except ValueError as exc:
        raise FinancialManualBlocked("shinhan_host_unverifiable") from exc
    runtime_names: set[str] = set()
    for step in planned:
        if step.url and "{{" in step.url:
            names = extract_variables(step.url)
            deferred = any(n in runtime_names or n.split(".", 1)[0] in runtime_names for n in names)
            if not deferred:
                try:
                    rendered = render_template(step.url, values, declared=sorted(values))
                except ValueError as exc:
                    raise FinancialManualBlocked("shinhan_host_unverifiable") from exc
                if is_shinhan_navigation_target(rendered):
                    raise FinancialManualBlocked("shinhan_host_unregistered_recipe")
        if step.save_as:
            runtime_names.add(step.save_as)


def assert_unregistered_navigation(payload: Mapping[str, Any]) -> None:
    """단계 디스패치 직전의 실행 경계 검사. 치환이 끝난 최종 URL 로 판정한다.

    금융 레시피는 도메인이 신한이고 ``ShinhanStageExecutor`` 가 호스트·허용 목록을 따로 강제한다.
    도메인이 신한이 아닌(= 금융으로 등록되지 않은) 레시피가 신한으로 navigate 하면 막는다.
    context.domain 은 서버가 recipe 에서 채운 값이다(payload 의 다른 필드는 믿지 않는다).
    """
    action = payload.get("action")
    if action == "navigate":
        target = payload.get("url")
    elif action == "api_call":
        target = payload.get("endpoint") or payload.get("url")
    else:
        return
    context = payload.get("context")
    domain = context.get("domain") if isinstance(context, Mapping) else ""
    if is_shinhan_url(domain):
        return
    if is_shinhan_navigation_target(target):
        raise FinancialManualBlocked("shinhan_host_unregistered_recipe")


# ------------------------------------------------- B. 단계 허용 목록 · 매니페스트


def _scan_fields(step: RecipeStep) -> list[tuple[str, str]]:
    fields = [
        ("selector", step.selector), ("value", step.value), ("url", step.url),
        ("description", step.description), ("wait_for", step.wait_for), ("endpoint", step.endpoint),
    ]
    fields.extend((f"extra.{key}", item) for key, item in step.extra.items())
    return [(name, str(item)) for name, item in fields if item not in (None, "")]


def _assert_step_allowed(step: RecipeStep, *, phase: str) -> None:
    if step.action not in FINANCIAL_ACTIONS:
        raise FinancialManualBlocked(f"financial_action_forbidden:{phase}:{step.seq}:{step.action}")
    if phase == "verify" and step.action != "snapshot":
        raise FinancialManualBlocked(f"financial_verify_action_forbidden:{step.seq}")
    if risk_rank(step.risk) >= risk_rank("IRREVERSIBLE"):
        raise FinancialManualBlocked(f"financial_risk_forbidden:{phase}:{step.seq}")
    for name, text in _scan_fields(step):
        if _FORBIDDEN_IDENT.search(text):
            raise FinancialManualBlocked(f"financial_transfer_identifier:{phase}:{step.seq}:{name}")
    if step.url:
        if "{{" in step.url:
            raise FinancialManualBlocked(f"financial_url_template_forbidden:{phase}:{step.seq}")
        if not is_shinhan_url(step.url):
            raise FinancialManualBlocked(f"financial_url_host_forbidden:{phase}:{step.seq}")
    if step.action == "fill" and not (step.value is None or _PURE_TEMPLATE.match(str(step.value))):
        raise FinancialManualBlocked(f"financial_literal_fill_value:{step.seq}")


def validate_manifest(recipe: WorkRecipe) -> tuple[int, int, int] | None:
    """금융 레시피의 매니페스트(단계별 step 수)를 검증해 돌려준다. 금융이 아니면 None.

    정적 검사(단계 허용 목록·이체 식별자)도 여기서 함께 한다 — 등록 승인과 실행이 같은 기준을 쓴다.
    """
    if not is_shinhan_recipe(recipe):
        return None
    manifest = recipe.metadata.get(MANIFEST_KEY)
    counts = manifest.get("stage_step_counts") if isinstance(manifest, Mapping) else None
    if (not isinstance(counts, (list, tuple)) or len(counts) != len(STAGE_NAMES)
            or any(isinstance(n, bool) or not isinstance(n, int) or n < 1 for n in counts)):
        raise FinancialManualBlocked("financial_manifest_missing_or_invalid")
    first, second, third = counts
    if first + second + third != len(recipe.steps):
        raise FinancialManualBlocked("financial_manifest_step_count_mismatch")
    if not recipe.verify:
        raise FinancialManualBlocked("financial_verify_required")
    if not is_shinhan_url(recipe.domain):
        raise FinancialManualBlocked("financial_domain_not_shinhan")
    for text in (recipe.name, recipe.description):
        if _FORBIDDEN_IDENT.search(str(text or "")):
            raise FinancialManualBlocked("financial_transfer_identifier:recipe")
    for step in recipe.steps:
        _assert_step_allowed(step, phase="step")
    for step in recipe.verify:
        _assert_step_allowed(step, phase="verify")
    return first, second, third


def stage_bounds(manifest: tuple[int, int, int]) -> list[tuple[int, int]]:
    """단계별 (첫 seq, 마지막 seq)."""
    bounds: list[tuple[int, int]] = []
    start = 1
    for count in manifest:
        bounds.append((start, start + count - 1))
        start += count
    return bounds


# ------------------------------------------------------- C. 단계 영수증 · 증거 체인


def _receipt_hash(receipt: Mapping[str, Any]) -> str:
    body = {key: value for key, value in receipt.items() if key != "hash"}
    return hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def make_receipt(stage_index: int, status: str, *, first_seq: int, last_seq: int,
                 markers: list[str], prev_hash: str) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "stage_index": stage_index,
        "stage": STAGE_NAMES[stage_index],
        "status": status,
        "first_seq": first_seq,
        "last_seq": last_seq,
        "markers": list(markers),
        "prev_hash": prev_hash,
        "self_reported": True,
    }
    receipt["hash"] = _receipt_hash(receipt)
    return receipt


def verify_chain(receipts: list[Mapping[str, Any]], manifest: tuple[int, int, int]) -> None:
    """영수증 1→2→3 이 실제로 이어지는지 검사한다. 위조·누락·순서 오류는 FinancialManualBlocked."""
    bounds = stage_bounds(manifest)
    if len(receipts) > len(bounds):
        raise FinancialManualBlocked("receipt_chain_too_long")
    prev = GENESIS_HASH
    for index, receipt in enumerate(receipts):
        if not isinstance(receipt, Mapping):
            raise FinancialManualBlocked("receipt_malformed")
        first, last = bounds[index]
        status = receipt.get("status")
        last_seq = receipt.get("last_seq")
        if (receipt.get("stage_index") != index or receipt.get("prev_hash") != prev
                or receipt.get("first_seq") != first or status not in ("open", "done")
                or not isinstance(last_seq, int) or isinstance(last_seq, bool)
                or receipt.get("hash") != _receipt_hash(receipt)):
            raise FinancialManualBlocked(f"receipt_forged_or_missing:{index}")
        if status == "done":
            if last_seq != last:
                raise FinancialManualBlocked(f"receipt_forged_or_missing:{index}")
        elif not first <= last_seq <= last:
            raise FinancialManualBlocked(f"receipt_forged_or_missing:{index}")
        if index < len(receipts) - 1 and status != "done":
            raise FinancialManualBlocked(f"receipt_predecessor_not_done:{index}")
        prev = str(receipt["hash"])


class StageReceiptLedger:
    """한 번의 실행에서 발급된 영수증 목록. 영수증은 단계 첫 step 성공 시 open, 마지막에 done.

    stage_index 는 0 부터다. 새 단계 진입은 stage_index == len(receipts),
    진행 중 단계의 다음 step 은 stage_index == len(receipts)-1(영수증이 open 일 때만).
    그보다 앞 단계(이미 done)나 건너뛴 단계, seq 가 단조 증가하지 않는 재실행은 모두 거부한다.
    """

    def __init__(self, manifest: tuple[int, int, int], receipts: list[Mapping[str, Any]] | None = None,
                 *, run_id: Any = None, verify_total: int = 1) -> None:
        self.manifest = manifest
        self.bounds = stage_bounds(manifest)
        self.receipts: list[dict[str, Any]] = [dict(item) for item in receipts or []]
        self.run_id = run_id
        self.verify_total = verify_total
        self.verify_done = 0

    @property
    def last_seq(self) -> int:
        return int(self.receipts[-1]["last_seq"]) if self.receipts else 0

    def stage_of(self, seq: int) -> int:
        for index, (first, last) in enumerate(self.bounds):
            if first <= seq <= last:
                return index
        raise FinancialManualBlocked("step_outside_manifest")

    def admit_step(self, seq: int) -> int:
        verify_chain(self.receipts, self.manifest)
        if seq != self.last_seq + 1:
            raise FinancialManualBlocked("step_seq_not_next")
        stage = self.stage_of(seq)
        count = len(self.receipts)
        if stage == count:
            if count and self.receipts[-1]["status"] != "done":
                raise FinancialManualBlocked("previous_stage_not_done")
        elif stage != count - 1 or self.receipts[-1]["status"] != "open":
            raise FinancialManualBlocked("stage_already_receipted")
        return stage

    def admit_verify(self) -> None:
        verify_chain(self.receipts, self.manifest)
        last = self.receipts[-1] if len(self.receipts) == len(self.bounds) else None
        if (last is None or last["status"] != "open" or last["last_seq"] != self.bounds[-1][1]
                or self.verify_done >= self.verify_total):
            raise FinancialManualBlocked("verify_before_steps_complete")

    async def step_succeeded(self, seq: int) -> None:
        stage = self.stage_of(seq)
        first, last = self.bounds[stage]
        if stage == len(self.receipts):
            prev = str(self.receipts[-1]["hash"]) if self.receipts else GENESIS_HASH
            self.receipts.append(make_receipt(stage, "open", first_seq=first, last_seq=seq,
                                              markers=["open"], prev_hash=prev))
            await self._persist(self.receipts[-1])
        else:
            current = self.receipts[-1]
            self.receipts[-1] = make_receipt(
                stage, "open", first_seq=first, last_seq=seq, markers=current["markers"],
                prev_hash=current["prev_hash"])
        if seq == last and stage < len(self.bounds) - 1:
            await self._finish_stage(["completed"])

    async def verify_succeeded(self) -> None:
        self.verify_done += 1
        if self.verify_done == self.verify_total:
            await self._finish_stage(["verified"])

    async def _finish_stage(self, extra_markers: list[str]) -> None:
        current = self.receipts[-1]
        self.receipts[-1] = make_receipt(
            current["stage_index"], "done", first_seq=current["first_seq"],
            last_seq=self.bounds[current["stage_index"]][1],
            markers=[*current["markers"], *extra_markers], prev_hash=current["prev_hash"])
        await self._persist(self.receipts[-1])

    async def _persist(self, receipt: Mapping[str, Any]) -> None:
        """기존 audit.record_step 으로 recipe_run_steps 에 INSERT 한다(append-only).

        audit 마스킹이 20자 이상 hex 를 가리므로 해시는 앞 16자만 남긴다.
        """
        from app.services.work_recipe import audit

        view = {**receipt, "hash": str(receipt["hash"])[:16], "prev_hash": str(receipt["prev_hash"])[:16]}
        await audit.record_step(
            self.run_id, int(receipt["last_seq"]), "snapshot",
            risk_level="READ",
            result={"status": "success", "stage_receipt": view},
            phase=RECEIPT_PHASE,
        )


def _result_ok(result: Any) -> bool:
    if not isinstance(result, Mapping):
        return result is not None
    return result.get("ok") is not False and str(result.get("status") or "") not in {"failed", "blocked"}


class ShinhanStageExecutor:
    """단계 실행기를 감싸 영수증 순서와 허용 목록을 강제한다. 승인 판정은 하지 않는다."""

    def __init__(self, inner: Any, ledger: StageReceiptLedger) -> None:
        self.inner = inner
        self.ledger = ledger

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            # C1: verify 는 seq 와 무관하므로 seq 비교보다 먼저 가른다.
            if payload.get("phase") == "verify":
                return await self._verify(payload)
            if payload.get("phase") == "step":
                return await self._step(payload)
            raise FinancialManualBlocked("unknown_phase")
        except FinancialManualBlocked as exc:
            return {"ok": False, "status": "blocked", "route": "pc_agent",
                    "error": f"financial_manual_blocked:{exc.code}"}

    def _check_payload(self, payload: Mapping[str, Any], *, phase: str) -> None:
        action = str(payload.get("action") or "")
        if action not in FINANCIAL_ACTIONS or (phase == "verify" and action != "snapshot"):
            raise FinancialManualBlocked(f"financial_action_forbidden:{action}")
        url = payload.get("url")
        if url and not is_shinhan_url(url):
            raise FinancialManualBlocked("financial_url_host_forbidden")
        for name in ("selector", "url", "description"):
            if _FORBIDDEN_IDENT.search(str(payload.get(name) or "")):
                raise FinancialManualBlocked(f"financial_transfer_identifier:{name}")

    async def _step(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._check_payload(payload, phase="step")
        seq = int(payload.get("seq") or 0)
        self.ledger.admit_step(seq)
        result = await self.inner(payload)
        if _result_ok(result):
            await self._record(self.ledger.step_succeeded(seq))
        return result

    async def _verify(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._check_payload(payload, phase="verify")
        self.ledger.admit_verify()
        result = await self.inner(payload)
        if not _result_ok(result):
            return result
        output = result.get("output") if isinstance(result, Mapping) else None
        if not (isinstance(output, Mapping) and output.get("verified") is True
                and output.get("screenshot_path")):
            return {"ok": False, "status": "failed", "route": "pc_agent",
                    "error": "financial_verify_evidence_required"}
        await self._record(self.ledger.verify_succeeded())
        return result

    @staticmethod
    async def _record(coro: Any) -> None:
        try:
            await coro
        except FinancialManualBlocked:
            raise
        except Exception as exc:
            raise FinancialManualBlocked("receipt_persist_failed") from exc


# ---------------------------------------------------- D. 승인·실행 경로 (서버측 검증)


class FinancialExecutionLock:
    """금융 실행은 테넌트당 한 번에 하나. 프로세스 내 집합 + Postgres advisory lock."""

    _held: set[str] = set()

    def __init__(self, tenant_id: Any) -> None:
        self.key = f"shinhan_financial:{tenant_id}"
        self._cm: Any = None
        self._conn: Any = None

    async def __aenter__(self) -> "FinancialExecutionLock":
        if self.key in self._held:
            raise FinancialManualBlocked("financial_execution_locked")
        self._held.add(self.key)
        try:
            self._cm = get_pool().acquire()
            self._conn = await self._cm.__aenter__()
            acquired = await self._conn.fetchval(
                "SELECT pg_try_advisory_lock(hashtextextended($1, 0))", self.key)
            if acquired is not True:
                raise FinancialManualBlocked("financial_execution_locked")
        except BaseException as exc:
            await self._release(unlock=False)
            if isinstance(exc, FinancialManualBlocked):
                raise
            if isinstance(exc, Exception):
                raise FinancialManualBlocked("financial_lock_unavailable") from exc
            raise
        return self

    async def __aexit__(self, *_: Any) -> bool:
        await self._release(unlock=True)
        return False

    async def _release(self, *, unlock: bool) -> None:
        try:
            if unlock and self._conn is not None:
                await self._conn.execute("SELECT pg_advisory_unlock(hashtextextended($1, 0))", self.key)
        finally:
            cm, self._cm, self._conn = self._cm, None, None
            self._held.discard(self.key)
            if cm is not None:
                await cm.__aexit__(None, None, None)


def _json_norm(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


_EXECUTION_METADATA_KEYS = (
    "lane", "work_key", "credential_scope", "native_auth_required",
    "requires_local_security_programs", "server_access_blocked", "requires_residential_ip", MANIFEST_KEY,
)


def _input_default(item: Any) -> Any:
    """기본값도 실행에 영향을 준다. secret 입력의 기본값은 원문 대신 sha256 해시로만 남긴다."""
    if item.default is None:
        return None
    normalized = _json_norm(item.default)
    if not item.secret:
        return normalized
    body = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def execution_fingerprint(recipe: WorkRecipe) -> dict[str, Any]:
    """실행에 영향을 주는 필드만 정규화한다. id·version·description·seq 같은 비실행 필드는 뺀다."""

    def view(step: RecipeStep) -> dict[str, Any]:
        return _json_norm({
            "action": step.action, "selector": step.selector, "url": step.url, "value": step.value,
            "wait_for": step.wait_for, "risk": step.risk, "endpoint": step.endpoint,
            "timeout": step.timeout, "save_as": step.save_as, "extra": step.extra,
        })

    return {
        "name": recipe.name,
        "domain": store.normalize_domain(recipe.domain),
        "inputs": [{"name": i.name, "secret": bool(i.secret), "required": bool(i.required),
                    "default": _input_default(i)} for i in recipe.inputs],
        "steps": [view(s) for s in recipe.steps],
        "verify": [view(s) for s in recipe.verify],
        "metadata": _json_norm({k: recipe.metadata[k] for k in _EXECUTION_METADATA_KEYS if k in recipe.metadata}),
    }


async def assert_server_approved(recipe: WorkRecipe, tenant_id: Any) -> None:
    """승인 상태는 호출자 입력이 아니라 서버 DB(work_recipes)의 활성 행에서만 판단한다."""
    try:
        row = await store.get_recipe_row(
            name=recipe.name, domain=recipe.domain, tenant_id=tenant_id, enabled_only=True)
    except Exception as exc:
        raise FinancialManualBlocked("approval_lookup_failed") from exc
    if not isinstance(row, Mapping) or row.get("enabled") is not True:
        raise FinancialManualBlocked("recipe_not_approved")
    if str(row.get("tenant_id") or "") != str(tenant_id):
        raise FinancialManualBlocked("approval_tenant_mismatch")
    spec = row.get("spec")
    if not isinstance(spec, Mapping):
        raise FinancialManualBlocked("approved_spec_unreadable")
    try:
        approved = parse_recipe(spec)
    except ValueError as exc:
        raise FinancialManualBlocked("approved_spec_unreadable") from exc
    if execution_fingerprint(approved) != execution_fingerprint(recipe):
        raise FinancialManualBlocked("approved_spec_mismatch")


def forbid_step_reexecution(executor: Any) -> Any:
    """같은 step 을 두 번 실행하지 못하게 executor 의 ``_attempt`` 를 한 번만 통과시킨다.

    BrowserRecipeExecutor 는 차단 화면을 감지하면 이미 PC 에서 실행한 단계를 ``_attempt`` 로
    다시 돌린다(레인 페일오버). 읽기 단계라면 무해하지만 금융 로그인 click 은 중복 제출이 된다.
    player 의 retries=0 은 이 내부 재시도를 막지 못하므로 여기서 막는다. 두 번째 호출은 실행하지 않고 blocked 로 돌려준다.
    """
    attempt = getattr(executor, "_attempt", None)
    if not callable(attempt):
        return executor
    dispatched: set[Any] = set()

    async def once(action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("phase") == "step":
            key = payload.get("seq")
            if key in dispatched:
                return {"ok": False, "status": "blocked", "route": "pc_agent", "llm_calls": 0,
                        "error": "financial_manual_blocked:financial_step_reexecution_forbidden"}
            dispatched.add(key)
        return await attempt(action, payload)

    executor._attempt = once
    return executor


def _same_key(left: Any, right: Any) -> bool:
    return hmac.compare_digest(str(left).encode("utf-8"), str(right).encode("utf-8"))


def assert_bound_local_session(browser_session_id: str, browser_work_key: str) -> None:
    """호출자가 넘긴 문자열이 아니라 서버 쪽 세션 레코드로 PC 세션 바인딩을 확인한다.

    기존 어댑터(acquire_browser_context)는 session_id 가 있으면 work_key 를 보지 않으므로,
    session_id 가 다른 업무의 세션이어도 여기서 막지 않으면 그대로 열린다. Vault 해석 전에 호출한다.
    """
    try:
        session = get_browser_bridge_service().sessions.get(browser_session_id)
    except Exception as exc:
        raise FinancialManualBlocked("browser_session_lookup_failed") from exc
    if session is None:
        raise FinancialManualBlocked("browser_session_not_found")
    try:
        session_key = str(getattr(session, "work_key", "") or "")
        kind = getattr(getattr(session, "endpoint", None), "kind", None)
        kind_value = str(getattr(kind, "value", kind) or "")
    except Exception as exc:
        raise FinancialManualBlocked("browser_session_lookup_failed") from exc
    if not _same_key(session_key, browser_work_key):
        raise FinancialManualBlocked("browser_session_work_key_mismatch")
    if kind_value != "local_agent":
        raise FinancialManualBlocked("browser_session_not_local")


def _assert_intent(action_intent: Any, tenant_id: Any) -> None:
    provenance = getattr(action_intent, "authenticated_provenance", None)
    payload = getattr(action_intent, "payload", None)
    if (action_intent is None or not isinstance(provenance, Mapping) or not provenance
            or not isinstance(payload, Mapping)):
        raise FinancialManualBlocked("action_intent_provenance_required")
    if str(getattr(action_intent, "tenant_id", "")) != str(tenant_id):
        raise FinancialManualBlocked("action_intent_tenant_mismatch")


class _ReceiptRunRecorder:
    """GuardedRunRecorder 를 그대로 위임하되, 시작된 run_id 를 영수증 기록에 넘긴다."""

    def __init__(self, inner: Any, ledger: StageReceiptLedger) -> None:
        self._inner = inner
        self._ledger = ledger

    async def start_run(self, **kwargs: Any) -> Any:
        run_id = await self._inner.start_run(**kwargs)
        self._ledger.run_id = run_id
        return run_id

    async def before_step(self, **kwargs: Any) -> Any:
        return await self._inner.before_step(**kwargs)

    async def record_step(self, **kwargs: Any) -> Any:
        return await self._inner.record_step(**kwargs)

    async def finish_run(self, **kwargs: Any) -> Any:
        return await self._inner.finish_run(**kwargs)


async def run_financial_recipe(
    recipe: WorkRecipe,
    *,
    action_intent: Any,
    tenant_id: Any,
    intent_inputs: Mapping[str, Any],
    browser_session_id: str | None,
    browser_work_key: str | None,
    triggered_by: str = "",
    task_id: str | None = None,
) -> Any:
    """금융 레시피 실행. 순서: 입력 검증 → 락 → 서버측 승인 → Vault 입력 해석 → 재생(retries=0)."""
    from app.services.work_recipe import orchestrator as orch

    _assert_intent(action_intent, tenant_id)
    manifest = validate_manifest(recipe)
    if manifest is None:
        raise FinancialManualBlocked("not_a_financial_recipe")
    if not browser_session_id or not browser_work_key:
        raise FinancialManualBlocked("browser_work_key_required")
    bound_key = recipe.metadata.get("work_key")
    if bound_key and not _same_key(bound_key, browser_work_key):
        raise FinancialManualBlocked("browser_work_key_mismatch")
    assert_bound_local_session(browser_session_id, browser_work_key)

    async with FinancialExecutionLock(tenant_id):
        await assert_server_approved(recipe, tenant_id)
        resolved_inputs = await orch._scoped_inputs(recipe, intent_inputs)
        ledger = StageReceiptLedger(manifest, verify_total=len(recipe.verify))
        recorder = _ReceiptRunRecorder(
            orch.GuardedRunRecorder(
                domain=recipe.domain, tenant_id=tenant_id, triggered_by=triggered_by,
                task_id=task_id, requested_by=triggered_by),
            ledger,
        )
        executor = forbid_step_reexecution(orch.BrowserRecipeExecutor(
            browser_session_id=browser_session_id, browser_work_key=browser_work_key))
        step_executor = ShinhanStageExecutor(orch.GenericVerificationExecutor(executor, recipe), ledger)
        return await orch.play_recipe(
            recipe,
            step_executor,
            resolved_inputs,
            recorder=recorder,
            max_risk="IRREVERSIBLE",
            retries=0,
            context={
                "domain": recipe.domain,
                "smart_browser": {
                    "native_auth_required": bool(recipe.metadata.get("native_auth_required")),
                    "requires_local_security_programs": bool(
                        recipe.metadata.get("requires_local_security_programs")),
                    "server_access_blocked": True,
                    "requires_residential_ip": bool(recipe.metadata.get("requires_residential_ip")),
                },
            },
        )
