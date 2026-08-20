"""KakaoBot SaaS — 시스템 트레이 아이콘.

pystray 기반. launcher.py에서 별도 스레드로 실행됨.
v1.0.40: 버전 표시, 트레이 숨기기/완전종료 분리, 확인 다이얼로그.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

logger = logging.getLogger("tray")

_EXIT_TITLE = "KakaoBot 종료"
_EXIT_MESSAGE = (
    "에이전트를 완전히 종료합니다.\n"
    "PC Agent 연결이 끊어집니다.\n\n"
    "정말 종료하시겠습니까?"
)
_SETTINGS_TITLE = "KakaoBot PC Agent 설정"
_MASKED = "••••••••••••"

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


def _mask_secret(value: object) -> str:
    secret = str(value or "")
    if not secret:
        return "미등록"
    if len(secret) <= 8:
        return _MASKED
    return f"{secret[:4]}…{secret[-4:]}"


def _load_config_snapshot(cfg: dict) -> dict:
    latest = dict(cfg or {})
    config_path = INSTALL_DIR / "config.json"
    try:
        if config_path.exists():
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                latest.update(loaded)
    except Exception:
        logger.exception("설정 파일 읽기 실패")
    return latest


def _save_config_updates(updates: dict) -> None:
    config_path = INSTALL_DIR / "config.json"
    current = {}
    try:
        if config_path.exists():
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                current = loaded
    except Exception:
        logger.exception("설정 파일 저장 전 읽기 실패")
    current.update(updates)
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")


def open_settings_window(
    cfg: dict,
    *,
    on_save: Callable[[dict], None] | None = None,
    on_reconnect: Callable[[], None] | None = None,
) -> None:
    """Open a human-safe settings window instead of exposing config.json."""
    try:
        import tkinter as tk
        from tkinter import messagebox
    except Exception:
        logger.exception("설정 UI 모듈 로드 실패")
        return

    snapshot = _load_config_snapshot(cfg)
    root = tk.Tk()
    root.title(_SETTINGS_TITLE)
    root.geometry("520x380")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    root.columnconfigure(1, weight=1)

    def _row(label: str, row: int, value: str, *, readonly: bool = False, show: str | None = None):
        tk.Label(root, text=label, anchor="w", font=("맑은 고딕", 10)).grid(
            row=row, column=0, padx=(18, 10), pady=8, sticky="w"
        )
        var = tk.StringVar(value=value)
        entry = tk.Entry(root, textvariable=var, width=44, show=show or "")
        if readonly:
            entry.configure(state="readonly")
        entry.grid(row=row, column=1, padx=(0, 18), pady=8, sticky="ew")
        return var

    server_url_var = _row("서버 URL", 0, str(snapshot.get("server_url") or ""), readonly=False)
    agent_id_var = _row("Agent ID", 1, str(snapshot.get("agent_id") or "미등록"), readonly=True)
    token_var = _row("Agent Token", 2, _mask_secret(snapshot.get("agent_token")), readonly=True)
    node_role_var = _row("노드 역할", 3, str(snapshot.get("node_role") or "interactive"), readonly=False)
    setup_var = _row("설치 방식", 4, str(snapshot.get("setup_method") or "unknown"), readonly=True)

    help_text = (
        "Agent Token은 보안상 표시하지 않습니다. 토큰 변경이 필요하면 "
        "AADS 대시보드에서 새 설치 티켓으로 재등록하십시오."
    )
    tk.Label(root, text=help_text, anchor="w", justify="left", wraplength=470, fg="#555").grid(
        row=5, column=0, columnspan=2, padx=18, pady=(8, 14), sticky="ew"
    )

    def save():
        updates = {
            "server_url": server_url_var.get().strip(),
            "node_role": node_role_var.get().strip() or "interactive",
        }
        try:
            _save_config_updates(updates)
            cfg.update(updates)
            if on_save:
                on_save(updates)
            messagebox.showinfo(_SETTINGS_TITLE, "설정을 저장했습니다. 재연결하면 새 설정이 적용됩니다.", parent=root)
        except Exception as exc:
            logger.exception("설정 저장 실패")
            messagebox.showerror(_SETTINGS_TITLE, f"설정 저장 실패: {exc}", parent=root)

    def reconnect():
        if on_reconnect:
            on_reconnect()
        messagebox.showinfo(_SETTINGS_TITLE, "재연결을 요청했습니다.", parent=root)

    def open_logs():
        log_path = str(LOG_DIR)
        if sys.platform == "win32":
            os.startfile(log_path)
        else:
            subprocess.Popen(["xdg-open", log_path])

    buttons = tk.Frame(root)
    buttons.grid(row=6, column=0, columnspan=2, padx=18, pady=10, sticky="e")
    tk.Button(buttons, text="로그 열기", command=open_logs, width=12).pack(side="left", padx=4)
    tk.Button(buttons, text="재연결 시도", command=reconnect, width=12).pack(side="left", padx=4)
    tk.Button(buttons, text="저장", command=save, width=10).pack(side="left", padx=4)
    tk.Button(buttons, text="닫기", command=root.destroy, width=10).pack(side="left", padx=4)

    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()


def confirm_full_exit() -> bool:
    """Ask for explicit confirmation without failing open.

    pystray invokes menu callbacks on a worker thread. Tk dialogs can become
    unresponsive on that thread on Windows, so use the native Win32 dialog
    there and retain Tk only as a non-Windows fallback.
    """
    if sys.platform == "win32":
        try:
            import ctypes

            flags = 0x00000004 | 0x00000030 | 0x00000100 | 0x00010000 | 0x00040000
            result = ctypes.windll.user32.MessageBoxW(
                None, _EXIT_MESSAGE, _EXIT_TITLE, flags
            )
            return result == 6  # IDYES
        except Exception:
            logger.exception("Win32 완전 종료 확인창 표시 실패")
            return False

    root = None
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        return bool(messagebox.askyesno(_EXIT_TITLE, _EXIT_MESSAGE, parent=root))
    except Exception:
        logger.exception("완전 종료 확인창 표시 실패")
        return False
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass


def create_tray(
    cfg: dict,
    agent_proc_or_ref,
    on_quit: Callable,
    on_reconnect: Callable[[], None] | None = None,
) -> None:
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
        threading.Thread(
            target=open_settings_window,
            args=(cfg,),
            kwargs={"on_reconnect": lambda: request_reconnect(icon)},
            daemon=True,
            name="KakaoBotSettings",
        ).start()

    def request_reconnect(icon=None):
        logger.info("트레이에서 재연결 요청")
        if on_reconnect:
            on_reconnect()
        try:
            if icon:
                icon.notify("재연결을 요청했습니다.", "KakaoBot")
        except Exception:
            pass

    def on_hide(icon, item):
        logger.info("트레이 숨기기 — 에이전트 실행 유지")
        try:
            icon.notify("트레이를 숨겼습니다. 에이전트는 계속 실행 중입니다.", "KakaoBot")
        except Exception:
            pass
        icon.visible = False

    def on_exit(icon, item):
        def _exit_worker():
            if not confirm_full_exit():
                logger.info("완전 종료 취소")
                return
            logger.info("트레이에서 완전 종료 요청")
            on_quit()
            try:
                icon.stop()
            except Exception:
                logger.exception("트레이 아이콘 종료 실패")

        threading.Thread(target=_exit_worker, daemon=True, name="KakaoBotExitConfirm").start()

    def auto_reply_text(item):
        return f"자동응답 {'ON ✓' if auto_reply_enabled else 'OFF'}"

    menu = pystray.Menu(
        Item(f"KakaoBot v{version}", on_status, enabled=False),
        Item(_status_text, on_status),
        pystray.Menu.SEPARATOR,
        Item(auto_reply_text, on_toggle_auto),
        Item("재연결 시도", request_reconnect),
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
