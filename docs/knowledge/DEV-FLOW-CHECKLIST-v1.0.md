# AADS 무결점·무중단 개발 흐름 체크리스트 v1.0
_생성: AADS-191 | 2026-04-01_

> 범례: ✅ 구현 완료 / ❌ 미구현 / ⚠️ 부분 구현

---

## 경로 A: 채팅 직접 수정 체크리스트

### 동시성 보호
- ✅ 파일별 `asyncio.Lock` — 동일 파일 동시 write/patch 순차 처리 (`_file_locks`)
- ✅ 메타 잠금(`_file_locks_meta_lock`) — 딕셔너리 동시 생성 경쟁 방지
- ✅ 30초 잠금 타임아웃 (`_FILE_LOCK_TIMEOUT`) — 무한 대기 방지
- ✅ `finally` 블록 잠금 해제 — 에러 시에도 잠금 반드시 해제
- ✅ write/patch 동일 키 공유 — 교차 충돌도 방지

### 파일 안전 보호
- ✅ 쓰기 전 `.bak_aads` 자동 백업 (`backup=True` 기본값)
- ✅ 민감 파일 차단 (`.env`, `.ssh`, `credentials` 등)
- ✅ 최대 파일 크기 제한 (1MB)
- ✅ 허용 프로젝트 화이트리스트 (AADS/KIS/GO100/SF/NTV2)

### 자동 후처리 (_post_file_modify_hook)
- ✅ `.py` 파일 hot reload 트리거 (AADS만)
- ✅ git add → commit → push (AADS만)
- ✅ push 실패 시 master 브랜치 fallback
- ✅ CHANGELOG 자동 기록 (`docs/CHANGELOG-direct-edit.md`)
- ✅ AI 코드 리뷰 자동 실행 (commit 완료 후)

### AI 코드 리뷰 (경로 A)
- ✅ git diff HEAD~1 HEAD로 실제 변경분 추출
- ✅ diff 크기 제한 10KB (초과 시 자동 절단)
- ✅ 5항목 가중 평균 점수 산출 (correctness/security/scope/preservation/quality)
- ✅ 판정 3단계: APPROVE / REQUEST_CHANGES / FLAG
- ✅ APPROVE 시 채팅 노이즈 없음 (로그만)
- ✅ REQUEST_CHANGES / FLAG 시 채팅 세션에 경고 메시지 INSERT
- ✅ DB `code_reviews` 테이블 저장
- ✅ 리뷰 실패 시 서비스 영향 없음 (try/except 격리)
- ⚠️ 리뷰 모델: `claude-haiku-4-5-20251001` (코드에 `_REVIEW_MODEL = "gemini-3.1-pro-preview"` 명시되어 있으나 실제 호출은 `call_llm_with_fallback`의 haiku 사용)

---

## 경로 B: Pipeline Runner 체크리스트

### 시작 조건
- ✅ 프로젝트별 `asyncio.Lock` — 동일 프로젝트 중복 실행 방지 (`_project_locks`)
- ✅ `chat_session_id` UUID 형식 검증 — 유효하지 않으면 채팅 보고 비활성
- ✅ `max_cycles` 상한 5 강제 (`min(max_cycles, 5)`)
- ✅ 검증 체크리스트 자동 삽입 (`_append_verification_checklist`)
- ✅ 채팅방에 시작 알림 메시지 INSERT

### Phase 1: Claude Code 작업
- ✅ SSH로 claude CLI 원격 실행
- ✅ 60분 타임아웃 (`_CLAUDE_TIMEOUT = 3600`)
- ✅ 타임아웃 시 에러 상태 + 채팅방 알림
- ✅ 에러 시 `_trigger_ai_reaction`으로 채팅 AI에게 원인 분석 요청

### Phase 2~3: AI 검수 루프
- ✅ 검수 모델: `claude-sonnet-4-6`
- ✅ git diff 기반 검수 (최대 50KB, `_MAX_DIFF_CHARS`)
- ✅ 판정 3종: PASS / FAIL / DELEGATED
- ✅ FAIL 시 재지시 + 루프 반복 (최대 `max_cycles`회)
- ✅ DELEGATED 시 채팅 AI에게 검수 위임 + `awaiting_approval` 전환
- ✅ 최대 횟수 도달 시에도 중단하지 않고 승인 요청으로 전환
- ✅ JSON 5단계 폴백 파싱 (코드펜스 제거 → 직접 파싱 → regex → 줄 단위 → 키워드 기반)
- ✅ 각 단계 채팅방 실시간 보고

### Phase 4: CEO 승인 대기
- ✅ `awaiting_approval` 상태 DB 저장
- ✅ 채팅방에 변경사항 diff 요약 + 승인 요청 메시지
- ✅ 채팅 AI 자동 반응 — CEO에게 변경사항 요약 제공
- ✅ "승인해" / "approve" 입력으로 Phase 5 진행
- ✅ "거부해" / "reject" 입력으로 취소 가능

### Phase 5: git push + 서비스 재시작
- ✅ `git push` 실행
- ✅ 프로젝트별 `_RESTART_CMD` 자동 선택
- ✅ AADS: `deploy.sh bluegreen` Blue-Green 무중단 배포
- ✅ KIS: `supervisorctl restart webapp`
- ✅ GO100: `supervisorctl restart go100`
- ✅ SF: `docker compose restart worker`
- ✅ NTV2: 재시작 없음 (PHP 즉시 반영)
- ✅ AADS 자기수정 안전장치: 재시작 전 DB 상태 선저장
- ✅ AADS 재시작 디바운스 30초 (`_AADS_RESTART_DEBOUNCE`)
- ✅ AADS health polling: 2초 × 15회 (최대 30초)

### Phase 6~7: 최종 검증 + 완료
- ✅ health check (`localhost:8080/health`)
- ✅ error_log 조회
- ✅ last commit 확인
- ✅ 완료 보고 (채팅방 + DB)
- ✅ QA 자동 보고 (`auto_report_on_completion`)
- ✅ aads-dashboard 변경 시 프론트엔드 QA 자동 실행 (`_run_frontend_qa_if_needed`)

---

## pre-commit hook 체크리스트

- ✅ 단계 1: API 키 패턴 탐지 (9개 패턴)
  - AIzaSy..., sk-ant-api..., sk-proj-..., gsk_..., sk-[hex]..., xoxb-..., ghp_..., AKIA..., API_KEY=...
- ✅ `.env*` 파일은 검사 제외 (시크릿 파일이므로)
- ✅ 단계 2: Python `ast.parse` 구문 검사
- ✅ 단계 3: `ruff` 정적 분석 (ruff 설치된 경우)
- ✅ 단계 4: 인증 핵심 파일 보호 (`ALLOW_AUTH_COMMIT=1` 없으면 차단)
  - `app/core/anthropic_client.py`, `app/core/auth_provider.py`, `app/services/model_selector.py`
  - `app/llm/client.py`, `docker-compose.yml`, `scripts/claude_relay_server.py`
- ✅ 단계 5: 채팅 관련 파일 수정 시 LLM smoke test 실행
- ✅ `--no-verify` 절대 금지 (CLAUDE.md R-COMMIT 규칙)
- ✅ hook 통과 감사 서명 (`.git/HOOK_VERIFIED` 기록)

---

## Blue-Green 배포 체크리스트

- ✅ Blue(8100) 상시 활성 / Green(8102) 배포 시 임시 스테이징
- ✅ Phase 0: 사전 검증 (postgres/redis/socket-proxy/litellm 상태 확인)
- ✅ Phase 0: Python 구문 + import 검증 (`python3 -m py_compile`, `import app.main`)
- ✅ Phase 0: 검증 실패 시 배포 즉시 차단 + 텔레그램 알림
- ✅ Phase 0.5: 배포 락 (`/tmp/aads-deploy.lock`)
- ✅ 볼륨 공유: Blue/Green이 `app` named volume 공유
- ✅ Swing-back: 배포 완료 후 항상 Blue로 복귀 + Green 제거
- ✅ `restart: always` (Blue) vs `restart: "no"` (Green) — 서버 재부팅 시 Blue만 자동 기동
- ✅ 상세 명세: `docs/BLUEGREEN_DEPLOY_SPEC.md`

---

## 복구 체계 체크리스트

- ✅ 재시작 중단 작업 자동 복구 (`recover_interrupted_jobs`)
  - Phase 0: 고아 `pipeline_jobs` 정리 (running/queued → error)
  - Phase 0.5: detached 작업 `.done` 파일 확인 후 결과 수거
  - Phase 0a: 서버 재시작으로 중단된 작업 자동 재실행
  - Phase 0b: 고아 `directive_lifecycle` 정리 (24시간 이상 in_progress → failed)
  - Phase 1: restarting 작업 복구
- ✅ Watchdog: 120초 간격 작업 감시 + 스톨 감지 + 채팅방 알림 (`start_watchdog`)
- ✅ per-job 승인 잠금 (`_job_approve_locks`) — 동시 approve/reject 경쟁 방지

---

## 버전 이력

| 버전 | 날짜 | 내용 |
|------|------|------|
| v1.0 | 2026-04-01 | 최초 작성. 코드 실측 기반 (AADS-191) |
