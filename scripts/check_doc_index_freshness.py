#!/usr/bin/env python3
"""문서 색인 신선도 감시 — AADS-OHVIS M8 (2026-09-19).

왜 별도 감시가 필요한가.
  index_docs.py 의 `index` 는 **sha256 가 바뀐 문서만** 다시 넣는다
  (cmd_index: `changed = [d for d in docs if known.get(path) != sha]`).
  indexed_at 은 INSERT 기본값 now() 라서, 정상적으로 돌았어도 바뀐 문서가
  없으면 MAX(indexed_at) 이 그대로다. 2026-09-19 00:47 CEST 실측에서
  "변경/신규 0개 (그대로 867개 건너뜀)" 으로 삽입 0건이었다.
  즉 **MAX(indexed_at) 하나로는 "색인이 돌았는가" 를 판정할 수 없다.**
  돈 것과 안 돈 것이 같은 값을 낸다.

그래서 두 가지를 같이 기록한다.
  ran_h    — 마지막 색인 *실행* 이 몇 시간 전인가 (로그의 === RUN 헤더)
  fresh_h  — MAX(indexed_at) 이 몇 시간 전인가 (완료기준 지표)

판정은 ran_h 로 한다. fresh_h 는 같이 남겨 3일 연속 기준의 근거로 쓴다.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from index_docs import psql  # noqa: E402

RUN_LOG = os.getenv("DOC_INDEX_RUN_LOG", "/var/log/index_docs.log")
MAX_AGE_H = float(os.getenv("DOC_INDEX_MAX_AGE_H", "24"))
KST = dt.timezone(dt.timedelta(hours=9))
RUN_RE = re.compile(r"^=== RUN (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (\S+)")


def last_run_utc() -> dt.datetime | None:
    """로그의 마지막 `=== RUN` 헤더 시각. 호스트 TZ 기준이라 date 로 환산한다."""
    try:
        with open(RUN_LOG, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        return None
    for line in reversed(lines):
        m = RUN_RE.match(line.strip())
        if not m:
            continue
        # 호스트 로컬시각 문자열 → epoch. TZ 이름 해석은 date 에 맡긴다.
        out = subprocess.run(
            ["date", "-d", m.group(1), "+%s"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return None
        return dt.datetime.fromtimestamp(int(out.stdout.strip()), tz=dt.timezone.utc)
    return None


def main() -> int:
    now = dt.datetime.now(dt.timezone.utc)
    raw = psql("SELECT max(indexed_at) FROM doc_chunks;").strip()
    if raw:
        newest = dt.datetime.fromisoformat(raw)
        fresh_h = (now - newest).total_seconds() / 3600
        fresh_s = f"{fresh_h:.1f}h"
    else:
        fresh_h, fresh_s = float("inf"), "none"

    run = last_run_utc()
    if run is None:
        ran_h, ran_s = float("inf"), "none"
    else:
        ran_h = (now - run).total_seconds() / 3600
        ran_s = f"{ran_h:.1f}h"

    ok = ran_h <= MAX_AGE_H
    verdict = "OK" if ok else "STALE"
    print(
        f"{now.astimezone(KST):%F %T} KST {verdict} "
        f"ran={ran_s} fresh={fresh_s} limit={MAX_AGE_H:.0f}h",
        flush=True,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
