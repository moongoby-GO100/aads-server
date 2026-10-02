#!/usr/bin/env python3
"""정본 게이트(그림자) — pre-commit 진입점. 경고와 로그만, 종료코드는 항상 0.

스테이징된 docs/(prd|plans|design|contracts)/** 추가·수정 파일을 찾아
  1) 노란색 경고를 stderr 로 내고
  2) <git-dir>/canonical_gate.log 에 고정 형식으로 append 한다.

DB 에 접근하지 않는다(commit hook 이 DB 지연으로 느려지거나 막히면 안 된다).
그래서 판정은 항상 `unknown` 이고, 정본 등록 여부는 집계 단계에서 로그의 경로를
project_document_revisions.source_path 와 대조해 가린다(reports/…RESULT.md 참고).

로그 한 줄(탭 구분, 9칸, 형식 버전 CGv1):
  CGv1 \t ts(UTC ISO8601) \t commit \t project \t path \t verdict \t mode \t status(A|M) \t blob_sha

CANONICAL_GATE_MODE=off 이면 아무것도 하지 않는다. enforce 는 shadow 와 동일하다.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import subprocess
import sys

# app/services/canonical_gate.py 의 CANDIDATE_PATH_RE 와 같아야 한다.
CANDIDATE_RE = re.compile(r"^docs/(prd|plans|design|contracts)/")
LOG_NAME = "canonical_gate.log"
FORMAT_TAG = "CGv1"
YELLOW = "\033[1;33m"
NC = "\033[0m"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, timeout=5, check=True,
    ).stdout


def candidates() -> list[tuple[str, str]]:
    """[(status, path)] — 스테이징된 추가·수정 중 후보 경로."""
    out = _git("diff", "--cached", "--name-status", "--diff-filter=AM", "-z")
    parts = out.split("\0")
    found = []
    for i in range(0, len(parts) - 1, 2):
        status, path = parts[i], parts[i + 1]
        if CANDIDATE_RE.match(path):
            found.append((status[:1], path))
    return found


def format_line(ts: str, project: str, path: str, mode: str, status: str, blob: str) -> str:
    fields = [FORMAT_TAG, ts, "commit", project, path, "unknown", mode, status, blob]
    return "\t".join(f.replace("\t", " ").replace("\n", " ") for f in fields)


def main() -> int:
    mode = (os.getenv("CANONICAL_GATE_MODE") or "shadow").strip().lower()
    if mode == "off":
        return 0
    if mode not in ("shadow", "enforce"):
        mode = "shadow"
    project = os.getenv("CANONICAL_GATE_PROJECT") or "AADS"
    found = candidates()
    if not found:
        return 0
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = []
    for status, path in found:
        try:
            blob = _git("rev-parse", ":" + path).strip()
        except Exception:
            blob = "-"
        lines.append(format_line(ts, project, path, mode, status, blob))
    for _status, path in found:
        sys.stderr.write(
            f"  {YELLOW}[정본 게이트] {path} — 정본(project_document_heads) 등록 여부를 "
            f"hook 에서는 확인하지 않습니다. 정본 없이 만든 문서면 등록하세요 (그림자 모드: 차단 없음){NC}\n"
        )
    log_path = os.path.join(_git("rev-parse", "--git-dir").strip(), LOG_NAME)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # fail-open: 어떤 오류도 커밋을 막지 않는다
        sys.stderr.write(f"  [정본 게이트] 건너뜀 ({type(exc).__name__})\n")
    sys.exit(0)
