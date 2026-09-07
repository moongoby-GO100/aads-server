# AADS Docker Build P1 Optimization Design and PRD

작성 시각: 2026-09-07 19:09 KST  
대상 프로젝트: AADS backend (`/root/aads/aads-server`)  
문서 목적: Docker 빌드 지연 P1 개선을 실제 구현 작업으로 전환하기 위한 설계, 기술스택, PRD, 검증 기준 정리

## 1. 요약

AADS backend Docker 빌드는 2026-09-07 기준 운영을 차단하지는 않지만, 배포 리드타임과 디스크 사용량을 계속 키우는 구조적 병목이 남아 있다.

실측 근거는 다음과 같다.

| 항목 | 실측값 | 근거 |
|---|---:|---|
| 최신 AADS 이미지 크기 | 4.01GB | `docker image ls`, 2026-09-07 19:09 KST |
| 이전 릴리스 이미지 크기 | 5.95GB | `docker image ls`, 2026-09-07 19:09 KST |
| Python 의존성 설치 레이어 | 1.69GB | `docker history aads-server:ec2383bed46e` |
| Playwright/Chromium 레이어 | 1.03GB | `docker history aads-server:ec2383bed46e` |
| Docker image 총량 | 48.1GB | `docker system df`, 2026-09-07 19:09 KST |
| Docker build cache | 22.28GB | `docker system df`, 2026-09-07 19:09 KST |
| 최근 성공 배포 run 128 | 1,581,803ms | `deploy_runs`, 2026-09-07 18:20~18:46 KST |
| 최근 실패 배포 run 127 | no space left on device | `deploy_runs`, 2026-09-07 17:39~17:52 KST |
| 최근 build_candidate_image | 589,000ms | `deploy_phase_events` |
| 최근 standby sync | 351,000ms | `deploy_phase_events` |

P0 조치로 디스크 preflight와 이미지 크기 게이트는 이미 들어갔다. P1은 배포를 더 안정적으로 빠르게 만들기 위한 구조 개선이며, 핵심은 `의존성 잠금`, `multi-stage wheelhouse`, `Playwright 분리`, `캐시/이미지 보존 정책 자동화`이다.

## 2. 현재 아키텍처

### 2.1 배포 흐름

현재 `deploy.sh bluegreen`은 다음 계약을 따른다.

1. clean release SHA 기준으로 release context 생성
2. Docker image를 SHA 태그로 1회 build
3. candidate slot을 `--no-build`로 기동
4. candidate direct health 확인
5. nginx upstream 전환
6. routed health 확인
7. standby slot을 같은 digest로 동기화
8. DB/schema/chat/LLM/frontend QA
9. 5분 P0/P1 모니터링 후 완료

이 흐름 자체는 유지해야 한다. P1 개선은 blue-green 배포 계약을 바꾸지 않고 Docker build 단계와 image size를 줄이는 데 집중한다.

### 2.2 Dockerfile 구조

현재 Dockerfile의 주요 특징은 다음과 같다.

| 영역 | 현재 구조 | 병목 |
|---|---|---|
| Base image | `python:3.12-slim` | 적절함 |
| Python 의존성 | `pip install -e ".[dev]"` | dev 의존성까지 runtime image에 포함 |
| Rust 설치 | `rustup`으로 빌드 중 설치 후 삭제 | 일부 패키지 wheel 미확보 시 느림 |
| 코드 복사 | `COPY . .` | context는 `git archive HEAD`로 제한되지만 tracked report 증가 시 위험 |
| Playwright | runtime image 안에서 `playwright install chromium --with-deps` | Chromium/OS deps 1.03GB 레이어 |
| 프로세스 | `supervisord`로 API + MCP 서버 동시 기동 | 현재 유지 |

### 2.3 이미 적용된 P0

| 항목 | 상태 | 설명 |
|---|---|---|
| 디스크 preflight | 반영됨 | `/var/lib/docker` 또는 지정 경로의 가용 공간이 부족하면 build 전 실패 |
| release context size gate | 반영됨 | clean context가 임계치 초과 시 실패 |
| release image size gate | 반영됨 | 빌드 후 이미지 크기 임계치 초과 시 실패 |
| Rust final layer 제거 | 부분 반영 | Rust home 삭제로 이전 5.95GB 이미지 대비 최신 4.01GB까지 축소 |

## 3. 문제 정의

### 3.1 제품/운영 문제

배포가 20분 이상 걸리면 CEO와 작업세션에서 다음 문제가 생긴다.

| 문제 | 영향 |
|---|---|
| 응답 대기 장기화 | 채팅 세션 중단, resume failure, 완료 보고 누락 가능성 증가 |
| 배포 큐 적체 | 후속 작업이 queued 상태로 밀림 |
| Docker 디스크 압박 | no-space 실패, 이미지 export 실패, standby sync 실패 가능성 증가 |
| rollback 품질 저하 | 오래된 이미지 보존과 재빌드 기준이 불명확하면 긴급 복구 시간이 늘어남 |
| 개발자 피드백 지연 | 사소한 backend 변경도 운영 검증까지 20분 이상 소요 |

### 3.2 기술 문제

| 원인 | 현재 근거 | 개선 방향 |
|---|---:|---|
| loose dependency range | `pyproject.toml`에 `>=` 다수 존재 | constraints/lock 도입 |
| dev 의존성 runtime 포함 | `pip install -e ".[dev]"` | runtime/dev 분리 |
| wheel build/re-resolve 반복 | Dockerfile에서 매 build pip install | wheelhouse builder stage |
| Playwright 일괄 포함 | Chromium 레이어 1.03GB | base image 분리 또는 optional visual QA worker |
| image/cache 보존 정책 수동 | Docker image 48.1GB, cache 22.28GB | 정책 기반 prune + keep set |
| release context 관리 수동 | `.dockerignore`는 있으나 tracked report 증가 위험 | context manifest audit |

## 4. 목표

### 4.1 정량 목표

| 목표 | 현재 | 목표 |
|---|---:|---:|
| image size | 4.01GB | 2.5GB 이하 |
| build_candidate_image | 589초 | 300초 이하 |
| full blue-green release | 1,581초 | 900초 이하 |
| no-space build failure | 최근 발생 | 0건 유지 |
| rollback 가능 release image | 수동 확인 | 최근 N개 SHA 자동 보존 |

위 목표는 구현 후 실측으로 검증해야 하며, 본 문서의 숫자는 현재 기준선이다.

### 4.2 비목표

다음은 이번 P1의 범위가 아니다.

| 제외 항목 | 이유 |
|---|---|
| blue-green 배포 계약 완화 | AADS 릴리스 안전 규칙 위반 위험 |
| 5분 P0/P1 모니터링 제거 | 완료 판정 신뢰도 저하 |
| active API 직접 restart | 운영 규칙 위반 |
| 모든 Docker cache 삭제 자동화 | 다음 빌드가 더 느려질 수 있음 |
| Playwright 기능 제거 | Visual QA/E2E 검증 기능 유지 필요 |

## 5. 기술스택

### 5.1 현행 유지 기술

| 계층 | 기술 | 유지 이유 |
|---|---|---|
| Runtime | Python 3.12 slim | 현재 운영 컨테이너 기준 |
| API | FastAPI, Uvicorn | AADS backend 표준 |
| Process | supervisord | API/MCP 동시 기동 구조 유지 |
| Packaging | `pyproject.toml`, setuptools | 현행 패키징 방식 |
| Build | Docker BuildKit | cache mount와 deterministic build 활용 |
| Deploy | `deploy.sh bluegreen` | 무중단/rollback/same-digest 계약 |
| DB | PostgreSQL deploy_runs/deploy_phase_events | 배포 원장 및 단계별 실측 |
| QA | pytest, curl health, P0/P1 log monitor | 현재 릴리스 인증 체계 |

### 5.2 신규/보강 기술

| 기술 | 용도 | 도입 방식 |
|---|---|---|
| `requirements.lock` 또는 `constraints.txt` | Docker build 의존성 고정 | `pyproject.toml`에서 compile된 lock을 build에 사용 |
| `pip-tools` 또는 `uv pip compile` | lock 생성 자동화 | CI/로컬 명령으로 lock 갱신 |
| multi-stage Docker build | builder/runtime 분리 | wheelhouse를 builder에서 만들고 runtime에 설치 |
| Docker cache mount | pip/wheel 캐시 재사용 | BuildKit cache id를 release SHA와 무관하게 안정화 |
| image profile split | runtime/API와 visual QA 분리 | `aads-server`와 `aads-server-visual` 또는 build arg |
| retention script | 이미지/캐시 정리 | running image, recent successful SHA, rollback SHA 보존 |
| release context manifest | context 구성 가시화 | tracked 대형 파일/리포트 포함 여부를 deploy preflight에 기록 |

## 6. 제안 아키텍처

### 6.1 Target build architecture

```text
pyproject.toml
   |
   +-- requirements.runtime.lock
   +-- requirements.dev.lock
   +-- requirements.visual.lock
              |
              v
Dockerfile
   |
   +-- builder stage
   |      - apt build deps
   |      - pip wheel -r requirements.runtime.lock
   |      - optional visual wheels
   |
   +-- runtime stage
   |      - apt runtime deps only
   |      - pip install --no-index --find-links=/wheels
   |      - copy app source
   |      - no dev/test tooling by default
   |
   +-- visual stage or optional image
          - Playwright browsers
          - Visual QA only
```

### 6.2 Dependency profiles

| Profile | 포함 | 사용처 |
|---|---|---|
| runtime | FastAPI, DB, LLM adapters, MCP minimum | production API image |
| dev | pytest, ruff, mypy | local/CI/test image |
| visual | playwright, Pillow, browser deps | E2E/visual QA worker |
| optional-heavy | chromadb, e2b, langfuse 등 optional | graceful degradation 또는 별도 worker |

현재 `pyproject.toml`은 runtime dependencies에 heavy/optional 성격의 패키지가 섞여 있다. P1에서는 “운영 API에 항상 필요한 것”과 “특정 기능에서만 필요한 것”을 분리해야 한다.

### 6.3 Deploy architecture changes

`deploy.sh`는 현재 safety gate를 유지하면서 다음 기능을 추가한다.

| 기능 | 설명 |
|---|---|
| dependency lock freshness check | `pyproject.toml` 변경 시 lock 파일이 함께 변경되지 않으면 preflight 실패 |
| image profile selection | 기본은 `runtime`, Visual QA 필요 시 `visual` 별도 build |
| build timing event detail | pip wheel, apt install, Playwright install 시간을 별도 metadata로 기록 |
| retention preflight | reclaimable image가 임계치 초과하면 경고 또는 자동 정리 제안 |
| release context manifest | 상위 tracked 파일/크기 Top N을 deploy log에 남김 |

## 7. PRD

### 7.1 제품명

AADS Build Optimizer P1

### 7.2 사용자

| 사용자 | 요구 |
|---|---|
| CEO | 배포가 끝났는지, 왜 늦는지, 어떤 변경이 반영됐는지 즉시 판단 |
| CTO/운영자 | Docker build 실패 전 위험 감지, rollback 가능성 보존 |
| Pipeline Runner | 커밋 승인 후 긴 build에 묶이지 않고 deploy queue로 handoff |
| 개발자/QA | 테스트 이미지와 운영 이미지의 의존성 차이를 명확히 이해 |

### 7.3 사용자 스토리

1. 운영자로서 build 전 디스크/컨텍스트/lock 상태가 위험하면 배포가 시작되기 전에 실패 이유를 알고 싶다.
2. CEO로서 배포 완료 보고를 받을 때 build, cutover, standby sync, P0/P1 monitor 중 어디서 시간이 걸렸는지 알고 싶다.
3. 개발자로서 `pyproject.toml` 의존성을 바꾸면 lock 파일 누락 때문에 운영 빌드가 비결정적으로 느려지는 일을 막고 싶다.
4. QA로서 Visual QA 기능은 유지하되, 일반 API 배포 이미지가 Chromium 때문에 매번 커지는 것을 피하고 싶다.
5. 운영자로서 Docker prune을 해도 현재 active/standby와 최근 rollback 이미지는 삭제되지 않는다는 보장이 필요하다.

### 7.4 기능 요구사항

| ID | 요구사항 | 우선순위 | 완료 기준 |
|---|---|---|---|
| P1-F01 | runtime dependency lock 생성 | P1 | `requirements.runtime.lock` 존재, Docker build가 lock 사용 |
| P1-F02 | dev/test dependency 분리 | P1 | production Dockerfile이 `.[dev]`를 설치하지 않음 |
| P1-F03 | multi-stage wheelhouse build | P1 | builder stage에서 wheel 생성, runtime stage는 wheel만 설치 |
| P1-F04 | Playwright 분리 | P1 | API 기본 이미지에서 Chromium 설치가 빠지거나 build arg로 분리 |
| P1-F05 | deploy lock freshness gate | P1 | `pyproject.toml` 변경 시 lock 누락을 preflight에서 차단 |
| P1-F06 | build phase timing 기록 | P1 | `deploy_phase_events.metadata`에 build substep time 저장 |
| P1-F07 | Docker retention script | P1 | running + recent success SHA 보존 후 안전 정리 |
| P1-F08 | context manifest audit | P2 | clean context size와 Top N 대형 tracked 파일 기록 |

### 7.5 비기능 요구사항

| 항목 | 요구 |
|---|---|
| 무중단성 | 기존 blue-green 순서와 lock 범위 유지 |
| 결정성 | 같은 release SHA는 같은 lock 의존성으로 build |
| 관측성 | build substep이 원장에 남아야 함 |
| 복구성 | 최근 성공 이미지 N개와 현재 양 슬롯 digest 보존 |
| 보안 | secret/env 파일은 image/context에 포함 금지 |
| 호환성 | 기존 `deploy.sh bluegreen` 호출 방식 유지 |

### 7.6 API/UI 요구사항

이번 P1의 1차 범위는 backend deploy tooling이다. 단, 이미 배포 상태 API와 dashboard deploy artifact가 있으므로 다음 정보를 노출할 수 있어야 한다.

| 화면/API | 추가 필드 |
|---|---|
| `/api/v1/ops/deploy/status` | image_size_mb, release_context_mb, build_seconds, standby_sync_seconds |
| Chat deploy artifact | build 병목 단계, 현재 release SHA, image profile |
| Ops dashboard | Docker disk available, reclaimable image size, latest failed build reason |

UI 구현은 P1-F01~F07 안정화 후 P2로 분리 가능하다.

## 8. 구현 계획

### Phase 1: 의존성 결정성 확보

| 작업 | 변경 파일 |
|---|---|
| runtime/dev/visual lock 생성 방식 결정 | `pyproject.toml`, 신규 lock 파일 |
| compile 명령 스크립트 추가 | `scripts/compile_requirements.sh` |
| lock freshness 테스트 추가 | `tests/unit/test_deploy_build_guards.py` |

권장 명령:

```bash
python3 -m pip install pip-tools
python3 -m piptools compile pyproject.toml --extra runtime -o requirements.runtime.lock
python3 -m piptools compile pyproject.toml --extra dev -o requirements.dev.lock
```

실제 명령은 패키지 구조에 맞춰 조정해야 한다. 현재 `runtime` extra가 없으므로 먼저 dependency group을 재정의해야 한다.

### Phase 2: Dockerfile multi-stage 전환

| 작업 | 변경 파일 |
|---|---|
| builder stage 추가 | `Dockerfile` |
| runtime apt deps 최소화 | `Dockerfile` |
| `pip install -e ".[dev]"` 제거 | `Dockerfile` |
| wheelhouse install 적용 | `Dockerfile` |

핵심 원칙:

```Dockerfile
FROM python:3.12-slim AS builder
COPY requirements.runtime.lock ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip wheel --wheel-dir /wheels -r requirements.runtime.lock

FROM python:3.12-slim AS runtime
COPY --from=builder /wheels /wheels
COPY requirements.runtime.lock ./
RUN pip install --no-index --find-links=/wheels -r requirements.runtime.lock
COPY . .
```

### Phase 3: Playwright/Visual QA 분리

| 선택지 | 장점 | 단점 | 권장 |
|---|---|---|---|
| 별도 image `aads-server-visual` | API 이미지 가장 작음 | 배포/QA orchestration 추가 필요 | 중기 권장 |
| Docker build arg `INSTALL_PLAYWRIGHT=true` | 변경 작음 | default 관리 실수 가능 | 1차 권장 |
| 현행 유지 | 작업 작음 | 1.03GB 병목 지속 | 비권장 |

1차 구현은 build arg로 분리하고, 운영 API 기본값은 `false`로 둔다. Visual QA가 필요한 release certification 단계에서만 visual image 또는 일회성 job을 사용한다.

### Phase 4: 안전 정리와 관측성

| 작업 | 변경 파일 |
|---|---|
| Docker image retention script | `scripts/prune_aads_images.sh` |
| retention dry-run 기본값 | 신규 script |
| deploy preflight에 retention 상태 출력 | `deploy.sh` |
| build substep metadata 기록 | `deploy.sh`, deploy DB insert/update 구간 |

보존 원칙:

1. 현재 running 컨테이너 image digest는 삭제 금지
2. `deploy_runs.status='success'` 최근 3개 release SHA 보존
3. 현재 queued/running release SHA 보존
4. 나머지는 dry-run 후 명시 실행

## 9. 테스트 계획

| 테스트 | 명령/도구 | 성공 기준 |
|---|---|---|
| Shell syntax | `bash -n deploy.sh` | exit 0 |
| Dockerfile build smoke | `docker build --target runtime ...` | build success |
| image size gate | `docker image inspect` | 2.5GB 이하 목표 |
| release contract | `scripts/verify-bluegreen-release-contract.sh` | pass |
| unit tests | `python3 -m pytest tests/unit/test_deploy_observability.py tests/unit/test_deploy_build_guards.py -q` | pass |
| blue-green deploy | `bash deploy.sh bluegreen` | success/completed |
| external health | `curl https://aads.newtalk.kr/api/v1/health` | HTTP 200 |
| same digest | `docker inspect` 양 슬롯 비교 | 동일 digest |
| P0/P1 monitoring | 5분 로그 감시 | error/critical 신규 없음 |

## 10. 배포 계획

1. P1 구현 파일만 선별 수정
2. `bash -n deploy.sh`와 단위 테스트 실행
3. `git status`로 unrelated dirty 제외 확인
4. 선별 커밋
5. 원격 push
6. clean SHA 기준 `deploy.sh bluegreen`
7. candidate health, routed health, same digest, 5분 P0/P1 monitoring 확인
8. 배포 원장 `success/completed` 확인
9. HANDOVER 업데이트

## 11. 롤백 계획

| 실패 지점 | 롤백 |
|---|---|
| lock 생성 실패 | lock 파일/pyproject 변경 revert |
| Docker build 실패 | 기존 성공 SHA 재배포 |
| candidate health 실패 | nginx cutover 전이므로 기존 active 유지 |
| routed health 실패 | deploy.sh rollback routing |
| standby sync 실패 | active 정상 여부 확인 후 같은 release image로 standby 재시도 |
| image size 과소 최적화로 runtime import 실패 | heavy optional 패키지 runtime profile에 복구 후 재빌드 |

## 12. 기존 데이터 소급 적용 여부

Docker 빌드 P1은 애플리케이션 데이터 마이그레이션 작업이 아니다. 따라서 고객 데이터, OHVIS memory, deploy_runs 업무 데이터에는 소급 변환이 필요 없다.

다만 Docker/배포 산출물에는 다음처럼 적용 범위가 나뉜다.

| 대상 | 소급 적용 여부 | 설명 |
|---|---|---|
| 기존 DB 데이터 | 해당 없음 | 앱 데이터 구조를 바꾸지 않음 |
| 기존 성공 deploy_runs | 부분 활용 | 과거 duration은 기준선으로 유지, 새 필드는 없으면 null 가능 |
| 기존 Docker image | 자동 축소 안 됨 | 새 Dockerfile로 재빌드한 release부터 작아짐 |
| 기존 build cache | 자동 최적화 아님 | retention/prune 정책으로 별도 정리 필요 |
| 기존 active/standby | 다음 blue-green 이후 적용 | 새 release image로 양 슬롯이 동기화되어야 반영 |

## 13. 리스크와 대응

| 리스크 | 가능성 | 영향 | 대응 |
|---|---:|---:|---|
| runtime/dev 분리 중 import 누락 | 중 | 높음 | import smoke + API route boot test |
| Playwright 분리 후 Visual QA 실패 | 중 | 중 | visual profile 별도 smoke |
| lock 파일 갱신 누락 | 높음 | 중 | deploy preflight gate |
| Docker cache 과도 정리로 첫 빌드 지연 | 중 | 중 | dry-run + keep set |
| optional 패키지 lazy import 누락 | 중 | 높음 | graceful degradation 테스트 |

## 14. 승인 기준

P1 구현은 다음 기준을 만족해야 완료로 보고할 수 있다.

| 기준 | 완료 조건 |
|---|---|
| 코드 | Dockerfile/deploy/lock/test 변경이 선별 커밋됨 |
| 테스트 | syntax, unit, Docker build smoke 통과 |
| 배포 | blue-green run `success/completed` |
| 운영 | `/api/v1/health` HTTP 200 |
| 이미지 | 양 슬롯 same digest |
| 모니터링 | 5분 P0/P1 신규 critical/error 없음 |
| 성능 | build_candidate_image와 full release 시간이 기준선 대비 단축 실측 |

## 15. 권장 실행 순서

1. `requirements.runtime.lock`/`requirements.dev.lock` 구조부터 도입한다.
2. Dockerfile을 multi-stage로 바꾸되, 처음에는 Playwright를 build arg로 optional 처리한다.
3. deploy preflight에 lock freshness와 context manifest를 추가한다.
4. `scripts/prune_aads_images.sh --dry-run`을 만들고 running/recent SHA 보존을 검증한다.
5. blue-green 배포 후 원장 기준으로 build time과 image size를 비교한다.

## 16. Runner 지시서 초안

```text
>>>DIRECTIVE_START
TASK_ID: AADS-DOCKER-BUILD-P1-OPTIMIZATION
TITLE: AADS Docker 빌드 P1 최적화 구현
PRIORITY: P1-HIGH
SIZE: M
MODEL: gpt-5.6-sol
DESCRIPTION:
AADS backend Docker 빌드 지연 P1 개선을 구현한다.

범위:
1. runtime/dev/visual dependency profile과 lock 파일 도입.
2. Dockerfile multi-stage wheelhouse 전환.
3. production image에서 dev dependencies 제거.
4. Playwright/Chromium 설치를 기본 API image에서 분리하거나 build arg로 optional 처리.
5. deploy.sh에 lock freshness, context manifest, image/cache retention status를 추가.
6. 안전한 Docker image prune dry-run script 추가.
7. 단위 테스트와 shell syntax test 추가.

필수 제약:
- 기존 blue-green 계약 유지.
- active API 직접 재시작 금지.
- unrelated dirty 파일 포함 금지.
- secret/env 파일 image/context 포함 금지.
- deploy.sh는 clean release SHA 기준으로만 build.

검증:
- bash -n deploy.sh
- python3 -m pytest 관련 deploy/build guard tests
- docker build smoke
- deploy.sh bluegreen
- /api/v1/health 200
- active/standby same digest
- 5분 P0/P1 monitoring

완료 보고:
- 변경 파일, image size before/after, build_candidate_image before/after, deploy run id, commit/push/deploy 상태, 미완료 리스크를 보고.
>>>DIRECTIVE_END
```

## 17. 구현 결과 보정

작성 시각: 2026-09-07 22:35 KST

P1 설계안 중 운영 이미지 최적화 1차 구현을 완료했다. 이번 구현은 DB 마이그레이션이나 기존 고객/OHVIS 데이터 변경이 아니라, 앞으로 생성되는 AADS backend Docker release image의 빌드 구조와 배포 preflight를 개선하는 작업이다.

| 항목 | 구현 상태 | 검증 근거 |
|---|---|---|
| runtime/dev/visual lock | 완료 | `requirements.runtime.lock`, `requirements.dev.lock`, `requirements.visual.lock` 생성 |
| Docker multi-stage wheelhouse | 완료 | `Dockerfile`에 `wheelhouse`/`runtime` stage 추가 |
| production dev 의존성 제거 | 완료 | Dockerfile에서 `pip install -e ".[dev]"` 제거 |
| Playwright optional 분리 | 완료 | `INSTALL_PLAYWRIGHT=false` 기본값, smoke image에서 `playwright_spec=False` |
| deploy lock freshness gate | 완료 | `deploy.sh`의 `require_dependency_lock_freshness` |
| release context manifest | 완료 | `deploy.sh`의 `emit_release_context_manifest` |
| retention dry-run | 완료 | `scripts/prune_aads_images.sh --dry-run` |
| unit/shell 검증 | 완료 | `pytest` 10 passed, `bash -n` 통과 |
| Docker runtime smoke | 완료 | `aads-server:p1-runtime-smoke`, 앱 import OK, routes 654 |

### 17.1 실측 결과

| 지표 | 구현 전 기준선 | 구현 후 smoke | 출처 |
|---|---:|---:|---|
| 최신 운영 이미지 표시 크기 | 4.01GB | 2.49GB | `docker image ls` |
| 이미지 inspect size | 미측정 | 752,219,227 bytes | `docker image inspect` |
| Playwright 기본 포함 | 포함 | 미포함 | `importlib.util.find_spec("playwright") is None` |
| FastAPI route import | 미측정 | 654 routes | `docker run ... from app.main import app` |
| supervisor 실행 | apt 기반 | pip `supervisord 4.3.0` | `supervisord --version` |

### 17.2 소급 적용 판정

| 대상 | 소급 여부 | 설명 |
|---|---|---|
| 기존 DB 데이터 | 소급 대상 아님 | Docker 빌드 구조 변경이라 앱 데이터 row를 변경하지 않음 |
| 기존 Docker 이미지 | 자동 소급 안 됨 | 과거 이미지 크기는 그대로이며 새 SHA 재빌드부터 축소 |
| 기존 deploy_runs 기록 | 보존 | 과거 duration은 기준선으로 남고 새 배포부터 개선값 측정 |
| 현재 운영 컨테이너 | blue-green 배포 후 적용 | 새 커밋을 release image로 배포해야 active/standby에 반영 |

### 17.3 남은 운영 적용 조건

현재 루트/도커 파일시스템 여유 공간이 16GB라 기본 배포 preflight 기준 20GB를 충족하지 못한다. `scripts/prune_aads_images.sh --dry-run` 기준 실행 중 이미지와 최근 성공 SHA는 보존되고, 구버전 AADS 이미지 5개가 정리 후보로 식별됐다. 운영 배포 전 이 정리를 실행해 공간을 확보해야 한다.
