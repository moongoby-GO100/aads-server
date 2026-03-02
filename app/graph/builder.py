"""
LangGraph StateGraph 빌드.
⚠️ langgraph-supervisor 사용 금지 (R-010).
⚠️ Native StateGraph만 사용.
"""
from langgraph.graph import StateGraph, START, END

from app.graph.state import AADSState
from app.graph.routing import (
    route_after_pm,
    route_after_developer,
    route_after_qa,
    route_after_judge,
)
from app.agents.pm import pm_requirements_node
from app.agents.supervisor import supervisor_node
from app.agents.developer import developer_node
from app.agents.qa_agent import qa_node
from app.agents.judge_agent import judge_node


def build_aads_graph() -> StateGraph:
    """
    Week 2 그래프: PM → Supervisor → Developer → QA → Judge

    흐름:
    START → pm_requirements → [route] → supervisor → developer → qa → judge
                                            ↑__________________________|
                                            (Judge fail, max 3회 재작업)
    """
    builder = StateGraph(AADSState)

    # 노드 등록
    builder.add_node("pm_requirements", pm_requirements_node)
    builder.add_node("supervisor", supervisor_node)
    builder.add_node("developer", developer_node)
    builder.add_node("qa", qa_node)
    builder.add_node("judge", judge_node)

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

    # Judge 이후: pass → END, fail → Developer 재작업
    builder.add_conditional_edges(
        "judge",
        route_after_judge,
        {
            "developer": "developer",
            "__end__": END,
        },
    )

    return builder


async def compile_graph(checkpointer=None):
    """그래프 컴파일. checkpointer가 있으면 interrupt 활성화."""
    builder = build_aads_graph()
    graph = builder.compile(checkpointer=checkpointer)
    return graph
