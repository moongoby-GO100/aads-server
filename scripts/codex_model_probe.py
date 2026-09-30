#!/usr/bin/env python3
"""codex CLI 신규 모델 실호출 검증 (AADS-CLI-MODEL-AUTOREG-20260930). 호스트 전용.

발견은 컨테이너 model_registry sync 가 한다(llm_model_candidates status='discovered').
여기서는 두 가지만 한다.

1. **카탈로그 사본.** 컨테이너에는 /root/.codex 가 마운트돼 있지 않다(2026-09-30 docker
   inspect 실측). codex CLI 캐시에서 identity/etag 를 뺀 사본을 app/data/codex_cli/ 에
   둔다 — 컨테이너에서는 /app/app/data/codex_cli/models_cache.json 으로 보인다.
2. **실호출 검증.** status in (discovered, blocked_account, probe_failed) 인 codex 후보마다
   `timeout 120 codex exec --skip-git-repo-check -m <id> "Reply with exactly: OK"` 1회.
       성공                                   → verified (+ llm_models codex 행 is_executable=true,
                                                runner_llm 비활성 후보 + 알림 1건)
       "not supported when using Codex with a ChatGPT account" → blocked_account (notes 에 원문 1줄)
       그 밖                                  → probe_failed
   모델당 24시간에 1회(last_probe_at). verified 는 다시 치지 않는다.

목록에 있다고 실행 가능한 것이 아니다 — 2026-09-30 gpt-6.1-sol 은 visibility=list 였지만
ChatGPT 계정 codex 로는 400 이었다. 그래서 발견과 판정을 분리했다.

R-BG: 전체 상한(--total-timeout), 모델별 timeout 120, 프로세스 그룹 회수, 단일 실행 락.

    python3 scripts/codex_model_probe.py              # 사본 갱신 + 대상 검증
    python3 scripts/codex_model_probe.py --dry-run    # DB 변경 없이 대상만 출력
    python3 scripts/codex_model_probe.py --mirror-only
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

ENV_FILE = "/root/aads/aads-server/.env"
CODEX_CACHE = os.getenv("AADS_CODEX_MODELS_CACHE", "/root/.codex/models_cache.json")
MIRROR_PATH = os.getenv(
    "AADS_CODEX_MODELS_CACHE_MIRROR_HOST",
    "/root/aads/aads-server/app/data/codex_cli/models_cache.json",
)
LOCK_PATH = os.getenv("CODEX_MODEL_PROBE_LOCK", "/tmp/aads-codex-model-probe.lock")
CODEX_BIN = os.getenv("CODEX_BIN", "codex")
# 카탈로그(models_cache.json)를 받은 계정과 같은 계정으로 친다. blocked_account 는 계정별 판정이다.
# 러너 셸은 CODEX_HOME 이 다른 계정 홈을 가리킬 수 있다(2026-09-30 실측: 무효 토큰 계정 → 401).
PROBE_CODEX_HOME = os.getenv("CODEX_PROBE_HOME", str(Path(CODEX_CACHE).parent))

PROBE_PROMPT = "Reply with exactly: OK"
PER_MODEL_TIMEOUT_SECONDS = 120
RETRY_INTERVAL = timedelta(hours=24)
PROBE_STATUSES = ("discovered", "blocked_account", "probe_failed")
BLOCKED_ACCOUNT_MARKER = "not supported when using codex with a chatgpt account"
# 인증 실패는 모델 판정이 아니다 — 기록하지 않고(24h 상한 소모 안 함) 이번 회차를 멈춘다.
AUTH_ERROR_MARKERS = ("401 unauthorized", "invalidated oauth token", "refresh token", "not logged in")
RUNNER_DISPLAY_ORDER = 900
VERIFIED_ALERT_TITLE = "신규 CLI 모델 실행 확인 — 설정에서 활성화 가능"
_MIRROR_MODEL_KEYS = ("slug", "display_name", "visibility", "supported_in_api", "context_window", "priority")


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 판정 (순수 함수 — tests/unit/test_cli_model_autoreg.py)
# ---------------------------------------------------------------------------

def _first_line(text: str, needle: str = "") -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not needle or needle in stripped.lower():
            return stripped[:300]
    return ""


def classify_probe(returncode: Optional[int], stdout: str, stderr: str, *, timed_out: bool = False) -> tuple[str, str]:
    """(status, note). note 는 한 줄."""
    combined = f"{stdout or ''}\n{stderr or ''}"
    if BLOCKED_ACCOUNT_MARKER in combined.lower():
        return "blocked_account", _first_line(combined, BLOCKED_ACCOUNT_MARKER)
    if timed_out or returncode == 124:
        return "probe_failed", f"timeout {PER_MODEL_TIMEOUT_SECONDS}s"
    if returncode == 0 and any(line.strip().strip(".").upper() == "OK" for line in (stdout or "").splitlines()):
        return "verified", "codex exec 응답 OK"
    lowered = combined.lower()
    for marker in AUTH_ERROR_MARKERS:
        if marker in lowered:
            return "auth_error", _first_line(combined, marker)
    detail = _first_line(stderr, "error") or _first_line(stdout, "error") or _first_line(stderr) or _first_line(stdout)
    return "probe_failed", f"rc={returncode} {detail}".strip()[:300]


def is_due(row: Mapping[str, Any], now: datetime) -> bool:
    if row.get("status") not in PROBE_STATUSES:
        return False
    last = row.get("last_probe_at")
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return now - last >= RETRY_INTERVAL


def build_mirror(payload: Mapping[str, Any], source_path: str) -> dict[str, Any]:
    """identity/etag 등 계정 정보는 빼고 모델 목록만 옮긴다."""
    models = []
    for item in payload.get("models") or []:
        if isinstance(item, Mapping) and item.get("slug"):
            models.append({key: item.get(key) for key in _MIRROR_MODEL_KEYS if key in item})
    return {
        "fetched_at": payload.get("fetched_at"),
        "client_version": payload.get("client_version"),
        "mirrored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mirrored_from": source_path,
        "models": models,
    }


# ---------------------------------------------------------------------------
# 카탈로그 사본
# ---------------------------------------------------------------------------

def write_mirror(source: str = CODEX_CACHE, target: str = MIRROR_PATH) -> dict[str, Any]:
    try:
        with open(source, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError) as exc:
        return {"status": "unavailable", "error": f"{type(exc).__name__}:{source}"}
    if not isinstance(payload, Mapping) or not isinstance(payload.get("models"), list):
        return {"status": "unavailable", "error": f"invalid_format:{source}"}
    mirror = build_mirror(payload, source)
    target_path = Path(target)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".models_cache.", dir=str(target_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(mirror, fh, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o644)
        os.replace(tmp, target_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    listed = sum(1 for m in mirror["models"] if str(m.get("visibility") or "").lower() == "list")
    return {"status": "ok", "models": len(mirror["models"]), "listed": listed, "fetched_at": mirror["fetched_at"]}


# ---------------------------------------------------------------------------
# 실호출
# ---------------------------------------------------------------------------

def run_codex_probe(model_id: str, *, deadline: float) -> tuple[str, str]:
    remaining = deadline - time.monotonic()
    if remaining < 10:
        return "skipped", "total timeout reached"
    wait = min(PER_MODEL_TIMEOUT_SECONDS + 10, remaining)
    cmd = ["timeout", str(PER_MODEL_TIMEOUT_SECONDS), CODEX_BIN, "exec", "--skip-git-repo-check", "-m", model_id, PROBE_PROMPT]
    env = dict(os.environ, CODEX_HOME=PROBE_CODEX_HOME)
    with tempfile.TemporaryDirectory(prefix="codex-probe-") as workdir:
        proc = subprocess.Popen(
            cmd,
            cwd=workdir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,  # 자식(codex 하위 프로세스)까지 그룹으로 회수
        )
        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=wait)
        except subprocess.TimeoutExpired:
            timed_out = True
            _reap_group(proc)
            stdout, stderr = proc.communicate()
        finally:
            if proc.poll() is None:
                _reap_group(proc)
    return classify_probe(proc.returncode, stdout, stderr, timed_out=timed_out)


def _reap_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def _load_db_env() -> dict[str, str]:
    env: dict[str, str] = {}
    with open(ENV_FILE, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.startswith("AADS_DB_"):
                env[key] = value.strip().strip('"').strip("'")
    return env


def connect_db():
    import psycopg2  # 호스트 실행 전제

    env = _load_db_env()
    return psycopg2.connect(
        host=os.getenv("PROBE_DB_HOST", "127.0.0.1"),
        port=int(os.getenv("PROBE_DB_PORT", "5433")),
        dbname=env.get("AADS_DB_NAME", "aads"),
        user=env.get("AADS_DB_USER", "aads"),
        password=env.get("AADS_DB_PASSWORD", ""),
        connect_timeout=10,
    )


class ProbeStore:
    """llm_model_candidates / llm_models / model_routing_preferences / alert_history 접점."""

    def __init__(self, conn):
        self.conn = conn

    def candidates(self) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT model_id, status, last_probe_at FROM llm_model_candidates "
                "WHERE provider = 'codex' AND status = ANY(%s) ORDER BY id",
                (list(PROBE_STATUSES),),
            )
            return [{"model_id": r[0], "status": r[1], "last_probe_at": r[2]} for r in cur.fetchall()]

    def record(self, model_id: str, status: str, note: str) -> dict[str, Any]:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        out: dict[str, Any] = {"runner_candidate_inserted": False}
        with self.conn, self.conn.cursor() as cur:
            cur.execute(
                "UPDATE llm_model_candidates SET status = %s, notes = %s, last_probe_at = NOW() "
                "WHERE provider = 'codex' AND model_id = %s",
                (status, f"[probe {stamp}] {status}: {note}"[:1000], model_id),
            )
            if status != "verified":
                return out
            cur.execute(
                "UPDATE llm_models SET is_executable = TRUE, is_selectable = TRUE, "
                "       verification_status = 'verified', last_verified_at = NOW(), updated_at = NOW() "
                "WHERE provider = 'codex' AND model_id = %s",
                (model_id,),
            )
            cur.execute(
                "INSERT INTO model_routing_preferences "
                "  (route_key, provider, model_id, display_order, is_enabled, is_default, notes, updated_by) "
                "VALUES ('runner_llm', 'codex', %s, %s, FALSE, FALSE, %s, 'auto_discovery') "
                "ON CONFLICT (route_key, provider, model_id) DO NOTHING RETURNING model_id",
                (model_id, RUNNER_DISPLAY_ORDER, "자동 발견 후보 — 설정 UI 에서 활성화해야 러너가 사용"),
            )
            inserted = cur.fetchone() is not None
            out["runner_candidate_inserted"] = inserted
            if inserted:
                cur.execute(
                    "INSERT INTO alert_history (severity, category, title, message, project, server, acknowledged) "
                    "VALUES ('info', 'llm_model', %s, %s, 'AADS', NULL, false)",
                    (
                        VERIFIED_ALERT_TITLE,
                        f"codex CLI 실호출 검증 통과: {model_id} — runner_llm 에 비활성 후보로 등록됨. "
                        "설정 > 모델 라우팅에서 켜면 러너가 사용한다.",
                    ),
                )
        return out


def probe_candidates(
    store,
    *,
    probe=run_codex_probe,
    now: Optional[datetime] = None,
    deadline: Optional[float] = None,
    max_models: int = 5,
    dry_run: bool = False,
    only: Iterable[str] = (),
) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    deadline = deadline if deadline is not None else time.monotonic() + 900
    only_set = set(only)
    due = [
        row for row in store.candidates()
        if (not only_set or row["model_id"] in only_set) and (only_set or is_due(row, now))
    ]
    results: list[dict[str, Any]] = []
    for row in due[:max(0, max_models)]:
        model_id = row["model_id"]
        if dry_run:
            results.append({"model_id": model_id, "status": "due", "previous": row["status"]})
            continue
        status, note = probe(model_id, deadline=deadline)
        if status in {"skipped", "auth_error"}:
            # 시간 초과·인증 실패는 모델 판정이 아니다. 기록하지 않고 다음 회차에 다시 본다.
            results.append({"model_id": model_id, "status": status, "note": note})
            break
        extra = store.record(model_id, status, note)
        results.append({"model_id": model_id, "status": status, "previous": row["status"], "note": note, **extra})
    return results


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--mirror-only", action="store_true")
    parser.add_argument("--model", action="append", default=[], help="지정 모델만(24h 상한 무시)")
    parser.add_argument("--max-models", type=int, default=int(os.getenv("CODEX_PROBE_MAX_MODELS", "5")))
    parser.add_argument("--total-timeout", type=int, default=int(os.getenv("CODEX_PROBE_TOTAL_TIMEOUT", "900")))
    args = parser.parse_args(argv)
    deadline = time.monotonic() + max(30, args.total_timeout)

    lock_fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("다른 probe 가 실행 중 — 건너뜀")
        return 0

    mirror = write_mirror() if not args.dry_run else {"status": "skipped_dry_run"}
    log(f"mirror {json.dumps(mirror, ensure_ascii=False)}")
    if args.mirror_only:
        return 0 if mirror.get("status") in {"ok", "skipped_dry_run"} else 1

    conn = connect_db()
    try:
        results = probe_candidates(
            ProbeStore(conn),
            deadline=deadline,
            max_models=args.max_models,
            dry_run=args.dry_run,
            only=args.model,
        )
    finally:
        conn.close()
    for item in results:
        log(f"probe {json.dumps(item, ensure_ascii=False, default=str)}")
    if not results:
        log("probe 대상 없음")
    return 0


if __name__ == "__main__":
    sys.exit(main())
