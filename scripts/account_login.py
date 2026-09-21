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
import os
import pty
import re
import secrets
import time
from pathlib import Path

CODEX_ACCOUNTS_ROOT = Path(os.getenv("CODEX_ACCOUNTS_ROOT", "/root/.codex-accounts"))
CLAUDE_SLOT_ROOT = Path(os.getenv("CLAUDE_RELAY_SLOT_HOME_ROOT", "/root/.claude-relay-slots"))
CODEX_BIN = os.getenv("CODEX_BIN", "codex")
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")

SESSION_TTL_SEC = 15 * 60  # 일회용 코드 수명과 같게 둔다
_MAX_OUTPUT = 16384

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_OSC8 = re.compile(r"\x1b\]8;[^\x07\x1b]*(?:\x07|\x1b\\)")
_CODEX_CODE = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4,6})\b")
_URL = re.compile(r"https://[^\s\"'<>]+")
# 화면·로그로 새면 안 되는 것들. 출력에 섞여 나오면 통째로 가린다.
_SECRETISH = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|rt\.[A-Za-z0-9_\-]{8,}|ey[A-Za-z0-9_\-]{20,})")

# {login_id: session dict}
_SESSIONS: dict[str, dict] = {}


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
            except asyncio.TimeoutError:
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
                sess["state"] = "success"
                sess["message"] = "자격증명이 기록됐다."
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
        if sess["state"] not in ("success", "expired"):
            if _credential_replaced(sess):
                sess["state"] = "success"
                sess["message"] = "자격증명이 기록됐다."
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
                "starting", "awaiting_browser", "awaiting_code"):
            return sess
    return None


async def start(target: str) -> dict:
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
        if sess["url"] or sess["state"] in ("failed", "expired", "success"):
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
            "bound": ok, "needs_login": not ok, "subscription": sub,
            "actual_account": actual_account,
        })

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
    }
