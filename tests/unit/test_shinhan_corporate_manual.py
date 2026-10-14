"""신한은행 기업 간편계좌 분할 레시피 금융 게이트 (M9, AADS-SB-M9-SHINHAN-CORP-R8).

항목 번호(A1~E6)는 지시서 체크리스트와 같다. 비밀번호·OTP·계좌번호 원문은 어디에도 없다.
"""
from __future__ import annotations

import copy
import inspect
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.channel_router import ChannelRouter, directive_from_authenticated_context
from app.browser_bridge.models import BrowserBridgeSession, BrowserEndpoint, BrowserEndpointKind
from app.browser_bridge.registry import SessionRegistry
from app.services.work_recipe import audit, executor as executor_module, orchestrator, registration
from app.services.work_recipe import shinhan_corporate_manual as manual
from app.services.work_recipe import store
from app.services.work_recipe.player import RunResult
from app.services.work_recipe.registration import RegistrationError
from app.services.work_recipe.schema import RecipeStep, WorkRecipe, parse_recipe

TENANT = "00000000-0000-0000-0000-000000000001"
OTHER_TENANT = "00000000-0000-0000-0000-000000000002"
BLOCKED = manual.FinancialManualBlocked
ROOT = Path(__file__).parents[2]


# ------------------------------------------------------------------ 픽스처 헬퍼


def _spec(**override: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "name": "shinhan_corp_simple_account",
        "domain": "bizbank.shinhan.com",
        "inputs": [
            {"name": "vault_username", "secret": True},
            {"name": "vault_password", "secret": True},
        ],
        "steps": [
            {"action": "navigate", "url": "https://bizbank.shinhan.com/login", "risk": "WRITE_EXTERNAL"},
            {"action": "fill", "selector": "#userId", "value": "{{vault_username}}", "risk": "WRITE_EXTERNAL"},
            {"action": "fill", "selector": "#userPw", "value": "{{vault_password}}", "risk": "WRITE_EXTERNAL"},
            {"action": "click", "selector": "#btnLogin", "risk": "WRITE_EXTERNAL"},
            {"action": "navigate", "url": "https://bizbank.shinhan.com/acct/list", "risk": "READ"},
            {"action": "snapshot", "risk": "READ"},
        ],
        "verify": [{"action": "snapshot", "assertion": "element_visible", "selector": "#acctList"}],
        "metadata": {"lane": "pc_agent", "shinhan_manual": {"stage_step_counts": [1, 3, 2]}},
    }
    spec.update(override)
    return spec


def _recipe(**override: Any) -> WorkRecipe:
    return parse_recipe(_spec(**override))


def _template_recipe(domain: str = "shop.example.com") -> WorkRecipe:
    return parse_recipe({
        "name": "templated_lookup", "domain": domain,
        "inputs": [{"name": "base"}],
        "steps": [{"action": "navigate", "url": "{{base}}/orders"}, {"action": "snapshot"}],
    })


def _intent(directive: str = "bizbank.shinhan.com 간편계좌 확인", **payload: Any):
    envelope = directive_from_authenticated_context(
        {"tenant": {"id": TENANT}, "membership": {"user_id": "user@example.com"}},
        session_id="session-1", correlation_id="corr-1",
        payload={"directive": directive, "inputs": {}, **payload},
        capabilities=frozenset({"recipe.execute"}),
    )
    return ChannelRouter().route_directive(envelope, capability="recipe.execute")


class _Conn:
    def __init__(self, row: Any = None, lock: Any = True) -> None:
        self.row, self.lock, self.queries = row, lock, []

    async def fetchval(self, query, *args):
        self.queries.append(query)
        return self.lock

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        return self.row

    async def execute(self, query, *args):
        self.queries.append(query)


class _Acquire:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_):
        return False


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


def _db_row(recipe: WorkRecipe, *, enabled: bool = True, tenant: str = TENANT, version: int = 7,
            mutate=None) -> dict[str, Any]:
    """work_recipes 실제 행 모양 — spec.version 은 저장 시점 값이라 메모리 레시피와 다르다."""
    spec = recipe.to_dict()
    spec["version"] = version
    if mutate:
        mutate(spec)
    now = datetime.now(timezone.utc)
    return {
        "id": uuid.uuid4(), "tenant_id": uuid.UUID(tenant), "name": recipe.name,
        "domain": store.normalize_domain(recipe.domain), "version": version,
        "description": "저장 시점 설명", "spec": json.dumps(spec, ensure_ascii=False),
        "yaml_source": "name: x", "max_risk": recipe.max_risk(), "enabled": enabled,
        "created_by": "approver", "created_at": now, "updated_at": now,
    }


@pytest.fixture(autouse=True)
def _clean_lock():
    manual.FinancialExecutionLock._held.clear()
    yield
    manual.FinancialExecutionLock._held.clear()


@pytest.fixture
def db(monkeypatch):
    """승인 행 조회(store)와 잠금(manual)이 같은 가짜 풀을 본다."""
    state = SimpleNamespace(conn=_Conn())
    monkeypatch.setattr(store, "get_pool", lambda: _Pool(state.conn))
    monkeypatch.setattr(manual, "get_pool", lambda: _Pool(state.conn))
    return state


class _FakeRecorder:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    async def start_run(self, *, recipe, inputs, recipe_id=None):
        return "11111111-1111-1111-1111-111111111111"

    async def before_step(self, **kwargs):
        return None

    async def record_step(self, **kwargs):
        return None

    async def finish_run(self, **kwargs):
        return None


def _session(session_id: str = "bs-1", work_key: str = "wk-1",
             kind: BrowserEndpointKind = BrowserEndpointKind.LOCAL_AGENT) -> BrowserBridgeSession:
    return BrowserBridgeSession(
        session_id=session_id, label="pc",
        endpoint=BrowserEndpoint(kind=kind, url="http://127.0.0.1:9222" if kind is BrowserEndpointKind.CDP else None),
        registered_at=datetime.now(timezone.utc), work_key=work_key)


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Vault·브라우저·recorder 를 가짜로 바꾸고 접점 호출을 기록한다. 세션 레코드는 진짜 레지스트리다."""
    registry = SessionRegistry(state_dir=tmp_path / "bridge")
    registry.register(_session(), activate=False)
    log = SimpleNamespace(vault=[], browser=[], payloads=[], audit_rows=[], verify_ok=True,
                          sessions=registry)
    monkeypatch.setattr(manual, "get_browser_bridge_service", lambda: SimpleNamespace(sessions=registry))

    async def scoped(recipe, inputs):
        log.vault.append(dict(inputs))
        return dict(inputs)

    class FakeBrowser:
        def __init__(self, **kwargs) -> None:
            log.browser.append(kwargs)

        async def __call__(self, payload):
            log.payloads.append(payload)
            if payload.get("phase") == "verify":
                if log.verify_ok:
                    return {"ok": True, "route": "pc_agent",
                            "output": {"verified": True, "screenshot_path": "/shots/ok.png"}}
                return {"ok": True, "route": "pc_agent", "output": {"verified": True}}
            return {"ok": True, "route": "pc_agent"}

    original = inspect.signature(audit.record_step)

    async def record(*args, **kwargs):
        original.bind(*args, **kwargs)
        log.audit_rows.append((args, kwargs))

    monkeypatch.setattr(orchestrator, "_scoped_inputs", scoped)
    monkeypatch.setattr(orchestrator, "BrowserRecipeExecutor", FakeBrowser)
    monkeypatch.setattr(orchestrator, "GenericVerificationExecutor", lambda executor, recipe: executor)
    monkeypatch.setattr(orchestrator, "GuardedRunRecorder", _FakeRecorder)
    monkeypatch.setattr(audit, "record_step", record)
    return log


def _run_args(recipe: WorkRecipe, **override: Any) -> dict[str, Any]:
    args: dict[str, Any] = dict(
        action_intent=_intent(), tenant_id=TENANT,
        intent_inputs={"vault_username": "vault-ref-user", "vault_password": "vault-ref-pass"},
        browser_session_id="bs-1", browser_work_key="wk-1", triggered_by="user@example.com",
    )
    args.update(override)
    return args


# ============================================================== A. 금융 판정


@pytest.mark.parametrize("domain", [
    "bizbank.shinhan.com", "bank.shinhan.com", "shinhan.com", "https://BIZBANK.SHINHAN.COM/path?a=1",
    "bizbank.shinhan.com:443", "bizbank.shinhan.com.",
])
def test_A1_shinhan_domain_is_financial(domain):
    assert manual.is_shinhan_recipe(_recipe(domain=domain))


def test_A1_step_url_alone_makes_recipe_financial():
    spec = _spec(domain="portal.example.com")
    spec["steps"][0]["url"] = "https://bizbank.shinhan.com/login"
    assert manual.is_shinhan_recipe(parse_recipe(spec))
    spec = _spec(domain="portal.example.com")
    for step in spec["steps"]:
        if "url" in step:
            step["url"] = "https://portal.example.com/x"
    spec["verify"] = [{"action": "snapshot", "assertion": "url_equals", "expected": "https://a.example.com"}]
    assert not manual.is_shinhan_recipe(parse_recipe(spec))


@pytest.mark.parametrize("domain", [
    "notshinhan.com", "shinhan.com.evil.io", "evil.io", "shinhan.co.kr", "https://evil.io/?u=shinhan.com",
    "https://shinhan.com@evil.io/", "https://evil.io\\@shinhan.com/", "xshinhan.com",
])
def test_A1_lookalike_hosts_are_not_financial(domain):
    spec = _spec(domain=domain)
    spec["steps"] = [{"action": "snapshot"}]
    spec["verify"] = []
    assert not manual.is_shinhan_recipe(parse_recipe(spec))


def test_A2_template_alone_does_not_make_recipe_financial():
    recipe = _template_recipe()
    assert not manual.is_shinhan_recipe(recipe)
    assert manual.validate_manifest(recipe) is None
    manual.assert_unregistered_runtime_host(recipe, {"base": "https://shop.example.com"})


def test_A2_unregistered_recipe_reaching_shinhan_host_is_blocked():
    with pytest.raises(BLOCKED) as exc:
        manual.assert_unregistered_runtime_host(_template_recipe(), {"base": "https://bizbank.shinhan.com"})
    assert exc.value.code == "shinhan_host_unregistered_recipe"
    manual.assert_unregistered_runtime_host(_template_recipe(), {"base": "https://notshinhan.com"})
    with pytest.raises(BLOCKED) as exc:   # R9: 렌더링 못 하는 템플릿 URL 은 건너뛰지 않고 막는다
        manual.assert_unregistered_runtime_host(_template_recipe(), {})
    assert exc.value.code == "shinhan_host_unverifiable"


async def test_A2_run_directive_blocks_before_play_or_vault(monkeypatch):
    calls: list[str] = []

    async def recipes(**kwargs):
        return [{"name": "templated_lookup", "domain": "shop.example.com",
                 "spec": _template_recipe().to_dict()}]

    async def play(*a, **k):
        calls.append("play")

    async def scoped(recipe, inputs):
        calls.append("vault")
        return dict(inputs)

    monkeypatch.setattr(orchestrator, "list_recipes", recipes)
    monkeypatch.setattr(orchestrator, "play_recipe", play)
    monkeypatch.setattr(orchestrator, "_scoped_inputs", scoped)
    intent = _intent("templated_lookup 실행", inputs={"base": "https://bizbank.shinhan.com"})
    with pytest.raises(BLOCKED):
        await orchestrator.run_directive("templated_lookup 실행", TENANT, action_intent=intent)
    assert calls == []


# ============================================================ B. 단계 허용 목록


def _with_step(step: RecipeStep, index: int = 1) -> WorkRecipe:
    recipe = _recipe()
    recipe.steps[index] = step
    return recipe


@pytest.mark.parametrize("action", ["evaluate", "script", "press_key", "api_call", "press", "select", "upload",
                                    "download", "wait"])
def test_B1_only_navigate_click_fill_snapshot_are_allowed(action):
    step = RecipeStep(seq=2, action=action, selector="#userId", risk="READ")
    with pytest.raises(BLOCKED) as exc:
        manual.validate_manifest(_with_step(step))
    assert exc.value.code.startswith("financial_action_forbidden")


def test_B1_allowed_actions_pass_and_verify_must_be_snapshot():
    assert manual.validate_manifest(_recipe()) == (1, 3, 2)
    recipe = _recipe()
    recipe.steps[5] = RecipeStep(seq=6, action="snapshot", selector="#acctList")
    assert manual.validate_manifest(recipe) == (1, 3, 2)
    recipe = _recipe()
    recipe.verify[0] = RecipeStep(seq=7, action="click", selector="#acctList")
    with pytest.raises(BLOCKED):
        manual.validate_manifest(recipe)


@pytest.mark.parametrize("field,value", [
    ("selector", "#btnTrnsfr"), ("selector", "#remitOk"), ("selector", "button.btn-transfer"),
    ("value", "이체 실행"), ("url", "https://bizbank.shinhan.com/송금/입력"),
    ("url", "https://bizbank.shinhan.com/pay/confirm"), ("description", "결제 진행"),
    ("description", "계좌 변경 확인"), ("wait_for", "#sendMoneyDone"),
])
def test_B2_transfer_identifiers_are_rejected_in_every_field(field, value):
    base = {"action": "click", "selector": "#ok", "risk": "READ"}
    base[field] = value
    spec = _spec()
    spec["steps"][3] = base
    with pytest.raises(BLOCKED) as exc:
        manual.validate_manifest(parse_recipe(spec))
    assert exc.value.code.startswith("financial_transfer_identifier")


def test_B2_benign_identifiers_pass_and_error_does_not_echo_values():
    spec = _spec()
    spec["steps"][3]["selector"] = "#display-paywall-free"  # 'pay' 가 단어 안에 있어도 식별자가 아니다
    assert manual.validate_manifest(parse_recipe(spec)) == (1, 3, 2)
    spec["steps"][3]["selector"] = "#btnTrnsfr"
    with pytest.raises(BLOCKED) as exc:
        manual.validate_manifest(parse_recipe(spec))
    assert "btnTrnsfr" not in str(exc.value)


def test_B_literal_fill_template_url_and_foreign_host_are_rejected():
    spec = _spec()
    spec["steps"][1]["value"] = "plain-literal"
    with pytest.raises(BLOCKED, match="financial_literal_fill_value"):
        manual.validate_manifest(parse_recipe(spec))
    spec = _spec()
    spec["steps"][0]["url"] = "https://evil.io/login"
    spec["domain"] = "bizbank.shinhan.com"
    with pytest.raises(BLOCKED, match="financial_url_host_forbidden"):
        manual.validate_manifest(parse_recipe(spec))
    spec = _spec(inputs=[{"name": "vault_username", "secret": True}, {"name": "vault_password", "secret": True},
                         {"name": "base"}])
    spec["steps"][4]["url"] = "{{base}}/acct"
    with pytest.raises(BLOCKED, match="financial_url_template_forbidden"):
        manual.validate_manifest(parse_recipe(spec))


def test_B_irreversible_risk_is_rejected():
    spec = _spec()
    spec["steps"][3]["risk"] = "IRREVERSIBLE"
    with pytest.raises(BLOCKED, match="financial_risk_forbidden"):
        manual.validate_manifest(parse_recipe(spec))


@pytest.mark.parametrize("counts", [None, [1, 3], [1, 3, 2, 0], [0, 3, 3], [1, 3, 3], ["1", 3, 2], [True, 3, 2]])
def test_manifest_shape_is_enforced(counts):
    spec = _spec()
    if counts is None:
        spec["metadata"] = {"lane": "pc_agent"}
    else:
        spec["metadata"] = {"shinhan_manual": {"stage_step_counts": counts}}
    with pytest.raises(BLOCKED):
        manual.validate_manifest(parse_recipe(spec))


def test_E4_validate_manifest_returns_tuple_or_none_without_type_ignore():
    source = (ROOT / "app/services/work_recipe/shinhan_corporate_manual.py").read_text(encoding="utf-8")
    assert "type: ignore" not in source
    assert "def validate_manifest(recipe: WorkRecipe) -> tuple[int, int, int] | None:" in source
    assert manual.validate_manifest(_recipe()) == (1, 3, 2)
    assert manual.validate_manifest(_template_recipe()) is None


# ===================================================== C. 단계 영수증 · 증거 체인


class _Inner:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.verify_output: dict | None = {"verified": True, "screenshot_path": "/shots/ok.png"}
        self.fail = False

    async def __call__(self, payload):
        self.calls.append(payload)
        if self.fail:
            return {"ok": False, "error": "boom"}
        if payload.get("phase") == "verify":
            return {"ok": True, "output": self.verify_output}
        return {"ok": True}


@pytest.fixture
def rows(monkeypatch):
    captured: list[dict] = []
    original = inspect.signature(audit.record_step)

    async def record(*args, **kwargs):
        bound = original.bind(*args, **kwargs)
        bound.apply_defaults()
        captured.append(dict(bound.arguments))

    monkeypatch.setattr(audit, "record_step", record)
    return captured


def _executor(inner=None, receipts=None, verify_total=1):
    inner = inner or _Inner()
    ledger = manual.StageReceiptLedger((1, 3, 2), receipts, run_id="run-1", verify_total=verify_total)
    return manual.ShinhanStageExecutor(inner, ledger), inner, ledger


def _step(seq: int, action: str = "click", **extra: Any) -> dict[str, Any]:
    return {"phase": "step", "seq": seq, "action": action, "selector": "#x", **extra}


def _verify(seq: int = 7) -> dict[str, Any]:
    return {"phase": "verify", "seq": seq, "action": "snapshot", "assertion": "element_visible", "selector": "#a"}


async def _drive_steps(executor, upto: int) -> None:
    for seq in range(1, upto + 1):
        result = await executor(_step(seq))
        assert result["ok"] is True, (seq, result)


def _blocked_code(result: dict) -> str:
    assert result["ok"] is False and result["status"] == "blocked"
    return result["error"].removeprefix("financial_manual_blocked:")


async def test_C3_receipts_chain_one_two_three_on_success_path(rows):
    executor, _, ledger = _executor()
    await _drive_steps(executor, 6)
    assert [(r["stage_index"], r["status"]) for r in ledger.receipts] == [(0, "done"), (1, "done"), (2, "open")]
    assert (await executor(_verify()))["ok"] is True
    assert [r["status"] for r in ledger.receipts] == ["done", "done", "done"]
    manual.verify_chain(ledger.receipts, (1, 3, 2))
    assert [r["prev_hash"] for r in ledger.receipts[1:]] == [r["hash"] for r in ledger.receipts[:2]]
    assert ledger.receipts[2]["markers"][-1] == "verified"


@pytest.mark.parametrize("tamper", [
    lambda rs: rs[1].__setitem__("hash", "f" * 64),
    lambda rs: rs[1].__setitem__("prev_hash", "e" * 64),
    lambda rs: rs[0].__setitem__("last_seq", 99),
    lambda rs: rs[0].__setitem__("markers", ["open", "completed", "verified"]),
    lambda rs: rs.__delitem__(0),
    lambda rs: rs.reverse(),
    lambda rs: rs[0].__setitem__("status", "open"),
    lambda rs: rs[1].__setitem__("stage_index", 0),
])
async def test_C3_forged_or_missing_receipts_are_rejected(rows, tamper):
    executor, _, ledger = _executor()
    await _drive_steps(executor, 4)  # 1,2 단계 영수증이 생기고 3 단계가 열린다
    receipts = copy.deepcopy(ledger.receipts[:2])
    tamper(receipts)
    forged, inner, _ = _executor(receipts=receipts)
    result = await forged(_step(5))
    assert _blocked_code(result).startswith(("receipt_", "step_seq_not_next", "stage_"))
    assert inner.calls == []


async def test_C3_payload_supplied_receipts_are_not_trusted(rows):
    executor, inner, _ = _executor()
    forged = {"receipts": [{"stage_index": 0, "status": "done"}], "stage_receipt": {"status": "done"}}
    result = await executor(_step(3, **forged))
    assert _blocked_code(result) == "step_seq_not_next" and inner.calls == []


async def test_C2_finished_stage_cannot_be_replayed_and_seq_is_monotonic(rows):
    executor, inner, ledger = _executor()
    await _drive_steps(executor, 3)
    count = len(inner.calls)
    assert _blocked_code(await executor(_step(1))) == "step_seq_not_next"   # 영수증이 끝난 1 단계
    assert _blocked_code(await executor(_step(3))) == "step_seq_not_next"   # 같은 seq 재실행
    assert _blocked_code(await executor(_step(5))) == "step_seq_not_next"   # 건너뛰기
    assert len(inner.calls) == count
    assert (await executor(_step(4)))["ok"] is True
    assert ledger.receipts[1]["status"] == "done"
    assert _blocked_code(await executor(_step(4))) == "step_seq_not_next"


def test_C2_ledger_stage_window_is_len_minus_one_or_len():
    ledger = manual.StageReceiptLedger((1, 3, 2))
    assert ledger.admit_step(1) == 0
    first = manual.make_receipt(0, "done", first_seq=1, last_seq=1, markers=["open", "completed"],
                                prev_hash=manual.GENESIS_HASH)
    ledger = manual.StageReceiptLedger((1, 3, 2), [first])
    assert ledger.admit_step(2) == 1                       # len(receipts) → 새 단계 진입
    mid = manual.make_receipt(1, "open", first_seq=2, last_seq=2, markers=["open"], prev_hash=first["hash"])
    ledger = manual.StageReceiptLedger((1, 3, 2), [first, mid])
    assert ledger.admit_step(3) == 1                       # len(receipts)-1 → 진행 중인 단계
    with pytest.raises(BLOCKED):                           # 영수증이 끝난 이전 단계는 거부
        manual.StageReceiptLedger((1, 3, 2), [first, mid]).admit_step(1)


async def test_C2_open_stage_cannot_be_skipped_to_next_stage(rows):
    executor, inner, _ = _executor()
    await _drive_steps(executor, 2)
    assert _blocked_code(await executor(_step(5))) == "step_seq_not_next"
    assert len(inner.calls) == 2


async def test_C1_verify_branch_runs_before_seq_comparison(rows):
    executor, inner, ledger = _executor()
    await _drive_steps(executor, 6)
    # verify seq(2) 가 본 단계 seq 와 겹쳐도 단계 영수증 순서에 걸리지 않는다.
    result = await executor(_verify(seq=2))
    assert result["ok"] is True and inner.calls[-1]["phase"] == "verify"
    assert ledger.receipts[-1]["status"] == "done"


async def test_C1_verify_before_steps_complete_and_step_after_completion_are_blocked(rows):
    executor, inner, ledger = _executor()
    await _drive_steps(executor, 3)
    assert _blocked_code(await executor(_verify(seq=2))) == "verify_before_steps_complete"
    await _drive_steps_from(executor, 4, 6)
    assert (await executor(_verify()))["ok"] is True
    assert _blocked_code(await executor(_verify())) == "verify_before_steps_complete"
    assert _blocked_code(await executor(_step(7))) == "step_outside_manifest"


async def _drive_steps_from(executor, start: int, end: int) -> None:
    for seq in range(start, end + 1):
        assert (await executor(_step(seq)))["ok"] is True


async def test_C5_open_marker_is_not_success_evidence_final_stage_needs_verified(rows):
    executor, inner, ledger = _executor()
    await _drive_steps(executor, 6)
    assert ledger.receipts[2]["status"] == "open" and ledger.receipts[2]["markers"] == ["open"]
    inner.verify_output = {"verified": True}            # 화면 증거(스크린샷) 없음
    assert (await executor(_verify()))["error"] == "financial_verify_evidence_required"
    assert ledger.receipts[2]["status"] == "open"
    inner.verify_output = {"verified": False, "screenshot_path": "/shots/x.png"}
    assert (await executor(_verify()))["ok"] is False
    assert ledger.receipts[2]["status"] == "open"


def test_C5_module_documents_self_reported_receipts():
    source = (ROOT / "app/services/work_recipe/shinhan_corporate_manual.py").read_text(encoding="utf-8")
    assert "자기보고" in source and "변조를 막지 못하고" in source and "실행 권한도 아니다" in source
    assert "``open`` 마커는 비로그인 화면에서도 충족" in source
    assert all(r.get("self_reported") is True for r in [manual.make_receipt(
        0, "open", first_seq=1, last_seq=1, markers=["open"], prev_hash=manual.GENESIS_HASH)])


async def test_C_failed_inner_step_issues_no_receipt(rows):
    inner = _Inner()
    inner.fail = True
    executor, _, ledger = _executor(inner)
    assert (await executor(_step(1)))["ok"] is False
    assert ledger.receipts == [] and rows == []


async def test_C4_receipts_are_persisted_through_audit_record_step(rows):
    executor, _, ledger = _executor()
    await _drive_steps(executor, 6)
    await executor(_verify())
    # 1·2 단계는 open+done, 3 단계는 open+done(verify 후) = 6 행
    assert len(rows) == 6
    assert {r["phase"] for r in rows} == {manual.RECEIPT_PHASE}
    assert all(r["run_id"] == "run-1" and r["action"] == "snapshot" for r in rows)
    persisted = [r["result"]["stage_receipt"] for r in rows]
    assert [(p["stage_index"], p["status"]) for p in persisted] == [
        (0, "open"), (0, "done"), (1, "open"), (1, "done"), (2, "open"), (2, "done")]
    assert all(len(p["hash"]) == 16 and len(p["prev_hash"]) == 16 for p in persisted)
    # audit 마스킹을 통과해도 해시 접두가 지워지지 않는다.
    assert all(audit.mask_audit_payload(r["result"])["stage_receipt"]["hash"] == r["result"]["stage_receipt"]["hash"]
               for r in rows)


async def test_C4_persist_failure_fails_closed(monkeypatch):
    async def broken(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(audit, "record_step", broken)
    executor, _, ledger = _executor()
    result = await executor(_step(1))
    assert _blocked_code(result) == "receipt_persist_failed"


def test_C4_audit_and_recorder_signatures_are_unchanged():
    params = list(inspect.signature(audit.record_step).parameters)
    assert params == ["run_id", "seq", "action", "url", "risk_level", "result", "error", "screenshot_path",
                      "approval_id", "phase", "selector", "attempts", "duration_ms", "llm_calls"]
    assert list(inspect.signature(audit.GuardedRunRecorder.record_step).parameters) == ["self", "run_id", "step"]


async def test_B2_runtime_payload_is_checked_too(rows):
    executor, inner, _ = _executor()
    assert _blocked_code(await executor(_step(1, "click", selector="#btnTrnsfr"))).startswith(
        "financial_transfer_identifier")
    assert _blocked_code(await executor(_step(1, "evaluate"))).startswith("financial_action_forbidden")
    assert _blocked_code(await executor(_step(1, "navigate", url="https://shinhan.com.evil.io/"))) == (
        "financial_url_host_forbidden")
    assert _blocked_code(await executor({**_step(1), "phase": "other"})) == "unknown_phase"
    assert inner.calls == []


# =========================================================== D. 승인·실행 경로


def test_D1_no_caller_supplied_approval_hook_exists():
    params = set(inspect.signature(manual.run_financial_recipe).parameters)
    assert not {p for p in params if re.search(r"approv|status|verify|callback|check", p)}
    assert not {p for p in inspect.signature(orchestrator.run_directive).parameters if "approv" in p}


async def test_D1_forged_callback_and_forged_status_are_ignored(db, wired):
    recipe = _recipe()
    recipe.metadata.update({"status": "approved", "approved": True})
    db.conn = _Conn(row=None)  # DB 에는 승인된 행이 없다
    with pytest.raises(TypeError):
        await manual.run_financial_recipe(recipe, **_run_args(recipe), approval_check=lambda *_: True)
    with pytest.raises(BLOCKED) as exc:
        await manual.run_financial_recipe(recipe, **_run_args(recipe))
    assert exc.value.code == "recipe_not_approved" and wired.vault == []


@pytest.mark.parametrize("case", ["disabled", "tenant", "spec_changed", "spec_unreadable"])
async def test_D1_server_side_rows_that_do_not_prove_approval_block(db, wired, case):
    recipe = _recipe()
    if case == "disabled":
        row = _db_row(recipe, enabled=False)
    elif case == "tenant":
        row = _db_row(recipe, tenant=OTHER_TENANT)
    elif case == "spec_changed":
        row = _db_row(recipe, mutate=lambda s: s["steps"][3].update(selector="#otherLogin"))
    else:
        row = {**_db_row(recipe), "spec": "not json"}
    db.conn = _Conn(row=row)
    with pytest.raises(BLOCKED):
        await manual.run_financial_recipe(recipe, **_run_args(recipe))
    assert wired.vault == [] and wired.browser == []


async def test_D2_real_db_row_shape_matches_despite_non_execution_fields(db, wired):
    recipe = _recipe()
    assert recipe.version == 1
    db.conn = _Conn(row=_db_row(recipe, version=7))   # version·description·yaml·id 는 비교하지 않는다
    result = await manual.run_financial_recipe(recipe, **_run_args(recipe))
    assert isinstance(result, RunResult) and result.status == "success"


def test_D2_fingerprint_ignores_non_execution_fields_but_sees_execution_ones():
    a, b = _recipe(), _recipe()
    b.version, b.description = 9, "다른 설명"
    b.steps[0].description = "다른 단계 설명"
    assert manual.execution_fingerprint(a) == manual.execution_fingerprint(b)
    b.steps[3].selector = "#other"
    assert manual.execution_fingerprint(a) != manual.execution_fingerprint(b)
    c = _recipe()
    c.metadata["shinhan_manual"] = {"stage_step_counts": [2, 2, 2]}
    assert manual.execution_fingerprint(a) != manual.execution_fingerprint(c)


async def test_D3_lock_then_approval_then_vault_in_that_order(monkeypatch, wired):
    order: list[str] = []

    class Lock:
        def __init__(self, tenant_id) -> None:
            pass

        async def __aenter__(self):
            order.append("lock")
            return self

        async def __aexit__(self, *_):
            order.append("unlock")
            return False

    async def approved(recipe, tenant_id):
        order.append("approval")

    async def scoped(recipe, inputs):
        order.append("vault")
        return dict(inputs)

    monkeypatch.setattr(manual, "FinancialExecutionLock", Lock)
    monkeypatch.setattr(manual, "assert_server_approved", approved)
    monkeypatch.setattr(orchestrator, "_scoped_inputs", scoped)
    result = await manual.run_financial_recipe(_recipe(), **_run_args(_recipe()))
    assert result.status == "success"
    assert order == ["lock", "approval", "vault", "unlock"]


async def test_D3_lock_failure_means_zero_vault_access(db, wired):
    db.conn = _Conn(row=_db_row(_recipe()), lock=False)
    with pytest.raises(BLOCKED) as exc:
        await manual.run_financial_recipe(_recipe(), **_run_args(_recipe()))
    assert exc.value.code == "financial_execution_locked"
    assert wired.vault == [] and wired.browser == []
    assert not any("work_recipes" in q for q in db.conn.queries)   # 승인 조회도 하지 않았다
    assert manual.FinancialExecutionLock._held == set()


async def test_D3_lock_is_exclusive_in_process_and_released_after_run(db, wired):
    db.conn = _Conn(row=_db_row(_recipe()))
    async with manual.FinancialExecutionLock(TENANT):
        with pytest.raises(BLOCKED, match="financial_execution_locked"):
            async with manual.FinancialExecutionLock(TENANT):
                pass
        async with manual.FinancialExecutionLock(OTHER_TENANT):
            pass
    assert any("pg_advisory_unlock" in q for q in db.conn.queries)
    await manual.run_financial_recipe(_recipe(), **_run_args(_recipe()))
    assert manual.FinancialExecutionLock._held == set()


async def test_D3_lock_backend_error_is_fail_closed(monkeypatch, wired):
    def broken():
        raise RuntimeError("pool closed")

    monkeypatch.setattr(manual, "get_pool", broken)
    with pytest.raises(BLOCKED, match="financial_lock_unavailable"):
        await manual.run_financial_recipe(_recipe(), **_run_args(_recipe()))
    assert wired.vault == [] and manual.FinancialExecutionLock._held == set()


async def test_D3_approval_failure_means_zero_vault_access(db, wired):
    db.conn = _Conn(row=None)
    with pytest.raises(BLOCKED, match="recipe_not_approved"):
        await manual.run_financial_recipe(_recipe(), **_run_args(_recipe()))
    assert wired.vault == []
    assert manual.FinancialExecutionLock._held == set()


async def test_D4_browser_work_key_stays_bound_on_financial_path(db, wired):
    db.conn = _Conn(row=_db_row(_recipe()))
    await manual.run_financial_recipe(_recipe(), **_run_args(_recipe()))
    assert wired.browser == [{"browser_session_id": "bs-1", "browser_work_key": "wk-1"}]
    for override in ({"browser_work_key": None}, {"browser_work_key": ""}, {"browser_session_id": None}):
        with pytest.raises(BLOCKED, match="browser_work_key_required"):
            await manual.run_financial_recipe(_recipe(), **_run_args(_recipe(), **override))
    bound = _recipe()
    bound.metadata["work_key"] = "wk-registered"
    with pytest.raises(BLOCKED, match="browser_work_key_mismatch"):
        await manual.run_financial_recipe(bound, **_run_args(bound))


@pytest.mark.parametrize("intent", [
    None,
    SimpleNamespace(authenticated_provenance=None, payload={}, tenant_id=TENANT),
    SimpleNamespace(authenticated_provenance={}, payload={}, tenant_id=TENANT),
    SimpleNamespace(authenticated_provenance="forged", payload={}, tenant_id=TENANT),
    SimpleNamespace(authenticated_provenance={"issuer": "x"}, payload=None, tenant_id=TENANT),
])
async def test_D5_missing_intent_or_provenance_fails_closed_not_attribute_error(db, wired, intent):
    with pytest.raises(BLOCKED) as exc:
        await manual.run_financial_recipe(_recipe(), **_run_args(_recipe(), action_intent=intent))
    assert exc.value.code == "action_intent_provenance_required"
    assert wired.vault == []


async def test_D5_tenant_mismatch_blocks(wired):
    intent = SimpleNamespace(authenticated_provenance={"issuer": "x"}, payload={}, tenant_id=OTHER_TENANT)
    with pytest.raises(BLOCKED, match="action_intent_tenant_mismatch"):
        await manual.run_financial_recipe(_recipe(), **_run_args(_recipe(), action_intent=intent))


async def test_financial_run_end_to_end_persists_chain_and_never_retries(db, wired):
    db.conn = _Conn(row=_db_row(_recipe()))
    result = await manual.run_financial_recipe(_recipe(), **_run_args(_recipe()))
    assert result.status == "success"
    assert [row[1]["phase"] for row in wired.audit_rows] == [manual.RECEIPT_PHASE] * 6
    assert all(row[0][0] == "11111111-1111-1111-1111-111111111111" for row in wired.audit_rows)


async def test_financial_run_stops_when_final_screen_evidence_is_missing(db, wired):
    db.conn = _Conn(row=_db_row(_recipe()))
    wired.verify_ok = False
    result = await manual.run_financial_recipe(_recipe(), **_run_args(_recipe()))
    assert result.status == "blocked" and "financial_verify_evidence_required" in result.error
    assert [r[1]["result"]["stage_receipt"]["status"] for r in wired.audit_rows][-1] == "open"


async def test_financial_run_through_run_directive_uses_retries_zero(db, wired, monkeypatch):
    recipe = _recipe()
    db.conn = _Conn(row=_db_row(recipe))
    seen: dict[str, Any] = {}
    real_play = orchestrator.play_recipe

    async def spy(recipe_, executor, inputs, **kwargs):
        seen.update(kwargs)
        return await real_play(recipe_, executor, inputs, **kwargs)

    async def recipes(**kwargs):
        return [{"name": recipe.name, "domain": recipe.domain, "spec": recipe.to_dict()}]

    monkeypatch.setattr(orchestrator, "list_recipes", recipes)
    monkeypatch.setattr(orchestrator, "play_recipe", spy)
    intent = _intent(inputs={"vault_username": "vault-ref-user", "vault_password": "vault-ref-pass"})
    result = await orchestrator.run_directive(
        "bizbank.shinhan.com 간편계좌 확인", TENANT, browser_session_id="bs-1", browser_work_key="wk-1",
        triggered_by="user@example.com", action_intent=intent)
    assert result.status == "success"
    assert seen["retries"] == 0 and seen["max_risk"] == "IRREVERSIBLE"
    assert seen["context"]["smart_browser"]["server_access_blocked"] is True


async def test_financial_recipe_without_work_key_is_blocked_at_run_directive(db, wired, monkeypatch):
    recipe = _recipe()

    async def recipes(**kwargs):
        return [{"name": recipe.name, "domain": recipe.domain, "spec": recipe.to_dict()}]

    monkeypatch.setattr(orchestrator, "list_recipes", recipes)
    with pytest.raises(BLOCKED, match="browser_work_key_required"):
        await orchestrator.run_directive(
            "bizbank.shinhan.com 간편계좌 확인", TENANT, action_intent=_intent())
    assert wired.vault == []


# ==================================================================== E. 보존


class _TxConn:
    """decide_registration 이 쓰는 질의에 SQL 조각으로 응답하는 가짜 연결."""

    def __init__(self, request_row: dict[str, Any]) -> None:
        self.request_row = request_row
        self.inserted: list[Any] = []
        self.sql: list[str] = []

    def transaction(self):
        return _Acquire(self)

    async def fetchrow(self, query, *args):
        self.sql.append(query)
        if "FOR UPDATE" in query:
            return self.request_row
        if "INSERT INTO work_recipes" in query:
            self.inserted.append(args)
            return {"id": uuid.uuid4(), "tenant_id": args[0], "name": args[1], "domain": args[2],
                    "version": args[3], "spec": args[5], "enabled": True}
        if "UPDATE work_recipe_registration_requests" in query:
            status = "rejected" if "status='rejected'" in query else "approved"
            return {**self.request_row, "status": status, "recipe_id": None}
        raise AssertionError(query)

    async def fetchval(self, query, *args):
        return 1

    async def execute(self, query, *args):
        self.sql.append(query)


def _request_row(spec: dict[str, Any], domain: str) -> dict[str, Any]:
    return {"id": uuid.uuid4(), "tenant_id": uuid.UUID(TENANT), "status": "pending", "name": spec["name"],
            "domain": domain, "spec": json.dumps(spec), "requested_by": "recorder", "recipe_id": None}


@pytest.fixture
def decide(monkeypatch):
    holder = SimpleNamespace(conn=None)
    monkeypatch.setattr(registration, "get_pool", lambda: _Pool(holder.conn))

    async def sync(**kwargs):
        return {"ref": "stub"}

    import app.services.ovis_recipe as ovis

    monkeypatch.setattr(ovis, "sync_legacy_reference", sync)

    async def go(spec: dict[str, Any], verdict: str):
        holder.conn = _TxConn(_request_row(spec, store.normalize_domain(spec["domain"])))
        result = await registration.decide_registration(
            holder.conn.request_row["id"], tenant_id=TENANT, decision=verdict, decided_by="ceo")
        return result, holder.conn

    return go


async def test_E1_reject_legacy_pending_shinhan_draft_without_manifest(decide):
    spec = _spec()
    spec["metadata"] = {"recorded": True}   # manifest 없는 기존 신한 draft
    result, conn = await decide(spec, "reject")
    assert result["status"] == "rejected" and conn.inserted == []


async def test_E1_approve_without_manifest_is_refused_as_registration_error(decide):
    spec = _spec()
    spec["metadata"] = {"recorded": True}
    with pytest.raises(RegistrationError, match="financial_manifest_invalid:financial_manifest_missing_or_invalid"):
        await decide(spec, "approve")


async def test_E1_approve_valid_shinhan_manifest_and_nonbank_template_recipe(decide):
    result, conn = await decide(_spec(), "approve")
    assert result["status"] == "approved" and len(conn.inserted) == 1
    template = _template_recipe().to_dict()
    result, conn = await decide(template, "approve")
    assert result["status"] == "approved" and len(conn.inserted) == 1
    stored = json.loads(conn.inserted[0][5])
    assert stored["steps"][0]["url"] == "{{base}}/orders" and stored["version"] == 1


def test_E1_validate_manifest_is_called_only_in_approve_branch():
    source = inspect.getsource(registration.decide_registration)
    approve_part = source.split("return _row(updated)", 1)[1]
    assert "validate_manifest" not in source.split("return _row(updated)", 1)[0]
    assert "validate_manifest" in approve_part
    assert "validate_manifest" not in inspect.getsource(registration.request_registration)


async def test_E2_action_intent_inputs_invalid_stays_value_error(monkeypatch):
    async def recipes(**kwargs):
        return []

    monkeypatch.setattr(orchestrator, "list_recipes", recipes)
    intent = _intent("x", inputs="not-a-mapping")
    with pytest.raises(ValueError, match="action_intent_inputs_invalid") as exc:
        await orchestrator.run_directive("x", TENANT, action_intent=intent)
    assert type(exc.value) is ValueError
    assert 'raise ValueError("action_intent_inputs_invalid")' in (
        ROOT / "app/services/work_recipe/orchestrator.py").read_text(encoding="utf-8")


@pytest.fixture
def played(monkeypatch):
    seen: dict[str, Any] = {}
    sentinel = object()

    async def play(recipe, executor, inputs, **kwargs):
        seen.update(recipe=recipe, executor=executor, inputs=inputs, kwargs=kwargs)
        return SimpleNamespace(to_dict=lambda: {"status": "success"})

    async def scoped(recipe, inputs):
        return dict(inputs)

    monkeypatch.setattr(orchestrator, "play_recipe", play)
    monkeypatch.setattr(orchestrator, "_scoped_inputs", scoped)
    monkeypatch.setattr(orchestrator, "GuardedRunRecorder", lambda **kw: "recorder")
    monkeypatch.setattr(orchestrator, "BrowserRecipeExecutor", lambda **kw: sentinel)
    seen["sentinel"] = sentinel
    return seen


async def test_E3_and_regression_nonfinancial_template_recipe_runs_exactly_as_before(played, monkeypatch):
    async def recipes(**kwargs):
        return [{"name": "templated_lookup", "domain": "shop.example.com", "spec": _template_recipe().to_dict()}]

    monkeypatch.setattr(orchestrator, "list_recipes", recipes)
    intent = _intent("templated_lookup 실행", inputs={"base": "https://shop.example.com"})
    result = await orchestrator.run_directive("templated_lookup 실행", TENANT, action_intent=intent)
    assert result.to_dict() == {"status": "success"}
    assert set(played["kwargs"]) == {"recorder", "max_risk", "context"}      # retries 를 넘기지 않는다
    assert played["executor"] is played["sentinel"]                          # 실행기를 감싸지 않는다
    assert played["kwargs"]["max_risk"] == "IRREVERSIBLE"
    assert played["kwargs"]["context"] == {
        "domain": "shop.example.com",
        "smart_browser": {"native_auth_required": False, "requires_local_security_programs": False,
                          "server_access_blocked": False, "requires_residential_ip": False},
    }
    assert played["inputs"] == {"base": "https://shop.example.com"}
    from app.services.work_recipe.player import DEFAULT_RETRIES
    assert DEFAULT_RETRIES == 2


def test_E3_orchestrator_play_call_passes_no_retries_for_generic_path():
    source = inspect.getsource(orchestrator.run_directive)
    generic_call = source.split("return await play_recipe(", 1)[1]
    assert "retries" not in generic_call


def test_regression_nonbank_registration_and_hosts_are_untouched():
    recipe = _template_recipe()
    assert manual.validate_manifest(recipe) is None
    assert not manual.is_shinhan_recipe(recipe)
    for host in ("coupangeats.com", "aads.newtalk.kr", "shop.example.com"):
        assert not manual.is_shinhan_url(f"https://{host}/x")


def test_orchestrator_and_registration_diffs_are_additive_hooks_only():
    import subprocess

    diff = subprocess.run(
        ["git", "diff", "--no-color", "-U0", "HEAD", "--",
         "app/services/work_recipe/orchestrator.py", "app/services/work_recipe/registration.py"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    ).stdout
    removed = [ln for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---")]
    assert removed == []


def test_E5_existing_comments_are_preserved():
    source = (ROOT / "app/services/work_recipe/orchestrator.py").read_text(encoding="utf-8")
    assert "a chat directive cannot promote itself there." in source
    assert "native_auth_required 를 대신 켜서 우회하지 않는다." in source


def test_E6_t4b_symbols_are_intact():
    for name in ("RecipeMatch", "RecipeConfirmation", "resolve_recipe_match", "resolve_recipe", "_confirmation_match"):
        assert hasattr(orchestrator, name), name
    assert "confirmed_draft_id" in inspect.getsource(orchestrator.run_directive)
    fields = set(orchestrator.RecipeMatch.__dataclass_fields__)
    assert {"recipe", "needs_confirmation", "draft_id", "draft_title", "matched_phrase",
            "matched_draft_ids"} <= fields


# ============================================================ R9. 금융 실행 경계 교정


def _save_as_recipe(name: str = "saved_target_nav") -> WorkRecipe:
    return parse_recipe({
        "name": name, "domain": "shop.example.com",
        "steps": [{"action": "snapshot", "selector": "#next", "save_as": "target"},
                  {"action": "navigate", "url": "{{target}}"}],
    })


@pytest.fixture
def generic_path(monkeypatch):
    """진짜 BrowserRecipeExecutor + 진짜 play_recipe. 브라우저 접점(_attempt)만 가짜로 바꾼다."""
    log = SimpleNamespace(attempted=[], target="https://bizbank.shinhan.com/login")

    async def attempt(self, action, payload):
        log.attempted.append((action, payload.get("url")))
        if action == "snapshot":
            return {"ok": True, "output": log.target, "route": "browser_agent"}
        return {"ok": True, "output": {"url": payload.get("url")}, "route": "browser_agent"}

    async def scoped(recipe, inputs):
        return dict(inputs)

    async def recipes(**kwargs):
        recipe = _save_as_recipe()
        return [{"name": recipe.name, "domain": recipe.domain, "spec": recipe.to_dict()}]

    monkeypatch.setattr(executor_module, "get_browser_bridge_service", lambda: object())
    monkeypatch.setattr(executor_module.BrowserRecipeExecutor, "_attempt", attempt)
    monkeypatch.setattr(orchestrator, "list_recipes", recipes)
    monkeypatch.setattr(orchestrator, "_scoped_inputs", scoped)
    monkeypatch.setattr(orchestrator, "GuardedRunRecorder", _FakeRecorder)
    return log


async def _run_generic():
    return await orchestrator.run_directive(
        "saved_target_nav 실행", TENANT, action_intent=_intent("saved_target_nav 실행"))


async def test_R9_1_save_as_made_shinhan_url_is_blocked_at_execution_boundary(generic_path):
    recipe = _save_as_recipe()
    manual.assert_unregistered_runtime_host(recipe, {})          # 사전 검사는 값을 모르므로 통과한다
    result = await _run_generic()
    assert result.status == "blocked"
    assert "shinhan_host_unregistered_recipe" in result.error
    assert generic_path.attempted == [("snapshot", None)]        # navigate 는 실행되지 않았다


async def test_R9_1_nonbank_save_as_recipe_still_passes(generic_path):
    generic_path.target = "https://shop.example.com/orders"
    result = await _run_generic()
    assert result.status == "success"
    assert generic_path.attempted == [("snapshot", None), ("navigate", "https://shop.example.com/orders")]


@pytest.mark.parametrize("target", [
    "https://bizbank.shinhan.com/login", "https://BIZBANK.Shinhan.com./x",
    "https://bizbank.shin\nhan.com/x", "https://bizbank.shinhan\t.com/x", "https:\\\\bizbank.shinhan.com\\x",
])
def test_R9_1_boundary_blocks_disguised_shinhan_targets_for_unregistered_recipes(target):
    payload = {"action": "navigate", "url": target, "context": {"domain": "shop.example.com"}}
    with pytest.raises(BLOCKED) as exc:
        manual.assert_unregistered_navigation(payload)
    assert exc.value.code == "shinhan_host_unregistered_recipe"
    with pytest.raises(BLOCKED):
        manual.assert_unregistered_navigation({"action": "navigate", "url": target})   # domain 없음도 막는다
    manual.assert_unregistered_navigation({**payload, "context": {"domain": "bizbank.shinhan.com"}})


def test_R9_1_boundary_ignores_other_actions_and_foreign_hosts():
    ctx = {"domain": "shop.example.com"}
    manual.assert_unregistered_navigation({"action": "navigate", "url": "https://notshinhan.com/", "context": ctx})
    manual.assert_unregistered_navigation({"action": "click", "url": "https://bizbank.shinhan.com/", "context": ctx})
    with pytest.raises(BLOCKED):
        manual.assert_unregistered_navigation(
            {"action": "api_call", "endpoint": "https://bizbank.shinhan.com/api", "context": ctx})


async def test_R9_1_executor_hook_blocks_before_any_browser_work(monkeypatch):
    called: list[str] = []

    async def attempt(self, action, payload):
        called.append(action)
        return {"ok": True}

    monkeypatch.setattr(executor_module, "get_browser_bridge_service", lambda: object())
    monkeypatch.setattr(executor_module.BrowserRecipeExecutor, "_attempt", attempt)
    browser = executor_module.BrowserRecipeExecutor()
    result = await browser({"action": "navigate", "url": "https://bizbank.shinhan.com/",
                            "context": {"domain": "shop.example.com"}})
    assert result["ok"] is False and result["status"] == "blocked"
    assert result["error"] == "financial_manual_blocked:shinhan_host_unregistered_recipe"
    assert called == []
    assert (await browser({"action": "navigate", "url": "https://shop.example.com/",
                           "context": {"domain": "shop.example.com"}}))["ok"] is True
    assert called == ["navigate"]


def test_R9_1_precheck_fails_closed_on_unrenderable_template_urls():
    for inputs in ({}, {"base": None}):
        with pytest.raises(BLOCKED, match="shinhan_host_unverifiable"):
            manual.assert_unregistered_runtime_host(_template_recipe(), inputs)
    plain = parse_recipe({"name": "plain", "domain": "shop.example.com",
                          "inputs": [{"name": "q"}],
                          "steps": [{"action": "navigate", "url": "https://shop.example.com/"},
                                    {"action": "fill", "selector": "#q", "value": "{{q}}"}]})
    manual.assert_unregistered_runtime_host(plain, {})    # URL 에 템플릿이 없으면 입력이 없어도 관여하지 않는다


# ---- 2. 세션 바인딩


async def _blocked_run(wired, code: str, **override):
    recipe = _recipe()
    with pytest.raises(BLOCKED) as exc:
        await manual.run_financial_recipe(recipe, **_run_args(recipe, **override))
    assert exc.value.code == code
    assert wired.vault == [] and wired.browser == [] and wired.payloads == []


async def test_R9_2_adapter_ignores_work_key_when_session_id_is_given(monkeypatch):
    """근거: aads_adapter.acquire_browser_context 는 session_id 가 있으면 work_key 를 보지 않는다."""
    from app.browser_bridge import aads_adapter

    seen: list[Any] = []

    class Service:
        async def acquire_playwright_context(self, *, session_id=None):
            seen.append(session_id)
            return object(), None

        async def ensure_work_session(self, **kwargs):
            raise AssertionError("work_key must not be consulted when session_id is given")

    monkeypatch.setattr(aads_adapter, "get_browser_bridge_service", lambda: Service())
    _, error = await aads_adapter.acquire_browser_context(
        browser_session_id="bs-A", browser_work_key="some-other-work", browser_lane="pc")
    assert error is None and seen == ["bs-A"]


async def test_R9_2_unknown_session_is_blocked_with_zero_vault_access(db, wired):
    db.conn = _Conn(row=_db_row(_recipe()))
    await _blocked_run(wired, "browser_session_not_found", browser_session_id="bs-missing")


async def test_R9_2_session_with_other_work_key_is_blocked_even_if_caller_key_matches_recipe(db, wired):
    wired.sessions.register(_session("bs-2", work_key="wk-2"), activate=False)
    db.conn = _Conn(row=_db_row(_recipe()))
    await _blocked_run(wired, "browser_session_work_key_mismatch", browser_session_id="bs-2")
    await _blocked_run(wired, "browser_session_work_key_mismatch", browser_work_key="wk-other")
    wired.sessions.register(_session("bs-nokey", work_key=""), activate=False)
    await _blocked_run(wired, "browser_session_work_key_mismatch", browser_session_id="bs-nokey")


async def test_R9_2_non_local_agent_session_is_blocked(db, wired):
    for kind in (BrowserEndpointKind.CDP, BrowserEndpointKind.HEADLESS):
        wired.sessions.register(_session("bs-x", work_key="wk-1", kind=kind), activate=False)
        db.conn = _Conn(row=_db_row(_recipe()))
        await _blocked_run(wired, "browser_session_not_local", browser_session_id="bs-x")


async def test_R9_2_lookup_error_is_blocked(db, wired, monkeypatch):
    def broken():
        raise RuntimeError("registry down")

    monkeypatch.setattr(manual, "get_browser_bridge_service", broken)
    db.conn = _Conn(row=_db_row(_recipe()))
    await _blocked_run(wired, "browser_session_lookup_failed")


async def test_R9_2_bound_local_session_passes(db, wired):
    db.conn = _Conn(row=_db_row(_recipe()))
    result = await manual.run_financial_recipe(_recipe(), **_run_args(_recipe()))
    assert result.status == "success" and len(wired.vault) == 1


# ---- 3. execution_fingerprint 의 inputs.default


def _with_inputs(*extra: dict[str, Any]) -> WorkRecipe:
    base = [{"name": "vault_username", "secret": True}, {"name": "vault_password", "secret": True}]
    return _recipe(inputs=[*base, *extra])


def test_R9_3_fingerprint_sees_input_defaults():
    a = manual.execution_fingerprint(_with_inputs({"name": "view", "default": "list"}))
    b = manual.execution_fingerprint(_with_inputs({"name": "view", "default": "detail"}))
    c = manual.execution_fingerprint(_with_inputs({"name": "view"}))
    assert a != b and a != c
    assert a == manual.execution_fingerprint(_with_inputs({"name": "view", "default": "list"}))
    assert manual.execution_fingerprint(_with_inputs({"name": "n", "default": {"b": 1, "a": 2}})) == \
        manual.execution_fingerprint(_with_inputs({"name": "n", "default": {"a": 2, "b": 1}}))


def test_R9_3_secret_default_is_hashed_not_stored():
    one = manual.execution_fingerprint(_with_inputs({"name": "tok", "secret": True, "default": "s3cr3t-one"}))
    two = manual.execution_fingerprint(_with_inputs({"name": "tok", "secret": True, "default": "s3cr3t-two"}))
    assert one != two
    assert "s3cr3t" not in json.dumps(one) and "s3cr3t" not in json.dumps(two)
    entry = next(i for i in one["inputs"] if i["name"] == "tok")
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", entry["default"])


async def test_R9_3_default_only_change_is_approved_spec_mismatch(db, wired):
    approved = _with_inputs({"name": "view", "default": "list"})
    db.conn = _Conn(row=_db_row(approved))
    changed = _with_inputs({"name": "view", "default": "detail"})
    with pytest.raises(BLOCKED) as exc:
        await manual.run_financial_recipe(changed, **_run_args(changed))
    assert exc.value.code == "approved_spec_mismatch"
    assert wired.vault == []
    same = _with_inputs({"name": "view", "default": "list"})
    assert (await manual.run_financial_recipe(same, **_run_args(same))).status == "success"


# ---- 4. FINANCIAL_ACTIONS 와 schema.ALLOWED_ACTIONS 의 일치


def test_R9_4_financial_actions_are_a_subset_of_schema_allowed_actions():
    from app.services.work_recipe.schema import ALLOWED_ACTIONS

    assert "wait" not in manual.FINANCIAL_ACTIONS and "wait" not in ALLOWED_ACTIONS
    assert manual.FINANCIAL_ACTIONS <= set(ALLOWED_ACTIONS)


def test_R9_4_real_json_recipe_goes_through_schema_parser_then_manifest_check():
    text = json.dumps(_spec(), ensure_ascii=False)
    recipe = parse_recipe(text)                       # 실제 schema 파서 (JSON 은 YAML 의 부분집합)
    assert manual.validate_manifest(recipe) == (1, 3, 2)
    broken = _spec()
    broken["steps"][5] = {"action": "wait", "selector": "#acctList"}
    with pytest.raises(ValueError, match="wait"):     # 파서가 먼저 거부한다 — 매니페스트까지 가지 못한다
        parse_recipe(json.dumps(broken, ensure_ascii=False))
    forbidden = _spec()
    forbidden["steps"][5] = {"action": "select", "selector": "#acctList"}
    with pytest.raises(BLOCKED, match="financial_action_forbidden"):
        manual.validate_manifest(parse_recipe(json.dumps(forbidden, ensure_ascii=False)))


# ============================================================ R9 재작업 — 리뷰 지적 교정


@pytest.mark.parametrize("target", [
    "https://bizbank.%73hinhan.com/login", "https://bizbank.shinhan%2ecom/login",
    "https:///bizbank.shinhan.com/login", "https://///bizbank.shinhan.com/login",
    "https:/bizbank.shinhan.com/login", "https:bizbank.shinhan.com/login",
    "//bizbank.shinhan.com/login", "///bizbank.shinhan.com/login", "\\\\bizbank.shinhan.com\\login",
    "https://ｂｉｚｂａｎｋ.ｓｈｉｎｈａｎ.ｃｏｍ/login", "https://bizbank。shinhan。com/login",
    "https://biz\u00adbank.shinhan.com/login", "https://bizbank.shinhan.com:8443/login",
    "https://user:pw@bizbank.shinhan.com/login", "  \x01https://bizbank.shinhan.com/login",
    "bizbank.shinhan.com:8443/login", "HTTPS://BIZBANK.SHINHAN.COM./login",
    "https://bizbank.%2573hinhan.com/login",
])
def test_R9r_navigation_host_matches_browser_for_disguised_shinhan_targets(target):
    assert manual.is_shinhan_navigation_target(target), target
    payload = {"action": "navigate", "url": target, "context": {"domain": "shop.example.com"}}
    with pytest.raises(BLOCKED) as exc:
        manual.assert_unregistered_navigation(payload)
    assert exc.value.code == "shinhan_host_unregistered_recipe"


@pytest.mark.parametrize("target", [
    "https://shop.example.com/", "https://notshinhan.com/", "https://bizbank.shinhan.com.evil.net/",
    "https://evil.net\\@bizbank.shinhan.com/", "https://evil.net/?next=bizbank.shinhan.com",
    "https://evil.net#@bizbank.shinhan.com", "https://shinhan.com.cn/", "about:blank", "",
])
def test_R9r_navigation_host_does_not_flag_foreign_targets(target):
    assert not manual.is_shinhan_navigation_target(target), target
    manual.assert_unregistered_navigation(
        {"action": "navigate", "url": target, "context": {"domain": "shop.example.com"}})


def test_R9r_disguised_step_url_still_makes_recipe_financial():
    recipe = parse_recipe({"name": "sneaky", "domain": "shop.example.com",
                           "steps": [{"action": "navigate", "url": "https://bizbank.%73hinhan.com/login"}]})
    assert manual.is_shinhan_recipe(recipe)
    with pytest.raises(BLOCKED, match="financial_manifest_missing_or_invalid"):
        manual.validate_manifest(recipe)


def test_R9r_precheck_blocks_disguised_rendered_url():
    with pytest.raises(BLOCKED, match="shinhan_host_unregistered_recipe"):
        manual.assert_unregistered_runtime_host(
            _template_recipe(), {"base": "https://bizbank.%73hinhan.com"})


async def test_R9r_blocked_screen_failover_does_not_dispatch_a_financial_step_twice(db, wired, monkeypatch):
    """진짜 BrowserRecipeExecutor: PC 에서 실행한 click 이 차단 화면 증거로 페일오버 재실행되면 안 된다."""
    calls: list[tuple[str, Any]] = []

    async def attempt(self, action, payload):
        calls.append((action, payload.get("seq")))
        return {"ok": True, "output": {}, "route": "pc_agent",
                "evidence": {"url": "https://bizbank.shinhan.com/login",
                             "dom": {"text": "Access Denied"}}}

    monkeypatch.setattr(executor_module, "get_browser_bridge_service", lambda: object())
    monkeypatch.setattr(executor_module.BrowserRecipeExecutor, "_attempt", attempt)
    monkeypatch.setattr(orchestrator, "BrowserRecipeExecutor", executor_module.BrowserRecipeExecutor)
    recipe = _recipe()
    db.conn = _Conn(row=_db_row(recipe))
    result = await manual.run_financial_recipe(recipe, **_run_args(recipe))
    assert calls == [("navigate", 1)]
    assert result.status == "blocked"
    assert "financial_step_reexecution_forbidden" in result.error
    assert wired.audit_rows == []                      # 실패한 단계는 영수증을 남기지 않는다


async def test_R9r_same_seq_is_dispatched_once_but_distinct_steps_and_verify_pass():
    seen: list[Any] = []

    class Inner:
        async def _attempt(self, action, payload):
            seen.append((payload.get("phase"), payload.get("seq")))
            return {"ok": True}

    inner = manual.forbid_step_reexecution(Inner())
    assert (await inner._attempt("click", {"phase": "step", "seq": 1}))["ok"] is True
    assert (await inner._attempt("click", {"phase": "step", "seq": 2}))["ok"] is True
    again = await inner._attempt("click", {"phase": "step", "seq": 1})
    assert again["status"] == "blocked" and "financial_step_reexecution_forbidden" in again["error"]
    assert (await inner._attempt("snapshot", {"phase": "verify", "seq": 1}))["ok"] is True
    assert seen == [("step", 1), ("step", 2), ("verify", 1)]


def test_R9r_guard_is_skipped_for_executors_without_attempt():
    sentinel = object()
    assert manual.forbid_step_reexecution(sentinel) is sentinel

