"""OAuth 계정 재로그인을 화면에서 진행시키기 위한 세션 관리.

2026-09-16 대표님 지시: "화면에 로그인이 필요한건 표시하고 재로그인버튼을 반영해서
재로그인이 가능하게 조치해줘"
설계: aads-docs/docs/PRD-LLM-ACCOUNT-RUNTIME-BINDING-v1.0.md

## 왜 서버에서 CLI 를 띄우나

코덱스·클로드 구독 계정의 자격증명은 **호스트 파일**(auth.json / .credentials.json)에
있고, 그 파일을 만들 수 있는 것은 각 CLI 뿐이다. API 컨테이너에는 그 경로가
마운트돼 있지도 않다. 그래서 호스트에서 도는 릴레이가 CLI 를 pty 로 띄우고,
화면은 그 진행 상태만 받아 보여준다.

## 두 CLI 의 흐름이 다르다

- **codex login --device-auth**: URL 과 일회용 코드를 찍고 스스로 폴링한다.
  대표님이 브라우저에서 코드를 넣으면 CLI 가 알아서 끝낸다. 화면은 보여주기만 한다.
- **claude auth login --claudeai**: authorize URL 을 찍고 **stdin 으로 코드를
  받기를 기다린다.** 그래서 화면에 입력칸이 필요하고, 받은 값을 프로세스에 써 준다.

이 차이를 needs_code 로 구분해 화면에 넘긴다.

## 지키는 것

- pty 로 띄운다. 둘 다 TTY 가 아니면 아무것도 출력하지 않는다(실측).
- **출력에서 토큰을 걸러낸다.** 화면과 로그에는 URL·코드·상태만 나간다 (R-KEY).
- 15분이 지나면 죽인다. 일회용 코드 수명이 15분이라 그 뒤로는 살려둘 이유가 없다.
- 같은 대상에 대해 진행 중인 세션이 있으면 새로 띄우지 않고 그것을 돌려준다.
  버튼 연타로 로그인 프로세스가 쌓이면 어느 것이 파일을 쓸지 알 수 없다.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import pty
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

CODEX_ACCOUNTS_ROOT = Path(os.getenv("CODEX_ACCOUNTS_ROOT", "/root/.codex-accounts"))
CLAUDE_SLOT_ROOT = Path(os.getenv("CLAUDE_RELAY_SLOT_HOME_ROOT", "/root/.claude-relay-slots"))
ANTIGRAVITY_HOME_ROOT = Path(os.getenv("ANTIGRAVITY_HOME_ROOT", "/root/.gemini"))
ANTIGRAVITY_BIN = Path(os.getenv("ANTIGRAVITY_BIN", "/usr/local/bin/agy"))
CODEX_BIN = os.getenv("CODEX_BIN", "codex")
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")

SESSION_TTL_SEC = 15 * 60  # 일회용 코드 수명과 같게 둔다

# 액세스 토큰이 만료돼도 keeper(10분 주기)가 곧 갱신한다. 이 유예를 넘겨서도
# 만료 상태면 갱신이 막힌 것으로 보고 재로그인을 요구한다.
EXPIRY_GRACE_SEC = int(os.getenv("CLAUDE_TOKEN_EXPIRY_GRACE_SEC", "1800"))
KEEPER_LOG = Path(os.getenv("CLAUDE_TOKEN_KEEPER_LOG", "/var/log/aads-claude-token-keeper.log"))
_KEEPER_TAIL_BYTES = 256 * 1024
_KEEPER_SLOT_LINE = re.compile(r"^\S+ \S+ slot(\d+):")
_MAX_OUTPUT = 16384

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_OSC8 = re.compile(r"\x1b\]8;[^\x07\x1b]*(?:\x07|\x1b\\)")
_CODEX_CODE = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4,6})\b")
_URL = re.compile(r"https://[^\s\"'<>]+")
# 화면·로그로 새면 안 되는 것들. 출력에 섞여 나오면 통째로 가린다.
_SECRETISH = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|rt\.[A-Za-z0-9_\-]{8,}|ey[A-Za-z0-9_\-]{20,})")

# {login_id: session dict}
_SESSIONS: dict[str, dict] = {}
_START_LOCK = asyncio.Lock()


class LoginError(RuntimeError):
    pass


def _clean(text: str) -> str:
    text = _OSC8.sub("", _ANSI.sub("", text))
    return _SECRETISH.sub("<가림>", text)


def resolve_target(target: str) -> dict:
    """'codex:CODEX_OAUTH_JINAH' 또는 'claude:3' 를 실행 계획으로 바꾼다."""
    kind, _, name = (target or "").partition(":")
    kind, name = kind.strip().lower(), name.strip()
    if not name:
        raise LoginError("대상이 비었다 (codex:<KEY_NAME> 또는 claude:<slot>)")

    if kind == "codex":
        if not re.fullmatch(r"[A-Za-z0-9_]{1,100}", name):
            raise LoginError("코덱스 계정 이름이 올바르지 않다")
        home = CODEX_ACCOUNTS_ROOT / name
        return {
            "kind": "codex", "name": name,
            "argv": [CODEX_BIN, "login", "--device-auth"],
            "env": {"CODEX_HOME": str(home)},
            "home": home,
            "credential": home / "auth.json",
            "needs_code": False,
        }

    if kind == "claude":
        if not re.fullmatch(r"[0-9]{1,3}", name):
            raise LoginError("클로드 슬롯 번호가 올바르지 않다")
        home = CLAUDE_SLOT_ROOT / f"slot{name}"
        return {
            "kind": "claude", "name": f"slot{name}",
            "argv": [CLAUDE_BIN, "auth", "login", "--claudeai"],
            # 슬롯 HOME 을 쓸 때 env 토큰을 주면 CLI 가 그것을 먼저 집어
            # 자격증명 파일을 만들지 않는다 (.claude-relay-slots/README.md).
            "env": {"HOME": str(home), "CLAUDE_CODE_OAUTH_TOKEN": "", "ANTHROPIC_AUTH_TOKEN": ""},
            "home": home,
            "credential": home / ".claude" / ".credentials.json",
            "needs_code": True,
        }

    raise LoginError("지원하지 않는 대상이다 (codex/claude)")


def _parse(sess: dict) -> None:
    """누적 출력에서 화면에 보낼 것을 뽑는다."""
    text = sess["output"]
    if sess["kind"] == "codex":
        if not sess.get("url"):
            for u in _URL.findall(text):
                if "/device" in u:
                    sess["url"] = u
                    break
        if not sess.get("user_code"):
            m = _CODEX_CODE.search(text)
            if m:
                sess["user_code"] = m.group(1)
        if sess["url"] and sess["state"] == "starting":
            sess["state"] = "awaiting_browser"
    else:
        if not sess.get("url"):
            for u in _URL.findall(text):
                if "oauth/authorize" in u:
                    # OSC-8 하이퍼링크는 같은 URL 을 두 번 붙여 내보낸다. 잘라낸다.
                    half = len(u) // 2
                    if u[:half] == u[half:]:
                        u = u[:half]
                    sess["url"] = u
                    break
        if "Paste code" in text and sess["state"] in ("starting", "awaiting_browser"):
            sess["state"] = "awaiting_code"
        elif sess["url"] and sess["state"] == "starting":
            sess["state"] = "awaiting_browser"


def _credential_ok(plan: dict) -> bool:
    """로그인이 실제로 끝났는지는 파일로 판정한다. 출력 문구는 버전마다 바뀐다."""
    path = plan["credential"]
    if not path.exists():
        return False
    try:
        import json
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    if plan["kind"] == "codex":
        return bool((data.get("tokens") or {}).get("access_token"))
    oauth = data.get("claudeAiOauth") or data
    # refreshToken 이 없으면 8시간 뒤 죽는다 — 성공으로 치면 안 된다.
    return bool(oauth.get("refreshToken"))


def claude_credential_status(path: Path, now: float | None = None) -> dict:
    """Claude 슬롯 자격 파일의 재로그인 필요 여부와 사유. 토큰 값은 반환하지 않는다 (R-KEY).

    reason: None(정상) | credential_missing | refresh_missing | token_expired.
    expiresAt 이 없으면 만료를 판단할 수 없으므로 만료로 치지 않는다.
    """
    now = time.time() if now is None else now
    status = {"ok": False, "reason": "credential_missing", "expires_at": None}
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return status
    except (OSError, ValueError):
        status["reason"] = "refresh_missing"
        return status
    oauth = (data.get("claudeAiOauth") if isinstance(data, dict) else None) or data
    if not isinstance(oauth, dict):
        status["reason"] = "refresh_missing"
        return status

    expires_sec = None
    try:
        raw = float(oauth.get("expiresAt") or 0)
        if raw > 0:
            expires_sec = raw / 1000 if raw > 100_000_000_000 else raw
    except (TypeError, ValueError):
        pass
    if expires_sec:
        status["expires_at"] = datetime.fromtimestamp(expires_sec, timezone.utc).isoformat()

    if not oauth.get("refreshToken"):
        status["reason"] = "refresh_missing"
    elif expires_sec and now - expires_sec >= EXPIRY_GRACE_SEC:
        status["reason"] = "token_expired"
    else:
        status.update(ok=True, reason=None)
    return status


def keeper_refresh_failures(slot: str, log_path: Path | None = None) -> int:
    """keeper 로그 끝에서 그 슬롯의 연속 갱신 실패 횟수. 로그를 못 읽으면 0."""
    path = log_path or KEEPER_LOG
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - _KEEPER_TAIL_BYTES))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return 0
    current = None
    streak = 0
    for line in lines:
        m = _KEEPER_SLOT_LINE.match(line)
        if m:
            current = m.group(1)
            continue
        if current != str(slot):
            continue
        if "갱신 실패" in line or "재로그인 필요" in line:
            streak += 1
        elif "갱신됨" in line:
            streak = 0
    return streak


def _credential_signature(plan: dict) -> str | None:
    """Stable internal fingerprint used to prove a re-login replaced credentials."""
    path = plan["credential"]
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _credential_replaced(sess: dict) -> bool:
    return (
        _credential_ok(sess["plan"])
        and _credential_signature(sess["plan"]) != sess.get("credential_before")
    )


def _fresh_claude_credential(plan: dict) -> bool:
    """A newly written refresh token alone does not prove usable OAuth access."""
    try:
        oauth = json.loads(plan["credential"].read_text()).get("claudeAiOauth") or {}
        expires = float(oauth.get("expiresAt") or 0)
        if expires > 100_000_000_000:
            expires /= 1000
        return bool(oauth.get("accessToken") and oauth.get("refreshToken")
                    and expires > time.time() + 300)
    except (OSError, ValueError, TypeError):
        return False


def _access_fingerprint(plan: dict) -> str | None:
    try:
        oauth = json.loads(plan["credential"].read_text()).get("claudeAiOauth") or {}
        token = oauth.get("accessToken")
        return hashlib.sha256(token.encode()).hexdigest() if isinstance(token, str) and token else None
    except (OSError, ValueError, TypeError):
        return None


async def _verify_claude_call(plan: dict) -> bool:
    """Probe through the isolated Claude CLI, without returning its output."""
    env = dict(os.environ)
    env.update(plan["env"])
    try:
        proc = await asyncio.create_subprocess_exec(
            CLAUDE_BIN, "-p", "Reply OK", "--output-format", "json",
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env,
        )
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=90)
        if proc.returncode != 0:
            return False
        result = json.loads(output)
        return bool(result.get("result")) and not result.get("is_error", False)
    except (OSError, TimeoutError, ValueError, TypeError):
        if "proc" in locals() and proc.returncode is None:
            proc.kill()
            await proc.communicate()
        return False


async def _sync_slot4_token_to_db() -> bool:
    """Run the existing keeper's slot-4 sync after a verified relogin."""
    keeper = Path(__file__).with_name("claude_token_keeper.sh")
    if not keeper.is_file():
        return False
    env = dict(os.environ, CLAUDE_TOKEN_KEEPER_SLOT="4")
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", str(keeper), stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, env=env,
        )
        return await asyncio.wait_for(proc.wait(), timeout=150) == 0
    except (OSError, TimeoutError):
        if "proc" in locals() and proc.returncode is None:
            proc.kill()
            await proc.wait()
        return False


async def _pump(sess: dict) -> None:
    """pty 를 읽어 상태를 갱신하고, 끝나면 파일로 성패를 판정한다."""
    loop = asyncio.get_running_loop()
    master = sess["master"]
    deadline = sess["started_at"] + SESSION_TTL_SEC
    try:
        while True:
            if time.time() > deadline:
                sess["state"] = "expired"
                sess["message"] = "15분이 지나 취소했다. 다시 시도해라."
                break
            try:
                chunk = await asyncio.wait_for(
                    loop.run_in_executor(None, os.read, master, 4096), timeout=2)
            except TimeoutError:
                if sess["proc"].returncode is not None:
                    break
                continue
            except OSError:
                break
            if not chunk:
                break
            sess["output"] = (sess["output"] + _clean(chunk.decode(errors="replace")))[-_MAX_OUTPUT:]
            _parse(sess)
            if _credential_replaced(sess):
                if sess["kind"] == "claude":
                    sess["state"] = "verifying"
                    sess["message"] = "새 자격증명의 유효 기간과 실호출을 확인 중이다."
                    if not _fresh_claude_credential(sess["plan"]) or not await _verify_claude_call(sess["plan"]):
                        sess["state"] = "verification_pending"
                        sess["message"] = "실사용 검증을 마치지 못했다. 격리를 유지한다. 재로그인으로 다시 시도해라."
                        break
                    if sess["target"] == "claude:4" and not await _sync_slot4_token_to_db():
                        sess["state"] = "verification_pending"
                        sess["message"] = "실호출은 성공했지만 토큰 DB 동기화를 확인하지 못했다. 슬롯 격리를 유지한다."
                        break
                sess["state"] = "success"
                sess["message"] = "새 자격증명의 실호출을 확인했다." if sess["kind"] == "claude" else "자격증명이 기록됐다."
                break
    finally:
        proc = sess["proc"]
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await asyncio.sleep(0)
        try:
            os.close(master)
        except OSError:
            pass
        if sess["state"] not in ("success", "expired", "verification_pending", "cancelled"):
            if _credential_replaced(sess):
                sess["state"] = "verification_pending" if sess["kind"] == "claude" else "success"
                sess["message"] = ("새 자격증명의 실사용 검증을 마치지 못했다. 재로그인으로 다시 시도해라."
                                   if sess["kind"] == "claude" else "자격증명이 기록됐다.")
            else:
                sess["state"] = "failed"
                sess["message"] = sess.get("message") or "로그인이 끝나지 않았다."
        sess["finished_at"] = time.time()


def _purge() -> None:
    now = time.time()
    for key in [k for k, s in _SESSIONS.items()
                if s.get("finished_at") and now - s["finished_at"] > 600]:
        _SESSIONS.pop(key, None)


def find_active(target: str) -> dict | None:
    for sess in _SESSIONS.values():
        if sess["target"] == target and sess["state"] in (
                "starting", "awaiting_browser", "awaiting_code", "verifying"):
            return sess
    return None


async def start(target: str) -> dict:
    async with _START_LOCK:
        _purge()
        plan = resolve_target(target)
        existing = find_active(target)
        if existing:
            return public(existing)

        plan["home"].mkdir(parents=True, exist_ok=True)
        credential_before = _credential_signature(plan)
        master, slave = pty.openpty()
        env = dict(os.environ)
        env.update(plan["env"])
        env["TERM"] = "xterm-256color"
        try:
            proc = await asyncio.create_subprocess_exec(
                *plan["argv"], stdin=slave, stdout=slave, stderr=slave,
                env=env, close_fds=True,
            )
        except FileNotFoundError as exc:
            os.close(master)
            os.close(slave)
            raise LoginError(f"CLI 를 찾을 수 없다: {exc}") from exc
        finally:
            try:
                os.close(slave)
            except OSError:
                pass

        login_id = secrets.token_urlsafe(9)
        sess = {
            "login_id": login_id, "target": target, "kind": plan["kind"],
            "account": plan["name"], "needs_code": plan["needs_code"],
            "plan": plan, "proc": proc, "master": master,
            "credential_before": credential_before,
            "output": "", "url": None, "user_code": None,
            "state": "starting", "message": "", "started_at": time.time(),
            "finished_at": None,
        }
        _SESSIONS[login_id] = sess
        sess["task"] = asyncio.create_task(_pump(sess))

    # URL/코드가 찍힐 때까지 잠깐 기다린다 — 화면이 빈 창을 띄우지 않게.
    for _ in range(50):
        if sess["url"] or sess["state"] in ("failed", "expired", "success", "verification_pending"):
            break
        await asyncio.sleep(0.2)
    return public(sess)


def get(login_id: str) -> dict | None:
    sess = _SESSIONS.get(login_id)
    return public(sess) if sess else None


async def submit_code(login_id: str, code: str) -> dict:
    sess = _SESSIONS.get(login_id)
    if not sess:
        raise LoginError("로그인 세션이 없다")
    if not sess["needs_code"]:
        raise LoginError("이 대상은 코드 입력이 필요 없다")
    if sess["state"] != "awaiting_code":
        raise LoginError(f"지금은 코드를 받을 수 없다 (상태={sess['state']})")
    code = (code or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-#/=+.]{4,256}", code):
        raise LoginError("코드 형식이 올바르지 않다")
    try:
        os.write(sess["master"], (code + "\n").encode())
    except OSError as exc:
        raise LoginError(f"코드 전달 실패: {exc}") from exc
    sess["state"] = "verifying"
    sess["message"] = "코드를 전달했다. 확인 중이다."
    return public(sess)


async def cancel(login_id: str) -> dict:
    sess = _SESSIONS.get(login_id)
    if not sess:
        raise LoginError("로그인 세션이 없다")
    if sess["proc"].returncode is None:
        try:
            sess["proc"].kill()
        except ProcessLookupError:
            pass
    sess["state"] = "cancelled"
    sess["message"] = "취소했다."
    return public(sess)


def _antigravity_binding() -> dict:
    """Read only agy's credential identity, never return tokens or refresh them.

    id_token_email belongs to this credential, unlike Gemini's independent active
    account. A valid local credential is bound, but is not a live provider probe.
    """
    path = ANTIGRAVITY_HOME_ROOT / "antigravity-cli" / "antigravity-oauth-token"
    account = None
    present = None
    bound = False
    try:
        present = ANTIGRAVITY_BIN.is_file() and path.is_file()
        if present:
            if path.stat().st_size > 65536:
                raise ValueError("oversized credential metadata")
            payload = json.loads(path.read_text())
            email = payload.get("id_token_email")
            if isinstance(email, str) and re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", email.strip()):
                account = email.strip()
            token = payload.get("token") or {}
            expiry = datetime.fromisoformat(str(token.get("expiry", "")).replace("Z", "+00:00"))
            bound = bool(account and token.get("access_token") and expiry.tzinfo
                         and expiry > datetime.now(timezone.utc))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return {
        "target": "antigravity:host", "kind": "antigravity", "account": account,
        "bound": bound, "needs_login": present is False,
        "credential_present": present, "account_verified": False,
        "account_source": "agy_credential_metadata" if account else None,
        "usage_status": "unsupported", "usage_collected_at": None,
        "usage_note": "Installed agy has no read-only quota command; local credentials are not a live authentication check.",
    }


def bindings() -> list[dict]:
    """구독 계정이 실제로 런타임에 닿아 있는지. 화면의 '로그인 필요' 판정에 쓴다.

    DB 등록만으로는 아무것도 보장되지 않는다 — 2026-09-16 사고가 정확히 그
    간극이었다. 여기서는 **파일만** 본다. 값은 읽지 않고 존재 여부만 본다 (R-KEY).
    """
    import json

    out = []
    for home in sorted(CODEX_ACCOUNTS_ROOT.glob("*")) if CODEX_ACCOUNTS_ROOT.is_dir() else []:
        if not home.is_dir():
            continue
        plan = {"kind": "codex", "credential": home / "auth.json"}
        out.append({
            "target": f"codex:{home.name}", "kind": "codex", "account": home.name,
            "bound": _credential_ok(plan),
            "needs_login": not _credential_ok(plan),
        })

    for home in sorted(CLAUDE_SLOT_ROOT.glob("slot*")) if CLAUDE_SLOT_ROOT.is_dir() else []:
        if not home.is_dir():
            continue
        num = home.name.replace("slot", "")
        plan = {"kind": "claude", "credential": home / ".claude" / ".credentials.json"}
        ok = _credential_ok(plan)
        cred = claude_credential_status(plan["credential"])
        sub = None
        if ok:
            try:
                data = json.loads(plan["credential"].read_text())
                sub = (data.get("claudeAiOauth") or data).get("subscriptionType")
            except (OSError, ValueError):
                sub = None
        # Claude CLI keeps the non-secret account identity in HOME/.claude.json.
        # A credential file existing is not enough: a wrong browser account can be
        # logged into the slot, which previously made another account's quota appear
        # under the configured label.  Expose only identity metadata so the API can
        # fail closed on a binding mismatch without exposing OAuth tokens.
        actual_account = None
        try:
            profile = json.loads((home / ".claude.json").read_text())
            oauth_account = profile.get("oauthAccount") or {}
            actual_account = oauth_account.get("emailAddress") or None
        except (OSError, ValueError):
            pass
        out.append({
            "target": f"claude:{num}", "kind": "claude", "account": home.name,
            "bound": ok, "needs_login": not cred["ok"], "subscription": sub,
            "actual_account": actual_account,
            "reason": cred["reason"], "expires_at": cred["expires_at"],
            "refresh_failures": keeper_refresh_failures(num),
        })

    out.append(_antigravity_binding())

    for row in out:
        active = find_active(row["target"])
        row["login_in_progress"] = bool(active)
        row["login_id"] = active["login_id"] if active else None
    return out


def public(sess: dict) -> dict:
    """화면에 내보낼 것만 추린다. proc/master/output 원문은 내보내지 않는다."""
    return {
        "login_id": sess["login_id"],
        "target": sess["target"],
        "kind": sess["kind"],
        "account": sess["account"],
        "needs_code": sess["needs_code"],
        "state": sess["state"],
        "message": sess["message"],
        "url": sess["url"],
        "user_code": sess["user_code"],
        "expires_in": max(0, int(sess["started_at"] + SESSION_TTL_SEC - time.time())),
        "credential_fingerprint": (
            _access_fingerprint(sess["plan"])
            if sess["kind"] == "claude" and sess["state"] == "success" else None
        ),
    }
