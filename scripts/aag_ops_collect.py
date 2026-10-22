#!/usr/bin/env python3
"""AAG S2 운영 관측 수집기 (읽기 전용). 기본은 **계획만 출력**하고 아무것도 실행하지 않는다.

저장소 그래프와 운영 서버 상태를 섞지 않기 위해, 이 도구는 `crontab -l` 과
`systemctl list-timers --all --no-pager` 의 출력만 관측 문서(JSON)로 만든다.
쓰기·재시작·원격 접속은 하지 않는다. 적재 대상 테이블은 aag_ops_observations 이다.

    # 무엇을 수집하고 어떤 형식으로 싣는지 보기 (실행 없음)
    python3 scripts/aag_ops_collect.py --plan

    # 이미 확보한 출력 파일로 관측 문서 만들기 (명령 실행 없음)
    python3 scripts/aag_ops_collect.py --project AADS --host contabo116 \\
        --crontab-file crontab.txt --timers-file timers.txt --out obs.json

    # 이 서버에서 읽기 전용 명령 실행 (활성화 절차에서만 쓴다)
    python3 scripts/aag_ops_collect.py --project AADS --host "$(hostname)" --execute --out obs.json

    # 관측 문서를 DB 에 적재 (멱등: 같은 내용이면 행이 늘지 않는다)
    python3 scripts/aag_ops_collect.py --from-json obs.json --load

종료코드: 0=성공 / 1=적재·입력 실패 / 2=사용법 오류.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools.aag import ops_observation as obs  # noqa: E402

PG_CONTAINER = os.getenv("AAG_PG_CONTAINER", "aads-postgres")
PG_USER = os.getenv("AAG_PG_USER", "aads")
PG_DB = os.getenv("AAG_PG_DB", "aads")


def plan_text() -> str:
    lines = ["[AAG ops] 수집 계획 (읽기 전용, 이 모드에서는 실행하지 않는다)"]
    for name, argv in obs.COLLECT_COMMANDS.items():
        lines.append("  - %-8s %s  (timeout %ds)" % (name, " ".join(argv), obs.COMMAND_TIMEOUT_SEC))
    lines += [
        "[AAG ops] 적재 형식: schema=%s" % obs.SCHEMA_VERSION,
        "  project, host, collected_at, command_status{crontab,timers}.ok, "
        "crontab{entries[],env,commented_entries}, timers[{unit,activates,when}], content_digest",
        "[AAG ops] 적재 테이블: aag_ops_observations (project, host, collected_at, content_digest 로 멱등)",
        "[AAG ops] 저장소 그래프(aag_graph_snapshots_v2)에는 합치지 않는다.",
    ]
    return "\n".join(lines)


def load_sql(doc: dict) -> str:
    return "BEGIN;\n%sCOMMIT;\n" % obs.insert_statement(doc)


def run_psql(sql: str) -> int:
    try:
        proc = subprocess.run(
            ["docker", "exec", "-i", PG_CONTAINER,
             "psql", "-v", "ON_ERROR_STOP=1", "-q", "-U", PG_USER, "-d", PG_DB],
            input=sql, universal_newlines=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print("psql 실행 실패: %s" % exc, file=sys.stderr)
        return 1
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        return 1
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="AAG 운영 관측 수집기 (읽기 전용)")
    ap.add_argument("--plan", action="store_true", help="수집 계획만 출력(기본)")
    ap.add_argument("--project", default="AADS")
    ap.add_argument("--host", default="")
    ap.add_argument("--crontab-file", help="`crontab -l` 출력 파일")
    ap.add_argument("--timers-file", help="`systemctl list-timers --all --no-pager` 출력 파일")
    ap.add_argument("--execute", action="store_true", help="이 서버에서 읽기 전용 명령을 실행")
    ap.add_argument("--from-json", help="이미 만든 관측 문서")
    ap.add_argument("--out", help="관측 문서를 쓸 파일")
    ap.add_argument("--sql", action="store_true", help="적재 SQL 을 표준출력으로")
    ap.add_argument("--load", action="store_true", help="docker exec psql 로 적재")
    args = ap.parse_args(argv)

    if args.plan or not (args.execute or args.crontab_file or args.timers_file or args.from_json):
        print(plan_text())
        return 0

    if args.from_json:
        try:
            doc = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print("관측 문서를 읽지 못했다: %s" % exc, file=sys.stderr)
            return 1
    else:
        if not args.host:
            print("--host 가 필요하다", file=sys.stderr)
            return 2
        errors: dict = {}
        if args.execute:
            outputs, errors = obs.collect()
        else:
            outputs = {}
            for name, path in (("crontab", args.crontab_file), ("timers", args.timers_file)):
                if not path:
                    outputs[name] = None
                    errors[name] = "입력 파일 없음"
                    continue
                try:
                    outputs[name] = Path(path).read_text(encoding="utf-8")
                except OSError as exc:
                    outputs[name] = None
                    errors[name] = "%s: %s" % (type(exc).__name__, exc)
        doc = obs.build_observation(
            args.project, args.host, datetime.now(timezone.utc).isoformat(), outputs, errors,
        )

    problems = obs.validate_observation(doc)
    if problems:
        print("관측 문서 검증 실패: " + "; ".join(problems), file=sys.stderr)
        return 1
    if args.out:
        Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ok = {k: v["ok"] for k, v in doc["command_status"].items()}
    print("[AAG ops] project=%s host=%s crontab=%d timers=%d ok=%s digest=%s" % (
        doc["project"], doc["host"], len(doc["crontab"]["entries"]), len(doc["timers"]), ok,
        doc["content_digest"][:12]))
    if args.sql:
        sys.stdout.write(load_sql(doc))
    if args.load:
        return run_psql(load_sql(doc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
