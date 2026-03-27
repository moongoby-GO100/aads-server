"""
AADS-195 Phase 3: PC Agent 명령 빌더
CEO 자연어 입력 → PC Agent 명령 JSON 변환.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ── 자연어 패턴 → 명령 매핑 ─────────────────────────────────────────────

_SCREENSHOT_PATTERNS = [
    r"스크린샷", r"화면\s*캡처", r"화면\s*찍", r"screenshot",
    r"화면\s*보여", r"모니터\s*캡처",
]

_KAKAO_SEND_PATTERNS = [
    r"카카오톡.*보내", r"카톡.*보내", r"카톡으로.*전달",
    r"카카오톡.*메시지", r"카톡.*메시지", r"카톡.*전송",
]

_KAKAO_READ_PATTERNS = [
    r"카톡.*읽어", r"카카오톡.*확인", r"카톡.*메시지.*확인",
    r"카톡.*최근", r"카카오톡.*읽",
]

_FILE_LIST_PATTERNS = [
    r"파일\s*목록", r"폴더\s*목록", r"파일\s*리스트",
    r"디렉토리.*보여", r"파일.*보여",
]

_FILE_READ_PATTERNS = [
    r"파일.*읽어", r"파일.*열어", r"파일\s*내용",
    r"파일.*확인",
]

_PROCESS_LIST_PATTERNS = [
    r"프로세스\s*목록", r"실행.*(프로그램|프로세스).*목록",
    r"프로세스.*보여", r"작업\s*관리자",
]

_SYSTEM_INFO_PATTERNS = [
    r"시스템\s*정보", r"PC\s*정보", r"컴퓨터\s*정보",
    r"하드웨어\s*정보", r"system\s*info",
]

_BROWSER_LAUNCH_PATTERNS = [
    r"브라우저\s*열어", r"크롬\s*열어", r"브라우저\s*실행",
    r"chrome\s*열어", r"chrome\s*실행",
]

_BROWSER_NAVIGATE_PATTERNS = [
    r"접속해", r"이동해", r"페이지\s*열어", r"사이트\s*열어", r"url\s*열어",
]

_BROWSER_CLICK_PATTERNS = [
    r"클릭해", r"버튼\s*눌러", r"요소\s*클릭", r"\bclick\b",
]

_BROWSER_FILL_PATTERNS = [
    r"입력해", r"텍스트\s*입력", r"값\s*넣어", r"작성해", r"\bfill\b", r"타이핑",
]

_BROWSER_SCREENSHOT_PATTERNS = [
    r"브라우저\s*캡처", r"페이지\s*스크린샷", r"웹\s*캡처", r"브라우저\s*스크린샷",
]

_BROWSER_TEXT_PATTERNS = [
    r"텍스트\s*가져와", r"내용\s*읽어", r"페이지\s*텍스트", r"get\s*text",
]

_BROWSER_EVAL_PATTERNS = [
    r"자바스크립트\s*실행", r"js\s*실행", r"\beval\b",
]

_BROWSER_TABS_PATTERNS = [
    r"탭\s*목록", r"열린\s*탭", r"browser\s*tabs",
]


def build_command(message: str) -> Optional[Dict[str, Any]]:
    """
    자연어 메시지 → PC Agent 명령 JSON 변환.
    매칭되지 않으면 None 반환.
    """
    msg = message.strip()
    msg_lower = msg.lower()

    # 1. 스크린샷
    if _match_any(msg_lower, _SCREENSHOT_PATTERNS):
        return {"type": "screenshot"}

    # 2. 카카오톡 읽기 (전송보다 먼저 매칭 — 확인/읽기가 전송보다 구체적)
    if _match_any(msg_lower, _KAKAO_READ_PATTERNS):
        return {"type": "kakao_read"}

    # 3. 카카오톡 전송
    if _match_any(msg_lower, _KAKAO_SEND_PATTERNS):
        return _parse_kakao_send(msg)

    # 4. 프로세스 목록
    if _match_any(msg_lower, _PROCESS_LIST_PATTERNS):
        return {"type": "process_list"}

    # 5. 시스템 정보
    if _match_any(msg_lower, _SYSTEM_INFO_PATTERNS):
        return {"type": "system_info"}

    # 6. 파일 읽기
    if _match_any(msg_lower, _FILE_READ_PATTERNS):
        path = _extract_path(msg)
        return {"type": "file_read", "path": path or ""}

    # 7. 파일 목록
    if _match_any(msg_lower, _FILE_LIST_PATTERNS):
        path = _extract_path(msg)
        return {"type": "file_list", "path": path or "C:\\"}

    # 8. 브라우저 스크린샷 (일반 스크린샷 앞에 위치하지 않도록 별도 분기)
    if _match_any(msg_lower, _BROWSER_SCREENSHOT_PATTERNS):
        return {"type": "browser_screenshot"}

    # 9. 브라우저 실행
    if _match_any(msg_lower, _BROWSER_LAUNCH_PATTERNS):
        return {"type": "browser_launch"}

    # 10. 브라우저 내비게이션
    if _match_any(msg_lower, _BROWSER_NAVIGATE_PATTERNS):
        url = _extract_url(msg)
        return {"type": "browser_navigate", "url": url or ""}

    # 11. 브라우저 클릭
    if _match_any(msg_lower, _BROWSER_CLICK_PATTERNS):
        selector = _extract_quoted(msg)
        return {"type": "browser_click", "selector": selector or ""}

    # 12. 브라우저 텍스트 입력
    if _match_any(msg_lower, _BROWSER_FILL_PATTERNS):
        selector = _extract_quoted(msg, index=0)
        value = _extract_quoted(msg, index=1)
        return {"type": "browser_fill", "selector": selector or "", "value": value or ""}

    # 13. 브라우저 텍스트 추출
    if _match_any(msg_lower, _BROWSER_TEXT_PATTERNS):
        selector = _extract_quoted(msg)
        return {"type": "browser_get_text", "selector": selector or "body"}

    # 14. 브라우저 JS 실행
    if _match_any(msg_lower, _BROWSER_EVAL_PATTERNS):
        script = _extract_quoted(msg)
        return {"type": "browser_eval", "script": script or ""}

    # 15. 브라우저 탭 목록
    if _match_any(msg_lower, _BROWSER_TABS_PATTERNS):
        return {"type": "browser_tabs"}

    # 16. 셸 명령 (프로그램 실행 등)
    shell_cmd = _parse_shell_command(msg_lower, msg)
    if shell_cmd:
        return shell_cmd

    return None


def build_command_for_intent(intent: str, message: str) -> Optional[Dict[str, Any]]:
    """
    인텐트 기반 명령 빌더.
    intent_router에서 분류된 인텐트로 빠른 매칭 후 자연어 파싱.
    """
    if intent == "pc_screenshot":
        return {"type": "screenshot"}
    if intent == "pc_kakao":
        if _match_any(message.lower(), _KAKAO_READ_PATTERNS):
            return {"type": "kakao_read"}
        return _parse_kakao_send(message)
    if intent == "pc_file":
        if _match_any(message.lower(), _FILE_READ_PATTERNS):
            path = _extract_path(message)
            return {"type": "file_read", "path": path or ""}
        path = _extract_path(message)
        return {"type": "file_list", "path": path or "C:\\"}
    if intent == "pc_browser":
        msg_lower = message.lower()
        if _match_any(msg_lower, _BROWSER_SCREENSHOT_PATTERNS):
            return {"type": "browser_screenshot"}
        if _match_any(msg_lower, _BROWSER_LAUNCH_PATTERNS):
            return {"type": "browser_launch"}
        if _match_any(msg_lower, _BROWSER_NAVIGATE_PATTERNS):
            url = _extract_url(message)
            return {"type": "browser_navigate", "url": url or ""}
        if _match_any(msg_lower, _BROWSER_CLICK_PATTERNS):
            selector = _extract_quoted(message)
            return {"type": "browser_click", "selector": selector or ""}
        if _match_any(msg_lower, _BROWSER_FILL_PATTERNS):
            selector = _extract_quoted(message, index=0)
            value = _extract_quoted(message, index=1)
            return {"type": "browser_fill", "selector": selector or "", "value": value or ""}
        if _match_any(msg_lower, _BROWSER_TEXT_PATTERNS):
            selector = _extract_quoted(message)
            return {"type": "browser_get_text", "selector": selector or "body"}
        if _match_any(msg_lower, _BROWSER_EVAL_PATTERNS):
            script = _extract_quoted(message)
            return {"type": "browser_eval", "script": script or ""}
        if _match_any(msg_lower, _BROWSER_TABS_PATTERNS):
            return {"type": "browser_tabs"}
        return build_command(message)
    if intent == "pc_control":
        # 일반 PC 제어 — 셸 명령 파싱 시도, 실패 시 자연어 그대로 전달
        cmd = build_command(message)
        return cmd or {"type": "shell", "command": message}

    # 인텐트 매칭 안 되면 자연어 파싱 시도
    return build_command(message)


def format_result(command_type: str, result: Dict[str, Any] | None) -> str:
    """
    PC Agent 실행 결과 → 채팅 표시용 포맷 변환.
    """
    if result is None:
        return "PC Agent 응답 없음 (타임아웃 또는 연결 끊김)"

    status = result.get("status", "unknown")
    data = result.get("data", result)

    if status == "error":
        error_msg = data.get("error", "알 수 없는 오류") if isinstance(data, dict) else str(data)
        return f"PC Agent 오류: {error_msg}"

    if command_type == "screenshot":
        # 스크린샷은 base64 이미지 데이터
        if isinstance(data, dict) and data.get("image"):
            return f"![PC 스크린샷](data:image/png;base64,{data['image']})"
        return "스크린샷 캡처 완료 (이미지 데이터 없음)"

    if command_type == "shell":
        output = data.get("output", "") if isinstance(data, dict) else str(data)
        exit_code = data.get("exit_code", 0) if isinstance(data, dict) else 0
        result_text = f"```\n{output}\n```" if output else "(출력 없음)"
        if exit_code != 0:
            result_text += f"\n종료 코드: {exit_code}"
        return result_text

    if command_type in ("kakao_send", "kakao_read"):
        if isinstance(data, dict):
            if command_type == "kakao_send":
                return f"카카오톡 전송 완료: {data.get('recipient', '')}에게 메시지 전송"
            messages = data.get("messages", [])
            if messages:
                lines = [f"- {m.get('sender', '?')}: {m.get('text', '')}" for m in messages[:10]]
                return "최근 카카오톡 메시지:\n" + "\n".join(lines)
        return str(data)

    if command_type in ("file_list", "process_list"):
        if isinstance(data, dict):
            items = data.get("files", data.get("processes", []))
            if isinstance(items, list):
                lines = [f"- {item}" if isinstance(item, str) else f"- {item}" for item in items[:50]]
                return "\n".join(lines) if lines else "(항목 없음)"
        return str(data)

    if command_type == "browser_screenshot":
        if isinstance(data, dict) and data.get("image"):
            return f"![브라우저 스크린샷](data:image/png;base64,{data['image']})"
        return "브라우저 스크린샷 캡처 완료 (이미지 데이터 없음)"

    if command_type == "browser_launch":
        return "브라우저가 실행되었습니다."

    if command_type == "browser_navigate":
        url = data.get("url", "") if isinstance(data, dict) else str(data)
        return f"페이지 이동 완료: {url}"

    if command_type == "browser_click":
        selector = data.get("selector", "") if isinstance(data, dict) else str(data)
        return f"클릭 완료: `{selector}`"

    if command_type == "browser_fill":
        selector = data.get("selector", "") if isinstance(data, dict) else ""
        return f"텍스트 입력 완료: `{selector}`"

    if command_type == "browser_get_text":
        text = data.get("text", "") if isinstance(data, dict) else str(data)
        return f"페이지 텍스트:\n```\n{text}\n```"

    if command_type == "browser_eval":
        result_val = data.get("result", "") if isinstance(data, dict) else str(data)
        return f"JS 실행 결과:\n```\n{result_val}\n```"

    if command_type == "browser_tabs":
        if isinstance(data, dict):
            tabs = data.get("tabs", [])
            if isinstance(tabs, list):
                lines = [f"- [{t.get('index', i)}] {t.get('title', '?')} ({t.get('url', '')})" for i, t in enumerate(tabs)]
                return "열린 탭 목록:\n" + "\n".join(lines) if lines else "(탭 없음)"
        return str(data)

    # 기본: JSON 또는 문자열 그대로
    if isinstance(data, dict):
        import json
        return f"```json\n{json.dumps(data, ensure_ascii=False, indent=2)}\n```"
    return str(data)


# ── 내부 헬퍼 ────────────────────────────────────────────────────────────

def _match_any(text: str, patterns: list[str]) -> bool:
    """패턴 목록 중 하나라도 매칭되면 True."""
    return any(re.search(p, text) for p in patterns)


def _parse_kakao_send(message: str) -> Dict[str, Any]:
    """
    카카오톡 전송 메시지 파싱.
    예: "카카오톡으로 김대리에게 '회의 5분전' 보내줘"
    """
    result: Dict[str, Any] = {"type": "kakao_send", "recipient": "", "message": ""}

    # 수신자 추출: ~에게, ~한테
    recipient_match = re.search(r"(?:에게|한테)\s", message)
    if recipient_match:
        # 수신자 앞부분 추출
        before = message[:recipient_match.start()].strip()
        # 마지막 단어가 수신자
        words = before.split()
        if words:
            result["recipient"] = words[-1]

    # 메시지 내용 추출: 따옴표 안 또는 '~' 안
    msg_match = re.search(r"['\"](.+?)['\"]", message)
    if msg_match:
        result["message"] = msg_match.group(1)
    elif result["recipient"]:
        # 수신자 이후의 동사 제거하고 내용 추출
        after_recipient = message.split(result["recipient"])[-1] if result["recipient"] in message else ""
        # "에게 ~ 보내줘" 에서 내용 추출
        content_match = re.search(r"(?:에게|한테)\s+(.+?)(?:\s+(?:보내|전달|전송))", after_recipient)
        if content_match:
            result["message"] = content_match.group(1).strip("'\"")

    return result


def _extract_path(message: str) -> Optional[str]:
    """메시지에서 파일/폴더 경로 추출."""
    # Windows 경로 패턴: C:\..., D:\...
    win_match = re.search(r"[A-Za-z]:\\[^\s'\"]+", message)
    if win_match:
        return win_match.group()

    # Unix 경로 패턴: /home/...
    unix_match = re.search(r"/[^\s'\"]+", message)
    if unix_match:
        return unix_match.group()

    return None


def _extract_url(message: str) -> Optional[str]:
    """메시지에서 URL 추출."""
    url_match = re.search(r"https?://[^\s'\"]+", message)
    if url_match:
        return url_match.group()
    # www로 시작하는 도메인도 처리
    www_match = re.search(r"www\.[^\s'\"]+", message)
    if www_match:
        return "https://" + www_match.group()
    return None


def _extract_quoted(message: str, index: int = 0) -> Optional[str]:
    """따옴표로 감싼 문자열 중 index번째 추출."""
    matches = re.findall(r"['\"](.+?)['\"]", message)
    if matches and index < len(matches):
        return matches[index]
    return None


def _parse_shell_command(msg_lower: str, original: str) -> Optional[Dict[str, Any]]:
    """자연어에서 셸 명령 추출."""
    # 프로그램 실행 패턴
    _app_map = {
        "메모장": "notepad.exe",
        "notepad": "notepad.exe",
        "계산기": "calc.exe",
        "calculator": "calc.exe",
        "탐색기": "explorer.exe",
        "explorer": "explorer.exe",
        "크롬": "start chrome",
        "chrome": "start chrome",
        "엣지": "start msedge",
        "edge": "start msedge",
        "cmd": "cmd.exe",
        "파워셸": "powershell.exe",
        "powershell": "powershell.exe",
        "터미널": "wt.exe",
        "terminal": "wt.exe",
    }

    for keyword, cmd in _app_map.items():
        if keyword in msg_lower and any(w in msg_lower for w in ("열어", "실행", "시작", "켜", "run", "open", "start")):
            return {"type": "shell", "command": cmd}

    return None
