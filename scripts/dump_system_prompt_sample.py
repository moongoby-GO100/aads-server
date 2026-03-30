"""일회성: build_messages_context system_prompt 덤프 → reports/*.md"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.db_pool import init_pool, get_pool
from app.services.context_builder import build_messages_context


async def main() -> None:
    await init_pool()
    pool = get_pool()
    sid = str(uuid.uuid4())
    ws = "[TEST] PromptDump"
    async with pool.acquire() as conn:
        _msgs, sp = await build_messages_context(
            workspace_name=ws,
            session_id=sid,
            raw_messages=[{"role": "user", "content": "안녕"}],
            base_system_prompt="",
            db_conn=conn,
        )
    kst = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M KST")
    out_path = "/app/reports/system_prompt_full_dump_20260330.md"
    header = (
        "# AADS 시스템 프롬프트 전체 덤프 (샘플)\n\n"
        f"- **생성 시각**: {kst}\n"
        f"- **조건**: 워크스페이스 `{ws}`, `base_system_prompt` 비어 있음, "
        f"첫 사용자 메시지 `안녕`, 세션 id `{sid}`\n"
        "- **내용**: `build_messages_context()` 반환 `system_prompt` 전체\n\n"
        f"- **통계**: {len(sp):,} 문자\n\n---\n\n"
    )
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("```text\n")
        f.write(sp)
        f.write("\n```\n")
    print("OK", out_path, len(sp))


if __name__ == "__main__":
    asyncio.run(main())
