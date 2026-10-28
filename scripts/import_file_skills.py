#!/usr/bin/env python3
"""SKILL.md 파일 스킬을 ops_skill_library 로 멱등 적재한다 (PRD CAT-1/2).

    import_file_skills.py              # = --dry-run (기본). DB 를 읽기만 한다.
    import_file_skills.py --apply      # 실제 반영 (승인된 배포 단계에서만)

- 정본은 slug(=frontmatter name, 없으면 디렉터리명) 이다.
- 없으면 insert, 있으면 **내용을 절대 덮어쓰지 않는다**. 저장된 content_sha256 과
  파일 해시가 다르면 metadata.file_drift=true 만 기록한다(다시 일치하면 false).
- metadata 에 source_path·content_sha256 을 저장한다. 기본 risk_tier=read, enabled=true.
- DB 접속은 error_book.py 와 같은 psql 경로를 쓴다(서버별 사본을 만들지 않는다).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

from app.services.skill_file_import import (  # noqa: E402
    CANONICAL_MARK,
    plan_import,
    scan_skill_files,
)

DEFAULT_TENANT = "00000000-0000-0000-0000-000000000000"


def lit(value) -> str:
    return "'" + str("" if value is None else value).replace("'", "''") + "'"


def load_existing(psql) -> dict[str, dict]:
    out = psql("SELECT slug, metadata::text FROM ops_skill_library ORDER BY slug;")
    rows: dict[str, dict] = {}
    for line in out.splitlines():
        slug, _, meta = line.partition("\x1f")
        try:
            rows[slug] = {"metadata": json.loads(meta or "{}")}
        except ValueError:
            rows[slug] = {"metadata": {}}
    return rows


def build_sql(plan: list[dict]) -> str:
    statements = ["BEGIN;"]
    for item in plan:
        if item["action"] == "insert":
            meta = json.dumps({
                "source_path": item["source_path"],
                "content_sha256": item["content_sha256"],
                "canonical": CANONICAL_MARK,
                "file_drift": False,
            }, ensure_ascii=False)
            statements.append(
                "INSERT INTO ops_skill_library"
                " (tenant_id,slug,title,description,risk_tier,enabled,source_path,metadata)"
                f" VALUES ({lit(DEFAULT_TENANT)}::uuid,{lit(item['slug'])},{lit(item['title'])},"
                f"{lit(item['description'])},{lit(item['risk_tier'])},TRUE,"
                f"{lit('file-skill:' + item['slug'])},{lit(meta)}::jsonb)"
                " ON CONFLICT (slug) DO NOTHING;"
            )
        elif item["action"] in ("drift", "resync_flag"):
            flag = "true" if item["action"] == "drift" else "false"
            statements.append(
                "UPDATE ops_skill_library SET metadata=jsonb_set(metadata,'{file_drift}',"
                f"'{flag}'::jsonb,true) WHERE slug={lit(item['slug'])}"
                " AND metadata->>'source_path' IS NOT NULL;"
            )
    statements.append("COMMIT;")
    return "\n".join(statements)


def render(plan: list[dict], *, db_available: bool, apply: bool) -> str:
    lines = [f"mode={'apply' if apply else 'dry-run'} db={'ok' if db_available else 'unavailable'}"
             f" entries={len(plan)}"]
    for item in plan:
        lines.append(f"  {item['action']:<15} {item['slug']:<28} risk={item['risk_tier']} "
                     f"sha={item['content_sha256'][:12]} {item['source_path']}")
    counts: dict[str, int] = {}
    for item in plan:
        counts[item["action"]] = counts.get(item["action"], 0) + 1
    lines.append("summary " + json.dumps(counts, sort_keys=True))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="기본값. DB 를 변경하지 않는다")
    group.add_argument("--apply", action="store_true", help="계획을 실제로 반영한다")
    parser.add_argument("--dir", action="append", type=Path, help="스킬 디렉터리(반복 가능)")
    args = parser.parse_args(argv)

    entries = scan_skill_files(args.dir)
    db_available = True
    if not shutil.which("psql"):
        os.environ.pop("PGHOST", None)  # 호스트에 psql 이 없으면 docker exec 경로(error_book 기본)를 쓴다
    try:
        import error_book

        existing = load_existing(error_book.psql)
    except Exception as exc:  # noqa: BLE001 - dry-run 은 DB 없이도 목록을 보여준다
        if args.apply:
            print(f"DB 접속 실패: {exc}", file=sys.stderr)
            return 2
        db_available, existing = False, {}
    plan = plan_import(entries, existing)
    print(render(plan, db_available=db_available, apply=args.apply))
    if not args.apply:
        return 0
    error_book.psql(build_sql(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
