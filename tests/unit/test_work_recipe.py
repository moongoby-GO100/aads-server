"""작업 레시피 스키마/재생 엔진 단위 테스트 (AADS-SB-P0).

핵심 계약 둘을 지킨다.
  AC-1  굳힌 레시피의 재생 경로에는 LLM 이 없다 — 2회차 llm_calls == 0
  AC-2  단계가 실패하면 재시도 후 실패로 끝나고, 실패 단계 seq 가 남는다
그리고 위험 등급이 높은 단계는 **실행되기 전에** 멈춘다(AC-6 의 앞부분).
"""
from __future__ import annotations

import asyncio

import pytest

from app.services.work_recipe import (
    ALLOWED_ACTIONS,
    RISK_LEVELS,
    RecipePlayer,
    WorkRecipe,
    parse_recipe,
    play_recipe,
    render_template,
    risk_rank,
)

RECIPE_YAML = """
name: baemin_daily_sales
domain: ceo.baemin.com
version: 1
description: 배민 사장님 사이트 일 매출 스냅샷
inputs:
  - name: shop_id
  - name: target_date
    required: false
    default: "2026-09-17"
  - name: password
    secret: true
steps:
  - action: navigate
    url: "https://ceo.baemin.com/shops/{{shop_id}}/sales"
    risk: READ
  - action: fill
    selector: "#password"
    value: "{{password}}"
    risk: READ
  - action: click
    selector: "button[type=submit]"
    wait_for: "#sales-table"
    risk: READ
  - action: snapshot
    selector: "#sales-table"
    save_as: sales_html
    risk: READ
verify:
  - action: snapshot
    selector: "#sales-total"
    risk: READ
"""


class RecordingExecutor:
    """가짜 실행기. 실제 Playwright 연결은 P0 범위 밖이다."""

    def __init__(self, *, fail_on_seq: int | None = None, fail_times: int = 99, llm_calls: int = 0):
        self.calls: list[dict] = []
        self.fail_on_seq = fail_on_seq
        self.fail_times = fail_times
        self.llm_calls = llm_calls
        self._failures = 0

    async def __call__(self, payload: dict) -> dict:
        self.calls.append(dict(payload))
        if payload["seq"] == self.fail_on_seq and self._failures < self.fail_times:
            self._failures += 1
            raise RuntimeError("selector not found: #sales-table")
        return {"ok": True, "output": f"seq-{payload['seq']}", "llm_calls": self.llm_calls}

    @property
    def executed_seqs(self) -> list[int]:
        return [call["seq"] for call in self.calls]


class MemoryRecorder:
    """recipe_runs / recipe_run_steps 기록을 메모리로 흉내낸다."""

    def __init__(self) -> None:
        self.runs: list[dict] = []
        self.steps: list[dict] = []
        self.finished: list[dict] = []

    async def start_run(self, *, recipe, inputs, recipe_id=None) -> str:
        run_id = f"run-{len(self.runs) + 1}"
        self.runs.append({"run_id": run_id, "recipe": recipe.name, "inputs": dict(inputs)})
        return run_id

    async def record_step(self, *, run_id, step) -> None:
        self.steps.append({"run_id": run_id, **step.to_dict()})

    async def finish_run(self, *, run_id, result) -> None:
        self.finished.append({"run_id": run_id, "status": result.status, "llm_calls": result.llm_calls})


def make_recipe() -> WorkRecipe:
    return parse_recipe(RECIPE_YAML)


# ----------------------------------------------------------------- 스키마


def test_parse_recipe_reads_steps_inputs_and_verify():
    recipe = make_recipe()
    assert recipe.name == "baemin_daily_sales"
    assert recipe.domain == "ceo.baemin.com"
    assert [step.action for step in recipe.steps] == ["navigate", "fill", "click", "snapshot"]
    assert [step.seq for step in recipe.steps] == [1, 2, 3, 4]
    assert len(recipe.verify) == 1
    assert recipe.declared_names() == ["shop_id", "target_date", "password"]
    assert recipe.max_risk() == "READ"


def test_recipe_roundtrips_through_yaml():
    recipe = make_recipe()
    again = parse_recipe(recipe.to_yaml())
    assert again.to_dict() == recipe.to_dict()


def test_undeclared_variable_raises_value_error():
    bad = RECIPE_YAML.replace("{{shop_id}}", "{{store_id}}")
    with pytest.raises(ValueError) as exc:
        parse_recipe(bad)
    assert "store_id" in str(exc.value)


def test_save_as_variable_is_usable_by_later_steps():
    recipe = parse_recipe(
        """
        name: chained
        domain: example.com
        inputs: [{name: url}]
        steps:
          - {action: navigate, url: "{{url}}", risk: READ}
          - {action: snapshot, selector: "#t", save_as: token, risk: READ}
          - {action: fill, selector: "#f", value: "{{token}}", risk: READ}
        """
    )
    assert recipe.steps[2].variables() == ["token"]


def test_save_as_cannot_be_used_before_it_is_produced():
    with pytest.raises(ValueError):
        parse_recipe(
            """
            name: out_of_order
            domain: example.com
            inputs: []
            steps:
              - {action: fill, selector: "#f", value: "{{token}}", risk: READ}
              - {action: snapshot, selector: "#t", save_as: token, risk: READ}
            """
        )


def test_invalid_action_raises_value_error():
    bad = RECIPE_YAML.replace("action: click", "action: teleport")
    with pytest.raises(ValueError) as exc:
        parse_recipe(bad)
    assert "teleport" in str(exc.value)
    assert "navigate" in str(exc.value)  # 허용값 안내


def test_invalid_risk_raises_value_error():
    bad = RECIPE_YAML.replace("risk: READ", "risk: MAYBE", 1)
    with pytest.raises(ValueError):
        parse_recipe(bad)


def test_allowed_vocabulary_matches_prd():
    assert ALLOWED_ACTIONS == (
        "navigate", "click", "fill", "select", "press",
        "upload", "download", "snapshot", "api_call",
    )
    assert RISK_LEVELS == ("READ", "WRITE_INTERNAL", "WRITE_EXTERNAL", "IRREVERSIBLE")
    assert risk_rank("READ") < risk_rank("WRITE_INTERNAL") < risk_rank("WRITE_EXTERNAL") < risk_rank("IRREVERSIBLE")


def test_render_template_rejects_undeclared_and_missing():
    assert render_template("a/{{x}}", {"x": 1}, declared=["x"]) == "a/1"
    with pytest.raises(ValueError):
        render_template("a/{{y}}", {"y": 1}, declared=["x"])
    with pytest.raises(ValueError):
        render_template("a/{{x}}", {}, declared=["x"])


def test_resolve_inputs_applies_default_and_requires_the_rest():
    recipe = make_recipe()
    values = recipe.resolve_inputs({"shop_id": "42", "password": "pw"})
    assert values["target_date"] == "2026-09-17"
    with pytest.raises(ValueError):
        recipe.resolve_inputs({"shop_id": "42"})


def test_missing_steps_raises_value_error():
    with pytest.raises(ValueError):
        parse_recipe({"name": "x", "domain": "example.com", "steps": []})


# ----------------------------------------------------------------- 재생


async def test_replay_twice_makes_no_llm_calls():
    """AC-1: 굳힌 레시피의 2회차 재생은 LLM 을 부르지 않는다."""
    recipe = make_recipe()
    inputs = {"shop_id": "42", "password": "pw"}

    first_executor = RecordingExecutor()
    first = await play_recipe(recipe, first_executor, inputs)

    second_executor = RecordingExecutor()
    second = await play_recipe(recipe, second_executor, inputs)

    assert first.status == "success"
    assert second.status == "success"
    assert second.llm_calls == 0
    assert first.llm_calls == 0
    # 두 번 모두 같은 단계를 같은 순서로 실행했다 (steps 4 + verify 1)
    assert first_executor.executed_seqs == second_executor.executed_seqs == [1, 2, 3, 4, 5]


async def test_replay_substitutes_variables_into_step_payload():
    recipe = make_recipe()
    executor = RecordingExecutor()
    await play_recipe(recipe, executor, {"shop_id": "42", "password": "pw"})
    assert executor.calls[0]["url"] == "https://ceo.baemin.com/shops/42/sales"
    assert executor.calls[1]["value"] == "pw"


async def test_step_failure_retries_then_fails_with_seq():
    """AC-2: 1단계 실패 주입 → 재시도 후 status=failed, 실패 단계 seq 기록."""
    recipe = make_recipe()
    executor = RecordingExecutor(fail_on_seq=1)
    result = await play_recipe(recipe, executor, {"shop_id": "42", "password": "pw"})

    assert result.status == "failed"
    assert result.failed_step_seq == 1
    assert "selector not found" in result.error
    # 기본 retries=2 → 총 3회 시도
    assert executor.executed_seqs == [1, 1, 1]

    failed = [step for step in result.steps if step.seq == 1][0]
    assert failed.attempts == 3
    assert failed.status == "failed"
    # 뒤 단계는 실행되지 않고 skipped 로 남는다
    assert [step.status for step in result.steps[1:]] == ["skipped"] * 4


async def test_transient_failure_recovers_within_retry_budget():
    recipe = make_recipe()
    executor = RecordingExecutor(fail_on_seq=2, fail_times=1)
    result = await play_recipe(recipe, executor, {"shop_id": "42", "password": "pw"})
    assert result.status == "success"
    assert [step for step in result.steps if step.seq == 2][0].attempts == 2


async def test_executor_reported_failure_is_retried_too():
    recipe = make_recipe()
    calls: list[int] = []

    async def executor(payload):
        calls.append(payload["seq"])
        if payload["seq"] == 3:
            return {"ok": False, "error": "timeout waiting for #sales-table"}
        return {"ok": True}

    result = await play_recipe(recipe, executor, {"shop_id": "42", "password": "pw"}, retries=1)
    assert result.status == "failed"
    assert result.failed_step_seq == 3
    assert calls.count(3) == 2


async def test_human_gateway_recovery_blocks_without_retrying():
    recipe = make_recipe()
    calls: list[int] = []

    async def executor(payload):
        calls.append(payload["seq"])
        return {
            "ok": False,
            "error": "session expired",
            "recovery": {
                "route": "human_gateway",
                "reason": "SESSION_EXPIRED",
                "resume": "login_then_retry_same_step",
            },
        }

    result = await play_recipe(recipe, executor, {"shop_id": "42", "password": "pw"}, retries=2)

    assert result.status == "blocked"
    assert result.blocked_step_seq == 1
    assert result.failed_step_seq is None
    assert calls == [1]
    assert result.steps[0].recovery["resume"] == "login_then_retry_same_step"


async def test_step_timeout_is_enforced_and_retried():
    recipe = parse_recipe(
        """
        name: slow
        domain: example.com
        inputs: []
        steps:
          - {action: navigate, url: "https://example.com", risk: READ, timeout: 0.05}
        """
    )

    async def slow_executor(payload):
        await asyncio.sleep(5)
        return {"ok": True}

    result = await play_recipe(recipe, slow_executor, retries=0)
    assert result.status == "failed"
    assert "timeout" in result.error


async def test_irreversible_step_blocks_before_execution():
    """risk=IRREVERSIBLE 단계는 실행되지 않고 status=blocked 로 멈춘다."""
    recipe = parse_recipe(
        """
        name: delete_menu
        domain: ceo.baemin.com
        inputs: [{name: menu_id}]
        steps:
          - {action: navigate, url: "https://ceo.baemin.com/menus/{{menu_id}}", risk: READ}
          - {action: click, selector: "#delete", risk: IRREVERSIBLE}
          - {action: snapshot, selector: "#result", risk: READ}
        """
    )
    executor = RecordingExecutor()
    result = await play_recipe(recipe, executor, {"menu_id": "7"})

    assert result.status == "blocked"
    assert result.blocked_step_seq == 2
    assert result.blocked_risk == "IRREVERSIBLE"
    # 위험 단계와 그 뒤는 실행기에 전달조차 되지 않았다
    assert executor.executed_seqs == [1]
    assert [step.status for step in result.steps] == ["success", "blocked", "skipped"]
    assert result.llm_calls == 0


async def test_write_external_step_blocks_by_default():
    recipe = parse_recipe(
        """
        name: send_mail
        domain: mail.example.com
        inputs: []
        steps:
          - {action: api_call, endpoint: "/send", risk: WRITE_EXTERNAL}
        """
    )
    executor = RecordingExecutor()
    result = await play_recipe(recipe, executor, {})
    assert result.status == "blocked"
    assert executor.calls == []


async def test_max_risk_can_be_raised_for_internal_writes():
    recipe = parse_recipe(
        """
        name: internal_write
        domain: example.com
        inputs: []
        steps:
          - {action: fill, selector: "#memo", value: "ok", risk: WRITE_INTERNAL}
        """
    )
    executor = RecordingExecutor()
    result = await play_recipe(recipe, executor, {})
    assert result.status == "success"
    assert executor.executed_seqs == [1]


async def test_run_and_steps_are_recorded():
    recipe = make_recipe()
    recorder = MemoryRecorder()
    player = RecipePlayer(RecordingExecutor(), recorder=recorder)
    result = await player.play(recipe, {"shop_id": "42", "password": "pw"})

    assert result.run_id == "run-1"
    assert len(recorder.steps) == 5
    assert recorder.steps[0]["run_id"] == "run-1"
    assert recorder.finished == [{"run_id": "run-1", "status": "success", "llm_calls": 0}]
    # secret 입력은 기록기로 넘어가되 마스킹은 저장 계층(store)의 책임이다
    assert recorder.runs[0]["inputs"]["password"] == "pw"


async def test_llm_calls_from_executor_are_counted():
    """실행 경로에 모델이 끼어들면 숫자로 드러나야 한다 — 0 을 지키는 근거."""
    recipe = make_recipe()
    result = await play_recipe(recipe, RecordingExecutor(llm_calls=1), {"shop_id": "42", "password": "pw"})
    assert result.status == "success"
    assert result.llm_calls == 5


async def test_sync_executor_is_supported():
    recipe = make_recipe()
    seen: list[int] = []

    def executor(payload):
        seen.append(payload["seq"])
        return {"ok": True}

    result = await play_recipe(recipe, executor, {"shop_id": "42", "password": "pw"})
    assert result.status == "success"
    assert seen == [1, 2, 3, 4, 5]


def test_player_rejects_bad_construction():
    with pytest.raises(ValueError):
        RecipePlayer("not-callable")
    with pytest.raises(ValueError):
        RecipePlayer(RecordingExecutor(), step_timeout=0)
    with pytest.raises(ValueError):
        RecipePlayer(RecordingExecutor(), retries=-1)
    with pytest.raises(ValueError):
        RecipePlayer(RecordingExecutor(), max_risk="NOPE")


# ----------------------------------------------------------------- 저장소


def test_store_normalizes_domains():
    from app.services.work_recipe import store

    assert store.normalize_domain("https://www.ceo.baemin.com/shops/1") == "ceo.baemin.com"
    assert store.normalize_domain("CEO.Baemin.com:443") == "ceo.baemin.com"
    assert store.normalize_domain("") == ""


def test_database_recorder_satisfies_player_recorder_contract():
    from app.services.work_recipe.store import DatabaseRunRecorder

    recorder = DatabaseRunRecorder(tenant_id=None, triggered_by="unit-test")
    for hook in ("start_run", "record_step", "finish_run"):
        assert callable(getattr(recorder, hook))


def test_secret_inputs_are_masked_before_storage():
    from app.services.work_recipe.store import _maskable

    recipe = make_recipe()
    masked = _maskable(recipe, {"shop_id": "42", "password": "pw"})
    assert masked["password"] == "***"
    assert masked["shop_id"] == "42"
