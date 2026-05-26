"""E2E Credential Vault — Fernet 기반 자격증명 암호화 저장·관리 모듈."""
from __future__ import annotations

import json
import logging
import os
from typing import Any
from uuid import UUID

from cryptography.fernet import Fernet

from app.core.db_pool import get_pool

logger = logging.getLogger(__name__)

# ── 암호화 키 ──────────────────────────────────────────
_VAULT_KEY: bytes | None = None


_VAULT_KEY_FILE = "/app/app/.vault.key"


def _coerce_json_list(value: Any) -> list[Any]:
    """Return a JSON list from asyncpg jsonb values or legacy double-encoded rows."""
    parsed = _coerce_json_value(value)
    return parsed if isinstance(parsed, list) else []


def _coerce_json_dict(value: Any) -> dict[str, Any]:
    """Return a JSON object from asyncpg jsonb values or legacy double-encoded rows."""
    parsed = _coerce_json_value(value)
    return parsed if isinstance(parsed, dict) else {}


def _coerce_json_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return value
    if not isinstance(value, str):
        return value

    text = value.strip()
    if not text:
        return None
    for _ in range(2):
        try:
            decoded = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            return None
        if isinstance(decoded, str):
            text = decoded.strip()
            continue
        return decoded
    return None


def _normalize_json_fields(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize JSONB fields before API responses or login execution."""
    item["extra_fields"] = _coerce_json_dict(item.get("extra_fields"))
    item["login_steps"] = _coerce_json_list(item.get("login_steps"))
    return item


def _get_fernet() -> Fernet:
    """암호화 키 로드: 환경변수 → 파일 → 자동 생성."""
    global _VAULT_KEY
    if _VAULT_KEY is None:
        key_str = os.getenv("VAULT_ENCRYPTION_KEY", "")
        if not key_str:
            # 파일 폴백
            if os.path.exists(_VAULT_KEY_FILE):
                key_str = open(_VAULT_KEY_FILE).read().strip()
            else:
                # 최초 1회 자동 생성 + 파일 저장
                key_str = Fernet.generate_key().decode()
                os.makedirs(os.path.dirname(_VAULT_KEY_FILE), exist_ok=True)
                with open(_VAULT_KEY_FILE, "w") as f:
                    f.write(key_str)
                logger.info("vault_encryption_key_auto_generated", path=_VAULT_KEY_FILE)
        _VAULT_KEY = key_str.encode()
    return Fernet(_VAULT_KEY)


def encrypt_value(plaintext: str) -> str:
    """평문을 Fernet 암호화하여 base64 문자열로 반환."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str) -> str:
    """Fernet 암호화 문자열을 복호화."""
    return _get_fernet().decrypt(ciphertext.encode()).decode()


# ── CRUD ───────────────────────────────────────────────

async def list_credentials(
    project: str | None = None,
    service: str | None = None,
    include_secrets: bool = False,
) -> list[dict[str, Any]]:
    """자격증명 목록 조회. include_secrets=False면 암호 마스킹."""
    pool = get_pool()
    conditions: list[str] = ["is_active = TRUE"]
    args: list[Any] = []
    idx = 1

    if project:
        conditions.append(f"project = ${idx}")
        args.append(project)
        idx += 1
    if service:
        conditions.append(f"service = ${idx}")
        args.append(service)
        idx += 1

    where = " AND ".join(conditions)
    rows = await pool.fetch(
        f"SELECT * FROM e2e_credentials WHERE {where} ORDER BY service, label",
        *args,
    )

    results = []
    for row in rows:
        item = dict(row)
        _normalize_json_fields(item)
        item["id"] = str(item["id"])
        # 시간 필드 직렬화
        for tf in ("created_at", "updated_at", "last_used_at", "last_verified"):
            if item.get(tf):
                item[tf] = item[tf].isoformat()
        # 복호화 또는 마스킹
        try:
            username = decrypt_value(item["username_enc"])
        except Exception:
            username = "[복호화 실패]"
        if include_secrets:
            item["username"] = username
            try:
                item["password"] = decrypt_value(item["password_enc"])
            except Exception:
                item["password"] = "[복호화 실패]"
        else:
            item["username"] = username
            item["password"] = "********"
        # 원본 암호화 필드 제거
        item.pop("username_enc", None)
        item.pop("password_enc", None)
        results.append(item)

    return results


async def get_credential(
    credential_id: str | UUID,
    include_secrets: bool = True,
) -> dict[str, Any] | None:
    """단일 자격증명 조회 (복호화 포함)."""
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM e2e_credentials WHERE id = $1",
        credential_id if isinstance(credential_id, UUID) else UUID(credential_id),
    )
    if not row:
        return None

    item = dict(row)
    _normalize_json_fields(item)
    item["id"] = str(item["id"])
    for tf in ("created_at", "updated_at", "last_used_at", "last_verified"):
        if item.get(tf):
            item[tf] = item[tf].isoformat()

    try:
        item["username"] = decrypt_value(item["username_enc"])
    except Exception:
        item["username"] = "[복호화 실패]"

    if include_secrets:
        try:
            item["password"] = decrypt_value(item["password_enc"])
        except Exception:
            item["password"] = "[복호화 실패]"
    else:
        item["password"] = "********"

    item.pop("username_enc", None)
    item.pop("password_enc", None)
    return item


async def create_credential(
    service: str,
    username: str,
    password: str,
    project: str | None = None,
    label: str = "기본",
    login_url: str | None = None,
    extra_fields: dict | None = None,
    login_steps: list | None = None,
) -> dict[str, Any]:
    """새 자격증명 생성 (암호화 저장)."""
    pool = get_pool()
    username_enc = encrypt_value(username)
    password_enc = encrypt_value(password)

    # extra_fields 내부 값도 암호화
    enc_extra = {}
    if extra_fields:
        for k, v in extra_fields.items():
            enc_extra[k] = encrypt_value(str(v))

    row = await pool.fetchrow(
        """
        INSERT INTO e2e_credentials
            (service, project, label, login_url, username_enc, password_enc, extra_fields, login_steps)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb)
        ON CONFLICT (service, COALESCE(project, '_ALL_'), label)
        DO UPDATE SET
            login_url = EXCLUDED.login_url,
            username_enc = EXCLUDED.username_enc,
            password_enc = EXCLUDED.password_enc,
            extra_fields = EXCLUDED.extra_fields,
            login_steps = EXCLUDED.login_steps,
            updated_at = NOW(),
            is_active = TRUE
        RETURNING id
        """,
        service, project, label, login_url,
        username_enc, password_enc,
        json.dumps(enc_extra), json.dumps(_coerce_json_list(login_steps)),
    )

    cred_id = str(row["id"])
    logger.info("자격증명 저장: service=%s project=%s label=%s id=%s", service, project, label, cred_id)
    return await get_credential(cred_id, include_secrets=False)


async def update_credential(
    credential_id: str,
    **kwargs,
) -> dict[str, Any] | None:
    """자격증명 수정. username/password/extra_fields 변경 시 재암호화."""
    pool = get_pool()
    existing = await pool.fetchrow(
        "SELECT id FROM e2e_credentials WHERE id = $1",
        UUID(credential_id),
    )
    if not existing:
        return None

    sets: list[str] = ["updated_at = NOW()"]
    args: list[Any] = []
    idx = 1

    for field in ("service", "project", "label", "login_url"):
        if field in kwargs and kwargs[field] is not None:
            sets.append(f"{field} = ${idx}")
            args.append(kwargs[field])
            idx += 1

    if "username" in kwargs:
        sets.append(f"username_enc = ${idx}")
        args.append(encrypt_value(kwargs["username"]))
        idx += 1

    if "password" in kwargs:
        sets.append(f"password_enc = ${idx}")
        args.append(encrypt_value(kwargs["password"]))
        idx += 1

    if "extra_fields" in kwargs and kwargs["extra_fields"]:
        enc_extra = {k: encrypt_value(str(v)) for k, v in kwargs["extra_fields"].items()}
        sets.append(f"extra_fields = ${idx}::jsonb")
        args.append(json.dumps(enc_extra))
        idx += 1

    if "login_steps" in kwargs:
        sets.append(f"login_steps = ${idx}::jsonb")
        args.append(json.dumps(_coerce_json_list(kwargs["login_steps"])))
        idx += 1

    if "is_active" in kwargs:
        sets.append(f"is_active = ${idx}")
        args.append(kwargs["is_active"])
        idx += 1

    args.append(UUID(credential_id))
    await pool.execute(
        f"UPDATE e2e_credentials SET {', '.join(sets)} WHERE id = ${idx}",
        *args,
    )
    return await get_credential(credential_id, include_secrets=False)


async def delete_credential(credential_id: str) -> bool:
    """자격증명 소프트 삭제."""
    pool = get_pool()
    result = await pool.execute(
        "UPDATE e2e_credentials SET is_active = FALSE, updated_at = NOW() WHERE id = $1",
        UUID(credential_id),
    )
    return result.endswith("1")


async def mark_used(credential_id: str) -> None:
    """사용 시각 갱신."""
    pool = get_pool()
    await pool.execute(
        "UPDATE e2e_credentials SET last_used_at = NOW() WHERE id = $1",
        UUID(credential_id),
    )


async def mark_verified(credential_id: str, success: bool = True) -> None:
    """로그인 검증 결과 기록."""
    pool = get_pool()
    if success:
        await pool.execute(
            "UPDATE e2e_credentials SET last_verified = NOW() WHERE id = $1",
            UUID(credential_id),
        )


# ── Playwright 자동 로그인 ─────────────────────────────

async def get_login_credential(
    service: str,
    project: str | None = None,
    label: str = "기본",
) -> dict[str, Any] | None:
    """서비스+프로젝트+라벨로 자격증명 조회 (복호화 포함, 자동 로그인용)."""
    pool = get_pool()
    if project:
        row = await pool.fetchrow(
            "SELECT * FROM e2e_credentials WHERE service = $1 AND project = $2 AND label = $3 AND is_active = TRUE",
            service, project, label,
        )
    else:
        row = await pool.fetchrow(
            "SELECT * FROM e2e_credentials WHERE service = $1 AND project IS NULL AND label = $2 AND is_active = TRUE",
            service, label,
        )
    if not row:
        return None

    item = dict(row)
    _normalize_json_fields(item)
    item["id"] = str(item["id"])
    item["username"] = decrypt_value(item["username_enc"])
    item["password"] = decrypt_value(item["password_enc"])

    # extra_fields 복호화
    if item.get("extra_fields"):
        dec_extra = {}
        for k, v in item["extra_fields"].items():
            try:
                dec_extra[k] = decrypt_value(str(v))
            except Exception:
                dec_extra[k] = v
        item["extra_fields"] = dec_extra

    item.pop("username_enc", None)
    item.pop("password_enc", None)
    return item


async def _api_token_inject(page: Any, credential: dict[str, Any], step: dict[str, Any]) -> bool:
    """API 호출로 JWT 토큰을 획득하여 브라우저에 직접 주입 (React 폼 우회)."""
    import aiohttp

    api_url = step.get("api_url", "")
    if not api_url:
        login_url = credential.get("login_url", "")
        if login_url:
            from urllib.parse import urlparse
            parsed = urlparse(login_url)
            api_url = f"{parsed.scheme}://{parsed.netloc}/api/v1/auth/login"
    if not api_url:
        logger.error("api_token_inject: api_url 미설정")
        return False

    username = credential.get("username", "")
    password = credential.get("password", "")
    email_field = step.get("email_field", "email")
    password_field = step.get("password_field", "password")
    token_path = step.get("token_path", "token")
    storage_key = step.get("storage_key", "aads_token")
    cookie_name = step.get("cookie_name", storage_key)
    cookie_max_age = step.get("cookie_max_age", 604800)
    redirect_url = step.get("redirect_url", "")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                api_url,
                json={email_field: username, password_field: password},
                timeout=aiohttp.ClientTimeout(total=10),
                ssl=False,
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error("api_token_inject: API %s returned %s: %s", api_url, resp.status, body[:200])
                    return False
                data = await resp.json()

        token = data
        for key in token_path.split("."):
            token = token[key]

        js_code = f"""() => {{
            localStorage.setItem('{storage_key}', '{token}');
            document.cookie = '{cookie_name}={token}; path=/; max-age={cookie_max_age}; SameSite=Lax';
        }}"""
        await page.evaluate(js_code)
        logger.info("api_token_inject: token injected storage_key=%s", storage_key)

        if redirect_url:
            await page.goto(redirect_url, wait_until="domcontentloaded", timeout=15000)

        return True
    except Exception as e:
        logger.error("api_token_inject failed: %s", e)
        return False


async def execute_login_steps(page: Any, credential: dict[str, Any]) -> bool:
    """Playwright page에 login_steps 시퀀스를 실행하여 자동 로그인.

    login_steps 형식:
    [
        {"action": "navigate", "url": "https://..."},
        {"action": "fill", "selector": "input#email", "value": "{{username}}"},
        {"action": "fill", "selector": "input[type='password']", "value": "{{password}}"},
        {"action": "click", "selector": "button[type='submit']"},
        {"action": "wait", "ms": 3000},
        {"action": "wait_for_url", "pattern": "/dashboard"},
        {"action": "api_token_inject", "api_url": "https://...", "token_path": "token",
         "storage_key": "aads_token", "redirect_url": "https://.../chat"},
        {"action": "evaluate", "script": "() => console.log('hello')"}
    ]
    """
    steps = credential.get("login_steps", [])
    username = credential.get("username", "")
    password = credential.get("password", "")

    if not steps:
        login_url = credential.get("login_url")
        # React 앱 감지: api_token_inject를 기본 전략으로 사용
        if login_url:
            inject_ok = await _api_token_inject(page, credential, {
                "redirect_url": login_url.replace("/login", "/chat").replace("/signin", "/"),
            })
            if inject_ok:
                await mark_used(credential["id"])
                return True
            # 폴백: 기존 폼 fill 방식
            await page.goto(login_url, wait_until="domcontentloaded", timeout=15000)

        email_input = page.locator("input[type='email'], input[name='email'], input[name='username'], input#email, input#username").first
        await email_input.clear(timeout=5000)
        await email_input.fill(username, timeout=5000)
        pw_input = page.locator("input[type='password']").first
        await pw_input.fill(password, timeout=5000)
        login_btn = page.locator("button[type='submit'], button:has-text('로그인'), button:has-text('Login'), button:has-text('Sign in')").first
        await login_btn.click(timeout=5000)
        await page.wait_for_timeout(3000)
        await mark_used(credential["id"])
        return True

    # 정의된 스텝 순차 실행
    for step in steps:
        action = step.get("action", "")
        # 템플릿 변수 치환
        selector = step.get("selector", "")
        value = step.get("value", "")
        value = value.replace("{{username}}", username).replace("{{password}}", password)
        for ek, ev in credential.get("extra_fields", {}).items():
            value = value.replace(f"{{{{{ek}}}}}", str(ev))

        try:
            if action == "navigate":
                url = step.get("url", "")
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            elif action == "fill":
                el = page.locator(selector).first
                await el.clear(timeout=5000)
                await el.fill(value, timeout=5000)
            elif action == "click":
                el = page.locator(selector).first
                await el.click(timeout=5000)
            elif action == "wait":
                ms = step.get("ms", 1000)
                await page.wait_for_timeout(ms)
            elif action == "wait_for_url":
                pattern = step.get("pattern", "")
                await page.wait_for_url(f"**{pattern}**", timeout=10000)
            elif action == "api_token_inject":
                ok = await _api_token_inject(page, credential, step)
                if not ok:
                    return False
            elif action == "evaluate":
                script = step.get("script", "")
                script = script.replace("{{username}}", username).replace("{{password}}", password)
                for ek, ev in credential.get("extra_fields", {}).items():
                    script = script.replace(f"{{{{{ek}}}}}", str(ev))
                await page.evaluate(script)
            elif action == "screenshot":
                pass
            else:
                logger.warning("알 수 없는 login_step action: %s", action)
        except Exception as e:
            logger.error("login_step 실행 실패: action=%s error=%s", action, e)
            return False

    await mark_used(credential["id"])
    return True


# --------------- E2E Auto-Login URL Generator ---------------

_E2E_PROJECT_CONFIG: dict[str, dict[str, Any]] = {
    "AADS": {
        "service": "aads-dashboard",
        "label": "E2E 자동 검증용",
        "api_url": "https://aads.newtalk.kr/api/v1/auth/login",
        "token_path": "token",
        "e2e_url": "https://aads.newtalk.kr/static/e2e-auth.html?token={token}&redirect={redirect}",
        "default_redirect": "/",
    },
    "GO100": {
        "service": "go100.newtalk.kr",
        "label": "E2E 테스트 계정",
        "api_url": "https://go100.newtalk.kr/api/v1/auth/login",
        "token_path": "access_token",
        "e2e_url": "https://go100.newtalk.kr/e2e-auth.html?token={token}&redirect={redirect}",
        "default_redirect": "/",
    },
    "NTV2": {
        "service": "newtalk-v2-admin",
        "label": "V2 관리자",
        "api_url": "https://v2.newtalk.kr/api/auth/login",
        "token_path": "token",
        "e2e_url": "https://v2.newtalk.kr/reports/e2e-login.html#token={token}&role=retail&name=E2E&email=e2e_verify%40newtalk.kr&uid=79747&redirect=none",
        "default_redirect": "/admin",
        "url_encode_token": True,
    },
    "SF": {
        "service": "shotflow.newtalk.kr",
        "label": "E2E 자동 검증용",
        "api_url": "https://ypvqgojexppcgilacxdz.supabase.co/auth/v1/token?grant_type=password",
        "token_path": "access_token",
        "refresh_token_path": "refresh_token",
        "e2e_url": "https://shotflow.newtalk.kr/e2e-auth.html?access_token={token}&refresh_token={refresh_token}&redirect={redirect}",
        "default_redirect": "/dashboard",
        "extra_headers": {"apikey": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InlwdnFnb2pleHBwY2dpbGFjeGR6Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzA5MjA1NDYsImV4cCI6MjA4NjQ5NjU0Nn0.c7WFH6wmwCObdjPTdu0QObDFa5KO7CXo8gas0ocLSvs"},
    },
}


async def get_e2e_login_url(project: str, redirect: str | None = None) -> dict[str, Any]:
    """프로젝트별 E2E 브라우저 자동 로그인 URL 생성.

    Returns dict with keys: url, project, success, error (if failed).
    """
    import aiohttp
    from urllib.parse import quote

    project = project.upper()
    if project == "KIS":
        project = "GO100"

    config = _E2E_PROJECT_CONFIG.get(project)
    if not config:
        return {"success": False, "error": f"Unsupported project: {project}"}

    cred = await get_login_credential(config["service"], project, config["label"])
    if not cred:
        return {"success": False, "error": f"No credential found for {project}"}

    username = cred.get("username", "")
    password = cred.get("password", "")
    headers = config.get("extra_headers", {})
    headers["Content-Type"] = "application/json"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                config["api_url"],
                json={"email": username, "password": password},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
                ssl=False,
            ) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    return {"success": False, "error": f"Auth API {resp.status}: {body[:200]}"}
                data = await resp.json()
    except Exception as e:
        return {"success": False, "error": f"Auth request failed: {e}"}

    token = data
    for key in config["token_path"].split("."):
        token = token[key]

    refresh_token = ""
    if "refresh_token_path" in config:
        rt = data
        for key in config["refresh_token_path"].split("."):
            rt = rt[key]
        refresh_token = str(rt)

    if config.get("url_encode_token"):
        token = quote(str(token), safe="")

    redir = redirect or config["default_redirect"]
    url = config["e2e_url"].format(token=token, refresh_token=refresh_token, redirect=redir)

    return {"success": True, "url": url, "project": project}
