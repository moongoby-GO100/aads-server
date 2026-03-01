"""
LangGraph StateGraph 빌드.
⚠️ langgraph-supervisor 사용 금지 (R-010).
⚠️ Native StateGraph만 사용.
"""
from langgraph.graph import StateGraph, START, END

from app.graph.state import AADSState
from app.graph.routing import route_after_pm, route_after_developer
from app.agents.pm import pm_requirements_node
from app.agents.supervisor import supervisor_node
from app.agents.developer import developer_node


def build_aads_graph() -> StateGraph:
    """
    Week 1 그래프: PM → Supervisor → Developer

    START → pm_requirements → [route] → supervisor → developer → [route] → END
                    ↑__________________|  (revision loop)
    """
    builder = StateGraph(AADSState)

    # 노드 등록
    builder.add_node("pm_requirements", pm_requirements_node)
    builder.add_node("supervisor", supervisor_node)
    builder.add_node("developer", developer_node)

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

    # supervisor는 Command(goto=...)로 직접 라우팅하므로
    # 별도 conditional edge 불필요

    # Developer 이후 조건부 라우팅
    builder.add_conditional_edges(
        "developer",
        route_after_developer,
        {
            "supervisor": "supervisor",
            "__end__": END,
        },
    )

    return builder


async def compile_graph(checkpointer=None):
    """그래프 컴파일. checkpointer가 있으면 interrupt 활성화."""
    builder = build_aads_graph()
    graph = builder.compile(checkpointer=checkpointer)
    return graph
