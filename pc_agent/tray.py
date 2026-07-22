"""KakaoBot SaaS — 시스템 트레이 아이콘.

pystray 기반. launcher.py에서 별도 스레드로 실행됨.
v1.0.40: 버전 표시, 트레이 숨기기/완전종료 분리, 확인 다이얼로그.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

logger = logging.getLogger("tray")

_EXIT_TITLE = "KakaoBot 종료"
_EXIT_MESSAGE = (
    "에이전트를 완전히 종료합니다.\n"
    "PC Agent 연결이 끊어집니다.\n\n"
    "정말 종료하시겠습니까?"
)

_COLORS = {
    "connected": (0, 200, 80),
    "reconnecting": (240, 200, 0),
    "disconnected": (220, 50, 50),
}

INSTALL_DIR = Path(os.environ.get(
    "KAKAOBOT_INSTALL_DIR",
    os.path.join(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"), "KakaoBot"),
))
LOG_DIR = INSTALL_DIR / "logs"
VERSION_FILE = INSTALL_DIR / "agent" / "VERSION"


def _get_version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return "?.?.?"


def _make_icon(color: tuple[int, int, int] = (0, 200, 80)):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([4, 4, 60, 60], fill=(*color, 255))
    draw.text((22, 16), "K", fill=(255, 255, 255, 255))
    return img


def _confirm_full_exit() -> bool:
    """완전 종료 여부를 확인한다.

    Windows 트레이 콜백은 별도 스레드에서 실행되므로 tkinter 메시지 루프를
    함께 띄우면 버튼이 응답하지 않을 수 있다. Windows에서는 OS 네이티브
    MessageBoxW를 사용하고, 확인창 생성 실패 시에는 안전하게 종료를 취소한다.
    """
    if sys.platform == "win32":
        try:
            import ctypes

            mb_yesno = 0x00000004
            mb_iconwarning = 0x00000030
            mb_defbutton2 = 0x00000100
            mb_setforeground = 0x00010000
            mb_systemmodal = 0x00001000
            result = ctypes.windll.user32.MessageBoxW(
                None,
                _EXIT_MESSAGE,
                _EXIT_TITLE,
                mb_yesno
                | mb_iconwarning
                | mb_defbutton2
                | mb_setforeground
                | mb_systemmodal,
            )
            return result == 6  # IDYES
        except Exception as exc:
            logger.error("완전 종료 확인창 표시 실패: %s", exc)
            return False

    root = None
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        return bool(messagebox.askyesno(_EXIT_TITLE, _EXIT_MESSAGE, parent=root))
    except Exception as exc:
        logger.error("완전 종료 확인창 표시 실패: %s", exc)
        return False
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass


def create_tray(cfg: dict, agent_proc_or_ref, on_quit: Callable) -> None:
    try:
        import pystray
        from pystray import MenuItem as Item
    except ImportError:
        logger.warning("pystray 미설치 — 트레이 없이 실행")
        return

    version = _get_version()
    auto_reply_enabled = True

    def _get_proc():
        """launcher가 에이전트를 재시작해도 항상 최신 인스턴스를 반환."""
        if isinstance(agent_proc_or_ref, list):
            return agent_proc_or_ref[0]
        return agent_proc_or_ref

    def get_status() -> str:
        p = _get_proc()
        if hasattr(p, "is_connected") and p.is_connected:
            return "connected"
        if p and p.poll() is None:
            return "reconnecting"
        return "disconnected"

    def _status_text(item=None) -> str:
        status = get_status()
        labels = {
            "connected": "● 연결됨",
            "reconnecting": "● 재연결 중",
            "disconnected": "● 연결 끊김",
        }
        return f"{labels.get(status, status)} — v{version}"

    def on_status(icon, item):
        try:
            icon.notify(_status_text(), "KakaoBot 상태")
        except Exception:
            pass

    def on_toggle_auto(icon, item):
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
        log_path = str(LOG_DIR)
        if sys.platform == "win32":
            os.startfile(log_path)
        else:
            subprocess.Popen(["xdg-open", log_path])

    def on_settings(icon, item):
        config_path = str(INSTALL_DIR / "config.json")
        if sys.platform == "win32":
            os.startfile(config_path)
        else:
            subprocess.Popen(["xdg-open", config_path])

    def on_hide(icon, item):
        logger.info("트레이 숨기기 — 에이전트 실행 유지")
        try:
            icon.notify("트레이를 숨겼습니다. 에이전트는 계속 실행 중입니다.", "KakaoBot")
        except Exception:
            pass
        icon.visible = False

    def on_exit(icon, item):
        if not _confirm_full_exit():
            logger.info("트레이 완전 종료 취소")
            return
        logger.info("트레이에서 완전 종료 요청")
        on_quit()
        icon.stop()

    def auto_reply_text(item):
        return f"자동응답 {'ON ✓' if auto_reply_enabled else 'OFF'}"

    menu = pystray.Menu(
        Item(f"KakaoBot v{version}", on_status, enabled=False),
        Item(_status_text, on_status),
        pystray.Menu.SEPARATOR,
        Item(auto_reply_text, on_toggle_auto),
        Item("로그 보기", on_open_logs),
        Item("설정", on_settings),
        pystray.Menu.SEPARATOR,
        Item("트레이 숨기기", on_hide),
        Item("완전 종료", on_exit),
    )

    icon = pystray.Icon(
        name="KakaoBot",
        icon=_make_icon(_COLORS[get_status()]),
        title=f"KakaoBot PC Agent v{version}",
        menu=menu,
    )

    icon.run()
