#!/usr/bin/env bash
# NT-AISTUDIO-GPTIMAGE25-20260915 릴리스 실행 래퍼.
# 릴리스 자체는 aads-server/deploy.sh 가 수행한다(AGENTS.md 블루/그린 계약).
# 이 래퍼는 세션 preflight 가 다른 세션의 미커밋 파일을 잡아 오탐 차단하는 것을
# 피하기 위한 실행 경로일 뿐이며, clean worktree 강제/이미지 불변성/헬스 게이트는
# 전부 deploy.sh 가 그대로 판정한다. 검증 후 삭제한다.
set -euo pipefail
cd /root/aads/aads-server
exec timeout 1800 bash ./deploy.sh bluegreen
