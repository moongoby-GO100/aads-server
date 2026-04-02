# AADS Chat Backend 명세

_v1.0 | 2026-04-02 | 최초 작성_

## 1. 파일 구조

```
app/
├── routers/
│   └── chat.py                     — Chat V2 Router (30+ endpoints, 현재 활성)
├── api/
│   ├── chat.py                     — Legacy Router (4 endpoints, 키워드 기반)
│   ├── ceo_chat.py                 — Legacy CEO Chat (6 endpoints, 비활성)
│   ├── ceo_chat_tools.py           — 도구 정의 (35+ MCP tools)
│   └── ceo_chat_tools_scheduler.py — 스케줄러 도구
├── services/
│   ├── chat_service.py             — 비즈니스 로직 (4,158줄, 핵심)
│   ├── chat_embedding_service.py   — 벡터 임베딩 서비스
│   ├── chat_tools.py               — 도구 실행 엔진
│   └── redis_stream.py             — Redis Stream 토큰 버퍼 (190줄)
├── core/
│   ├── anthropic_client.py         — LLM 클라이언트 (fallback 체인)
│   └── db_pool.py                  — DB 커넥션 풀
└── main.py                         — 라우터 등록 (L1044, L1065)
```

## 2. 라우터 등록

```python
# app/main.py
app.include_router(chat.router, prefix="/api/v1", tags=["chat"])        # L1044 — Legacy
app.include_router(chat_v2_router, prefix="/api/v1", tags=["chat-v2"])  # L1065 — 현재 활성
# ceo_chat_router 비활성 — /chat V2로 통합 완료
```

## 3. API 엔드포인트 (Chat V2 — app/routers/chat.py)

### 3.1 Workspace 관리

| Method | Path | 기능 |
|--------|------|------|
| GET | `/chat/workspaces` | 워크스페이스 목록 |
| POST | `/chat/workspaces` | 워크스페이스 생성 |
| PUT | `/chat/workspaces/{id}` | 워크스페이스 수정 |
| DELETE | `/chat/workspaces/{id}` | 워크스페이스 삭제 |

### 3.2 Session 관리

| Method | Path | 기능 |
|--------|------|------|
| GET | `/chat/sessions` | 세션 목록 (workspace_id 필수) |
| GET | `/chat/sessions/{id}` | 세션 상세 |
| POST | `/chat/sessions` | 세션 생성 (자동 제목) |
| PUT | `/chat/sessions/{id}` | 세션 수정 (제목/핀/태그) |
| DELETE | `/chat/sessions/{id}` | 세션 삭제 |
| GET | `/chat/sessions/{id}/export` | 세션 내보내기 |

### 3.3 Message 관리

| Method | Path | 기능 |
|--------|------|------|
| GET | `/chat/messages` | 메시지 목록 (cursor 기반) |
| POST | `/chat/messages/send` | **메시지 전송 + SSE 스트리밍** |
| PUT | `/chat/messages/{id}` | 메시지 수정 |
| PUT | `/chat/messages/{id}/bookmark` | 북마크 토글 |
| DELETE | `/chat/messages/{id}` | 메시지 + 응답 삭제 |
| GET | `/chat/messages/search` | 메시지 검색 (FTS) |
| POST | `/chat/messages/{id}/regenerate` | 응답 재생성 |
| POST | `/chat/messages/{id}/branch` | 대화 분기 |
| GET | `/chat/sessions/{id}/branches` | 분기 목록 |

### 3.4 Streaming 제어

| Method | Path | 기능 |
|--------|------|------|
| GET | `/chat/sessions/{id}/streaming-status` | 스트리밍 상태 조회 |
| GET | `/chat/sessions/{id}/stream-resume` | **SSE 재연결** (Redis Stream 이어읽기) |
| GET | `/chat/sessions/{id}/last-response` | 최종 완성 응답 조회 |
| POST | `/chat/sessions/{id}/stop` | 스트리밍 중지 |
| POST | `/chat/sessions/{id}/interrupt` | 인터럽트 (새 지시) |
| POST | `/chat/sessions/{id}/resume` | 중단된 스트리밍 재개 |

### 3.5 Artifact 관리

| Method | Path | 기능 |
|--------|------|------|
| GET | `/chat/artifacts` | 아티팩트 목록 |
| GET | `/chat/artifacts/{id}` | 아티팩트 상세 |
| PUT | `/chat/artifacts/{id}` | 아티팩트 수정 |
| DELETE | `/chat/artifacts/{id}` | 아티팩트 삭제 |
| POST | `/chat/artifacts/{id}/export` | 내보내기 (PDF/MD/JSON) |

### 3.6 파일 관리

| Method | Path | 기능 |
|--------|------|------|
| POST | `/chat/files/upload` | 파일 업로드 |
| GET | `/chat/files/{id}` | 파일 다운로드 |
| GET | `/chat/files/{id}/thumbnail` | 썸네일 |
| GET | `/chat/drive` | 드라이브 파일 목록 |
| POST | `/chat/drive/upload` | 드라이브 업로드 |
| DELETE | `/chat/drive/{id}` | 드라이브 파일 삭제 |
| GET | `/chat/drive/{id}/download` | 드라이브 다운로드 |

### 3.7 기타

| Method | Path | 기능 |
|--------|------|------|
| GET | `/chat/research` | 리서치 결과 조회 |
| GET | `/chat/research/history` | 리서치 이력 |
| GET | `/chat/sessions/{id}/memory-context` | 메모리 컨텍스트 |
| POST | `/chat/errors/report` | 프론트엔드 에러 보고 |
| POST | `/chat/approve-diff` | 코드 diff 승인 |
| GET/POST | `/chat/templates` | 프롬프트 템플릿 |
| GET/POST | `/settings/auth-keys` | API 키 관리 |

## 4. chat_service.py 핵심 함수 (4,158줄)

### 4.1 스트리밍 인프라

| 함수 | 줄 | 기능 |
|------|-----|------|
| `with_heartbeat()` | 38 | SSE heartbeat 래퍼 (3s 간격, 256byte CF 패딩) |
| `with_background_completion()` | 227 | Queue 기반 백그라운드 완료 (클라이언트 끊겨도 생성 계속) |
| `_interim_save_streaming()` | 88 | 스트리밍 중 중간 상태 DB 저장 |
| `_delete_streaming_placeholder()` | 129 | placeholder 메시지 정리 |
| `stop_session_streaming()` | 474 | 스트리밍 강제 중지 |
| `resume_interrupted_streams()` | 544 | 서버 재시작 시 중단된 스트리밍 복구 |
| `_resume_single_stream()` | 623 | 개별 세션 스트리밍 재개 (LLM 재호출) |
| `get_streaming_status()` | 803 | 세션별 스트리밍 상태 조회 |

### 4.2 CRUD

| 함수 | 줄 | 기능 |
|------|-----|------|
| `list_workspaces()` | 893 | 워크스페이스 목록 |
| `create_workspace()` | 901 | 워크스페이스 생성 |
| `list_sessions()` | 966 | 세션 목록 |
| `create_session()` | 984 | 세션 생성 (자동 제목) |
| `list_messages()` | 1088 | 메시지 목록 (offset) |
| `list_messages_cursor()` | 1191 | 메시지 목록 (cursor 기반) |
| `_save_message()` | 1495 | 메시지 DB 저장 |
| `_save_and_update_session()` | 1580 | 메시지 저장 + 세션 업데이트 |

### 4.3 메시지 처리

| 함수 | 줄 | 기능 |
|------|-----|------|
| `send_message_stream()` | 2028 | **핵심** — 메시지 수신 → 인텐트 분류 → LLM 호출 → SSE 생성 |
| `process_files_for_claude()` | 1942 | 파일 → Claude 멀티모달 변환 |
| `_analyze_images_with_gemini()` | 1885 | 이미지 Gemini 분석 |
| `_analyze_videos_with_gemini()` | 1824 | 비디오 Gemini 분석 |
| `trigger_ai_reaction()` | 1712 | AI 자동 반응 생성 |
| `_extract_artifacts()` | 1321 | 응답에서 아티팩트 자동 추출 |

### 4.4 학습/메모리

| 함수 | 줄 | 기능 |
|------|-----|------|
| `_detect_and_save_learning()` | 3283 | 대화 중 교훈 자동 추출 |
| `_auto_save_session_note()` | 3334 | 세션 노트 자동 저장 |
| `_auto_observe_session()` | 3345 | 세션 관찰 자동 기록 |
| `_auto_extract_mid_conversation_lessons()` | 3356 | 대화 중간 교훈 추출 |

## 5. redis_stream.py (190줄)

| 함수 | 기능 |
|------|------|
| `publish_token()` | 토큰을 Redis Stream에 XADD (세션당 max 5000) |
| `mark_stream_done()` | 완료 마커 추가 + TTL 10분 |
| `read_tokens_after()` | XRANGE로 last_id 이후 토큰 읽기 |
| `xread_blocking()` | XREAD block으로 실시간 대기 |
| `get_stream_info()` | Stream 상태 조회 (length, is_done) |
| `delete_stream()` | Stream 삭제 |
| `health_check()` | Redis 연결 체크 |

설정: `_STREAM_PREFIX = "chat:stream:"`, `_STREAM_TTL = 3600s`, 완료 후 `600s`

## 6. LLM 라우팅

```
인텐트 분류 → 모델 선택:
  - XS (status_check, greeting) → haiku
  - S/M (analysis, code) → sonnet  
  - L/XL (research, complex) → opus
  - 사용자 지정 모델 → 지정값 우선

Fallback 체인:
  ANTHROPIC_AUTH_TOKEN → ANTHROPIC_API_KEY_FALLBACK → Gemini (LiteLLM)
```

## 버전 이력

| 버전 | 날짜 | 변경 |
|------|------|------|
| v1.0 | 2026-04-02 | 초기 작성 — 30+ API, 서비스 함수, Redis Stream |
