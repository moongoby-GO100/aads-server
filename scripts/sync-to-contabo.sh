#!/bin/bash
# AADS 서버68 → Contabo 도쿄 동기화 스크립트
# 용도: DNS 전환 전까지 코드/설정 실시간 동기화

CONTABO="root@5.104.86.116"
SSH_KEY="/root/.ssh/id_ed25519"
LOG="/tmp/contabo-sync.log"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] sync start" >> $LOG

# 1. aads-server 코드 동기화 (git 추적 파일 + 주요 설정)
rsync -az --delete \
  --exclude='.git' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.env' \
  --exclude='node_modules' \
  --exclude='dist/' \
  --exclude='*.egg-info' \
  --exclude='app.db' \
  --exclude='aads.db' \
  --exclude='RESULT*.md' \
  --exclude='RUNNER_TEST*.md' \
  --exclude='.bak_aads*' \
  -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no" \
  /root/aads/aads-server/ $CONTABO:/root/aads/aads-server/ \
  >> $LOG 2>&1

# 2. aads-dashboard 코드 동기화
rsync -az --delete \
  --exclude='.git' \
  --exclude='node_modules' \
  --exclude='.next' \
  --exclude='.env.local' \
  --exclude='dist/' \
  -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no" \
  /root/aads/aads-dashboard/ $CONTABO:/root/aads/aads-dashboard/ \
  >> $LOG 2>&1

# 3. aads-docs 동기화
rsync -az \
  -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no" \
  /root/aads/aads-docs/ $CONTABO:/root/aads/aads-docs/ \
  >> $LOG 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] sync done" >> $LOG
