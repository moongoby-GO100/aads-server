#!/usr/bin/env python3
"""
.bashrc OAuth 토큰 → LiteLLM 라우팅 전환 패치 (2026-04-02)
OAuth 토큰(sk-ant-oat01-)은 api.anthropic.com REST에서 401 반환
→ 터미널 Claude Code도 LiteLLM 프록시(포트 4000) 경유로 전환
"""
import shutil, sys

path = "/root/.bashrc"
shutil.copy(path, path + ".bak_aads")

with open(path, "r") as f:
    content = f.read()

old_block = """# Claude Code OAuth 토큰 주입 (터미널 인증 — 2026-04-01)
# Gmail 계정(1순위) 사용, Naver 캐시된 .claude.json 무시
_AADS_ENV="/root/aads/aads-server/.env"
if [ -f "$_AADS_ENV" ]; then
    _GMAIL_TOKEN=$(grep ^ANTHROPIC_AUTH_TOKEN= "$_AADS_ENV" | cut -d= -f2- | tr -d '[:space:]')
    if [ -n "$_GMAIL_TOKEN" ]; then
        export CLAUDE_CODE_OAUTH_TOKEN="$_GMAIL_TOKEN"
        # LiteLLM 라우팅 비활성화 (OAuth 토큰은 직접 api.anthropic.com으로)
        unset ANTHROPIC_API_KEY ANTHROPIC_BASE_URL  # AUTH_TOKEN 전환 전 구버전 정리  # AUTH_TOKEN 전환 전 구버전 정리
    fi
    unset _GMAIL_TOKEN _AADS_ENV
fi"""

new_block = """# Claude Code → LiteLLM 프록시 라우팅 (2026-04-02 수정)
# OAuth 토큰(sk-ant-oat01-)은 api.anthropic.com REST API에서 401 반환 → LiteLLM 경유로 전환
# LiteLLM이 내부적으로 OAuth/API Key 슬롯 관리 및 계정 전환 처리
export ANTHROPIC_API_KEY="sk-litellm"  # not ANTHROPIC_AUTH_TOKEN — LiteLLM proxy dummy, not a real key
export ANTHROPIC_BASE_URL="http://localhost:4000"
unset CLAUDE_CODE_OAUTH_TOKEN"""

if old_block in content:
    content = content.replace(old_block, new_block)
    with open(path, "w") as f:
        f.write(content)
    print("OK: .bashrc 패치 완료 — LiteLLM 라우팅 적용")
else:
    print("SKIP: 대상 블록 없음 (내용 확인 필요)")
    for i, line in enumerate(content.splitlines(), 1):
        if "CLAUDE_CODE_OAUTH" in line or "OAUTH" in line or "OAuth" in line:
            print(f"  L{i}: {line}")
    sys.exit(1)
