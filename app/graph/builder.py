"""
LangGraph StateGraph 빌드.
⚠️ langgraph-supervisor 사용 금지 (R-010).
⚠️ Native StateGraph만 사용.
"""
from langgraph.graph import StateGraph, START, END

from app.graph.state import AADSState
from app.graph.routing import (
    route_after_pm,
    route_after_supervisor,
    route_after_developer,
    route_after_qa,
    route_after_judge,
    route_after_devops,
)
from app.agents.pm import pm_requirements_node
from app.agents.supervisor import supervisor_node
from app.agents.architect_agent import architect_node
from app.agents.developer import developer_node
from app.agents.qa_agent import qa_node
from app.agents.judge_agent import judge_node
from app.agents.devops_agent import devops_node
from app.agents.researcher_agent import researcher_node


def build_aads_graph() -> StateGraph:
    """
    Week 2+ 그래프: PM → Supervisor → [Architect →] Developer → QA → Judge → DevOps
    Researcher는 Supervisor가 온디맨드 호출

    흐름:
    START → pm → supervisor → architect → developer → qa → judge → devops → END
                    ↕ researcher (온디맨드)       ↑_____(fail, max 3회)
    """
    builder = StateGraph(AADSState)

    # 노드 등록 (8개)
    builder.add_node("pm_requirements", pm_requirements_node)
    builder.add_node("supervisor", supervisor_node)
    builder.add_node("architect", architect_node)
    builder.add_node("developer", developer_node)
    builder.add_node("qa", qa_node)
    builder.add_node("judge", judge_node)
    builder.add_node("devops", devops_node)
    builder.add_node("researcher", researcher_node)

    # 시작 엣지
    builder.add_edge(START, "pm_requirements")

    # PM 이후 조건부 라우팅
    builder.add_conditional_edges(
        "pm_requirements",
        route_after_pm,
        {
            "supervisor": "supervisor",
            "pm_requirements": "pm_requirements",
            "__end__": END,
        },
    )

    # Supervisor 이후: Architect 또는 Developer (설계 이미 있으면)
    builder.add_conditional_edges(
        "supervisor",
        route_after_supervisor,
        {
            "architect": "architect",
            "developer": "developer",
            "researcher": "researcher",
            "__end__": END,
        },
    )

    # Architect 이후: Developer로
    builder.add_edge("architect", "developer")

    # Researcher 이후: supervisor로 복귀
    builder.add_edge("researcher", "supervisor")

    # Developer 이후 QA로 이동 (조건부 라우팅)
    builder.add_conditional_edges(
        "developer",
        route_after_developer,
        {
            "qa": "qa",
            "supervisor": "supervisor",
            "__end__": END,
        },
    )

    # QA 이후 Judge로 이동
    builder.add_conditional_edges(
        "qa",
        route_after_qa,
        {
            "judge": "judge",
            "__end__": END,
        },
    )

    # Judge 이후: pass → DevOps, fail → Developer 재작업
    builder.add_conditional_edges(
        "judge",
        route_after_judge,
        {
            "developer": "developer",
            "devops": "devops",
            "__end__": END,
        },
    )

    # DevOps 이후: END
    builder.add_conditional_edges(
        "devops",
        route_after_devops,
        {
            "__end__": END,
        },
    )

    return builder


async def compile_graph(checkpointer=None):
    """그래프 컴파일. checkpointer가 있으면 interrupt 활성화."""
    builder = build_aads_graph()
    graph = builder.compile(checkpointer=checkpointer)
    return graph
