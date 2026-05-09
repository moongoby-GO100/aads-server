# AADS Chat-Direct Edit Changelog (aads-dashboard)

## [2026-04-29 08:20:13 KST] [aads-dashboard] src/app/chat/page.tsx
- Chat-Direct 수정: run_remote_command: cp /tmp/aads-wt-runner-81832c42/aads-dashboard/src/app/chat/page.tsx /root/aads/

## [2026-04-29 09:36:07 KST] [aads-dashboard] src/app/chat/page.tsx
- Chat-Direct 수정: run_remote_command: sed -i '5104a\            {roleKey && !roleOptions.some(r => r.id === roleKey) &

## [2026-04-29 09:37:36 KST] [aads-dashboard] src/app/chat/page.tsx.bak_role
- Chat-Direct 수정: run_remote_command: python3 /root/aads/aads-server/scripts/_patch_role_dropdown.py

## [2026-04-29 19:43:15 KST] [aads-dashboard] tsconfig.tsbuildinfo
- Chat-Direct 수정: run_remote_command: ssh -i /root/.ssh/id_ed25519 root@5.104.86.116 "chmod a+r /etc/apt/keyrings/dock

## [2026-05-09 07:26:38 KST] [aads-dashboard] ../aads-dashboard/src/components/chat/SlashCommandMenu.tsx
- Chat-Direct 수정: patch:   { cmd: "/도움말", desc: "사용 가능한 명령어 목록", →  { cmd: "/토론", desc: "멀티 LLM 토론 시작", ex

## [2026-05-09 07:26:55 KST] [aads-dashboard] src/components/chat/SlashCommandMenu.tsx
- Chat-Direct 수정: run_remote_command: python3 -c "
import pathlib
p = pathlib.Path('/root/aads/aads-dashboard/src/comp

## [2026-05-09 07:28:48 KST] [aads-dashboard] src/components/chat/DiscussionPanel.tsx
- Chat-Direct 수정: run_remote_command: python3 << 'PYEOF'
import pathlib
content = r'''"use client";

import React, { u

## [2026-05-09 07:30:56 KST] [aads-dashboard] src/app/chat/ChatInput.tsx
- Chat-Direct 수정: run_remote_command: python3 -c "
import pathlib
p = pathlib.Path('/root/aads/aads-dashboard/src/app/

## [2026-05-09 07:31:26 KST] [aads-dashboard] src/app/chat/page.tsx
- Chat-Direct 수정: run_remote_command: python3 -c "
import pathlib
p = pathlib.Path('/root/aads/aads-dashboard/src/app/
