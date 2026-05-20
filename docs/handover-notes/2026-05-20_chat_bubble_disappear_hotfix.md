## 2026-05-20 11:35 KST — 채팅 응답 버블 사라짐 핫픽스 (세션 ac5278a7)

> 컨테이너 내부 `/app/HANDOVER.md`는 overlay 계층이라 호스트에 반영되지 않음.
> 호스트에서 `cat docs/handover-notes/2026-05-20_chat_bubble_disappear_hotfix.md >> HANDOVER.md`로 동기화 또는 수동 머지 필요.

### 배경
CEO가 `https://aads.newtalk.kr/chat#ac5278a7-2f13-4cd7-9aa1-83d41fb23c97`에서 "응답을 못 마치고 진행하다 응답 버블이 사라진다"고 보고했다.

### 실측
`/var/log/aads-api.log.1` 동일 세션에서 다음 이벤트 직접 관찰:
- `bg_auto_cancel: session=ac5278a7 client gone for 1806~1814s` (2회, 2026-05-19 23:37 / 2026-05-20 01:54)
- `list_messages_promote_skipped: real response exists, placeholder deleted session=ac5278a7` (2026-05-20 02:09)
- `list_messages_auto_promoted session=ac5278a7 count=1`
- `bg_producer_error … CancelledError` 다수 — `app/services/model_selector.py:2536 _stream_cli_relay_once`의 `resp.aiter_lines()`에서 상류 SSE 단절

### 근본 원인 (3중 결합)
1. **상류 SSE 단절**: claude_relay_server ↔ aads-server 간 httpcore CancelledError. `chat_service.py:2200`의 `_is_retryable` 판정이 `CancelledError`를 제외하므로 자동 재시도 없음.
2. **30분 idle 자동 취소**: `with_background_completion`의 `_BG_AUTO_CANCEL_SEC*3 = 1800s` 초과 시 producer 종료 → 세션 비활성 전환.
3. **placeholder 자동 청소가 버블을 지움**:
   - `_promote_inactive_streaming_placeholders` (line 3878-3970): 빈/짧은(<10자) placeholder를 결과 목록에서 제외, 부분 placeholder는 `intent='interrupted_partial'`로 UPDATE.
   - `_delete_streaming_placeholder` (line 1948-1998): 최종 응답 없는 빈 placeholder를 DELETE.
   - 두 경로 모두 `_AUTO_MESSAGE_EXCLUDE_FILTER`(line 3814)가 `interrupted_partial`을 가리므로 후속 폴링에서 메시지가 사라짐.

### 조치 (app/services/chat_service.py — 호스트 bind-mount, 이미 디스크 반영)
- `_promote_inactive_streaming_placeholders`:
  - 빈/짧은 placeholder를 `_empty_ids`로 분리, 결과에서 빼지 않고 `intent=NULL, model_used='interrupted', content="⚠️ 응답이 중단되었습니다. 다시 시도해 주세요."`로 UPDATE 보존.
  - 부분 보존 분기의 `intent='interrupted_partial'` → `intent=NULL`로 변경.
- `_delete_streaming_placeholder`:
  - "최종 응답 없음 + 내용 없음" 분기: DELETE → 안내 메시지 UPDATE로 변경.
  - "최종 응답 없음 + 내용 있음" 분기: `intent='interrupted_partial'` → `intent=NULL`.

### 검증
- `python3 -m pytest tests/unit/test_chat_service.py tests/unit/test_tools_and_pipeline.py` → **77 passed, 1 warning**.
- `python3 -m py_compile app/services/chat_service.py` 통과.
- `ruff check`: 변경 영역 신규 위반 없음 (기존 15건은 모두 다른 라인).
- `bash /app/scripts/reload-api.sh` → 67 modules reloaded, 0ms 다운타임.
- `curl https://aads.newtalk.kr/api/v1/ops/health-check` → `pipeline_healthy=true`.

### 남은 작업 (별건 P1/P2)
1. **호스트 커밋 필요** (컨테이너 내부에선 git 접근 불가):
   ```bash
   cd /root/aads/aads-server
   git status
   git diff app/services/chat_service.py
   # HANDOVER.md 동기화 (이 노트 머지)
   git add app/services/chat_service.py HANDOVER.md
   git commit  # pre-commit hook 5단계 통과 확인
   git push
   ```
2. **상류 SSE 단절 대응 (P1)**: `model_selector._stream_cli_relay_once`에 read timeout / 재연결 로직 추가, `chat_service._is_retryable`에 `CancelledError + 네트워크 단절 패턴`을 retryable로 추가.
3. **프론트엔드 폴링 정리 (P2)**: 1~3초마다 `streaming-status` + `messages` + `todos` 3중 폴링 → 통합 엔드포인트 또는 간격 5초로 완화. `aads-dashboard/src/app/chat/page.tsx` 폴링 hook 검토.

### 영향 범위
- 변경된 함수는 **비활성 세션의 placeholder 정리 경로**에서만 동작. 정상 스트림 완료(`_save_and_update_session` → `_delete_streaming_placeholder` "최종 응답 있음" 분기)는 기존대로 DELETE 유지.
- DB 메시지 수가 증가할 수 있음(중단된 응답마다 1건 "⚠️ 응답이 중단되었습니다" 보존) — 사용자에게 명시적 실패 안내 제공이 더 중요.
