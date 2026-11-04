"""User-Agent 한 줄을 감사 기록용 요약으로 줄인다. 원문 UA·IP·쿠키는 남기지 않는다."""
from __future__ import annotations

import re

_IN_APP_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("kakaotalk", re.compile(r"KAKAOTALK", re.I)),
    ("naver", re.compile(r"NAVER\(inapp|NAVER", re.I)),
    ("instagram", re.compile(r"Instagram", re.I)),
    ("facebook", re.compile(r"FBAN|FBAV|FB_IAB", re.I)),
    ("line", re.compile(r"\bLine/", re.I)),
    ("daum", re.compile(r"DaumApps", re.I)),
    ("webview", re.compile(r";\s*wv\)", re.I)),
)

_BROWSERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("edge", re.compile(r"EdgA?/|EdgiOS/|Edge/")),
    ("samsung", re.compile(r"SamsungBrowser/")),
    ("whale", re.compile(r"Whale/")),
    ("opera", re.compile(r"OPR/|OPiOS/")),
    ("firefox", re.compile(r"Firefox/|FxiOS/")),
    ("chrome", re.compile(r"Chrome/|CriOS/")),
    ("safari", re.compile(r"Safari/")),
)


def _os_of(ua: str) -> str:
    if re.search(r"iPhone|iPad|iPod", ua):
        return "ios"
    if "Android" in ua:
        return "android"
    if "Windows" in ua:
        return "windows"
    if "Macintosh" in ua or "Mac OS X" in ua:
        return "macos"
    if "Linux" in ua or "X11" in ua:
        return "linux"
    return "other"


def summarize_client(user_agent: str | None) -> dict[str, object]:
    """앱 안 브라우저 여부·OS·브라우저 종류만 돌려준다."""
    ua = str(user_agent or "")[:500]
    os_name = _os_of(ua)
    in_app_name = next((name for name, pattern in _IN_APP_MARKERS if pattern.search(ua)), "")
    browser = next((name for name, pattern in _BROWSERS if pattern.search(ua)), "other" if ua else "unknown")
    return {
        "in_app": bool(in_app_name),
        "in_app_name": in_app_name,
        "os": os_name,
        "browser": browser,
        "mobile": os_name in {"ios", "android"},
    }
