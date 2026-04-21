# 채팅 Hook 대시보드(Next.js) 확장 + 다른 프로젝트 Workdir 정리

- 작업일: 2026-04-21 16:36 KST
- 작업자: AADS 프로젝트 전담 AI
- 커밋: `f4cd880` (aads-server) / Hook 자동 검증 `0d90939` (aads-dashboard)
- 브라우저: https://github.com/moongoby-GO100/aads-server/commit/f4cd880

---

## 1. 배경

CEO 지시 2건:
1. **대시보드(Next.js) 경로도 Hook에 포함해 무중단 빌드 트리거까지 자동화**
2. **다른 프로젝트 세션의 workdir은 어떻게 적용되는지 보고**

이전 세션(`3e7f03d`/`8ae6a7e`)에서 `run_remote_command`에 sed/tee/리다이렉트 우회 차단 Hook을 적용했으나 **`aads-server` 레포만** 감시 대상이었다. 대시보드 파일은 여전히 Hook 밖.

---

## 2. 구현 요약

### 2.1 핵심 변경 (`app/services/tool_executor.py`)
| 변경 | 요약 |
|------|------|
| `_git_status_snapshot(repo_dir)` | 임의 레포 스냅샷으로 일반화. `--porcelain` 파싱 대신 `git diff --name-only HEAD` + `git ls-files --others --exclude-standard` 조합 — XY 상태코드 앞 공백 처리 버그 원천 차단. |
| `_git_status_snapshot_dashboard()` | 신규. `/root/aads/aads-dashboard` 감시. |
| `_run_remote_command` | 서버·대시보드 두 레포의 before/after를 동시 스냅샷. 차집합의 파일마다 `_post_file_modify_hook(..., repo="aads-server" | "aads-dashboard")` 호출. |
| `_post_file_modify_hook(..., repo=)` | 레포 인자로 git commit/push 대상 분기. 대시보드 변경이고 `.ts/.tsx/.js/.jsx/.css/.scss/next.config.*/package*.json/tsconfig.json` 등 빌드 필요 파일이면 `_trigger_dashboard_build_async` 호출. 빌드 불필요(`.md`, `public/*.json` 등)는 commit만. |
| `_trigger_dashboard_build_async` | 30초 in-proc 쿨다운 + 래퍼 스크립트 호출. |
| CHANGELOG | 레포별 파일 분리 — `CHANGELOG-direct-edit.md` (서버), `CHANGELOG-dashboard-direct.md` (대시보드). |
| AI 코드 리뷰 | `aads-server`만 수행. 대시보드는 빌드로 자동 검증. |

### 2.2 래퍼 스크립트 신규 (`scripts/trigger-dashboard-build.sh`)
- `pgrep`으로 `build-dashboard.sh` 실행 중이면 즉시 SKIP (중복 빌드 차단)
- `/tmp/dash-build.log` mtime 기준 60초 이내면 SKIP (빠른 연속 저장 흡수)
- 통과 시 `nohup bash build-dashboard.sh >> log 2>&1 &` 후 즉시 `exit 0` (10초 타임아웃 회피)

### 2.3 오염 방지 — 중요 버그 2건 수정
1. **파싱 버그**: `line[3:]` 슬라이스가 wrapper leading whitespace를 만나 첫 글자 누락 → 잘못된 경로로 차집합 발생 → 기존 dirty 파일(`env_unknown.json`)까지 자동 커밋. 정규식 교체도 `\s*` 소비 문제로 불완전. 최종적으로 `--porcelain` 파싱 자체를 제거하고 `git diff --name-only` + `git ls-files`로 교체.
2. **오염 커밋 복구**: 테스트 과정에서 발생한 `67c95f8`, `47f811c` 두 커밋을 main에서 revert(`6a6f4f4`, `4d1c798`) 후 원래 dirty 상태를 working tree에 복원.

---

## 3. E2E 검증 결과

### 3.1 파싱 정상 확인
```
[BEFORE] server: ['app/services/tool_executor.py', 'scripts/build-dashboard.sh',
                  'scripts/rebuild-dashboard.sh', 'scripts/trigger-dashboard-build.sh']
[BEFORE] dash  : ['public/manager/env_unknown.json']
```
(이전 버그 시: `pp/services/...`, `ublic/manager/...` ← 첫 글자 누락)

### 3.2 sed → Hook 자동 감지·commit·push 검증
```
[B] dash: ['public/manager/env_unknown.json']       ← 이미 dirty
[SED exit]: True                                     ← sed 실행
[A] dash: ['public/manager/env_unknown.json']       ← 수정 파일은 이미 commit됨
[DIFF]: []                                           ← 차집합 정상

INFO: run_cmd_hook_fire: repo=dashboard file=docs/hook_chat_test.md
INFO: dashboard_hook_no_build: file=docs/hook_chat_test.md (commit-only)
INFO: post_hook_commit: [main 0d90939] Chat-Direct[aads-dashboard]: docs/hook_chat_test.md 수정
INFO: post_hook_push:   → origin/main
```
→ **수정한 파일만 정확히 커밋**, dirty였던 `env_unknown.json`은 건드리지 않음.

### 3.3 빌드 트리거 스크립트 독립 테스트
```
[16:35:55 KST] [trigger] START — dashboard rebuild dispatched
[16:35:55 KST] [trigger] dispatched pid=21058
---2nd call---
[16:35:57 KST] [trigger] SKIP — build-dashboard.sh already running  ← 중복 차단 정상
```

---

## 4. "무중단" 표현 관련 솔직 보고

본 Hook이 호출하는 `scripts/build-dashboard.sh`는 `docker compose build` + `docker compose up -d aads-dashboard` 조합이다. 이는 **단일 컨테이너 교체** 방식이므로 완전 무중단이 아니라 **수 초 수준의 짧은 다운타임**이 있다. 진정한 무중단(blue/green)을 원하시면 aads-dashboard에도 aads-server처럼 `deploy.sh bluegreen`을 도입해야 한다. 현재는:
- 빌드(30~90초)는 기존 컨테이너가 서빙하는 동안 백그라운드 진행
- `up -d`로 교체되는 순간만 짧은 다운
→ 필요 시 다음 작업으로 dashboard bluegreen 도입 가능.

---

## 5. 다른 프로젝트 Workdir & Hook 적용 현황

`app/core/project_config.py` `PROJECT_MAP` 실측:

| 프로젝트 | 서버 | Workdir | 언어 | 채팅 Hook | 배포 정식 경로 |
|---------|------|---------|------|-----------|----------------|
| AADS | host.docker.internal (68) | `/root` (실 repo `/root/aads/aads-server`) | Python | ✅ 적용 | Hot-Reload (동일 컨테이너) |
| AADS-Dashboard | 동일 (68) | `/root/aads/aads-dashboard` | Next.js | ✅ **금번 추가** | `trigger-dashboard-build.sh` |
| KIS | 211.188.51.113 | `/root/kis-autotrade-v4` | Python | ❌ 미적용 | Pipeline Runner (서버211) |
| GO100 | 211.188.51.113 | `/root/kis-autotrade-v4` | Python | ❌ 미적용 | Pipeline Runner (서버211) |
| SF | 114.207.244.86:7916 | `/` | Python | ❌ 미적용 | Pipeline Runner (서버114) |
| NTV2 | 114.207.244.86:7916 | `/` (+ `workdir_v2=/srv/newtalk-v2`) | PHP | ❌ 미적용 | Pipeline Runner (서버114) |

### 미적용 근거 (설계 의도)
1. **서버 분리**: AADS만 같은 서버(68)라 저지연 git/reload 가능. 나머지는 SSH 왕복이라 채팅 응답을 수초 막음.
2. **배포 방식 이질성**: KIS/GO100(git pull + supervisor), SF(docker compose), NTV2(PHP 핫리로드) — Hook 한 벌로 처리 불가.
3. **안전장치 일원화**: 다른 프로젝트는 `pipeline_runner_submit` → commit → AI 검수 → CEO 승인 → push/배포 루프를 강제. 채팅에서 즉시 수정·배포하면 검수 단계 누락.

### 확장 필요 시
프로젝트별 `{repo_dir, commit/push/reload 명령}` 레지스트리를 신설해야 한다. 현재는 하드코딩 분기(`if project == "AADS"`). CEO 판단 필요.

---

## 6. 교훈

- **`git status --porcelain` 파싱 금지**. tool wrapper가 leading whitespace를 붙이면 XY 상태코드 앞 공백이 소비되어 파일명 첫 글자가 잘린다. `git diff --name-only HEAD` + `git ls-files --others --exclude-standard`가 안전.
- **차집합 기반 Hook은 파싱 정확도에 100% 의존**. 한 글자 누락이 기존 dirty 파일을 "신규 변경"으로 오판해 무관한 파일까지 자동 commit.
- 테스트 시 **기존 dirty 파일이 있는 레포**에서는 반드시 BEFORE/AFTER 스냅샷 내용을 로그로 찍어 차집합을 육안 확인해야 한다.

---

## 7. 다음 액션 후보

- (P2) `aads-dashboard`에 blue/green 배포 도입 (`deploy.sh bluegreen` 포팅)
- (P2) `public/` 정적 파일은 빌드 없이 `docker cp`로 핫 적용하도록 별도 분기
- (P3) 다른 프로젝트(KIS/GO100/SF/NTV2) 채팅 Hook 확장 — 현재는 Pipeline Runner 경유가 공식 경로
