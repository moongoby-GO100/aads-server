"""AADS-195: 화면 탐색/OCR + URL/대기/매크로 (P1)."""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import webbrowser
from typing import Any, Dict

logger = logging.getLogger(__name__)


async def find_on_screen(params: Dict[str, Any]) -> Dict[str, Any]:
    """화면에서 이미지 찾기. params: image_path, confidence(0.0~1.0)"""
    try:
        import pyautogui
        image_path = params.get("image_path", "")
        if not image_path or not os.path.exists(image_path):
            return {"status": "error", "data": {"error": f"이미지 파일 없음: {image_path}"}}
        confidence = float(params.get("confidence", 0.8))
        try:
            loc = pyautogui.locateOnScreen(image_path, confidence=confidence)
        except pyautogui.ImageNotFoundException:
            return {"status": "success", "data": {"found": False, "message": "화면에서 찾지 못함"}}
        if loc is None:
            return {"status": "success", "data": {"found": False, "message": "화면에서 찾지 못함"}}
        center = pyautogui.center(loc)
        return {"status": "success", "data": {
            "found": True,
            "x": center.x, "y": center.y,
            "left": loc.left, "top": loc.top,
            "width": loc.width, "height": loc.height,
        }}
    except ImportError as ie:
        return {"status": "error", "data": {"error": f"필요 라이브러리 미설치: {ie}"}}
    except Exception as e:
        logger.error("find_on_screen error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}


async def screen_text(params: Dict[str, Any]) -> Dict[str, Any]:
    """화면 OCR 텍스트 추출. params: region(선택, {x,y,w,h}), lang(선택, 기본 eng+kor)"""
    try:
        from PIL import ImageGrab
        region = params.get("region")
        if region:
            bbox = (int(region["x"]), int(region["y"]),
                    int(region["x"]) + int(region["w"]),
                    int(region["y"]) + int(region["h"]))
            img = ImageGrab.grab(bbox=bbox)
        else:
            img = ImageGrab.grab(all_screens=True)

        try:
            import pytesseract
            lang = params.get("lang", "eng+kor")
            text = pytesseract.image_to_string(img, lang=lang)
            return {"status": "success", "data": {"text": text.strip(), "length": len(text.strip())}}
        except ImportError:
            return {"status": "error", "data": {"error": "pytesseract 미설치. pip install pytesseract + Tesseract-OCR 설치 필요"}}
    except Exception as e:
        logger.error("screen_text error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}


async def open_url(params: Dict[str, Any]) -> Dict[str, Any]:
    """브라우저에서 URL 열기. params: url, browser(선택)"""
    try:
        url = params.get("url", "")
        if not url:
            return {"status": "error", "data": {"error": "url 파라미터 필수"}}
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        browser_name = params.get("browser")
        if browser_name:
            try:
                b = webbrowser.get(browser_name)
                b.open(url)
            except webbrowser.Error:
                webbrowser.open(url)
        else:
            webbrowser.open(url)
        return {"status": "success", "data": {"url": url}}
    except Exception as e:
        logger.error("open_url error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}


async def wait(params: Dict[str, Any]) -> Dict[str, Any]:
    """지정 시간(초) 대기. params: seconds"""
    try:
        seconds = float(params.get("seconds", 1))
        if seconds > 60:
            return {"status": "error", "data": {"error": "최대 60초까지 대기 가능"}}
        await asyncio.sleep(seconds)
        return {"status": "success", "data": {"waited": seconds}}
    except Exception as e:
        logger.error("wait error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}


async def batch_command(params: Dict[str, Any]) -> Dict[str, Any]:
    """여러 명령 순차 실행 (매크로). params: commands([{command_type, params}, ...])"""
    try:
        commands = params.get("commands", [])
        if not commands:
            return {"status": "error", "data": {"error": "commands 리스트 필수"}}
        if len(commands) > 50:
            return {"status": "error", "data": {"error": "최대 50개 명령까지 가능"}}

        # 지연 import — agent의 dispatch를 직접 호출하지 않고 결과만 수집
        from commands import COMMAND_HANDLERS

        results = []
        for i, cmd in enumerate(commands):
            cmd_type = cmd.get("command_type", "")
            cmd_params = cmd.get("params", {})
            delay = float(cmd.get("delay", 0))

            if delay > 0:
                await asyncio.sleep(min(delay, 10))

            handler = COMMAND_HANDLERS.get(cmd_type)
            if handler is None:
                results.append({"index": i, "command_type": cmd_type, "status": "error", "error": f"지원하지 않는 명령: {cmd_type}"})
                continue

            try:
                result = await handler(cmd_params)
                results.append({"index": i, "command_type": cmd_type, "status": result.get("status", "success")})
            except Exception as e:
                results.append({"index": i, "command_type": cmd_type, "status": "error", "error": str(e)})

        return {"status": "success", "data": {"results": results, "total": len(commands), "success": sum(1 for r in results if r["status"] == "success")}}
    except Exception as e:
        logger.error("batch_command error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}
