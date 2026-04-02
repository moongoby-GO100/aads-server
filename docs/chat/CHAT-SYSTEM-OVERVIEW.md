# AADS Chat System — 전체 아키텍처 개요

_v1.0 | 2026-04-02 | 최초 작성_

## 1. 시스템 개요

AADS CEO Chat은 CEO(moongoby)가 자연어로 6개 프로젝트를 통합 운영하는 **Chat-First 인터페이스**입니다.  
워크스페이스 기반으로 프로젝트별 컨텍스트를 분리하고, 35+ 도구를 실시간 호출하며, SSE 스트리밍으로 응답합니다.

## 2. 아키텍처 다이어그램

```
┌──────────────────────────────────────────────────────────┐
│  CEO 브라우저 (aads.newtalk.kr/chat)                       │
│  Next.js 16 Dashboard                                     │
│  ┌──────────┬──────────┬──────────┬──────────┐           │
│  │ChatSidebar│ page.tsx │ChatInput │Artifact  │           │
│  │(566L)    │(4501L)  │(406L)   │Panel(513L)│           │
│  └────┬─────┴────┬─────┴────┬─────┴──────────┘           │
│       │          │          │                              │
│       └──────────┼──────────┘                              │
│                  │ SSE (EventSource)                       │
└──────────────────┼─────────────────────────────────────────┘
                   │
         Cloudflare CDN (120s proxy read timeout)
                   │
         Nginx (600s timeout, proxy_buffering off)
                   │
┌──────────────────┼─────────────────────────────────────────┐
│  FastAPI 0.115 (서버68, 포트 8100)                         │
│                  │                                          │
│  ┌───────────────▼───────────────┐                         │
│  │ app/routers/chat.py           │  ← Chat V2 Router       │
│  │ 30+ REST/SSE endpoints        │                         │
│  └───────────────┬───────────────┘                         │
│                  │                                          │
│  ┌───────────────▼───────────────┐                         │
│  │ app/services/chat_service.py  │  ← 비즈니스 로직 (4158L) │
│  │ send_message_stream()         │                         │
│  │ with_background_completion()  │                         │
│  │ resume_interrupted_streams()  │                         │
│  └──────┬────────────┬───────────┘                         │
│         │            │                                      │
│  ┌──────▼──────┐ ┌──▼──────────────┐                      │
│  │ Redis Stream │ │ Anthropic/Gemini│                      │
│  │ (토큰 버퍼)  │ │ LLM API         │                      │
│  │ redis_stream │ │ anthropic_client│                      │
│  │ .py (190L)  │ │ .py             │                      │
│  └──────┬──────┘ └─────────────────┘                      │
│         │                                                   │
│  ┌──────▼──────────────────────────┐                      │
│  │ PostgreSQL 15 (pgvector)        │                      │
│  │ chat_workspaces / sessions /    │                      │
│  │ messages / artifacts / files    │                      │
│  └─────────────────────────────────┘                      │
└─────────────────────────────────────────────────────────────┘
```

## 3. 핵심 데이터 흐름

### 3.1 메시지 전송 흐름
```
CEO 입력 → ChatInput.tsx → POST /api/v1/chat/messages/send
  → chat_service.send_message_stream()
    → 인텐트 분류 (키워드 + LLM)
    → 도구 호출 (35+ MCP tools)
    → LLM 응답 생성 (Anthropic/Gemini)
    → SSE 토큰 스트리밍 (asyncio.Queue + Redis Stream 병행)
  → 프론트엔드 EventSource 수신 → 실시간 렌더링
```

### 3.2 끊김 복구 흐름 (6계층 방어)
```
SSE 끊김 감지
  → [1] stream-resume (5회, 120s) — Redis Stream에서 이어읽기
  → [2] last-response 폴링 (3회) — DB에서 완성 응답 조회
  → [3] waitingBg (30s) — 백그라운드 완료 대기
  → [4] 서버 재시작 → resume_interrupted_streams() — placeholder UPDATE
  → [5] heartbeat 3s + 256byte CF flush 패딩
  → [6] Nginx 600s + maxStreamTimeout 3600s
```

### 3.3 세션/워크스페이스 구조
```
Workspace (프로젝트별)
  └── Session (대화 단위)
       ├── Message (user/assistant/system)
       │    ├── Attachments (이미지/파일)
       │    ├── Tools Called (도구 호출 기록)
       │    └── Embedding (pgvector 768차원)
       ├── Artifact (보고서/코드/차트)
       └── Branch (대화 분기)
```

## 4. 기술 스택

| 계층 | 기술 | 버전 |
|------|------|------|
| Frontend | Next.js (App Router) | 16 |
| Backend | FastAPI | 0.115 |
| Database | PostgreSQL + pgvector | 15 |
| Cache/Stream | Redis (aioredis) | 7.x |
| LLM Primary | Anthropic Claude | Opus/Sonnet |
| LLM Fallback | Google Gemini (LiteLLM) | 2.5 |
| Infra | Docker Compose | — |
| CDN | Cloudflare | — |
| Reverse Proxy | Nginx | — |

## 5. 관련 문서

| 문서 | 경로 | 설명 |
|------|------|------|
| 프론트엔드 명세 | [CHAT-FRONTEND-SPEC.md](./CHAT-FRONTEND-SPEC.md) | 컴포넌트/상태/이벤트 |
| 백엔드 명세 | [CHAT-BACKEND-SPEC.md](./CHAT-BACKEND-SPEC.md) | API/서비스/도구 |
| 스트리밍 명세 | [CHAT-STREAMING-SPEC.md](./CHAT-STREAMING-SPEC.md) | SSE/복구/버퍼링 |
| DB 스키마 | [CHAT-DB-SCHEMA.md](./CHAT-DB-SCHEMA.md) | 테이블/인덱스/관계 |
| 변경 이력 | [CHAT-CHANGELOG.md](./CHAT-CHANGELOG.md) | 버전별 변경 기록 |
| SSE 아키텍처 | [../knowledge/SSE-STREAMING-ARCHITECTURE.md](../knowledge/SSE-STREAMING-ARCHITECTURE.md) | 6계층 방어 상세 |
| CEO-019 보고서 | [../reports/CEO-019-SSE-IMPROVEMENT-REPORT.md](../reports/CEO-019-SSE-IMPROVEMENT-REPORT.md) | SSE 개선 13건 |

## 버전 이력

| 버전 | 날짜 | 변경 |
|------|------|------|
| v1.0 | 2026-04-02 | 초기 작성 — 전체 아키텍처/데이터흐름/기술스택 |
