#!/usr/bin/env python3
"""오류 사전 — 증상을 원인·예방으로 바꾼다.

2026-09-14, 세 세션이 같은 실패를 "러너 계정 문제" 로 보고했다. 실제 원인은
호스트/컨테이너 경로 불일치였고, 로그에는 Codex 시작 배너만 남아 있었다.
아무도 게을러서가 아니라, **증상에서 원인으로 가는 길이 없어서** 벌어진 일이다.

`ohvis_wiki_error_book` 테이블은 error_key·symptom·root_cause·prevention 까지
갖추고 있었으나 0행이었다. 계획 문서에만 등장하고 아무도 쓰지 않았다.

이 도구가 그 자리를 채운다.

    error_book.py match <파일|-->      오류 텍스트에서 알려진 원인을 찾는다
    error_book.py register --key ...   새 항목을 등록/갱신한다
    error_book.py list                 등록된 항목

match 가 핵심이다. 실패 지점에서 이걸 부르면 "알 수 없는 오류" 대신
"알려진 오류 X — 원인 Y — 예방 Z" 가 로그에 남는다.

서명(signature)은 metadata.signatures 에 정규식 목록으로 둔다. 증상 문구는
버전마다 조금씩 달라지므로 완전 일치를 요구하지 않는다.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

PG_CONTAINER = "aads-postgres"
PG_PASSWORD = "aads2026secure"


def psql(sql: str) -> str:
    proc = subprocess.run(
        ["docker", "exec", "-i", "-e", f"PGPASSWORD={PG_PASSWORD}", PG_CONTAINER,
         "psql", "-U", "aads", "-d", "aads", "-At", "-F", "\x1f", "-f", "-"],
        input=sql, text=True, capture_output=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "")[:300])
    return (proc.stdout or "").strip()


def lit(v) -> str:
    return "'" + str(v if v is not None else "").replace("'", "''") + "'"


def load_entries() -> list[dict]:
    out = psql(
        "SELECT error_key, symptom, root_cause, prevention, recurrence_count, status, metadata::text "
        "FROM ohvis_wiki_error_book WHERE status <> 'retired' ORDER BY error_key;"
    )
    entries = []
    for line in out.splitlines():
        f = line.split("\x1f")
        if len(f) < 7:
            continue
        try:
            meta = json.loads(f[6] or "{}")
        except ValueError:
            meta = {}
        entries.append({
            "error_key": f[0], "symptom": f[1], "root_cause": f[2],
            "prevention": f[3], "recurrence_count": int(f[4] or 0),
            "status": f[5], "signatures": meta.get("signatures") or [],
        })
    return entries


def do_match(text: str, bump: bool) -> int:
    entries = load_entries()
    hits = []
    for e in entries:
        for sig in e["signatures"]:
            try:
                if re.search(sig, text, re.I | re.S):
                    hits.append((e, sig))
                    break
            except re.error:
                if sig.lower() in text.lower():
                    hits.append((e, sig))
                    break
    if not hits:
        print("알려진 오류 없음")
        return 1
    for e, sig in hits:
        print(f"알려진 오류: {e['error_key']}  (재발 {e['recurrence_count']}회)")
        print(f"  증상   {e['symptom']}")
        print(f"  원인   {e['root_cause']}")
        print(f"  예방   {e['prevention']}")
        if bump:
            psql(
                "UPDATE ohvis_wiki_error_book "
                "SET recurrence_count = recurrence_count + 1, updated_at = NOW() "
                f"WHERE error_key = {lit(e['error_key'])};"
            )
    return 0


def do_register(a) -> int:
    meta = json.dumps({"signatures": a.signature}, ensure_ascii=False)
    psql(
        "INSERT INTO ohvis_wiki_error_book "
        "(project, error_key, symptom, root_cause, prevention, status, metadata) VALUES ("
        f"{lit(a.project)}, {lit(a.key)}, {lit(a.symptom)}, {lit(a.cause)}, "
        f"{lit(a.prevention)}, 'active', {lit(meta)}::jsonb) "
        "ON CONFLICT (project, error_key) DO UPDATE SET "
        "  symptom=EXCLUDED.symptom, root_cause=EXCLUDED.root_cause, "
        "  prevention=EXCLUDED.prevention, metadata=EXCLUDED.metadata, updated_at=NOW();"
    )
    print(f"등록: {a.key}")
    return 0


def do_list() -> int:
    for e in load_entries():
        print(f"  {e['error_key']:<38} 재발 {e['recurrence_count']:>3}회  서명 {len(e['signatures'])}개")
        print(f"      {e['symptom'][:90]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="오류 사전")
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("match", help="오류 텍스트에서 알려진 원인 찾기")
    m.add_argument("path", help="오류 파일 경로, 또는 - (표준입력)")
    m.add_argument("--bump", action="store_true", help="일치 시 재발 횟수 증가")

    r = sub.add_parser("register", help="항목 등록/갱신")
    r.add_argument("--key", required=True)
    r.add_argument("--symptom", required=True)
    r.add_argument("--cause", default="")
    r.add_argument("--prevention", default="")
    r.add_argument("--project", default="AADS")
    r.add_argument("--signature", action="append", default=[], required=True)

    sub.add_parser("list", help="등록 목록")
    a = ap.parse_args()

    if a.cmd == "match":
        text = sys.stdin.read() if a.path == "-" else open(a.path, encoding="utf-8", errors="replace").read()
        return do_match(text, a.bump)
    if a.cmd == "register":
        return do_register(a)
    return do_list()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:      # 사전 조회 실패가 호출부를 막으면 안 된다
        print(f"error_book 실패: {str(exc)[:200]}", file=sys.stderr)
        sys.exit(2)
