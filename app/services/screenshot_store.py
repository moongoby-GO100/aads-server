"""레시피 실행이 찍은 PNG 를 사람이 열 수 있는 URL 로 남긴다.

`work_recipe/executor.py` 는 단계마다 `page.screenshot()` 을 찍고 sha256 과
바이트 수만 증거에 적은 뒤 **바이트를 버렸다**. 그래서 실행은 "화면 증거 3건
저장" 이라고 보고하는데 정작 그 화면을 열어 볼 방법이 없었다 — 2026-09-21
대표님이 "왜 아티팩트에 안 띄우냐" 고 지적한 것이 이 구멍이다.

저장 경로는 `capture_screenshot` 도구(`app/api/ceo_chat_tools.py`)가 쓰는 것과
같은 곳이다. nginx 가 `/var/www/certbot/screenshots` 를
`https://aads.newtalk.kr/screenshots/` 로 서빙한다. 컨테이너에는 그 디렉터리가
마운트돼 있지 않으므로 호스트로 ssh 하여 stdin 으로 밀어 넣는다(base64 를
argv 로 넘기면 길이 제한에 걸린다).

증거 수집은 읽기를 실패로 만들면 안 된다. 저장이 실패하면 None 을 돌려주고,
호출측은 sha256 만 남긴 기존 증거를 그대로 쓴다.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

PUBLIC_BASE = "https://aads.newtalk.kr/screenshots"
HOST_DIR = "/var/www/certbot/screenshots"
_SAVE_TIMEOUT_SEC = 15.0


async def save_png(data: bytes, *, prefix: str = "recipe") -> str | None:
    """PNG 바이트를 호스트에 저장하고 공개 URL 을 돌려준다. 실패하면 None."""
    if not isinstance(data, bytes) or not data:
        return None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{prefix}_{ts}_{uuid.uuid4().hex[:6]}.png"
    save_cmd = f"mkdir -p {HOST_DIR} && cat > {HOST_DIR}/{filename}"
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "StrictHostKeyChecking=no",
            "root@host.docker.internal",
            save_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(data), timeout=_SAVE_TIMEOUT_SEC)
    except Exception:
        # 저장 실패는 증거를 줄일 뿐이며 실행 결과를 뒤집지 않는다.
        return None
    if proc.returncode != 0:
        return None
    return f"{PUBLIC_BASE}/{filename}"
