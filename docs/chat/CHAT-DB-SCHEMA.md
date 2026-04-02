# AADS Chat DB 스키마

_v1.0 | 2026-04-02 | 최초 작성_

## 1. ER 다이어그램

```
┌─────────────────┐
│ chat_workspaces  │
│─────────────────│
│ id (PK, UUID)   │──┐
│ name             │  │
│ system_prompt    │  │ 1:N
│ files (JSONB)    │  │
│ settings (JSONB) │  │
│ color, icon      │  │
│ created_at       │  │
│ updated_at       │  │
└─────────────────┘  │
                      │
     ┌────────────────┘
     │
     ▼
┌─────────────────┐     ┌──────────────────┐
│ chat_sessions    │     │ chat_drive_files  │
│─────────────────│     │──────────────────│
│ id (PK, UUID)   │──┐  │ id (PK, UUID)    │
│ workspace_id(FK)│  │  │ workspace_id(FK) │
│ title            │  │  │ filename          │
│ summary          │  │  │ file_path         │
│ message_count    │  │  │ file_type         │
│ cost_total       │  │  │ file_size         │
│ pinned           │  │  │ uploaded_by       │
│ tags (TEXT[])    │  │  │ metadata (JSONB)  │
│ created_at       │  │  │ created_at        │
│ updated_at       │  │  └──────────────────┘
└─────────────────┘  │
     │               │
     │ 1:N           │ 1:N
     ▼               ▼
┌─────────────────┐  ┌─────────────────┐  ┌──────────────────┐
│ chat_messages    │  │ chat_artifacts   │  │ chat_files        │
│─────────────────│  │─────────────────│  │──────────────────│
│ id (PK, UUID)   │  │ id (PK, UUID)   │  │ id (PK, UUID)    │
│ session_id (FK) │  │ session_id (FK) │  │ session_id (FK)  │
│ role             │  │ type             │  │ message_id        │
│ content          │  │ title            │  │ original_name     │
│ model_used       │  │ content          │  │ stored_name       │
│ intent           │  │ metadata (JSONB)│  │ mime_type          │
│ cost             │  │ workspace_id    │  │ file_size          │
│ tokens_in/out    │  │ created_at      │  │ category           │
│ bookmarked       │  │ updated_at      │  │ uploaded_by        │
│ attachments(JSONB)│ └─────────────────┘  │ storage_path       │
│ sources (JSONB)  │                       │ thumbnail_path     │
│ tools_called(JSONB)│                     │ width, height      │
│ reply_to_id (FK→self)│                   │ metadata (JSONB)   │
│ branch_id        │                       │ created_at         │
│ idempotency_key  │                       └──────────────────┘
│ embedding(vector)│
│ quality_score    │
│ quality_details  │
│ thinking_summary │
│ edited_at        │
│ is_compacted     │
│ created_at       │
└─────────────────┘
```

## 2. 테이블 상세

### 2.1 chat_workspaces

| 컬럼 | 타입 | 기본값 | 설명 |
|------|------|--------|------|
| id | UUID PK | gen_random_uuid() | 워크스페이스 ID |
| name | VARCHAR(100) | NOT NULL | 이름 |
| system_prompt | TEXT | — | 시스템 프롬프트 |
| files | JSONB | '[]' | 첨부 파일 목록 |
| settings | JSONB | '{}' | 설정 (모델, 온도 등) |
| color | VARCHAR(7) | '#6366F1' | 표시 색상 |
| icon | VARCHAR(10) | '💬' | 표시 아이콘 |
| created_at | TIMESTAMPTZ | now() | 생성일 |
| updated_at | TIMESTAMPTZ | now() | 수정일 |

**참조**: chat_sessions, chat_drive_files, memory_facts

### 2.2 chat_sessions

| 컬럼 | 타입 | 기본값 | 설명 |
|------|------|--------|------|
| id | UUID PK | gen_random_uuid() | 세션 ID |
| workspace_id | UUID FK | NOT NULL | 워크스페이스 |
| title | VARCHAR(200) | — | 자동 생성 제목 |
| summary | TEXT | — | 세션 요약 |
| message_count | INT | 0 | 메시지 수 |
| cost_total | NUMERIC(10,4) | 0 | 누적 비용 ($) |
| pinned | BOOLEAN | false | 고정 여부 |
| tags | TEXT[] | '{}' | 태그 배열 |
| created_at | TIMESTAMPTZ | now() | 생성일 |
| updated_at | TIMESTAMPTZ | now() | 수정일 |

**인덱스**: `idx_sessions_workspace` (workspace_id, updated_at DESC), `idx_session_tags` (GIN)

### 2.3 chat_messages

| 컬럼 | 타입 | 기본값 | 설명 |
|------|------|--------|------|
| id | UUID PK | gen_random_uuid() | 메시지 ID |
| session_id | UUID FK | NOT NULL | 세션 |
| role | VARCHAR(20) | NOT NULL | user / assistant / system |
| content | TEXT | NOT NULL | 메시지 내용 |
| model_used | VARCHAR(100) | — | 사용 모델명 |
| intent | VARCHAR(50) | — | 인텐트 (streaming_placeholder 포함) |
| cost | NUMERIC(10,6) | 0 | 비용 ($) |
| tokens_in | INT | 0 | 입력 토큰 |
| tokens_out | INT | 0 | 출력 토큰 |
| bookmarked | BOOLEAN | false | 북마크 |
| attachments | JSONB | '[]' | 첨부 파일 메타 |
| sources | JSONB | '[]' | 참조 소스 |
| tools_called | JSONB | '[]' | 도구 호출 기록 |
| reply_to_id | UUID FK→self | — | 답글 대상 |
| branch_id | UUID | — | 분기 ID |
| idempotency_key | VARCHAR(64) | — | 중복 방지 키 |
| embedding | VECTOR(768) | — | pgvector 임베딩 |
| quality_score | FLOAT | — | 품질 점수 (0~1) |
| quality_details | JSONB | — | 품질 상세 |
| thinking_summary | TEXT | — | 사고 요약 |
| edited_at | TIMESTAMPTZ | — | 수정일 |
| is_compacted | BOOLEAN | false | 압축 여부 |
| created_at | TIMESTAMPTZ | now() | 생성일 |

**핵심 인덱스**:
- `idx_messages_session` (session_id, created_at) — 메시지 조회 기본
- `idx_chat_msg_embedding` HNSW (embedding vector_cosine_ops) — 시맨틱 검색
- `idx_messages_fts` GIN (to_tsvector) — 전문 검색
- `idx_msg_idempotency` UNIQUE — 중복 메시지 방지
- `idx_one_placeholder_per_session` UNIQUE — 세션당 1개 placeholder 보장

**CHECK**: role ∈ {user, assistant, system}

### 2.4 chat_artifacts

| 컬럼 | 타입 | 기본값 | 설명 |
|------|------|--------|------|
| id | UUID PK | gen_random_uuid() | 아티팩트 ID |
| session_id | UUID FK | NOT NULL | 세션 |
| type | VARCHAR(20) | NOT NULL | report/code/chart/dashboard/table/image/file/text |
| title | VARCHAR(200) | — | 제목 |
| content | TEXT | NOT NULL | 내용 |
| metadata | JSONB | '{}' | 메타데이터 |
| workspace_id | UUID | — | 워크스페이스 |
| created_at | TIMESTAMPTZ | now() | 생성일 |
| updated_at | TIMESTAMPTZ | now() | 수정일 |

### 2.5 chat_files

| 컬럼 | 타입 | 기본값 | 설명 |
|------|------|--------|------|
| id | UUID PK | gen_random_uuid() | 파일 ID |
| session_id | UUID FK | NOT NULL | 세션 |
| message_id | UUID | — | 연결 메시지 |
| original_name | TEXT | NOT NULL | 원본 파일명 |
| stored_name | TEXT | NOT NULL | 저장 파일명 |
| mime_type | TEXT | NOT NULL | MIME 타입 |
| file_size | BIGINT | NOT NULL | 파일 크기 |
| category | TEXT | 'attachment' | 카테고리 |
| uploaded_by | TEXT | 'user' | 업로더 |
| storage_path | TEXT | NOT NULL | 저장 경로 |
| thumbnail_path | TEXT | — | 썸네일 경로 |
| width / height | INT | — | 이미지 크기 |
| metadata | JSONB | '{}' | 메타데이터 |
| created_at | TIMESTAMPTZ | now() | 생성일 |

### 2.6 chat_drive_files

워크스페이스별 영구 파일 저장소. `chat_files`와 유사하나 워크스페이스 단위.

| 컬럼 | 타입 | 기본값 | 설명 |
|------|------|--------|------|
| id | UUID PK | gen_random_uuid() | 파일 ID |
| workspace_id | UUID FK | NOT NULL | 워크스페이스 |
| filename | VARCHAR(255) | NOT NULL | 파일명 |
| file_path | VARCHAR(500) | NOT NULL | 저장 경로 |
| file_type | VARCHAR(50) | — | 파일 유형 |
| file_size | BIGINT | 0 | 크기 |
| uploaded_by | VARCHAR(20) | 'user' | 업로더 |
| metadata | JSONB | '{}' | 메타데이터 |
| created_at | TIMESTAMPTZ | now() | 생성일 |

## 3. 관련 테이블 (chat 외)

| 테이블 | 관계 | 용도 |
|--------|------|------|
| `memory_facts` | workspace_id, session_id FK | 대화에서 추출한 사실 |
| `session_notes` | session_id FK | 세션별 자동 노트 |
| `research_archive` | session_id FK | 심층 리서치 결과 |
| `tool_results_archive` | message_id FK | 도구 실행 결과 아카이브 |

## 버전 이력

| 버전 | 날짜 | 변경 |
|------|------|------|
| v1.0 | 2026-04-02 | 초기 작성 — 6테이블 + 인덱스 + ER 다이어그램 |
