#!/usr/bin/env python3
"""diff 절단만으로 review_failed 가 된 작업을 review_hold(REVIEW_DIFF_TRUNCATED)로 되돌린다.

배경: 러너는 diff 를 45000B 에서 자른다. 리뷰어가 "잘려서 확인 불가"만 사유로 반려하면
review_failed 가 되어 원 세션이 판정할 길이 없었다(GO100 runner-4105e588·runner-aa48c16d).
이후 작업은 리뷰어의 구조화 근거(issues[].basis)로 자동 분류되지만, 과거 작업에는 basis 가
없다. 그래서 이 도구는 **과거 작업을 자동으로 고르지 않는다** — `--job <id>` 로 사람이
지정한 작업만 대상이다. `--job` 이 없으면 0건을 선택하고 아무것도 읽지도 쓰지도 않는다.

    # 기본은 dry-run: 대상 조회만 한다
    restore_truncated_review_failed.py --job runner-aa48c16d
    # 쓰기는 --apply 일 때만
    restore_truncated_review_failed.py --job runner-aa48c16d --apply

복구한 작업은 review_flag_category=REVIEW_DIFF_TRUNCATED 이므로 자동 재검수(스위퍼)에서
제외되고, pipeline_review_adjudicate 가 retry_count 임계값 없이 바로 받는다.
SHA·diff hash 검증은 adjudicate 쪽에서 그대로 한다.

DB 접속은 scripts/error_book.py 와 같은 규약(PGHOST 터널 또는 aads-postgres 컨테이너)을 쓴다.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

CATEGORY = "REVIEW_DIFF_TRUNCATED"
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
# capture_job_diff_text 가 자를 때 diff 끝에 남기는 고정 표지(줄 맨앞).
_MARKER_SQL_RE = r"(^|\n)# \[TRUNCATED\] 상한 [0-9]+바이트"
_NOTE = "[복구] 구조화 근거 없음, 사람 지정 — diff 절단 반려를 review_hold(REVIEW_DIFF_TRUNCATED)로 되돌림, 원 세션 판정 대기"


def select_job_ids(requested: list[str] | None) -> list[str]:
    """--job 으로 명시한 id 만 대상이다. 없으면 빈 목록(자동 선택 금지). 형식이 틀리면 ValueError."""
    ids: list[str] = []
    for raw in requested or []:
        job_id = (raw or "").strip()
        if not _JOB_ID_RE.match(job_id):
            raise ValueError(f"유효하지 않은 job_id: {raw!r}")
        if job_id not in ids:
            ids.append(job_id)
    return ids


def build_select_sql(job_ids: list[str]) -> str:
    quoted = ",".join(f"'{job_id}'" for job_id in job_ids)
    return (
        "SELECT job_id, status, COALESCE(phase,''), COALESCE(review_verdict,''), "
        "COALESCE(review_flag_category,''), COALESCE(commit_hash,''), "
        "(COALESCE(git_diff,'') ~ E'" + _MARKER_SQL_RE.replace("\\", "\\\\") + "') AS has_marker, "
        "length(COALESCE(git_diff,'')) "
        f"FROM pipeline_jobs WHERE job_id IN ({quoted}) ORDER BY job_id;"
    )


def build_update_sql(job_id: str) -> str:
    """상태 조건을 WHERE 에 다시 건다 — 조회와 쓰기 사이에 바뀐 작업은 건드리지 않는다."""
    note = _NOTE.replace("'", "''")
    return (
        "UPDATE pipeline_jobs SET status='review_hold', phase='review_hold', "
        f"review_flag_category='{CATEGORY}', review_needs_retry=FALSE, review_request_id=NULL, "
        "error_detail='review_infra_failed: category=" + CATEGORY + " source=restore_truncated_review_failed', "
        "review_feedback=COALESCE(review_feedback,'') || E'\\n' || '" + note + "', updated_at=NOW() "
        f"WHERE job_id='{job_id}' AND status='error' AND phase='review_failed' "
        "AND review_verdict='REQUEST_CHANGES' AND COALESCE(commit_hash,'') <> '' "
        "AND COALESCE(git_diff,'') <> '' RETURNING job_id;"
    )


def classify(row: list[str]) -> tuple[bool, str]:
    """(복구 가능 여부, 사유). 조회된 한 행에 대한 판정."""
    job_id, status, phase, verdict, category, commit, has_marker, diff_len = row
    if status != "error" or phase != "review_failed":
        return False, f"상태가 error/review_failed 가 아님 (status={status} phase={phase})"
    if verdict != "REQUEST_CHANGES":
        return False, f"review_verdict 가 REQUEST_CHANGES 가 아님 ({verdict or '없음'})"
    if not commit:
        return False, "commit_hash 없음 — adjudicate 가 SHA 를 검증할 수 없다"
    if int(diff_len or 0) == 0:
        return False, "저장된 git_diff 없음"
    if has_marker not in ("t", "true", "True"):
        return False, "git_diff 에 절단 표지(# [TRUNCATED] 상한 …)가 없음 — 절단 반려가 아니다"
    return True, "복구 가능 (구조화 근거 없음, 사람 지정)"


def _psql(sql: str) -> str:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import error_book  # 같은 접속 규약 — 서버별 사본을 만들지 않는다

    argv, env = error_book._psql_argv()
    proc = subprocess.run(
        argv + ["-At", "-F", "|", "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True, text=True, env=env, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip()[:500])
    return proc.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--job", action="append", default=[], metavar="JOB_ID",
                        help="복구할 작업(반복 지정 가능). 없으면 0건 — 자동 선택하지 않는다")
    parser.add_argument("--apply", action="store_true", help="실제로 쓴다(기본은 dry-run)")
    args = parser.parse_args(argv)

    try:
        job_ids = select_job_ids(args.job)
    except ValueError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    if not job_ids:
        print("대상 0건 — 과거 작업은 구조화 근거가 없어 자동 선택하지 않는다. --job <id> 로 지정하라.")
        return 0

    mode = "APPLY" if args.apply else "DRY-RUN"
    try:
        out = _psql(build_select_sql(job_ids))
    except Exception as exc:  # noqa: BLE001 - 접속 실패는 사유를 보이고 종료코드로 알린다
        print(f"DB 조회 실패: {exc}", file=sys.stderr)
        return 3
    rows = {line.split("|")[0]: line.split("|") for line in out.splitlines() if line.strip()}
    restorable: list[str] = []
    for job_id in job_ids:
        row = rows.get(job_id)
        if row is None or len(row) != 8:
            print(f"[{mode}] {job_id}: 작업 없음")
            continue
        ok, reason = classify(row)
        print(f"[{mode}] {job_id}: {'대상' if ok else '제외'} — {reason}")
        if ok:
            restorable.append(job_id)

    if not args.apply:
        print(f"dry-run: 복구 가능 {len(restorable)}건 (쓰기 없음). 적용하려면 --apply")
        return 0
    restored = 0
    for job_id in restorable:
        if _psql(build_update_sql(job_id)).strip():
            restored += 1
            print(f"[APPLY] {job_id}: review_hold/{CATEGORY} 로 복구")
    print(f"apply: {restored}/{len(restorable)}건 복구")
    return 0


if __name__ == "__main__":
    sys.exit(main())
