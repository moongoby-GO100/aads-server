#!/usr/bin/env python3
"""
AADS LiteLLM Runner — MCP + LangGraph ReAct 에이전트
=======================================================
Claude Runner 대신 LiteLLM 프록시 경유 모델(Gemini/DeepSeek/Qwen 등)로 코딩 작업 수행.
MCP 도구(filesystem/git/memory)를 langchain-mcp-adapters로 직접 연결.

사용법:
    python3 scripts/litellm_runner.py \\
        --model gemini-2.5-flash \\
        --instruction "app/api/health.py에 /ping 엔드포인트 추가" \\
        --workdir /app

환경변수:
    LITELLM_BASE_URL   : LiteLLM 프록시 URL (기본: http://aads-litellm:4000)
    LITELLM_MASTER_KEY : LiteLLM 마스터 키
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time

from langchain_core.messages import HumanMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("litellm_runner")

# ── 상수 ────────────────────────────────────────────────────────────────────

_DEFAULT_BASE_URL = os.getenv("LITELLM_BASE_URL", "http://aads-litellm:4000")
_MASTER_KEY = os.getenv("LITELLM_MASTER_KEY", "")

# MCP 서버 포트 (aads-server 내부, supervisord 관리)
_MCP_SERVERS = {
    "filesystem": "http://localhost:8765/sse",
    "git": "http://localhost:8766/sse",
    "memory": "http://localhost:8767/sse",
}

# 가드레일
_MAX_ITERATIONS = 15
_MAX_WALL_SECONDS = 3600  # 1시간

# 시스템 프롬프트
_SYSTEM_PROMPT = """당신은 AADS 자율 코딩 에이전트입니다.
주어진 지시에 따라 코드를 수정·생성하고, MCP 도구(filesystem/git)를 활용해 파일을 읽고 쓰세요.

핵심 규칙:
1. 작업 전 반드시 관련 파일을 먼저 읽어 현재 코드를 파악하세요.
2. 코드 수정 후 문법 오류가 없는지 확인하세요.
3. git 도구로 변경사항을 확인하되, 커밋은 하지 마세요 (Runner가 처리).
4. 작업이 완료되면 "작업 완료: [요약]" 형태로 마무리하세요.
5. --no-verify 절대 금지, .env 파일 절대 수정 금지.
6. MCP filesystem 경로는 반드시 상대경로 사용. 예: read_file("app/api/health.py") (O), read_file("/app/api/health.py") (X).
7. 도구 호출 에러 시 경로를 수정하여 재시도하세요.
"""


# ── 핵심 에이전트 ─────────────────────────────────────────────────────────


async def run_agent(model: str, instruction: str, workdir: str) -> str:
    """LangGraph ReAct + MCP 에이전트 실행."""

    logger.info("Starting LiteLLM runner: model=%s workdir=%s", model, workdir)
    logger.info("LiteLLM base URL: %s", _DEFAULT_BASE_URL)

    # LiteLLM 프록시 경유 ChatOpenAI 클라이언트
    llm = ChatOpenAI(
        model=model,
        base_url=f"{_DEFAULT_BASE_URL}/v1",
        api_key=_MASTER_KEY,
        temperature=1 if "kimi" in model.lower() else 0,
    )

    # MCP 서버 연결 설정 (SSE transport)
    mcp_config = {
        name: {"url": url, "transport": "sse"}
        for name, url in _MCP_SERVERS.items()
    }

    start_time = time.time()
    output_lines: list[str] = []
    iteration = 0

    try:
        # langchain-mcp-adapters 0.2.0: async with 제거됨, 직접 await 사용
        mcp_client = MultiServerMCPClient(mcp_config)
        tools = await mcp_client.get_tools()
        logger.info("MCP tools loaded: %d tools from %d servers", len(tools), len(_MCP_SERVERS))

        # 도구 이름 목록 출력 (검수용)
        tool_names = [t.name for t in tools]
        logger.info("Available tools: %s", tool_names[:20])

        # ReAct 에이전트 생성
        agent = create_react_agent(
            model=llm,
            tools=tools,
            prompt=_SYSTEM_PROMPT,
        )
        # Tool 에러를 LLM에 전달하여 재시도 가능하게 함
        agent.nodes['tools'].handle_tool_errors = True

        # 작업 컨텍스트 구성
        full_instruction = f"""MCP filesystem 루트 = 프로젝트 루트.
파일 경로는 상대경로로 지정하세요 (예: app/api/health.py, scripts/deploy.sh).

지시사항:
{instruction}

작업을 시작하세요."""

        messages = [HumanMessage(content=full_instruction)]

        # 에이전트 루프
        async for chunk in agent.astream(
            {"messages": messages},
            config={"recursion_limit": _MAX_ITERATIONS},
        ):
            elapsed = time.time() - start_time
            if elapsed > _MAX_WALL_SECONDS:
                logger.warning("Max wall time exceeded, stopping.")
                break

            # 결과 수집
            if "agent" in chunk:
                for msg in chunk["agent"].get("messages", []):
                    content = getattr(msg, "content", "")
                    if content:
                        output_lines.append(str(content))
                        print(content, flush=True)

            elif "tools" in chunk:
                iteration += 1
                for msg in chunk["tools"].get("messages", []):
                    tool_name = getattr(msg, "name", "unknown")
                    logger.info("[iter %d] tool_result: %s", iteration, tool_name)

    except Exception as e:
        logger.error("Agent error: %s", e, exc_info=True)
        print(f"ERROR: {e}", flush=True)
        sys.exit(1)  # Shell Runner 실패 감지 -> Claude 폴백

    elapsed = time.time() - start_time
    logger.info("Runner completed in %.1fs, iterations=%d", elapsed, iteration)

    return "\n".join(output_lines) if output_lines else "작업 완료 (출력 없음)"


# ── 도구 목록 조회 ────────────────────────────────────────────────────────


async def list_tools() -> None:
    """사용 가능한 MCP 도구 목록 출력."""
    mcp_config = {
        name: {"url": url, "transport": "sse"}
        for name, url in _MCP_SERVERS.items()
    }
    # langchain-mcp-adapters 0.2.0: 직접 await 사용
    mcp_client = MultiServerMCPClient(mcp_config)
    tools = await mcp_client.get_tools()
    print(f"\n사용 가능한 MCP 도구 ({len(tools)}개):\n")
    for t in tools:
        desc = (t.description or "")[:60]
        print(f"  [{t.name}] {desc}")


# ── CLI ───────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="AADS LiteLLM Runner")
    parser.add_argument("--model", default="gemini-2.5-flash",
                        help="LiteLLM 모델명 (예: gemini-2.5-flash, deepseek-chat, qwen3-235b)")
    parser.add_argument("--instruction", "-i", default="",
                        help="실행할 작업 지시문")
    parser.add_argument("--instruction-file", "-f", default="",
                        help="파일에서 지시문 읽기 (긴 instruction용, docker exec arg 깨짐 방지)")
    parser.add_argument("--workdir", "-w",
                        default="/app",
                        help="작업 디렉토리")
    parser.add_argument("--list-tools", action="store_true",
                        help="사용 가능한 MCP 도구 목록 출력 후 종료")
    args = parser.parse_args()

    if args.list_tools:
        asyncio.run(list_tools())
        return

    # --instruction-file 우선: 파일에서 읽어 args.instruction 덮어쓰기
    if args.instruction_file:
        with open(args.instruction_file, "r", encoding="utf-8") as f:
            args.instruction = f.read().strip()

    if not args.instruction:
        parser.error("--instruction 또는 --instruction-file 필수")

    if not _MASTER_KEY:
        logger.error("LITELLM_MASTER_KEY 환경변수가 설정되지 않았습니다.")
        sys.exit(1)

    result = asyncio.run(run_agent(
        model=args.model,
        instruction=args.instruction,
        workdir=args.workdir,
    ))
    # 에이전트 실패 시 exit(1) — Shell Runner가 Claude 폴백으로 전환하도록
    if isinstance(result, str) and result.startswith("FAILED:"):
        logger.error("Runner failed: %s", result[:200])
        sys.exit(1)


if __name__ == "__main__":
    main()
