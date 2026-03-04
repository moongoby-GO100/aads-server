"""
Mobile QA API — T-039: Android 에뮬레이터 + Appium + Gemini Vision 감리.

라우터: /api/v1/mobile-qa
엔드포인트:
  GET  /mobile-qa/health           → 에뮬레이터/Appium 상태
  POST /mobile-qa/install          → APK 다운로드 + 설치 + 실행 + 스크린샷
  POST /mobile-qa/test-scenario    → 시나리오 실행 + 각 단계 스크린샷
  POST /mobile-qa/audit-screen     → Gemini Vision 6항목 감리
  POST /mobile-qa/full-qa          → 설치→시나리오→감리→판정→알림
"""
from __future__ import annotations

import base64
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.mobile_qa import mobile_qa_service
from app.services.design_auditor import design_auditor

logger = structlog.get_logger()

router = APIRouter(prefix="/mobile-qa", tags=["mobile-qa"])

# ---------------------------------------------------------------------------
# 요청/응답 스키마
# ---------------------------------------------------------------------------


class InstallRequest(BaseModel):
    apk_url: str = Field(..., description="APK 다운로드 URL")
    package_name: str = Field(..., description="Android 패키지명")
    activity_name: str = Field(..., description="실행할 Activity명 (예: .MainActivity)")


class TestScenarioRequest(BaseModel):
    package_name: str = Field(..., description="Android 패키지명")
    scenario: List[Dict[str, Any]] = Field(..., description="실행할 시나리오 단계 목록")
    capture_screenshots: bool = Field(True, description="각 단계마다 스크린샷 캡처 여부")


class AuditScreenRequest(BaseModel):
    screenshot_base64: str = Field(..., description="스크린샷 base64 인코딩 문자열")
    platform: str = Field("android", description="플랫폼: android 또는 ios")


class FullQARequest(BaseModel):
    apk_url: str = Field(..., description="APK 다운로드 URL")
    package_name: str = Field(..., description="Android 패키지명")
    activity_name: str = Field(..., description="실행할 Activity명")
    scenarios: List[Dict[str, Any]] = Field(default_factory=list, description="QA 시나리오 목록")
    platform: str = Field("android", description="플랫폼: android 또는 ios")


# ---------------------------------------------------------------------------
# 엔드포인트
# ---------------------------------------------------------------------------


@router.get("/health")
async def mobile_qa_health():
    """
    Android 에뮬레이터 연결 상태, Appium 서버 상태 반환.

    Returns:
        {
          "android_emulator": "connected"|"not_available"|"adb_not_installed",
          "appium": "running"|"stopped",
          "ios": "not_configured",
          "kvm_available": bool,
          "timestamp": str
        }
    """
    android_status = mobile_qa_service.check_android_emulator()
    appium_status = mobile_qa_service.check_appium_server()

    # KVM 가용성 확인
    kvm_available = Path("/dev/kvm").exists()

    # iOS: IOS_APPIUM_URL 설정 여부 확인
    ios_appium_url = os.getenv("IOS_APPIUM_URL", "")
    ios_status = "configured" if ios_appium_url else "not_configured"

    logger.info(
        "mobile_qa_health_check",
        android=android_status,
        appium=appium_status,
        kvm=kvm_available,
    )

    return {
        "android_emulator": android_status,
        "appium": appium_status,
        "ios": ios_status,
        "kvm_available": kvm_available,
        "timestamp": datetime.utcnow().isoformat(),
    }


@router.post("/install")
async def install_apk(req: InstallRequest):
    """
    APK 다운로드 → ADB 설치 → 앱 실행 → 스크린샷 캡처.

    Returns:
        {
          "success": bool,
          "package_name": str,
          "screenshot_base64": str | None,
          "screenshot_path": str | None,
          "error": str | None
        }
    """
    logger.info("mobile_qa_install_request", apk_url=req.apk_url, package=req.package_name)

    # Appium 서버 상태 확인
    appium_status = mobile_qa_service.check_appium_server()
    if appium_status != "running":
        raise HTTPException(status_code=503, detail="Appium server is not running")

    # 임시 디렉토리에 APK 다운로드
    with tempfile.TemporaryDirectory() as tmpdir:
        apk_path = os.path.join(tmpdir, "app.apk")
        downloaded = mobile_qa_service.download_apk(req.apk_url, apk_path)
        if not downloaded:
            raise HTTPException(status_code=400, detail=f"APK 다운로드 실패: {req.apk_url}")

        # Android 연결
        try:
            driver = mobile_qa_service.connect_android()
        except Exception as e:
            logger.error("mobile_qa_install_connect_failed", error=str(e))
            raise HTTPException(status_code=503, detail=f"Android 에뮬레이터 연결 실패: {e}")

        try:
            # 설치 및 실행
            success = mobile_qa_service.install_and_launch(
                driver,
                apk_path,
                req.package_name,
                req.activity_name,
            )

            # 스크린샷 캡처
            screenshot_path = None
            screenshot_base64 = None
            if success:
                import time
                time.sleep(2)  # 앱 로딩 대기
                screenshot_dir = Path("/tmp/mobile_qa_screenshots")
                screenshot_dir.mkdir(parents=True, exist_ok=True)
                ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
                screenshot_path = str(screenshot_dir / f"{req.package_name}_{ts}.png")
                saved = mobile_qa_service.take_screenshot(driver, screenshot_path)
                if saved:
                    with open(saved, "rb") as f:
                        screenshot_base64 = base64.b64encode(f.read()).decode("utf-8")

            return {
                "success": success,
                "package_name": req.package_name,
                "screenshot_base64": screenshot_base64,
                "screenshot_path": screenshot_path,
                "error": None,
            }
        finally:
            mobile_qa_service.close(driver)


@router.post("/test-scenario")
async def run_test_scenario(req: TestScenarioRequest):
    """
    시나리오 실행 — 각 단계별 스크린샷+결과+크래시 감지.

    Returns:
        {
          "package_name": str,
          "steps": list[dict],
          "crash_detected": bool,
          "crash_log": str,
          "total_steps": int,
          "passed_steps": int,
          "executed_at": str
        }
    """
    logger.info("mobile_qa_scenario_request", package=req.package_name, steps=len(req.scenario))

    # Appium 상태 확인
    appium_status = mobile_qa_service.check_appium_server()
    if appium_status != "running":
        raise HTTPException(status_code=503, detail="Appium server is not running")

    try:
        driver = mobile_qa_service.connect_android()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Android 에뮬레이터 연결 실패: {e}")

    try:
        # 스크린샷 경로 설정
        scenario = list(req.scenario)
        if req.capture_screenshots:
            screenshot_dir = Path("/tmp/mobile_qa_screenshots")
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            for i, step in enumerate(scenario):
                if step.get("action") not in ("screenshot",):
                    # 각 단계 후 자동 스크린샷 추가
                    pass  # run_test_scenario에서 처리

        # 시나리오 실행
        results = mobile_qa_service.run_test_scenario(driver, scenario)

        # 스크린샷 base64 인코딩 추가
        for step_result in results:
            screenshot_path = step_result.get("screenshot")
            if screenshot_path and Path(screenshot_path).exists():
                with open(screenshot_path, "rb") as f:
                    step_result["screenshot_base64"] = base64.b64encode(f.read()).decode("utf-8")
            else:
                step_result["screenshot_base64"] = None

        # 크래시 감지
        crash_info = mobile_qa_service.check_crash(driver, req.package_name)
        passed = sum(1 for r in results if r.get("success"))

        return {
            "package_name": req.package_name,
            "steps": results,
            "crash_detected": crash_info["crashed"],
            "crash_log": crash_info["log"],
            "total_steps": len(results),
            "passed_steps": passed,
            "executed_at": datetime.utcnow().isoformat(),
        }
    finally:
        mobile_qa_service.close(driver)


@router.post("/audit-screen")
async def audit_screen(req: AuditScreenRequest):
    """
    Gemini Vision 모바일 스크린샷 6항목 감리 → PASS/CONDITIONAL/FAIL.

    Returns:
        {
          "platform": str,
          "scores": dict,
          "total_score": int,
          "verdict": str,
          "summary": str,
          "critical_issues": list,
          "provider_used": str
        }
    """
    logger.info("mobile_qa_audit_request", platform=req.platform)

    if not req.screenshot_base64:
        raise HTTPException(status_code=400, detail="screenshot_base64가 비어 있습니다")

    result = await design_auditor.audit_mobile_screen(
        screenshot_path_or_base64=req.screenshot_base64,
        platform=req.platform,
        is_base64=True,
    )

    if result.get("verdict") == "ERROR":
        raise HTTPException(status_code=500, detail=result.get("error", "감리 실패"))

    return result


@router.post("/full-qa")
async def full_mobile_qa(req: FullQARequest):
    """
    전체 모바일 QA 파이프라인:
    설치 → 시나리오 실행 → 각 화면 감리 → 종합 판정 → Context API 저장 → CEO 알림

    Returns:
        {
          "package_name": str,
          "install_success": bool,
          "scenario_results": list,
          "audit_results": list,
          "overall_verdict": str,
          "overall_score": float,
          "crash_detected": bool,
          "executed_at": str
        }
    """
    logger.info("mobile_qa_full_qa_request", package=req.package_name, platform=req.platform)

    # iOS 처리
    if req.platform == "mobile_ios" or req.platform == "ios":
        ios_appium_url = os.getenv("IOS_APPIUM_URL", "")
        if not ios_appium_url:
            return {
                "status": "skipped",
                "reason": "iOS Mac not connected. Set IOS_APPIUM_URL environment variable.",
                "package_name": req.package_name,
                "platform": req.platform,
                "executed_at": datetime.utcnow().isoformat(),
            }

    # Appium 상태 확인
    appium_status = mobile_qa_service.check_appium_server()
    if appium_status != "running":
        raise HTTPException(status_code=503, detail="Appium server is not running")

    result: Dict[str, Any] = {
        "package_name": req.package_name,
        "platform": req.platform,
        "install_success": False,
        "scenario_results": [],
        "audit_results": [],
        "overall_verdict": "AUTO FAIL",
        "overall_score": 0.0,
        "crash_detected": False,
        "crash_log": "",
        "executed_at": datetime.utcnow().isoformat(),
    }

    # Step 1: APK 다운로드
    with tempfile.TemporaryDirectory() as tmpdir:
        apk_path = os.path.join(tmpdir, "app.apk")
        downloaded = mobile_qa_service.download_apk(req.apk_url, apk_path)
        if not downloaded:
            result["error"] = f"APK 다운로드 실패: {req.apk_url}"
            return result

        # Step 2: Android 에뮬레이터 연결
        try:
            driver = mobile_qa_service.connect_android()
        except Exception as e:
            result["error"] = f"에뮬레이터 연결 실패: {e}"
            return result

        try:
            # Step 3: 설치 및 실행
            install_ok = mobile_qa_service.install_and_launch(
                driver, apk_path, req.package_name, req.activity_name
            )
            result["install_success"] = install_ok

            if not install_ok:
                result["error"] = "APK 설치/실행 실패"
                return result

            import time
            time.sleep(2)  # 앱 로딩 대기

            # Step 4: 시나리오 실행 + 각 단계 스크린샷
            screenshot_dir = Path("/tmp/mobile_qa_screenshots")
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            ts_prefix = datetime.utcnow().strftime("%Y%m%d_%H%M%S")

            # 시나리오에 자동 스크린샷 삽입
            augmented_scenario = []
            for i, step in enumerate(req.scenarios):
                augmented_scenario.append(step)
                augmented_scenario.append({
                    "action": "screenshot",
                    "path": str(screenshot_dir / f"{ts_prefix}_step{i}.png"),
                })

            if augmented_scenario:
                scenario_results = mobile_qa_service.run_test_scenario(driver, augmented_scenario)
                result["scenario_results"] = scenario_results
            else:
                # 시나리오 없으면 초기 화면 스크린샷만
                init_shot = str(screenshot_dir / f"{ts_prefix}_initial.png")
                mobile_qa_service.take_screenshot(driver, init_shot)
                result["scenario_results"] = [{"action": "initial_screenshot", "screenshot": init_shot, "success": True}]

            # Step 5: 크래시 감지
            crash_info = mobile_qa_service.check_crash(driver, req.package_name)
            result["crash_detected"] = crash_info["crashed"]
            result["crash_log"] = crash_info["log"]

            # Step 6: 각 화면 Gemini Vision 감리
            audit_results = []
            for step_r in result["scenario_results"]:
                shot_path = step_r.get("screenshot")
                if shot_path and Path(shot_path).exists():
                    audit = await design_auditor.audit_mobile_screen(shot_path, platform=req.platform)
                    audit_results.append(audit)

            result["audit_results"] = audit_results

            # Step 7: 종합 판정
            valid_audits = [a for a in audit_results if a.get("verdict") not in ("ERROR", None)]
            if valid_audits:
                avg_score = sum(a["total_score"] for a in valid_audits) / len(valid_audits)
                result["overall_score"] = round(avg_score, 1)
                if crash_info["crashed"]:
                    result["overall_verdict"] = "AUTO FAIL"
                elif avg_score >= 48:
                    result["overall_verdict"] = "AUTO PASS"
                elif avg_score >= 36:
                    result["overall_verdict"] = "CEO 확인 요청"
                else:
                    result["overall_verdict"] = "AUTO FAIL"
            else:
                result["overall_verdict"] = "AUTO FAIL" if crash_info["crashed"] else "CEO 확인 요청"

            # Step 8: Context API 저장
            try:
                from app.memory.store import memory_store
                await memory_store.put_system(
                    category="mobile_qa_results",
                    key=f"{req.package_name}_latest",
                    value={
                        "package_name": req.package_name,
                        "platform": req.platform,
                        "verdict": result["overall_verdict"],
                        "overall_score": result["overall_score"],
                        "crash_detected": result["crash_detected"],
                        "executed_at": result["executed_at"],
                    },
                    updated_by="mobile_qa_pipeline",
                )
            except Exception as e:
                logger.warning("mobile_qa_context_save_failed", error=str(e))

            # Step 9: CEO 알림
            try:
                from app.services.ceo_notify import notify_ceo
                await notify_ceo(
                    project_id=req.package_name,
                    qa_result=result,
                    screenshots=[
                        s.get("screenshot") for s in result["scenario_results"]
                        if s.get("screenshot")
                    ],
                    scorecard={
                        "total_score": result["overall_score"],
                        "verdict": result["overall_verdict"],
                        "summary": f"모바일 QA 완료: {req.package_name}",
                        "critical_issues": crash_info["log"].splitlines()[:3] if crash_info["log"] else [],
                    },
                )
            except Exception as e:
                logger.warning("mobile_qa_ceo_notify_failed", error=str(e))

            logger.info(
                "mobile_qa_full_qa_done",
                package=req.package_name,
                verdict=result["overall_verdict"],
                score=result["overall_score"],
            )
            return result

        finally:
            mobile_qa_service.close(driver)
