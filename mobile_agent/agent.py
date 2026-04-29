"""AndroidAgent — Termux 기반 모바일 에이전트 메인."""
from __future__ import annotations

import logging
import os

from device_sdk.client import DeviceAgent

logger = logging.getLogger(__name__)


class AndroidAgent(DeviceAgent):
    def __init__(
        self,
        server_url: str | None = None,
        token: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        from mobile_agent.config import SERVER_URL, AGENT_TOKEN

        super().__init__(
            server_url=server_url or SERVER_URL,
            token=token or AGENT_TOKEN,
            agent_id=agent_id,
            device_type="android",
        )
        self._load_commands()

    def _load_commands(self) -> None:
        from mobile_agent.commands import AVAILABLE_COMMANDS
        for cmd_type, handler in AVAILABLE_COMMANDS.items():
            self.dispatcher.register_handler(cmd_type, handler)
        logger.info("Android 커맨드 %d개 로드", len(AVAILABLE_COMMANDS))

    async def on_connect(self) -> None:
        logger.info("AADS 서버 연결 성공 (Android)")

    async def on_disconnect(self) -> None:
        logger.info("AADS 서버 연결 종료 (Android)")

    @staticmethod
    def _get_os_info() -> str:
        prefix = os.environ.get("PREFIX", "")
        return f"Android (Termux: {bool(prefix)})"


async def main() -> None:
    import asyncio
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    agent = AndroidAgent()
    await agent.run()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
