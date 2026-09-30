#!/usr/bin/env python3
"""AADS VAULT_ENCRYPTION_KEY 교체 — 이전 키 암호문을 새 주키로 재암호화.

AADS-VAULT-KEY-ROTATION-20260930. 앱은 MultiFernet([주키, 이전키...]) 으로 옛
암호문도 읽으므로 이 스크립트는 무중단 배포 뒤 아무 때나 돌려도 된다.

각 행: 모든 키로 복호화 → 주키로 재암호화 → 주키 단독으로 재복호화해 원문과
일치하는지 검증 → 통과한 것만 UPDATE → UPDATE 후 다시 읽어 한 번 더 검증.
테이블 단위 트랜잭션이다. 한 행이라도 검증에 실패하면 그 테이블 전체를 롤백하고
실패 행의 id 만 보고한다. 이미 주키로 풀리는 값은 건너뛴다(멱등).
어떤 키로도 풀리지 않는 값은 손대지 않고 id 만 undecryptable 로 보고한다
(교체 전부터 이미 깨져 있던 값이다 — 교체로 잃는 것이 없다).

e2e_credentials.extra_fields 는 JSON 값 하나하나가 개별 암호문이다. 값 단위로
처리하고, 어떤 키로도 안 풀리는 값은 평문 메타데이터(source 등)로 보고 그대로 둔다
(credential_vault.get_credential 과 같은 규칙).

기본은 --dry-run 이다. --apply 를 줘야만 쓴다.
평문·암호문·키·DSN 은 출력·로그·파일 어디에도 남기지 않는다. 출력은 건수와 id 뿐이다.

키 (값은 인자로 받지 않는다 — 셸 히스토리에 남지 않게 env 로만):
  주키: env VAULT_ENCRYPTION_KEY
  이전키: env VAULT_ENCRYPTION_KEY_PREVIOUS (쉼표 구분 다중 허용)
DSN: --dsn / env DATABASE_URL

종료코드: 0 정상 / 1 검증 실패·오류(해당 테이블 롤백됨) / 2 설정 오류
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Mapping

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: tuple[str, ...]
    json_column: str | None = None


TABLES: dict[str, TableSpec] = {
    spec.name: spec
    for spec in (
        TableSpec("agent_vault_credentials", ("username_enc", "password_enc")),
        TableSpec("e2e_credentials", ("username_enc", "password_enc"), json_column="extra_fields"),
        TableSpec("llm_api_keys", ("encrypted_value",)),
        TableSpec("user_api_keys", ("encrypted_key",)),
    )
}


class ConfigError(RuntimeError):
    """메시지에는 설정 이름만 넣는다."""


class VerifyError(RuntimeError):
    """재암호화 검증 실패. 필드 이름만 담는다."""


@dataclass(frozen=True)
class Keys:
    primary: Fernet
    any_key: MultiFernet
    previous_count: int


@dataclass
class RowPlan:
    id: Any
    status: str  # current | rotate | undecryptable | failed
    updates: dict[str, Any] = field(default_factory=dict)
    # 재조회 검증용. 평문 대신 sha256 만 들고 있다.
    digests: dict[str, str] = field(default_factory=dict)
    json_digests: dict[str, str] = field(default_factory=dict)


# ── 키 ─────────────────────────────────────────────────────

def load_keys(env: Mapping[str, str]) -> Keys:
    primary_raw = (env.get("VAULT_ENCRYPTION_KEY") or "").strip()
    if not primary_raw:
        raise ConfigError("VAULT_ENCRYPTION_KEY: not set")
    try:
        primary = Fernet(primary_raw.encode())
    except (ValueError, TypeError):
        raise ConfigError("VAULT_ENCRYPTION_KEY: not a Fernet key") from None
    previous: list[Fernet] = []
    seen = {primary_raw}
    for index, part in enumerate((env.get("VAULT_ENCRYPTION_KEY_PREVIOUS") or "").split(",")):
        part = part.strip()
        if not part or part in seen:
            continue
        try:
            previous.append(Fernet(part.encode()))
        except (ValueError, TypeError):
            print(f"WARN VAULT_ENCRYPTION_KEY_PREVIOUS[{index}]: not a Fernet key, skipped", file=sys.stderr)
            continue
        seen.add(part)
    if not previous:
        raise ConfigError("VAULT_ENCRYPTION_KEY_PREVIOUS: no valid previous key (nothing to rotate)")
    return Keys(primary=primary, any_key=MultiFernet([primary, *previous]), previous_count=len(previous))


# ── 순수 함수 (단위 테스트 대상) ─────────────────────────────

def _digest(plain: bytes) -> str:
    return hashlib.sha256(plain).hexdigest()


def _encrypt(primary: Fernet, plain: bytes) -> bytes:
    return primary.encrypt(plain)


def _is_current(token: str, keys: Keys) -> bool:
    try:
        keys.primary.decrypt(token.encode())
        return True
    except (InvalidToken, ValueError, TypeError):
        return False


def rotate_token(token: str, keys: Keys, label: str) -> tuple[str, str]:
    """이전키 암호문 → (주키 암호문, 원문 sha256). 호출 전에 _is_current 로 걸러야 한다."""
    try:
        plain = keys.any_key.decrypt(token.encode())
    except (InvalidToken, ValueError, TypeError):
        raise LookupError(label) from None
    new_token = _encrypt(keys.primary, plain)
    try:
        ok = keys.primary.decrypt(new_token) == plain
    except InvalidToken:
        ok = False
    digest = _digest(plain)
    del plain
    if not ok:
        raise VerifyError(label)
    return new_token.decode(), digest


def _json_dict(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def plan_row(spec: TableSpec, row: Mapping[str, Any], keys: Keys) -> RowPlan:
    plan = RowPlan(id=row["id"], status="current")
    undecryptable = False
    try:
        for column in spec.columns:
            token = row.get(column)
            if not isinstance(token, str) or not token or _is_current(token, keys):
                continue
            try:
                plan.updates[column], plan.digests[column] = rotate_token(token, keys, column)
            except LookupError:
                undecryptable = True
        if spec.json_column:
            extra = _json_dict(row.get(spec.json_column))
            if extra:
                moved = dict(extra)
                for key, value in extra.items():
                    if not isinstance(value, str) or not value or _is_current(value, keys):
                        continue
                    try:
                        moved[key], plan.json_digests[key] = rotate_token(value, keys, f"{spec.json_column}.{key}")
                    except LookupError:
                        continue  # 평문 메타데이터 — get_credential 과 같은 규칙.
                if plan.json_digests:
                    plan.updates[spec.json_column] = moved
    except VerifyError:
        plan.status = "failed"
        plan.updates.clear()
        return plan
    if plan.updates:
        plan.status = "rotate"
    elif undecryptable:
        plan.status = "undecryptable"
    return plan


def verify_stored(spec: TableSpec, plan: RowPlan, row: Mapping[str, Any], keys: Keys) -> bool:
    """UPDATE 뒤 다시 읽은 행이 주키 단독으로 풀리고 원문 digest 가 같은지."""
    try:
        for column, digest in plan.digests.items():
            if _digest(keys.primary.decrypt(str(row[column]).encode())) != digest:
                return False
        if plan.json_digests:
            extra = _json_dict(row.get(spec.json_column)) or {}
            for key, digest in plan.json_digests.items():
                if _digest(keys.primary.decrypt(str(extra.get(key, "")).encode())) != digest:
                    return False
    except (InvalidToken, KeyError, ValueError, TypeError):
        return False
    return True


# ── DB ─────────────────────────────────────────────────────

class _Rollback(Exception):
    pass


def _select_sql(spec: TableSpec, for_update: bool) -> str:
    cols = ", ".join(("id", *spec.columns, *((spec.json_column,) if spec.json_column else ())))
    return f"SELECT {cols} FROM {spec.name} ORDER BY id" + (" FOR UPDATE" if for_update else "")


def _update_sql(spec: TableSpec, columns: list[str]) -> str:
    sets = []
    for index, column in enumerate(columns, start=2):
        cast = "::jsonb" if column == spec.json_column else ""
        sets.append(f"{column} = ${index}{cast}")
    return f"UPDATE {spec.name} SET {', '.join(sets)} WHERE id = $1"


async def rotate_table(conn: Any, spec: TableSpec, keys: Keys, apply: bool) -> dict[str, Any]:
    """테이블 하나를 처리하고 요약(건수·id)을 돌려준다. apply 가 아니면 쓰지 않는다."""
    summary: dict[str, Any] = {"table": spec.name, "mode": "apply" if apply else "dry-run"}
    plans: list[RowPlan] = []

    def _count() -> None:
        summary.update(
            rows=len(plans),
            current=sum(p.status == "current" for p in plans),
            rotate=sum(p.status == "rotate" for p in plans),
            undecryptable=sum(p.status == "undecryptable" for p in plans),
            undecryptable_ids=[str(p.id) for p in plans if p.status == "undecryptable"],
            failed_ids=[str(p.id) for p in plans if p.status == "failed"],
        )

    try:
        async with conn.transaction(readonly=not apply):
            rows = await conn.fetch(_select_sql(spec, for_update=apply))
            plans = [plan_row(spec, dict(r), keys) for r in rows]
            _count()
            if summary["failed_ids"]:
                raise _Rollback()
            if not apply:
                summary.update(status="ok", updated=0)
                return summary
            todo = [p for p in plans if p.status == "rotate"]
            for p in todo:
                columns = list(p.updates)
                values = [
                    json.dumps(p.updates[c], ensure_ascii=False) if c == spec.json_column else p.updates[c]
                    for c in columns
                ]
                await conn.execute(_update_sql(spec, columns), p.id, *values)
            if todo:
                stored = {r["id"]: dict(r) for r in await conn.fetch(_select_sql(spec, for_update=False))}
                for p in todo:
                    if p.id not in stored or not verify_stored(spec, p, stored[p.id], keys):
                        p.status = "failed"
                _count()
                if summary["failed_ids"]:
                    raise _Rollback()
            summary.update(status="ok", updated=len(todo))
    except _Rollback:
        summary.update(status="rolled_back", updated=0)
    except Exception as exc:
        # 예외 메시지에 값이 섞일 수 있으므로 타입만 남긴다.
        summary.update(status="rolled_back", updated=0, error=type(exc).__name__)
    return summary


async def _run(args: argparse.Namespace, keys: Keys) -> int:
    import asyncpg

    conn = await asyncpg.connect(args.dsn, timeout=10)
    exit_code = 0
    try:
        names = [args.table] if args.table else list(TABLES)
        for name in names:
            summary = await rotate_table(conn, TABLES[name], keys, apply=args.apply)
            print(json.dumps(summary, ensure_ascii=False), flush=True)
            if summary["status"] != "ok":
                exit_code = 1
    finally:
        await conn.close()
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True, help="기본값. 쓰지 않는다")
    mode.add_argument("--apply", action="store_true", help="검증 통과 행을 UPDATE 한다")
    parser.add_argument("--table", choices=sorted(TABLES), help="이 테이블만 처리")
    parser.add_argument("--dsn", default=os.getenv("DATABASE_URL", ""))
    args = parser.parse_args(argv)

    if not args.dsn:
        print("ERROR DATABASE_URL/--dsn: not set", file=sys.stderr)
        return 2
    try:
        keys = load_keys(os.environ)
    except ConfigError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", "previous_keys": keys.previous_count}), flush=True)
    return asyncio.run(_run(args, keys))


if __name__ == "__main__":
    sys.exit(main())
