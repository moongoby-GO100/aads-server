"""체크포인트 승인/수정."""
from fastapi import APIRouter, HTTPException
from langgraph.types import Command
from pydantic import BaseModel

router = APIRouter()


class CheckpointAction(BaseModel):
    action: str  # "approve" | "revise" | "cancel"
    feedback: str = ""


@router.post("/projects/{project_id}/checkpoint")
async def handle_checkpoint(project_id: str, body: CheckpointAction):
    """interrupt 재개: 승인/수정/취소."""
    from app.main import app_state

    graph = app_state.get("graph")
    if not graph:
        raise HTTPException(503, "Graph not ready")

    thread_id = f"project-{project_id}"
    config = {"configurable": {"thread_id": thread_id}}

    if body.action == "approve":
        resume_value = True
    elif body.action == "revise":
        resume_value = body.feedback or "수정 요청"
    elif body.action == "cancel":
        resume_value = {"approved": False, "cancel": True}
    else:
        raise HTTPException(400, f"Unknown action: {body.action}")

    # Command(resume=...)로 그래프 재개
    result = await graph.ainvoke(
        Command(resume=resume_value),
        config=config,
    )

    # 다음 interrupt 확인
    interrupt_payload = None
    if "__interrupt__" in result:
        interrupts = result["__interrupt__"]
        if interrupts:
            interrupt_payload = {
                "value": interrupts[0].value,
                "id": str(interrupts[0].id) if hasattr(interrupts[0], "id") else None,
            }

    return {
        "project_id": project_id,
        "status": "completed" if not interrupt_payload else "checkpoint_pending",
        "checkpoint_stage": result.get("checkpoint_stage", "unknown"),
        "interrupt_payload": interrupt_payload,
        "generated_files": result.get("generated_files", []),
        "total_cost_usd": result.get("total_cost_usd", 0.0),
    }
