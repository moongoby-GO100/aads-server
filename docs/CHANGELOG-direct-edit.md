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

## [2026-05-03 20:28:12 KST] [aads-server] android_agent/app/src/main/java/kr/newtalk/aads/agent/AndroidCommandHandlers.java
- Chat-Direct 수정: patch:             JSONArray jsonValues = new J→            JSONArray jsonValues = new J

## [2026-05-03 20:28:29 KST] [aads-server] /root/aads/aads-server/android_agent/app/src/main/java/kr/newtalk/aads/agent/AndroidCommandHandlers.java
- Chat-Direct 수정: patch:             JSONArray jsonValues = new J→            JSONArray jsonValues = new J

## [2026-05-03 20:28:35 KST] [aads-server] android_agent/app/src/main/java/kr/newtalk/aads/agent/AndroidCommandHandlers.java
- Chat-Direct 수정: patch:             JSONArray jsonValues = new J→            JSONArray jsonValues = new J

## [2026-05-03 20:29:12 KST] [aads-server] android_agent/app/src/main/java/kr/newtalk/aads/agent/AndroidCommandHandlers.java
- Chat-Direct 수정: run_remote_command: cd /root/aads/aads-server/android_agent && cp app/src/main/java/kr/newtalk/aads/

## [2026-05-03 20:53:30 KST] [aads-server/aads-dashboard] runner_response 채팅 표시 복구
- Chat-Direct 수정: `app/services/chat_service.py`, `app/routers/chat.py`, `/root/aads/aads-dashboard/src/app/chat/page.tsx`
- 내용: DB에 저장된 AI 검수/상태 보고(`intent=runner_response`)가 채팅 본문에 표시되도록 백엔드 조회 필터와 프론트 시스템 메시지 분류를 수정. 대시보드 blue-green 배포 완료.

## [2026-05-04 08:20:33 KST] [aads-server/aads-dashboard] Android Agent + runner_response follow-up
- Chat-Direct 수정: Android sensor JSON hardening, `runner_response` main chat visibility, Pipeline Runner concurrency docs/config.
- 기술문서: `docs/reports/20260504_ANDROID_AGENT_CHAT_VISIBILITY_TECHNICAL.md`
- HANDOVER: `HANDOVER.md` 2026-05-04 현재 진행 상태 추가.

## [2026-05-06 10:02:20 KST] [aads-server] app/services/pc_agent_manager.py
- Chat-Direct 수정: write: app/services/pc_agent_manager.py

## [2026-05-06 10:02:23 KST] [aads-server] app/services/pc_agent_manager.py
- Chat-Direct 수정: run_remote_command: grep -rn "list_messages\|list_messages_cursor\|fields=minimal\|fields=full" /roo

## [2026-05-06 10:02:36 KST] [aads-server] docs/CHANGELOG-direct-edit.md
- Chat-Direct 수정: run_remote_command: grep -rn "list_messages\|list_messages_cursor\|fields=minimal\|fields=full" /roo

## [2026-05-06 10:03:45 KST] [aads-server] app/api/pc_agent.py
- Chat-Direct 수정: write: app/api/pc_agent.py

## [2026-05-06 10:03:48 KST] [aads-server] app/api/pc_agent.py
- Chat-Direct 수정: run_remote_command: grep -rn "quality_score\|memory_facts\|extract_facts\|reflexion\|sleep_time" /ro

## [2026-05-06 10:05:12 KST] [aads-server] app/services/pc_agent_manager.py
- Chat-Direct 수정: patch: # 싱글톤 인스턴스 — hot-reload 시 기존 연결 상태 보존
im→# 싱글톤 인스턴스 — hot-reload 시 기존 연결 상태 보존
im

## [2026-05-06 10:06:48 KST] [aads-server] app/api/pipeline_runner.py
- Chat-Direct 수정: run_remote_command: docker exec aads-server cat /app/app/api/pc_agent.py | wc -l

## [2026-05-06 10:06:55 KST] [aads-server] migrations/078_pipeline_runner_active_dedup.sql
- Chat-Direct 수정: run_remote_command: docker exec aads-server cat /app/app/api/pc_agent.py | wc -l

## [2026-05-06 10:07:07 KST] [aads-server] scripts/pipeline-runner.sh
- Chat-Direct 수정: run_remote_command: docker exec aads-server curl -s -X POST http://localhost:8080/api/v1/pc-agent/gr

## [2026-05-06 10:07:23 KST] [aads-server] HANDOVER.md
- Chat-Direct 수정: run_remote_command: docker exec aads-server curl -s http://localhost:8080/api/v1/ops/health-check | 

## [2026-05-06 10:07:26 KST] [aads-server] docs/reports/20260506_CHAT_LIGHTWEIGHT_PLAN_v2.md
- Chat-Direct 수정: write: docs/reports/20260506_CHAT_LIGHTWEIGHT_PLAN_v2.md

## [2026-05-06 10:07:27 KST] [aads-server] HANDOVER.md
- Chat-Direct 수정: run_remote_command: docker exec aads-server cat /app/pc_agent/agent.py | wc -l

## [2026-05-06 10:07:30 KST] [aads-server] docs/pipeline-runner/PIPELINE-RUNNER-API-REFERENCE.md
- Chat-Direct 수정: run_remote_command: docker exec aads-server curl -s http://localhost:8080/api/v1/ops/health-check | 

## [2026-05-06 10:07:37 KST] [aads-server] docs/pipeline-runner/PIPELINE-RUNNER-ARCHITECTURE.md
- Chat-Direct 수정: run_remote_command: docker exec aads-server curl -s http://localhost:8080/api/v1/ops/health-check | 

## [2026-05-06 10:07:41 KST] [aads-server] docs/pipeline-runner/PIPELINE-RUNNER-API-REFERENCE.md
- Chat-Direct 수정: run_remote_command: docker exec aads-server cat /app/pc_agent/agent.py | wc -l

## [2026-05-06 10:07:47 KST] [aads-server] docs/pipeline-runner/PIPELINE-RUNNER-ARCHITECTURE.md
- Chat-Direct 수정: run_remote_command: docker exec aads-server cat /app/pc_agent/agent.py | wc -l

## [2026-05-06 10:07:57 KST] [aads-server] app/services/tool_executor.py
- Chat-Direct 수정: run_remote_command: find /root/aads -type f -name "*.py" | grep -E "(context|prompt|builder|memory|e

## [2026-05-06 10:08:00 KST] [aads-server] app/services/tool_executor.py
- Chat-Direct 수정: run_remote_command: docker exec aads-server grep -n "pc_agent" /app/app/main.py

## [2026-05-06 10:08:01 KST] [aads-server] docs/reports/20260506_CHAT_LIGHTWEIGHT_PLAN_v2.md
- Chat-Direct 수정: run_remote_command: docker exec aads-server grep -n "pc_agent" /app/app/main.py

## [2026-05-06 10:08:02 KST] [aads-server] docs/reports/20260506_CHAT_LIGHTWEIGHT_V2.md
- Chat-Direct 수정: run_remote_command: docker exec aads-server grep -n "pc_agent" /app/app/main.py

## [2026-05-06 10:08:03 KST] [aads-server] migrations/078_pipeline_runner_active_dedup.sql
- Chat-Direct 수정: run_remote_command: docker exec aads-server grep -n "pc_agent" /app/app/main.py

## [2026-05-06 10:08:04 KST] [aads-server] reports/20260506_AADS_tool_audit_and_improvement_report.md
- Chat-Direct 수정: run_remote_command: docker exec aads-server grep -n "pc_agent" /app/app/main.py

## [2026-05-06 10:10:16 KST] [aads-server] app/services/pc_agent_manager.py
- Chat-Direct 수정: patch:     def __init__(self, agent_id: str, we→    def __init__(self, agent_id: str, we

## [2026-05-06 10:10:32 KST] [aads-server] pc_agent/agent.py
- Chat-Direct 수정: write: pc_agent/agent.py

## [2026-05-06 10:10:33 KST] [aads-server] aads-dashboard/src/app/chat/page.tsx
- Chat-Direct 수정: run_remote_command: cd /tmp/aads-wt-runner-27bf4563 && git diff --stat HEAD~1

## [2026-05-06 10:10:34 KST] [aads-server] app/routers/chat.py
- Chat-Direct 수정: run_remote_command: cd /tmp/aads-wt-runner-27bf4563 && git diff --stat HEAD~1

## [2026-05-06 10:10:37 KST] [aads-server] app/services/chat_service.py
- Chat-Direct 수정: run_remote_command: cd /tmp/aads-wt-runner-27bf4563 && git diff --stat HEAD~1

## [2026-05-06 10:10:38 KST] [aads-server] tests/unit/test_chat_service.py
- Chat-Direct 수정: run_remote_command: cd /tmp/aads-wt-runner-27bf4563 && git diff --stat HEAD~1

## [2026-05-06 10:12:58 KST] [aads-server] docs/reports/20260506_CHAT_LIGHTWEIGHT_V2.md
- Chat-Direct 수정: write: docs/reports/20260506_CHAT_LIGHTWEIGHT_V2.md

## [2026-05-06 10:13:04 KST] [aads-server] docs/reports/20260506_CHAT_LIGHTWEIGHT_V2.md
- Chat-Direct 수정: run_remote_command: docker exec aads-server cat /var/log/aads-api.log 2>/dev/null | grep -i "pc_agen

## [2026-05-06 10:16:23 KST] [aads-server] aads-dashboard/src/app/chat/page.tsx
- Chat-Direct 수정: run_remote_command: find /root/aads -name "*.py" -path "*pc*agent*" -not -path "*/node_modules/*" -n

## [2026-05-06 10:16:24 KST] [aads-server] app/models/chat.py
- Chat-Direct 수정: run_remote_command: find /root/aads -name "*.py" -path "*pc*agent*" -not -path "*/node_modules/*" -n

## [2026-05-06 10:16:25 KST] [aads-server] app/routers/chat.py
- Chat-Direct 수정: run_remote_command: find /root/aads -name "*.py" -path "*pc*agent*" -not -path "*/node_modules/*" -n

## [2026-05-06 10:16:31 KST] [aads-server] docs/CHANGELOG-direct-edit.md
- Chat-Direct 수정: run_remote_command: cd /tmp/aads-wt-runner-1de4489e && git diff HEAD~1 -- aads-dashboard/src/app/cha

## [2026-05-06 10:19:20 KST] [aads-server] app/api/pc_agent.py
- Chat-Direct 수정: patch:     except (WebSocketDisconnect, asyncio→    except (WebSocketDisconnect, asyncio

## [2026-05-06 10:19:24 KST] [aads-server] app/api/pc_agent.py
- Chat-Direct 수정: run_remote_command: sed -n '30,130p' /root/aads/aads-dashboard/src/services/chatApi.ts

## [2026-05-06 10:19:25 KST] [aads-server] app/api/pc_agent.py
- Chat-Direct 수정: run_remote_command: cd /tmp/aads-wt-runner-567dd48a && git diff HEAD~1 -- app/services/pc_agent_mana
