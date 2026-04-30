# AADS Chat-Direct Edit Changelog (aads-server)

## [2026-04-29 09:35:04 KST] [aads-server] app/services/chat_service.py
- Chat-Direct 수정: patch:                    OR ($1 = 'NTV2' AND '→                   OR ($1 = 'NTV2' AND '

## [2026-04-29 09:35:48 KST] [aads-server] app/services/chat_service.py
- Chat-Direct 수정: patch:                OR ($1 = 'NTV2' AND 'NT' →               OR ($1 = 'NTV2' AND 'NT' 

## [2026-04-29 09:35:50 KST] [aads-server] app/services/chat_service.py
- Chat-Direct 수정: patch:                OR ($1 = 'NTV2' AND 'NT' →               OR ($1 = 'NTV2' AND 'NT' 

## [2026-04-29 09:37:20 KST] [aads-server] scripts/_patch_role_dropdown.py
- Chat-Direct 수정: write: scripts/_patch_role_dropdown.py

## [2026-04-29 09:52:07 KST] [aads-server] migrations/069_seed_project_ux_role_overlays.sql
- Chat-Direct 수정: run_remote_command: docker exec aads-server tail -100 /var/log/aads-api.log | grep -i "error\|except

## [2026-04-29 10:37:00 KST] [aads-server] app/logging_config.py
- Chat-Direct 수정: patch: """structlog 표준화 설정 — 구조화 JSON 로깅."""
im→"""structlog 표준화 설정 — 구조화 JSON 로깅 + File

## [2026-04-29 19:45:38 KST] [aads-server] docs/CHANGELOG-go100-direct.md
- Chat-Direct 수정: run_remote_command: ls /root/aads/aads-server/device_sdk/pc_agent/

## [2026-04-29 20:41:31 KST] [aads-server] scripts/sync-to-contabo.sh
- Chat-Direct 수정: write: scripts/sync-to-contabo.sh

## [2026-04-30 06:07:41 KST] [aads-server] app/main.py
- Chat-Direct 수정: patch:     allow_origins=["https://aads.newtalk→    allow_origins=["https://aads.newtalk

## [2026-04-30 12:57:45 KST] [aads-server] docker-compose.yml
- Chat-Direct 수정: patch:       # Agent SDK 번들 CLI 인증 (OAuth 토큰 직접→      # Agent SDK 번들 CLI 인증 (OAuth 토큰 직접

## [2026-04-30 18:59:19 KST] [aads-server] migrations/077_role_taxonomy_and_business_roles.sql
- Chat-Direct 수정: run_remote_command: grep -r "playwright" /root/aads/aads-server/supervisord.conf

## [2026-04-30 19:58:00 KST] [aads-server] chat streaming reliability
- Chat-Direct 수정: 스트리밍 중 active API 재시작 방지, blue/green resume owner 분리, placeholder 보존, 강제 끊김 e2e 및 브라우저 확인 기록.
