"""레시피 재생 엔진 — 정상 경로에서 LLM 호출 0회.

한 번 학습해 굳힌 레시피를 다시 실행할 때 모델을 부르지 않는 것이 이 모듈의
존재 이유다. 그래서 player 는 어떤 LLM 클라이언트도 import 하지 않는다.
`llm_calls` 는 실행기가 스스로 보고한 값만 합산하므로, 재생 경로에 모델이
끼어들면 숫자로 드러난다(AC-1).

실제 브라우저 연결은 이 단계에서 하지 않는다. 실행기(executor)는 주입받는
callable 이고, 테스트는 가짜 실행기로 같은 계약을 검증한다.
"""
from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Optional

from app.services.work_recipe.schema import RecipeStep, WorkRecipe, risk_rank

DEFAULT_STEP_TIMEOUT = 30.0
DEFAULT_RETRIES = 2
# 이 등급을 **넘는** 단계를 만나면 중단한다. 승인 게이트는 P1.
DEFAULT_MAX_RISK = "WRITE_INTERNAL"

STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_BLOCKED = "blocked"
STATUS_SKIPPED = "skipped"

ExecutorResult = Mapping[str, Any] | None
StepExecutor = Callable[[dict[str, Any]], "ExecutorResult | Awaitable[ExecutorResult]"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class StepResult:
    """단계 하나의 실행 결과."""

    seq: int
    action: str
    risk: str
    status: str
    phase: str = "step"
    attempts: int = 0
    duration_ms: int = 0
    error: str = ""
    output: Any = None
    llm_calls: int = 0
    save_as: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "action": self.action,
            "risk": self.risk,
            "status": self.status,
            "phase": self.phase,
            "attempts": self.attempts,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "output": self.output,
            "llm_calls": self.llm_calls,
            "save_as": self.save_as,
        }


@dataclass
class RunResult:
    """레시피 한 번의 재생 결과."""

    recipe_name: str
    domain: str
    version: int
    status: str
    steps: list[StepResult] = field(default_factory=list)
    llm_calls: int = 0
    error: str = ""
    failed_step_seq: int | None = None
    blocked_step_seq: int | None = None
    blocked_risk: str = ""
    variables: dict[str, Any] = field(default_factory=dict)
    run_id: Any = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCESS

    @property
    def duration_ms(self) -> int:
        if not self.started_at or not self.finished_at:
            return 0
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id) if self.run_id is not None else None,
            "recipe_name": self.recipe_name,
            "domain": self.domain,
            "version": self.version,
            "status": self.status,
            "llm_calls": self.llm_calls,
            "error": self.error,
            "failed_step_seq": self.failed_step_seq,
            "blocked_step_seq": self.blocked_step_seq,
            "blocked_risk": self.blocked_risk,
            "duration_ms": self.duration_ms,
            "steps": [step.to_dict() for step in self.steps],
        }


class StepFailure(RuntimeError):
    """실행기가 보고한 단계 실패."""


class RecipePlayer:
    """레시피를 순차 실행한다.

    Parameters
    ----------
    executor:
        단계 payload(dict)를 받아 실행하는 callable. 동기/비동기 모두 허용.
        실패는 예외를 던지거나 `{"ok": False, "error": "..."}` 로 보고한다.
    recorder:
        recipe_runs / recipe_run_steps 기록기. None 이면 기록하지 않는다
        (DB 없이도 재생 자체는 동작해야 하므로 선택 사항이다).
    step_timeout:
        단계별 기본 제한 시간(초). 단계에 timeout 이 있으면 그쪽이 우선.
    retries:
        실패 시 **추가** 시도 횟수. 기본 2 → 최대 3회 시도.
    max_risk:
        이 등급을 넘는 단계를 만나면 실행을 멈추고 status=blocked 로 반환한다.
    """

    def __init__(
        self,
        executor: StepExecutor,
        *,
        recorder: Any | None = None,
        step_timeout: float = DEFAULT_STEP_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        retry_delay: float = 0.0,
        max_risk: str = DEFAULT_MAX_RISK,
        run_verify: bool = True,
    ) -> None:
        if not callable(executor):
            raise ValueError("executor 는 호출 가능한 객체여야 합니다")
        if step_timeout <= 0:
            raise ValueError("step_timeout 은 0보다 커야 합니다")
        if retries < 0:
            raise ValueError("retries 는 0 이상이어야 합니다")
        self._executor = executor
        self._recorder = recorder
        self._step_timeout = float(step_timeout)
        self._retries = int(retries)
        self._retry_delay = max(0.0, float(retry_delay))
        self._max_risk_rank = risk_rank(max_risk)
        self._max_risk = max_risk.strip().upper()
        self._run_verify = run_verify

    # ---- 공개 API ------------------------------------------------------

    async def play(
        self,
        recipe: WorkRecipe,
        inputs: Mapping[str, Any] | None = None,
        *,
        context: Mapping[str, Any] | None = None,
        recipe_id: Any = None,
    ) -> RunResult:
        values = recipe.resolve_inputs(inputs)
        declared: set[str] = set(values.keys())

        result = RunResult(
            recipe_name=recipe.name,
            domain=recipe.domain,
            version=recipe.version,
            status=STATUS_SUCCESS,
            started_at=_now(),
        )
        result.run_id = await self._start_run(recipe, values, recipe_id=recipe_id)

        planned: list[tuple[str, RecipeStep]] = [("step", step) for step in recipe.steps]
        if self._run_verify:
            planned.extend(("verify", step) for step in recipe.verify)

        stop_index: int | None = None
        for index, (phase, step) in enumerate(planned):
            guard_hook = getattr(self._recorder, "before_step", None)  # P1 승인 게이트/도메인 가드
            if guard_hook is not None:
                await _maybe_await(guard_hook(run_id=result.run_id, step=step, phase=phase))
            if risk_rank(step.risk) > self._max_risk_rank:
                # 승인 게이트는 P1. 여기서는 실행하지 않고 멈추는 것까지만 한다.
                blocked = StepResult(
                    seq=step.seq,
                    action=step.action,
                    risk=step.risk,
                    status=STATUS_BLOCKED,
                    phase=phase,
                    save_as=step.save_as,
                    error=f"risk={step.risk} 단계는 승인이 필요합니다(허용 상한 {self._max_risk})",
                )
                result.steps.append(blocked)
                await self._record_step(result.run_id, blocked)
                result.status = STATUS_BLOCKED
                result.blocked_step_seq = step.seq
                result.blocked_risk = step.risk
                result.error = blocked.error
                stop_index = index
                break

            step_result = await self._run_step(phase, step, values, declared, context)
            result.steps.append(step_result)
            await self._record_step(result.run_id, step_result)
            result.llm_calls += step_result.llm_calls

            if step_result.status != STATUS_SUCCESS:
                result.status = STATUS_FAILED
                result.failed_step_seq = step.seq
                result.error = step_result.error
                stop_index = index
                break

            if step.save_as:
                values[step.save_as] = step_result.output
                declared.add(step.save_as)

        if stop_index is not None:
            for phase, step in planned[stop_index + 1:]:
                skipped = StepResult(
                    seq=step.seq,
                    action=step.action,
                    risk=step.risk,
                    status=STATUS_SKIPPED,
                    phase=phase,
                    save_as=step.save_as,
                )
                result.steps.append(skipped)
                await self._record_step(result.run_id, skipped)

        result.variables = dict(values)
        result.finished_at = _now()
        await self._finish_run(result)
        return result

    # ---- 내부 ----------------------------------------------------------

    async def _run_step(
        self,
        phase: str,
        step: RecipeStep,
        values: Mapping[str, Any],
        declared: set[str],
        context: Mapping[str, Any] | None,
    ) -> StepResult:
        outcome = StepResult(
            seq=step.seq,
            action=step.action,
            risk=step.risk,
            status=STATUS_FAILED,
            phase=phase,
            save_as=step.save_as,
        )
        started = time.monotonic()
        timeout = step.timeout or self._step_timeout

        try:
            rendered = step.render(values, declared=sorted(declared))
        except ValueError as exc:
            outcome.error = str(exc)
            outcome.attempts = 0
            outcome.duration_ms = int((time.monotonic() - started) * 1000)
            return outcome

        payload_base = rendered.to_dict(include_seq=True)
        payload_base["phase"] = phase
        if context:
            payload_base["context"] = dict(context)

        last_error = ""
        for attempt in range(1, self._retries + 2):
            outcome.attempts = attempt
            payload = dict(payload_base)
            payload["attempt"] = attempt
            try:
                raw = await self._invoke(payload, timeout)
            except asyncio.TimeoutError:
                last_error = f"timeout after {timeout}s"
            except StepFailure as exc:
                last_error = str(exc) or "step failed"
            except Exception as exc:  # 실행기 예외는 재시도 대상이다
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                data = dict(raw) if isinstance(raw, Mapping) else {}
                outcome.llm_calls += _int_or_zero(data.get("llm_calls"))
                if data.get("ok") is False or data.get("status") == STATUS_FAILED:
                    last_error = str(data.get("error") or "step reported failure")
                else:
                    outcome.status = STATUS_SUCCESS
                    outcome.error = ""
                    outcome.output = data.get("output", data.get("data", raw))
                    outcome.duration_ms = int((time.monotonic() - started) * 1000)
                    return outcome
            if attempt <= self._retries and self._retry_delay > 0:
                await asyncio.sleep(self._retry_delay)

        outcome.error = last_error or "step failed"
        outcome.duration_ms = int((time.monotonic() - started) * 1000)
        return outcome

    async def _invoke(self, payload: dict[str, Any], timeout: float) -> ExecutorResult:
        if inspect.iscoroutinefunction(self._executor):
            return await asyncio.wait_for(self._executor(payload), timeout)
        result = self._executor(payload)
        if inspect.isawaitable(result):
            return await asyncio.wait_for(result, timeout)
        return result

    async def _start_run(
        self,
        recipe: WorkRecipe,
        values: Mapping[str, Any],
        *,
        recipe_id: Any,
    ) -> Any:
        hook = getattr(self._recorder, "start_run", None)
        if hook is None:
            return None
        return await _maybe_await(hook(recipe=recipe, inputs=dict(values), recipe_id=recipe_id))

    async def _record_step(self, run_id: Any, step_result: StepResult) -> None:
        hook = getattr(self._recorder, "record_step", None)
        if hook is None:
            return
        await _maybe_await(hook(run_id=run_id, step=step_result))

    async def _finish_run(self, result: RunResult) -> None:
        hook = getattr(self._recorder, "finish_run", None)
        if hook is None:
            return
        await _maybe_await(hook(run_id=result.run_id, result=result))


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


async def play_recipe(
    recipe: WorkRecipe,
    executor: StepExecutor,
    inputs: Mapping[str, Any] | None = None,
    *,
    recorder: Optional[Any] = None,
    step_timeout: float = DEFAULT_STEP_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    max_risk: str = DEFAULT_MAX_RISK,
    context: Mapping[str, Any] | None = None,
    recipe_id: Any = None,
) -> RunResult:
    """한 줄로 재생하는 편의 함수."""
    player = RecipePlayer(
        executor,
        recorder=recorder,
        step_timeout=step_timeout,
        retries=retries,
        max_risk=max_risk,
    )
    return await player.play(recipe, inputs, context=context, recipe_id=recipe_id)
