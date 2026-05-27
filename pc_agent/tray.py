"""KakaoBot SaaS — 시스템 트레이 아이콘.

pystray 기반. launcher.py에서 별도 스레드로 실행됨.
메뉴: 상태 보기 / 카카오 자동응답 ON·OFF / 로그 보기 / 설정 / 종료.
상태: 연결됨(초록) / 재연결 중(노랑) / 연결 끊김(빨강).
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

logger = logging.getLogger("tray")

# ---------------------------------------------------------------------------
# 아이콘 생성 (Pillow로 단색 원)
# ---------------------------------------------------------------------------
_COLORS = {
    "connected": (0, 200, 80),      # 초록
    "reconnecting": (240, 200, 0),   # 노랑
    "disconnected": (220, 50, 50),   # 빨강
}

_STATUS_LABELS = {
    "connected": "연결됨 — 정상 동작 중",
    "reconnecting": "재연결 중 — 서버 연결 시도 중",
    "disconnected": "연결 끊김 — 프로세스 종료됨",
}

INSTALL_DIR = Path(os.environ.get(
    "KAKAOBOT_INSTALL_DIR",
    os.path.join(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"), "KakaoBot"),
))
LOG_DIR = INSTALL_DIR / "logs"


def _make_icon(color: tuple[int, int, int] = (0, 200, 80)):
    """64x64 단색 원 아이콘 생성."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, 60, 60], fill=(*color, 255))
    draw.text((22, 16), "K", fill=(255, 255, 255, 255))
    return img


# ---------------------------------------------------------------------------
# 트레이 생성
# ---------------------------------------------------------------------------
def create_tray(cfg: dict, agent_proc, on_quit: Callable) -> None:
    """시스템 트레이 아이콘 생성 및 실행."""
    try:
        import pystray
        from pystray import MenuItem as Item
    except ImportError:
        logger.warning("pystray 미설치 — 트레이 없이 실행")
        return

    auto_reply_enabled = True

    def get_status() -> str:
        """실제 WebSocket 연결 상태 확인."""
        if hasattr(agent_proc, 'is_connected') and agent_proc.is_connected:
            return "connected"
        if agent_proc and agent_proc.poll() is None:
            return "reconnecting"
        return "disconnected"

    def on_status(icon, item):
        """상태 보기."""
        status = get_status()
        try:
            icon.notify(_STATUS_LABELS.get(status, status), "KakaoBot 상태")
        except Exception:
            pass

    def on_toggle_auto(icon, item):
        """카카오 자동응답 ON/OFF 토글."""
        nonlocal auto_reply_enabled
        auto_reply_enabled = not auto_reply_enabled
        state = "ON" if auto_reply_enabled else "OFF"
        logger.info("카카오 자동응답: %s", state)
        try:
            icon.notify(f"자동응답: {state}", "KakaoBot")
        except Exception:
            pass
        icon.update_menu()

    def on_open_logs(icon, item):
        """로그 폴더 열기."""
        log_path = str(LOG_DIR)
        if sys.platform == "win32":
            os.startfile(log_path)
        else:
            subprocess.Popen(["xdg-open", log_path])

    def on_settings(icon, item):
        """설정 파일 열기."""
        config_path = str(INSTALL_DIR / "config.json")
        if sys.platform == "win32":
            os.startfile(config_path)
        else:
            subprocess.Popen(["xdg-open", config_path])

    def on_exit(icon, item):
        """종료."""
        logger.info("트레이에서 종료 요청")
        on_quit()
        icon.stop()

    def auto_reply_text(item):
        return f"자동응답 {'ON ✓' if auto_reply_enabled else 'OFF'}"

    menu = pystray.Menu(
        Item("상태 보기", on_status),
        Item(auto_reply_text, on_toggle_auto),
        pystray.Menu.SEPARATOR,
        Item("로그 보기", on_open_logs),
        Item("설정", on_settings),
        pystray.Menu.SEPARATOR,
        Item("종료", on_exit),
    )

    icon = pystray.Icon(
        name="KakaoBot",
        icon=_make_icon(_COLORS[get_status()]),
        title="KakaoBot PC Agent",
        menu=menu,
    )

    icon.run()
