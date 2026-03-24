"""AADS-195: 스크린샷 캡처 (듀얼모니터 지원)."""
from __future__ import annotations

import base64
import io
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


async def execute(params: Dict[str, Any]) -> Dict[str, Any]:
    """화면 스크린샷 캡처 → base64 PNG 반환.
    
    params:
        all_screens: True면 전체 모니터 캡처 (기본 True)
        monitor: 특정 모니터 번호 (0=전체, 1=주모니터, 2=보조모니터)
    """
    try:
        from PIL import ImageGrab

        all_screens = params.get("all_screens", True)
        monitor = params.get("monitor", 0)

        if monitor == 0:
            img = ImageGrab.grab(all_screens=all_screens)
        else:
            # 특정 모니터만 캡처 — screeninfo로 좌표 계산
            try:
                from screeninfo import get_monitors
                monitors = get_monitors()
                if monitor <= len(monitors):
                    m = monitors[monitor - 1]
                    bbox = (m.x, m.y, m.x + m.width, m.y + m.height)
                    img = ImageGrab.grab(bbox=bbox)
                else:
                    img = ImageGrab.grab(all_screens=True)
            except ImportError:
                img = ImageGrab.grab(all_screens=True)

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
        return {"status": "error", "data": {"error": "Pillow 미설치. pip install Pillow"}}
    except Exception as e:
        logger.error("screenshot_error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}
