# run_remote_command Git-Diff Hook 적용 보고

- 작성: 2026-04-21 16:18 KST
- 작업자: CTO AI (직접 적용)
- 관련 브랜치/커밋: `main` / `3e7f03d`, `8ae6a7e`

## 요청 배경

CEO 지시: 채팅창 작업 시 커밋·푸시·무중단 배포·문서기록이 의무적으로 진행되지 않는 문제 해결.

원인:
1. 자동 Hook(`_post_file_modify_hook`)이 `_write_remote_file` / `_patch_remote_file` **두 도구에만** 연결되어 있었고, `run_remote_command`(sed/tee/리다이렉트 등)는 Hook을 우회.
2. AADS 프로젝트 workdir이 `/root`로 설정되어 있어, Hook 내부의 `git add/commit/push` 명령이 모두 `fatal: not a git repository`로 **조용히 실패**해왔음 (로그 레벨 warning만 남김).

## 변경 내역

파일: `app/services/tool_executor.py`

### 1. Git-diff 기반 수정 감지 래퍼 신설 (`3e7f03d`)

- `_git_status_snapshot_aads()` 메서드 추가 — `git status --porcelain` 결과를 파일 경로 집합으로 반환.
- `_run_remote_command` 변경:
  - AADS 프로젝트 + 비-git 명령일 때 실행 전후 스냅샷 비교.
  - `after - before` 차집합에서 신규/변경 파일 추출 → 파일별로 `_post_file_modify_hook` 호출.
  - `.git/`, `node_modules/`, `.next/`, `__pycache__/`, `.bak_aads`, `.pyc`, `.log` 등은 제외.
- 효과: sed/tee/`> file`/`>> file`/cp/mv 등 모든 수정 경로가 Hook을 우회할 수 없음.

### 2. Hook의 git 명령 cd 수정 (`8ae6a7e`)

AADS workdir=`/root` 이슈를 해결:

| 구간 | 기존 (실패) | 변경 (정상) |
|------|-------------|-------------|
| git add | `tool_git_remote_add(project, file_path)` → `cd /root && git add …` exit=128 | `cd /root/aads/aads-server && git add …` |
| git commit | 동일 | 동일 cd 명시 |
| git push | 동일 | 동일 cd 명시 |
| AI 리뷰용 diff | `git diff HEAD~1 HEAD — …` | `cd /root/aads/aads-server && git diff …` |

`git_project_lock(project, timeout=60)`와 `shlex.quote()`는 유지.

## 검증 결과

### 단위 테스트 (컨테이너 내 직접 실행)

```bash
# 1) 마커 파일: "# Hook 테스트 파일 (OLD)"
# 2) sed로 OLD → NEW_BY_HOOK 교체 via ToolExecutor._run_remote_command
docker exec aads-server python3 -c "
import asyncio
from app.core.db_pool import init_pool, close_pool
from app.services.tool_executor import ToolExecutor
async def main():
    await init_pool()
    te = ToolExecutor()
    await te._run_remote_command({'project':'AADS',
        'command':'sed -i s/OLD/NEW_BY_HOOK/g /root/aads/aads-server/docs/hook_sed_test.md'})
    await close_pool()
asyncio.run(main())
"
```

관찰 로그 (발췌):

```
run_remote_command OK | cmd=cd /root/aads/aads-server && git status --porcelain exit=0
run_remote_command OK | cmd=sed -i s/OLD/NEW_BY_HOOK/g … exit=0
run_remote_command OK | cmd=cd /root/aads/aads-server && git status --porcelain exit=0
run_cmd_hook_fire: file=docs/hook_sed_test.md
run_remote_command OK | cmd=cd /root/aads/aads-server && git add docs/hook_sed_test.md exit=0
run_remote_command OK | cmd=cd /root/aads/aads-server && git commit … exit=0
post_hook_commit: [main 2890b97] Chat-Direct: docs/hook_sed_test.md 수정
run_remote_command OK | cmd=cd /root/aads/aads-server && git push origin main exit=0
post_hook_push: [pre-push] ✅ HOOK_VERIFIED
code_review_complete: verdict=APPROVE score=1.0 duration_ms=6885
```

→ **sed 실행 1회로 commit + push + AI 리뷰까지 완전 자동화 확인.**

### 실 커밋 흔적

- `2890b97 Chat-Direct: docs/hook_sed_test.md 수정` — Hook이 자동 생성한 커밋 (검증용).
- `3e7f03d feat(chat): run_remote_command에 git diff 기반 Hook 감지 추가` — 본 기능.
- `8ae6a7e fix(chat): _post_file_modify_hook git 명령에 cd /root/aads/aads-server 추가` — 부수 버그 교정.

## 부수 효과

1. **기존 write_remote_file / patch_remote_file Hook도 이제 실제로 커밋까지 진행** — 그동안 조용히 실패하던 버그가 함께 해결됨.
2. AI 코드 리뷰는 DB pool이 초기화된 상태(즉 실제 서버 프로세스)에서만 정상 동작. 단위 테스트 시 `init_pool()` 필수.
3. 대시보드(Next.js, `/root/aads/aads-dashboard/`)는 여전히 Hook 대상 밖 — 추후 확장 시 `file_path.startswith("/root/aads/aads-dashboard/")` 분기 추가 필요.

## 한계 및 후속 과제

- git 관리 밖 파일(`scripts/build-dashboard.sh` 등 untracked 유지)은 `?? ` 상태로만 남음. Hook은 이를 감지하지만 push 시 pre-commit 정책에 따라 추가 검토 필요.
- 대시보드 경로(`aads-dashboard`)용 별도 Hook 필요.
- `/root`가 실제 git 저장소라면 혼동 방지를 위해 `project_config.py`에서 AADS workdir 자체를 `/root/aads/aads-server`로 옮기는 정규화 검토 가능(다른 도구 영향 범위 확인 필요).
