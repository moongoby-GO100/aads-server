# 2026-04-17 Codex HOME config.toml 자동 갱신 검증 리포트

## 1) 코드 변경

- 대상 파일: `scripts/claude_relay_server.py`
- `_build_codex_home(session_id)`에 `config.toml` 내용 해시 비교 로직 추가
  - 기존 내용과 신규 내용의 `sha256` 비교
  - 변경 시에만 덮어쓰기 + INFO 로그
    - `logger.info(f"[codex] config.toml refreshed for session={session_id or 'default'}")`
  - 변경 없으면 DEBUG 로그
    - `logger.debug(f"[codex] config.toml unchanged for session={session_id or 'default'}")`
- `auth.json` 심볼릭 링크 로직은 기존 방식 유지
  - 기존 링크/파일 `unlink()` 후 `symlink_to()` 재생성

## 2) 필수 항목 점검

- `AADS_SESSION_ID` 기본값 처리
  - `_load_mcp_template()`에서 `safe_sid = session_id or "default"` 적용 유지
  - `handle_codex_stream()`에서 `proc_env["AADS_SESSION_ID"] = session_id or "default"` 적용 유지

## 3) 검증 결과

### 3-1. config.toml 파싱/필드 확인

- 로컬 환경 Python 3.6 기준 `tomllib` 미지원으로 `tomli`로 동일 TOML 파싱 검증 수행
- 세션: `833a7bb4-d42a-46ad-ba38-2ba8a2b1c24a`
- 확인 결과:
  - `approval_policy = "never"`
  - `sandbox_mode = "workspace-write"`
  - `startup_timeout_sec = 30`
  - `tool_timeout_sec = 120`
  - `[projects."/root/aads/aads-server"]` 존재

생성된 예시 config:

```toml
approval_policy = "never"
sandbox_mode = "workspace-write"
model_reasoning_effort = "high"

[projects."/root/aads/aads-server"]
trust_level = "trusted"

[mcp_servers.aads-tools]
command = "docker"
args = ["exec", "-i", "-e", "AADS_SESSION_ID=833a7bb4-d42a-46ad-ba38-2ba8a2b1c24a", "aads-server", "python", "-m", "mcp_servers.aads_tools_bridge"]
startup_timeout_sec = 30
tool_timeout_sec = 120
```

### 3-2. idempotent 확인

- 동일 세션으로 `_build_codex_home()` 2회 연속 호출 시 `config.toml` `mtime` 동일
  - 결과: `mtime_same=True`
  - 즉, 변경 없을 때 파일 재기록되지 않음

### 3-3. 로그/런타임 검증 항목

- `journalctl` 로그 확인, relay 재시작, `run_remote_command(AADS, "date")` 실측 호출은 본 작업 범위에서 수행하지 않음.

## 4) 백업 파일 확인

- 요청된 `scripts/pipeline-runner.sh.bak.litellm` 파일은 작업 트리에서 확인되지 않음.
- 유사 파일 `scripts/pipeline-runner.sh.bak_litellm_fix`는 **tracked 파일**이라 삭제하지 않음.
