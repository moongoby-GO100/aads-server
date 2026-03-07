"""
AADS-164: CEO Chat -> Agent Node 호출을 위한 경량 상태 빌더.
LangGraph AADSState를 CEO Chat 컨텍스트에서 최소한으로 생성.
"""
import uuid
from datetime import datetime, timezone
from typing import Optional, List, Dict


def build_agent_state(
    description: str,
    success_criteria: Optional[List[str]] = None,
    constraints: Optional[List[str]] = None,
    generated_files: Optional[List[Dict]] = None,
    project_id: str = "ceo-chat",
) -> dict:
    """CEO Chat에서 에이전트를 개별 호출할 때 필요한 최소 AADSState dict 구성."""
    return {
        "messages": [],
        "current_task": {
            "task_id": str(uuid.uuid4())[:8],
            "description": description,
            "assigned_agent": "ceo_chat",
            "success_criteria": success_criteria or [],
            "constraints": constraints or [],
            "input_artifacts": [],
            "output_artifacts": [],
            "max_iterations": 3,
            "max_llm_calls": 10,
            "budget_limit_usd": 5.0,
            "status": "in_progress",
        },
        "task_queue": [],
        "next_agent": None,
        "active_agents": [],
        "checkpoint_stage": "development",
        "approved_stages": [],
        "revision_count": 0,
        "llm_calls_count": 0,
        "total_cost_usd": 0.0,
        "cost_breakdown": {},
        "generated_files": generated_files or [],
        "sandbox_results": [],
        "qa_test_results": [],
        "judge_verdict": None,
        "project_id": project_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "iteration_count": 0,
        "error_log": [],
        "architect_design": None,
        "devops_result": None,
        "research_results": [],
    }
