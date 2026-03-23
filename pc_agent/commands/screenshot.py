"""AADS-195: 스크린샷 캡처."""
from __future__ import annotations

import base64
import io
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


async def execute(params: Dict[str, Any]) -> Dict[str, Any]:
    """화면 스크린샷 캡처 → base64 PNG 반환."""
    try:
        # PIL/Pillow의 ImageGrab (Windows 전용)
        from PIL import ImageGrab

        img = ImageGrab.grab()
        buffer = io.BytesIO()
        img.save(buffer, format="PNG", optimize=True)
        buffer.seek(0)
        img_base64 = base64.b64encode(buffer.read()).decode("utf-8")

        return {
            "status": "success",
            "data": {
                "image": img_base64,
                "width": img.width,
                "height": img.height,
            },
        }
    except ImportError:
        return {"status": "error", "data": {"error": "Pillow 라이브러리가 설치되어 있지 않습니다. pip install Pillow"}}
    except Exception as e:
        logger.error("screenshot_error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}
