# AADS 채팅창 전수 조사 및 수정 보고서
**일시**: 2026-03-09 17:00~19:00 KST
**작성**: Claude Code (Opus 4.6)

## 1. 조사 범위
- 백엔드: FastAPI (ceo_chat, tools, MCP, chat_service, routers)
- 프론트엔드: Next.js (page.tsx, Sidebar, chatApi, useChatSSE)
- 데이터베이스: PostgreSQL (세션, 메시지, FK, 정합성)
- MCP 서버: filesystem, git, memory, postgres
- 인프라: Docker, 디스크, logrotate

## 2. 발견 이슈 및 수정 (17건)

### CRITICAL (3건)
| # | 파일 | 이슈 | 수정 |
|---|------|------|------|
| 1 | `ceo_chat.py:1195-1199` | `await` on sync function + 잘못된 Pydantic 필드 | 동기 호출 + 올바른 필드(task_id, description 등) |
| 2 | `ceo_chat_tools.py:267` | SQL injection: UNION 미차단 | UNION, INTO OUTFILE, LOAD_FILE 추가 |
| 3 | `mcp/config.py:30` | 존재하지 않는 postgres MCP를 ALWAYS_ON에 포함 | 제거 |

### HIGH (7건)
| # | 파일 | 이슈 | 수정 |
|---|------|------|------|
| 4 | `ceo_chat.py` (6개소) | DB 연결 누수 (try without finally) | try/finally로 conn.close() 보장 |
| 5 | `ceo_chat.py:605,713` | 헬스체크가 외부 URL 호출 | localhost:8080으로 변경 |
| 6 | `code_explorer_service.py:25` | NTV2 workdir 불일치 | `/srv/newtalk-v2`로 통일 |
| 7 | 7개 파일 | stdlib logger에 structlog kwargs → 크래시 | structlog.get_logger()로 전환 |
| 8 | `Sidebar.tsx:15,202` | session_id vs id 필드 불일치 | `s.session_id ?? s.id` 호환 |
| 9 | `chatApi.ts:55-57` | API 응답 필드명 불일치 | tokens_in/tokens_out/cost 호환 추가 |
| 10 | `chat.py:113, chat_service.py:237` | 메시지 limit=50 → 129개 세션에서 최신 대화 안 보임 | 기본 200, 최대 1000, 프론트 500 |

### MEDIUM (3건)
| # | 파일 | 이슈 | 수정 |
|---|------|------|------|
| 11 | `page.tsx:578` | SSE 타임아웃 30초 (Extended Thinking 시 부족) | 90초로 증가 |
| 12 | `page.tsx:695` | 폴링 fallback이 가장 오래된 응답 선택 | reverse().find()로 최신 선택 |
| 13 | `supervisord.conf:60` | Git MCP root가 빈 디렉토리 | 실제 git repo 경로로 변경 |

### DB (3건)
| # | 이슈 | 수정 |
|---|------|------|
| 14 | research_archive FK: ON DELETE NO ACTION → 삭제 시 오류 | CASCADE로 변경 |
| 15 | stale data: cross_msg 308건, 빈 세션 1건 | 삭제 |
| 16 | 6개 소형 테이블 dead tuple 누적 | VACUUM ANALYZE |

### 기능 추가 (1건)
| # | 파일 | 내용 |
|---|------|------|
| 17 | `chat_service.py`, `chat.py` | 백그라운드 스트리밍 완료: 탭 닫아도 LLM 응답 DB 저장 보장 |

## 3. 인프라 조치

### 소스 읽기 복구
- `docker-compose.yml`에 볼륨 마운트 추가:
  - `/root/aads/aads-server:/root/aads/aads-server:ro`
  - `/root/aads/aads-dashboard:/root/aads/aads-dashboard:ro`

### 디스크 정리 (99% → 40%)
| 항목 | 정리량 |
|------|--------|
| Docker 빌드 캐시 | ~84GB |
| 미사용 Docker 이미지 | 8.5GB |
| 로그 파일 | 0.86GB |
| stale 클론/캐시 | 0.63GB |

### 재발방지
- `/root/scripts/docker_disk_cleanup.sh`: 매주 일 04:00 Docker 정리
- cron: PG 백업 7일 초과분 자동 삭제
- `/etc/logrotate.d/syslog`: rotate 2, compress, maxsize 200M
- `/etc/docker/daemon.json`: 컨테이너 로그 20MB x 3파일 제한

### 한국시간 기본값
- `page.tsx`, `Sidebar.tsx`: `timeZone: "Asia/Seoul"` 명시

## 4. 수정 파일 목록

### aads-server (14파일)
- `app/api/ceo_chat.py`
- `app/api/ceo_chat_tools.py`
- `app/api/directives.py`
- `app/core/langfuse_config.py`
- `app/core/mcp_server.py`
- `app/mcp/config.py`
- `app/routers/chat.py`
- `app/services/alert_manager.py`
- `app/services/chat_service.py`
- `app/services/code_explorer_service.py`
- `app/services/model_router.py`
- `app/services/telegram_bot.py`
- `docker-compose.yml`
- `supervisord.conf`

### aads-dashboard (4파일)
- `src/app/chat/page.tsx`
- `src/components/chat/Sidebar.tsx`
- `src/services/chatApi.ts`
- `src/hooks/useChatSSE.ts`
