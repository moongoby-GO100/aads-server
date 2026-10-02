"""PC 레인 도구 1회 호출의 상위 시한(budget)과 취소 토큰.

run_with_pc_agent_deadline 이 호출 맥락(ContextVar)에 budget 을 걸고, 시한을 넘기면
cancel() 한다. 이후 같은 맥락에서 나가는 browser_* 명령은 ensure_open() 이 막는다.
정리용 browser_close_session 만 예외다.
"""
from __future__ import annotations

import contextvars
import os
import time
from dataclasses import dataclass
from typing import Optional


def _float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


# 시한 직전에 결과를 돌려줄 여유. 이보다 남지 않으면 일반 eval 은 보내지 않는다.
BUDGET_RESERVE_SECONDS = _float_env("AADS_PC_AGENT_BUDGET_RESERVE_SECONDS", 2.0)
# goto 직후 href 확인 eval 1건의 예상 소요(명령 시한 10s + 큐 여유). 잔여가 이보다 적으면 생략.
HREF_EVAL_ESTIMATE_SECONDS = _float_env("AADS_PC_AGENT_HREF_EVAL_ESTIMATE_SECONDS", 12.0)

_CLEANUP_COMMAND_TYPES = {"browser_close_session"}


class PcAgentDeadlineExceeded(RuntimeError):
    """상위 시한이 지났거나 잔여 시간이 부족해 명령을 보내지 않았다."""

    error_code = "PC_AGENT_DEADLINE_EXCEEDED"


@dataclass
class PcAgentCallBudget:
    deadline: float
    stage: str = ""
    cancelled: bool = False

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def cancel(self) -> None:
        self.cancelled = True


_current_budget: contextvars.ContextVar[Optional[PcAgentCallBudget]] = contextvars.ContextVar(
    "pc_agent_call_budget", default=None
)


def begin_call_budget(total_seconds: float, stage: str = "") -> tuple[PcAgentCallBudget, contextvars.Token]:
    budget = PcAgentCallBudget(deadline=time.monotonic() + float(total_seconds), stage=stage)
    return budget, _current_budget.set(budget)


def end_call_budget(token: contextvars.Token) -> None:
    _current_budget.reset(token)


def current_call_budget() -> Optional[PcAgentCallBudget]:
    return _current_budget.get()


def remaining_seconds() -> Optional[float]:
    budget = _current_budget.get()
    return None if budget is None else budget.remaining()


def ensure_open(command_type: str) -> Optional[float]:
    """명령 발송 직전 호출. 시한이 취소/경과됐으면 예외, 아니면 잔여 초(budget 없으면 None)."""
    budget = _current_budget.get()
    if budget is None:
        return None
    if str(command_type or "").strip().lower() in _CLEANUP_COMMAND_TYPES:
        return budget.remaining()
    if budget.cancelled or budget.remaining() <= 0:
        raise PcAgentDeadlineExceeded(
            f"pc_agent_deadline_exceeded: {command_type} not sent after deadline (stage={budget.stage})"
        )
    return budget.remaining()
