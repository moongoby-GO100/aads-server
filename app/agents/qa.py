"""
QA Agent — T-026: 디자인 검수 단계 통합.

검증 파이프라인:
  기존: 코드 테스트 → 판정
  변경: 코드 테스트 → 스크린샷 촬영 → Visual Regression → LLM 디자인 감리 → 종합 판정

qa_node()는 qa_agent.py의 기존 로직을 유지하면서
state에 deploy_url이 있을 경우 qa_pipeline.run_full_qa()를 추가 실행한다.
"""
from __future__ import annotations

import structlog
from langchain_core.messages import AIMessage

from app.agents.qa_agent import qa_node as _base_qa_node
from app.graph.state import AADSState

logger = structlog.get_logger()


async def qa_node(state: AADSState) -> dict:
    """
    QA Agent 진입점 (T-026 통합 버전).

    1. 기존 코드 테스트 실행 (qa_agent.qa_node)
    2. deploy_url이 state에 있으면 full QA 파이프라인 실행
       - Visual Regression
       - LLM 디자인 감리
       - 종합 판정 (AUTO PASS / CEO 확인 요청 / AUTO FAIL)
    3. 결과를 state에 병합하여 반환
    """
    logger.info("qa_node_t026_start")

    # Step 1: 기존 코드 테스트
    base_result = await _base_qa_node(state)

    # deploy_url 없으면 기존 결과만 반환
    deploy_url = state.get("deploy_url", "")
    if not deploy_url:
        logger.info("qa_node_t026_no_deploy_url_skip_visual")
        return base_result

    # Step 2: Visual + Design QA
    project_id = state.get("project_id", "unknown")
    pages = state.get("qa_pages", ["/"])

    try:
        from app.services.qa_pipeline import run_full_qa

        qa_result = await run_full_qa(
            project_id=project_id,
            deploy_url=deploy_url,
            pages=pages,
            existing_test_results=base_result.get("qa_test_results", []),
        )

        # CEO 알림 (notify_ceo=True 기본)
        try:
            from app.services.ceo_notify import notify_ceo
            await notify_ceo(
                project_id=project_id,
                qa_result=qa_result,
                screenshots=qa_result.get("screenshots", []),
                scorecard=qa_result.get("scorecard"),
            )
        except Exception as e:
            logger.warning("qa_node_ceo_notify_failed", error=str(e))

        # base_result에 병합
        merged = dict(base_result)
        merged["qa_full_result"] = qa_result
        merged["qa_verdict"] = qa_result.get("verdict", "UNKNOWN")
        merged["messages"] = base_result.get("messages", []) + [
            AIMessage(
                content=(
                    f"QA 종합 판정: {qa_result.get('verdict')} "
                    f"(테스트: {qa_result.get('test_status')}, "
                    f"Visual: {qa_result.get('visual_status')}, "
                    f"디자인: {qa_result.get('design_score')}/50)"
                )
            )
        ]
        logger.info(
            "qa_node_t026_done",
            verdict=qa_result.get("verdict"),
            design_score=qa_result.get("design_score"),
        )
        return merged

    except Exception as e:
        logger.error("qa_node_t026_pipeline_error", error=str(e))
        # 파이프라인 실패해도 기존 결과는 유지
        return base_result
