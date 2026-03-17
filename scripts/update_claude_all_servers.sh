#!/bin/bash
# Claude Code CLI + Agent SDK 전 서버 자동 업데이트
# 크론: 매주 월요일 05:00 KST
LOG="/var/log/claude_update.log"
echo "=== $(date) ===" >> "$LOG"

# 68서버
echo "[68] CLI" >> "$LOG"
npm update -g @anthropic-ai/claude-code >> "$LOG" 2>&1
echo "[68] SDK" >> "$LOG"
python3.11 -m pip install --upgrade claude-agent-sdk >> "$LOG" 2>&1

# 68 Docker (Agent SDK는 이미지 빌드 시 포함, CLI는 번들)
echo "[68-docker] SDK" >> "$LOG"
docker exec aads-server pip install --upgrade claude-agent-sdk >> "$LOG" 2>&1

# 211서버
echo "[211] CLI" >> "$LOG"
ssh 211.188.51.113 "npm update -g @anthropic-ai/claude-code" >> "$LOG" 2>&1
echo "[211] SDK" >> "$LOG"
ssh 211.188.51.113 "pip3 install --break-system-packages --upgrade claude-agent-sdk" >> "$LOG" 2>&1

# 114서버
echo "[114] CLI" >> "$LOG"
ssh -p 7916 114.207.244.86 "npm update -g @anthropic-ai/claude-code" >> "$LOG" 2>&1
echo "[114] SDK" >> "$LOG"
ssh -p 7916 114.207.244.86 "python3.11 -m pip install --upgrade claude-agent-sdk" >> "$LOG" 2>&1

# 버전 확인
echo "[versions]" >> "$LOG"
echo "68: $(claude --version 2>&1)" >> "$LOG"
echo "211: $(ssh 211.188.51.113 'claude --version' 2>&1)" >> "$LOG"
echo "114: $(ssh -p 7916 114.207.244.86 'claude --version' 2>&1)" >> "$LOG"
echo "done" >> "$LOG"
