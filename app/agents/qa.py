"""
QA Agent — T-026/T-039: 디자인 검수 + 모바일 QA 통합.

검증 파이프라인:
  기존: 코드 테스트 → 판정
  T-026: 코드 테스트 → 스크린샷 촬영 → Visual Regression → LLM 디자인 감리 → 종합 판정
  T-039: project_type=mobile_android|mobile_ios → 모바일 QA 파이프라인 분기

qa_node()는 qa_agent.py의 기존 로직을 유지하면서
state에 deploy_url이 있을 경우 qa_pipeline.run_full_qa()를 추가 실행하고,
project_type이 mobile_*이면 mobile QA 파이프라인으로 분기한다.
"""
from __future__ import annotations

import structlog
from langchain_core.messages import AIMessage

from app.agents.qa_agent import qa_node as _base_qa_node
from app.graph.state import AADSState

logger = structlog.get_logger()


async def qa_node(state: AADSState) -> dict:
    """
    QA Agent 진입점 (T-026/T-039 통합 버전).

    project_type 분기:
      - web|video|image (기본): Visual Regression + LLM 감리
      - mobile_android: MobileQAService.full_qa() 호출
      - mobile_ios: Mac 연결 확인 후 동일 플로우, 미연결 시 skip+경고

    1. 기존 코드 테스트 실행 (qa_agent.qa_node)
    2. project_type 확인 후 분기
    3. 결과를 state에 병합하여 반환
    """
    logger.info("qa_node_t039_start")

    # Step 1: 기존 코드 테스트
    base_result = await _base_qa_node(state)

    # project_type 확인
    project_type = state.get("project_type", "web")
    project_id = state.get("project_id", "unknown")

    # ------------------------------------------------------------------
    # Mobile 분기 (T-039)
    # ------------------------------------------------------------------
    if project_type in ("mobile_android", "mobile_ios"):
        try:
            from app.services.qa_pipeline import run_mobile_qa
            mobile_result = await run_mobile_qa(state, project_type)

            merged = dict(base_result)
            merged["qa_full_result"] = mobile_result
            merged["qa_verdict"] = mobile_result.get("overall_verdict", mobile_result.get("status", "UNKNOWN"))
            merged["messages"] = base_result.get("messages", []) + [
                AIMessage(
                    content=(
                        f"모바일 QA 결과: {merged['qa_verdict']} "
                        f"(플랫폼: {project_type}, "
                        f"점수: {mobile_result.get('overall_score', 0)}/60)"
                    )
                )
            ]
            logger.info(
                "qa_node_mobile_done",
                project_type=project_type,
                verdict=merged["qa_verdict"],
            )
            return merged

        except Exception as e:
            logger.error("qa_node_mobile_pipeline_error", error=str(e))
            return base_result

    # ------------------------------------------------------------------
    # Web/기존 분기 (T-026)
    # ------------------------------------------------------------------
    deploy_url = state.get("deploy_url", "")
    if not deploy_url:
        logger.info("qa_node_t026_no_deploy_url_skip_visual")
        return base_result

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
