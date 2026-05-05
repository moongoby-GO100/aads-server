# AADS Pipeline Runner 신뢰도 복구 리포트

- 작성 시각: 2026-05-04 10:39 KST
- 범위: AADS Pipeline Runner 실행, 검수, 승인, push, 배포 경로
- 근거: `pipeline_jobs`, `code_reviews` 실측과 러너/리뷰 코드 확인

## 1. 요약

AADS Pipeline Runner의 현재 핵심 장애는 "배포가 가끔 느리다"가 아니라, 그 이전 단계인 "실제 코드 수정이 없는 작업이 대량 발생하고, 그 결과 검수와 배포 신뢰도가 연쇄적으로 무너진다"에 있다.

가장 큰 원인은 세 가지다.

1. Claude/Codex/LiteLLM 실행이 끝나도 실제 파일 변경이 없는 작업이 너무 많다.
2. 리뷰 시스템은 이를 감지하지만, `INVALID_REVIEW_INPUT`/`REVIEW_SYSTEM_FAILURE`가 반복돼 품질 신호가 오염된다.
3. AADS 프론트엔드는 서버 레포 내부 복사본 `./aads-dashboard`와 실제 배포 레포 `/root/aads/aads-dashboard`가 분리되어 있어, 검수 통과 후에도 실배포 누락이 발생할 수 있다.

## 2. 실측 증거

### 2.1 최근 AADS 러너 상태

- 최근 30일 AADS `pipeline_jobs` 상태: `rejected_done` 120건, `queued` 4건, `failed` 1건, `rolling_back` 1건 [DB 조회]
- 같은 기간 AADS 종료 계열 작업(`awaiting_approval/approved/deploying/done/error/rejected/rejected_done`) 120건 중 `git_diff` 빈 값 106건, 비어있지 않은 값 14건 [DB 조회]
- 즉, 종료 계열 작업의 `88.3%`가 실질 diff 없이 끝난 상태다 [DB 조회]

### 2.2 최근 리뷰 분포

- 최근 30일 `code_reviews` 판정: AADS `FLAG` 655건, `APPROVE` 70건, `REQUEST_CHANGES` 51건 [DB 조회]
- 운영 DB의 `code_reviews` 스키마에는 `flag_category`, `failure_stage` 컬럼이 아직 없어, 코드가 기대하는 구조화 실패 사유가 분석 테이블에 완전히 남지 않는다 [DB 조회 + 코드]

### 2.3 대표 실패 흔적

`pipeline_jobs.review_feedback` 최근 사례:

- "git_diff 비어 있음 — 파일 쓰기 권한 차단으로 실제 코드 변경 없음"
- "OAuth 401 인증 실패로 변경사항 0건"
- "Claude Code API 사용량 제한으로 인해 변경사항이 생성되지 않음"

위 패턴은 2026-04-08~2026-05-04 AADS 잡들에서 반복 확인된다 [DB 조회].

## 3. 코드 기준 원인 분석

### 3.1 실행 단계: "완료"와 "실제 수정"이 분리되어 있음

`scripts/pipeline-runner.sh`는 작업 종료 후 `git diff HEAD`를 읽고 승인 대기로 넘긴다. 그러나 diff가 비어도 러너는 별도 실패 처리 없이 `awaiting_approval`로 보낼 수 있다.

관련 위치:

- `scripts/pipeline-runner.sh`: Phase1 종료 후 `git diff HEAD` 수집
- `scripts/pipeline-runner.sh`: `git_diff`를 그대로 저장하고 승인 요청 메시지 생성

즉, "모델 실행 성공"과 "파일 수정 성공"이 같은 판정으로 묶여 있다.

### 3.2 리뷰 단계: 실패를 감지하지만 신뢰도 회복 장치가 약함

`app/services/code_reviewer.py`는 diff 형식이 아니면 `INVALID_REVIEW_INPUT`, 리뷰 JSON 파싱 실패면 `REVIEW_SYSTEM_FAILURE`로 `FLAG`를 반환한다. 이 감지 자체는 맞다.

하지만 현재는 다음 문제가 있다.

1. 러너가 잘못된 입력을 보내는 순간 리뷰는 품질 판정이 아니라 장애 알림기로 변한다.
2. `code_reviews` 운영 스키마가 최신 컬럼을 담지 못해, 실패 유형 통계가 약해진다.
3. 리뷰 실패가 러너 재실행 정책과 직접 연결되지 않아 같은 유형이 반복된다.

### 3.3 배포 단계: AADS 프론트 경로가 이중화되어 있음

현재 확인 결과:

- 서버 레포 루트: `/root/aads/aads-server`
- 서버 레포 내부 복사본: `/root/aads/aads-server/aads-dashboard`
- 실제 대시보드 레포: `/root/aads/aads-dashboard`

그런데 `deploy_job()`은 메인 worktree를 `/root/aads/aads-server` 기준으로 병합하고, 대시보드 배포는 `/root/aads/aads-dashboard`의 git 상태를 본다. 따라서 러너가 서버 레포 내부 복사본 `./aads-dashboard`를 수정하면:

1. 검수 diff에는 프론트 변경이 보일 수 있다.
2. 실제 배포 레포는 깨끗해서 대시보드 배포가 누락될 수 있다.

이 구조는 AADS-203 류의 "검수 통과했는데 화면에 안 보임" 문제를 재발시킨다.

### 3.4 worktree 병합 경로가 복잡해 diff 신뢰도를 더 떨어뜨림

`deploy_job()`은 worktree에서 `git diff --cached HEAD`를 main workdir에 `git apply --3way`로 병합하고, 실패 시 파일 복사 fallback까지 사용한다. 이 경로는 복구력이 있지만, 다음 부작용이 있다.

1. 변경이 실제로 어느 레포에 들어갔는지 추적이 어렵다.
2. diff 비어 있음과 파일 복사 fallback이 섞이면 진짜 변경 여부 판단이 늦어진다.
3. dashboard처럼 별도 레포가 섞인 경우 더욱 불안정해진다.

## 4. 복구 전략

### P0. "무변경 작업" 즉시 차단

목표: 실질 수정 없는 작업이 승인 대기로 올라오지 못하게 막는다.

조치:

1. `scripts/pipeline-runner.sh`에서 `git_diff`가 비어 있거나 너무 짧으면 즉시 `error` 또는 `rejected` 계열로 종료
2. `result_output`에 권한/인증/limit 문구가 있으면 `RUNNER_AUTH_FAILURE`, `RUNNER_PERMISSION_FAILURE`, `RUNNER_RATE_LIMIT`로 분류
3. `awaiting_approval` 진입 조건을 "모델 실행 성공"이 아니라 "실제 파일 변경 존재"로 바꾼다

성공 기준:

- `empty git_diff / terminal jobs` 비율 88.3% → 20% 이하로 감소 [목표, 미측정]

### P0. AADS 프론트엔드 레포 경로 단일화

목표: `aads-dashboard`가 어느 레포의 진실 소스인지 하나로 고정한다.

조치:

1. 러너 지시에서 AADS 프론트 수정 경로를 반드시 `/root/aads/aads-dashboard`로 강제
2. 서버 레포 내부 `./aads-dashboard`를 참조 전용으로 둘지, 제거할지 결정
3. `deploy_job()`에서 대시보드 변경 감지를 "실제 배포 레포" 기준으로만 수행

성공 기준:

- "검수 diff 존재 but 실배포 미반영" 유형 0건 [목표, 미측정]

### P1. 리뷰 실패를 운영 지표로 승격

목표: 리뷰 `FLAG`를 품질 판정이 아니라 장애 카테고리로 재분류한다.

조치:

1. `code_reviews`에 `flag_category`, `failure_stage`, `needs_retry` 컬럼 추가
2. `INVALID_REVIEW_INPUT`, `REVIEW_SYSTEM_FAILURE`, `RUNNER_AUTH_FAILURE`를 대시보드 집계에 노출
3. 같은 카테고리가 3회 이상 반복되면 자동으로 러너 제출 차단 또는 다른 worker model로 우회

성공 기준:

- AADS `FLAG` 대비 `APPROVE+REQUEST_CHANGES` 비율 개선 [목표, 미측정]

### P1. worktree 병합 단순화

목표: "worktree 수정 → 실제 배포 레포 반영" 경로를 추적 가능하게 만든다.

조치:

1. AADS는 `aads-server`와 `aads-dashboard`를 별도 작업 단위로 분리
2. 러너가 둘 다 수정해야 하면 job을 2개로 쪼개거나, repo별 명시적 copyback 함수를 둔다
3. `git apply --3way` 실패 후 file-copy fallback이 실행되면 로그와 DB에 강하게 표시한다

## 5. 즉시 실행 권고안

가장 먼저 할 일은 세 가지다.

1. `scripts/pipeline-runner.sh`에 `empty git_diff hard-fail` 추가
2. AADS 프론트엔드 경로를 단일화하고, `./aads-dashboard`와 `/root/aads/aads-dashboard`의 역할을 정리
3. `code_reviews` 스키마를 현재 코드 기대치에 맞춰 확장

이 세 가지를 먼저 하지 않으면, 이후 timeout 조정이나 모델 교체는 증상 완화만 하고 신뢰도 복구는 못 한다.

## 6. 관련 파일

- `scripts/pipeline-runner.sh`
- `app/services/code_reviewer.py`
- `app/api/code_review.py`
- `app/api/pipeline_runner.py`
- `docs/knowledge/CTO-SYSTEM-MAP.md`

