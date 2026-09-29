#!/usr/bin/env python3
"""AADS e2e_credentials 중 오비서 도메인 행만 오비서 인증 DB 로 재암호화 이관.

AADS-OBYS-VAULT-SEPARATION-20260930. 오비서는 AADS VAULT_ENCRYPTION_KEY 를
쓰지 않는다. 대상 행을 AADS 키로 복호화해 OBYS_VAULT_KEY 로 다시 암호화한 뒤
오비서 DB 에 id 기준으로 upsert 한다(멱등). AADS DB 는 읽기 전용 트랜잭션으로만 연다.

대상: login_url 호스트가 fb.newtalk.kr 이거나 project in (OBYS, FOOD, ACCT).

기본은 --dry-run 이다. --apply 를 줘야만 오비서 DB 에 쓴다.
평문·키·DSN 은 출력·로그·파일 어디에도 남기지 않는다. 행 식별은 id/service/label 만.

키 (값은 인자로 받지 않는다 — 셸 히스토리에 남지 않게 env/파일로만):
  원본(AADS): env VAULT_ENCRYPTION_KEY, 없으면 --aads-key-file (기본 /app/app/.vault.key)
  대상(오비서): env OBYS_VAULT_KEY
DSN: --source-dsn / env AADS_DATABASE_URL,  --target-dsn / env OBYS_AUTH_DATABASE_URL

종료코드: 0 정상 / 1 이관 중 오류(롤백됨) / 2 설정 오류
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken

OBYS_HOST = "fb.newtalk.kr"
OBYS_PROJECTS = ("OBYS", "FOOD", "ACCT")
DEFAULT_AADS_KEY_FILE = "/app/app/.vault.key"

COLUMNS = (
    "id", "tenant_id", "service", "project", "label", "login_url",
    "username_enc", "password_enc", "extra_fields", "login_steps", "is_active",
    "last_used_at", "last_verified", "created_at", "updated_at",
)

SOURCE_SQL = """
SELECT {cols}
  FROM e2e_credentials
 WHERE login_url ILIKE '%' || $1 || '%'
    OR upper(project) = ANY($2::text[])
 ORDER BY created_at NULLS LAST, id
""".format(cols=", ".join(COLUMNS))

UPSERT_SQL = """
INSERT INTO public.e2e_credentials ({cols})
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb, $11, $12, $13, $14, $15)
ON CONFLICT (id) DO UPDATE SET
    tenant_id = EXCLUDED.tenant_id, service = EXCLUDED.service,
    project = EXCLUDED.project, label = EXCLUDED.label,
    login_url = EXCLUDED.login_url, username_enc = EXCLUDED.username_enc,
    password_enc = EXCLUDED.password_enc, extra_fields = EXCLUDED.extra_fields,
    login_steps = EXCLUDED.login_steps, is_active = EXCLUDED.is_active,
    last_used_at = EXCLUDED.last_used_at, last_verified = EXCLUDED.last_verified,
    updated_at = EXCLUDED.updated_at
""".format(cols=", ".join(COLUMNS))


class ConfigError(RuntimeError):
    """메시지에는 설정 이름만 넣는다."""


class SourceDecryptError(RuntimeError):
    """AADS 키로 복호화 실패. 어떤 필드인지만 담는다."""


# ── 순수 함수 (단위 테스트 대상) ─────────────────────────────

def login_host(login_url: str | None) -> str:
    text = (login_url or "").strip()
    if not text:
        return ""
    if "//" not in text:
        text = "//" + text
    try:
        return (urlsplit(text).hostname or "").lower()
    except ValueError:
        return ""


def is_obys_row(row: Mapping[str, Any]) -> bool:
    if str(row.get("project") or "").strip().upper() in OBYS_PROJECTS:
        return True
    return login_host(row.get("login_url")) == OBYS_HOST


def _json_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def reencrypt_row(row: Mapping[str, Any], src: Fernet, dst: Fernet) -> dict[str, Any]:
    """username/password/extra_fields 를 src→dst 로 옮긴 새 dict. 평문은 지역변수에만."""
    out = dict(row)
    for field in ("username_enc", "password_enc"):
        try:
            plain = src.decrypt(str(row[field]).encode())
        except (InvalidToken, KeyError, TypeError, ValueError):
            raise SourceDecryptError(field) from None
        out[field] = dst.encrypt(plain).decode()
        if dst.decrypt(out[field].encode()) != plain:  # 왕복 확인
            raise SourceDecryptError(f"{field}:roundtrip")
        del plain
    extra = _json_value(row.get("extra_fields"), {})
    if isinstance(extra, dict):
        moved = {}
        for key, value in extra.items():
            # credential_vault.get_credential 과 같은 규칙: 복호화되면 암호문,
            # 안 되면 원래 평문 메타데이터(source 등)로 보고 그대로 둔다.
            try:
                plain = src.decrypt(str(value).encode())
            except (InvalidToken, ValueError):
                moved[key] = value
                continue
            moved[key] = dst.encrypt(plain).decode()
            del plain
        extra = moved
    out["extra_fields"] = extra
    out["login_steps"] = _json_value(row.get("login_steps"), [])
    return out


def plan_rows(
    rows: Iterable[Mapping[str, Any]],
    src: Fernet,
    dst: Fernet,
    target_tenants: set[str] | None,
    target_ids: set[str] | None,
    target_natural_keys: Mapping[tuple, str] | None,
) -> list[dict[str, Any]]:
    """각 행에 action(insert/update/skip) 과 reason 을 붙인다. target_* 가 None 이면 대상 DB 미조회."""
    planned = []
    for row in rows:
        if not is_obys_row(row):
            continue
        rid = str(row["id"])
        entry: dict[str, Any] = {
            "id": rid,
            "tenant_id": str(row.get("tenant_id") or ""),
            "service": row.get("service"),
            "project": row.get("project"),
            "label": row.get("label"),
            "host": login_host(row.get("login_url")),
        }
        try:
            entry["row"] = reencrypt_row(row, src, dst)
        except SourceDecryptError as exc:
            entry.update(action="skip", reason=f"source_decrypt_failed:{exc}")
            planned.append(entry)
            continue
        if target_tenants is not None and entry["tenant_id"] not in target_tenants:
            entry.update(action="skip", reason="tenant_missing_in_target")
        elif target_natural_keys is not None and target_natural_keys.get(natural_key(row), rid) != rid:
            entry.update(action="skip", reason="natural_key_taken_by_other_id")
        elif target_ids is None:
            entry.update(action="upsert", reason="target_not_checked")
        else:
            entry.update(action="update" if rid in target_ids else "insert", reason="")
        planned.append(entry)
    return planned


def natural_key(row: Mapping[str, Any]) -> tuple:
    return (str(row.get("tenant_id") or ""), row.get("service"), row.get("project") or "_ALL_", row.get("label"))


def public_view(entry: Mapping[str, Any]) -> dict[str, Any]:
    """출력용. 암호문도 내보내지 않는다."""
    return {k: v for k, v in entry.items() if k != "row"}


def load_keys(env: Mapping[str, str], aads_key_file: str) -> tuple[Fernet, Fernet]:
    src_raw = (env.get("VAULT_ENCRYPTION_KEY") or "").strip()
    if not src_raw:
        path = Path(aads_key_file)
        if not path.is_file():
            raise ConfigError("VAULT_ENCRYPTION_KEY/--aads-key-file: AADS key not found")
        src_raw = path.read_text().strip()
    dst_raw = (env.get("OBYS_VAULT_KEY") or "").strip()
    if not dst_raw:
        raise ConfigError("OBYS_VAULT_KEY: not set")
    try:
        src = Fernet(src_raw.encode())
    except ValueError:
        raise ConfigError("AADS key: not a Fernet key") from None
    try:
        dst = Fernet(dst_raw.encode())
    except ValueError:
        raise ConfigError("OBYS_VAULT_KEY: not a Fernet key") from None
    if src_raw == dst_raw:
        raise ConfigError("OBYS_VAULT_KEY: must differ from the AADS key")
    return src, dst


# ── DB ─────────────────────────────────────────────────────

async def _run(args: argparse.Namespace, src: Fernet, dst: Fernet) -> int:
    import asyncpg

    source = await asyncpg.connect(args.source_dsn, timeout=10)
    try:
        async with source.transaction(readonly=True):
            rows = [dict(r) for r in await source.fetch(SOURCE_SQL, OBYS_HOST, list(OBYS_PROJECTS))]
    finally:
        await source.close()

    target = None
    tenants = ids = keys = None
    if args.target_dsn:
        target = await asyncpg.connect(args.target_dsn, timeout=10)
    try:
        if target is not None:
            if not await target.fetchval("SELECT to_regclass('public.e2e_credentials') IS NOT NULL"):
                print("ERROR target: e2e_credentials missing — apply migrations/20260930_obys_e2e_credentials_jinah.sql first",
                      file=sys.stderr)
                return 2
            tenants = {str(r["id"]) for r in await target.fetch("SELECT id FROM public.tenants")}
            existing = await target.fetch(
                "SELECT id, tenant_id, service, project, label FROM public.e2e_credentials"
            )
            ids = {str(r["id"]) for r in existing}
            keys = {natural_key(dict(r)): str(r["id"]) for r in existing}
        elif args.apply:
            print("ERROR --apply requires --target-dsn or OBYS_AUTH_DATABASE_URL", file=sys.stderr)
            return 2

        planned = plan_rows(rows, src, dst, tenants, ids, keys)
        for entry in planned:
            print(json.dumps(public_view(entry), ensure_ascii=False))
        writable = [e for e in planned if e["action"] in ("insert", "update")]
        summary = {
            "mode": "apply" if args.apply else "dry-run",
            "source_rows": len(rows),
            "selected": len(planned),
            "insert": sum(e["action"] == "insert" for e in planned),
            "update": sum(e["action"] == "update" for e in planned),
            "skip": sum(e["action"] == "skip" for e in planned),
            "unchecked": sum(e["action"] == "upsert" for e in planned),
        }
        if args.apply and writable:
            try:
                async with target.transaction():
                    for entry in writable:
                        r = entry["row"]
                        await target.execute(
                            UPSERT_SQL,
                            r["id"], r["tenant_id"], r["service"], r["project"], r["label"],
                            r["login_url"], r["username_enc"], r["password_enc"],
                            json.dumps(r["extra_fields"], ensure_ascii=False),
                            json.dumps(r["login_steps"], ensure_ascii=False),
                            r["is_active"], r["last_used_at"], r["last_verified"],
                            r["created_at"], r["updated_at"],
                        )
            except Exception as exc:
                # 예외 메시지에 값이 섞일 수 있으므로 타입만 남긴다.
                print(f"ERROR apply failed ({type(exc).__name__}); transaction rolled back", file=sys.stderr)
                return 1
            summary["written"] = len(writable)
        print(json.dumps({"summary": summary}, ensure_ascii=False))
        return 0
    finally:
        if target is not None:
            await target.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True, help="기본값. 쓰지 않는다")
    mode.add_argument("--apply", action="store_true", help="오비서 DB 에 upsert 한다")
    parser.add_argument("--source-dsn", default=os.getenv("AADS_DATABASE_URL", ""))
    parser.add_argument("--target-dsn", default=os.getenv("OBYS_AUTH_DATABASE_URL", ""))
    parser.add_argument("--aads-key-file", default=DEFAULT_AADS_KEY_FILE)
    args = parser.parse_args(argv)

    if not args.source_dsn:
        print("ERROR AADS_DATABASE_URL/--source-dsn: not set", file=sys.stderr)
        return 2
    try:
        src, dst = load_keys(os.environ, args.aads_key_file)
    except ConfigError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    return asyncio.run(_run(args, src, dst))


if __name__ == "__main__":
    sys.exit(main())
