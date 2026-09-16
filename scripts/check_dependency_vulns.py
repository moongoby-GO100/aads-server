#!/usr/bin/env python3
"""설치된 의존성을 OSV.dev 에 물어 알려진 취약점을 보고한다.

왜 OSV.dev 인가. 키가 필요 없고(등록·과금·rate limit 협상 없음), Python·npm·Go 를
한 API 로 묻는다. 2026-09-17 서버에서 `POST https://api.osv.dev/v1/query` HTTP 200
도달을 확인했다. 별도 스캐너 바이너리를 설치하지 않아도 되므로 게이트가 외부
바이너리 설치 상태에 의존하지 않는다 — ruff 가 없는 줄 모르고 반년을 보낸 것과
같은 실패를 만들지 않으려는 선택이다.

선언 버전(`>=1.2.0`)이 아니라 **실제 설치 버전**을 묻는다. 선언 범위로는 지금
무엇이 돌고 있는지 알 수 없고, 취약점은 돌고 있는 것에만 의미가 있다.

사용:
    python3 scripts/check_dependency_vulns.py              # 요약
    python3 scripts/check_dependency_vulns.py --json       # 기계 판독용
    python3 scripts/check_dependency_vulns.py --severity HIGH  # HIGH 이상만 exit 1

종료코드: 0=취약점 없음 / 1=임계치 이상 발견 / 2=실행 불가(조회 실패 등)
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

OSV_BATCH = "https://api.osv.dev/v1/querybatch"
OSV_VULN = "https://api.osv.dev/v1/vulns/"
TIMEOUT = 30
SEVERITY_ORDER = {"": 0, "LOW": 1, "MODERATE": 2, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


def load_acks(path: str) -> tuple[list[dict], list[dict]]:
    """예외 목록을 (유효, 만료) 로 나눠 돌려준다.

    왜 만료를 강제하는가. 예외는 한 번 적으면 영원히 남는다 — 그러면 일일 점검은
    빨간 줄만 늘어난 채 아무도 안 보는 보고서가 되거나, 예외에 묻혀 새 취약점을
    놓친다. 만료가 지난 예외는 **무시하고 취약점을 되살린다.** 다시 판단하게
    만드는 것이 목적이다.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            items = json.load(fh).get("acknowledgements") or []
    except FileNotFoundError:
        return [], []
    except (OSError, json.JSONDecodeError) as exc:
        print(f"WARN: 예외 목록 읽기 실패 ({exc}) — 예외 없이 진행", file=sys.stderr)
        return [], []

    today = datetime.date.today().isoformat()
    live, expired = [], []
    for it in items:
        # 만료일이 없는 예외는 받지 않는다. 영구 예외는 예외가 아니라 방치다.
        if not it.get("expires"):
            print(f"WARN: expires 없는 예외 무시: {it.get('package')} {it.get('id')}", file=sys.stderr)
            continue
        (live if it["expires"] >= today else expired).append(it)
    return live, expired


def is_acked(finding: dict, acks: list[dict]) -> dict | None:
    for a in acks:
        if a.get("package") != finding["package"]:
            continue
        if a.get("ecosystem") and a["ecosystem"] != finding["ecosystem"]:
            continue
        if a.get("id") not in (None, "*", finding["id"]):
            continue
        if a.get("version") and a["version"] != finding["version"]:
            continue
        return a
    return None


def _post(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def collect_pypi(container: str) -> list[tuple[str, str]]:
    """실행 중인 컨테이너에 실제로 설치된 Python 패키지."""
    try:
        out = subprocess.run(
            ["docker", "exec", container, "python3", "-m", "pip", "list", "--format=json"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"WARN: pip list 실패 ({exc}) — PyPI 생태계 건너뜀", file=sys.stderr)
        return []
    if out.returncode != 0:
        print(f"WARN: pip list rc={out.returncode} — PyPI 생태계 건너뜀", file=sys.stderr)
        return []
    try:
        return [(p["name"], p["version"]) for p in json.loads(out.stdout)]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        print(f"WARN: pip list 파싱 실패 ({exc})", file=sys.stderr)
        return []


def collect_npm(lockfile: str) -> list[tuple[str, str]]:
    """package-lock.json 의 확정 버전. lockfile 이 없으면 빈 목록."""
    try:
        with open(lockfile, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"WARN: {lockfile} 읽기 실패 ({exc}) — npm 생태계 건너뜀", file=sys.stderr)
        return []

    pkgs: list[tuple[str, str]] = []
    for path, meta in (data.get("packages") or {}).items():
        if not path or not isinstance(meta, dict):
            continue  # "" 는 루트 프로젝트 자신
        name = meta.get("name") or path.split("node_modules/")[-1]
        version = meta.get("version")
        if name and version:
            pkgs.append((name, version))
    return pkgs


def query_osv(packages: list[tuple[str, str]], ecosystem: str) -> dict[int, list[str]]:
    """querybatch 는 vuln id 만 준다. 인덱스→id 목록."""
    found: dict[int, list[str]] = {}
    # 배치가 너무 크면 서버가 잘라내므로 나눠 던진다.
    CHUNK = 200
    for start in range(0, len(packages), CHUNK):
        chunk = packages[start : start + CHUNK]
        payload = {
            "queries": [
                {"package": {"name": n, "ecosystem": ecosystem}, "version": v} for n, v in chunk
            ]
        }
        try:
            res = _post(OSV_BATCH, payload)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"ERROR: OSV 조회 실패 ({ecosystem}, offset={start}): {exc}", file=sys.stderr)
            raise
        for i, r in enumerate(res.get("results") or []):
            ids = [v["id"] for v in (r.get("vulns") or []) if v.get("id")]
            if ids:
                found[start + i] = ids
    return found


def vuln_detail(vid: str, cache: dict[str, dict]) -> dict:
    if vid in cache:
        return cache[vid]
    try:
        d = _get(OSV_VULN + vid)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        d = {}
    cache[vid] = d
    return d


def worst_severity(detail: dict) -> str:
    """OSV 는 severity 표기가 통일돼 있지 않다. database_specific 을 먼저 본다."""
    sev = (detail.get("database_specific") or {}).get("severity") or ""
    if sev:
        return str(sev).upper()
    for aff in detail.get("affected") or []:
        s = (aff.get("database_specific") or {}).get("severity")
        if s:
            return str(s).upper()
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--container", default="aads-server")
    ap.add_argument(
        "--npm-lock", default="/root/aads/aads-dashboard/package-lock.json"
    )
    ap.add_argument(
        "--severity",
        default="",
        help="이 등급 이상이면 exit 1 (LOW/MODERATE/HIGH/CRITICAL). 생략 시 1건이라도 있으면 exit 1",
    )
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "--ack-file",
        default=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "security",
            "vuln_acknowledgements.json",
        ),
        help="고쳐진 버전이 없어 당장 올릴 수 없는 항목의 예외 목록(만료일 필수)",
    )
    args = ap.parse_args()

    threshold = SEVERITY_ORDER.get(args.severity.upper(), 0)
    acks, expired_acks = load_acks(args.ack_file)
    ecosystems = [
        ("PyPI", collect_pypi(args.container)),
        ("npm", collect_npm(args.npm_lock)),
    ]

    findings: list[dict] = []
    cache: dict[str, dict] = {}
    scanned = 0

    for eco, pkgs in ecosystems:
        if not pkgs:
            continue
        scanned += len(pkgs)
        try:
            hits = query_osv(pkgs, eco)
        except Exception:
            return 2
        for idx, ids in hits.items():
            name, version = pkgs[idx]
            for vid in ids:
                d = vuln_detail(vid, cache)
                findings.append(
                    {
                        "ecosystem": eco,
                        "package": name,
                        "version": version,
                        "id": vid,
                        "severity": worst_severity(d),
                        "summary": (d.get("summary") or "").strip()[:160],
                        "url": f"https://osv.dev/vulnerability/{vid}",
                    }
                )

    findings.sort(key=lambda f: -SEVERITY_ORDER.get(f["severity"], 0))
    for f in findings:
        a = is_acked(f, acks)
        f["acknowledged"] = bool(a)
        f["ack_reason"] = (a or {}).get("reason", "")
        f["ack_expires"] = (a or {}).get("expires", "")

    open_findings = [f for f in findings if not f["acknowledged"]]
    acked_findings = [f for f in findings if f["acknowledged"]]
    blocking = [f for f in open_findings if SEVERITY_ORDER.get(f["severity"], 0) >= threshold]

    if args.json:
        print(
            json.dumps(
                {
                    "scanned": scanned,
                    "findings": findings,
                    "open": len(open_findings),
                    "acknowledged": len(acked_findings),
                    "expired_acks": expired_acks,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(
            f"OSV.dev 점검 — 패키지 {scanned}개, 취약점 {len(findings)}건 "
            f"(조치대상 {len(open_findings)} / 예외 {len(acked_findings)})"
        )
        if not open_findings:
            print("  ✅ 조치 대상 취약점 없음")
        for f in open_findings:
            sev = f["severity"] or "UNSPEC"
            print(f"  [{sev:8}] {f['ecosystem']:5} {f['package']}=={f['version']}  {f['id']}")
            if f["summary"]:
                print(f"             {f['summary']}")
            print(f"             {f['url']}")
        for f in acked_findings:
            sev = f["severity"] or "UNSPEC"
            print(
                f"  [예외 {sev:8}] {f['package']}=={f['version']} {f['id']} "
                f"(만료 {f['ack_expires']}) {f['ack_reason'][:80]}"
            )
        for a in expired_acks:
            print(
                f"  ⚠️ 만료된 예외 — 재판단 필요: {a.get('package')} {a.get('id')} "
                f"(만료일 {a.get('expires')})"
            )

    if scanned == 0:
        print("ERROR: 점검한 패키지가 0개 — 수집 경로를 확인하라", file=sys.stderr)
        return 2
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
