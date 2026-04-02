# AADS 무결점·무중단 개발 흐름 v1.1
_갱신: AADS-191 | 2026-04-02_

---

## 개요

AADS는 두 가지 독립적인 개발 경로를 운영한다. 소규모 즉시 수정에는 **경로 A(채팅 직접 수정)**, 대규모 변경 및 전체 배포에는 **경로 B(Pipeline Runner)**를 사용한다. 두 경로 모두 공통 품질 게이트(pre-commit hook, pre-push hook, AI 코드 리뷰, health check)를 통과해야 최종 반영된다.

- 서버: 68.183.183.11 (서버 68)
- 백엔드: FastAPI 0.115, Python 3.11, Docker Compose
- 프론트엔드: Next.js 16
- DB: PostgreSQL 15 (aads-postgres:5432)

---

## v1.1 변경사항 요약

| 항목 | 유형 | 내용 |
|------|------|------|
| P2 | 신규 | 배포 이력 DB 기록 (`deploy_history` 테이블) |
| P4 | 신규 | 동시작업 파일 덮어쓰기 방지 (`git_lock.py`, fcntl.flock) |
| I1 | 신규 | pre-push hook — `--no-verify` 우회 차단 |
| 감시 | 개선 | Cross-Monitor SSH 경유 health check, 90% 임계값 |
| 감시 | 개선 | Watchdog API unhealthy 시 1회 자동복구 시도 |
| 배포 | 신규 | Blue-Green 배포 스크립트 (`blue_green_deploy.sh`) |
| 검증 | 신규 | 배포 전 사전 검증 (`pre_deploy_validate.sh`) |

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
    ├─ 1. 프로젝트별 fcntl.flock 획득 (60초 타임아웃) ← v1.1 P4
    │       /tmp/aads-git-lock-{PROJECT} 파일 기반
    │       동일 프로젝트 동시 git 작업 → 순차 처리
    │
    ├─ 2. ceo_chat_tools.tool_write_remote_file (SSH 파일 쓰기 + .bak_aads 백업)
    │   또는 tool_patch_remote_file (old_string → new_string 교체)
    │
    └─ 3. _post_file_modify_hook (AADS 프로젝트만)
            ├─ 3-1. hot_reload_trigger (.py 파일이면)
            ├─ 3-2. git add → commit → push main
            ├─ 3-3. CHANGELOG 기록 (docs/CHANGELOG-direct-edit.md)
            └─ 3-4. AI 코드 리뷰 (_run_ai_code_review_after_commit)


[경로 B: Pipeline Runner]

CEO 채팅 지시 / auto_trigger.sh 감지
    │
    ▼
pipeline_c.start_pipeline → PipelineCJob.run()
    │
    ├─ 프로젝트별 asyncio.Lock 획득
    │
    ├─ Phase 1: Claude Code 작업 수행
    ├─ Phase 2~3: AI 검수 루프 (최대 3회)
    ├─ Phase 4: CEO 승인 대기
    └─ Phase 5~7: 배포 + 검증
            ├─ Phase 5: git push (pre-push hook 검증) ← v1.1 I1
            ├─ 배포: blue_green_deploy.sh → deploy_history DB 기록 ← v1.1 P2
            ├─ Phase 6: 최종 검증 (_final_verify)
            └─ Phase 7: 완료 보고
```

---

## v1.1 신규 컴포넌트 상세

### P2: 배포 이력 DB 기록

모든 배포(Blue-Green, hot-reload)를 `deploy_history` 테이블에 자동 기록.

**테이블 스키마** (`migrations/041_deploy_history.sql`):
```sql
CREATE TABLE IF NOT EXISTS deploy_history (
    id SERIAL PRIMARY KEY,
    deploy_type VARCHAR(30),      -- 'blue_green', 'code_only', 'hot_reload'
    project VARCHAR(20),          -- 'AADS', 'KIS', 'GO100', 'SF', 'NTV2'
    trigger_by VARCHAR(50),       -- 'pipeline_runner', 'chat_direct', 'manual'
    git_commit VARCHAR(50),
    git_message TEXT,
    status VARCHAR(20),           -- 'started', 'success', 'failed', 'rolled_back'
    duration_s INTEGER,
    error_msg TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);
```

**기록 위치**: `scripts/blue_green_deploy.sh`의 `db_record_start()` / `db_record_finish()` 함수.

### P4: 동시작업 파일 덮어쓰기 방지

경로 A에서 동일 프로젝트에 대한 동시 git 작업(add/commit/push)을 직렬화.

**구현**: `app/core/git_lock.py`
- `git_project_lock(project, timeout=60)` — async context manager
- `fcntl.flock()` 기반 OS 레벨 파일 잠금 (asyncio.Lock보다 프로세스 간 안전)
- Lock 파일: `/tmp/aads-git-lock-{PROJECT}`
- 0.5초 간격 논블로킹 폴링, 60초 타임아웃

**사용 위치**: `app/services/tool_executor.py` L403-415 (`_post_file_modify_hook`)

### I1: pre-push hook (--no-verify 우회 차단)

**파일**: `.git/hooks/pre-push`

pre-commit hook이 통과 시 `.git/HOOK_VERIFIED`에 `hook-verified:{timestamp}` 서명을 기록.
pre-push hook이 push 시 이 서명의 유효성을 검증 (120초 이내).

| 시나리오 | 결과 |
|---------|------|
| 정상 커밋 후 push | 통과 |
| `--no-verify`로 커밋 후 push | 차단 (서명 없음) |
| 커밋 후 120초 이상 경과 후 push | 차단 (서명 만료) |
| `ALLOW_FORCE_PUSH=1 git push` | 긴급 우회 (CEO 승인) |

---

## 품질 게이트

### 게이트 1: pre-commit hook (7단계)

모든 git commit 시 자동 실행. `--no-verify` 절대 금지.

| 단계 | 내용 | 차단 조건 |
|------|------|-----------|
| 1 | API 키 패턴 탐지 (9개) | 패턴 감지 시 |
| 2 | Python 구문 검사 (`ast.parse`) | 실패 시 |
| 3 | ruff 정적 분석 (F821/F811) | 오류 시 |
| 4 | Docker import 검증 | `app/*` 파일 import 실패 시 |
| 5 | R-AUTH 위반 감지 | `ANTHROPIC_API_KEY` 직접 사용 시 |
| 6 | AUTH 핵심 파일 보호 | 6개 파일 수정 시 (`ALLOW_AUTH_COMMIT=1` 없으면) |
| 7 | LLM smoke test | 채팅 관련 파일 수정 시 호출 실패 |

통과 시 `.git/HOOK_VERIFIED`에 서명 기록.

### 게이트 2: pre-push hook (v1.1 신규)

`HOOK_VERIFIED` 서명 검증. `--no-verify` 커밋 우회 차단.

### 게이트 3: AI 코드 리뷰

- 경로 A: commit 후 자동 (비동기, 차단 안 함)
- 경로 B: Phase 2~3 검수 루프 (FAIL → 재지시, PASS 필수)

### 게이트 4: CEO 승인 (경로 B)

`awaiting_approval` → CEO "승인해" / "approve" 입력 필수.

### 게이트 5: 배포 후 health check + 모니터링

- health check: `curl -sf http://localhost:8080/health`
- Cross-Monitor: 68↔211↔114 상호 감시 (SSH 경유, 2분 주기, 90% 임계값)
- Watchdog: 5분 주기, API unhealthy 시 1회 자동복구 시도 후 알림

---

## 감시 체계 (v1.1 개선)

### Cross-Monitor (`cross_monitor.sh`)

| 항목 | v1.0 | v1.1 |
|------|------|------|
| HTTP health check 방식 | 외부 직접 curl (오탐) | SSH 경유 localhost curl |
| 알림 임계값 | 50% (3/6) | 90% (5.4/6 → 6개 중 1개 실패도 알림) |
| 211/114 포트 | 외부 9090 (비개방) | SSH → localhost:9090 |

### Watchdog (`watchdog-host.sh`)

| 항목 | v1.0 | v1.1 |
|------|------|------|
| API unhealthy 대응 | 알림만 | 1회 `supervisorctl restart aads-api` 시도 후 알림 |

---

## 배포 방식 (v1.1 개선)

### Blue-Green 무중단 배포 (`scripts/blue_green_deploy.sh`)

```
1. 사전 검증 (pre_deploy_validate.sh)
2. 배포 락 획득 (/tmp/aads-deploy.lock)
3. deploy_history DB 기록 (started) ← v1.1 P2
4. Green(8102) 기동 + health check
5. nginx upstream 전환 (Blue→Green)
6. 검증 (5초 대기 + health)
7. Blue 중지 + Green→Blue 스왑
8. deploy_history DB 기록 (success/failed/rolled_back) ← v1.1 P2
```

롤백: Green health 실패 시 자동 → nginx Blue 복구 → DB에 `rolled_back` 기록.

### 사전 검증 (`scripts/pre_deploy_validate.sh`)

배포 전 자동 실행. Python 구문 검사 + Docker import 검증 + 현재 서비스 health 확인.

---

## 컴포넌트 위치 참조

| 컴포넌트 | 파일 경로 | 비고 |
|---------|-----------|------|
| 파일 수정 + 후처리 훅 | `app/services/tool_executor.py` (L371~625) | |
| 프로젝트별 Git Lock | `app/core/git_lock.py` | v1.1 P4 |
| AI 코드 리뷰 서비스 | `app/services/code_reviewer.py` | |
| Pipeline Runner 핵심 | `app/services/pipeline_c.py` | |
| pre-commit hook | `.git/hooks/pre-commit` | 7단계 |
| pre-push hook | `.git/hooks/pre-push` | v1.1 I1 |
| Blue-Green 배포 | `scripts/blue_green_deploy.sh` | v1.1 |
| 사전 검증 | `scripts/pre_deploy_validate.sh` | v1.1 |
| 배포 이력 마이그레이션 | `migrations/041_deploy_history.sql` | v1.1 P2 |
| Cross-Monitor | `cross_monitor.sh` (호스트) | v1.1 개선 |
| Watchdog | `watchdog-host.sh` (호스트) | v1.1 개선 |

---

## 버전 이력

| 버전 | 날짜 | 내용 |
|------|------|------|
| v1.0 | 2026-04-01 | 최초 작성. 경로 A/B 실측 기반 전체 흐름 문서화 (AADS-191) |
| v1.0.1 | 2026-04-01 | asyncio.Lock 동시수정 보호 추가, AI리뷰 3버그 수정 |
| **v1.1** | **2026-04-02** | **P2 배포이력 DB, P4 fcntl git lock, I1 pre-push hook, Cross-Monitor SSH 경유, Watchdog 자동복구, Blue-Green 스크립트, 사전검증 스크립트** |
