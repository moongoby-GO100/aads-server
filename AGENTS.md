# AADS Server Agent Rules

Read and obey `/root/aads/AGENTS.md` before changing or deploying this repository.

For every API release, `deploy.sh bluegreen` is the default and must enforce: one image build per release SHA, `--no-build` slot starts, candidate health before the nginx lock, DB-fenced execution ownership, same-digest standby synchronization, rollback on routed-health failure, and five-minute P0/P1 monitoring before completion is reported.

Never overwrite unrelated dirty files, restart the active API directly, deploy the full compose stack for an app change, or use `git commit --no-verify`.

Target-slot drain policy (CEO directive 2026-10-06): `deploy.sh` waits up to `AADS_DEPLOY_TARGET_DRAIN_MAX_WAIT` (default 180s) for the candidate slot's active streams, then proceeds even if some remain (`AADS_DEPLOY_DRAIN_TIMEOUT_ACTION=proceed`, the default). The remaining count and session ids are logged, written to the control audit as `target-drain`/`proceeded_busy`, and kept in `deploy_runs.error_summary`. Blocking on a busy slot is opt-in via `AADS_DEPLOY_DRAIN_TIMEOUT_ACTION=block`; `AADS_DEPLOY_ALLOW_BUSY_TARGET=true` still skips the wait entirely. Health, image-verification and rollback gates are unchanged.

## 핸드오버 기록 (R-001, CEO 승인 2026-10-08 개정)

- 핸드오버는 DB(`handover_write` 또는 `POST /api/v1/handovers`)에만 기록한다. 정본은 `project_handover_entries` 다.
- `HANDOVER.md` 파일을 수정하지 않는다. 파일은 읽기 호환용으로만 남긴다 — 삭제하지 않는다.
- 러너는 `HANDOVER.md`, `*/HANDOVER.md`, `docs/HANDOVER.md` 변경을 커밋 직전에 스테이징에서 제외한다(워크트리 파일은 보존, `handover_md_excluded` 이벤트 기록). 예외는 `RUNNER_ALLOW_HANDOVER_MD=1` 뿐이다.
- 러너는 작업이 `awaiting_approval`/`done` 이 될 때 `entry_key=runner:<job_id>` 로 결과를 DB 에 upsert 하고 다시 읽어 확인한다. 실패하면 `handover_db_write_failed` 이벤트를 남기고 작업 결과에 "핸드오버 DB 미기록"을 표시한다(작업 자체는 실패시키지 않는다).
- 이유: 2026-10-08 origin/main 커밋 10건 중 6건이 `HANDOVER.md` 를 수정해 병행 러너가 stale_base 를 반복했다.

### 원격 러너 핸드오버 쓰기 키 (`x-handover-key`)

원격 러너(contabo14 등)에 `AADS_MONITOR_KEY`(관리자 권한)를 복사하지 않는다. 대신 **`POST /api/v1/handovers` 하나만** 허용하는 쓰기 전용 키를 쓴다.

- 서버: `AADS_HANDOVER_WRITE_KEY_SHA256` — 키 원문이 아니라 SHA-256 hex. 쉼표로 여러 개(키 교체 기간에 둘 다 받는다). 비어 있거나 64자리 hex 가 아니면 기능은 꺼지고 모든 요청이 401 이다.
- 러너: `AADS_HANDOVER_WRITE_KEY` — 원문. `x-handover-key` 헤더로 보낸다. 없으면 `AADS_MONITOR_KEY`(`x-monitor-key`), 둘 다 없으면 `http_fallback_unconfigured`.
- 범위: (POST, `/api/v1/handovers`) 정확히 하나(`app/auth.py` `is_handover_write_request`). `/handovers/import`, GET 조회, 그 밖의 라우트는 401. 주체는 `system:handover-writer`(is_admin=False, internal 테넌트 member).
- 발급(CEO 승인 후, 시크릿 변경이므로 건별 승인):
  1. `python3 -c "import secrets;print(secrets.token_urlsafe(32))"` 로 원문을 만든다.
  2. `printf '%s' '<원문>' | sha256sum` 의 hex 를 AADS 서버 `.env` 의 `AADS_HANDOVER_WRITE_KEY_SHA256` 에 넣고 aads-server 를 배포한다.
  3. 원문을 contabo14 `/root/.config/aads-runner.env` 의 `AADS_HANDOVER_WRITE_KEY` 에 넣는다. 원문은 채팅·로그·커밋에 남기지 않는다.
  4. 교체: 새 해시를 쉼표로 추가 → 러너 키 교체 → 옛 해시 제거.

## 원격 러너 반영 (R-RELEASE, 2026-10-20)

- 원격 러너 스크립트 반영은 `scripts/sync_pipeline_runner_remote.sh` 가 한다. 고부하 호스트는 `systemctl restart` 대신 `--target X --drain-wait N` 으로 반영한다(N=60~3600초, 신규 claim 만 멈추고 실행 중 작업이 끝나면 교체, 타임아웃이면 변경 없이 claim 재개 → 요약 `<host>=drain_timeout`). 타이머 경로는 이 플래그를 쓰지 않는다.
