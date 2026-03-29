#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LiteLLM 프록시(aads-litellm) Docker 로그 기반 사용량 요약.
DB/spend API가 비활성화였거나 장애 시에도 동작.

예:
  python3 scripts/litellm_docker_usage_report.py --since-kst '2026-03-29 10:00:00'
  python3 scripts/litellm_docker_usage_report.py --container aads-litellm --since-utc '2026-03-29T01:00:00Z'
"""
import argparse
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

# INFO:     host:port - "POST /v1/messages?beta=true HTTP/1.1" 200 OK
LINE_RE = re.compile(
    r'"POST\s+(/v1/[^\s"]+)\s+HTTP/[^"]+"\s+(\d+)'
)


def parse_since_kst(s: str) -> str:
    """'YYYY-MM-DD HH:MM[:SS]' KST -> Docker --since RFC3339 UTC Z."""
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=KST)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    raise SystemExit("invalid --since-kst (use 'YYYY-MM-DD HH:MM' or with :SS)")


def docker_logs(container, since):
    cmd = ["docker", "logs", container, "--since", since]
    p = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    if p.returncode != 0:
        raise SystemExit("docker logs failed: %s" % (p.stderr or p.stdout))
    return p.stdout + p.stderr


def main():
    ap = argparse.ArgumentParser(description="LiteLLM Docker log usage report")
    ap.add_argument("--container", default="aads-litellm", help="Docker container name")
    ap.add_argument("--since-utc", help="RFC3339 UTC e.g. 2026-03-29T01:00:00Z (= KST 10:00)")
    ap.add_argument("--since-kst", help="KST wall time e.g. '2026-03-29 10:00:00'")
    args = ap.parse_args()

    if bool(args.since_utc) == bool(args.since_kst):
        print("Specify exactly one of --since-utc or --since-kst", file=sys.stderr)
        sys.exit(2)

    since = args.since_utc if args.since_utc else parse_since_kst(args.since_kst)
    raw = docker_logs(args.container, since)

    by_path = defaultdict(int)
    by_status = defaultdict(int)
    claude_hint = 0
    anthropic_messages = 0
    chat_completions = 0

    for line in raw.splitlines():
        if "/v1/messages" in line and "POST" in line:
            anthropic_messages += 1
        if "/v1/chat/completions" in line and "POST" in line:
            chat_completions += 1
        if re.search(r"claude", line, re.I):
            claude_hint += 1
        m = LINE_RE.search(line)
        if m:
            path, status = m.group(1), m.group(2)
            by_path[path] += 1
            by_status[status] += 1

    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S %Z")
    print("=== LiteLLM Docker log usage ===")
    print("container: %s" % args.container)
    print("since (docker): %s" % since)
    print("report_generated_kst: %s" % now_kst)
    print("")
    print("POST lines (substring match):")
    print("  anthropic_messages /v1/messages: %d" % anthropic_messages)
    print("  openai_compat /v1/chat/completions: %d" % chat_completions)
    print("  lines mentioning 'claude' (incl. errors/stack): %d" % claude_hint)
    print("")
    print("Parsed POST status (regex):")
    for st in sorted(by_status.keys(), key=lambda x: int(x)):
        print("  HTTP %s: %d" % (st, by_status[st]))
    print("")
    print("By path (parsed):")
    for path in sorted(by_path.keys(), key=lambda p: -by_path[p])[:25]:
        print("  %s: %d" % (path, by_path[path]))
    if len(by_path) > 25:
        print("  ... (%d paths total)" % len(by_path))


if __name__ == "__main__":
    main()
