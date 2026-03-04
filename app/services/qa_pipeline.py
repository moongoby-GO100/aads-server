"""
QA Pipeline Service — T-026: 종합 QA 파이프라인.

run_full_qa(project_id, deploy_url, pages) → QAResult dict

5단계 파이프라인:
  Step 1: 기존 테스트 결과 수집 (unit test, API test)
  Step 2: Playwright 스크린샷 촬영
  Step 3: Visual Regression (baseline 비교)
  Step 4: LLM 디자인 감리 (스코어카드)
  Step 5: 종합 판정
    - 테스트 PASS + Visual PASS + 디자인 35+ → AUTO PASS
    - 테스트 PASS + (Visual diff있음 OR 디자인 25-34) → CEO 확인 요청 (CONDITIONAL)
    - 테스트 FAIL OR 디자인 24 이하 → AUTO FAIL
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

from app.services.visual_qa import visual_qa_service, BASELINES_DIR
from app.services.design_auditor import design_auditor

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# 판정 상수
# ---------------------------------------------------------------------------

VERDICT_AUTO_PASS = "AUTO PASS"
VERDICT_CONDITIONAL = "CEO 확인 요청"
VERDICT_AUTO_FAIL = "AUTO FAIL"


def _calc_verdict(
    test_status: str,
    visual_status: str,
    design_score: int,
) -> str:
    """
    종합 판정 로직.
    - 테스트 PASS + Visual PASS + 디자인 35+ → AUTO PASS
    - 테스트 PASS + (Visual diff있음 OR 디자인 25-34) → CEO 확인 요청
    - 테스트 FAIL OR 디자인 24 이하 → AUTO FAIL
    """
    if test_status == "FAIL" or design_score <= 24:
        return VERDICT_AUTO_FAIL

    if test_status == "PASS" and visual_status == "PASS" and design_score >= 35:
        return VERDICT_AUTO_PASS

    # test PASS but visual diff 있음 OR 25<=design<=34
    return VERDICT_CONDITIONAL


# ---------------------------------------------------------------------------
# 메인 파이프라인
# ---------------------------------------------------------------------------

async def run_full_qa(
    project_id: str,
    deploy_url: str,
    pages: List[str],
    existing_test_results: Optional[List[dict]] = None,
    project_context: str = "",
) -> Dict[str, Any]:
    """
    종합 QA 파이프라인 실행.

    Returns:
        {
            "project_id": str,
            "deploy_url": str,
            "pages": List[str],
            "test_status": "PASS" | "FAIL" | "SKIP",
            "visual_status": "PASS" | "DIFF" | "NO_BASELINE" | "ERROR",
            "visual_details": List[dict],
            "design_score": int,
            "design_verdict": str,
            "scorecard": dict | None,
            "screenshots": List[str],
            "diff_images": List[str],
            "verdict": "AUTO PASS" | "CEO 확인 요청" | "AUTO FAIL",
            "report_markdown": str,
            "executed_at": str,
        }
    """
    executed_at = datetime.utcnow().isoformat()
    logger.info("run_full_qa_start", project_id=project_id, deploy_url=deploy_url, pages=pages)

    result: Dict[str, Any] = {
        "project_id": project_id,
        "deploy_url": deploy_url,
        "pages": pages,
        "test_status": "SKIP",
        "visual_status": "NO_BASELINE",
        "visual_details": [],
        "design_score": 0,
        "design_verdict": "UNKNOWN",
        "scorecard": None,
        "screenshots": [],
        "diff_images": [],
        "verdict": VERDICT_AUTO_FAIL,
        "report_markdown": "",
        "executed_at": executed_at,
    }

    # ------------------------------------------------------------------
    # Step 1: 기존 테스트 결과 수집
    # ------------------------------------------------------------------
    test_results = existing_test_results or []
    if test_results:
        all_pass = all(r.get("status") == "pass" for r in test_results if r.get("status") != "skip")
        any_skip = all(r.get("status") == "skip" for r in test_results)
        result["test_status"] = "SKIP" if any_skip else ("PASS" if all_pass else "FAIL")
    else:
        result["test_status"] = "SKIP"
    logger.info("qa_pipeline_step1_tests", test_status=result["test_status"])

    # ------------------------------------------------------------------
    # Step 2: Playwright 스크린샷 촬영
    # ------------------------------------------------------------------
    screenshot_paths: List[str] = []
    try:
        screenshot_results = await visual_qa_service.capture_screenshots(
            base_url=deploy_url,
            pages=pages,
            project_id=project_id,
        )
        screenshot_paths = [r.path for r in screenshot_results if r.success and r.path]
        result["screenshots"] = screenshot_paths
        logger.info("qa_pipeline_step2_screenshots", count=len(screenshot_paths))
    except Exception as e:
        logger.error("qa_pipeline_step2_error", error=str(e))
        result["visual_status"] = "ERROR"
        result["verdict"] = _calc_verdict(
            result["test_status"], "ERROR", result["design_score"]
        )
        result["report_markdown"] = _build_report(result)
        return result

    if not screenshot_paths:
        logger.warning("qa_pipeline_no_screenshots")
        result["visual_status"] = "ERROR"
        result["verdict"] = _calc_verdict(
            result["test_status"], "ERROR", result["design_score"]
        )
        result["report_markdown"] = _build_report(result)
        return result

    # ------------------------------------------------------------------
    # Step 3: Visual Regression (baseline 비교)
    # ------------------------------------------------------------------
    visual_details: List[dict] = []
    has_diff = False
    has_no_baseline = False

    for sr in screenshot_results:
        if not sr.success or not sr.path:
            continue
        baseline_path = str(BASELINES_DIR / project_id / f"{sr.page_name}_baseline.png")
        if not Path(baseline_path).exists():
            visual_details.append({
                "page": sr.page,
                "page_name": sr.page_name,
                "status": "NO_BASELINE",
                "diff_percent": None,
                "diff_image_path": None,
            })
            has_no_baseline = True
            continue
        try:
            compare = await visual_qa_service.compare_with_baseline(sr.path, baseline_path)
            status = "PASS" if compare.match else "DIFF"
            if not compare.match:
                has_diff = True
                if compare.diff_image_path:
                    result["diff_images"].append(compare.diff_image_path)
            visual_details.append({
                "page": sr.page,
                "page_name": sr.page_name,
                "status": status,
                "diff_percent": compare.diff_percent,
                "diff_image_path": compare.diff_image_path,
                "error": compare.error,
            })
        except Exception as e:
            logger.warning("qa_pipeline_compare_error", page=sr.page, error=str(e))
            visual_details.append({
                "page": sr.page,
                "page_name": sr.page_name,
                "status": "ERROR",
                "diff_percent": None,
                "diff_image_path": None,
                "error": str(e),
            })

    result["visual_details"] = visual_details

    if has_no_baseline and not has_diff:
        result["visual_status"] = "NO_BASELINE"
    elif has_diff:
        result["visual_status"] = "DIFF"
    else:
        result["visual_status"] = "PASS"

    logger.info("qa_pipeline_step3_visual", visual_status=result["visual_status"])

    # ------------------------------------------------------------------
    # Step 4: LLM 디자인 감리
    # ------------------------------------------------------------------
    ctx = project_context or f"project_id={project_id}, deploy_url={deploy_url}"
    try:
        audit_results = await design_auditor.audit_multiple(
            screenshot_paths=screenshot_paths,
            project_context=ctx,
        )
        valid = [ar for ar in audit_results if ar.verdict != "ERROR"]
        if valid:
            avg_score = sum(ar.total_score for ar in valid) / len(valid)
            design_score = int(round(avg_score))
        else:
            design_score = 0

        # 첫 번째 감리 결과의 스코어카드를 대표로 사용
        first_valid = next((ar for ar in audit_results if ar.verdict != "ERROR"), None)
        if first_valid:
            result["scorecard"] = {
                "total_score": first_valid.total_score,
                "verdict": first_valid.verdict,
                "summary": first_valid.summary,
                "critical_issues": first_valid.critical_issues,
                "scores": {
                    k: {"score": v.score, "issues": v.issues, "fixes": v.fixes}
                    for k, v in first_valid.scores.items()
                },
            }
            result["design_verdict"] = first_valid.verdict
        else:
            result["design_verdict"] = "ERROR"

        result["design_score"] = design_score

        # 보고서
        try:
            result["report_markdown"] = await design_auditor.generate_report(audit_results)
        except Exception as e:
            logger.warning("qa_pipeline_report_error", error=str(e))
            result["report_markdown"] = f"보고서 생성 실패: {e}"

        logger.info(
            "qa_pipeline_step4_design",
            design_score=design_score,
            design_verdict=result["design_verdict"],
        )
    except Exception as e:
        logger.error("qa_pipeline_step4_error", error=str(e))
        result["design_score"] = 0
        result["design_verdict"] = "ERROR"

    # ------------------------------------------------------------------
    # Step 5: 종합 판정
    # ------------------------------------------------------------------
    # test_status가 SKIP이면 테스트 조건을 PASS로 간주
    effective_test_status = "PASS" if result["test_status"] in ("PASS", "SKIP") else "FAIL"
    result["verdict"] = _calc_verdict(
        effective_test_status,
        result["visual_status"],
        result["design_score"],
    )
    logger.info(
        "qa_pipeline_step5_verdict",
        verdict=result["verdict"],
        test=result["test_status"],
        visual=result["visual_status"],
        design=result["design_score"],
    )

    # Context API 저장
    await _save_to_context(project_id, result)

    return result


# ---------------------------------------------------------------------------
# 모바일 QA 파이프라인 (T-039)
# ---------------------------------------------------------------------------


async def run_mobile_qa(state: dict, project_type: str) -> dict:
    """
    모바일 QA 파이프라인 실행.

    project_type:
      - mobile_android: Android 에뮬레이터 + Appium + Gemini Vision
      - mobile_ios: Mac 연결 필요, 미연결 시 skip

    Returns:
        mobile QA 결과 dict (overall_verdict, overall_score, ...)
    """
    import os

    package_name = state.get("package_name", state.get("project_id", "unknown"))
    apk_url = state.get("apk_url", "")
    activity_name = state.get("activity_name", ".MainActivity")
    scenarios = state.get("qa_scenarios", [])

    logger.info(
        "run_mobile_qa_start",
        project_type=project_type,
        package_name=package_name,
    )

    # iOS: Mac 연결 여부 확인
    if project_type == "mobile_ios":
        ios_appium_url = os.getenv("IOS_APPIUM_URL", "")
        if not ios_appium_url:
            logger.warning("run_mobile_qa_ios_not_connected")
            return {
                "status": "skipped",
                "reason": "iOS Mac not connected. Set IOS_APPIUM_URL environment variable.",
                "project_type": project_type,
                "package_name": package_name,
                "overall_verdict": "CEO 확인 요청",
                "overall_score": 0,
            }

    # Android: MobileQAService 사용
    try:
        from app.services.mobile_qa import mobile_qa_service
        from app.services.design_auditor import design_auditor
        from pathlib import Path
        import tempfile
        import base64

        # Appium 상태 확인
        appium_status = mobile_qa_service.check_appium_server()
        if appium_status != "running":
            logger.error("run_mobile_qa_appium_not_running")
            return {
                "status": "error",
                "reason": "Appium server is not running",
                "project_type": project_type,
                "package_name": package_name,
                "overall_verdict": "AUTO FAIL",
                "overall_score": 0,
            }

        result = {
            "project_type": project_type,
            "package_name": package_name,
            "install_success": False,
            "scenario_results": [],
            "audit_results": [],
            "overall_verdict": "AUTO FAIL",
            "overall_score": 0.0,
            "crash_detected": False,
        }

        if not apk_url:
            result["reason"] = "apk_url이 state에 없습니다"
            return result

        with tempfile.TemporaryDirectory() as tmpdir:
            import os as _os
            apk_path = _os.path.join(tmpdir, "app.apk")
            downloaded = mobile_qa_service.download_apk(apk_url, apk_path)
            if not downloaded:
                result["reason"] = f"APK 다운로드 실패: {apk_url}"
                return result

            appium_url = os.getenv("APPIUM_URL", "http://localhost:4723")
            if project_type == "mobile_ios":
                appium_url = os.getenv("IOS_APPIUM_URL", appium_url)

            driver = None
            try:
                if project_type == "mobile_android":
                    driver = mobile_qa_service.connect_android()
                else:
                    driver = mobile_qa_service.connect_ios(
                        platform_version=os.getenv("IOS_PLATFORM_VERSION", "17.0"),
                        device_name=os.getenv("IOS_DEVICE_NAME", "iPhone 15"),
                    )

                install_ok = mobile_qa_service.install_and_launch(
                    driver, apk_path, package_name, activity_name
                )
                result["install_success"] = install_ok

                if install_ok:
                    import time
                    time.sleep(2)

                    screenshot_dir = Path("/tmp/mobile_qa_screenshots")
                    screenshot_dir.mkdir(parents=True, exist_ok=True)
                    from datetime import datetime as _dt
                    ts = _dt.utcnow().strftime("%Y%m%d_%H%M%S")

                    # 시나리오 실행
                    if scenarios:
                        augmented = []
                        for i, step in enumerate(scenarios):
                            augmented.append(step)
                            augmented.append({
                                "action": "screenshot",
                                "path": str(screenshot_dir / f"{ts}_step{i}.png"),
                            })
                        result["scenario_results"] = mobile_qa_service.run_test_scenario(driver, augmented)
                    else:
                        init_shot = str(screenshot_dir / f"{ts}_initial.png")
                        mobile_qa_service.take_screenshot(driver, init_shot)
                        result["scenario_results"] = [{"action": "initial_screenshot", "screenshot": init_shot, "success": True}]

                    # 크래시 감지
                    crash_info = mobile_qa_service.check_crash(driver, package_name)
                    result["crash_detected"] = crash_info["crashed"]

                    # 화면 감리
                    platform_name = "android" if project_type == "mobile_android" else "ios"
                    audit_results = []
                    for step_r in result["scenario_results"]:
                        shot = step_r.get("screenshot")
                        if shot and Path(shot).exists():
                            audit = await design_auditor.audit_mobile_screen(shot, platform=platform_name)
                            audit_results.append(audit)
                    result["audit_results"] = audit_results

                    # 종합 판정
                    valid = [a for a in audit_results if a.get("verdict") not in ("ERROR", None)]
                    if valid:
                        avg_score = sum(a["total_score"] for a in valid) / len(valid)
                        result["overall_score"] = round(avg_score, 1)
                        if crash_info["crashed"]:
                            result["overall_verdict"] = VERDICT_AUTO_FAIL
                        elif avg_score >= 48:
                            result["overall_verdict"] = VERDICT_AUTO_PASS
                        elif avg_score >= 36:
                            result["overall_verdict"] = VERDICT_CONDITIONAL
                        else:
                            result["overall_verdict"] = VERDICT_AUTO_FAIL
                    else:
                        result["overall_verdict"] = VERDICT_AUTO_FAIL if crash_info["crashed"] else VERDICT_CONDITIONAL

            finally:
                if driver:
                    mobile_qa_service.close(driver)

        return result

    except Exception as e:
        logger.error("run_mobile_qa_error", error=str(e))
        return {
            "status": "error",
            "reason": str(e),
            "project_type": project_type,
            "package_name": package_name,
            "overall_verdict": "AUTO FAIL",
            "overall_score": 0,
        }


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------

async def _save_to_context(project_id: str, qa_result: Dict[str, Any]) -> None:
    """QA 결과를 Context API (system_memory)에 저장."""
    try:
        from app.memory.store import memory_store

        await memory_store.put_system(
            category="qa_results",
            key=f"{project_id}_latest",
            value={
                "project_id": project_id,
                "verdict": qa_result.get("verdict"),
                "test_status": qa_result.get("test_status"),
                "visual_status": qa_result.get("visual_status"),
                "design_score": qa_result.get("design_score"),
                "design_verdict": qa_result.get("design_verdict"),
                "executed_at": qa_result.get("executed_at"),
                "deploy_url": qa_result.get("deploy_url"),
            },
            updated_by="qa_pipeline",
        )
        logger.info("qa_pipeline_context_saved", project_id=project_id)
    except Exception as e:
        logger.warning("qa_pipeline_context_save_failed", error=str(e))


def _build_report(result: Dict[str, Any]) -> str:
    """간단한 텍스트 보고서."""
    lines = [
        "# QA 종합 보고서",
        f"- project_id: {result.get('project_id')}",
        f"- deploy_url: {result.get('deploy_url')}",
        f"- 테스트: {result.get('test_status')}",
        f"- Visual: {result.get('visual_status')}",
        f"- 디자인 점수: {result.get('design_score')}/50",
        f"- **최종 판정: {result.get('verdict')}**",
        f"- 실행 시각: {result.get('executed_at')}",
    ]
    return "\n".join(lines)
