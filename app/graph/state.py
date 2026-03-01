from typing import TypedDict, Annotated, Optional, Literal
from pydantic import BaseModel, Field
from langgraph.graph import add_messages
import uuid
from datetime import datetime


# TaskSpec — T-007 12필드 (Pydantic BaseModel)
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
    max_llm_calls: int = 15       # R-012
    budget_limit_usd: float = 10.0
    status: Literal[
        "pending", "in_progress", "completed",
        "failed", "blocked", "cancelled"
    ] = "pending"


# Judge 결과 스키마
class JudgeVerdict(BaseModel):
    verdict: Literal["pass", "fail", "conditional_pass"]
    score: float = 0.0
    issues: list[str] = []
    recommendation: str = ""


# 체크포인트 단계 (설계서 6단계)
UserCheckpointStage = Literal[
    "requirements",      # 1. 요구사항 수집
    "plan_review",       # 2. 기획서 승인
    "design_review",     # 3. 설계 확인
    "development",       # 4. 자율 개발 (자동)
    "midpoint_review",   # 5. 중간 확인
    "final_review",      # 6. 최종 승인·배포
    "completed",
    "cancelled"
]


# AADSState — 그래프 상태 (TypedDict)
class AADSState(TypedDict):
    # 메시지 히스토리 (add_messages reducer)
    messages: Annotated[list, add_messages]

    # 현재 작업
    current_task: Optional[dict]       # TaskSpec.model_dump()
    task_queue: list[dict]             # 대기 중 작업들

    # 에이전트 라우팅
    next_agent: Optional[str]
    active_agents: list[str]

    # 체크포인트 (6단계)
    checkpoint_stage: str              # UserCheckpointStage
    approved_stages: list[str]
    revision_count: int

    # 비용 추적
    llm_calls_count: int               # R-012: <= 15
    total_cost_usd: float
    cost_breakdown: dict               # {agent: cost}

    # 코드/파일
    generated_files: list[dict]        # [{path, content, language}]
    sandbox_results: list[dict]        # [{stdout, stderr, exit_code}]

    # 메타
    project_id: str
    created_at: str
    iteration_count: int               # 루프 카운터, max 5
    error_log: list[str]
