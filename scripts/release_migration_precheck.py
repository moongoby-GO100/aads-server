#!/usr/bin/env python3
"""적용 직전 precheck: 마이그레이션이 만들 표·열·색인이 카탈로그에 이미 있으면 중단한다.

배경(2026-11-16 교차검토 id=552): 정방향 SQL 은 IF NOT EXISTS 라 같은 이름의 기존 객체를
건너뛰고도 schema_migrations 에 적용 기록을 남긴다. 그러면 down 가드(적용 기록 확인)가
"원래 있던 객체"를 지울 수 있다. 그래서 적용 전에 "없었음"을 입증해 기록(note)으로 남긴다.

사용: release_migration_precheck.py <migration.sql>
  stdout 마지막 줄: 성공 시 note JSON  {"precheck":"absent","objects":[...]}
  종료코드: 0 전부 없음 / 9 이미 있음(적용 중단) / 8 카탈로그 조회 실패
읽기 전용 SELECT 만 실행한다(운영 DB 에 쓰지 않는다).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_migration_autoverify as av  # noqa: E402


def _lit(v: str) -> str:
    return "'" + v.replace("'", "''") + "'"


def catalog_query(kind: str, sch: str, name: str, col: str) -> str | None:
    """대상이 이미 있으면 1 을 돌려주는 SELECT. 함수는 CREATE OR REPLACE 라 대상이 아니다."""
    if kind == "table":
        return (f"SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                f"WHERE n.nspname={_lit(sch)} AND c.relname={_lit(name)} AND c.relkind IN ('r','p','v','m','f')")
    if kind == "column":
        return (f"SELECT count(*) FROM information_schema.columns WHERE table_schema={_lit(sch)} "
                f"AND table_name={_lit(name)} AND column_name={_lit(col)}")
    if kind == "index":
        return (f"SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                f"WHERE n.nspname={_lit(sch)} AND c.relname={_lit(name)}")
    return None


def precheck(path: Path) -> tuple[int, dict, list[str]]:
    present: list[str] = []
    objects: list[str] = []
    for kind, sch, name, col in av.expected_objects([path]):
        q = catalog_query(kind, sch, name, col)
        if q is None:
            continue
        label = f"{kind} {sch}.{name}" + (f".{col}" if col else "")
        r = av._psql(q + ";", read_only=True, timeout=60)
        out = r.stdout.strip()
        if r.returncode != 0 or not out.isdigit():
            return 8, {}, [f"카탈로그 조회 실패: {label}"]
        objects.append(label)
        if int(out):
            present.append(label)
    if present:
        return 9, {}, present
    return 0, {"precheck": "absent", "objects": objects}, []


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: release_migration_precheck.py <migration.sql>", file=sys.stderr)
        return 2
    rc, note, items = precheck(Path(argv[1]))
    if rc == 9:
        for it in items:
            print(f"PRECHECK-EXISTS {it} 이(가) 이미 있습니다 — 소유권을 입증할 수 없어 적용을 중단합니다", file=sys.stderr)
    elif rc:
        print("PRECHECK-ERROR " + "; ".join(items), file=sys.stderr)
    else:
        print(json.dumps(note, ensure_ascii=False, separators=(",", ":")))
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
