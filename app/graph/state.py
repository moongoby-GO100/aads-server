from typing import TypedDict, Annotated, Optional, Literal
import operator
from pydantic import BaseModel, Field
from langgraph.graph import add_messages
import uuid
from datetime import datetime


def _last_value(a, b):
    """마지막 값 우선 리듀서 — 동일 superstep 내 다중 업데이트 허용."""
    return b


def _merge_dicts(a: dict, b: dict) -> dict:
    """dict 병합 리듀서 — 숫자 값은 합산."""
    result = dict(a)
    for k, v in b.items():
        if k in result and isinstance(result[k], (int, float)) and isinstance(v, (int, float)):
            result[k] = result[k] + v
        else:
            result[k] = v
    return result


class TaskSpec(BaseModel):
    task_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    parent_task_id: Optional[str] = None
    description: str
    assigned_agent: str
    success_criteria: list[str]
    constraints: list[str] = []
    input_artifacts: list[str] = []
    output_artifacts: list[str] = []
    max_iterations: int = 5
    max_llm_calls: int = 15
    budget_limit_usd: float = 10.0
    status: Literal[
        "pending", "in_progress", "completed",
        "failed", "blocked", "cancelled"
    ] = "pending"


class JudgeVerdict(BaseModel):
    verdict: Literal["pass", "fail", "conditional_pass"]
    score: float = 0.0
    issues: list[str] = []
    recommendation: str = ""


UserCheckpointStage = Literal[
    "requirements",
    "plan_review",
    "design_review",
    "development",
    "midpoint_review",
    "final_review",
    "completed",
    "cancelled"
]


class AADSState(TypedDict):
    # 메시지 (add_messages reducer)
    messages: Annotated[list, add_messages]

    # 현재 작업
    current_task: Annotated[Optional[dict], _last_value]
    task_queue: Annotated[list[dict], _last_value]

    # 에이전트 라우팅
    next_agent: Annotated[Optional[str], _last_value]
    active_agents: Annotated[list[str], _last_value]

    # 체크포인트
    checkpoint_stage: Annotated[str, _last_value]
    approved_stages: Annotated[list[str], _last_value]
    revision_count: Annotated[int, _last_value]

    # 비용 추적 (절대값, _last_value)
    llm_calls_count: Annotated[int, _last_value]
    total_cost_usd: Annotated[float, _last_value]
    cost_breakdown: Annotated[dict, _merge_dicts]

    # 코드/파일
    generated_files: Annotated[list[dict], _last_value]
    sandbox_results: Annotated[list[dict], _last_value]

    # QA/Judge
    qa_test_results: Annotated[list[dict], _last_value]
    judge_verdict: Annotated[Optional[dict], _last_value]

    # 메타
    project_id: Annotated[str, _last_value]
    created_at: Annotated[str, _last_value]
    iteration_count: Annotated[int, _last_value]
    error_log: Annotated[list[str], operator.add]

    # Architect / DevOps / Researcher
    architect_design: Annotated[Optional[dict], _last_value]
    devops_result: Annotated[Optional[dict], _last_value]
    research_results: Annotated[list[dict], _last_value]
