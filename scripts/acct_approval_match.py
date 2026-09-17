#!/usr/bin/env python3
"""ACCT 승인번호 매칭 배치 (ACCT-LAYOUT-002 후속).

카드 매입전표에 증빙의 승인번호를 (사업자번호, 일자, 금액) 3키로 붙이고
매칭률을 측정한다. **읽기 전용** — ACCT DB에 아무것도 쓰지 않는다.

증빙 원천은 하드코딩하지 않고 `field_key` 존재로 자동 탐색한다. 스냅샷이
새로 들어와도 같은 배치가 그대로 돈다.

  tax            전자세금계산서 (no_eserotax, 24자리 국세청 승인번호)
  cash           현금영수증     (approval_no)
  card_statement 카드사 명세서  (approval_no + 카드번호)  ※ 적재되면 자동 인식

실행:
    docker exec aads-server python3 /app/scripts/acct_approval_match.py
    docker exec aads-server python3 /app/scripts/acct_approval_match.py \
        --days 3 --json /tmp/acct_match.json --unmatched 20

종료코드: 0=정상 실행(매칭률과 무관) / 2=조회 실패·데이터 없음
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api.ceo_chat_tools_db import query_acct_database  # noqa: E402

# 증빙 원천 정의 — 승인번호 필드와 사업자번호 필드가 원천마다 다르다.
EVIDENCE_SOURCES: Dict[str, Dict[str, str]] = {
    "tax": {
        "label": "전자세금계산서",
        "approval_key": "no_eserotax",
        "bizno_key": "no_bisocial",
    },
    "cash": {
        "label": "현금영수증",
        "approval_key": "approval_no",
        "bizno_key": "bisocial_no",
    },
}

CARD_APPROVAL_KEYS = ("approval_no", "no_cash")
CARD_NUMBER_KEYS = ("id_sa", "imsi_f")


async def fetch(sql: str) -> List[Dict[str, Any]]:
    result = await query_acct_database(sql)
    if isinstance(result, dict) and result.get("error"):
        print(f"[ERROR] ACCT 조회 실패: {result['error']}", file=sys.stderr)
        raise SystemExit(2)
    return list(result.get("rows") or [])


def _digits(expr: str) -> str:
    return f"regexp_replace(coalesce({expr}, ''), '[^0-9]', '', 'g')"


def _pivot(source_expr: str, bizno_key: str, approval_key: Optional[str]) -> str:
    """atom_record EAV → (rec_idx, 일자, 금액, 사업자번호[, 승인번호]) pivot."""
    approval = (
        f",\n           max(raw_value) FILTER (WHERE field_key = '{approval_key}') AS appr"
        if approval_key
        else ""
    )
    biz = _digits(f"max(raw_value) FILTER (WHERE field_key = '{bizno_key}')")
    return (
        "    SELECT rec_idx,\n"
        "           max(raw_value) FILTER (WHERE field_key = 'da_sbook') AS d,\n"
        "           max(num_value) FILTER (WHERE field_key = 'mn_total') AS amt,\n"
        f"           {biz} AS biz,\n"
        "           max(raw_value) FILTER (WHERE field_key = 'nm_trade') AS vendor,\n"
        "           max(raw_value) FILTER (WHERE field_key = 'nm_remark') AS remark,\n"
        "           max(raw_value) FILTER (WHERE field_key = 'ty_jungstat') AS st"
        f"{approval}\n"
        f"      FROM atom_record\n     WHERE source_file_id IN ({source_expr})\n"
        "     GROUP BY rec_idx"
    )


async def discover_sources() -> Dict[str, List[int]]:
    """승인번호 필드를 가진 스냅샷 파일을 원천별로 찾는다."""
    found: Dict[str, List[int]] = {}
    for name, spec in EVIDENCE_SOURCES.items():
        rows = await fetch(
            "SELECT source_file_id, count(*) AS n FROM atom_record\n"
            f" WHERE field_key = '{spec['approval_key']}'\n"
            " GROUP BY 1 ORDER BY n DESC LIMIT 20"
        )
        found[name] = [int(r["source_file_id"]) for r in rows]
    return found


async def latest_card_snapshot() -> Optional[int]:
    rows = await fetch(
        "SELECT id FROM source_file\n"
        " WHERE domain = 'wehago' AND abs_path ILIKE '%card%'\n"
        " ORDER BY mtime_kst DESC LIMIT 1"
    )
    return int(rows[0]["id"]) if rows else None


async def card_statement_readiness() -> Dict[str, Any]:
    """카드사 명세서 원본이 파싱·적재됐는지 확인한다 (매칭의 선행 조건)."""
    rows = await fetch(
        "SELECT count(DISTINCT sf.id) AS files, count(ar.id) AS atom_rows\n"
        "  FROM source_file sf LEFT JOIN atom_record ar ON ar.source_file_id = sf.id\n"
        " WHERE sf.domain = 'card'"
    )
    row = rows[0] if rows else {}
    keys = ", ".join(f"'{k}'" for k in CARD_APPROVAL_KEYS + CARD_NUMBER_KEYS)
    card_rows = await fetch(
        "SELECT count(*) AS n FROM atom_record ar\n"
        "  JOIN source_file sf ON sf.id = ar.source_file_id\n"
        f" WHERE sf.domain = 'card' AND ar.field_key IN ({keys})"
    )
    return {
        "card_domain_files": int(row.get("files") or 0),
        "card_domain_atom_rows": int(row.get("atom_rows") or 0),
        "card_approval_fields": int((card_rows[0] if card_rows else {}).get("n") or 0),
    }


async def match(card_file: int, sources: Dict[str, List[int]], days: int,
                unmatched_limit: int) -> Dict[str, Any]:
    card_cte = _pivot(str(card_file), "bisocial_no", None)

    per_source: Dict[str, Any] = {}
    for name, files in sources.items():
        spec = EVIDENCE_SOURCES[name]
        if not files:
            per_source[name] = {
                "label": spec["label"],
                "evidence_rows": 0,
                "matched_3key": 0,
                "matched_3key_window": 0,
                "matched_date_amount": 0,
                "note": "해당 승인번호 필드를 가진 스냅샷이 없음",
            }
            continue

        ev_cte = _pivot(", ".join(str(f) for f in files), spec["bizno_key"],
                        spec["approval_key"])
        rows = await fetch(
            f"WITH c AS (\n{card_cte}\n), e AS (\n{ev_cte}\n)\n"
            "SELECT (SELECT count(*) FROM e WHERE appr IS NOT NULL) AS evidence_rows,\n"
            "       count(*) FILTER (WHERE EXISTS (\n"
            "           SELECT 1 FROM e WHERE e.biz = c.biz AND e.biz <> ''\n"
            "             AND e.amt = c.amt AND e.d = c.d)) AS matched_3key,\n"
            "       count(*) FILTER (WHERE EXISTS (\n"
            "           SELECT 1 FROM e WHERE e.biz = c.biz AND e.biz <> ''\n"
            "             AND e.amt = c.amt\n"
            "             AND abs(to_date(e.d, 'YYYYMMDD') - to_date(c.d, 'YYYYMMDD'))"
            f" <= {days})) AS matched_3key_window,\n"
            "       count(*) FILTER (WHERE EXISTS (\n"
            "           SELECT 1 FROM e WHERE e.amt = c.amt AND e.d = c.d))"
            " AS matched_date_amount\n"
            "  FROM c"
        )
        row = rows[0] if rows else {}
        per_source[name] = {
            "label": spec["label"],
            "evidence_rows": int(row.get("evidence_rows") or 0),
            "matched_3key": int(row.get("matched_3key") or 0),
            "matched_3key_window": int(row.get("matched_3key_window") or 0),
            "matched_date_amount": int(row.get("matched_date_amount") or 0),
        }

    totals = (
        await fetch(
            f"WITH c AS (\n{card_cte}\n)\n"
            "SELECT count(*) AS total,\n"
            "       count(*) FILTER (WHERE st = '3') AS excluded,\n"
            "       count(*) FILTER (WHERE biz = '' OR biz IS NULL) AS no_bizno,\n"
            "       count(*) FILTER (WHERE amt IS NULL) AS no_amount\n"
            "  FROM c"
        )
    )[0]

    unmatched: List[Dict[str, Any]] = []
    if unmatched_limit > 0:
        live = [f for files in sources.values() for f in files]
        if live:
            # 원천 전체를 합쳐 한 번이라도 걸리면 매칭으로 본다.
            union = " UNION ALL ".join(
                f"SELECT * FROM ({_pivot(', '.join(str(f) for f in files), EVIDENCE_SOURCES[n]['bizno_key'], EVIDENCE_SOURCES[n]['approval_key'])}) AS s_{n}"
                for n, files in sources.items()
                if files
            )
            unmatched = await fetch(
                f"WITH c AS (\n{card_cte}\n), e AS (\n{union}\n)\n"
                "SELECT c.d, c.amt, c.biz, c.vendor, c.remark\n"
                "  FROM c WHERE NOT EXISTS (\n"
                "    SELECT 1 FROM e WHERE e.biz = c.biz AND e.biz <> ''\n"
                "      AND e.amt = c.amt\n"
                "      AND abs(to_date(e.d, 'YYYYMMDD') - to_date(c.d, 'YYYYMMDD'))"
                f" <= {days})\n"
                " ORDER BY abs(c.amt) DESC NULLS LAST"
                f" LIMIT {unmatched_limit}"
            )

    total = int(totals.get("total") or 0)
    best = max((s.get("matched_3key_window", 0) for s in per_source.values()), default=0)
    combined = sum(s.get("matched_3key_window", 0) for s in per_source.values())
    return {
        "card_source_file_id": card_file,
        "date_window_days": days,
        "card_totals": {
            "total": total,
            "excluded_status3": int(totals.get("excluded") or 0),
            "no_bizno": int(totals.get("no_bizno") or 0),
            "no_amount": int(totals.get("no_amount") or 0),
        },
        "per_source": per_source,
        "matched_total_upper_bound": combined,
        "match_rate_percent": round(combined / total * 100, 2) if total else 0.0,
        "best_single_source": best,
        "unmatched_top": unmatched,
    }


def render(report: Dict[str, Any], readiness: Dict[str, Any]) -> None:
    t = report["card_totals"]
    print("=" * 72)
    print("ACCT 승인번호 매칭 배치 결과")
    print("=" * 72)
    print(f"카드 스냅샷 source_file_id : {report['card_source_file_id']}")
    print(f"일자 허용 범위             : ±{report['date_window_days']}일")
    print(f"카드 전표 총건             : {t['total']:,}건"
          f" (제외상태 {t['excluded_status3']:,} / 사업자번호 없음 {t['no_bizno']:,})")
    print("-" * 72)
    for name, s in report["per_source"].items():
        print(f"[{name}] {s['label']}")
        print(f"   증빙 승인번호 보유    : {s['evidence_rows']:,}건")
        print(f"   3키 정확 매칭         : {s['matched_3key']:,}건")
        print(f"   3키 + 일자허용 매칭   : {s['matched_3key_window']:,}건")
        print(f"   일자+금액만 매칭      : {s['matched_date_amount']:,}건")
        if s.get("note"):
            print(f"   비고                  : {s['note']}")
    print("-" * 72)
    print(f"매칭률(상한)               : {report['match_rate_percent']}%"
          f" ({report['matched_total_upper_bound']:,}/{t['total']:,})")
    print("-" * 72)
    print("카드사 명세서 적재 상태 (매칭의 선행 조건)")
    print(f"   card 도메인 파일       : {readiness['card_domain_files']:,}건")
    print(f"   파싱된 atom_record     : {readiness['card_domain_atom_rows']:,}행")
    print(f"   카드 승인번호/카드번호 필드: {readiness['card_approval_fields']:,}행")
    if readiness["card_domain_atom_rows"] == 0:
        print("   ⚠️ 카드사 명세서 원본이 파싱되지 않았다. 카드 승인번호 매칭은")
        print("      명세서 적재가 선행돼야 한다 — 이 배치는 적재 즉시 그대로 동작한다.")
    if report["unmatched_top"]:
        print("-" * 72)
        print(f"미매칭 상위 {len(report['unmatched_top'])}건 (금액순)")
        for r in report["unmatched_top"]:
            amt = r.get("amt")
            amt_s = f"{int(float(amt)):,}" if amt is not None else "-"
            print(f"   {r.get('d','')} {amt_s:>14} {str(r.get('vendor') or '')[:22]:22}"
                  f" {str(r.get('remark') or '')[:30]}")
    print("=" * 72)


async def main() -> int:
    ap = argparse.ArgumentParser(description="ACCT 승인번호 매칭 배치 (읽기 전용)")
    ap.add_argument("--card-file", type=int, default=None,
                    help="카드 전표 스냅샷 source_file_id (기본: 최신 card 스냅샷)")
    ap.add_argument("--days", type=int, default=0,
                    help="일자 허용 범위(일). 기본 0 = 같은 날짜만")
    ap.add_argument("--unmatched", type=int, default=10,
                    help="미매칭 상위 N건 출력 (기본 10)")
    ap.add_argument("--json", dest="json_path", default=None,
                    help="결과 JSON 저장 경로")
    args = ap.parse_args()

    card_file = args.card_file or await latest_card_snapshot()
    if not card_file:
        print("[ERROR] 카드 전표 스냅샷을 찾지 못했습니다", file=sys.stderr)
        return 2

    sources = await discover_sources()
    readiness = await card_statement_readiness()
    report = await match(card_file, sources, max(0, args.days), max(0, args.unmatched))
    report["card_statement_readiness"] = readiness

    render(report, readiness)

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fp:
            json.dump(report, fp, ensure_ascii=False, indent=2)
        print(f"JSON 저장: {args.json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
