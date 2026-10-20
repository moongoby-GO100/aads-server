"""R-DOC M5 일일 정본 등록률 계측 — 지표 A(문서 단위 등록률)와 지표 B(게이트 ok 비율)를 분리해 센다.

A = (신규 docs/reports 원문 중 정본에 내용 일치로 등록된 수) / (신규 docs/reports 원문 수)
    - 신규는 git 에서 파일이 처음 추가된 커밋의 KST 날짜로 센다.
    - 등록은 같은 tenant·project 의 revision 에서 source_path 와 content_hash(sha256) 가 모두 맞을 때만 센다.
B = canonical_gate_events 중 verdict=ok 의 비율. 문서 수가 아니라 게이트 이벤트 수라 A 와 섞지 않는다.

불변식
  - 분모 0 은 rate=None(N/A) 이다. 0% 도 100% 도 아니다.
  - 수집하지 않은 날짜는 행이 없다. 행이 없는 날짜를 0% 나 성공으로 채우지 않는다(coverage_report 가 not_collected 로 보고).
  - 같은 (tenant, project, 날짜, 지표) 는 한 행이다. 다시 수집해도 행이 늘지 않는다.
  - 순수 계산만 한다. DB 쓰기는 upsert_rows 한 곳이고, 호출하는 쪽(scripts/rdoc_registration_collect.py)이 --apply 일 때만 부른다.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import re
import subprocess
import uuid
from collections import Counter, defaultdict
from typing import Any, Iterable, Optional

KST = _dt.timezone(_dt.timedelta(hours=9))

METRIC_A = "A_new_doc_registration"
METRIC_B = "B_gate_ok_ratio"
METRICS = (METRIC_A, METRIC_B)

STATUS_MEASURED = "measured"
STATUS_NOT_APPLICABLE = "not_applicable"

QUALITY_ON_TIME = "on_time"
QUALITY_LATE = "late_backfill"
QUALITY_PARTIAL = "partial_day"
QUALITY_UNVERIFIED = "unverified_source"
STATE_NOT_COLLECTED = "not_collected"

COLLECTOR_VERSION = "rdoc-m5-v1"
REQUIRED_CONSECUTIVE_DAYS = 7
MAX_LISTED_PATHS = 50

EXCL_NON_MARKDOWN = "non_markdown_asset"
EXCL_EMPTY = "empty_file"
EXCL_DUP_CONTENT = "duplicate_content"
EXCL_READD = "readded_path"

REPORT_PATH_RE = re.compile(r"^docs/reports/.+")
_MARKDOWN_RE = re.compile(r"\.md$")
_FILENAME_PROJECT_RE = re.compile(r"^\d{8}_([A-Z0-9][A-Z0-9_-]{0,63})_")


# ── tenant fail-closed ────────────────────────────────────────────────


def parse_tenant_uuid(value: Optional[str], what: str) -> Optional[str]:
    """None 은 None. 값이 있으면 UUID 여야 하고(빈 문자열 포함) 정규화(소문자 하이픈형)해 돌려준다."""
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, AttributeError, TypeError):
        raise ValueError(f"{what} 가 UUID 가 아님 — 수집/조회를 하지 않는다") from None


def resolve_trusted_tenant(trusted: Optional[str], requested: Optional[str]) -> str:
    """지표 SELECT·쓰기에 쓸 tenant. 신뢰 tenant(aads_internal_tenant_id)가 있고 요청과 같을 때만 통과한다.

    신뢰 tenant 없음, 형식 오류, 요청과 불일치는 모두 ValueError. 요청이 없으면 신뢰 tenant 를 쓴다.
    DB 연결의 읽기 전용 모드는 어느 tenant 를 읽는지 막지 못하므로 이 검증이 유일한 tenant 경계다.
    """
    trusted_id = parse_tenant_uuid(trusted, "신뢰 tenant") if trusted else None
    if not trusted_id:
        raise ValueError("신뢰 tenant(aads_internal_tenant_id)를 확인할 수 없음 — tenant 를 지정해도 수집/조회하지 않는다")
    requested_id = parse_tenant_uuid(requested, "--tenant-id")
    if requested_id is not None and requested_id != trusted_id:
        raise ValueError("--tenant-id 가 신뢰 tenant 와 다름 — 다른 tenant 의 수집/조회는 지원하지 않는다")
    return trusted_id


# ── 날짜·품질 ─────────────────────────────────────────────────────────


def kst_date_of(epoch: float) -> _dt.date:
    return _dt.datetime.fromtimestamp(epoch, tz=KST).date()


def _day_end_kst(day: _dt.date) -> _dt.datetime:
    return _dt.datetime.combine(day + _dt.timedelta(days=1), _dt.time(0, 0), tzinfo=KST)


def date_range(start: _dt.date, end: _dt.date) -> list[_dt.date]:
    if end < start:
        return []
    return [start + _dt.timedelta(days=i) for i in range((end - start).days + 1)]


def collection_quality(day: _dt.date, collected_at: _dt.datetime, source_verified: bool) -> str:
    """수집 증거의 질. 7일 연속 판정에는 on_time 만 쓴다.

    on_time: 그 날이 끝난 뒤(다음 날 00:00 KST 이후) 24시간 안에, 최신성이 확인된 소스로 수집.
    partial_day: 그 날이 끝나기 전에 수집(하루치가 아직 덜 쌓임).
    late_backfill: 하루치가 끝난 뒤 24시간이 지나서 수집(소급 수집 — 그 날의 실측이 아니다).
    unverified_source: 소스 최신성을 확인하지 못함(예: fetch 없이 로컬 ref 사용).
    """
    if not source_verified:
        return QUALITY_UNVERIFIED
    end = _day_end_kst(day)
    if collected_at < end:
        return QUALITY_PARTIAL
    if collected_at >= end + _dt.timedelta(days=1):
        return QUALITY_LATE
    return QUALITY_ON_TIME


def _rate(numerator: int, denominator: int) -> tuple[str, Optional[float]]:
    if denominator <= 0:
        return STATUS_NOT_APPLICABLE, None
    return STATUS_MEASURED, round(numerator / denominator, 6)


# ── 지표 A: 신규 docs/reports 원문 ─────────────────────────────────────


def project_of_path(path: str, default_project: str, known_projects: Iterable[str]) -> str:
    """파일명 `YYYYMMDD_{PROJECT}_…` 의 PROJECT 가 이 tenant 의 알려진 프로젝트일 때만 그 프로젝트로 본다."""
    m = _FILENAME_PROJECT_RE.match(path.rsplit("/", 1)[-1])
    if m and m.group(1) in set(known_projects):
        return m.group(1)
    return default_project


def classify_new_files(
    files: Iterable[dict[str, Any]], default_project: str, known_projects: Iterable[str],
    *, since: Optional[_dt.date] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """git 의 '추가' 기록을 분모 문서와 제외 건으로 나눈다.

    files 항목: path, added_epoch, added_sha256, head_sha256(없으면 None), size, added_blob(선택).
    files 는 창(window)이 아니라 전체 이력이어야 한다 — 최초추가·동일내용 판정이 창 시작일에 따라
    달라지면 같은 날짜의 분모가 창마다 달라진다. since 는 결과 출력만 그 날짜(KST) 이후로 자른다.
    동일내용은 git blob id(없으면 added_sha256)로 본다: 같은 내용이면 같은 blob 이라 본문을 읽지 않는다.
    반환: (분모 문서, 제외 건). 제외 건은 사유를 갖고 날짜별 집계에 남는다.
    """
    known = set(known_projects)
    counted: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    seen_content: set[tuple[str, str]] = set()
    for f in sorted(files, key=lambda x: (x["added_epoch"], x["path"])):
        path = f["path"]
        if not REPORT_PATH_RE.match(path):
            continue
        day = kst_date_of(f["added_epoch"])
        project = project_of_path(path, default_project, known)
        base = {"path": path, "kst_date": day, "project": project}
        visible = since is None or day >= since
        if path in seen_paths:
            if visible:
                excluded.append({**base, "reason": EXCL_READD})
            continue
        seen_paths.add(path)
        if not _MARKDOWN_RE.search(path):
            if visible:
                excluded.append({**base, "reason": EXCL_NON_MARKDOWN})
            continue
        if not f.get("size"):
            if visible:
                excluded.append({**base, "reason": EXCL_EMPTY})
            continue
        key = (project, f.get("added_blob") or f["added_sha256"])
        if key in seen_content:
            if visible:
                excluded.append({**base, "reason": EXCL_DUP_CONTENT})
            continue
        seen_content.add(key)
        if visible:
            counted.append({
                **base, "added_sha256": f["added_sha256"], "head_sha256": f.get("head_sha256"),
            })
    return counted, excluded


def registration_state(doc: dict[str, Any], revisions: Iterable[dict[str, Any]], tenant_id: str) -> str:
    """registered | path_only | unregistered. 다른 tenant·project 의 revision 은 보지 않는다."""
    hashes = {h for h in (doc["added_sha256"], doc.get("head_sha256")) if h}
    path_hit = False
    for r in revisions:
        if str(r["tenant_id"]) != str(tenant_id) or r["project_key"] != doc["project"]:
            continue
        if r["source_path"] != doc["path"]:
            continue
        if r["content_hash"] in hashes:
            return "registered"
        path_hit = True
    return "path_only" if path_hit else "unregistered"


def build_daily_a(
    *, docs: list[dict[str, Any]], excluded: list[dict[str, Any]], revisions: list[dict[str, Any]],
    tenant_id: str, projects: Iterable[str], start: _dt.date, end: _dt.date,
    collected_at: _dt.datetime, source_verified: bool, source_info: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """(project × 날짜) 마다 정확히 한 행. 신규 0건인 날도 행을 만들되 N/A 로 둔다."""
    by_key: dict[tuple[str, _dt.date], list[dict[str, Any]]] = defaultdict(list)
    for d in docs:
        by_key[(d["project"], d["kst_date"])].append(d)
    excl_key: dict[tuple[str, _dt.date], Counter] = defaultdict(Counter)
    for e in excluded:
        excl_key[(e["project"], e["kst_date"])][e["reason"]] += 1

    rows = []
    for project in sorted(set(projects)):
        for day in date_range(start, end):
            todays = by_key.get((project, day), [])
            states = [registration_state(d, revisions, tenant_id) for d in todays]
            registered = sum(1 for s in states if s == "registered")
            path_only = sum(1 for s in states if s == "path_only")
            reasons = dict(excl_key.get((project, day), {}))
            status, rate = _rate(registered, len(todays))
            unreg = [d["path"] for d, s in zip(todays, states) if s != "registered"]
            rows.append({
                "tenant_id": str(tenant_id), "project_key": project, "kst_date": day,
                "metric": METRIC_A, "numerator": registered, "denominator": len(todays),
                "excluded_count": sum(reasons.values()), "excluded_reasons": reasons,
                "rate": rate, "status": status,
                "quality": collection_quality(day, collected_at, source_verified),
                "collected_at": collected_at, "collector_version": COLLECTOR_VERSION,
                "detail": {
                    "path_only": path_only,
                    "unregistered_paths": unreg[:MAX_LISTED_PATHS],
                    "source": source_info or {},
                },
            })
    return rows


# ── 지표 B: canonical_gate_events ─────────────────────────────────────


def build_daily_b(
    *, events: list[dict[str, Any]], tenant_id: str, project: str, start: _dt.date, end: _dt.date,
    collected_at: _dt.datetime, source_verified: bool = True,
) -> list[dict[str, Any]]:
    """게이트 이벤트의 ok 비율. 분모는 그 날 그 프로젝트의 모든 이벤트(ok+missing+unknown).

    canonical_gate_events 에는 tenant 컬럼이 없다. project 로만 거르므로 같은 project 이름을
    여러 tenant 가 쓰면 섞인다 — detail.tenant_scope 에 그 한계를 남긴다.
    """
    per_day: dict[_dt.date, list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        if e.get("project") != project:
            continue
        per_day[e["occurred_at"].astimezone(KST).date()].append(e)
    rows = []
    for day in date_range(start, end):
        todays = per_day.get(day, [])
        verdicts = Counter(e["verdict"] for e in todays)
        entry: dict[str, Counter] = defaultdict(Counter)
        for e in todays:
            entry[e["entrypoint"]][e["verdict"]] += 1
        status, rate = _rate(verdicts.get("ok", 0), len(todays))
        rows.append({
            "tenant_id": str(tenant_id), "project_key": project, "kst_date": day,
            "metric": METRIC_B, "numerator": verdicts.get("ok", 0), "denominator": len(todays),
            "excluded_count": 0, "excluded_reasons": {},
            "rate": rate, "status": status,
            "quality": collection_quality(day, collected_at, source_verified),
            "collected_at": collected_at, "collector_version": COLLECTOR_VERSION,
            "detail": {
                "verdicts": dict(verdicts),
                "by_entrypoint": {k: dict(v) for k, v in sorted(entry.items())},
                "tenant_scope": "project_only_events_table_has_no_tenant_column",
            },
        })
    return rows


# ── 누락·7일 연속 판정 ────────────────────────────────────────────────


def coverage_report(
    rows: Iterable[dict[str, Any]], *, tenant_id: str, project: str, metric: str,
    start: _dt.date, end: _dt.date,
) -> dict[str, Any]:
    """날짜별 수집 상태. 행이 없는 날짜는 not_collected 이며 rate 는 None 이다(0% 아님).

    consecutive_on_time_days: end 에서 거슬러 올라가며 on_time 행이 이어진 일수.
    ready_for_m5: 그 일수가 7 이상. 이것은 '7일 실측 확보' 조건이지 M5 완료 판정이 아니다.
    """
    mine = {
        r["kst_date"]: r for r in rows
        if str(r["tenant_id"]) == str(tenant_id) and r["project_key"] == project and r["metric"] == metric
    }
    days = []
    for day in date_range(start, end):
        r = mine.get(day)
        if r is None:
            days.append({"kst_date": day, "state": STATE_NOT_COLLECTED, "rate": None,
                         "numerator": None, "denominator": None})
        else:
            days.append({"kst_date": day, "state": r["quality"], "status": r["status"],
                         "rate": r["rate"], "numerator": r["numerator"], "denominator": r["denominator"]})
    streak = 0
    for d in reversed(days):
        if d["state"] != QUALITY_ON_TIME:
            break
        streak += 1
    return {
        "metric": metric, "project_key": project, "start": start, "end": end, "days": days,
        "not_collected_dates": [d["kst_date"] for d in days if d["state"] == STATE_NOT_COLLECTED],
        "states": dict(Counter(d["state"] for d in days)),
        "consecutive_on_time_days": streak,
        "ready_for_m5": streak >= REQUIRED_CONSECUTIVE_DAYS,
    }


# ── git 수집 (읽기 전용) ───────────────────────────────────────────────


def _git(repo: str, *args: str, timeout: int = 30, input: Optional[bytes] = None) -> bytes:
    return subprocess.run(
        ["git", "-c", "core.quotepath=false", *args], cwd=repo, capture_output=True,
        timeout=timeout, check=True, input=input,
    ).stdout


def _read_blobs(repo: str, blob_ids: Iterable[str]) -> dict[str, bytes]:
    """`git cat-file --batch` 한 번으로 필요한 blob 만 읽는다(파일마다 git show 를 띄우지 않는다)."""
    ids = sorted(set(blob_ids))
    if not ids:
        return {}
    out = _git(repo, "cat-file", "--batch", input=("\n".join(ids) + "\n").encode(), timeout=120)
    blobs: dict[str, bytes] = {}
    pos = 0
    for oid in ids:
        nl = out.index(b"\n", pos)
        header = out[pos:nl].decode().split()
        if len(header) != 3 or header[1] != "blob":
            raise RuntimeError(f"blob 을 읽지 못함: {oid} ({' '.join(header)})")
        size = int(header[2])
        blobs[header[0]] = out[nl + 1:nl + 1 + size]
        pos = nl + 1 + size + 1
    return blobs


def _blob_sizes(repo: str, blob_ids: Iterable[str]) -> dict[str, int]:
    ids = sorted(set(blob_ids))
    if not ids:
        return {}
    out = _git(repo, "cat-file", "--batch-check", input=("\n".join(ids) + "\n").encode(), timeout=120)
    sizes = {}
    for line in out.decode().splitlines():
        parts = line.split()
        if len(parts) != 3 or parts[1] != "blob":
            raise RuntimeError(f"blob 크기를 읽지 못함: {line}")
        sizes[parts[0]] = int(parts[2])
    return sizes


def collect_report_history(repo: str, ref: str, since: _dt.date) -> list[dict[str, Any]]:
    """ref 의 docs/reports '추가(A)' 전체 이력. 이름 바꿈(R)은 신규가 아니다.

    since(KST 날짜) 이후 추가분만 added_sha256·head_sha256 을 채운다(등록 판정에 필요한 본문 해시).
    그 이전 이력은 최초추가 경로·동일내용(blob id) 대조용이라 본문을 읽지 않는다.
    git 호출 수는 파일 수와 무관하다: log 1 + ls-tree 1 + cat-file 2.
    얕은 clone 은 최초추가를 알 수 없으므로 실패한다(재추가를 신규로 오인하지 않는다).
    """
    if _git(repo, "rev-parse", "--is-shallow-repository").strip() == b"true":
        raise RuntimeError("shallow 저장소: 최초추가 이력을 알 수 없어 신규 판정을 할 수 없다")
    out = _git(repo, "log", ref, "--diff-filter=A", "--raw", "--no-abbrev",
               "--format=@@%H\t%ct", "--", "docs/reports", timeout=120).decode("utf-8")
    adds, commit, epoch = [], None, 0
    for line in out.splitlines():
        if line.startswith("@@"):
            commit, ts = line[2:].split("\t")
            epoch = int(ts)
        elif line.startswith(":") and commit:
            meta, path = line.split("\t", 1)
            adds.append((commit, epoch, meta.split()[3], path))
    sizes = _blob_sizes(repo, (blob for _, _, blob, _ in adds))
    head_blobs: dict[str, str] = {}
    tree = _git(repo, "ls-tree", "-r", "-z", ref, "--", "docs/reports", timeout=60).decode("utf-8")
    for entry in tree.split("\0"):
        if entry:
            meta, path = entry.split("\t", 1)
            head_blobs[path] = meta.split()[2]
    window = [a for a in adds if kst_date_of(a[1]) >= since]
    content = _read_blobs(repo, [a[2] for a in window] + [head_blobs[a[3]] for a in window if a[3] in head_blobs])
    files = []
    for commit, epoch, blob, path in adds:
        in_window = kst_date_of(epoch) >= since
        head_blob = head_blobs.get(path)
        files.append({
            "path": path, "added_epoch": epoch, "added_commit": commit, "added_blob": blob,
            "size": sizes[blob], "in_window": in_window,
            "added_sha256": hashlib.sha256(content[blob]).hexdigest() if in_window else None,
            "head_sha256": (hashlib.sha256(content[head_blob]).hexdigest()
                            if in_window and head_blob else None),
        })
    return files


def collect_new_reports(repo: str, ref: str, since: _dt.date) -> list[dict[str, Any]]:
    """since(KST) 이후 추가분만 돌려준다. 신규 판정(재추가·동일내용)에는 collect_report_history 를 쓴다."""
    return [f for f in collect_report_history(repo, ref, since) if f["in_window"]]


# ── SQL (읽기) / upsert (쓰기, --apply 일 때만) ─────────────────────────

KNOWN_PROJECTS_SQL = (
    "SELECT DISTINCT project_key FROM project_document_heads WHERE tenant_id = $1::uuid"
)
REVISIONS_BY_PATH_SQL = (
    "SELECT tenant_id::text AS tenant_id, project_key, source_path, content_hash "
    "FROM project_document_revisions WHERE tenant_id = $1::uuid AND source_path = ANY($2::text[])"
)
GATE_EVENTS_SQL = (
    "SELECT occurred_at, entrypoint, project, verdict FROM canonical_gate_events "
    "WHERE occurred_at >= $1 AND occurred_at < $2 AND project = $3"
)
DAILY_ROWS_SQL = (
    "SELECT tenant_id::text AS tenant_id, project_key, kst_date, metric, numerator, denominator, "
    "rate::float8 AS rate, status, quality FROM rdoc_registration_daily "
    "WHERE tenant_id = $1::uuid AND project_key = $2 AND metric = $3 AND kst_date BETWEEN $4 AND $5"
)
# 이미 on_time 으로 남은 날을 늦은/미검증 재수집이 덮어쓰지 못한다. first_collected_at 은 갱신하지 않는다.
UPSERT_SQL = """
INSERT INTO rdoc_registration_daily
    (tenant_id, project_key, kst_date, metric, numerator, denominator, excluded_count,
     excluded_reasons, rate, status, quality, first_collected_at, collected_at,
     collector_version, detail)
VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11, $12, $12, $13, $14::jsonb)
ON CONFLICT (tenant_id, project_key, kst_date, metric) DO UPDATE SET
    numerator = EXCLUDED.numerator, denominator = EXCLUDED.denominator,
    excluded_count = EXCLUDED.excluded_count, excluded_reasons = EXCLUDED.excluded_reasons,
    rate = EXCLUDED.rate, status = EXCLUDED.status, quality = EXCLUDED.quality,
    collected_at = EXCLUDED.collected_at, collector_version = EXCLUDED.collector_version,
    detail = EXCLUDED.detail
WHERE rdoc_registration_daily.quality <> 'on_time' OR EXCLUDED.quality = 'on_time'
"""


async def upsert_rows(conn: Any, rows: list[dict[str, Any]]) -> int:
    import json

    n = 0
    for r in rows:
        await conn.execute(
            UPSERT_SQL, r["tenant_id"], r["project_key"], r["kst_date"], r["metric"],
            r["numerator"], r["denominator"], r["excluded_count"],
            json.dumps(r["excluded_reasons"], ensure_ascii=False), r["rate"], r["status"],
            r["quality"], r["collected_at"], r["collector_version"],
            json.dumps(r["detail"], ensure_ascii=False, default=str),
        )
        n += 1
    return n
