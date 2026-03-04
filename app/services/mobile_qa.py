"""
Mobile QA Service — T-039: Android 에뮬레이터 + Appium + iOS 사전 준비.

MobileQAService 클래스:
  - connect_android(platform_version, device_name) → Appium driver
  - connect_ios(platform_version, device_name, udid, wda_port) → Appium driver (Mac 연결 시)
  - install_and_launch(driver, apk_or_ipa_path, package_name, activity_name) → bool
  - take_screenshot(driver, save_path) → str
  - tap_element(driver, accessibility_id, xpath, text) → bool
  - scroll_down(driver) → bool
  - input_text(driver, element_locator, text) → bool
  - get_page_source(driver) → str
  - check_crash(driver, package_name) → dict
  - run_test_scenario(driver, scenario) → list[dict]
  - close(driver) → None
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()


class MobileQAService:
    """Android/iOS 모바일 QA 서비스 (Appium 기반)."""

    def __init__(
        self,
        appium_url: str = "http://localhost:4723",
        adb_host: str = "localhost",
        adb_port: int = 5555,
    ):
        self.appium_url = appium_url
        self.adb_host = adb_host
        self.adb_port = adb_port

    # ------------------------------------------------------------------
    # 연결 메서드
    # ------------------------------------------------------------------

    def connect_android(
        self,
        platform_version: str = "13",
        device_name: str = "emulator-5554",
    ):
        """
        Android 에뮬레이터에 Appium driver 연결.

        Returns:
            Appium WebDriver instance
        Raises:
            ImportError: Appium-Python-Client 미설치 시
            Exception: 연결 실패 시
        """
        try:
            from appium import webdriver as appium_webdriver
            from appium.options import UiAutomator2Options
        except ImportError as e:
            raise ImportError(
                "Appium-Python-Client가 설치되지 않았습니다. "
                "`pip install Appium-Python-Client`를 실행하세요."
            ) from e

        options = UiAutomator2Options()
        options.platform_name = "Android"
        options.platform_version = platform_version
        options.device_name = device_name
        options.automation_name = "UiAutomator2"
        options.no_reset = True
        options.auto_grant_permissions = True

        logger.info(
            "mobile_qa_connect_android",
            appium_url=self.appium_url,
            device_name=device_name,
            platform_version=platform_version,
        )
        driver = appium_webdriver.Remote(self.appium_url, options=options)
        logger.info("mobile_qa_android_connected", session_id=driver.session_id)
        return driver

    def connect_ios(
        self,
        platform_version: str = "17.0",
        device_name: str = "iPhone 15",
        udid: Optional[str] = None,
        wda_local_port: int = 8100,
    ):
        """
        iOS 기기/시뮬레이터에 Appium driver 연결.

        Mac이 연결되지 않은 환경에서는 NotImplementedError를 발생시킵니다.
        Mac mini 또는 클라우드 Mac 연결 시 정상 동작합니다.

        Args:
            platform_version: iOS 버전 (예: "17.0")
            device_name: 기기 이름 (예: "iPhone 15")
            udid: 실기기 UDID (시뮬레이터는 None)
            wda_local_port: WebDriverAgent 포트

        Returns:
            Appium WebDriver instance (Mac 연결 시)

        Raises:
            NotImplementedError: Mac 미연결 환경
        """
        # Mac 연결 여부 확인 (IOS_APPIUM_URL 환경변수가 없으면 Mac 미연결)
        ios_appium_url = os.getenv("IOS_APPIUM_URL", "")
        if not ios_appium_url:
            raise NotImplementedError(
                "iOS requires Mac connection. See AADS docs for setup. "
                "Set IOS_APPIUM_URL environment variable to enable iOS testing. "
                "Refer to /root/aads/aads-docs/docs/IOS-QA-SETUP.md"
            )

        try:
            from appium import webdriver as appium_webdriver
            from appium.options import XCUITestOptions
        except ImportError as e:
            raise ImportError(
                "Appium-Python-Client가 설치되지 않았습니다. "
                "`pip install Appium-Python-Client`를 실행하세요."
            ) from e

        options = XCUITestOptions()
        options.platform_name = "iOS"
        options.platform_version = platform_version
        options.device_name = device_name
        options.automation_name = "XCUITest"
        options.wda_local_port = wda_local_port
        if udid:
            options.udid = udid

        actual_url = ios_appium_url or self.appium_url
        logger.info(
            "mobile_qa_connect_ios",
            appium_url=actual_url,
            device_name=device_name,
            platform_version=platform_version,
            udid=udid,
        )
        driver = appium_webdriver.Remote(actual_url, options=options)
        logger.info("mobile_qa_ios_connected", session_id=driver.session_id)
        return driver

    # ------------------------------------------------------------------
    # 앱 조작 메서드
    # ------------------------------------------------------------------

    def install_and_launch(
        self,
        driver,
        apk_or_ipa_path: str,
        package_name: str,
        activity_name: str,
    ) -> bool:
        """
        APK/IPA 설치 후 앱 실행.

        Args:
            driver: Appium driver instance
            apk_or_ipa_path: 로컬 APK/IPA 파일 경로
            package_name: Android 패키지명 또는 iOS bundle ID
            activity_name: Android 액티비티명 (iOS는 무시됨)

        Returns:
            True if successful, False otherwise
        """
        try:
            logger.info(
                "mobile_qa_install_app",
                path=apk_or_ipa_path,
                package=package_name,
            )
            driver.install_app(apk_or_ipa_path)
            logger.info("mobile_qa_app_installed", package=package_name)

            # 앱 실행
            platform = driver.capabilities.get("platformName", "").lower()
            if platform == "android":
                driver.activate_app(package_name)
            else:
                driver.activate_app(package_name)

            logger.info("mobile_qa_app_launched", package=package_name)
            return True
        except Exception as e:
            logger.error("mobile_qa_install_failed", error=str(e))
            return False

    def take_screenshot(self, driver, save_path: str) -> str:
        """
        스크린샷 캡처 후 파일 저장.

        Returns:
            저장된 파일 경로
        """
        try:
            Path(save_path).parent.mkdir(parents=True, exist_ok=True)
            driver.save_screenshot(save_path)
            logger.info("mobile_qa_screenshot_saved", path=save_path)
            return save_path
        except Exception as e:
            logger.error("mobile_qa_screenshot_failed", error=str(e))
            return ""

    def tap_element(
        self,
        driver,
        accessibility_id: Optional[str] = None,
        xpath: Optional[str] = None,
        text: Optional[str] = None,
    ) -> bool:
        """
        화면 요소 탭.

        Args:
            driver: Appium driver
            accessibility_id: Accessibility ID (우선순위 1)
            xpath: XPath 선택자 (우선순위 2)
            text: 텍스트로 요소 찾기 (우선순위 3)

        Returns:
            True if tapped, False otherwise
        """
        try:
            from appium.webdriver.common.appiumby import AppiumBy
            from selenium.webdriver.common.by import By

            element = None
            if accessibility_id:
                element = driver.find_element(AppiumBy.ACCESSIBILITY_ID, accessibility_id)
            elif xpath:
                element = driver.find_element(AppiumBy.XPATH, xpath)
            elif text:
                element = driver.find_element(
                    AppiumBy.XPATH, f'//*[@text="{text}" or @label="{text}"]'
                )

            if element:
                element.click()
                logger.info("mobile_qa_tap_success", accessibility_id=accessibility_id, xpath=xpath, text=text)
                return True
            return False
        except Exception as e:
            logger.warning("mobile_qa_tap_failed", error=str(e))
            return False

    def scroll_down(self, driver) -> bool:
        """화면 아래로 스크롤."""
        try:
            size = driver.get_window_size()
            width = size["width"]
            height = size["height"]
            driver.swipe(
                start_x=width // 2,
                start_y=int(height * 0.7),
                end_x=width // 2,
                end_y=int(height * 0.3),
                duration=500,
            )
            logger.info("mobile_qa_scroll_down")
            return True
        except Exception as e:
            logger.warning("mobile_qa_scroll_failed", error=str(e))
            return False

    def input_text(self, driver, element_locator: str, text: str) -> bool:
        """
        텍스트 입력.

        Args:
            driver: Appium driver
            element_locator: accessibility_id 또는 xpath
            text: 입력할 텍스트

        Returns:
            True if successful
        """
        try:
            from appium.webdriver.common.appiumby import AppiumBy

            # accessibility_id 먼저 시도, 실패 시 xpath
            try:
                element = driver.find_element(AppiumBy.ACCESSIBILITY_ID, element_locator)
            except Exception:
                element = driver.find_element(AppiumBy.XPATH, element_locator)

            element.clear()
            element.send_keys(text)
            logger.info("mobile_qa_input_text", locator=element_locator)
            return True
        except Exception as e:
            logger.warning("mobile_qa_input_failed", error=str(e))
            return False

    def get_page_source(self, driver) -> str:
        """UI XML 계층 구조 반환."""
        try:
            source = driver.page_source
            logger.info("mobile_qa_page_source_obtained", length=len(source))
            return source
        except Exception as e:
            logger.error("mobile_qa_page_source_failed", error=str(e))
            return ""

    def check_crash(self, driver, package_name: str) -> Dict[str, Any]:
        """
        앱 크래시 여부 확인.

        Returns:
            {"crashed": bool, "log": str}
        """
        try:
            platform = driver.capabilities.get("platformName", "").lower()
            log_types = driver.log_types

            log = ""
            if platform == "android":
                log_type = "logcat" if "logcat" in log_types else (log_types[0] if log_types else "")
                if log_type:
                    logs = driver.get_log(log_type)
                    crash_logs = [
                        entry["message"]
                        for entry in logs
                        if "FATAL" in entry.get("message", "") or "AndroidRuntime" in entry.get("message", "")
                    ]
                    if crash_logs:
                        log = "\n".join(crash_logs[-20:])
                        return {"crashed": True, "log": log}

            # 앱이 현재 실행 중인지 확인
            current_package = driver.current_package if platform == "android" else ""
            crashed = bool(package_name and current_package and package_name not in current_package)

            logger.info("mobile_qa_crash_check", package=package_name, crashed=crashed)
            return {"crashed": crashed, "log": log}
        except Exception as e:
            logger.warning("mobile_qa_crash_check_failed", error=str(e))
            return {"crashed": False, "log": f"check failed: {e}"}

    def run_test_scenario(
        self,
        driver,
        scenario: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        시나리오 실행.

        시나리오 형식:
          [
            {"action": "tap", "target": "login_btn"},
            {"action": "input", "target": "email_field", "value": "test@test.com"},
            {"action": "scroll"},
            {"action": "screenshot", "path": "/tmp/step1.png"},
            {"action": "wait", "seconds": 2},
          ]

        Returns:
            각 단계별 실행 결과 list[dict]
        """
        results = []
        for i, step in enumerate(scenario):
            action = step.get("action", "")
            target = step.get("target", "")
            value = step.get("value", "")
            path = step.get("path", f"/tmp/scenario_step_{i}.png")
            seconds = step.get("seconds", 1)

            step_result: Dict[str, Any] = {
                "step": i,
                "action": action,
                "target": target,
                "success": False,
                "screenshot": None,
                "error": None,
            }

            try:
                if action == "tap":
                    step_result["success"] = self.tap_element(driver, accessibility_id=target)
                elif action == "input":
                    step_result["success"] = self.input_text(driver, target, value)
                elif action == "scroll":
                    step_result["success"] = self.scroll_down(driver)
                elif action == "screenshot":
                    saved = self.take_screenshot(driver, path)
                    step_result["success"] = bool(saved)
                    step_result["screenshot"] = saved
                elif action == "wait":
                    import time
                    time.sleep(float(seconds))
                    step_result["success"] = True
                else:
                    step_result["error"] = f"알 수 없는 액션: {action}"
            except Exception as e:
                step_result["error"] = str(e)
                logger.warning("mobile_qa_scenario_step_failed", step=i, action=action, error=str(e))

            results.append(step_result)
            logger.info("mobile_qa_scenario_step", step=i, action=action, success=step_result["success"])

        return results

    # ------------------------------------------------------------------
    # 종료
    # ------------------------------------------------------------------

    def close(self, driver) -> None:
        """Appium driver 세션 종료."""
        try:
            if driver:
                driver.quit()
                logger.info("mobile_qa_driver_closed")
        except Exception as e:
            logger.warning("mobile_qa_close_failed", error=str(e))

    # ------------------------------------------------------------------
    # ADB 유틸리티
    # ------------------------------------------------------------------

    def _adb(self, *args) -> str:
        """ADB 명령 실행."""
        cmd = ["adb", f"-s", f"{self.adb_host}:{self.adb_port}"] + list(args)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return result.stdout.strip()
        except Exception as e:
            logger.warning("mobile_qa_adb_error", cmd=cmd, error=str(e))
            return ""

    def adb_connect(self) -> str:
        """ADB 연결."""
        result = subprocess.run(
            ["adb", "connect", f"{self.adb_host}:{self.adb_port}"],
            capture_output=True, text=True, timeout=10
        )
        return result.stdout.strip()

    def adb_devices(self) -> str:
        """연결된 ADB 기기 목록."""
        result = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=10)
        return result.stdout.strip()

    def install_apk(self, apk_path: str) -> bool:
        """ADB로 APK 직접 설치."""
        result = subprocess.run(
            ["adb", "-s", f"{self.adb_host}:{self.adb_port}", "install", "-r", apk_path],
            capture_output=True, text=True, timeout=120
        )
        success = "Success" in result.stdout
        logger.info("mobile_qa_adb_install", path=apk_path, success=success)
        return success

    def download_apk(self, apk_url: str, dest_path: str) -> bool:
        """APK URL에서 파일 다운로드."""
        try:
            import urllib.request
            logger.info("mobile_qa_download_apk", url=apk_url, dest=dest_path)
            Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
            urllib.request.urlretrieve(apk_url, dest_path)
            logger.info("mobile_qa_apk_downloaded", dest=dest_path)
            return True
        except Exception as e:
            logger.error("mobile_qa_download_failed", url=apk_url, error=str(e))
            return False

    # ------------------------------------------------------------------
    # 에뮬레이터 상태 확인
    # ------------------------------------------------------------------

    def check_android_emulator(self) -> str:
        """Android 에뮬레이터 연결 상태 확인."""
        try:
            result = subprocess.run(
                ["adb", "devices"],
                capture_output=True, text=True, timeout=5
            )
            lines = [l.strip() for l in result.stdout.splitlines() if l.strip() and "List" not in l]
            connected = any("device" in line for line in lines)
            return "connected" if connected else "not_available"
        except FileNotFoundError:
            return "adb_not_installed"
        except Exception as e:
            return f"error: {e}"

    def check_appium_server(self) -> str:
        """Appium 서버 상태 확인."""
        try:
            import urllib.request
            with urllib.request.urlopen(f"{self.appium_url}/status", timeout=3) as resp:
                if resp.status == 200:
                    return "running"
            return "stopped"
        except Exception:
            return "stopped"


# 싱글톤 인스턴스
mobile_qa_service = MobileQAService(
    appium_url=os.getenv("APPIUM_URL", "http://localhost:4723"),
    adb_host=os.getenv("ANDROID_EMULATOR_HOST", "localhost"),
    adb_port=int(os.getenv("ANDROID_EMULATOR_PORT", "5555")),
)
