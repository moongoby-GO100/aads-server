# 채팅 파일 업로드 장애 수정 + 첨부파일 재읽기 도구 추가

**일시**: 2026-03-11
**작업자**: Claude Code (Opus 4.6)
**커밋**: aads-server aa64ebd, 291a8c6 / aads-dashboard 335f4a3

---

## 1. 파일 업로드 장애 (근본 원인 + 수정)

### 증상
- 채팅창에서 파일 업로드 후 "파일이 샌드박스에 도달하지 않았습니다" 응답
- DB에 `attachments=[]` 저장 (파일 내용 미연결)

### 근본 원인
`chat_drive_files.metadata` (JSONB 컬럼)이 `_JSONB_FIELDS` 화이트리스트에 누락.
→ `_row_to_dict()`가 문자열 `'{}'`을 dict로 파싱하지 않음
→ `DriveFileOut` Pydantic 모델 유효성 검증 실패
→ `/chat/drive/upload` API 500 ResponseValidationError 반환
→ 프론트엔드 `res.ok === false` → `pendingAttachments` 미등록
→ 메시지 전송 시 `attachments: []`

### 수정 내역
| 파일 | 수정 |
|------|------|
| `chat_service.py` | `_JSONB_FIELDS`에 `"metadata"` 추가 |
| `chat_service.py` | 첨부파일 디버그 로깅 `[ATTACH]` 추가 |
| `page.tsx` | 업로드 중 전송 차단 (`uploading` state) |
| `page.tsx` | 업로드 진행 표시기 + 첨부 파일 목록 표시 |

---

## 2. `read_uploaded_file` 도구 신설

### 목적
이전 대화에서 업로드한 파일을 AI가 다시 읽을 수 있도록 함.
(기존: 업로드 시 1회만 content에 합침 → compaction 후 소실)

### 구현
| 구성요소 | 파일 | 내용 |
|---------|------|------|
| 도구 정의 | `tool_registry.py` | `read_uploaded_file` 스키마 (filename, workspace_id, max_chars) |
| 도구 실행 | `tool_executor.py` | `chat_drive_files` DB 검색 → 디스크 파일 읽기 (100KB 제한) |
| 인텐트 | `intent_router.py` | `file_read` 인텐트 추가 (tools=True, group="all") |
| 키워드 오버라이드 | `chat_service.py` | "업로드한 파일/첨부파일/파일 읽어" 등 감지 시 file_read 강제 |
| defer_loading | `tool_registry.py` | `False` (상시 로드) |

### 동작 흐름
```
사용자: "이전에 업로드한 DESK-MANAGER 파일 다시 읽고 보고해"
→ 인텐트: file_read (키워드 오버라이드)
→ tool_use: read_uploaded_file({filename: ""}) → 파일 9건 목록
→ tool_use: read_uploaded_file({filename: "DESK‑MANAGER"}) → 54KB 전문
→ AI: 파일 내용 기반 보고서 생성
```

---

## 3. SSH 경로 특수문자 수정

### 증상
`read_remote_file` 도구가 한글/공백/비표준 하이픈(‑ U+2011) 포함 경로 거부

### 수정
```python
# Before
_SSH_PATH_WHITELIST = re.compile(r'^[a-zA-Z0-9._/\-]*$')

# After
_SSH_PATH_WHITELIST = re.compile(r'^[\w._/\- \u2010-\u2015\u00a0]+$', re.UNICODE)
```
- 유니코드(한글), 공백, 비표준 하이픈 허용
- `shlex.quote()` + 위험 문자 차단은 유지

---

## 4. 검증 결과

| 테스트 | 결과 |
|--------|------|
| 파일 업로드 API (`/chat/drive/upload`) | 200 OK ✅ (이전: 500) |
| 메시지 전송 시 attachments 전달 | `[{name, path}]` 정상 ✅ (이전: `[]`) |
| `os.path.isfile()` 파일 접근 | `True` ✅ |
| AI 파일 내용 읽기 | 정상 ✅ |
| `read_uploaded_file` 도구 호출 | 파일 목록 + 전문 읽기 정상 ✅ |
| SSH 한글 경로 | 정상 통과 ✅ |

## 5. 배포 상태

| 서비스 | 상태 |
|--------|------|
| aads-server | 재시작 완료 (볼륨마운트, 자동반영) |
| aads-dashboard | 재빌드+재배포 완료 (docker compose build --no-cache) |
