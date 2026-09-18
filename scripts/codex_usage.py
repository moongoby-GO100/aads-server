#!/usr/bin/env python3
"""코덱스 계정별 사용량 집계 · 한도 상태 동기화.

2026-09-16 대표님 지시: "현재 사용량 확인 할수 있는 방법이 없는데 어떻게 해야할지 보고해"
설계: aads-docs/docs/PRD-LLM-ACCOUNT-RUNTIME-BINDING-v1.0.md

## 왜 파일을 긁나

ChatGPT 구독 인증이라 API 청구 대시보드가 없고, codex CLI 0.154.0 에는
usage/status 서브커맨드가 없다(doctor 는 설치 진단 전용). 한도 정보가 남는
곳은 rollout 파일의 token_count 이벤트 하나뿐이다.

    rate_limits.primary = {used_percent, window_minutes, resets_at}

## 무엇을 하는가

1. **집계** — 릴레이 세션 rollout 을 훑어 계정별 최신 스냅샷을 찾는다.
   세션이 어느 계정으로 돌았는지는 세션 홈의 account.json 이 알려준다
   (릴레이 _pick_codex_account 가 기록). 없으면 옛 방식이므로 MAIN 으로 본다.
2. **한도 반영** — "usage limit ... try again at <시각>" 을 만나면 그 계정의
   rate_limited_until 을 DB 에 적는다. 그러면 릴레이가 다음 계정을 고른다.
   클로드 쪽이 이미 쓰는 규약과 같다(restore_claude_slot1.sh 참고).
3. **상태 배포** — DB 의 priority/is_active/rate_limited_until 을
   /root/.codex-accounts/state.json 으로 내린다. 릴레이는 이 파일만 읽는다.
   조회가 실패해도 인증이 끊기면 안 되기 때문이다.

읽기만 하는 기본 동작은 언제 돌려도 안전하다. --sync 를 줘야 DB/state 를 쓴다.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ACCOUNTS_ROOT = Path(os.getenv("CODEX_ACCOUNTS_ROOT", "/root/.codex-accounts"))
RELAY_ROOT = Path(os.getenv("CODEX_HOME_ROOT", "/root/.codex-relay"))
LEGACY_HOME = Path("/root/.codex")
STATE_FILE = ACCOUNTS_ROOT / "state.json"
MAIN_KEY_NAME = "CODEX_OAUTH_MAIN"
KST = timezone(timedelta(hours=9))
PSQL = ["/usr/bin/docker", "exec", "aads-postgres", "psql", "-U", "aads", "-d", "aads", "-tAc"]

# "try again at Sep 19th, 2026 5:13 PM" — 계정 표시 시각은 KST 다(실측 대조 완료:
# resets_at 1789805594 == 2026-09-19 17:13:14 KST == 메시지의 5:13 PM).
_RETRY_RE = re.compile(r"try again at ([A-Za-z]{3} \d{1,2}\w{2}, \d{4} \d{1,2}:\d{2} [AP]M)")
_LIMIT_RE = re.compile(r"usage limit", re.I)


def psql(sql: str) -> list[list[str]]:
    out = subprocess.run(PSQL + [sql], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"psql 실패: {out.stderr.strip()[:200]}")
    return [line.split("|") for line in out.stdout.strip().splitlines() if line]


def _session_account(session_home: Path) -> tuple[str, float]:
    """세션이 어느 계정으로 돌았는지와 그 계정이 배정된 시각.

    배정 시각이 필요한 이유 — 세션은 계정보다 오래 산다. 한도에 걸린 계정에서
    다른 계정으로 갈아탄 세션의 홈에는 **이전 계정 시절의 rollout 이 그대로
    남아** 있다. 2026-09-16 실측: 한 세션의 rollout 109건 중 107건이 배정 이전
    것이었고, 그 안의 MAIN 한도 실패가 새로 배정된 JINAH 에 귀속돼 멀쩡한
    계정이 '한도정지' 로 꺼졌다. 배정 시각 이전 기록은 이전 계정 몫으로 돌린다.

    표식이 없으면 계정 홈 도입 전이므로 전부 MAIN 이다.
    """
    try:
        marker = json.loads((session_home / ".codex" / "account.json").read_text())
        return marker["key_name"], float(marker.get("bound_at") or 0)
    except (OSError, ValueError, KeyError, TypeError):
        return MAIN_KEY_NAME, 0.0


def _scan_rollout(path: Path) -> dict:
    """rollout 1건에서 필요한 것만 뽑는다. 파일이 커서 뒤에서부터 본다."""
    found = {"snapshot": None, "ts": None, "tokens": 0, "limit_at": None, "ok": False}
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except OSError:
        return found
    for line in reversed(lines):
        if '"task_complete"' in line and found["limit_at"] is None and not found["ok"]:
            try:
                err = (json.loads(line)["payload"].get("error") or {}).get("message") or ""
            except (ValueError, KeyError):
                err = ""
            if _LIMIT_RE.search(err):
                m = _RETRY_RE.search(err)
                found["limit_at"] = m.group(1) if m else ""
            elif not err:
                found["ok"] = True
        if '"rate_limits"' not in line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        payload = obj.get("payload", {})
        info = payload.get("info") or {}
        if info:
            found["tokens"] += (info.get("last_token_usage") or {}).get("total_tokens", 0)
        rl = payload.get("rate_limits") or {}
        if found["snapshot"] is None and rl.get("primary"):
            found["snapshot"] = rl
            found["ts"] = obj.get("timestamp")
    return found


def collect(max_files_per_home: int = 40) -> dict:
    """계정별 최신 스냅샷·최근 실패를 모은다."""
    acc: dict[str, dict] = {}

    def slot(key_name):
        return acc.setdefault(key_name, {
            "snapshot": None, "ts": None, "tokens": 0,
            "ok": 0, "limit": 0, "limit_at": None, "sessions": 0,
            "last_used": 0.0,
        })

    def mark_used(key_name, when):
        rec = slot(key_name)
        if when and when > rec["last_used"]:
            rec["last_used"] = float(when)

    homes = [(h, *_session_account(h)) for h in RELAY_ROOT.glob("*") if h.is_dir()]
    homes.append((LEGACY_HOME.parent, MAIN_KEY_NAME, 0.0))  # /root/.codex 자체

    cutoff = time.time() - 72 * 3600
    for home, key_name, bound_at in homes:
        # 배정 자체가 사용의 하한이다. 막 배정된 세션은 아직 rollout 이 없다.
        mark_used(key_name, bound_at)
        codex_dir = home / ".codex" if (home / ".codex").is_dir() else home
        files = sorted(codex_dir.glob("sessions/*/*/*/rollout-*.jsonl"),
                       key=os.path.getmtime, reverse=True)
        if not files:
            continue
        for f in files:
            # 배정 이전 기록은 이전 계정(=MAIN) 몫이다. _session_account 주석 참고.
            # 세션 수도 같은 기준으로 센다 — 한쪽만 다른 기준이면 표가 어긋난다.
            mtime = os.path.getmtime(f)
            owner = key_name if mtime >= bound_at else MAIN_KEY_NAME
            slot(owner)["sessions"] += 1
            mark_used(owner, mtime)
        for f in files[:max_files_per_home]:
            rec = slot(key_name if os.path.getmtime(f) >= bound_at else MAIN_KEY_NAME)
            got = _scan_rollout(f)
            rec["tokens"] += got["tokens"]
            if os.path.getmtime(f) >= cutoff:
                if got["limit_at"] is not None:
                    rec["limit"] += 1
                    rec["limit_at"] = rec["limit_at"] or got["limit_at"]
                elif got["ok"]:
                    rec["ok"] += 1
            # 홈 순회 순서는 최신순이 아니다. 반드시 타임스탬프로 비교해
            # 가장 최근 스냅샷을 남긴다 — 옛 값을 잡으면 사용률을 과소보고한다.
            if got["snapshot"] and (rec["ts"] is None or (got["ts"] or "") > rec["ts"]):
                rec["snapshot"] = got["snapshot"]
                rec["ts"] = got["ts"]
    return acc


def live_rate_limits(account_home: Path, timeout: int = 25) -> dict | None:
    """codex app-server JSON-RPC 로 계정의 현재 한도를 직접 묻는다.

    rollout 파일 수집만으로는 값이 낡는다 — 한도에 걸린 호출은 사용률을 갱신해
    주지 않아 실측 50~121시간 전 값이 남는다. 이 경로는 항상 지금 값이고
    토큰도 쓰지 않는다. 릴레이의 _query_codex_rate_limits() 와 같은 방식이며,
    다른 점은 CODEX_HOME 을 계정 홈으로 지정해 **계정별로** 묻는다는 것이다.

    CLI 업데이트로 스키마가 바뀌어도 죽지 않게 방어적으로 읽고, 실패하면
    None 을 돌려 호출부가 rollout 수집으로 되돌아가게 한다.
    """
    import select

    if not (account_home / "auth.json").exists():
        return None
    env = dict(os.environ, CODEX_HOME=str(account_home))
    try:
        proc = subprocess.Popen(
            [os.getenv("CODEX_BIN", "codex"), "app-server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, env=env,
        )
    except OSError:
        return None

    def send(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    deadline = time.time() + timeout
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "aads", "title": "AADS", "version": "1.0"},
                         "capabilities": {}}})
        stage = "init"
        while time.time() < deadline:
            if not select.select([proc.stdout], [], [], 1)[0]:
                continue
            line = proc.stdout.readline()
            if not line:
                return None
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if stage == "init" and msg.get("id") == 1:
                send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                send({"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}})
                stage = "ask"
                continue
            if stage == "ask" and msg.get("id") == 2:
                rl = (msg.get("result") or {}).get("rateLimits") or {}
                pri = rl.get("primary") or {}
                if not pri:
                    return None
                # 이름이 camelCase 다. rollout 쪽(snake_case)과 모양을 맞춰 돌려준다.
                return {
                    "primary": {
                        "used_percent": pri.get("usedPercent"),
                        "window_minutes": pri.get("windowDurationMins"),
                        "resets_at": pri.get("resetsAt"),
                    },
                    "plan_type": rl.get("planType"),
                    "rate_limit_reached": rl.get("rateLimitReachedType") is not None,
                }
        return None
    except (OSError, ValueError):
        return None
    finally:
        try:
            proc.kill()
        except OSError:
            pass


def auth_usable(path: Path) -> bool:
    """auth.json 이 '있다'가 아니라 '지금 쓸 수 있다'를 판정한다.

    2026-09-19: CODEX_OAUTH_JINAH 의 access_token 이 04:41 KST 에 만료됐는데
    has_auth 는 파일 존재만 봐서 true 였다. 러너(codex_pick_account_home)가
    그 계정을 1순위로 골라 매 시도를 401 로 태웠다. 만료분은 false 로 준다.
    """
    try:
        payload = json.loads(path.read_text())
        token = (payload.get("tokens") or {}).get("access_token") or ""
        chunk = token.split(".")[1]
        chunk += "=" * (-len(chunk) % 4)
        exp = json.loads(base64.urlsafe_b64decode(chunk)).get("exp", 0)
    except Exception:  # noqa: BLE001 — 파일 없음/형식 변경 모두 '못 쓴다'로 본다
        return False
    return float(exp) > time.time() + 60


def db_accounts() -> list[dict]:
    rows = psql(
        "SELECT key_name, COALESCE(label,''), priority, is_active, "
        "COALESCE(EXTRACT(EPOCH FROM rate_limited_until)::bigint::text,'') "
        "FROM llm_api_keys WHERE provider='codex' ORDER BY priority ASC, id ASC"
    )
    return [{
        "key_name": r[0], "label": r[1], "priority": int(r[2]),
        "is_active": r[3] == "t",
        "rate_limited_until_epoch": int(r[4]) if r[4] else None,
        "has_auth": auth_usable(ACCOUNTS_ROOT / r[0] / "auth.json"),
    } for r in rows]


def write_state(accounts: list[dict]) -> None:
    ACCOUNTS_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_name("state.json.tmp")
    tmp.write_text(json.dumps({
        # 사람이 읽는 값이므로 KST 로 적는다 (서버 로컬은 Europe/Berlin).
        "updated_at": datetime.now(KST).isoformat(),
        "accounts": accounts,
    }, ensure_ascii=False, indent=2))
    tmp.replace(STATE_FILE)


def push_rate_limit(key_name: str, retry_text: str) -> str:
    """한도 실패에서 읽은 복귀 시각을 DB 에 적는다."""
    try:
        dt = datetime.strptime(re.sub(r"(\d+)(st|nd|rd|th)", r"\1", retry_text),
                               "%b %d, %Y %I:%M %p")
    except ValueError:
        return "복귀시각 파싱 실패"
    stamp = dt.strftime("%Y-%m-%d %H:%M:%S+09")
    psql("UPDATE llm_api_keys SET rate_limited_until='%s', updated_at=NOW() "
         "WHERE key_name='%s' AND provider='codex' "
         "AND (rate_limited_until IS NULL OR rate_limited_until < '%s')"
         % (stamp, key_name, stamp))
    return stamp


# UPSERT 문은 한 덩어리로 떼어 둔다. 여러 줄 문자열을 이어 붙이면
# dup_guard 의 SQL 스캐너가 뒤따르는 파이썬 코드까지 같은 문장으로 읽어
# 'UPDATE SET 에 같은 컬럼이 두 번' 이라는 오탐을 낸다(2026-09-16).
_SNAPSHOT_UPSERT = """
INSERT INTO codex_usage_snapshots
(key_name, used_percent, window_minutes, resets_at, snapshot_at,
 ok_72h, limit_72h, sessions, tokens_recent, auth_usable, collected_at)
VALUES ('{k}', {used}, {win}, {resets}, {snap_at}, {ok}, {lim}, {sess}, {tok}, {auth}, NOW())
ON CONFLICT (key_name) DO UPDATE SET
used_percent=EXCLUDED.used_percent, window_minutes=EXCLUDED.window_minutes,
resets_at=EXCLUDED.resets_at, snapshot_at=EXCLUDED.snapshot_at,
ok_72h=EXCLUDED.ok_72h, limit_72h=EXCLUDED.limit_72h,
sessions=EXCLUDED.sessions, tokens_recent=EXCLUDED.tokens_recent,
auth_usable=EXCLUDED.auth_usable, collected_at=NOW()
"""


def push_snapshots(accounts: list[dict], usage: dict) -> None:
    """계정별 최신 사용량을 DB 에 올린다 — API 컨테이너는 호스트 파일을 못 본다."""
    for a in accounts:
        u = usage.get(a["key_name"], {})
        snap = (u.get("snapshot") or {}).get("primary") or {}
        used = a.get("used_percent")
        resets = snap.get("resets_at")
        psql(_SNAPSHOT_UPSERT.format(
            k=a["key_name"],
            used="NULL" if used is None else f"{float(used):.1f}",
            win=snap.get("window_minutes") or "NULL",
            resets=f"to_timestamp({int(resets)})" if resets else "NULL",
            snap_at=(f"'{u['ts']}'" if u.get("ts") else "NULL"),
            ok=a.get("ok_72h", 0), lim=a.get("limit_72h", 0),
            sess=a.get("sessions", 0), tok=a.get("tokens_recent", 0),
            # 한도가 남아도 자격증명이 죽었으면 쓸 수 없는 계정이다. API 컨테이너는
            # 호스트의 auth.json 을 못 보므로 이 값이 유일한 판단 근거다
            # (2026-09-19: JINAH 한도 37% 인데 토큰 만료로 호출이 전부 실패했다).
            auth="TRUE" if a.get("has_auth") else "FALSE",
        ))


def push_rate_limit_epoch(key_name: str, resets_at) -> None:
    """실시간 조회가 알려준 복귀 시각을 그대로 적는다."""
    if not resets_at:
        return
    stamp = datetime.fromtimestamp(int(resets_at), KST).strftime("%Y-%m-%d %H:%M:%S%z")
    psql("UPDATE llm_api_keys SET rate_limited_until='%s', updated_at=NOW() "
         "WHERE key_name='%s' AND provider='codex'" % (stamp, key_name))


def push_last_used(accounts: list[dict]) -> None:
    """어느 계정이 실제로 돌았는지를 llm_api_keys.last_used_at 에 남긴다.

    구독 슬롯 화면은 이 컬럼을 본다. 릴레이는 DB 에 붙지 않으므로(조회가 실패해도
    인증이 끊기면 안 된다 — _codex_accounts 주석) 계정 배정을 아는 쪽이 대신 적는다.
    2026-09-19 실측: 릴레이 세션 20개가 전부 JINAH 로 돌고 있는데 두 계정 모두
    last_used_at 이 NULL 이라, 화면만 보고는 어느 계정이 도는지 알 수 없었다.

    되돌아가지 않게 더 최근일 때만 쓴다(여러 서버가 같은 표를 갱신한다).
    """
    for a in accounts:
        used = a.get("last_used_epoch")
        if not used:
            continue
        stamp = datetime.fromtimestamp(int(used), KST).strftime("%Y-%m-%d %H:%M:%S%z")
        psql("UPDATE llm_api_keys SET last_used_at='%s', updated_at=NOW() "
             "WHERE key_name='%s' AND provider='codex' "
             "AND (last_used_at IS NULL OR last_used_at < '%s')"
             % (stamp, a["key_name"], stamp))


def clear_rate_limit(key_name: str) -> None:
    """계정이 지금 멀쩡하면 남은 정지 표시를 지운다."""
    psql("UPDATE llm_api_keys SET rate_limited_until=NULL, updated_at=NOW() "
         "WHERE key_name='%s' AND provider='codex' AND rate_limited_until IS NOT NULL"
         % key_name)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sync", action="store_true", help="DB 와 state.json 을 갱신한다")
    ap.add_argument("--state-only", action="store_true",
                    help="사용량 수집 없이 DB 의 priority/한도만 state.json 으로 내린다")
    ap.add_argument("--json", action="store_true", help="기계 판독용 출력")
    args = ap.parse_args()

    if args.state_only:
        # 주계정 조정기가 2분마다 부른다. collect() 는 CLI 를 돌려 비싸므로
        # 여기서는 DB 값만 내린다 — 릴레이가 보는 것은 이 파일뿐이다.
        accounts = db_accounts()
        write_state(accounts)
        print("state.json 갱신: %d개 계정" % len(accounts))
        return 0

    usage = collect()
    accounts = db_accounts()
    now = time.time()

    for a in accounts:
        u = usage.get(a["key_name"], {})
        # 실시간 조회를 먼저 쓴다. 실패하면 rollout 수집값으로 되돌아간다.
        live = live_rate_limits(ACCOUNTS_ROOT / a["key_name"])
        a["live"] = live
        if live:
            u = dict(u)
            u["snapshot"] = live
            # 끝에 Z 를 붙인다. rollout 타임스탬프와 모양을 맞추고, TIMESTAMPTZ
            # 컬럼에 그대로 넣었을 때 Postgres 가 세션 시간대(+09)로 오해하지
            # 않게 한다 — 붙이지 않으면 DB 값이 9시간 어긋난다.
            u["ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            usage[a["key_name"]] = u
            a["source"] = "live"
        else:
            a["source"] = "rollout"
        snap = (u.get("snapshot") or {}).get("primary") or {}
        a["used_percent"] = snap.get("used_percent")
        a["resets_at"] = snap.get("resets_at")
        a["snapshot_age_h"] = None
        if u.get("ts"):
            try:
                t = datetime.strptime(u["ts"][:19], "%Y-%m-%dT%H:%M:%S")
                a["snapshot_age_h"] = max(0.0, round((now - t.replace(tzinfo=timezone.utc).timestamp()) / 3600, 1))
            except ValueError:
                pass
        a["ok_72h"] = u.get("ok", 0)
        a["limit_72h"] = u.get("limit", 0)
        a["sessions"] = u.get("sessions", 0)
        a["tokens_recent"] = u.get("tokens", 0)
        a["last_used_epoch"] = u.get("last_used") or None

    if args.sync:
        # 한도 상태는 **실시간 조회가 우선**이다. rollout 에서 읽은 실패 기록은
        # 과거이고, 계정 홈이 바뀐 세션에서는 이전 계정 몫이 섞일 수 있다.
        # 계정이 지금 멀쩡하다고 답하면 남아 있던 정지 표시를 지운다 — 이게
        # 없으면 한 번 잘못 찍힌 정지가 스스로 풀리지 않는다(2026-09-16 실측).
        for a in accounts:
            live = a.get("live")
            if live is not None:
                if live.get("rate_limit_reached"):
                    push_rate_limit_epoch(a["key_name"], (live.get("primary") or {}).get("resets_at"))
                else:
                    clear_rate_limit(a["key_name"])
                continue
            u = usage.get(a["key_name"], {})
            if u.get("limit_at"):
                push_rate_limit(a["key_name"], u["limit_at"])
        accounts_db = {r["key_name"]: r for r in db_accounts()}
        for a in accounts:
            fresh = accounts_db.get(a["key_name"])
            if fresh:
                a["rate_limited_until_epoch"] = fresh["rate_limited_until_epoch"]

        # 릴레이는 state.json 만 읽고, API/대시보드는 DB 만 읽는다. 둘 다 여기서 쓴다.
        write_state(accounts)
        push_snapshots(accounts, usage)
        push_last_used(accounts)

    if args.json:
        print(json.dumps(accounts, ensure_ascii=False, indent=2))
        return 0

    print(f"{'계정':<20} {'상태':<8} {'사용률':>6} {'스냅샷':>7} {'복귀':>18} {'72h ok/한도':>11} {'세션':>5}")
    print("-" * 88)
    usable = 0
    for a in accounts:
        limited = a["rate_limited_until_epoch"] and a["rate_limited_until_epoch"] > now
        if not a["has_auth"]:
            status = "자격없음"
        elif not a["is_active"]:
            status = "비활성"
        elif limited:
            status = "한도정지"
        else:
            status = "가용"
            usable += 1
        used = f"{a['used_percent']:.0f}%" if a["used_percent"] is not None else "-"
        age = f"{a['snapshot_age_h']:.0f}h전" if a["snapshot_age_h"] is not None else "-"
        # 보고 기준 시각은 KST 다. 서버 로컬(Europe/Berlin)로 찍으면 대표님이
        # 보는 복귀 시각과 8시간 어긋난다.
        back = (datetime.fromtimestamp(a["rate_limited_until_epoch"], KST).strftime("%m-%d %H:%M KST")
                if a["rate_limited_until_epoch"] else "-")
        print(f"{a['key_name']:<20} {status:<8} {used:>6} {age:>7} {back:>18} "
              f"{str(a['ok_72h']) + '/' + str(a['limit_72h']):>11} {a['sessions']:>5}")
        print(f"{'':<20} {a['label']}")
    print("-" * 88)
    print(f"가용 계정 {usable} / {len(accounts)}"
          + ("  ⛔ 가용 계정이 없다 — 코덱스 호출은 모두 실패한다" if usable == 0 else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
