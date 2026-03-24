"""AADS-195: 시스템 유틸 — 볼륨/모니터/전원/앱목록/알림/파일검색 (P2)."""
from __future__ import annotations

import logging
import os
import subprocess
from typing import Any, Dict

logger = logging.getLogger(__name__)


async def volume_control(params: Dict[str, Any]) -> Dict[str, Any]:
    """볼륨 조절. params: action(up/down/mute/unmute/set), value(0~100, set용)"""
    try:
        action = params.get("action", "")
        if not action:
            return {"status": "error", "data": {"error": "action 필수 (up/down/mute/unmute/set)"}}

        try:
            from ctypes import cast, POINTER
            import comtypes
            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

            devices = AudioUtilities.GetSpeakers()
            interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            volume = cast(interface, POINTER(IAudioEndpointVolume))

            if action == "mute":
                volume.SetMute(1, None)
                return {"status": "success", "data": {"action": "mute"}}
            elif action == "unmute":
                volume.SetMute(0, None)
                return {"status": "success", "data": {"action": "unmute"}}
            elif action == "set":
                val = int(params.get("value", 50))
                volume.SetMasterVolumeLevelScalar(max(0, min(100, val)) / 100.0, None)
                return {"status": "success", "data": {"action": "set", "value": val}}
            elif action == "up":
                current = volume.GetMasterVolumeLevelScalar()
                new_val = min(1.0, current + 0.1)
                volume.SetMasterVolumeLevelScalar(new_val, None)
                return {"status": "success", "data": {"action": "up", "volume_pct": int(new_val * 100)}}
            elif action == "down":
                current = volume.GetMasterVolumeLevelScalar()
                new_val = max(0.0, current - 0.1)
                volume.SetMasterVolumeLevelScalar(new_val, None)
                return {"status": "success", "data": {"action": "down", "volume_pct": int(new_val * 100)}}
            else:
                return {"status": "error", "data": {"error": f"지원하지 않는 action: {action}"}}
        except ImportError:
            # pycaw 없으면 nircmd fallback
            if action == "mute":
                subprocess.run(["nircmd", "mutesysvolume", "1"], capture_output=True)
            elif action == "unmute":
                subprocess.run(["nircmd", "mutesysvolume", "0"], capture_output=True)
            elif action == "up":
                subprocess.run(["nircmd", "changesysvolume", "6553"], capture_output=True)
            elif action == "down":
                subprocess.run(["nircmd", "changesysvolume", "-6553"], capture_output=True)
            elif action == "set":
                val = int(params.get("value", 50))
                level = int(65535 * val / 100)
                subprocess.run(["nircmd", "setsysvolume", str(level)], capture_output=True)
            return {"status": "success", "data": {"action": action, "method": "nircmd_fallback"}}
    except Exception as e:
        logger.error("volume_control error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}


async def monitor_info(params: Dict[str, Any]) -> Dict[str, Any]:
    """연결된 모니터 목록, 해상도, 배치 정보."""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        monitors = []

        def monitor_callback(hMonitor, hdcMonitor, lprcMonitor, dwData):
            info = wintypes.RECT()
            ctypes.memmove(ctypes.byref(info), lprcMonitor, ctypes.sizeof(wintypes.RECT))
            monitors.append({
                "index": len(monitors) + 1,
                "left": info.left,
                "top": info.top,
                "right": info.right,
                "bottom": info.bottom,
                "width": info.right - info.left,
                "height": info.bottom - info.top,
                "is_primary": info.left == 0 and info.top == 0,
            })
            return True

        MONITORENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.POINTER(wintypes.RECT), ctypes.c_double)
        user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(monitor_callback), 0)

        return {"status": "success", "data": {"monitors": monitors, "count": len(monitors)}}
    except Exception as e:
        logger.error("monitor_info error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}


async def power_control(params: Dict[str, Any]) -> Dict[str, Any]:
    """전원 관리. params: action(sleep/restart/shutdown/lock)"""
    try:
        action = params.get("action", "")
        if not action:
            return {"status": "error", "data": {"error": "action 필수 (sleep/restart/shutdown/lock)"}}

        if action == "lock":
            import ctypes
            ctypes.windll.user32.LockWorkStation()
        elif action == "sleep":
            subprocess.run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], capture_output=True)
        elif action == "restart":
            subprocess.run(["shutdown", "/r", "/t", "5"], capture_output=True)
        elif action == "shutdown":
            subprocess.run(["shutdown", "/s", "/t", "5"], capture_output=True)
        else:
            return {"status": "error", "data": {"error": f"지원하지 않는 action: {action}"}}

        return {"status": "success", "data": {"action": action}}
    except Exception as e:
        logger.error("power_control error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}


async def installed_apps(params: Dict[str, Any]) -> Dict[str, Any]:
    """설치된 프로그램 목록 (레지스트리 기반)."""
    try:
        import winreg
        apps = []
        reg_paths = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]
        seen = set()
        for hive, path in reg_paths:
            try:
                key = winreg.OpenKey(hive, path)
                for i in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        subkey_name = winreg.EnumKey(key, i)
                        subkey = winreg.OpenKey(key, subkey_name)
                        try:
                            name = winreg.QueryValueEx(subkey, "DisplayName")[0]
                            if name and name not in seen:
                                seen.add(name)
                                version = ""
                                try:
                                    version = winreg.QueryValueEx(subkey, "DisplayVersion")[0]
                                except (FileNotFoundError, OSError):
                                    pass
                                apps.append({"name": name, "version": version})
                        except (FileNotFoundError, OSError):
                            pass
                        winreg.CloseKey(subkey)
                    except OSError:
                        pass
                winreg.CloseKey(key)
            except OSError:
                pass

        query = params.get("query", "").lower()
        if query:
            apps = [a for a in apps if query in a["name"].lower()]

        apps.sort(key=lambda x: x["name"])
        return {"status": "success", "data": {"apps": apps[:200], "total": len(apps)}}
    except Exception as e:
        logger.error("installed_apps error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}


async def notification(params: Dict[str, Any]) -> Dict[str, Any]:
    """PC에 알림 토스트 표시. params: title, message, duration(초)"""
    try:
        title = params.get("title", "AADS")
        message = params.get("message", "")
        if not message:
            return {"status": "error", "data": {"error": "message 파라미터 필수"}}
        duration = int(params.get("duration", 5))

        try:
            from win10toast import ToastNotifier
            toaster = ToastNotifier()
            toaster.show_toast(title, message, duration=duration, threaded=True)
            return {"status": "success", "data": {"title": title, "message": message}}
        except ImportError:
            # PowerShell fallback
            ps_script = f"""
            [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
            $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
            $text = $template.GetElementsByTagName('text')
            $text.Item(0).AppendChild($template.CreateTextNode('{title}')) | Out-Null
            $text.Item(1).AppendChild($template.CreateTextNode('{message}')) | Out-Null
            $toast = [Windows.UI.Notifications.ToastNotification]::new($template)
            [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('AADS').Show($toast)
            """
            subprocess.Popen(["powershell", "-Command", ps_script],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"status": "success", "data": {"title": title, "message": message, "method": "powershell"}}
    except Exception as e:
        logger.error("notification error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}


async def file_search(params: Dict[str, Any]) -> Dict[str, Any]:
    """파일명으로 검색. params: query, path(검색 시작 경로), max_results"""
    try:
        query = params.get("query", "")
        if not query:
            return {"status": "error", "data": {"error": "query 파라미터 필수"}}

        search_path = params.get("path", os.path.expanduser("~"))
        max_results = int(params.get("max_results", 50))
        query_lower = query.lower()

        results = []
        try:
            for root, dirs, files in os.walk(search_path):
                # 시스템/숨김 폴더 스킵
                dirs[:] = [d for d in dirs if not d.startswith(".") and d not in (
                    "node_modules", "__pycache__", ".git", "AppData", "$Recycle.Bin",
                    "Windows", "Program Files", "ProgramData",
                )]
                for f in files:
                    if query_lower in f.lower():
                        full = os.path.join(root, f)
                        try:
                            size = os.path.getsize(full)
                        except OSError:
                            size = 0
                        results.append({"path": full, "name": f, "size": size})
                        if len(results) >= max_results:
                            return {"status": "success", "data": {"files": results, "count": len(results), "truncated": True}}
        except PermissionError:
            pass

        return {"status": "success", "data": {"files": results, "count": len(results), "truncated": False}}
    except Exception as e:
        logger.error("file_search error: %s", e)
        return {"status": "error", "data": {"error": str(e)}}
