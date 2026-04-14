# AADS 무결점·무중단 개발 흐름 v1.0
_생성: AADS-191 | 2026-04-01_

---

## 개요

AADS는 두 가지 독립적인 개발 경로를 운영한다. 소규모 즉시 수정에는 **경로 A(채팅 직접 수정)**, 대규모 변경 및 전체 배포에는 **경로 B(Pipeline Runner)**를 사용한다. 두 경로 모두 공통 품질 게이트(pre-commit hook, AI 코드 리뷰, health check)를 통과해야 최종 반영된다.

- 서버: 68.183.183.11 (서버 68)
- 백엔드: FastAPI 0.115, Python 3.11, Docker Compose
- 프론트엔드: Next.js 16
- DB: PostgreSQL 15 (aads-postgres:5432)

---

## 두 가지 개발 경로

### 경로 A: 채팅 직접 수정 (write_remote_file / patch_remote_file)

CEO 채팅에서 AI에게 파일 수정을 지시하면 즉시 반영되는 경로.
`tool_executor.py`의 `_write_remote_file` / `_patch_remote_file`이 실행 주체다.

**대상**: AADS 프로젝트의 `.py` 파일 및 일반 파일.
**제약**: `.env`, `.ssh`, `credentials` 등 민감 파일은 보안 차단.

### 경로 B: Pipeline Runner (대규모 변경 / 배포)

CEO 채팅에서 `pipeline_runner_submit`으로 시작하거나 자동 트리거(`auto_trigger.sh`)로 실행되는 경로.
`pipeline_c.py`의 `PipelineCJob`이 실행 주체다. Claude Code CLI를 SSH로 원격 실행하고, 검수·승인·배포까지 자동화한다.

**대상**: 모든 프로젝트(AADS, KIS, GO100, SF, NTV2).
**배포 방식**: 프로젝트별 `_RESTART_CMD` 참조.

---

## 전체 흐름도

```
[경로 A: 채팅 직접 수정]

CEO 채팅 지시
    │
    ▼
ToolExecutor._write_remote_file / _patch_remote_file
    │
    ├─ 1. 파일별 asyncio.Lock 획득 (30초 타임아웃)
    │       동일 파일 동시 수정 → 순차 처리
    │
    ├─ 2. ceo_chat_tools.tool_write_remote_file (SSH 파일 쓰기 + .bak_aads 백업)
    │   또는 tool_patch_remote_file (old_string → new_string 교체)
    │
    └─ 3. _post_file_modify_hook (AADS 프로젝트만)
            ├─ 3-1. hot_reload_trigger (.py 파일이면)
            ├─ 3-2. git add → commit ("Chat-Direct: {file_path} 수정") → push main
            ├─ 3-3. CHANGELOG 기록 (docs/CHANGELOG-direct-edit.md)
            └─ 3-4. AI 코드 리뷰 (_run_ai_code_review_after_commit)
                        ├─ git diff HEAD~1 HEAD -- {file}
                        ├─ code_reviewer.review_code_diff (AI_REVIEW 모델 우선순위)
                        │     precheck: invalid diff / runner 오류 텍스트 선분류
                        │     APPROVE (score ≥ 0.7) → 로그만
                        │     REQUEST_CHANGES (0.4~0.69) → 채팅방 경고 저장
                        │     FLAG (< 0.4) → 채팅방 긴급 경고 저장
                        └─ 잠금 해제 (finally)


[경로 B: Pipeline Runner]

CEO 채팅 지시 / auto_trigger.sh 감지
    │
    ▼
pipeline_c.start_pipeline → PipelineCJob.run()
    │
    ├─ 프로젝트별 asyncio.Lock 획득 (동일 프로젝트 중복 실행 방지)
    │
    ├─ Phase 1: Claude Code 작업 수행
    │       _run_claude_code (SSH로 claude CLI 실행, 타임아웃 60분)
    │       검증 체크리스트 자동 삽입 (_append_verification_checklist)
    │
    ├─ Phase 2~3: AI 검수 루프 (최대 3회, max_cycles=3)
    │       _ai_review (claude-sonnet-4-6, git diff + 출력 분석)
    │       │
    │       ├─ PASS → 루프 탈출 → Phase 4
    │       ├─ FAIL → Claude Code 재지시 → 루프 반복
    │       └─ DELEGATED (LLM 호출 실패) → 채팅 AI에게 검수 위임 → awaiting_approval
    │
    ├─ Phase 4: CEO 승인 대기 (awaiting_approval)
    │       채팅방에 변경사항 요약 + 승인 요청 메시지
    │       CEO: "승인해" / "approve" 입력
    │
    └─ Phase 5~7: 배포 + 검증 (approve_pipeline 호출)
            ├─ Phase 5: git push
            ├─ 서비스 재시작 (_RESTART_CMD)
            │       AADS:  deploy.sh bluegreen (Blue-Green 무중단)
            │       KIS:   supervisorctl restart webapp
            │       GO100: supervisorctl restart go100
            │       SF:    docker compose restart worker
            │       NTV2:  (PHP, 파일 수정 즉시 반영 — 재시작 없음)
            ├─ Phase 6: 최종 검증 (_final_verify)
            │       health check (localhost:8080/health)
            │       error_log 조회
            │       last commit 확인
            └─ Phase 7: 완료 보고 + QA 자동 보고
                    aads-dashboard 변경 포함 시 → _run_frontend_qa_if_needed 자동 실행
```

---

## 단계별 상세 설명

### A-1. 파일별 asyncio.Lock

- 구현: `_file_locks: dict[str, asyncio.Lock]` (모듈 레벨 딕셔너리)
- 키: `"{project}:{file_path}"` (write와 patch가 동일 키 공유 → 교차 충돌도 방지)
- 타임아웃: 30초(`_FILE_LOCK_TIMEOUT`). 초과 시 즉시 에러 반환, 서비스 영향 없음.
- 잠금 해제: `finally` 블록에서 무조건 실행.

### A-2. 파일 백업

- `tool_write_remote_file`은 쓰기 전 자동으로 `.bak_aads` 백업 생성.
- `backup=False`로 비활성화 가능.

### A-3. AI 코드 리뷰 (경로 A)

- 실행 시점: commit + push 완료 후 (실패해도 서비스에 영향 없음)
- 리뷰 모델: `runner_model_config.size='AI_REVIEW'` 우선순위 사용, 미설정 시 `qwen-turbo` 폴백
- diff 크기 제한: 10KB (초과 시 자동 절단)
- 입력 사전검사: git diff 형식 검증, 러너 인증 실패/실행 오류/`git diff` 수집 실패를 `FLAG` 타입으로 선분류
- 평가 기준 5항목 + 가중 평균:
  - correctness 30%, security 25%, scope_compliance 20%, preservation 15%, quality 10%
- 판정:
  - APPROVE (≥ 0.7): 로그만 기록
  - REQUEST_CHANGES (0.4 ~ 0.69): 채팅 세션에 경고 메시지 INSERT
  - FLAG (< 0.4): 채팅 세션에 긴급 경고 메시지 INSERT
- FLAG 세분화: `RUNNER_AUTH_FAILURE`, `RUNNER_EXECUTION_FAILURE`, `GIT_DIFF_FAILURE`, `INVALID_REVIEW_INPUT`, `CODE_QUALITY`, `REVIEW_MODEL_NO_RESPONSE`, `REVIEW_SYSTEM_FAILURE`
- DB 저장: `code_reviews` 테이블 (job_id, project, verdict, score, feedback, diff_size, model_used, cost, flag_category, failure_stage, needs_retry)

### B-1. 검증 체크리스트 자동 삽입

- `_append_verification_checklist`로 지시서에 검증 항목 자동 추가.
- Claude Code가 작업 완료 시 체크리스트 항목도 처리해야 PASS 판정 가능.

### B-2. AI 검수 루프 (경로 B)

- 검수 모델: `claude-sonnet-4-6` (`_REVIEW_MODEL`)
- 판정: PASS / FAIL / DELEGATED (LLM 호출 실패 시)
- 최대 재지시 횟수: `max_cycles=3` (상한 5)
- DELEGATED: `_trigger_ai_reaction`으로 채팅 AI에게 검수 위임 후 `awaiting_approval` 상태 전환

### B-3. AADS 자기수정 안전장치

- AADS 프로젝트 배포 시 재시작 전 DB에 상태 선저장.
- 디바운스 (`_AADS_RESTART_DEBOUNCE=30초`): 연속 배포 시 마지막 1회만 재시작.
- 재시작 후 health polling: 2초 간격 × 최대 15회(30초) 확인.

### B-4. Blue-Green 무중단 배포

- 명령: `bash /root/aads/aads-server/deploy.sh bluegreen`
- 상세 명세: `docs/BLUEGREEN_DEPLOY_SPEC.md`
- Blue(8100): 상시 활성. Green(8102): 배포 중 임시 스테이징.
- 7단계 자동화: 사전 검증 → 배포 락 획득 → Green 기동 → nginx 전환 → 검증 → Swing-back(Blue 복귀) → Green 제거.
- 볼륨 공유: Blue/Green이 `app` named volume 공유 → code 모드 변경은 두 인스턴스에 즉시 반영.

---

## 품질 게이트

### 게이트 1: pre-commit hook (5단계)

모든 git commit 시 자동 실행. `--no-verify` 절대 금지.

| 단계 | 내용 | 차단 조건 |
|------|------|-----------|
| 1 | API 키 패턴 탐지 | AIzaSy..., sk-ant-api..., sk-proj-..., ghp_... 등 9개 패턴 감지 시 |
| 2 | Python 구문 검사 | `ast.parse` 실패 시 |
| 3 | ruff 정적 분석 | undefined name, unused import 등 |
| 4 | 인증 핵심 파일 보호 | `anthropic_client.py`, `auth_provider.py`, `model_selector.py`, `docker-compose.yml` 등 수정 시 (`ALLOW_AUTH_COMMIT=1` 없으면 차단) |
| 5 | LLM smoke test | 채팅 관련 파일 수정 시 `call_llm_with_fallback` 실제 호출 확인 |

- 감사 서명: hook 통과 시 `.git/HOOK_VERIFIED` 파일에 타임스탬프 기록.

### 게이트 2: AI 코드 리뷰

- 경로 A: commit 후 자동 실행 (비동기, 실패해도 배포 차단 안 함)
- 경로 B: Phase 2~3 검수 루프 (FAIL 시 재지시, PASS 필수)

### 게이트 3: CEO 승인 (경로 B 전용)

- Phase 4: `awaiting_approval` 상태에서 CEO "승인해" / "approve" 입력 필수.
- 채팅 AI가 변경사항 요약 + 승인 판단 근거 자동 제공.

### 게이트 4: 배포 후 health check

- `curl -sf http://localhost:8080/health`
- error_log 증가 추이 5분 모니터링 (watchdog)

---

## 컴포넌트 위치 참조

| 컴포넌트 | 파일 경로 |
|---------|-----------|
| 파일 수정 + 후처리 훅 | `app/services/tool_executor.py` (L371~625) |
| AI 코드 리뷰 서비스 | `app/services/code_reviewer.py` |
| AI 코드 리뷰 API | `app/api/code_review.py` |
| Pipeline Runner 핵심 | `app/services/pipeline_c.py` |
| pre-commit hook | `.git/hooks/pre-commit` |
| Blue-Green 배포 명세 | `docs/BLUEGREEN_DEPLOY_SPEC.md` |
| 자동 트리거 | `scripts/auto_trigger.sh` |

---

## 버전 이력

| 버전 | 날짜 | 내용 |
|------|------|------|
| v1.0 | 2026-04-01 | 최초 작성. 경로 A/B 실측 기반 전체 흐름 문서화 (AADS-191) |
