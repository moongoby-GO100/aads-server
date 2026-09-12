#!/usr/bin/env python3
"""OHVIS Research Dataset v1 — read-only pilot extractor.

목적
----
AADS/OHVIS 운영 DB에서 Pipeline Runner 작업 30건을 층화 추출해
비식별(de-identified) 연구용 데이터셋 파일럿을 생성한다.

절대 규칙
---------
1. DB 접근은 SELECT 전용이다. INSERT/UPDATE/DELETE/DDL을 수행하지 않는다.
2. 원문 지시서·로그·diff는 그대로 반출하지 않는다. 반드시 redact 후 절단한다.
3. 식별자(job_id, session_id, commit_hash)는 salt + SHA-256 의사식별자로 치환한다.
   salt는 데이터셋 디렉터리 밖(REPO 외부)에 보관하며 산출물에 포함하지 않는다.
4. 출력 직후 자체 PII/시크릿 재스캔을 수행한다. 1건이라도 탐지되면 exit 2로 실패한다.

사용
----
    docker exec aads-server python3 /app/research/ohvis_dataset_v1/extract_pilot.py \
        --n 30 --outdir /app/research/ohvis_dataset_v1/out
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg

KST = timezone(timedelta(hours=9))
SALT_PATH = Path(os.getenv("OHVIS_RESEARCH_SALT_PATH", "/root/.ohvis_research_salt"))
DATASET_VERSION = "ohvis-pilot-v1"

# --------------------------------------------------------------------------
# 1. redaction 규칙 — (이름, 정규식, 치환토큰)
# --------------------------------------------------------------------------
REDACTIONS: list[tuple[str, re.Pattern[str], str]] = [
    ("anthropic_oauth", re.compile(r"sk-ant-oat\d{2}-[A-Za-z0-9_\-]{8,}"), "[REDACTED:ANTHROPIC_OAUTH]"),
    ("anthropic_api", re.compile(r"sk-ant-api\d{2}-[A-Za-z0-9_\-]{8,}"), "[REDACTED:ANTHROPIC_API]"),
    ("openai_key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9]{20,}"), "[REDACTED:LLM_KEY]"),
    ("google_key", re.compile(r"AIza[0-9A-Za-z_\-]{30,}"), "[REDACTED:GOOGLE_KEY]"),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED:GITHUB_TOKEN]"),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"), "[REDACTED:SLACK_TOKEN]"),
    ("telegram_token", re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_\-]{30,}"), "[REDACTED:TELEGRAM_TOKEN]"),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), "[REDACTED:JWT]"),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "[REDACTED:PRIVATE_KEY]"),
    ("bearer", re.compile(r"(?i)\b(?:bearer|authorization:)\s+[A-Za-z0-9._\-]{16,}"), "[REDACTED:BEARER]"),
    ("url_userinfo", re.compile(r"://[^\s/:@]+:[^\s/@]+@"), "://[REDACTED:CREDENTIAL]@"),
    ("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), "[REDACTED:EMAIL]"),
    ("kr_rrn", re.compile(r"\b\d{6}[-\s]?[1-4]\d{6}\b"), "[REDACTED:RRN]"),
    ("kr_phone", re.compile(r"\b(?:\+?82[-\s]?)?01[016789][-\s]?\d{3,4}[-\s]?\d{4}\b"), "[REDACTED:PHONE]"),
    ("account_no", re.compile(r"\b\d{3,6}-\d{2,6}-\d{4,8}\b"), "[REDACTED:ACCOUNT]"),
    ("ipv4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[REDACTED:IP]"),
    ("home_path", re.compile(r"/(?:home|Users)/[A-Za-z0-9._\-]+"), "/[REDACTED:HOME]"),
    ("win_userpath", re.compile(r"[A-Za-z]:\\\\Users\\\\[A-Za-z0-9._\-]+"), "[REDACTED:WINPATH]"),
    ("card_no", re.compile(r"\b(?:\d{4}[-\s]?){3}\d{4}\b"), "[REDACTED:CARD]"),
]

# 내부 도메인/호스트는 조직 식별을 피하기 위해 일반화한다.
DOMAIN_MAP = {
    "aads.newtalk.kr": "[ORG_HOST_A]",
    "newtalk.kr": "[ORG_HOST_A]",
    "moongoby": "[ORG_ACTOR_1]",
}

# 출력 재스캔용 — redaction 이후에도 남으면 실패로 판정한다.
LEAK_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (name, pat) for name, pat, _ in REDACTIONS
    if name not in ("home_path", "win_userpath")
]


def load_salt() -> str:
    """pseudonym salt를 로드한다. 없으면 1회 생성 후 0600으로 저장한다."""
    if SALT_PATH.exists():
        return SALT_PATH.read_text(encoding="utf-8").strip()
    salt = secrets.token_hex(32)
    SALT_PATH.write_text(salt, encoding="utf-8")
    os.chmod(SALT_PATH, 0o600)
    return salt


def pseudo(salt: str, kind: str, value: str | None) -> str | None:
    if value is None or value == "":
        return None
    digest = hashlib.sha256(f"{salt}|{kind}|{value}".encode()).hexdigest()
    return f"{kind}_{digest[:16]}"


def redact(text: str | None) -> tuple[str, dict[str, int]]:
    """텍스트를 비식별화하고 규칙별 적중 횟수를 반환한다."""
    if not text:
        return "", {}
    hits: dict[str, int] = {}
    out = text
    for name, pattern, token in REDACTIONS:
        out, n = pattern.subn(token, out)
        if n:
            hits[name] = hits.get(name, 0) + n
    for raw, token in DOMAIN_MAP.items():
        if raw in out:
            hits[f"domain:{raw}"] = hits.get(f"domain:{raw}", 0) + out.count(raw)
            out = out.replace(raw, token)
    return out, hits


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"…[truncated {len(text) - limit} chars]"


# --------------------------------------------------------------------------
# 2. 파생 라벨 — 사람 라벨링 전 단계의 결정론적 auto label
# --------------------------------------------------------------------------
TASK_ID_RE = re.compile(r"TASK_ID:\s*([A-Z0-9\-]+)")
PRIORITY_RE = re.compile(r"PRIORITY:\s*(P[0-3])")


def auto_labels(row: dict) -> dict:
    status = (row.get("status") or "").lower()
    verdict = (row.get("review_verdict") or "").lower()
    err = (row.get("error_detail") or "").lower()

    # 주의: 'rejected_done'은 코드상(app/api/admin.py:51) done/approved와 같은 종료군으로
    # 집계되지만, 이름은 '반려 후 종료'를 뜻한다. 이 모호성은 자동 라벨로 확정하지 않고
    # needs_human_adjudication=True로 표시해 rater A/B 판정 대상으로 넘긴다.
    if status in ("done", "approved"):
        outcome = "accepted"
    elif status == "rejected_done":
        outcome = "terminal_after_rejection"
    elif status in ("error", "cancelled"):
        outcome = "failed"
    elif status in ("queued", "running", "awaiting_approval", "review_hold"):
        outcome = "in_flight"
    else:
        outcome = "other"

    if not err:
        failure_mode = "none"
    elif "timeout" in err:
        failure_mode = "timeout"
    elif "auth" in err or "rate_limit" in err or "401" in err:
        failure_mode = "auth_or_quota"
    elif "conflict" in err or "git" in err:
        failure_mode = "vcs_conflict"
    elif "crash" in err or "process_died" in err:
        failure_mode = "worker_crash"
    elif "build" in err or "test" in err:
        failure_mode = "build_or_test"
    else:
        failure_mode = "other"

    return {
        "outcome": outcome,
        "review_verdict_norm": verdict or None,
        "failure_mode": failure_mode,
        "reached_commit": bool(row.get("commit_hash")),
        "reached_deploy": row.get("deployed_at") is not None,
        "rework_cycles": int(row.get("cycle") or 0),
        "needs_human_adjudication": outcome == "terminal_after_rejection",
        "human_label": None,          # 이중 라벨링(rater A/B)용 공란
        "human_label_rater": None,
        "label_disagreement": None,
    }


def dur_sec(a, b) -> float | None:
    if not a or not b:
        return None
    return round((b - a).total_seconds(), 1)


def iso_kst(ts) -> str | None:
    if not ts:
        return None
    return ts.astimezone(KST).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# 3. 층화 추출 — project × outcome 층에서 결정론적으로 뽑는다.
# --------------------------------------------------------------------------
SELECT_SQL = """
SELECT job_id, chat_session_id, project, instruction, phase, cycle, max_cycles,
       status, error_detail, priority::text AS priority, size, model, worker_model, actual_model,
       review_verdict, review_score, review_flag_category, review_needs_retry,
       commit_hash, actual_changed_files::text AS actual_changed_files,
       parallel_group, depends_on,
       created_at, started_at, updated_at, completed_at,
       approval_requested_at, approved_at, rejected_at, deployed_at,
       length(coalesce(instruction,''))      AS instruction_len,
       length(coalesce(git_diff,''))         AS git_diff_len,
       length(coalesce(logs::text,''))       AS logs_len,
       left(coalesce(instruction,''), 4000) AS instruction_head,
       left(coalesce(review_feedback,''), 1500) AS review_feedback_head
FROM pipeline_jobs
WHERE created_at >= now() - interval '120 days'
ORDER BY created_at
"""


def stratify(rows: list[dict], n: int) -> list[dict]:
    """project × outcome 층별로 균등 배분하고, 각 층 내부는 job_id 해시 순으로 결정론 추출."""
    buckets: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        key = (r["project"] or "UNKNOWN", auto_labels(r)["outcome"])
        buckets.setdefault(key, []).append(r)
    for items in buckets.values():
        items.sort(key=lambda r: hashlib.sha256(r["job_id"].encode()).hexdigest())

    # 층이 큰 순으로 라운드로빈하여 소수 층도 최소 1건 확보
    order = sorted(buckets, key=lambda k: (-len(buckets[k]), k))
    picked: list[dict] = []
    idx = {k: 0 for k in order}
    while len(picked) < n:
        progressed = False
        for key in order:
            if len(picked) >= n:
                break
            i = idx[key]
            if i < len(buckets[key]):
                picked.append(buckets[key][i])
                idx[key] = i + 1
                progressed = True
        if not progressed:
            break
    return picked


async def fetch(dsn: str) -> list[dict]:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("SET TRANSACTION READ ONLY")  # 세션 레벨 안전장치
    except asyncpg.PostgresError as exc:
        print(f"[warn] SET TRANSACTION READ ONLY 실패(쿼리는 SELECT 전용): {exc}", file=sys.stderr)
    try:
        recs = await conn.fetch(SELECT_SQL)
        return [dict(r) for r in recs]
    finally:
        await conn.close()


def build_record(salt: str, row: dict, redact_stats: dict[str, int]) -> dict:
    head = row.get("instruction_head") or ""
    instr_red, hits = redact(head)
    for k, v in hits.items():
        redact_stats[k] = redact_stats.get(k, 0) + v
    fb_red, fb_hits = redact(row.get("review_feedback_head") or "")
    for k, v in fb_hits.items():
        redact_stats[k] = redact_stats.get(k, 0) + v

    task_id = TASK_ID_RE.search(head)
    prio = PRIORITY_RE.search(head)
    changed = row.get("actual_changed_files")
    changed_n = None
    if isinstance(changed, str) and changed.strip():
        try:
            parsed = json.loads(changed)
        except json.JSONDecodeError:
            parsed = [x for x in changed.replace(",", "\n").split("\n") if x.strip()]
        if isinstance(parsed, (list, dict)):
            changed_n = len(parsed)
    elif isinstance(changed, (list, tuple)):
        changed_n = len(changed)

    return {
        "dataset_version": DATASET_VERSION,
        "case_id": pseudo(salt, "case", row["job_id"]),
        "session_pseudo": pseudo(salt, "sess", str(row.get("chat_session_id") or "")),
        "commit_pseudo": pseudo(salt, "cmt", row.get("commit_hash") or ""),
        "project": row.get("project"),
        "declared_task_id": task_id.group(1) if task_id else None,
        "declared_priority": prio.group(1) if prio else (row.get("priority") or None),
        "declared_size": row.get("size") or None,
        "orchestrator_model": row.get("model") or None,
        "worker_model": row.get("worker_model") or row.get("actual_model") or None,
        "phase_final": row.get("phase"),
        "status_raw": row.get("status"),
        "review_score": float(row["review_score"]) if row.get("review_score") is not None else None,
        "review_flag_category": row.get("review_flag_category") or None,
        "review_needs_retry": row.get("review_needs_retry"),
        "max_cycles": row.get("max_cycles"),
        "parallel_group": pseudo(salt, "pg", row.get("parallel_group") or ""),
        "has_dependency": bool(row.get("depends_on")),
        "metrics": {
            "instruction_len": row.get("instruction_len"),
            "git_diff_len": row.get("git_diff_len"),
            "logs_len": row.get("logs_len"),
            "changed_files_n": changed_n,
            "queue_wait_sec": dur_sec(row.get("created_at"), row.get("started_at")),
            "work_sec": dur_sec(row.get("started_at"), row.get("completed_at") or row.get("updated_at")),
            "approval_wait_sec": dur_sec(row.get("approval_requested_at"),
                                         row.get("approved_at") or row.get("rejected_at")),
            "total_lead_sec": dur_sec(row.get("created_at"),
                                      row.get("completed_at") or row.get("updated_at")),
        },
        "timestamps_kst": {
            "created": iso_kst(row.get("created_at")),
            "started": iso_kst(row.get("started_at")),
            "completed": iso_kst(row.get("completed_at")),
            "deployed": iso_kst(row.get("deployed_at")),
        },
        "labels": auto_labels(row),
        "text": {
            "instruction_redacted": clip(instr_red, 1200),
            "review_feedback_redacted": clip(fb_red, 600),
            "error_detail_redacted": clip(redact(row.get("error_detail"))[0], 200),
        },
        "provenance": {
            "source_table": "pipeline_jobs",
            "extractor": "research/ohvis_dataset_v1/extract_pilot.py",
            "extracted_at_kst": datetime.now(KST).isoformat(timespec="seconds"),
            "access_mode": "read_only_select",
        },
    }


def rescan(path: Path) -> list[dict]:
    """출력 파일을 다시 읽어 잔존 시크릿/PII를 탐지한다."""
    findings: list[dict] = []
    text = path.read_text(encoding="utf-8")
    for line_no, line in enumerate(text.splitlines(), start=1):
        for name, pattern in LEAK_PATTERNS:
            m = pattern.search(line)
            if m:
                findings.append({"line": line_no, "rule": name, "sample_len": len(m.group(0))})
    return findings


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--outdir", default="/app/research/ohvis_dataset_v1/out")
    args = ap.parse_args()

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL 미설정", file=sys.stderr)
        return 1

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    salt = load_salt()

    rows = await fetch(dsn)
    picked = stratify(rows, args.n)

    redact_stats: dict[str, int] = {}
    records = [build_record(salt, r, redact_stats) for r in picked]

    jsonl = outdir / "pilot_v1.jsonl"
    with jsonl.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    findings = rescan(jsonl)

    strata: dict[str, int] = {}
    for rec in records:
        key = f"{rec['project']}/{rec['labels']['outcome']}"
        strata[key] = strata.get(key, 0) + 1

    report = {
        "dataset_version": DATASET_VERSION,
        "generated_at_kst": datetime.now(KST).isoformat(timespec="seconds"),
        "population_rows_120d": len(rows),
        "sampled": len(records),
        "strata": strata,
        "redaction_hits": redact_stats,
        "leak_findings": findings,
        "leak_clean": len(findings) == 0,
        "fields_per_record": len(json.loads(json.dumps(records[0]))) if records else 0,
        "labeling_status": {
            "auto_labeled": len(records),
            "human_labeled": 0,
            "raters_planned": 2,
            "agreement_metric": "Cohen's kappa (미측정)",
        },
    }
    (outdir / "pii_scan_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if findings:
        print(f"LEAK DETECTED: {len(findings)}건 — 산출물을 배포하지 마라.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
